"""Dashboard delivery is durable, attributable and recoverable without blind retry."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from src.channels.base import Delivery
from src.dashboard import auth, queries
from src.dashboard.app import app, TEMPLATES
from src.dashboard.i18n import strings
from src.database.db import db_session
from src.database.delivery_models import OutboundJob
from src.database.models import Message, Team, Ticket, User
from src.services import admin_user_service, announce_service, outbound
from tests.conftest import next_tg_id


@pytest.fixture()
async def context(initialized_db, channel, monkeypatch):
    monkeypatch.setitem(TEMPLATES.env.globals, "T", strings("en"))
    monkeypatch.setattr(announce_service, "announce_message", AsyncMock())
    operator = await admin_user_service.create_admin(
        email=f"delivery-{next_tg_id()}@test.example", password="test-password",
        role="agent", name="Delivery agent",
    )
    async with db_session() as session:
        team = Team(name=f"delivery-{next_tg_id()}")
        session.add(team)
        await session.flush()
        user = User(tg_id=next_tg_id(), team_id=team.id, is_active=True)
        session.add(user)
        await session.flush()
        ticket = Ticket(user_id=user.id, team_id=team.id, status="active", category="Network")
        session.add(ticket)
        await session.flush()
        tid, address = ticket.id, str(user.tg_id)
    client = TestClient(app, base_url="http://testserver")
    client.cookies.set(auth.SESSION_COOKIE, auth.make_session_cookie(operator.id))
    client.get("/login", follow_redirects=False)
    yield SimpleNamespace(client=client, token=client.cookies.get("supbot_csrf"),
                          ticket_id=tid, address=address, operator=operator, channel=channel)
    client.close()


@pytest.mark.parametrize("attachment", [False, True])
async def test_reply_is_committed_before_send_and_preserves_operator(context, monkeypatch, attachment):
    seen = []

    async def send(address, *args, **kwargs):
        # A separate writer can observe the intent while provider I/O runs:
        # no uncommitted row and no database lock held by the HTTP handler.
        async with db_session(write=True) as session:
            row, job = (await session.execute(select(Message, OutboundJob).join(
                OutboundJob, OutboundJob.message_id == Message.id,
            ).where(Message.ticket_id == context.ticket_id))).one()
            assert row.operator_id == context.operator.id
            assert row.delivery_status == job.status == "sending"
            seen.append(row.id)
        return Delivery(ok=True, ref="offline-ref", file_ref="offline-file" if attachment else None)

    monkeypatch.setattr(context.channel, "send_file" if attachment else "send_text", send)
    kwargs = {"files": [("attachments", ("proof.txt", b"durable bytes", "text/plain"))]} if attachment else {}
    response = context.client.post(f"/tickets/{context.ticket_id}/reply",
        data={"text": "durable answer", "csrf_token": context.token}, follow_redirects=False, **kwargs)
    assert response.status_code == 303 and "sent=1" in response.headers["location"]
    assert len(seen) == 1
    async with db_session() as session:
        row = await session.get(Message, seen[0])
        assert row.delivery_status == "delivered"
        assert row.operator_id == context.operator.id
    if attachment:
        fetched = context.client.get(f"/tickets/attachment/{seen[0]}")
        assert fetched.status_code == 200 and fetched.content == b"durable bytes"
    detail = await queries.ticket_detail(context.ticket_id)
    assert detail["stage"] == "waiting"
    assert detail["messages"][0]["delivery_status"] == "delivered"


async def test_uncertain_retry_requires_consent_and_exact_ticket_binding(context):
    async with db_session(write=True) as session:
        job = await outbound.enqueue(session, ticket_id=context.ticket_id, channel="telegram",
            address=context.address, text="a possibly delivered answer", operator_id=context.operator.id)
        mid, jid = job.message_id, job.id
        job.status = "uncertain"
        row = await session.get(Message, mid)
        row.delivery_status = "uncertain"
    url = f"/tickets/{context.ticket_id}/message/{mid}/retry"
    assert context.client.post(url).status_code == 403
    assert context.client.post(url, data={"csrf_token": context.token}).status_code == 409
    mismatched = context.client.post(f"/tickets/{context.ticket_id + 1}/message/{mid}/retry",
        data={"csrf_token": context.token, "acknowledge_duplicate": "yes"})
    assert mismatched.status_code == 404 and not context.channel.texts
    page = context.client.get(f"/tickets/{context.ticket_id}")
    assert page.status_code == 200
    assert 'name="acknowledge_duplicate" value="yes" required' in page.text
    assert "may already have received" in page.text
    snapshot = context.client.get(f"/tickets/{context.ticket_id}/messages.json?after_id={mid}").json()
    assert snapshot["messages"] == []
    assert snapshot["deliveries"] == [{"message_id": mid, "status": "uncertain", "can_retry": True}]
    result = context.client.post(url, data={"csrf_token": context.token,
        "acknowledge_duplicate": "yes"}, follow_redirects=False)
    assert result.status_code == 303 and "sent=1" in result.headers["location"]
    assert len(context.channel.texts) == 1
    async with db_session() as session:
        assert (await session.get(OutboundJob, jid)).status == "delivered"
    again = context.client.post(url, data={"csrf_token": context.token,
        "acknowledge_duplicate": "yes"}, follow_redirects=False)
    assert "err=" in again.headers["location"] and len(context.channel.texts) == 1


async def test_failed_file_remains_visible_and_can_be_retried(context):
    context.channel.fail_on = 1
    response = context.client.post(f"/tickets/{context.ticket_id}/reply",
        data={"csrf_token": context.token},
        files=[("attachments", ("keep.txt", b"persist this", "text/plain"))], follow_redirects=False)
    assert response.status_code == 303 and "err=" in response.headers["location"]
    async with db_session() as session:
        row = (await session.execute(select(Message).where(Message.ticket_id == context.ticket_id))).scalar_one()
        mid = row.id
        assert row.delivery_status == "failed" and row.operator_id == context.operator.id
        assert (await session.get(Ticket, context.ticket_id)).stage == "new"
    page = context.client.get(f"/tickets/{context.ticket_id}")
    assert "Delivery failed" in page.text and f"/message/{mid}/retry" in page.text
    assert context.client.get(f"/tickets/attachment/{mid}").content == b"persist this"
    context.channel.fail_on = None
    retried = context.client.post(f"/tickets/{context.ticket_id}/message/{mid}/retry",
        data={"csrf_token": context.token}, follow_redirects=False)
    assert "sent=1" in retried.headers["location"]
    assert context.channel.files[0]["content"] == b"persist this"
    assert (await queries.ticket_detail(context.ticket_id))["stage"] == "waiting"


async def test_delayed_reply_does_not_hide_new_customer_message(context, monkeypatch):
    from src.services import intake_service

    async def send(*args, **kwargs):
        async with db_session(write=True) as session:
            received = await intake_service.receive(session, intake_service.Inbound(
                channel="telegram", address=context.address, text="Another question during delivery"))
            assert received.ticket_id == context.ticket_id
        return Delivery(ok=True, ref="delayed-answer")

    monkeypatch.setattr(context.channel, "send_text", send)
    result = await outbound.say(ticket_id=context.ticket_id, channel="telegram",
        address=context.address, text="Answer to the earlier question", operator_id=context.operator.id)
    assert result.ok
    assert (await queries.ticket_detail(context.ticket_id))["stage"] == "in_progress"
    assert context.ticket_id in dict(await queries._queue_rows(None, "all"))


async def test_automatic_ack_does_not_settle_response_clock(context):
    from datetime import datetime, timedelta
    from src.services import sla_service
    async with db_session(write=True) as session:
        session.add(Message(ticket_id=context.ticket_id, sender_type="user", sender_id=1,
            text="Still waiting", created_at=datetime.utcnow() - timedelta(days=3)))
    result = await outbound.say(ticket_id=context.ticket_id, channel="telegram",
        address=context.address, text="We received your request")
    assert result.ok
    assert (await queries.ticket_detail(context.ticket_id))["stage"] == "new"
    assert context.ticket_id in dict(await queries._queue_rows(None, "all"))
    assert context.ticket_id in {b['id'] for b in await sla_service.find_new_breaches()}


async def test_reply_waits_then_customer_response_reopens_clock(context):
    from src.services import intake_service, workflow_service
    result = await outbound.say(ticket_id=context.ticket_id, channel="telegram",
        address=context.address, text="Please send the details", operator_id=context.operator.id)
    assert result.ok
    assert (await queries.ticket_detail(context.ticket_id))["stage"] == "waiting"
    assert context.ticket_id not in dict(await queries._queue_rows(None, "all"))
    await workflow_service.add_internal_note(context.ticket_id, text="A private note", operator_id=context.operator.id)
    assert (await queries.ticket_detail(context.ticket_id))["stage"] == "waiting"
    async with db_session(write=True) as session:
        received = await intake_service.receive(session, intake_service.Inbound(
            channel="telegram", address=context.address, text="Here are the details"))
        assert received.ticket_id == context.ticket_id
    assert (await queries.ticket_detail(context.ticket_id))["stage"] == "in_progress"
    assert context.ticket_id in dict(await queries._queue_rows(None, "all"))


async def test_old_retry_cannot_override_a_more_recent_answer(context):
    from src.services import intake_service
    async with db_session(write=True) as session:
        job = await outbound.enqueue(session, ticket_id=context.ticket_id, channel='telegram',
            address=context.address, text='Old failed answer', operator_id=context.operator.id)
        old_job = job.id
        job.status = 'failed'
        (await session.get(Message, job.message_id)).delivery_status = 'failed'
    async with db_session(write=True) as session:
        await intake_service.receive(session, intake_service.Inbound(
            channel='telegram', address=context.address, text='A newer question'))
    result = await outbound.say(ticket_id=context.ticket_id, channel='telegram',
        address=context.address, text='Answer to the newer question', operator_id=context.operator.id)
    assert result.ok
    assert (await outbound.retry(old_job)).ok
    assert (await queries.ticket_detail(context.ticket_id))['stage'] == 'waiting'
    assert context.ticket_id not in dict(await queries._queue_rows(None, 'all'))


async def test_email_reply_shows_submission_not_inbox_confirmation(context, monkeypatch):
    from src import channels
    from tests.conftest import RecordingChannel
    pipe = RecordingChannel(name='email')
    monkeypatch.setitem(channels._registry, 'email', pipe)
    async with db_session(write=True) as session:
        ticket = await session.get(Ticket, context.ticket_id)
        user = await session.get(User, ticket.user_id)
        user.channel = 'email'
        user.channel_user_id = 'customer@example.test'
    result = await outbound.say(ticket_id=context.ticket_id, channel='email',
        address='customer@example.test', text='Answer', operator_id=context.operator.id)
    assert result.ok
    page = context.client.get(f'/tickets/{context.ticket_id}')
    assert page.status_code == 200
    assert strings('en')['email_submitted'] in page.text
