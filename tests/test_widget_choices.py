"""Tapping a choice in the panel.

The rating and the assistant's "did that solve it?" were Telegram-only, not
because anybody decided so but because Telegram is the only place a button
could be drawn. A widget customer got the question and no way to answer it,
so the CSAT figure quietly meant "of the people who happened to use
Telegram".

The endpoint that fixes that is open to anyone holding a session, and its
payloads are short strings with a ticket id inside them. So the tests that
matter are the refusals: a payload nobody offered, a message belonging to
somebody else, and the same answer given twice.
"""

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

import src.dashboard.routes.widget as widget_routes
from src.channels import get as get_channel
from src.channels.widget import NAME as WIDGET
from src.dashboard.app import app
from src.database.db import db_session
from src.database.models import Message as MessageRecord, Ticket, User
from src.services import csat_service, settings_service


@pytest.fixture(autouse=True)
async def _clean(initialized_db):
    widget_routes._hits.clear()
    yield
    widget_routes._hits.clear()


@pytest.fixture()
async def widget_on(initialized_db):
    await settings_service.set_widget_enabled(True, updated_by=0)
    await settings_service.set_widget_origins("", updated_by=0)
    yield
    await settings_service.set_widget_enabled(False, updated_by=0)


async def _conversation(c: TestClient, text: str = "help please"):
    opened = c.post("/widget/api/message", headers={"origin": "https://shop.example"},
                    content=json.dumps({"text": text})).json()
    async with db_session() as session:
        ticket = await session.get(Ticket, opened["ticket"])
        user = await session.get(User, ticket.user_id)
        routing_key = user.tg_id
    return opened["session"], opened["ticket"], routing_key


async def _ask_for_a_rating(ticket_id: int, routing_key: int) -> int:
    """Close the ticket and ask, the way the dashboard does.

    Actually closed, not merely notified: `save_rating` refuses a ticket that
    is still open, which is right — a rating for a conversation still in
    progress is a rating of nothing.
    """
    async with db_session() as session:
        ticket = await session.get(Ticket, ticket_id)
        ticket.status = "closed"
        await session.commit()

    await csat_service.send_close_notification(
        None, user_tg_id=routing_key, ticket_id=ticket_id, lang="en")
    async with db_session() as session:
        row = (await session.execute(
            select(MessageRecord)
            .where(MessageRecord.ticket_id == ticket_id,
                   MessageRecord.options.is_not(None))
            .order_by(MessageRecord.id.desc())
        )).scalars().first()
        return row.id, [o["payload"] for o in row.options]


def _choose(c: TestClient, token: str, message_id: int, payload: str):
    return c.post("/widget/api/choose",
                  headers={"origin": "https://shop.example",
                           "x-spooldesk-session": token},
                  content=json.dumps({"message": message_id, "payload": payload}))


# --------------------------------------------------------------- the happy bit


async def test_a_visitor_can_rate_the_support_they_got(widget_on):
    """The whole point. Before this, the closing message asked a question the
    panel had no way to answer."""
    with TestClient(app, base_url="http://testserver") as c:
        token, ticket_id, key = await _conversation(c)
        message_id, payloads = await _ask_for_a_rating(ticket_id, key)

        five = [p for p in payloads if p.endswith(":5")][0]
        r = _choose(c, token, message_id, five)

    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert r.json()["said"], "the customer was thanked with nothing"

    async with db_session() as session:
        ticket = await session.get(Ticket, ticket_id)
    assert ticket.csat_rating == 5


async def test_the_question_closes_once_it_is_answered(widget_on):
    """A rating that still looks tappable is a customer wondering whether the
    first tap counted."""
    with TestClient(app, base_url="http://testserver") as c:
        token, ticket_id, key = await _conversation(c)
        message_id, payloads = await _ask_for_a_rating(ticket_id, key)
        _choose(c, token, message_id, payloads[0])

        seen = c.get(f"/widget/api/thread/{ticket_id}",
                     headers={"x-spooldesk-session": token}).json()

    async with db_session() as session:
        row = await session.get(MessageRecord, message_id)
    assert row.options is None

    offered = [m.get("options") for m in seen["messages"] if m.get("options")]
    assert offered == [], "the panel would still draw the buttons"


