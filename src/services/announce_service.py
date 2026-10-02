"""Optional Telegram operator view: real forum topics and per-message mirroring."""

from __future__ import annotations

import asyncio
import logging
import secrets
from datetime import datetime

from aiogram import Bot
from sqlalchemy import select, union
from sqlalchemy.orm import selectinload

from src.channels import DEFAULT as TELEGRAM
from src.channels.identity import channel_of, display_address
from src.database.db import db_session
from src.database.models import Message as MessageRecord, Team, Ticket

logger = logging.getLogger(__name__)
ANNOUNCE_TIMEOUT = 45
LEASE_SECONDS = 60
POLL_SECONDS = 15
# Only scheduling fairness is process-local. Receipts and provider ids remain
# durable, so a restart merely begins the next sweep at the earliest receipt.
_retry_after_id = 0


def _op(key: str, **kwargs) -> str:
    """Operator-facing text: the admin group's language, not the customer's."""
    from src.dashboard.i18n import t as dash_t
    return dash_t(key, **kwargs)


async def _header_text(ticket: Ticket, team_name: str, session=None) -> str:
    """What an operator reads before they read the customer.

    The channel line is the addition. On a Telegram-only instance it says
    Telegram every time and costs a line; on any other it is the difference
    between "reply here" and "this person is not in Telegram at all".
    """
    user = ticket.user
    channel = channel_of(user) if user is not None else TELEGRAM
    username = (user.username if user else None) or ""
    who = f"@{username}" if username else _op(f"ch_{channel}", ) or channel

    lines = [
        "#ticket",
        f"Ticket ID: {ticket.id}",
        f"Channel: {_op(f'ch_{channel}') or channel}",
        f"Team: {team_name}",
        f"User: {who} ({display_address(user) if user else '—'})",
        f"Category: {ticket.category or '—'}",
        "Status: Open",
    ]
    if session is not None and user is not None and user.linked_to_user_id:
        from src.database.models import User
        work = await session.get(User, user.linked_to_user_id)
        if work is not None:
            label = f"@{work.username}" if work.username else str(work.tg_id)
            lines.append(f"{_op('op_writes_from_personal')}{label}")
    return "\n".join(lines)


async def _create_header(bot: Bot, ticket: Ticket, chat_id: int, text: str) -> tuple[int, int | None] | None:
    """Network only: the caller has closed its snapshot transaction."""
    thread_id = None
    try:
        chat = await bot.get_chat(chat_id=chat_id)
        if chat.is_forum:
            topic = await bot.create_forum_topic(
                chat_id=chat_id, name=f"#{ticket.id} {ticket.category or 'Support'}"[:128],
            )
            thread_id = topic.message_thread_id
    except Exception:
        # Missing can_manage_topics, a regular group or unavailable getChat:
        # the header's message id still provides explicit safe reply routing.
        logger.warning("Forum topic unavailable for ticket %s; using header replies", ticket.id)
    kwargs = {"chat_id": chat_id, "text": text}
    if thread_id is not None:
        kwargs["message_thread_id"] = thread_id
    try:
        header = await bot.send_message(**kwargs)
    except Exception:
        # A topic created just before a failed header must not accumulate as
        # an orphan on every retry. Best-effort cleanup; no customer data was
        # sent into it yet.
        if thread_id is not None:
            try:
                await bot.delete_forum_topic(chat_id=chat_id, message_thread_id=thread_id)
            except Exception:
                logger.warning("Could not remove unused topic %s", thread_id)
        logger.warning("Could not announce ticket %s", ticket.id)
        return None
    return header.message_id, thread_id


def _visible(row: MessageRecord) -> bool:
    return (row.sender_type in {"user", "admin"} and not row.is_internal
            and row.deleted_at is None
            and (row.sender_type != "admin" or row.delivery_status == "delivered"))


