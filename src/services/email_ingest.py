"""Reading the support mailbox.

Runs in the dashboard process, not the bot's. The bot needs a Telegram token
and an instance may not have one — a helpdesk on the widget and email alone
is a supported configuration, and putting the mail loop where it might never
start would quietly make it not one.

Three things this has to get right, and the third is the one that bites.

**Not reprocessing.** Messages are fetched UNSEEN and marked seen once they
have landed in a ticket. A message that fails to parse is marked seen anyway
and logged, because the alternative is a poison message the loop retries
every minute forever.

**Not blocking.** `imaplib` is synchronous and a mail server may take half a
minute to answer, so the whole exchange happens in a worker thread. An event
loop waiting on somebody else's IMAP is a dashboard that has stopped serving
pages.

**Not talking to itself.** This is the failure mode that turns a support
mailbox into an outage. Our own reply lands back in the mailbox on some
hosts; an out-of-office autoresponder answers it; that answer is a new
ticket; closing the ticket sends a message; the autoresponder answers again.
Four separate guards below, and they are cheap compared to the incident.
"""

from __future__ import annotations

import asyncio
import email as email_lib
import imaplib
import logging
from email.message import Message

from src.channels import email as email_channel
from src.channels import email_parse
from src.database.db import db_session
from src.services import file_store, intake_service

logger = logging.getLogger(__name__)

#: How often the mailbox is checked. A minute is invisible to somebody who
#: has just sent an email and cheap enough to run forever.
POLL_SECONDS = 60

#: Never fetch more than this in one pass. A mailbox that has been collecting
#: for a month should not turn its first poll into a thousand tickets and a
#: timeout; the rest arrive on the next pass, a minute later.
MAX_PER_PASS = 25

_TIMEOUT = 60.0


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


def _fetch_unseen(settings) -> list[tuple[bytes, bytes]]:
    """(uid, raw message) for everything unread. Blocking; called in a thread."""
    with imaplib.IMAP4_SSL(settings.email_imap_host, settings.email_imap_port) as box:
        box.socket().settimeout(_TIMEOUT)
        box.login(settings.email_user, settings.email_password)
        box.select("INBOX")

        ok, data = box.search(None, "UNSEEN")
        if ok != "OK":
            return []
        uids = (data[0] or b"").split()[:MAX_PER_PASS]

        out: list[tuple[bytes, bytes]] = []
        for uid in uids:
            # BODY.PEEK, so fetching does not mark it read behind our back —
            # a message must not be considered handled until it is in a
            # ticket, or a crash mid-pass loses it silently.
            ok, payload = box.fetch(uid, "(BODY.PEEK[])")
            if ok != "OK" or not payload or not isinstance(payload[0], tuple):
                continue
            out.append((uid, payload[0][1]))
        return out


def _mark_seen(settings, uids: list[bytes]) -> None:
    """Blocking; called in a thread. Failure here means a message is handled
    twice, so it is worth its own connection rather than riding on one that
    may already have timed out."""
    if not uids:
        return
    with imaplib.IMAP4_SSL(settings.email_imap_host, settings.email_imap_port) as box:
        box.socket().settimeout(_TIMEOUT)
        box.login(settings.email_user, settings.email_password)
        box.select("INBOX")
        for uid in uids:
            box.store(uid, "+FLAGS", "\\Seen")


