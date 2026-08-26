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
from tests.conftest import next_tg_id

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


async def _slack_customers() -> int:
    """How many customers have ever arrived through Slack.

    Used as a before/after difference, never as an absolute: every test file
    shares one database, so a total is really a claim about running order.
    """
    from sqlalchemy import func, select

    from src.database.models import User

    async with db_session() as session:
        return (await session.execute(
            select(func.count()).select_from(User).where(User.channel == SLACK)
        )).scalar()


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
    before = await _slack_customers()
    with TestClient(app, base_url="http://testserver") as c:
        by_bot_id = _signed(c, _message("Thanks, on it.", event_id="Ev-bot-1",
                                        bot_id="B01SPOOL"))
        by_subtype = _signed(c, _message("Thanks, on it.", event_id="Ev-bot-2",
                                         subtype="bot_message"))
    assert by_bot_id.status_code == by_subtype.status_code == 200
    assert await _slack_customers() == before, \
        "the bot opened a ticket about its own message"


async def test_an_edit_is_not_a_new_message(slack_env):
    """Edits, joins and deletions all arrive as messages with a subtype.
    Treating an edit as new shows an operator the same question twice."""
    before = await _slack_customers()
    with TestClient(app, base_url="http://testserver") as c:
        r = _signed(c, _message("typo fixed", event_id="Ev-edit",
                                subtype="message_changed"))
    assert r.status_code == 200
    assert await _slack_customers() == before, "an edit opened a new ticket"


async def test_a_retried_delivery_is_recorded_once(slack_env):
    """Slack resends anything it does not get a prompt 2xx for. Without
    dedupe one question becomes three."""
    # A phrase no other test writes: the suite shares a database, and
    # "how many rows say this" is only a question about retries if nothing
    # else says it.
    said = f"my build is stuck {next_tg_id()}"
    payload = _message(said, event_id=f"Ev-retry-{next_tg_id()}")
    with TestClient(app, base_url="http://testserver") as c:
        first = _signed(c, payload)
        again = _signed(c, payload)
        third = _signed(c, payload)

    assert first.status_code == again.status_code == third.status_code == 200
    async with db_session() as session:
        from sqlalchemy import select
        rows = (await session.execute(
            select(Message).where(Message.text == said)
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


async def test_an_upload_takes_all_three_steps_slack_now_requires(slack_env, monkeypatch):
    """`files.upload` was one call and was retired in 2025. The replacement
    hands out a URL, takes the bytes over plain HTTP, then wants a third call
    to say where the file belongs — and nothing is visible to the customer
    until that third one lands."""
    import httpx

    steps: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("files.getUploadURLExternal"):
            steps.append("reserve")
            return httpx.Response(200, json={
                "ok": True, "upload_url": "https://files.slack.test/upload/abc",
                "file_id": "F0ABC",
            })
        if url.startswith("https://files.slack.test/"):
            steps.append("bytes")
            assert b"the log itself" in request.content, "the file never went"
            return httpx.Response(200, text="OK")
        if url.endswith("files.completeUploadExternal"):
            steps.append("complete")
            import json as _json
            body = _json.loads(request.content)
            assert body["channel_id"] == "C0FILES"
            assert body["files"] == [{"id": "F0ABC", "title": "log.txt"}]
            assert body["initial_comment"] == "here you go"
            return httpx.Response(200, json={
                "ok": True, "files": [{"id": "F0ABC", "timestamp": "1700000000.000900"}],
            })
        raise AssertionError(f"unexpected call to {url}")

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **kw: real(
        *a, **{**kw, "transport": httpx.MockTransport(handler)}))

    delivery = await SlackChannel(token="xoxb-test").send_file(
        "C0FILES", filename="log.txt", content=b"the log itself",
        caption="here you go",
    )

    assert steps == ["reserve", "bytes", "complete"]
    assert delivery.ok and delivery.ref == "1700000000.000900"


async def test_a_missing_scope_stops_the_channel_offering_files(slack_env):
    """A scope is a fact about somebody's install, not about the medium. The
    adapter starts optimistic and demotes itself the first time Slack says
    no, so the button stops appearing instead of failing for every operator
    in turn."""
    channel = _FakeSlack({"ok": False, "error": "missing_scope"})
    assert channel.caps.files is True

    delivery = await channel.send_file("C0X", filename="log.txt", content=b"x")
    assert not delivery.ok and delivery.error_key == "e_slack_files_scope"
    assert channel.caps.files is False, "it kept advertising a refused scope"
    assert SlackChannel.caps.files is True, "one install's refusal changed the class"

    # And the next attempt is refused before anything leaves.
    calls_before = len(channel.calls)
    again = await channel.send_file("C0X", filename="log.txt", content=b"x")
    assert not again.ok and len(channel.calls) == calls_before


async def test_a_file_a_customer_sent_can_be_fetched_back(slack_env, monkeypatch):
    """`url_private` is not public despite being a URL."""
    import httpx

    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, content=b"PNGDATA",
                              headers={"content-type": "image/png"})

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **kw: real(
        *a, **{**kw, "transport": httpx.MockTransport(handler)}))

    data = await SlackChannel(token="xoxb-secret").fetch_file(
        "https://files.slack.com/files-pri/T1-F1/error.png")
    assert data == b"PNGDATA"
    assert seen["auth"] == "Bearer xoxb-secret"


