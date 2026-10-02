"""Failure boundaries: accepted input survives, and retry does not duplicate it."""

import asyncio
import hashlib
import hmac
import json
import time
import uuid
from email.message import EmailMessage
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select

from src.database.db import db_session
from src.database.inbound_models import InboundEvent
from src.database.models import Message, Ticket
from src.services import email_ingest, inbound_service


def mail(text="Question", *, message_id=None, reply_to=None, address="visitor@example.test"):
    message = EmailMessage()
    message["From"] = address
    message["To"] = "support@example.test"
    message["Subject"] = text
    message["Message-ID"] = message_id or f"<{uuid.uuid4().hex}@example.test>"
    if reply_to:
        message["In-Reply-To"] = reply_to
    message.set_content(text)
    return message


@pytest.fixture(autouse=True)
async def isolated(initialized_db, monkeypatch, tmp_path):
    async with db_session(write=True) as session:
        await session.execute(delete(InboundEvent))
    monkeypatch.setenv("EMAIL_ADDRESS", "support@example.test")
    monkeypatch.setenv("EMAIL_PASSWORD", "test-only")
    monkeypatch.setenv("EMAIL_IMAP_HOST", "imap.example.test")
    monkeypatch.setenv("EMAIL_SMTP_HOST", "smtp.example.test")
    monkeypatch.setenv("ATTACHMENTS_DIR", str(tmp_path / "attachments"))
    monkeypatch.setattr("src.dashboard.routes.slack._speaker", AsyncMock(return_value="Visitor"))
    yield
    async with db_session(write=True) as session:
        await session.execute(delete(InboundEvent))


def fetched(message, *, uid=b"1", validity=None):
    return email_ingest.FetchedMail(uid, validity or str(uuid.uuid4().int), message.as_bytes())


async def count_text(text):
    async with db_session() as session:
        return (await session.execute(select(func.count(Message.id)).where(Message.text == text))).scalar_one()


async def test_email_partial_intake_rolls_back_and_retries_after_restart(monkeypatch):
    original = mail("atomic " + uuid.uuid4().hex)
    original.add_attachment(b"first", maintype="application", subtype="octet-stream", filename="one.txt")
    original.add_attachment(b"second", maintype="application", subtype="octet-stream", filename="two.txt")
    raw = fetched(original)
    marked = []
    monkeypatch.setattr(email_ingest, "_fetch_unseen", lambda settings: [raw])
    monkeypatch.setattr(email_ingest, "_mark_seen", lambda settings, messages: marked.extend(messages))
    real_record = email_ingest.record_event

    async def fail_after_writes(*args, **kwargs):
        await real_record(*args, **kwargs)
        raise OSError("simulated commit-boundary failure")

    monkeypatch.setattr(email_ingest, "record_event", fail_after_writes)
    result = await email_ingest.run_once()
    assert result.get("error")
    assert marked == [raw], "Seen is safe only after the original raw input is durable"
    assert await count_text(original["Subject"]) == 0
    async with db_session() as session:
        event = (await session.execute(select(InboundEvent).where(
            InboundEvent.payload == raw.payload))).scalar_one()
        assert event.status == "pending" and event.last_error == "OSError"
        event_id = event.id
    monkeypatch.setattr(email_ingest, "record_event", real_record)
    # Recovery reads persisted payload; no process-local receipt/state is used.
    recovered = await inbound_service.process(event_id, force=True)
    assert recovered.status == "processed"
    await email_ingest.run_once()
    assert marked == [raw, raw]
    assert await count_text(original["Subject"]) == 1
    async with db_session() as session:
        rows = (await session.execute(select(Message).where(Message.ticket_id == recovered.ticket_id))).scalars().all()
    assert sorted(m.file_name for m in rows if m.file_name) == ["one.txt", "two.txt"]