async def _record(inbound: email_parse.Inbound) -> int | None:
    """Put one email into a ticket. Returns the ticket id, or None."""
    stored: list[tuple[str, email_parse.Attachment]] = []
    for attachment in inbound.attachments:
        try:
            stored.append((file_store.put(attachment.content), attachment))
        except OSError as e:  # noqa: BLE001 — a full or read-only disk
            logger.error(f"Could not store a mail attachment: {e}")

    # The subject carries the question often enough to be worth keeping when
    # the body is a screenshot and the word "this".
    text = inbound.text
    if inbound.subject and not text:
        text = inbound.subject

    first_ref, first = (stored[0] if stored else (None, None))
    async with db_session() as session:
        result = await intake_service.receive(session, intake_service.Inbound(
            channel=email_channel.NAME,
            address=inbound.from_address,
            text=text,
            display_name=inbound.from_name or None,
            email=inbound.from_address,
            attachment=intake_service.Attachment(
                ref=first_ref, name=first.filename,
                mime_type=first.mime_type, size=len(first.content),
                kind="photo" if first.mime_type.startswith("image/") else "document",
            ) if first_ref else None,
        ))
        await session.commit()

    if result.rejected:
        for ref, _ in stored:
            file_store.delete(ref)
        return None

    # The rest of the attachments, each as its own message. One email with
    # four files is four things to look at, which is how Telegram albums
    # arrive too — and `receive` takes one attachment because that is what a
    # message is.
    for ref, attachment in stored[1:]:
        async with db_session() as session:
            await intake_service.receive(session, intake_service.Inbound(
                channel=email_channel.NAME,
                address=inbound.from_address,
                text="",
                attachment=intake_service.Attachment(
                    ref=ref, name=attachment.filename,
                    mime_type=attachment.mime_type, size=len(attachment.content),
                    kind="photo" if attachment.mime_type.startswith("image/") else "document",
                ),
            ))
            await session.commit()

    if result.opened_ticket:
        await _announce(result.ticket_id)
    return result.ticket_id


async def _announce(ticket_id: int) -> None:
    """Put it in front of the operators. Swallowed: the mail is recorded
    either way, and failing the pass because the operators' chat was
    unreachable would have the priorities backwards."""
    try:
        from src.dashboard.bot_client import get_bot
        from src.services import announce_service

        await announce_service.announce_ticket(get_bot(), ticket_id)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Ticket #{ticket_id} not announced: {e}")


async def run_once() -> dict:
    """One pass over the mailbox. Never raises."""
    from src.config import load_settings

    settings = load_settings()
    if not settings.has_email:
        return {"fetched": 0, "opened": 0, "skipped": 0}

    try:
        raw = await asyncio.to_thread(_fetch_unseen, settings)
    except Exception as e:  # noqa: BLE001 — imaplib raises a wide family
        logger.warning(f"Could not read the mailbox: {e}")
        return {"fetched": 0, "opened": 0, "skipped": 0, "error": str(e)}

    handled: list[bytes] = []
    opened = skipped = 0

    for uid, payload in raw:
        try:
            message = email_lib.message_from_bytes(payload)
            if _looks_automated(message, settings.email_address):
                logger.info("Skipped an automated message")
                skipped += 1
                handled.append(uid)
                continue

            inbound = email_parse.parse(message)
            if not inbound.from_address or inbound.is_empty:
                skipped += 1
                handled.append(uid)
                continue

            ticket_id = await _record(inbound)
            if ticket_id:
                opened += 1
                logger.info(f"Mail from {inbound.from_address} → ticket #{ticket_id}")
            handled.append(uid)
        except Exception as e:  # noqa: BLE001 — one bad message must not stop the rest
            # Marked seen anyway. A message that cannot be parsed will not
            # parse next minute either, and a poison message retried forever
            # is a mailbox that stops working entirely.
            logger.error(f"Could not handle a message, skipping it: {e}", exc_info=True)
            skipped += 1
            handled.append(uid)

    try:
        await asyncio.to_thread(_mark_seen, settings, handled)
    except Exception as e:  # noqa: BLE001
        # The messages are in tickets; failing to flag them means they arrive
        # again next pass. Worth a warning and not worth losing sleep: the
        # duplicate lands on the same open ticket.
        logger.warning(f"Could not mark messages read: {e}")

    return {"fetched": len(raw), "opened": opened, "skipped": skipped}


async def poll_loop() -> None:
    """Check the mailbox until cancelled.

    Every failure is swallowed and retried on the next pass, for the same
    reason the SLA loop does it: a background loop that dies quietly is worse
    than one that logs and keeps going, because nobody notices until the day
    it mattered.
    """
    logger.info(f"Mailbox polling every {POLL_SECONDS}s")
    while True:
        try:
            await asyncio.sleep(POLL_SECONDS)
            result = await run_once()
            if result.get("opened") or result.get("skipped"):
                logger.info(f"Mail pass: {result}")
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Mail pass failed, will retry: {e}")
