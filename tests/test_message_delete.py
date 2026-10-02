"""Unsending a delivered message.

Telegram lets a bot delete its own message for 48 hours; other channels give
less, or nothing. The tests care about three things: that we never claim to
have removed something the customer can still read, that the transcript still
says a message existed after it is withdrawn, and that the button is not
offered on a channel that cannot honour it.
"""

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from src.dashboard import queries
from src.dashboard.app import app
from src.database.db import db_session
from src.database.models import Message, Team, Ticket, User
from src.channels.base import Capabilities
from src.services import message_service
from tests.conftest import RecordingChannel, next_tg_id


def _login(c: TestClient) -> str:
    c.get("/login")
    token = c.cookies.get("supbot_csrf")
    r = c.post("/login", data={"email": "admin@local", "password": "test-password-123",
                               "next": "/", "csrf_token": token}, follow_redirects=False)
    assert r.status_code == 303
    return c.cookies.get("supbot_csrf") or token


class _Refusing(RecordingChannel):
    """A channel that can unsend in principle and declines this one.

    The real reasons вЂ” already gone, past the medium's own window вЂ” are the
    adapter's to recognise; above it they all look the same, and the point of
    the test is that "the adapter said no" never becomes "we deleted it".
    """

    async def unsend(self, address, ref):
        return False


# The bot is no longer the thing being deleted through, but the service still
# takes one for the admin-thread side of its work.
_BOT = object()


async def _sent_message(*, tg_message_id=555, age=timedelta(minutes=5),
                        internal=False, sender="admin") -> tuple[int, int]:
    """A ticket with one operator message. Returns (ticket_id, message_id)."""
    async with db_session() as session:
        team = Team(name=f"del-{next_tg_id()}")
        session.add(team)
        await session.flush()
        u = User(tg_id=next_tg_id(), username="del_user", team_id=team.id,
                 is_verified=True, is_active=True)
        session.add(u)
        await session.flush()
        t = Ticket(user_id=u.id, team_id=team.id, category="Network", status="active")
        session.add(t)
        await session.flush()
        m = Message(ticket_id=t.id, sender_type=sender, sender_id=1,
                    text="sent by mistake", is_internal=internal,
                    tg_message_id=tg_message_id,
                    created_at=datetime.utcnow() - age)
        session.add(m)
        await session.flush()
        ids = (t.id, m.id)
        await session.commit()
    return ids


async def test_unsend_removes_it_from_the_customer_chat(initialized_db, channel):
    _tid, mid = await _sent_message()
    result = await message_service.delete_for_user(_BOT, mid)

    assert result.ok and result.removed_from_channel
    assert len(channel.unsent) == 1 and channel.unsent[0][1] == "555"

    async with db_session() as session:
        row = await session.get(Message, mid)
        assert row is not None, "the row must survive вЂ” reports count admin replies"
        assert row.deleted_at is not None
        assert row.text == "sent by mistake", "the text stays for the next operator"


async def test_the_database_is_untouched_when_the_channel_refuses(initialized_db):
    """Never mark something withdrawn that the customer can still read."""
    import src.channels as channels

    _tid, mid = await _sent_message()
    channels.register(_Refusing())
    try:
        result = await message_service.delete_for_user(_BOT, mid)
    finally:
        channels.reset()
    assert not result.ok and result.error_key == "chat_edit_uncertain"

    async with db_session() as session:
        assert (await session.get(Message, mid)).deleted_at is None


async def test_offer_expires_with_the_channels_window(initialized_db):
    fresh = {"sender_type": "admin", "is_internal": False, "tg_message_id": 1,
             "deleted_at": None, "created_at": datetime.utcnow() - timedelta(hours=47)}
    stale = {**fresh, "created_at": datetime.utcnow() - timedelta(hours=49)}
    assert message_service.can_delete(fresh)
    assert not message_service.can_delete(stale)


async def test_nothing_to_unsend_without_a_recorded_id(initialized_db):
    """Messages sent before the id was recorded simply cannot be withdrawn."""
    _tid, mid = await _sent_message(tg_message_id=None)
    assert not message_service.can_delete(
        {"sender_type": "admin", "is_internal": False, "tg_message_id": None,
         "deleted_at": None, "created_at": datetime.utcnow()})

    result = await message_service.delete_for_user(_BOT, mid)
    assert not result.ok and result.error_key == "e_delete_window_passed"


@pytest.mark.parametrize("kind,kwargs", [
    ("the customer's own message", {"sender": "user"}),
    ("an internal note", {"internal": True}),
])
async def test_only_operator_messages_can_be_unsent(initialized_db, channel, kind, kwargs):
    """A note was never sent anywhere, and their message is not ours to remove."""
    _tid, mid = await _sent_message(**kwargs)
    result = await message_service.delete_for_user(_BOT, mid)
    assert not result.ok, kind
    assert channel.unsent == []


async def test_unsend_is_idempotent(initialized_db, channel):
    _tid, mid = await _sent_message()
    await message_service.delete_for_user(_BOT, mid)
    again = await message_service.delete_for_user(_BOT, mid)
    assert again.ok and not again.removed_from_channel
    assert len(channel.unsent) == 1, "must not ask the channel twice"


async def test_the_card_offers_the_button_only_where_it_works(initialized_db, channel):
    tid, mid = await _sent_message()
    detail = await queries.ticket_detail(tid)
    msg = next(m for m in detail["messages"] if m["id"] == mid)
    assert msg["can_delete"] is True
    assert msg["deleted_at"] is None

    await message_service.delete_for_user(_BOT, mid)
    after = next(m for m in (await queries.ticket_detail(tid))["messages"]
                 if m["id"] == mid)
    assert after["can_delete"] is False, "already withdrawn вЂ” nothing left to offer"
    assert after["deleted_at"] is not None


async def test_route_reports_a_refusal_instead_of_claiming_success(initialized_db):
    import src.channels as channels

    tid, mid = await _sent_message()
    channels.register(_Refusing())
    with TestClient(app, base_url="http://testserver") as c:
        token = _login(c)
        r = c.post(f"/tickets/{tid}/message/{mid}/delete",
                   data={"csrf_token": token}, follow_redirects=False)
    channels.reset()
    assert r.status_code == 303
    assert "err=" in r.headers["location"]


async def test_a_channel_that_cannot_unsend_is_told_apart_from_being_too_late(
    initialized_db,
):
    """Two different noes.

    "Too late" means the operator should have been quicker; "this channel
    can't" means no amount of quickness would have helped. Collapsing them
    into one message sends someone hunting for a window that never existed.
    """
    import src.channels as channels

    _tid, mid = await _sent_message()
    channels.register(RecordingChannel(caps=Capabilities(files=True, unsend=False)))
    try:
        result = await message_service.delete_for_user(_BOT, mid)
        assert not result.ok
        assert result.error_key == "e_delete_unsupported"

        # And the button is never offered there in the first place.
        detail = await queries.ticket_detail(_tid)
        assert all(not m["can_delete"] for m in detail["messages"])
    finally:
        channels.reset()
