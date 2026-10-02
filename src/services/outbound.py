"""Commit outgoing intent before network I/O; recover ambiguous sends safely."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from datetime import datetime, timedelta

from sqlalchemy import select, text as sql_text, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.channels import get as get_channel
from src.channels.base import Delivery, Option
from src.channels.identity import address_of, channel_of
from src.database.db import acquire_write_lock, db_session
from src.database.delivery_models import OutboundJob
from src.database.models import AdminUser, Message, ReplyTemplate, Ticket, User
from src.services import file_store
from src.services.response_clock import WAITING_SINCE_SQL

logger = logging.getLogger(__name__)
SEND_TIMEOUT = 90
STALE_AFTER = timedelta(minutes=5)
MAX_ATTEMPTS = 6


async def enqueue(
    session: AsyncSession, *, ticket_id: int, channel: str | None, address: str,
    text: str, options: Sequence[Option] | None = None, sender_id: int = 0,
    operator_id: int | None = None, template_id: int | None = None,
    admin_message_id: int | None = None,
    require_active: bool = False,
    purpose: str | None = None,
    allow_unconfigured: bool = False,
    filename: str | None = None, content: bytes | None = None,
    mime_type: str | None = None,
    reply_to_id: int | None = None,
) -> OutboundJob:
    """Stage an intent in the caller's transaction. Commit before deliver()."""
    await acquire_write_lock(session)
    from src.services.conversation_service import reply_context
    await reply_context(session, ticket_id, reply_to_id)
    ticket = await session.get(Ticket, ticket_id)
    user = await session.get(User, ticket.user_id) if ticket else None
    if user is None or channel_of(user) != (channel or "telegram") or address_of(user) != address:
        raise ValueError("Ticket does not belong to this channel address")
    if (require_active or operator_id is not None or sender_id) and (
            not user.is_active or ticket.status != "active"):
        raise ValueError("Ticket is closed or customer access has been revoked")
    if purpose:
        existing = (await session.execute(select(OutboundJob).join(
            Message, Message.id == OutboundJob.message_id).where(
                Message.ticket_id == ticket_id, OutboundJob.purpose == purpose,
        ))).scalar_one_or_none()
        if existing is not None:
            return existing
    if admin_message_id is not None:
        existing = (await session.execute(select(OutboundJob).join(
            Message, Message.id == OutboundJob.message_id).where(
                Message.ticket_id == ticket_id, Message.admin_message_id == admin_message_id,
        ))).scalar_one_or_none()
        if existing is not None:
            return existing
    try:
        pipe = get_channel(channel)
    except LookupError:
        if not allow_unconfigured:
            raise
        pipe = None
    if content is not None and (pipe is None or not pipe.caps.files):
        raise ValueError("This channel cannot deliver attachments")
    if operator_id is None and sender_id:
        operator_id = (await session.execute(select(AdminUser.id).where(
            AdminUser.tg_id == sender_id, AdminUser.is_active.is_(True),
        ))).scalar_one_or_none()
    actor = await session.get(AdminUser,operator_id) if operator_id is not None else None
    from src.services.permissions import can
    section = 'users' if purpose == 'customer_direct' else 'tickets'
    if operator_id is not None and not await can(actor, section, edit=True, session=session):
        raise ValueError('Operator access has been revoked')
    file_key = file_store.put(content) if content is not None else None
    row = Message(
        ticket_id=ticket_id, sender_type="admin", sender_id=sender_id,
        operator_id=operator_id, text=text, template_id=template_id,
        sender_label=(actor.name or actor.email.split('@')[0]) if actor else None,
        reply_to_id=reply_to_id,
        admin_message_id=admin_message_id,
        delivery_status="pending", file_id=file_key,
        file_type="document" if file_key else None, file_name=filename,
        file_size=len(content) if content is not None else None, mime_type=mime_type,
        options=[{"label": o.label, "payload": o.payload} for o in options] if options else None,
    )
    session.add(row)
    await session.flush()
    job = OutboundJob(message_id=row.id, channel=channel or "telegram", address=address,
                      file_key=file_key, purpose=purpose)
    session.add(job)
    await session.flush()
    return job


async def _enqueue_and_send(**kwargs) -> Delivery:
    try:
        async with db_session(write=True) as session:
            job = await enqueue(session, **kwargs)
            job_id = job.id
    except (LookupError, ValueError) as exc:
        return Delivery.failed("e_send_failed", str(exc))
    return await deliver(job_id)