async def test_email_enqueue_failure_never_marks_seen(monkeypatch):
    raw = fetched(mail("not accepted " + uuid.uuid4().hex))
    marked = []
    monkeypatch.setattr(email_ingest, "_fetch_unseen", lambda settings: [raw])
    monkeypatch.setattr(email_ingest, "_mark_seen", lambda settings, messages: marked.extend(messages))
    monkeypatch.setattr(inbound_service, "enqueue", AsyncMock(side_effect=OSError("database offline")))
    assert (await email_ingest.run_once()).get("error")
    assert not marked


async def test_email_failed_seen_update_replays_without_duplicate(monkeypatch):
    message = mail("seen retry " + uuid.uuid4().hex)
    raw = fetched(message)
    monkeypatch.setattr(email_ingest, "_fetch_unseen", lambda settings: [raw])
    def fail_seen(*args):
        raise OSError("mailbox offline after commit")
    monkeypatch.setattr(email_ingest, "_mark_seen", fail_seen)
    assert (await email_ingest.run_once()).get("error")
    assert (await email_ingest.run_once()).get("error")
    assert await count_text(message["Subject"]) == 1


async def test_same_message_id_after_uidvalidity_change_is_deduplicated(monkeypatch):
    message = mail("mailbox reset " + uuid.uuid4().hex)
    messages = [fetched(message), fetched(message)]
    monkeypatch.setattr(email_ingest, "_fetch_unseen", lambda settings: [messages.pop(0)])
    monkeypatch.setattr(email_ingest, "_mark_seen", lambda settings, messages: None)
    await email_ingest.run_once()
    await email_ingest.run_once()
    assert await count_text(message["Subject"]) == 1


async def test_email_separate_topics_and_replies_keep_their_own_ticket():
    from src.channels import email_parse
    first, second = mail("First topic"), mail("Second topic")
    one = await email_ingest._record(email_parse.parse(first))
    two = await email_ingest._record(email_parse.parse(second))
    assert one != two
    reply = mail("Answer first", reply_to=first["Message-ID"])
    assert await email_ingest._record(email_parse.parse(reply)) == one
    async with db_session(write=True) as session:
        (await session.get(Ticket, one)).status = "closed"
    reopened = await email_ingest._record(email_parse.parse(mail("After close", reply_to=reply["Message-ID"])))
    assert reopened not in (one, two)
    async with db_session() as session:
        assert (await session.get(Ticket, reopened)).conversation_key == (await session.get(Ticket, one)).conversation_key


async def test_reference_from_another_sender_does_not_join_their_ticket():
    from src.channels import email_parse
    original = mail("Private topic", address="owner@example.test")
    original_ticket = await email_ingest._record(email_parse.parse(original))
    outsider = mail("Forged reference", reply_to=original["Message-ID"], address="other@example.test")
    assert await email_ingest._record(email_parse.parse(outsider)) != original_ticket


async def test_email_reply_headers_use_exact_ticket_and_stable_delivery_id(monkeypatch):
    from src.channels import email_parse
    from src.channels.email import EmailChannel
    original = mail("Billing " + uuid.uuid4().hex)
    ticket_id = await email_ingest._record(email_parse.parse(original))
    sent = []
    monkeypatch.setattr("src.channels.email._deliver", lambda settings, message: sent.append(message))
    channel = EmailChannel()
    first = await channel.send_for_ticket("visitor@example.test", "Answer", ticket_id=ticket_id, delivery_key="job-42")
    second = await channel.send_for_ticket("visitor@example.test", "Answer", ticket_id=ticket_id, delivery_key="job-42")
    assert first.ok and second.ok and first.ref == second.ref
    assert sent[0]["In-Reply-To"] == sent[1]["In-Reply-To"] == original["Message-ID"]
    assert sent[0]["Subject"] == "Re: " + original["Subject"]
    from email.utils import parsedate_to_datetime
    assert parsedate_to_datetime(sent[0]["Date"]).tzinfo is not None
    follow_up = mail("Reply to operator", reply_to=sent[0]["Message-ID"])
    assert await email_ingest._record(email_parse.parse(follow_up)) == ticket_id


