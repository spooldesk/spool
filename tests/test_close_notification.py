"""Telling the customer their ticket is closed, on the channel they wrote from.

`send_close_notification` has taken `channel` and `address` since the channel
layer landed, and not one of its five call sites passed them. So it always
fell back to "Telegram, addressed by the routing key" — which for a Telegram
customer *is* their chat id, and worked, and for everyone else was a negative
synthetic number that no chat has ever had.

The result was invisible in exactly the way that matters: closing a widget
ticket produced a log line and a customer who was never told, and never asked
how it went. Every Telegram test passed throughout.
"""

import json

import pytest
from fastapi.testclient import TestClient

import src.dashboard.routes.widget as widget_routes
from src.channels import get as get_channel
from src.channels.widget import NAME as WIDGET
from src.dashboard.app import app
from src.database.db import db_session
from src.services import csat_service, intake_service, settings_service
from tests.conftest import next_tg_id


@pytest.fixture(autouse=True)
async def _clean_limits(initialized_db):
    widget_routes._hits.clear()
    yield
    widget_routes._hits.clear()


@pytest.fixture()
async def widget_on(initialized_db):
    await settings_service.set_widget_enabled(True, updated_by=0)
    await settings_service.set_widget_origins("", updated_by=0)
    yield
    await settings_service.set_widget_enabled(False, updated_by=0)


async def _widget_ticket() -> tuple[str, int]:
    with TestClient(app, base_url="http://testserver") as c:
        opened = c.post("/widget/api/message",
                        headers={"origin": "https://shop.example"},
                        content=json.dumps({"text": "my order is late"})).json()
    return opened["session"], opened["ticket"]


async def _transcript(ticket_id: int) -> list[str]:
    async with db_session() as session:
        rows = await intake_service.conversation(session, ticket_id)
        return [m.text or "" for m in rows]


async def test_a_widget_customer_is_told_their_ticket_closed(widget_on):
    """The whole bug in one assertion.

    The widget has no inbox to push to — its transcript *is* the delivery — so
    a closing notice that is never recorded is a closing notice that never
    happened.
    """
    token, ticket_id = await _widget_ticket()

    async with db_session() as session:
        user = await _customer_of(session, ticket_id)
        routing_key = user.tg_id

    before = await _transcript(ticket_id)
    await csat_service.send_close_notification(
        None, user_tg_id=routing_key, ticket_id=ticket_id, lang="en",
    )
    after = await _transcript(ticket_id)

    assert len(after) == len(before) + 1, "the customer was told nothing"

    with TestClient(app, base_url="http://testserver") as c:
        seen = c.get("/widget/api/poll",
                     headers={"x-spooldesk-session": token}).json()

    said = " ".join(m["text"] for m in seen["messages"])
    assert str(ticket_id) in said, "the closing notice never reached the panel"


async def test_the_rating_request_arrives_as_something_tappable(widget_on):
    """The panel can draw buttons now, so the choices go on the message rather
    than being spelled into its text.

    They were spelled out for a while, which was the honest thing to do when
    nothing could draw them — the Channel protocol requires a channel without
    `caps.buttons` to keep the question somehow. Now it has them.
    """
    from sqlalchemy import select

    from src.database.models import Message as MessageRecord

    assert get_channel(WIDGET).caps.buttons is True

    _, ticket_id = await _widget_ticket()
    async with db_session() as session:
        user = await _customer_of(session, ticket_id)
        routing_key = user.tg_id

    await csat_service.send_close_notification(
        None, user_tg_id=routing_key, ticket_id=ticket_id, lang="en",
    )

    async with db_session() as session:
        row = (await session.execute(
            select(MessageRecord)
            .where(MessageRecord.ticket_id == ticket_id)
            .order_by(MessageRecord.id.desc())
        )).scalars().first()

    labels = [o["label"] for o in (row.options or [])]
    assert len(labels) == 5, "the five ratings were not offered"
    payloads = [o["payload"] for o in row.options]
    assert all(p.startswith("csat:") for p in payloads)
    # And the text stays a sentence rather than a sentence with five stars
    # stapled to it.
    assert "⭐" not in row.text


async def test_a_telegram_customer_still_gets_it_the_old_way(channel):
    """The path that already worked must keep working — a fix that reaches
    new channels by dropping the one with users is not a fix."""
    from src.database.models import User

    tg_id = next_tg_id()
    async with db_session() as session:
        session.add(User(tg_id=tg_id, channel="telegram", channel_user_id=str(tg_id),
                         username="ann", is_verified=True, is_active=True))
        await session.flush()
        result = await intake_service.receive(session, intake_service.Inbound(
            channel="telegram", address=str(tg_id), text="printer is on fire",
        ))
        await session.commit()

    await csat_service.send_close_notification(
        None, user_tg_id=tg_id, ticket_id=result.ticket_id, lang="en",
    )

    sent = [text for _addr, text in channel.texts]
    assert sent, "nothing was sent to the Telegram channel"
    assert str(result.ticket_id) in " ".join(sent)
    # And the buttons are still offered where they can actually be drawn,
    # rather than being spelled into the text for everyone.
    assert channel.offered[-1], "the rating options were dropped"


async def test_the_internal_closure_marker_never_reaches_a_customer(widget_on):
    """It says so in its own comment — "NOT user-facing ... do not localize" —
    and that held while Telegram was the only channel, where the transcript is
    a record of what was sent rather than the sending itself.

    On the widget it was a fixed Ukrainian sentence appearing in the middle of
    a stranger's conversation, whatever language they were writing in.
    """
    from src.database.models import Message as MessageRecord
    from src.utils.i18n import CLOSURE_SYSTEM_TEXT

    token, ticket_id = await _widget_ticket()

    async with db_session() as session:
        session.add(MessageRecord(
            ticket_id=ticket_id, sender_type="admin", sender_id=0,
            text=CLOSURE_SYSTEM_TEXT,
        ))
        await session.commit()

    assert CLOSURE_SYSTEM_TEXT not in await _transcript(ticket_id)

    with TestClient(app, base_url="http://testserver") as c:
        seen = c.get("/widget/api/poll",
                     headers={"x-spooldesk-session": token}).json()
    assert CLOSURE_SYSTEM_TEXT not in [m["text"] for m in seen["messages"]]


async def _customer_of(session, ticket_id: int):
    from sqlalchemy import select

    from src.database.models import Ticket, User

    ticket = await session.get(Ticket, ticket_id)
    return (await session.execute(
        select(User).where(User.id == ticket.user_id)
    )).scalar_one()