async def test_a_sign_in_page_is_not_mistaken_for_a_file(slack_env, monkeypatch):
    """Without the token Slack answers 200 with HTML, which would otherwise be
    stored as a corrupt image and puzzled over later."""
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>Sign in to Slack</html>",
                              headers={"content-type": "text/html"})

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **kw: real(
        *a, **{**kw, "transport": httpx.MockTransport(handler)}))

    assert await SlackChannel(token="xoxb-secret").fetch_file(
        "https://files.slack.com/files-pri/T1-F1/error.png") is None


def test_the_adapter_matches_the_protocol():
    """The same conformance check the other channels get. A signature that
    drifts fails at one call site, months later, looking like a feature bug."""
    import inspect

    from src.channels.base import Channel

    for name in ("send_text", "send_file", "fetch_file", "unsend", "typing"):
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


async def test_the_composer_offers_what_the_channel_declares(slack_env):
    """The composer reads capabilities rather than assuming. Slack says it
    can carry files, so the button is there; the widget says it cannot, and
    the test in test_widget.py checks the other half of the same rule."""
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

    assert 'name="attachments"' in page, "Slack can carry a file; the button should be there"
    assert "Send" in page or "reply" in page.lower(), "the composer disappeared entirely"


# ---------------------------------------------------------------- inbound files


def _with_files(text: str, files: list[dict], **kw):
    payload = _message(text, **kw)
    payload["event"]["files"] = files
    return payload


async def test_a_file_a_customer_sent_arrives_with_the_message(slack_env):
    with TestClient(app, base_url="http://testserver") as c:
        r = _signed(c, _with_files(
            "screenshot of the error",
            [{"name": "error.png", "mimetype": "image/png", "size": 4096,
              "url_private": "https://files.slack.com/files-pri/T1-F1/error.png"}],
            channel_id="C0SHOT", event_id="Ev-file-1",
        ))
    assert r.status_code == 200

    async with db_session() as session:
        from sqlalchemy import select
        msg = (await session.execute(
            select(Message).where(Message.text == "screenshot of the error")
        )).scalar_one()
    assert msg.file_name == "error.png"
    assert msg.mime_type == "image/png"
    assert msg.file_id == "https://files.slack.com/files-pri/T1-F1/error.png"


async def test_a_file_with_no_words_still_opens_a_ticket(slack_env):
    """People drop a screenshot and say nothing, constantly."""
    with TestClient(app, base_url="http://testserver") as c:
        r = _signed(c, _with_files(
            "",
            [{"name": "trace.log", "mimetype": "text/plain", "size": 900,
              "url_private": "https://files.slack.com/files-pri/T1-F2/trace.log"}],
            channel_id="C0SILENT", event_id="Ev-file-2",
        ))
    assert r.status_code == 200

    async with db_session() as session:
        from sqlalchemy import select
        rows = (await session.execute(
            select(Message).where(Message.file_name == "trace.log")
        )).scalars().all()
    assert len(rows) == 1


async def test_several_files_become_several_openable_messages(slack_env):
    """One Slack message, three attachments. Recorded a row each — which is
    how Slack shows them, and it keeps every one individually openable."""
    with TestClient(app, base_url="http://testserver") as c:
        _signed(c, _with_files(
            "all three logs",
            [{"name": f"n{i}.log", "mimetype": "text/plain", "size": 10,
              "url_private": f"https://files.slack.com/files-pri/T1-F{i}/n{i}.log"}
             for i in range(3)],
            channel_id="C0MANY", event_id="Ev-file-3",
        ))

    async with db_session() as session:
        from sqlalchemy import select
        rows = (await session.execute(
            select(Message).where(Message.file_name.in_(["n0.log", "n1.log", "n2.log"]))
            .order_by(Message.id)
        )).scalars().all()

    assert [m.file_name for m in rows] == ["n0.log", "n1.log", "n2.log"]
    assert len({m.ticket_id for m in rows}) == 1, "one message became several tickets"
    assert rows[0].text == "all three logs", "the words belong on the first one"
    assert rows[1].text == "" and rows[2].text == ""


async def test_a_file_slack_will_not_hand_over_is_skipped(slack_env):
    """External and hidden files come with a card and no bytes. Recording a
    reference we cannot fetch would put a permanently broken attachment into
    the transcript."""
    with TestClient(app, base_url="http://testserver") as c:
        _signed(c, _with_files(
            "see the doc",
            [{"name": "external.pdf", "mimetype": "application/pdf"}],
            channel_id="C0EXT", event_id="Ev-file-4",
        ))

    async with db_session() as session:
        from sqlalchemy import select
        msg = (await session.execute(
            select(Message).where(Message.text == "see the doc")
        )).scalar_one()
    assert msg.file_id is None and msg.file_name is None


