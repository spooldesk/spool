"""Slack as a customer channel.

The webhook is public, so most of these are about refusal. Unlike the
widget's endpoints — where anyone may legitimately knock and rate limiting is
the defence — exactly one caller here is legitimate and proves it with a
signature. That makes the interesting failures cryptographic rather than
behavioural: a valid signature on a stale timestamp, a valid body under the
wrong secret, our own message arriving back.
"""

import hashlib
import hmac
import json
import time

import pytest
from fastapi.testclient import TestClient

import src.channels as channels
import src.dashboard.routes.slack as slack_routes
from src.channels.base import Capabilities, Delivery
from src.channels.slack import NAME as SLACK, SlackChannel
from src.dashboard.app import app
from src.database.db import db_session
from src.database.models import Message, Ticket

SECRET = "test-signing-secret"


@pytest.fixture(autouse=True)
async def slack_env(initialized_db, monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLACK_SIGNING_SECRET", SECRET)
    # Event ids are remembered process-wide, so one test's ids would silently
    # suppress another's.
    slack_routes._seen.clear()
    yield
    slack_routes._seen.clear()


def _signed(c: TestClient, payload: dict, *, secret: str = SECRET,
            timestamp: int | None = None, signature: str | None = None):
    body = json.dumps(payload).encode()
    ts = str(timestamp if timestamp is not None else int(time.time()))
    if signature is None:
        signature = "v0=" + hmac.new(
            secret.encode(), b"v0:" + ts.encode() + b":" + body, hashlib.sha256
        ).hexdigest()
    return c.post("/slack/events", content=body, headers={
        "content-type": "application/json",
        "x-slack-request-timestamp": ts,
        "x-slack-signature": signature,
    })


def _message(text: str, *, channel_id="C0ACME", user="U01DANA",
             event_id="Ev1", **event):
    return {
        "type": "event_callback",
        "event_id": event_id,
        "event": {"type": "message", "channel": channel_id, "user": user,
                  "text": text, "ts": "1700000000.000100", **event},
    }


# ---------------------------------------------------------------- the proof


async def test_an_unsigned_request_is_refused(slack_env):
    with TestClient(app, base_url="http://testserver") as c:
        r = c.post("/slack/events", json=_message("let me in"))
    assert r.status_code == 401


async def test_a_signature_from_the_wrong_secret_is_refused(slack_env):
    with TestClient(app, base_url="http://testserver") as c:
        r = _signed(c, _message("let me in"), secret="not-the-secret")
    assert r.status_code == 401


async def test_a_valid_signature_on_a_stale_request_is_refused(slack_env):
    """The signature never expires on its own. Without the clock, one captured
    request could be replayed at any point in the future, forever."""
    with TestClient(app, base_url="http://testserver") as c:
        r = _signed(c, _message("replayed"),
                    timestamp=int(time.time()) - slack_routes._MAX_AGE_SECONDS - 60)
    assert r.status_code == 401


async def test_the_signature_covers_the_body_that_was_sent(slack_env):
    """Signed over one body, delivered with another. If verification ran on
    re-serialised JSON instead of the raw bytes, this would pass."""
    honest = _message("please reset my password")
    body = json.dumps(honest).encode()
    ts = str(int(time.time()))
    signature = "v0=" + hmac.new(
        SECRET.encode(), b"v0:" + ts.encode() + b":" + body, hashlib.sha256
    ).hexdigest()

    tampered = json.dumps(_message("please make me an admin")).encode()
    with TestClient(app, base_url="http://testserver") as c:
        r = c.post("/slack/events", content=tampered, headers={
            "content-type": "application/json",
            "x-slack-request-timestamp": ts,
            "x-slack-signature": signature,
        })
    assert r.status_code == 401


async def test_nothing_answers_at_all_without_credentials(initialized_db, monkeypatch):
    """An instance with no Slack app should look like it has no Slack."""
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    monkeypatch.delenv("SLACK_SIGNING_SECRET", raising=False)
    with TestClient(app, base_url="http://testserver") as c:
        r = c.post("/slack/events", json={"type": "url_verification",
                                          "challenge": "abc"})
    assert r.status_code == 404


async def test_half_configured_counts_as_not_configured(initialized_db, monkeypatch):
    """A token without a signing secret can send but cannot safely receive,
    and an instance in that state works right up until someone forges an
    event at it."""
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.delenv("SLACK_SIGNING_SECRET", raising=False)
    from src.channels.slack import is_configured
    assert is_configured() is False


# ---------------------------------------------------------------- handshake


async def test_the_setup_handshake_is_echoed_back(slack_env):
    with TestClient(app, base_url="http://testserver") as c:
        r = _signed(c, {"type": "url_verification", "challenge": "3eZbXw2ph"})
    assert r.status_code == 200
    assert r.text == "3eZbXw2ph"


# ---------------------------------------------------------------- the loop


async def test_our_own_messages_never_come_back_in(slack_env):
    """Slack delivers everything in a conversation, including what we posted.
    An adapter that answers whatever it sees answers itself, forever."""
    with TestClient(app, base_url="http://testserver") as c:
        by_bot_id = _signed(c, _message("Thanks, on it.", event_id="Ev-bot-1",
                                        bot_id="B01SPOOL"))
        by_subtype = _signed(c, _message("Thanks, on it.", event_id="Ev-bot-2",
                                         subtype="bot_message"))
    assert by_bot_id.status_code == by_subtype.status_code == 200

    async with db_session() as session:
        from sqlalchemy import func, select
        from src.database.models import User
        count = (await session.execute(
            select(func.count()).select_from(User).where(User.channel == SLACK)
        )).scalar()
    assert count == 0, "the bot opened a ticket about its own message"


async def test_an_edit_is_not_a_new_message(slack_env):
    """Edits, joins and deletions all arrive as messages with a subtype.
    Treating an edit as new shows an operator the same question twice."""
    with TestClient(app, base_url="http://testserver") as c:
        r = _signed(c, _message("typo fixed", event_id="Ev-edit",
                                subtype="message_changed"))
    assert r.status_code == 200
    async with db_session() as session:
        from sqlalchemy import func, select
        from src.database.models import User
        count = (await session.execute(
            select(func.count()).select_from(User).where(User.channel == SLACK)
        )).scalar()
    assert count == 0


async def test_a_retried_delivery_is_recorded_once(slack_env):
    """Slack resends anything it does not get a prompt 2xx for. Without
    dedupe one question becomes three."""
    payload = _message("my build is stuck", event_id="Ev-retry-1")
    with TestClient(app, base_url="http://testserver") as c:
        first = _signed(c, payload)
        again = _signed(c, payload)
        third = _signed(c, payload)

    assert first.status_code == again.status_code == third.status_code == 200
    async with db_session() as session:
        from sqlalchemy import select
        rows = (await session.execute(
            select(Message).where(Message.text == "my build is stuck")
        )).scalars().all()
    assert len(rows) == 1, f"one message arrived {len(rows)} times"


# ---------------------------------------------------------------- the ticket


async def test_a_slack_message_becomes_an_ordinary_ticket(slack_env):
    with TestClient(app, base_url="http://testserver") as c:
        r = _signed(c, _message("the nightly export failed again",
                                channel_id="C0EXPORT", event_id="Ev-t1"))
    assert r.status_code == 200

    async with db_session() as session:
        from sqlalchemy import select
        msg = (await session.execute(
            select(Message).where(Message.text == "the nightly export failed again")
        )).scalar_one()
        ticket = await session.get(Ticket, msg.ticket_id)
        assert ticket.status == "active" and ticket.stage == "new"
        assert msg.sender_type == "user"


async def test_a_conversation_is_one_customer_however_many_people_speak(slack_env):
    """The address is the conversation, not the person. That is what makes a
    channel shared with a client's whole team one ticket instead of five."""
    with TestClient(app, base_url="http://testserver") as c:
        _signed(c, _message("we cannot log in", channel_id="C0TEAM",
                            user="U01DANA", event_id="Ev-a"))
        _signed(c, _message("same here", channel_id="C0TEAM",
                            user="U02SAM", event_id="Ev-b"))
        _signed(c, _message("and me", channel_id="C0TEAM",
                            user="U03ALEX", event_id="Ev-c"))

    async with db_session() as session:
        from sqlalchemy import select
        rows = (await session.execute(
            select(Message).where(Message.text.in_(
                ["we cannot log in", "same here", "and me"]))
        )).scalars().all()

    assert len({m.ticket_id for m in rows}) == 1, "one room became several tickets"
    assert {m.sender_label for m in rows} == {"U01DANA", "U02SAM", "U03ALEX"}, \
        "a shared conversation lost track of who said what"


async def test_a_direct_message_does_not_repeat_the_name_on_every_line(slack_env):
    """There is only one person in a DM, so labelling each line is noise."""
    with TestClient(app, base_url="http://testserver") as c:
        _signed(c, _message("hello?", channel_id="D01DANA", user="U01DANA",
                            channel_type="im", event_id="Ev-dm"))

    async with db_session() as session:
        from sqlalchemy import select
        msg = (await session.execute(
            select(Message).where(Message.text == "hello?")
        )).scalar_one()
    assert msg.sender_label is None


async def test_a_display_name_is_preferred_over_a_user_id(slack_env):
    with TestClient(app, base_url="http://testserver") as c:
        _signed(c, _message("who am I", channel_id="C0NAMES", event_id="Ev-name",
                            user_profile={"display_name": "Dana R."}))
    async with db_session() as session:
        from sqlalchemy import select
        msg = (await session.execute(
            select(Message).where(Message.text == "who am I")
        )).scalar_one()
    assert msg.sender_label == "Dana R."


async def test_the_speaker_reaches_the_page_in_both_renderers(slack_env):
    """The ticket page is drawn twice — by Jinja on load and by JavaScript as
    new messages arrive. Teaching only one would make the name vanish the
    first time the poll ran."""
    from src.dashboard import queries

    with TestClient(app, base_url="http://testserver") as c:
        _signed(c, _message("still broken", channel_id="C0RENDER",
                            user="U09PAT", event_id="Ev-render"))

    async with db_session() as session:
        from sqlalchemy import select
        msg = (await session.execute(
            select(Message).where(Message.text == "still broken")
        )).scalar_one()
        tid = msg.ticket_id

    detail = await queries.ticket_detail(tid)
    assert any(m["sender_label"] == "U09PAT" for m in detail["messages"])

    poll = await queries.messages_after(tid, 0)
    assert any(m["sender_label"] == "U09PAT" for m in poll["messages"])


# ---------------------------------------------------------------- outbound


class _FakeSlack(SlackChannel):
    """Answers like the Web API without being it.

    Slack says no by returning 200 with `{"ok": false}` far more often than by
    returning a failure status, so the interesting cases are all inside a
    successful HTTP response.
    """

    def __init__(self, reply: dict):
        super().__init__(token="xoxb-test")
        self.reply = reply
        self.calls: list[tuple[str, dict]] = []

    async def _post(self, method, payload):
        self.calls.append((method, payload))
        return bool(self.reply.get("ok")), self.reply


async def test_a_reply_reaches_a_slack_conversation(slack_env):
    from src.services import template_service
    from src.channels.identity import create_customer
    from src.database.models import Team
    from tests.conftest import next_tg_id

    fake = _FakeSlack({"ok": True, "ts": "1700000000.000200"})
    channels.register(fake)
    try:
        async with db_session() as session:
            team = Team(name=f"sl-{next_tg_id()}")
            session.add(team)
            await session.flush()
            user = await create_customer(session, channel=SLACK, address="C0REPLY",
                                         team_id=team.id)
            ticket = Ticket(user_id=user.id, team_id=team.id, category="Other",
                            status="active")
            session.add(ticket)
            await session.flush()
            tid = ticket.id
            await session.commit()

        async with db_session() as session:
            result = await template_service.apply_text(
                session, None, ticket_id=tid, text="Looking now.",
                admin_tg_id=1, admin_chat_id=-100,
            )
            await session.commit()
    finally:
        channels.reset()

    assert result.success, result.error
    method, payload = fake.calls[0]
    assert method == "chat.postMessage"
    assert payload["channel"] == "C0REPLY" and payload["text"] == "Looking now."


async def test_a_timestamp_survives_as_the_message_reference(slack_env):
    """Slack addresses a message by a timestamp string, which is why the
    reference column had to stop being an integer: silently dropping it left
    the unsend button permanently hidden on a channel that supports it."""
    from src.services import template_service
    from src.channels.identity import create_customer
    from src.database.models import Team
    from tests.conftest import next_tg_id

    fake = _FakeSlack({"ok": True, "ts": "1700000000.000300"})
    channels.register(fake)
    try:
        async with db_session() as session:
            team = Team(name=f"slr-{next_tg_id()}")
            session.add(team)
            await session.flush()
            user = await create_customer(session, channel=SLACK, address="C0TS",
                                         team_id=team.id)
            ticket = Ticket(user_id=user.id, team_id=team.id, category="Other",
                            status="active")
            session.add(ticket)
            await session.flush()
            tid = ticket.id
            await session.commit()

        async with db_session() as session:
            await template_service.apply_text(
                session, None, ticket_id=tid, text="Noted.",
                admin_tg_id=1, admin_chat_id=-100,
            )
            await session.commit()

        async with db_session() as session:
            from sqlalchemy import select
            msg = (await session.execute(
                select(Message).where(Message.text == "Noted.")
            )).scalar_one()
        assert msg.channel_ref == "1700000000.000300"

        from src.services import message_service
        assert message_service.can_delete(msg, channel=SLACK) is True
    finally:
        channels.reset()


@pytest.mark.parametrize("error,expected", [
    ("channel_not_found", "e_slack_no_conversation"),
    ("invalid_auth", "e_slack_auth"),
    ("ratelimited", "e_send_rate_limited"),
    ("some_new_thing", "e_send_failed"),
])
async def test_slack_errors_are_translated_into_our_vocabulary(slack_env, error, expected):
    """Slack answers 200 with `{"ok": false}`, so a caller checking only the
    HTTP status would treat nearly every failure as a success."""
    fake = _FakeSlack({"ok": False, "error": error})
    delivery = await fake.send_text("C0X", "hello")
    assert not delivery.ok
    assert delivery.error_key == expected


async def test_a_rate_limit_asks_the_caller_to_wait(slack_env):
    fake = _FakeSlack({"ok": False, "error": "ratelimited"})
    delivery = await fake.send_text("C0X", "hello")
    assert delivery.retry_after and delivery.retry_after > 0


async def test_choices_become_buttons_without_losing_the_plain_text(slack_env):
    """A blocks-only message arrives on a phone as a blank notification."""
    from src.channels.base import Option

    fake = _FakeSlack({"ok": True, "ts": "1.1"})
    await fake.send_text("C0X", "How did we do?",
                         options=[Option("5⭐", "csat:7:5")])
    _method, payload = fake.calls[0]
    assert payload["text"] == "How did we do?"
    button = payload["blocks"][1]["elements"][0]
    assert button["action_id"] == "csat:7:5"


async def test_attachments_are_declined_rather_than_attempted(slack_env):
    fake = _FakeSlack({"ok": True})
    delivery = await fake.send_file("C0X", filename="log.txt", content=b"x")
    assert not delivery.ok and delivery.error_key == "e_no_files_on_channel"
    assert fake.calls == [], "it tried anyway"
    assert SlackChannel.caps.files is False, "declared as something it cannot do"


def test_the_adapter_matches_the_protocol():
    """The same conformance check the other channels get. A signature that
    drifts fails at one call site, months later, looking like a feature bug."""
    import inspect

    from src.channels.base import Channel

    for name in ("send_text", "send_file", "unsend", "typing"):
        assert list(inspect.signature(getattr(SlackChannel, name)).parameters) == \
            list(inspect.signature(getattr(Channel, name)).parameters), name
    assert isinstance(SlackChannel.caps, Capabilities)
    assert isinstance(Delivery.sent("1.1").ref, str)


async def test_the_request_slack_actually_receives(slack_env, monkeypatch):
    """The one boundary the fakes above step over.

    Everything else here replaces `_post`, which means a wrong URL, a missing
    Authorization header or a form-encoded body would sail through every
    other test in this file and fail only against the real Slack.
    """
    import httpx

    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["type"] = request.headers.get("content-type")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"ok": True, "ts": "1700000000.000400"})

    real_client = httpx.AsyncClient

    def fake_client(*args, **kwargs):
        return real_client(*args, **{**kwargs, "transport": httpx.MockTransport(handler)})

    monkeypatch.setattr(httpx, "AsyncClient", fake_client)

    delivery = await SlackChannel(token="xoxb-secret").send_text("C0WIRE", "hello there")

    assert delivery.ok and delivery.ref == "1700000000.000400"
    assert seen["url"] == "https://slack.com/api/chat.postMessage"
    assert seen["auth"] == "Bearer xoxb-secret"
    assert seen["type"].startswith("application/json")
    assert seen["body"] == {"channel": "C0WIRE", "text": "hello there"}