async def test_the_same_answer_twice_is_refused(widget_on):
    """A chat message can be tapped twice, and the second tap must not become
    a second rating."""
    with TestClient(app, base_url="http://testserver") as c:
        token, ticket_id, key = await _conversation(c)
        message_id, payloads = await _ask_for_a_rating(ticket_id, key)

        first = _choose(c, token, message_id, payloads[0])
        second = _choose(c, token, message_id, payloads[4])

    assert first.status_code == 200
    assert second.status_code == 400


# ------------------------------------------------------------- the refusals


async def test_a_payload_nobody_offered_is_refused(widget_on):
    """The decisive check. These payloads are short strings with a ticket id
    in them, and the handlers behind them were written when the only source
    of one was a button we had drawn ourselves."""
    with TestClient(app, base_url="http://testserver") as c:
        token, ticket_id, key = await _conversation(c)
        message_id, _ = await _ask_for_a_rating(ticket_id, key)

        # Well-formed and plausible, and not among the five this message
        # actually offered: a rating outside 1-5, and one aimed at another
        # ticket entirely.
        out_of_range = _choose(c, token, message_id, f"csat:{ticket_id}:9")
        elsewhere = _choose(c, token, message_id, f"csat:{ticket_id + 999}:5")
        nonsense = _choose(c, token, message_id, "drop-everything")

    assert out_of_range.status_code == 400
    assert elsewhere.status_code == 400
    assert nonsense.status_code == 400

    async with db_session() as session:
        ticket = await session.get(Ticket, ticket_id)
    assert ticket.csat_rating is None


async def test_somebody_elses_question_cannot_be_answered(widget_on):
    """Message ids are small integers. Without an ownership check this is a
    way to rate and close strangers' tickets by counting."""
    with TestClient(app, base_url="http://testserver") as c:
        _, theirs, their_key = await _conversation(c, "their problem")
        message_id, payloads = await _ask_for_a_rating(theirs, their_key)

        mine, _, _ = await _conversation(c, "my problem")
        r = _choose(c, mine, message_id, payloads[0])

    assert r.status_code == 400
    async with db_session() as session:
        ticket = await session.get(Ticket, theirs)
    assert ticket.csat_rating is None


async def test_a_choice_needs_a_session(widget_on):
    with TestClient(app, base_url="http://testserver") as c:
        token, ticket_id, key = await _conversation(c)
        message_id, payloads = await _ask_for_a_rating(ticket_id, key)

        r = c.post("/widget/api/choose",
                   headers={"origin": "https://shop.example"},
                   content=json.dumps({"message": message_id,
                                       "payload": payloads[0]}))
    assert r.status_code == 401


async def test_a_malformed_request_is_refused_not_crashed(widget_on):
    with TestClient(app, base_url="http://testserver") as c:
        token, _, _ = await _conversation(c)
        for body in ({}, {"message": "abc"}, {"payload": "csat:1:5"},
                     {"message": 0, "payload": ""}):
            r = c.post("/widget/api/choose",
                       headers={"origin": "https://shop.example",
                                "x-spooldesk-session": token},
                       content=json.dumps(body))
            assert r.status_code in (400, 401), body


# ------------------------------------------------------------ the transcript


async def test_the_choices_ride_on_the_message_that_offered_them(widget_on):
    """Telegram keeps a keyboard attached to a message and remembers which
    button was which. A list of rows has no such memory, so the offer is
    stored where the message is."""
    with TestClient(app, base_url="http://testserver") as c:
        token, ticket_id, key = await _conversation(c)
        await _ask_for_a_rating(ticket_id, key)

        # The thread endpoint, not the poll: the ticket is closed now, and
        # poll answers about the *open* conversation, of which there is none.
        # A closed conversation is still readable, which is the whole reason
        # the panel has a history.
        seen = c.get(f"/widget/api/thread/{ticket_id}",
                     headers={"x-spooldesk-session": token}).json()

    with_options = [m for m in seen["messages"] if m.get("options")]
    assert len(with_options) == 1
    labels = [o["label"] for o in with_options[0]["options"]]
    assert len(labels) == 5
    # And the text is a sentence, not a sentence with five stars stapled on.
    assert "⭐" not in with_options[0]["text"]


def test_the_widget_says_it_can_draw_them():
    """`caps.buttons` is what the rest of the system reads to decide whether
    to spell the choices into the text instead."""
    assert get_channel(WIDGET).caps.buttons is True