def test_imap_uses_uid_and_validates_tls_timeout_and_uidvalidity(monkeypatch):
    from src.config import load_settings
    calls = []

    class Box:
        def __init__(self, host, port, **kwargs):
            assert kwargs["timeout"] > 0
            assert kwargs["ssl_context"].check_hostname
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def login(self, *args): pass
        def select(self, *args): return "OK", [b"2"]
        def response(self, name): return name, [b"999"]
        def uid(self, command, *args):
            calls.append((command, args))
            if command == "SEARCH": return "OK", [b"42"]
            if command == "FETCH" and args[1] == "(RFC822.SIZE)": return "OK", [b"1 (UID 42 RFC822.SIZE 4)"]
            if command == "FETCH": return "OK", [(b"1 (UID 42 BODY[] {4}", b"mail")]
            return "OK", [b""]
        def search(self, *args): raise AssertionError("sequence SEARCH")
        def fetch(self, *args): raise AssertionError("sequence FETCH")
        def store(self, *args): raise AssertionError("sequence STORE")

    monkeypatch.setattr(email_ingest.imaplib, "IMAP4_SSL", Box)
    result = email_ingest._fetch_unseen(load_settings())
    assert result == [email_ingest.FetchedMail(b"42", "999", b"mail")]
    email_ingest._mark_seen(load_settings(), result)
    assert calls[-1] == ("STORE", (b"42", "+FLAGS.SILENT", r"(\Seen)"))
    calls.clear()
    with pytest.raises(OSError, match="UIDVALIDITY changed"):
        email_ingest._mark_seen(load_settings(), [email_ingest.FetchedMail(b"42", "998", b"mail")])
    assert not calls


async def test_real_email_failure_reaches_loop_health_and_then_recovers(monkeypatch):
    from src.services import health
    monkeypatch.setattr(email_ingest, "POLL_SECONDS", 0)
    monkeypatch.setattr(email_ingest, "last_success", None)
    monkeypatch.setattr(email_ingest, "last_error", None)
    entered = asyncio.Event()
    def unavailable(settings):
        raise OSError("auth failed")
    monkeypatch.setattr(email_ingest, "_fetch_unseen", unavailable)
    real_run = email_ingest.run_once
    async def observed():
        result = await real_run()
        entered.set()
        return result
    monkeypatch.setattr(email_ingest, "run_once", observed)
    task = asyncio.create_task(email_ingest.poll_loop())
    try:
        await entered.wait()
        await asyncio.sleep(0)
        assert email_ingest.last_success is None
        assert email_ingest.last_error == "OSError"
        monkeypatch.setattr(email_ingest, "started_at", time.monotonic() - 1000)
        assert health._mailbox().ok is False
        monkeypatch.setattr(email_ingest, "_fetch_unseen", lambda settings: [])
        monkeypatch.setattr(email_ingest, "_mark_seen", lambda *args: None)
        entered.clear()
        await entered.wait()
        await asyncio.sleep(0)
        assert email_ingest.last_success is not None and email_ingest.last_error is None
        monkeypatch.setattr(email_ingest, "POLL_SECONDS", 60)
        assert health._mailbox().ok is True
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


def signed(client, payload):
    raw = json.dumps(payload).encode()
    stamp = str(int(time.time()))
    signature = "v0=" + hmac.new(b"durable-secret", b"v0:" + stamp.encode() + b":" + raw, hashlib.sha256).hexdigest()
    return client.post("/slack/events", content=raw, headers={
        "content-type": "application/json", "X-Slack-Request-Timestamp": stamp,
        "X-Slack-Signature": signature,
    })


