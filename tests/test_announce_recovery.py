"""An unavailable operator chat must not block customer writes or lose copies."""

import asyncio
import uuid
from email.message import EmailMessage
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

import src.channels as channels
from src.channels.email import EmailChannel
from src.config import load_settings
from src.database.db import db_session
from src.database.delivery_models import OutboundJob
from src.database.inbound_models import InboundMirror
from src.database.models import Message, Ticket
from src.services import announce_service, email_thread_service, inbound_service, worker_lease
from tests.conftest import RecordingChannel, next_tg_id
from tests.test_announce import FakeBot, _ticket_from


@pytest.mark.parametrize("phase", ["header", "message", "file_fetch"])
async def test_waiting_for_a_provider_does_not_hold_the_database_writer(initialized_db, monkeypatch, phase):
    entered, proceed = asyncio.Event(), asyncio.Event()
    pipe = RecordingChannel(name="relay")
    channels.register(pipe)
    tid = await _ticket_from("relay", str(next_tg_id()), "Question")
    async with db_session(write=True) as session:
        row = (await session.execute(select(Message).where(Message.ticket_id == tid))).scalar_one()
        mid = row.id
        if phase != "header":
            (await session.get(Ticket, tid)).header_message_id = 8000
        if phase == "file_fetch":
            row.file_id, row.file_name, row.file_type = "remote", "input.txt", "document"
    async def pause():
        entered.set()
        await proceed.wait()
    class SlowBot(FakeBot):
        async def get_chat(self, *args, **kwargs):
            if phase == "header":
                await pause()
            return await super().get_chat(*args, **kwargs)
        async def send_message(self, *args, **kwargs):
            if phase == "message":
                await pause()
            return await super().send_message(*args, **kwargs)
    if phase == "file_fetch":
        async def slow_fetch(ref):
            await pause()
            return b"original file"
        monkeypatch.setattr(pipe, "fetch_file", slow_fetch)
    task = asyncio.create_task(announce_service.announce_message(SlowBot(), tid, mid))
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        # This takes BEGIN IMMEDIATE / the same PostgreSQL advisory writer
        # lock as intake/outbox. It must commit while provider I/O is paused.
        async def unrelated_write():
            async with db_session(write=True) as session:
                ticket = await session.get(Ticket, tid)
                ticket.category = "Changed while Telegram waits"
        await asyncio.wait_for(unrelated_write(), timeout=2)
        assert not task.done()
    finally:
        proceed.set()
        await asyncio.wait_for(task, timeout=2)
        channels.reset()
    assert task.result()


async def test_network_timeout_releases_the_ticket_lease(initialized_db, monkeypatch):
    tid = await _ticket_from("widget", str(next_tg_id()), "Question")
    class StuckBot(FakeBot):
        async def get_chat(self, *args, **kwargs):
            await asyncio.Event().wait()
    monkeypatch.setattr(announce_service, "ANNOUNCE_TIMEOUT", 0.2)
    assert not await asyncio.wait_for(announce_service.announce_ticket(StuckBot(), tid), timeout=2)
    assert await worker_lease.acquire(f"mirror:{tid}", "next-worker", 1)
    await worker_lease.release(f"mirror:{tid}", "next-worker")


async def test_recovery_dispatches_delivered_jobs_without_replaying_history(initialized_db):
    tid = await _ticket_from("widget", str(next_tg_id()), "Unannounced historical opening")
    unique = uuid.uuid4().hex
    async with db_session(write=True) as session:
        (await session.get(Ticket, tid)).header_message_id = 8100
        rows = [Message(ticket_id=tid, sender_type="admin", sender_id=0,
                        text=f"{unique}-{state}", delivery_status=state)
                for state in ("delivered", "failed", "pending", "uncertain")]
        historical = Message(ticket_id=tid, sender_type="admin", sender_id=0,
                             text=f"{unique}-old-delivered", delivery_status="delivered")
        session.add_all([*rows, historical])
        await session.flush()
        for row in rows:
            session.add(OutboundJob(message_id=row.id, channel="widget", address="unused",
                                    status=row.delivery_status))
        delivered_id = rows[0].id
    bot = FakeBot()
    await announce_service.retry_mirrors(bot, limit=10000)
    await announce_service.retry_mirrors(bot, limit=10000)
    assert sum(f"{unique}-delivered" in value for value in bot.texts) == 1
    assert all(not any(f"{unique}-{state}" in value for value in bot.texts)
               for state in ("failed", "pending", "uncertain", "old-delivered"))
    assert "Unannounced historical opening" not in bot.texts
    async with db_session() as session:
        assert (await session.get(Message, delivered_id)).admin_message_id is not None
        assert (await session.get(Message, historical.id)).admin_message_id is None


