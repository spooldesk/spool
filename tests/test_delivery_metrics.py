"""Queued or failed output must not count as an answer to a customer."""

from datetime import datetime, timedelta

import pytest

from src.dashboard import queries, report_queries
from src.database.db import db_session
from src.database.models import Message, Ticket, User
from tests.conftest import next_tg_id


@pytest.mark.parametrize("state", ["pending", "sending", "retry", "failed", "uncertain", "cancelled"])
async def test_only_delivered_replies_affect_queue_and_reports(initialized_db, state):
    states = ["pending", "sending", "retry", "failed", "uncertain", "cancelled"]
    base = datetime(2038, 5, 10, states.index(state))
    before = (await queries.queue_status())["awaiting"]
    category = f"delivery-metrics-{next_tg_id()}"
    async with db_session(write=True) as session:
        user = User(tg_id=next_tg_id(), is_active=True, is_verified=True)
        session.add(user)
        await session.flush()
        ticket = Ticket(user_id=user.id, category=category, status="active", created_at=base)
        session.add(ticket)
        await session.flush()
        ticket_id = ticket.id
        session.add_all([
            Message(ticket_id=ticket_id, sender_type="user", sender_id=user.tg_id,
                    text="Question", created_at=base),
            Message(ticket_id=ticket_id, sender_type="admin", sender_id=0,
                    text="Undelivered answer", delivery_status=state, created_at=base + timedelta(minutes=1)),
        ])

    assert (await queries.queue_status())["awaiting"] == before + 1
    assert ticket_id in dict(await queries._queue_rows(None, "all"))
    report = await report_queries.report_summary(base, base + timedelta(hours=1))
    assert report["avg_first_response_min"] is None
    exported = await queries.list_tickets_for_export(category=category)
    assert exported[0]["msg_admin"] == 0
    async with db_session() as session:
        times = await queries._effective_close_seconds_map(session, [ticket_id])
    assert ticket_id not in times  # an open conversation has no resolution time

    async with db_session(write=True) as session:
        session.add(Message(ticket_id=ticket_id, sender_type="admin", sender_id=0,
                            text="Delivered answer", delivery_status="delivered",
                            created_at=base + timedelta(minutes=2),
                            delivered_at=base + timedelta(minutes=10)))
        # This follow-up arrived while the reply was waiting in the outbox.
        # A reply composed at minute two cannot answer a later question.
        session.add(Message(ticket_id=ticket_id, sender_type="user", sender_id=user.tg_id,
                            text="Still waiting", created_at=base + timedelta(minutes=5)))
    assert (await queries.queue_status())["awaiting"] == before + 1
    assert ticket_id in dict(await queries._queue_rows(None, "all"))
    report = await report_queries.report_summary(base, base + timedelta(hours=1))
    assert report["avg_first_response_min"] == pytest.approx(10, abs=0.1)
    exported = await queries.list_tickets_for_export(category=category)
    assert exported[0]["msg_admin"] == 1