async def test_slack_failed_processing_ack_is_durable_and_retried_once(monkeypatch):
    from src.dashboard.app import app
    from src.dashboard.routes import slack
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-durable-test")
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "durable-secret")
    text = "durable slack " + uuid.uuid4().hex
    payload = {"type": "event_callback", "event_id": uuid.uuid4().hex, "team_id": "Tdurable",
        "event": {"type": "message", "subtype": "file_share", "channel": "Cdurable", "user": "U1",
                  "text": text, "ts": "1888888888.1", "files": [
                      {"url_private": "https://files.slack.com/files-pri/T1-F1/one.txt", "name": "one.txt"},
                      {"url_private": "https://files.slack.com/files-pri/T1-F2/two.txt", "name": "two.txt"},
                  ]}}
    real_record = slack.record_event
    async def failure(*args, **kwargs):
        await real_record(*args, **kwargs)
        raise OSError("simulated failure after intake")
    monkeypatch.setattr(slack, "record_event", failure)
    with TestClient(app) as client:
        assert signed(client, payload).status_code == 200
    assert await count_text(text) == 0
    raw = json.dumps(payload).encode()
    async with db_session() as session:
        event = (await session.execute(select(InboundEvent).where(InboundEvent.payload == raw))).scalar_one()
        assert event.status == "pending" and event.payload == raw
        event_id = event.id
    monkeypatch.setattr(slack, "record_event", real_record)
    done = await inbound_service.process(event_id, force=True)
    assert done.status == "processed"
    with TestClient(app) as client:
        assert signed(client, payload).status_code == 200
    assert await count_text(text) == 1
    async with db_session() as session:
        files = (await session.execute(select(Message.file_name).where(Message.ticket_id == done.ticket_id))).scalars().all()
    assert sorted(files) == ["one.txt", "two.txt"]


async def test_slack_cannot_ack_if_persistence_fails(monkeypatch):
    from src.dashboard.app import app
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-durable-test")
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "durable-secret")
    monkeypatch.setattr(inbound_service, "enqueue", AsyncMock(side_effect=OSError("offline")))
    payload = {"type": "event_callback", "event_id": uuid.uuid4().hex,
               "event": {"type": "message", "channel": "C1", "text": "question", "ts": "1.1"}}
    with TestClient(app) as client:
        assert signed(client, payload).status_code == 503


async def test_two_workers_cannot_process_one_receipt_twice():
    from src.services import email_thread_service
    from src.config import load_settings
    message = mail("concurrent receipt " + uuid.uuid4().hex)
    event_id = await inbound_service.enqueue(channel="email", source_key=uuid.uuid4().hex,
        payload=message.as_bytes(), context={"mailbox": email_thread_service.mailbox_key(load_settings()),
                                           "our_address": "support@example.test"})
    results = await asyncio.gather(inbound_service.process(event_id), inbound_service.process(event_id))
    assert all(result.status == "processed" for result in results)
    assert await count_text(message["Subject"]) == 1


async def test_queue_quota_refuses_before_ack_and_retains_existing(monkeypatch):
    monkeypatch.setattr(inbound_service, "MAX_STORED_BYTES", 16)
    # Existing processed payload can be evicted; pending content never can.
    unique = uuid.uuid4().hex
    first = await inbound_service.enqueue(channel="slack", source_key=unique, payload=b"1234567890")
    with pytest.raises(inbound_service.InboxFull):
        await inbound_service.enqueue(channel="slack", source_key=unique + "x", payload=b"1234567890")
    async with db_session() as session:
        assert (await session.get(InboundEvent, first)).payload == b"1234567890"


async def test_slack_ack_precedes_profile_lookup_and_ticket_processing(monkeypatch):
    from starlette.requests import Request
    from src.dashboard.routes import slack
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-durable-test")
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "durable-secret")
    speaker = AsyncMock(return_value="Visitor")
    monkeypatch.setattr(slack, "_speaker", speaker)
    raw = json.dumps({"type": "event_callback", "event_id": uuid.uuid4().hex,
        "event": {"type": "message", "channel": "Cack", "user": "Uslow", "text": "hello", "ts": "1.2"}}).encode()
    stamp = str(int(time.time()))
    signature = "v0=" + hmac.new(b"durable-secret", b"v0:" + stamp.encode() + b":" + raw, hashlib.sha256).hexdigest()
    async def receive():
        return {"type": "http.request", "body": raw, "more_body": False}
    request = Request({"type": "http", "method": "POST", "path": "/slack/events",
                       "headers": [(b"x-slack-request-timestamp", stamp.encode()),
                                   (b"x-slack-signature", signature.encode())]}, receive)
    response = await slack.events(request)
    assert response.status_code == 200 and response.background is not None
    speaker.assert_not_awaited()
    async with db_session() as session:
        event = (await session.execute(select(InboundEvent))).scalar_one()
        assert event.status == "pending" and event.payload == raw