async def _mirror(bot: Bot, ticket: Ticket, row: MessageRecord, chat_id: int,
                  file_key: str | None) -> int | None:
    """Network/file I/O only; return the provider id for a later short commit."""
    from src.channels import get as get_channel
    from src.channels.telegram import TelegramChannel
    from src.utils.file_processor import ATTACHMENT_SIZE_LIMIT
    from aiogram.types import BufferedInputFile

    body = ((row.sender_label + ":\n") if row.sender_label else "") + (row.text or "")
    if row.sender_type == "admin":
        body = "📤 " + body
    kwargs = {"chat_id": chat_id, "reply_to_message_id": ticket.header_message_id}
    if ticket.thread_id is not None:
        kwargs["message_thread_id"] = ticket.thread_id
    try:
        if row.file_id:
            if row.file_size and row.file_size > ATTACHMENT_SIZE_LIMIT:
                return None
            channel = channel_of(ticket.user)
            # Outgoing intents retain portable bytes even if the provider's
            # download API is unavailable. Incoming attachments use their own
            # channel's reference. Telegram ids must never cross adapters.
            content = None
            if row.sender_type == "admin":
                from src.services import file_store
                content = await asyncio.to_thread(file_store.get, file_key) if file_key else None
            if content is None:
                pipe = TelegramChannel(bot) if channel == TELEGRAM else get_channel(channel)
                content = await pipe.fetch_file(row.file_id)
            if not content or len(content) > ATTACHMENT_SIZE_LIMIT:
                logger.warning("Could not fetch inbound attachment on message %s", row.id)
                return None
            filename = row.file_name or ("photo.jpg" if row.file_type == "photo" else "attachment.bin")
            sent = await bot.send_document(document=BufferedInputFile(content, filename=filename),
                                            caption=body[:1023] + ("…" if len(body) > 1023 else ""),
                                            **kwargs)
        else:
            if not body:
                return None
            sent = await bot.send_message(text=body[:4095] + ("…" if len(body) > 4095 else ""), **kwargs)
    except Exception:
        logger.warning("Could not mirror message %s", row.id)
        return None
    return sent.message_id


async def _owned(session, ticket_id: int, owner: str) -> bool:
    from src.database.delivery_models import WorkerLease
    lease = await session.get(WorkerLease, f"mirror:{ticket_id}")
    return lease is not None and lease.owner == owner and lease.expires_at > datetime.utcnow()


async def _announce_locked(bot: Bot, ticket_id: int, message_id: int | None,
                           chat_id: int, owner: str) -> bool:
    """A per-ticket lease protects snapshots; no DB session spans I/O."""
    from src.database.delivery_models import OutboundJob
    async with db_session() as session:
        ticket = (await session.execute(select(Ticket).where(Ticket.id == ticket_id)
                  .options(selectinload(Ticket.user)))).scalar_one_or_none()
        if ticket is None or ticket.user is None:
            return False
        already_announced = ticket.header_message_id is not None
        row = None
        if message_id is not None:
            row = await session.get(MessageRecord, message_id)
            if row is None or row.ticket_id != ticket_id or not _visible(row):
                return False
        elif not already_announced:
            row = (await session.execute(select(MessageRecord).where(
                MessageRecord.ticket_id == ticket_id, MessageRecord.sender_type == "user",
                MessageRecord.is_internal.is_(False), MessageRecord.deleted_at.is_(None),
            ).order_by(MessageRecord.id).limit(1))).scalar_one_or_none()
        file_key = None
        if row is not None and row.sender_type == "admin" and row.file_id:
            file_key = (await session.execute(select(OutboundJob.file_key).where(
                OutboundJob.message_id == row.id))).scalar_one_or_none()
        header_text = ""
        if not already_announced:
            team_name = (await session.execute(select(Team.name).where(
                Team.id == ticket.team_id))).scalar_one_or_none() if ticket.team_id else None
            header_text = await _header_text(ticket, team_name or "—", session)

    if not already_announced:
        header = await _create_header(bot, ticket, chat_id, header_text)
        if header is None:
            return False
        ticket.header_message_id, ticket.thread_id = header
    elif ticket.thread_id == 1:
        ticket.thread_id = None  # Legacy General topic is never a routing id.

    # Header routing must be committed before exposing an actionable mirror.
    async with db_session(write=True) as session:
        if not await _owned(session, ticket_id, owner):
            return False
        saved_ticket = await session.get(Ticket, ticket_id)
        if saved_ticket is None:
            return False
        saved_ticket.header_message_id = ticket.header_message_id
        saved_ticket.thread_id = ticket.thread_id
        saved_ticket.notification_sent = True

    if row is None or row.admin_message_id is not None:
        return True
    provider_id = await _mirror(bot, ticket, row, chat_id, file_key)
    if provider_id is None:
        # announce_ticket historically reports whether the header succeeded.
        return message_id is None
    async with db_session(write=True) as session:
        if not await _owned(session, ticket_id, owner):
            return False
        saved_row = await session.get(MessageRecord, row.id)
        if saved_row is None or saved_row.ticket_id != ticket_id or not _visible(saved_row):
            return False
        if saved_row.admin_message_id is None:
            saved_row.admin_message_id = provider_id
    return True