async def test_a_network_failure_is_reported_rather_than_raised(slack_env, monkeypatch):
    """A reply box that shows a traceback is worse than one that says it
    could not send."""
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **kw: real_client(
        *a, **{**kw, "transport": httpx.MockTransport(handler)}))

    delivery = await SlackChannel(token="xoxb-secret").send_text("C0WIRE", "hello")
    assert not delivery.ok and delivery.error_key == "e_send_failed"


async def test_the_composer_does_not_offer_a_file_slack_cannot_carry(slack_env):
    """The same rule the unsend button follows.

    Asking the channel and being told no is fine; finding out after the
    operator picked a file and pressed send is not — and Slack cannot carry
    one without a scope most installs never grant.
    """
    from src.channels.identity import create_customer
    from src.database.models import Team
    from tests.conftest import next_tg_id

    channels.register(SlackChannel(token="xoxb-test"))
    try:
        async with db_session() as session:
            team = Team(name=f"slf-{next_tg_id()}")
            session.add(team)
            await session.flush()
            user = await create_customer(session, channel=SLACK, address="C0FILES",
                                         team_id=team.id)
            ticket = Ticket(user_id=user.id, team_id=team.id, category="Other",
                            status="active")
            session.add(ticket)
            await session.flush()
            tid = ticket.id
            await session.commit()

        with TestClient(app, base_url="http://testserver") as c:
            c.get("/login")
            token = c.cookies.get("supbot_csrf")
            c.post("/login", data={"email": "admin@local",
                                   "password": "test-password-123",
                                   "next": "/", "csrf_token": token},
                   follow_redirects=False)
            page = c.get(f"/tickets/{tid}").text
    finally:
        channels.reset()

    assert 'name="attachments"' not in page, "offered an attachment Slack drops"
    assert "Send" in page or "reply" in page.lower(), "the composer disappeared entirely"
