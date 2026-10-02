"""Release regressions: commit ordering, crash recovery and concurrency."""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select, text

from src.channels import register
from src.channels.base import Delivery
from src.database.db import db_session
from src.database.delivery_models import OutboundJob
from src.database.models import Message, Ticket, User
from src.services import intake_service, outbound, sla_service
from tests.conftest import RecordingChannel, next_tg_id


async def conversation(channel="telegram"):
    address = str(next_tg_id())
    async with db_session(write=True) as session:
        incoming = await intake_service.receive(session, intake_service.Inbound(
            channel=channel, address=address, text="Please help",
        ))
    return incoming.ticket_id, address


async def jobs_for(tid):
    async with db_session() as session:
        return list((await session.execute(select(OutboundJob).join(
            Message, Message.id == OutboundJob.message_id).where(Message.ticket_id == tid)
        )).scalars())


async def test_concurrent_intake_preserves_every_message_in_one_ticket(initialized_db):
    address = str(next_tg_id())
    async def arrive(i):
        async with db_session() as session:
            return await intake_service.receive(session, intake_service.Inbound(
                channel="widget", address=address, text=f"message {i}",
            ))
    results = await asyncio.gather(*(arrive(i) for i in range(8)))
    assert len({r.ticket_id for r in results}) == 1
    async with db_session() as session:
        messages = await intake_service.conversation(session, results[0].ticket_id)
        assert {m.text for m in messages} == {f"message {i}" for i in range(8)}


async def test_intent_commit_failure_never_calls_provider(initialized_db, channel, monkeypatch):
    tid, address = await conversation()
    real = outbound.db_session
    @asynccontextmanager
    async def broken(**kwargs):
        async with real(**kwargs) as session:
            yield session
            raise RuntimeError("database commit failed")
    monkeypatch.setattr(outbound, "db_session", broken)
    with pytest.raises(RuntimeError, match="commit failed"):
        await outbound.say(ticket_id=tid, channel="telegram", address=address, text="Reply")
    assert channel.sent == 0
    assert await jobs_for(tid) == []


async def test_concurrent_delivery_claims_send_exactly_once(initialized_db, channel):
    tid, address = await conversation()
    async with db_session(write=True) as session:
        job = await outbound.enqueue(session, ticket_id=tid, channel="telegram", address=address, text="Reply")
        jid = job.id
    await asyncio.gather(*(outbound.deliver(jid) for _ in range(5)))
    assert channel.sent == 1
    assert (await jobs_for(tid))[0].status == "delivered"


async def test_crash_after_provider_acceptance_requires_explicit_retry(initialized_db, channel, monkeypatch):
    tid, address = await conversation()
    real = outbound.db_session
    calls = 0
    @asynccontextmanager
    async def fail_after_send(**kwargs):
        nonlocal calls
        calls += 1
        async with real(**kwargs) as session:
            yield session
            if calls == 3:
                raise RuntimeError("commit after send failed")
    with monkeypatch.context() as patch:
        patch.setattr(outbound, "db_session", fail_after_send)
        with pytest.raises(RuntimeError, match="after send"):
            await outbound.say(ticket_id=tid, channel="telegram", address=address, text="Reply")
    assert channel.sent == 1
    job = (await jobs_for(tid))[0]
    assert job.status == "sending"
    async with db_session(write=True) as session:
        saved = await session.get(OutboundJob, job.id)
        saved.started_at = datetime.utcnow() - timedelta(hours=1)
    await outbound.run_once()
    # The worker also recovers pending jobs left by other scenarios in the
    # shared test DB. Only this customer's accepted reply must not be resent.
    assert [body for recipient, body in channel.texts if recipient == address] == ["Reply"]
    assert (await jobs_for(tid))[0].status == "uncertain"
    assert not (await outbound.retry(job.id)).ok


