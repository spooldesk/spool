"""IMAP UID intake with a durable inbox and commit-before-Seen semantics.

Every fetched RFC822 body is persisted before marking the IMAP message Seen.
A receipt changes from pending to processed in the same transaction as all
ticket messages; failed processing retries the durable original input.
"""

from __future__ import annotations

import asyncio
import email as email_lib
import hashlib
import imaplib
import logging
import re
import ssl
import time
from dataclasses import dataclass
from email.message import Message

from src.channels import email as email_channel, email_parse
from src.database.db import db_session
from src.services import file_store, intake_service, inbound_service, email_thread_service

logger = logging.getLogger(__name__)
POLL_SECONDS = 60
MAX_PER_PASS = 25
MAX_BATCH_BYTES = 32 * 1024 * 1024
_TIMEOUT = 60.0


@dataclass(frozen=True)
class FetchedMail:
    uid: bytes
    uidvalidity: str
    payload: bytes


class FetchedBatch(list[FetchedMail]):
    error: str | None = None


def _looks_automated(message: Message, our_address: str) -> bool:
    """Whether answering this would be talking to a machine.

    Every one of these has caused a real mail loop somewhere. `Auto-Submitted`
    is the header autoresponders are supposed to set and mostly do;
    `Precedence: bulk` is what mailing lists and notification systems use; an
    empty envelope sender is what bounces carry; and our own address in From
    means the message is ours, come back.
    """
    if (message.get("Auto-Submitted") or "no").lower() != "no":
        return True
    if (message.get("Precedence") or "").lower() in ("bulk", "junk", "list"):
        return True
    if message.get("List-Id") or message.get("List-Unsubscribe"):
        return True
    if (message.get("Return-Path") or "").strip() in ("<>", ""):
        # A null return path is a bounce. Replying to one is how two servers
        # spend a weekend writing to each other.
        if message.get("Return-Path") is not None:
            return True
    sender, _ = email_parse.sender_of(message)
    return bool(sender) and sender == (our_address or "").lower()



def _open(settings):
    return imaplib.IMAP4_SSL(settings.email_imap_host, settings.email_imap_port,
                            timeout=_TIMEOUT, ssl_context=ssl.create_default_context())


def _select(box, settings) -> str:
    box.login(settings.email_user, settings.email_password)
    ok, _ = box.select("INBOX")
    if ok != "OK":
        raise OSError("IMAP inbox cannot be selected")
    _, values = box.response("UIDVALIDITY")
    value = (values or [None])[0]
    validity = value.decode("ascii") if isinstance(value, bytes) else str(value or "")
    if not validity.isdigit():
        raise OSError("IMAP server did not provide UIDVALIDITY")
    return validity


def _fetch_unseen(settings) -> list[FetchedMail]:
    with _open(settings) as box:
        validity = _select(box, settings)
        ok, data = box.uid("SEARCH", None, "UNSEEN")
        if ok != "OK":
            raise OSError("IMAP UID SEARCH failed")
        out = FetchedBatch()
        total_bytes = 0
        for uid in (data[0] or b"").split():
            if len(out) >= MAX_PER_PASS:
                break
            if not uid.isdigit():
                raise OSError("invalid IMAP UID")
            ok, sizes = box.uid("FETCH", uid, "(RFC822.SIZE)")
            if ok != "OK":
                raise OSError("IMAP UID FETCH size failed")
            match = re.search(rb"RFC822.SIZE\s+(\d+)", b" ".join(
                entry for entry in (sizes or []) if isinstance(entry, bytes)))
            if match and int(match.group(1)) > inbound_service.MAX_EVENT_BYTES:
                # Leave the original unread for operator attention, but do
                # not let one oversized message hide later valid messages.
                out.error = "InboxFull"
                continue
            if out and match and total_bytes + int(match.group(1)) > MAX_BATCH_BYTES:
                break
            ok, parts = box.uid("FETCH", uid, "(BODY.PEEK[])")
            if ok != "OK":
                raise OSError("IMAP UID FETCH failed")
            literal = next((part[1] for part in (parts or [])
                            if isinstance(part, tuple) and isinstance(part[1], bytes)), None)
            if literal is None:
                # An expunge between SEARCH/FETCH means there is no message;
                # absence is safe, but an actual IMAP failure above is not.
                continue
            if len(literal) > inbound_service.MAX_EVENT_BYTES:
                out.error = "InboxFull"
                continue
            if out and total_bytes + len(literal) > MAX_BATCH_BYTES:
                break
            out.append(FetchedMail(uid, validity, literal))
            total_bytes += len(literal)
        return out


def _mark_seen(settings, messages: list[FetchedMail]) -> None:
    if not messages:
        return
    with _open(settings) as box:
        validity = _select(box, settings)
        for message in messages:
            if validity != message.uidvalidity:
                raise OSError("IMAP UIDVALIDITY changed; no messages marked seen")
            ok, _ = box.uid("STORE", message.uid, "+FLAGS.SILENT", r"(\Seen)")
            if ok != "OK":
                raise OSError("IMAP UID STORE failed")


async def prepare_event(raw: bytes, context: dict):
    message = email_lib.message_from_bytes(raw)
    if _looks_automated(message, context.get("our_address", "")):
        return None
    return email_parse.parse(message)


async def record_event(session, prepared, context: dict, *, event_id: int):
    from src.services.inbound_service import Recorded

    if prepared is None or not prepared.from_address or prepared.is_empty:
        return Recorded(rejected="automated_or_empty")
    return await _record_in_session(session, prepared, mailbox=context["mailbox"],
                                    fallback=context.get("source_key") or str(event_id))