async def test_retry_backoff_does_not_make_email_health_falsely_green(monkeypatch):
    from datetime import datetime, timedelta
    monkeypatch.setattr(email_ingest, "_fetch_unseen", lambda settings: [])
    monkeypatch.setattr(email_ingest, "_mark_seen", lambda *args: None)
    event_id = await inbound_service.enqueue(channel="email", source_key=uuid.uuid4().hex, payload=b"retained")
    async with db_session(write=True) as session:
        event = await session.get(InboundEvent, event_id)
        event.last_error = "OSError"
        event.next_attempt_at = datetime.utcnow() + timedelta(minutes=5)
    assert (await email_ingest.run_once()).get("error")


def test_oversized_mail_does_not_hide_next_uid(monkeypatch):
    from src.config import load_settings
    calls = []
    class Box:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def login(self, *args): pass
        def select(self, *args): return "OK", [b"2"]
        def response(self, name): return name, [b"777"]
        def uid(self, command, *args):
            calls.append((command, args))
            if command == "SEARCH": return "OK", [b"50 51"]
            if args[1] == "(RFC822.SIZE)":
                size = inbound_service.MAX_EVENT_BYTES + 1 if args[0] == b"50" else 4
                return "OK", [f"1 (RFC822.SIZE {size})".encode()]
            assert args[0] == b"51", "oversized body must never be downloaded"
            return "OK", [(b"2 (BODY[] {4}", b"mail")]
    monkeypatch.setattr(email_ingest, "_open", lambda settings: Box())
    result = email_ingest._fetch_unseen(load_settings())
    assert result == [email_ingest.FetchedMail(b"51", "777", b"mail")]
    assert result.error == "InboxFull"


@pytest.mark.parametrize("attachment", [None, ("answer.txt", b"reply attachment")])
async def test_slack_ticket_reply_carries_the_original_thread(monkeypatch, attachment):
    import httpx
    from src.channels.slack import SlackChannel
    seen = []
    def transport(request):
        url = str(request.url)
        if url.endswith("files.getUploadURLExternal"):
            return httpx.Response(200, json={"ok": True, "upload_url": "https://files.slack.test/upload", "file_id": "F1"})
        if url == "https://files.slack.test/upload":
            return httpx.Response(200)
        body = json.loads(request.content)
        seen.append(body)
        return httpx.Response(200, json={"ok": True, "ts": "9.9", "files": [{"id": "F1", "timestamp": "9.9"}]})
    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda *args, **kwargs:
                        real_client(*args, **{**kwargs, "transport": httpx.MockTransport(transport)}))
    result = await SlackChannel(token="xoxb-test").send_for_ticket(
        "Cthreads", "Answer", conversation_key="slack:1888888888.123", attachment=attachment)
    assert result.ok and len(seen) == 1
    assert seen[0]["thread_ts"] == "1888888888.123"


@pytest.mark.parametrize("refused", [False, True])
async def test_smtp_distinguishes_server_rejection_from_ambiguous_disconnect(monkeypatch, refused):
    import smtplib
    from src.channels.email import EmailChannel
    def deliver(*args):
        if refused:
            raise smtplib.SMTPDataError(550, b"refused")
        raise smtplib.SMTPServerDisconnected("lost after DATA")
    monkeypatch.setattr("src.channels.email._deliver", deliver)
    result = await EmailChannel().send_text("visitor@example.test", "hello")
    assert result.error_key == ("e_send_rejected" if refused else "e_delivery_uncertain")