async def _announce(bot: Bot, ticket_id: int, message_id: int | None) -> bool:
    from src.config import load_settings
    from src.services import worker_lease
    settings = load_settings()
    if bot is None or not settings.has_telegram or not settings.admin_chat_id:
        return False
    owner, acquired = secrets.token_hex(16), False
    try:
        async with asyncio.timeout(ANNOUNCE_TIMEOUT):
            # Parallel requests for one ticket wait for its short-lived lease;
            # unrelated intake/outbox writes remain free throughout the wait.
            while not acquired:
                acquired = await worker_lease.acquire(f"mirror:{ticket_id}", owner, LEASE_SECONDS)
                if not acquired:
                    await asyncio.sleep(0.1)
            return await _announce_locked(bot, ticket_id, message_id, settings.admin_chat_id, owner)
    except Exception as exc:
        logger.warning("Operator copy for ticket %s deferred (%s)", ticket_id, type(exc).__name__)
        return False
    finally:
        if acquired:
            try:
                await worker_lease.release(f"mirror:{ticket_id}", owner)
            except Exception:
                logger.warning("Operator-copy lease release deferred for ticket %s", ticket_id)


async def announce_ticket(bot: Bot, ticket_id: int) -> bool:
    """Announce a new ticket and its opening row, preserving existing history.

    Repeated calls never replay an old ticket's history. For a new inbound
    message, call announce_message with that exact row id after its commit.
    """
    return await _announce(bot, ticket_id, None)


async def announce_message(bot: Bot, ticket_id: int, message_id: int) -> bool:
    """Mirror one committed public message, including follow-ups and files.

    Outgoing messages become visible only after confirmed delivery. Serial
    retries are deduplicated; a crash after send but before DB commit can still
    repeat a notification because Telegram offers no idempotent send key.
    """
    return await _announce(bot, ticket_id, message_id)


async def retry_mirrors(bot: Bot | None = None, *, limit: int = 25) -> int:
    """Recover only exact ids recorded by this release's durable intake/outbox.

    Historical transcript rows have no receipt and are never replayed. A crash
    after Telegram accepts a copy but before its id commits can still duplicate
    that copy; Telegram does not support an idempotency key for sendMessage.
    """
    from src.config import load_settings
    from src.database.delivery_models import OutboundJob
    from src.database.inbound_models import InboundEvent, InboundMirror
    global _retry_after_id
    if limit <= 0:
        return 0
    if not load_settings().has_telegram or not load_settings().admin_chat_id:
        return 0
    if bot is None:
        from src.dashboard.bot_client import get_bot
        bot = get_bot()
    if bot is None:
        return 0
    async with db_session() as session:
        eligible = union(
            select(OutboundJob.message_id.label("message_id")).where(OutboundJob.status == "delivered"),
            select(InboundMirror.message_id.label("message_id")).join(
                InboundEvent, InboundEvent.id == InboundMirror.event_id).where(InboundEvent.status == "processed"),
            # Receipts made just before mirror mappings existed still identify
            # their opening row exactly, without looking through old tickets.
            select(InboundEvent.message_id.label("message_id")).where(InboundEvent.status == "processed"),
        ).subquery()
        query = select(MessageRecord.ticket_id, MessageRecord.id).join(
            eligible, eligible.c.message_id == MessageRecord.id).where(
                MessageRecord.admin_message_id.is_(None), MessageRecord.is_internal.is_(False),
                MessageRecord.deleted_at.is_(None),
                (MessageRecord.sender_type == "user") | (
                    (MessageRecord.sender_type == "admin") & (MessageRecord.delivery_status == "delivered")),
            ).order_by(MessageRecord.id).limit(limit)
        rows = (await session.execute(query.where(MessageRecord.id > _retry_after_id))).all()
        if not rows and _retry_after_id:
            rows = (await session.execute(query)).all()
        # A permanently unavailable attachment must not occupy the first
        # batch forever and hide every newer operator copy behind it.
        _retry_after_id = rows[-1][1] if rows else 0
    mirrored = 0
    for ticket_id, message_id in rows:
        mirrored += await announce_message(bot, ticket_id, message_id)
    return mirrored


async def poll_loop() -> None:
    while True:
        try:
            await retry_mirrors()
        except Exception:
            logger.warning("Operator-copy recovery pass failed; will retry")
        await asyncio.sleep(POLL_SECONDS)