async def _record_in_session(session, inbound, *, mailbox: str, fallback: str):
    from src.services.inbound_service import Recorded

    prior = await email_thread_service.find(session, mailbox, inbound.from_address, inbound.message_id)
    if prior is not None and prior.direction == "inbound":
        return Recorded(prior.ticket_id or 0, prior.message_row_id or 0)
    conversation_key = await email_thread_service.conversation_for(
        session, inbound, mailbox=mailbox, fallback=fallback)
    stored = []
    try:
        # Every part belongs to the same transaction and ticket. A disk error
        # is a retryable failure, never permission to discard an attachment.
        for attachment in inbound.attachments:
            stored.append((await asyncio.to_thread(file_store.put, attachment.content), attachment))
        text = inbound.text or inbound.subject

        def incoming(body, entry=None):
            ref, part = entry if entry else (None, None)
            return intake_service.Inbound(
                channel=email_channel.NAME, address=inbound.from_address, text=body,
                display_name=inbound.from_name or None, email=inbound.from_address,
                conversation_key=conversation_key,
                attachment=intake_service.Attachment(
                    ref=ref, name=part.filename, mime_type=part.mime_type,
                    size=len(part.content), kind="photo" if part.mime_type.startswith("image/") else "document",
                ) if part else None,
            )
        first = await intake_service.receive(session, incoming(text, stored[0] if stored else None))
        if first.rejected:
            for ref, _ in stored:
                file_store.delete(ref)
            return Recorded(rejected=first.rejected)
        ids = [first.message_id]
        for entry in stored[1:]:
            result = await intake_service.receive(session, incoming("", entry))
            if result.rejected:
                raise RuntimeError("attachment intake refused after first message")
            ids.append(result.message_id)
        await email_thread_service.remember(
            session, mailbox=mailbox, address=inbound.from_address, message_id=inbound.message_id,
            conversation_key=conversation_key, ticket_id=first.ticket_id, subject=inbound.subject,
            message_row_id=first.message_id,
        )
        return Recorded(first.ticket_id, first.message_id, first.opened_ticket, message_ids=tuple(ids))
    except BaseException:
        for ref, _ in stored:
            file_store.delete(ref)
        raise


async def _record(inbound: email_parse.Inbound) -> int | None:
    """Local intake helper; the IMAP path uses the durable event processor."""
    from src.config import load_settings
    async with db_session(write=True) as session:
        result = await _record_in_session(session, inbound,
            mailbox=email_thread_service.mailbox_key(load_settings()), fallback=hashlib.sha256(
                (inbound.from_address + inbound.subject + inbound.full_text).encode()).hexdigest())
    return result.ticket_id or None


async def run_once() -> dict:
    from src.config import load_settings

    settings = load_settings()
    if not settings.has_email:
        return {"fetched": 0, "opened": 0, "skipped": 0}
    result = {"fetched": 0, "opened": 0, "skipped": 0}
    mailbox = email_thread_service.mailbox_key(settings)
    try:
        # Restart recovery still works if the original has since been read or
        # removed by another mailbox client. Accepted raw data is ours now.
        retried = await inbound_service.run_once(channel="email")
        if retried["failed"] or await inbound_service.has_failed_pending(channel="email"):
            result["error"] = "inbound processing failed; messages retained for retry"
        fetched = await asyncio.to_thread(_fetch_unseen, settings)
        result["fetched"] = len(fetched)
        if getattr(fetched, "error", None):
            result["error"] = fetched.error
    except Exception as exc:
        result["error"] = type(exc).__name__
        logger.warning("Mailbox pass failed (%s)", type(exc).__name__)
        return result

    handled = []
    for message in fetched:
        try:
            source_key = hashlib.sha256(f"{mailbox}:{message.uidvalidity}:{message.uid.decode('ascii')}".encode()).hexdigest()
            event_id = await inbound_service.enqueue(
                channel="email", source_key=source_key, payload=message.payload,
                context={"mailbox": mailbox, "source_key": source_key,
                         "our_address": settings.email_address},
            )
            # The raw message is committed now. Even a parser/disk failure
            # can be retried without IMAP, and cannot block the next batch.
            handled.append(message)
            processed = await inbound_service.process(event_id)
            if processed.status in ("processed", "ignored"):
                result["opened"] += processed.opened_ticket
                result["skipped"] += processed.status == "ignored"
            else:
                result["error"] = processed.error or "inbound processing pending"
        except Exception as exc:
            result["error"] = type(exc).__name__
            logger.warning("Email UID could not be persisted/processed (%s)", type(exc).__name__)
    try:
        await asyncio.to_thread(_mark_seen, settings, handled)
    except Exception as exc:
        result["error"] = type(exc).__name__
        logger.warning("Could not mark committed email receipts seen; retry is deduplicated")
    return result


last_success: float | None = None
last_error: str | None = None
started_at: float | None = None


async def poll_loop() -> None:
    global last_success, last_error, started_at
    started_at = time.monotonic()
    while True:
        try:
            await asyncio.sleep(POLL_SECONDS)
            result = await run_once()
            if result.get("error"):
                last_error = str(result["error"])
            else:
                last_success, last_error = time.monotonic(), None
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            last_error = type(exc).__name__
            logger.warning("Mailbox pass failed; retained work will be retried")