async def test_rate_limit_is_durable_and_retried(initialized_db, monkeypatch):
    recorder = RecordingChannel()
    register(recorder)
    original = recorder.send_text
    attempts = 0
    async def limited(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return Delivery.failed("e_send_rate_limited", retry_after=1)
        return await original(*args, **kwargs)
    monkeypatch.setattr(recorder, "send_text", limited)
    tid, address = await conversation()
    first = await outbound.say(ticket_id=tid, channel="telegram", address=address, text="Reply")
    assert not first.ok
    job = (await jobs_for(tid))[0]
    assert job.status == "retry"
    async with db_session(write=True) as session:
        saved = await session.get(OutboundJob, job.id)
        saved.next_attempt_at = datetime.utcnow() - timedelta(seconds=1)
    await outbound.deliver(job.id)
    assert recorder.sent == 1
    assert (await jobs_for(tid))[0].attempts == 2


async def test_widget_delivery_is_committed_transcript_not_adapter_call(initialized_db):
    class Widget(RecordingChannel):
        name = "widget"
        async def send_text(self, *args, **kwargs):
            raise AssertionError("Widget delivery must not depend on an external send")
    register(Widget())
    tid, address = await conversation("widget")
    assert (await outbound.say(ticket_id=tid, channel="widget", address=address, text="Reply")).ok
    async with db_session() as session:
        assert [m.text for m in await intake_service.conversation(session, tid)] == ["Please help", "Reply"]


async def test_failed_reply_is_not_visible_to_customer(initialized_db):
    register(RecordingChannel(fail_on=1))
    tid, address = await conversation()
    await outbound.say(ticket_id=tid, channel="telegram", address=address, text="Unsent")
    assert (await jobs_for(tid))[0].status == "failed"
    async with db_session() as session:
        assert [m.text for m in await intake_service.conversation(session, tid)] == ["Please help"]


async def test_deleted_team_preserves_customer_and_ticket_integrity(initialized_db):
    from src.database.models import Team
    from src.services.team_service import delete_team
    tid, _ = await conversation()
    async with db_session(write=True) as session:
        team = Team(name=f"release-{next_tg_id()}")
        session.add(team)
        await session.flush()
        ticket = await session.get(Ticket, tid)
        user = await session.get(User, ticket.user_id)
        user.team_id = ticket.team_id = team.id
        team_id, user_id = team.id, user.id
    async with db_session(write=True) as session:
        await delete_team(session, team_id)
    async with db_session() as session:
        assert (await session.get(User, user_id)).team_id is None
        assert (await session.get(Ticket, tid)).team_id is None


async def test_reply_rearms_sla_and_user_reply_leaves_waiting(initialized_db, channel):
    tid, address = await conversation()
    async with db_session(write=True) as session:
        ticket = await session.get(Ticket, tid)
        ticket.sla_breached_at = datetime.utcnow()
    await outbound.say(ticket_id=tid, channel="telegram", address=address, text="Reply", sender_id=next_tg_id())
    async with db_session(write=True) as session:
        ticket = await session.get(Ticket, tid)
        assert ticket.sla_breached_at is None
        assert ticket.stage == "waiting"
    async with db_session(write=True) as session:
        result = await intake_service.receive(session, intake_service.Inbound(channel="telegram", address=address, text="More"))
        assert result.ticket_id == tid
        ticket = await session.get(Ticket, tid)
        assert ticket.stage == "in_progress"


async def test_failed_sla_policy_is_retried(initialized_db, monkeypatch):
    tid, _ = await conversation()
    async def policy():
        return {"notify": False, "return_to_pool": True}
    async def breaches():
        async with db_session() as session:
            ticket = await session.get(Ticket, tid)
            return [] if ticket.sla_breached_at else [{"id": tid, "user_label": "Visitor", "waited_seconds": 900, "assigned_admin_id": None}]
    monkeypatch.setattr(sla_service, "get_policy", policy)
    monkeypatch.setattr(sla_service, "find_new_breaches", breaches)
    real_mark = sla_service.mark_handled
    async def fail_mark(ids):
        raise RuntimeError('database unavailable')
    monkeypatch.setattr(sla_service, 'mark_handled', fail_mark)
    with pytest.raises(RuntimeError):
        await sla_service.run_once()
    monkeypatch.setattr(sla_service, 'mark_handled', real_mark)
    assert (await sla_service.run_once())["breached"] == 1
    assert (await sla_service.run_once())["breached"] == 0
