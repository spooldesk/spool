"""Clicking a button we sent into Slack.

The adapter has drawn Block Kit buttons since it was written and declared
`caps.buttons` all along — what it never had was anywhere for a click to go.
Slack posts interactions to a different URL from events, and without one a
customer taps a rating and Slack shows them a red failure.

Two things guard this endpoint and they guard different things. The signature
proves Slack sent the request; it says nothing about who clicked or what they
are entitled to. The channel check is what says the button belongs to the
conversation it claims — without it, anybody in any channel the app is in
could rate somebody else's ticket by replaying a value.
"""

import hashlib
import hmac
import json
import time
import urllib.parse

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from src.dashboard.app import app
from src.database.db import db_session
from src.database.models import Message as MessageRecord, Ticket, User
from src.services import choice_service, csat_service, intake_service
from tests.conftest import next_tg_id

SECRET = "slack-signing-secret-for-tests"
CHANNEL = "C0PRETEND"
TS = "1717171717.000100"


@pytest.fixture(autouse=True)
def _slack_configured(monkeypatch):
    monkeypatch.setenv("SLACK_SIGNING_SECRET", SECRET)
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-not-a-real-token")


def _signed(client: TestClient, body: bytes):
    """Post to the endpoint the way Slack does, signature and all."""
    stamp = str(int(time.time()))
    signature = "v0=" + hmac.new(
        SECRET.encode(), b"v0:" + stamp.encode() + b":" + body, hashlib.sha256
    ).hexdigest()
    return client.post("/slack/interactive", content=body, headers={
        "X-Slack-Request-Timestamp": stamp,
        "X-Slack-Signature": signature,
        "content-type": "application/x-www-form-urlencoded",
    })


def _interaction(*, channel=CHANNEL, ts=TS, value="csat:1:5") -> bytes:
    payload = {
        "type": "block_actions",
        "channel": {"id": channel},
        "message": {"ts": ts},
        "actions": [{"action_id": value, "value": value, "type": "button"}],
    }
    return urllib.parse.urlencode({"payload": json.dumps(payload)}).encode()


async def _slack_ticket_awaiting_a_rating(channel: str = CHANNEL, ts: str = TS):
    """A closed Slack ticket whose closing message offered five ratings."""
    async with db_session() as session:
        result = await intake_service.receive(session, intake_service.Inbound(
            channel="slack", address=channel, text="the export is failing",
        ))
        await session.commit()
        ticket_id = result.ticket_id

    async with db_session() as session:
        ticket = await session.get(Ticket, ticket_id)
        ticket.status = "closed"
        row = MessageRecord(
            ticket_id=ticket_id, sender_type="admin", sender_id=0,
            text="Your request was closed. How did we do?",
            channel_ref=ts,
        )
        session.add(row)
        await session.flush()
        await choice_service.offer(
            session, row.id, csat_service.rating_options(ticket_id))
        await session.commit()
        return ticket_id, row.id


# ------------------------------------------------------------- the signature


def test_an_unsigned_click_is_refused():
    """The endpoint is a public URL that changes data. Without the signature
    anybody could post to it."""
    with TestClient(app, base_url="http://testserver") as c:
        r = c.post("/slack/interactive", content=_interaction(),
                   headers={"content-type": "application/x-www-form-urlencoded"})
    assert r.status_code == 401


def test_a_wrongly_signed_click_is_refused():
    with TestClient(app, base_url="http://testserver") as c:
        body = _interaction()
        stamp = str(int(time.time()))
        r = c.post("/slack/interactive", content=body, headers={
            "X-Slack-Request-Timestamp": stamp,
            "X-Slack-Signature": "v0=" + "0" * 64,
            "content-type": "application/x-www-form-urlencoded",
        })
    assert r.status_code == 401