async def say(*, ticket_id: int, channel: str | None, address: str, text: str,
              options: Sequence[Option] | None = None, sender_id: int = 0,
              operator_id: int | None = None, admin_message_id: int | None = None,
              require_active: bool = False) -> Delivery:
    return await _enqueue_and_send(ticket_id=ticket_id, channel=channel, address=address,
                                   text=text, options=options, sender_id=sender_id,
                                   operator_id=operator_id, admin_message_id=admin_message_id,
                                   require_active=require_active)


async def say_file(*, ticket_id: int, channel: str | None, address: str, filename: str,
                   content: bytes, caption: str | None = None, sender_id: int = 0,
                   operator_id: int | None = None, mime_type: str | None = None,
                   admin_message_id: int | None = None, require_active: bool = False) -> Delivery:
    return await _enqueue_and_send(ticket_id=ticket_id, channel=channel, address=address,
                                   text=caption or "", filename=filename, content=content,
                                   sender_id=sender_id, operator_id=operator_id, mime_type=mime_type,
                                   admin_message_id=admin_message_id, require_active=require_active)


async def deliver(job_id: int) -> Delivery:
    async with db_session(write=True) as session:
        job = await session.get(OutboundJob, job_id)
        if job is None:
            return Delivery.failed("e_send_failed", "Delivery no longer exists")
        row = await session.get(Message, job.message_id)
        if job.status == "delivered":
            return Delivery(ok=True, ref=row.channel_ref, file_ref=row.file_id)
        if job.status not in {"pending", "retry"}:
            return Delivery.failed("e_delivery_" + job.status)
        ticket = await session.get(Ticket, row.ticket_id)
        user = await session.get(User, ticket.user_id)
        if not user.is_active:
            job.status = row.delivery_status = "cancelled"
            row.delivery_error = job.last_error = "e_delivery_cancelled"
            return Delivery.failed("e_delivery_cancelled")
        if job.next_attempt_at and job.next_attempt_at > datetime.utcnow():
            return Delivery.failed("e_delivery_pending")
        earlier = (await session.execute(select(OutboundJob.id).join(
            Message, Message.id == OutboundJob.message_id).where(
                Message.ticket_id == row.ticket_id, OutboundJob.id < job.id,
                OutboundJob.status.in_(["pending", "sending", "retry"]),
            ).limit(1))).scalar_one_or_none()
        if earlier is not None:
            return Delivery.failed("e_delivery_pending")
        if row.deleted_at:
            job.status = row.delivery_status = "cancelled"
            return Delivery.failed("e_delivery_cancelled")
        job.status = row.delivery_status = "sending"
        job.started_at = datetime.utcnow()
        job.attempts += 1
        from src.services.conversation_service import reply_context
        try:
            parent = await reply_context(session, row.ticket_id, row.reply_to_id)
        except ValueError:
            job.status = row.delivery_status = "failed"
            job.last_error = row.delivery_error = "chat_reply_unavailable"
            return Delivery.failed("chat_reply_unavailable")
        data = dict(channel=job.channel, address=job.address, file_key=job.file_key,
                    text=row.text, ticket_id=row.ticket_id, filename=row.file_name,
                    conversation_key=ticket.conversation_key,
                    reply_ref=parent.channel_ref if parent else None,
                    reply_text=(parent.text or parent.file_name) if parent else None,
                    reply_author=(parent.sender_label or parent.sender_type) if parent else None,
                    options=[Option(**o) for o in row.options] if row.options else None)

    uncertain = False
    try:
        pipe = get_channel(data["channel"])
        content = file_store.get(data["file_key"]) if data["file_key"] else None
        if data["file_key"] and content is None:
            outcome = Delivery.failed("e_send_failed", "Stored attachment is missing")
        elif data["channel"] == "widget":
            outcome = Delivery(ok=True, file_ref=data["file_key"])
        else:
            async with asyncio.timeout(SEND_TIMEOUT):
                if data["reply_text"] and (data["channel"] != "telegram" or not data["reply_ref"]):
                    from src.channels.conversation_policy import quote_reply
                    quote = quote_reply(data["reply_author"] or "", data["reply_text"])
                    if data["channel"] == "slack":
                        quote = quote.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                    data["text"] = quote + data["text"]
                if data["channel"] == "email" and hasattr(pipe, "send_for_ticket"):
                    outcome = await pipe.send_for_ticket(
                        data["address"], data["text"], ticket_id=data["ticket_id"],
                        options=data["options"], delivery_key=str(job_id),
                        attachment=(data["filename"], content) if content is not None else None,
                    )
                elif data["channel"] == "slack" and hasattr(pipe, "send_for_ticket"):
                    outcome = await pipe.send_for_ticket(
                        data["address"], data["text"], conversation_key=data["conversation_key"],
                        options=data["options"],
                        attachment=(data["filename"], content) if content is not None else None,
                    )
                elif content is not None:
                    extra = {"reply_ref": data["reply_ref"]} if data["channel"] == "telegram" and data["reply_ref"] else {}
                    outcome = await pipe.send_file(data["address"], filename=data["filename"],
                                                   content=content, caption=data["text"] or None, **extra)
                else:
                    extra = {"reply_ref": data["reply_ref"]} if data["channel"] == "telegram" and data["reply_ref"] else {}
                    outcome = await pipe.send_text(data["address"], data["text"], options=data["options"], **extra)
    except LookupError:
        outcome = Delivery.failed("e_send_failed", "Channel is not configured")
    except Exception:
        logger.warning("Ambiguous delivery for outgoing job %s", job_id)
        outcome = Delivery.failed("e_delivery_uncertain")
        uncertain = True

    uncertain = uncertain or outcome.error_key == "e_delivery_uncertain"
    async with db_session(write=True) as session:
        job = await session.get(OutboundJob, job_id)
        row = await session.get(Message, job.message_id)
        if outcome.ok:
            human_reply = not row.is_internal and bool(row.operator_id or row.sender_id)
            clock_query = sql_text(f"SELECT {WAITING_SINCE_SQL} FROM tickets t WHERE t.id = :id")
            before_waiting = (await session.execute(clock_query, {"id": row.ticket_id})).scalar_one() if human_reply else None
            job.status = row.delivery_status = "delivered"
            job.delivered_at = datetime.utcnow()
            row.delivered_at = job.delivered_at
            row.channel_ref = outcome.ref
            if outcome.file_ref:
                row.file_id = outcome.file_ref
            job.last_error = row.delivery_error = None
            ticket = await session.get(Ticket, row.ticket_id)
            if ticket.status == "active" and human_reply:
                # Do not hide a customer message that arrived during a slow
                # send/retry. Both this decision and intake own the write lock.
                await session.flush()
                waiting_since = (await session.execute(clock_query, {"id": ticket.id})).scalar_one()
                ticket.stage = "in_progress" if waiting_since is not None else "waiting"
                if waiting_since is None or waiting_since != before_waiting:
                    ticket.sla_breached_at = None
                if row.operator_id and ticket.assigned_admin_id is None:
                    from src.services.permissions import can
                    actor = await session.get(AdminUser, row.operator_id)
                    if await can(actor, 'tickets', edit=True, session=session):
                        ticket.assigned_admin_id = row.operator_id
            if row.template_id:
                await session.execute(update(ReplyTemplate).where(ReplyTemplate.id == row.template_id)
                                      .values(usage_count=ReplyTemplate.usage_count + 1))
        else:
            state = "uncertain" if uncertain else "failed"
            if outcome.retry_after is not None and job.attempts < MAX_ATTEMPTS:
                state = "retry"
                job.next_attempt_at = datetime.utcnow() + timedelta(seconds=max(1, outcome.retry_after))
            job.status = row.delivery_status = state
            job.last_error = row.delivery_error = outcome.error_key or "e_send_failed"
    return outcome