async def test_the_attachment_route_asks_the_channel_not_telegram(slack_env, monkeypatch):
    """It used to call the Bot API directly, which quietly made every
    attachment in the product a Telegram attachment."""
    import httpx

    from src.channels.identity import create_customer
    from src.database.models import Team
    from tests.conftest import next_tg_id

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"REALBYTES",
                              headers={"content-type": "image/png"})

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **kw: real(
        *a, **{**kw, "transport": httpx.MockTransport(handler)}))

    channels.register(SlackChannel(token="xoxb-test"))
    try:
        async with db_session() as session:
            team = Team(name=f"sla-{next_tg_id()}")
            session.add(team)
            await session.flush()
            user = await create_customer(session, channel=SLACK, address="C0ATT",
                                         team_id=team.id)
            ticket = Ticket(user_id=user.id, team_id=team.id, category="Other",
                            status="active")
            session.add(ticket)
            await session.flush()
            msg = Message(ticket_id=ticket.id, sender_type="user",
                          sender_id=user.tg_id, text="here",
                          file_id="https://files.slack.com/files-pri/T1-F9/a.png",
                          file_name="a.png", mime_type="image/png")
            session.add(msg)
            await session.flush()
            mid = msg.id
            await session.commit()

        with TestClient(app, base_url="http://testserver") as c:
            c.get("/login")
            token = c.cookies.get("supbot_csrf")
            c.post("/login", data={"email": "admin@local",
                                   "password": "test-password-123",
                                   "next": "/", "csrf_token": token},
                   follow_redirects=False)
            r = c.get(f"/tickets/attachment/{mid}")
    finally:
        channels.reset()

    assert r.status_code == 200
    assert r.content == b"REALBYTES"
    assert r.headers["content-type"].startswith("image/png")


# ---------------------------------------------------------------- names


async def test_a_user_id_becomes_a_name_when_the_scope_allows_it(slack_env):
    looked_up: list[str] = []

    class _Named(_FakeSlack):
        async def _post(self, method, payload):
            if method == "users.info":
                looked_up.append(payload["user"])
                return True, {"ok": True, "user": {"profile": {
                    "display_name": "Dana R.", "real_name": "Dana Robinson"}}}
            return await super()._post(method, payload)

    channels.register(_Named({"ok": True}))
    try:
        with TestClient(app, base_url="http://testserver") as c:
            _signed(c, _message("who am I really", channel_id="C0NAME2",
                                user="U77DANA", event_id="Ev-name-2"))
            # Asked once, then remembered.
            _signed(c, _message("still me", channel_id="C0NAME2",
                                user="U77DANA", event_id="Ev-name-3"))
    finally:
        channels.reset()

    assert looked_up == ["U77DANA"], f"asked Slack {len(looked_up)} times for one name"
    async with db_session() as session:
        from sqlalchemy import select
        rows = (await session.execute(
            select(Message).where(Message.text.in_(["who am I really", "still me"]))
        )).scalars().all()
    assert {m.sender_label for m in rows} == {"Dana R."}


async def test_without_the_scope_the_id_is_used_and_asked_for_once(slack_env):
    """`users:read` is often not granted. An id is not a name, but it still
    tells two people apart, which is the label's actual job."""
    asked: list[str] = []

    class _Refused(_FakeSlack):
        async def _post(self, method, payload):
            if method == "users.info":
                asked.append(payload["user"])
                return False, {"ok": False, "error": "missing_scope"}
            return await super()._post(method, payload)

    channels.register(_Refused({"ok": True}))
    try:
        with TestClient(app, base_url="http://testserver") as c:
            _signed(c, _message("no name here", channel_id="C0NOSCOPE",
                                user="U88SAM", event_id="Ev-name-4"))
            _signed(c, _message("nor here", channel_id="C0NOSCOPE",
                                user="U88SAM", event_id="Ev-name-5"))
    finally:
        channels.reset()

    assert asked == ["U88SAM"], "a missing scope cost one call per message"
    async with db_session() as session:
        from sqlalchemy import select
        rows = (await session.execute(
            select(Message).where(Message.text.in_(["no name here", "nor here"]))
        )).scalars().all()
    assert {m.sender_label for m in rows} == {"U88SAM"}


@pytest.mark.parametrize("url", [
    "https://169.254.169.254/latest/meta-data/",
    "http://files.slack.com/files-pri/T1-F1/a.png",
    "https://files.slack.com.evil.test/a.png",
    "https://evil.test/?x=https://files.slack.com/",
    "",
])
async def test_a_file_reference_that_is_not_slacks_is_not_fetched(slack_env, url):
    """The reference comes out of an event payload and the fetch carries our
    bearer token, so an unrestricted fetch would be a way to make the server
    reach an address of somebody else's choosing with a credential attached.
    Forging such an event needs the signing secret — unlikely, not
    impossible — and pinning the host costs nothing."""
    assert await SlackChannel(token="xoxb-secret").fetch_file(url) is None