async def test_recovery_remembers_each_inbound_attachment_after_partial_mirroring(
    initialized_db, monkeypatch, tmp_path,
):
    for key, value in {"EMAIL_ADDRESS": "support@example.test", "EMAIL_PASSWORD": "test-only",
                       "EMAIL_IMAP_HOST": "imap.example.test", "EMAIL_SMTP_HOST": "smtp.example.test",
                       "ATTACHMENTS_DIR": str(tmp_path)}.items():
        monkeypatch.setenv(key, value)
    raw = EmailMessage()
    raw["From"], raw["To"] = "visitor@example.test", "support@example.test"
    raw["Message-ID"] = f"<{uuid.uuid4().hex}@example.test>"
    raw.set_content("Original")
    for name in ("one", "two", "three"):
        raw.add_attachment(name.encode(), maintype="application", subtype="octet-stream", filename=name + ".txt")
    event_id = await inbound_service.enqueue(channel="email", source_key=uuid.uuid4().hex, payload=raw.as_bytes(),
        context={"mailbox": email_thread_service.mailbox_key(load_settings()), "our_address": "support@example.test"})
    result = await inbound_service.process(event_id)
    assert result.status == "processed"
    async with db_session() as session:
        ids = list((await session.execute(select(InboundMirror.message_id).where(
            InboundMirror.event_id == event_id).order_by(InboundMirror.message_id))).scalars())
    assert len(ids) == 3
    class FileBot(FakeBot):
        files = []
        async def send_document(self, document, caption, **kwargs):
            self.files.append((document.filename, document.data))
            return await super().send_document(document, caption, **kwargs)
    bot = FileBot()
    channels.register(EmailChannel())
    try:
        assert await announce_service.announce_message(bot, result.ticket_id, ids[0])
        await announce_service.retry_mirrors(bot, limit=10000)
        await announce_service.retry_mirrors(bot, limit=10000)
    finally:
        channels.reset()
    assert [entry for entry in bot.files if entry[0] in {"one.txt", "two.txt", "three.txt"}] == [
        ("one.txt", b"one"), ("two.txt", b"two"), ("three.txt", b"three")]
    async with db_session() as session:
        mirrored = list((await session.execute(select(Message.admin_message_id).where(
            Message.id.in_(ids)))).scalars())
    assert len(mirrored) == 3 and all(value is not None for value in mirrored)


async def test_one_unavailable_copy_cannot_starve_the_next_batch(initialized_db, monkeypatch):
    tid = await _ticket_from("missing-provider", str(next_tg_id()), "Opening")
    async with db_session(write=True) as session:
        (await session.get(Ticket, tid)).header_message_id = 8200
        failed = Message(ticket_id=tid, sender_type="admin", sender_id=0, text="Unavailable file",
                         delivery_status="delivered", file_id="missing", file_name="missing.bin")
        ready = Message(ticket_id=tid, sender_type="admin", sender_id=0, text="Next copy still arrives",
                        delivery_status="delivered")
        session.add_all([failed, ready])
        await session.flush()
        session.add_all(OutboundJob(message_id=row.id, channel="missing-provider", address="unused", status="delivered")
                        for row in (failed, ready))
        first_id = failed.id
    monkeypatch.setattr(announce_service, "_retry_after_id", first_id - 1)
    bot = FakeBot()
    assert await announce_service.retry_mirrors(bot, limit=1) == 0
    assert await announce_service.retry_mirrors(bot, limit=1) == 1
    assert "📤 Next copy still arrives" in bot.texts


@pytest.mark.parametrize("channel", ["email", "slack"])
async def test_durable_intake_completes_while_operator_mirroring_is_unavailable(
    initialized_db, monkeypatch, channel,
):
    import json
    for key, value in {"EMAIL_ADDRESS": "support@example.test", "EMAIL_PASSWORD": "test-only",
                       "EMAIL_IMAP_HOST": "imap.example.test", "EMAIL_SMTP_HOST": "smtp.example.test"}.items():
        monkeypatch.setenv(key, value)
    texts, event_ids = [], []
    for index in range(2):
        text = "Independent intake " + uuid.uuid4().hex
        texts.append(text)
        if channel == "email":
            mail = EmailMessage()
            mail["From"], mail["To"] = "visitor@example.test", "support@example.test"
            mail["Message-ID"] = f"<{uuid.uuid4().hex}@example.test>"
            mail.set_content(text)
            payload = mail.as_bytes()
        else:
            payload = json.dumps({"event": {"type": "message", "text": text,
                "channel": "Cdetached", "ts": str(index) + ".001"}}).encode()
        event_ids.append(await inbound_service.enqueue(channel=channel, source_key=uuid.uuid4().hex,
            payload=payload, context={"mailbox": email_thread_service.mailbox_key(load_settings()),
                                     "our_address": "support@example.test"}))
    async def unavailable(*args, **kwargs):
        await asyncio.Event().wait()
    blocked = AsyncMock(side_effect=unavailable)
    with monkeypatch.context() as patch:
        patch.setattr(announce_service, "announce_message", blocked)
        result = await asyncio.wait_for(inbound_service.run_once(channel=channel), timeout=2)
    assert result["processed"] >= 2 and result["failed"] == 0
    blocked.assert_not_awaited()
    async with db_session() as session:
        ids = list((await session.execute(select(InboundMirror.message_id).where(
            InboundMirror.event_id.in_(event_ids)).order_by(InboundMirror.message_id))).scalars())
        assert len(ids) == 2
        provider_ids = list((await session.execute(select(Message.admin_message_id).where(Message.id.in_(ids)))).scalars())
    assert provider_ids == [None, None]
    # Once the provider recovers, the independent worker finds both receipts.
    monkeypatch.setattr(announce_service, "_retry_after_id", ids[0] - 1)
    bot = FakeBot()
    assert await announce_service.retry_mirrors(bot, limit=2) == 2
    assert all(text in bot.texts for text in texts)