async def retry(job_id: int, *, allow_uncertain: bool = False) -> Delivery:
    """Explicit operator retry; uncertain delivery requires a distinct action."""
    async with db_session(write=True) as session:
        job = await session.get(OutboundJob, job_id)
        if job is None or job.status not in ({"failed", "uncertain"} if allow_uncertain else {"failed"}):
            return Delivery.failed("e_send_failed")
        job.status = "pending"
        job.next_attempt_at = None
        row = await session.get(Message, job.message_id)
        row.delivery_status = "pending"
    return await deliver(job_id)


async def run_once(limit: int = 25) -> int:
    async with db_session(write=True) as session:
        stale = list((await session.execute(select(OutboundJob).where(
            OutboundJob.status == "sending", OutboundJob.started_at < datetime.utcnow() - STALE_AFTER,
        ))).scalars())
        for job in stale:
            job.status = "uncertain"
            row = await session.get(Message, job.message_id)
            row.delivery_status = "uncertain"
            row.delivery_error = job.last_error = "e_delivery_uncertain"
        ids = list((await session.execute(select(OutboundJob.id).where(
            OutboundJob.status.in_(["pending", "retry"]),
            (OutboundJob.next_attempt_at.is_(None)) | (OutboundJob.next_attempt_at <= datetime.utcnow()),
        ).order_by(OutboundJob.id).limit(limit))).scalars())
    for job_id in ids:
        await deliver(job_id)
    return len(ids)


async def poll_loop() -> None:
    while True:
        try:
            from src.services.conversation_service import recover_stale_edits
            await recover_stale_edits()
            await run_once()
        except Exception:
            logger.exception("Outbound queue pass failed")
        await asyncio.sleep(5)