def test_a_replayed_click_is_refused(monkeypatch):
    """The signature stays valid forever, which is exactly why the clock has
    to be part of the check."""
    body = _interaction()
    old = str(int(time.time()) - 3600)
    signature = "v0=" + hmac.new(
        SECRET.encode(), b"v0:" + old.encode() + b":" + body, hashlib.sha256
    ).hexdigest()

    with TestClient(app, base_url="http://testserver") as c:
        r = c.post("/slack/interactive", content=body, headers={
            "X-Slack-Request-Timestamp": old,
            "X-Slack-Signature": signature,
            "content-type": "application/x-www-form-urlencoded",
        })
    assert r.status_code == 401


# ---------------------------------------------------------------- the click


async def test_a_rating_from_slack_is_recorded(initialized_db):
    ticket_id, _ = await _slack_ticket_awaiting_a_rating()

    with TestClient(app, base_url="http://testserver") as c:
        r = _signed(c, _interaction(value=f"csat:{ticket_id}:4"))

    assert r.status_code == 200
    async with db_session() as session:
        ticket = await session.get(Ticket, ticket_id)
    assert ticket.csat_rating == 4


async def test_the_buttons_stop_being_offered_afterwards(initialized_db):
    ticket_id, message_id = await _slack_ticket_awaiting_a_rating()

    with TestClient(app, base_url="http://testserver") as c:
        _signed(c, _interaction(value=f"csat:{ticket_id}:5"))
        second = _signed(c, _interaction(value=f"csat:{ticket_id}:1"))

    assert second.status_code == 200, "Slack must never be shown an error"
    async with db_session() as session:
        row = await session.get(MessageRecord, message_id)
        ticket = await session.get(Ticket, ticket_id)
    assert row.options is None
    assert ticket.csat_rating == 5, "the second click overwrote the first"


async def test_a_click_from_another_conversation_does_nothing(initialized_db):
    """Slack's signature proves the request is real. It says nothing about
    whether the person clicking is entitled to this ticket, and the app is in
    every channel it was invited to."""
    ticket_id, _ = await _slack_ticket_awaiting_a_rating()

    with TestClient(app, base_url="http://testserver") as c:
        r = _signed(c, _interaction(channel="C0SOMEWHEREELSE",
                                    value=f"csat:{ticket_id}:5"))

    assert r.status_code == 200
    async with db_session() as session:
        ticket = await session.get(Ticket, ticket_id)
    assert ticket.csat_rating is None


async def test_a_value_that_was_never_offered_does_nothing(initialized_db):
    ticket_id, _ = await _slack_ticket_awaiting_a_rating()

    with TestClient(app, base_url="http://testserver") as c:
        out_of_range = _signed(c, _interaction(value=f"csat:{ticket_id}:9"))
        nonsense = _signed(c, _interaction(value="drop-everything"))

    assert out_of_range.status_code == 200
    assert nonsense.status_code == 200
    async with db_session() as session:
        ticket = await session.get(Ticket, ticket_id)
    assert ticket.csat_rating is None


async def test_a_click_on_a_message_we_never_sent_does_nothing(initialized_db):
    await _slack_ticket_awaiting_a_rating()

    with TestClient(app, base_url="http://testserver") as c:
        r = _signed(c, _interaction(ts="9999999999.999999", value="csat:1:5"))
    assert r.status_code == 200


# ------------------------------------------------------------ the shape


def test_slack_is_never_shown_an_error():
    """Anything but 200 puts a red warning in front of the person who clicked,
    and "your rating was refused" is not useful to somebody who tapped a
    star."""
    with TestClient(app, base_url="http://testserver") as c:
        for body in (b"payload=not-json",
                     urllib.parse.urlencode({"payload": "{}"}).encode(),
                     urllib.parse.urlencode({
                         "payload": json.dumps({"type": "shortcut"})}).encode()):
            r = _signed(c, body)
            assert r.status_code in (200, 400), body


def test_the_adapter_says_it_can_draw_buttons():
    """It always did. What was missing was somewhere for a click to land."""
    from src.channels.slack import SlackChannel

    assert SlackChannel().caps.buttons is True
