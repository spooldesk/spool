"""Channel-aware message edits with durable intent and optimistic concurrency."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from sqlalchemy import select
from src.channels import get as get_channel
from src.channels.base import Delivery
from src.channels.conversation_policy import policy_for
from src.channels.identity import address_of, channel_of
from src.database.db import db_session
from src.database.models import AdminUser, Message, MessageRevision, Ticket, User
from src.services.permissions import can


def can_edit(message, channel: str) -> bool:
    get = message.get if isinstance(message, dict) else lambda k: getattr(message, k, None)
    if (get("sender_type") != "admin" or get("deleted_at") or get("options")
            or get("mutation_status") in {"pending", "uncertain"}):
        return False
    if get("is_internal"):
        return True
    if get("delivery_status") != "delivered":
        return False
    policy = policy_for(channel)
    if channel == "widget":
        return policy.edit_text
    return bool(get("channel_ref") and (policy.edit_caption if get("file_id") else policy.edit_text))


async def edit_message(ticket_id: int, message_id: int, operator_id: int,
                       text: str, expected_revision: int) -> Delivery:
    text = text.strip()
    if not text or len(text) > 4096:
        return Delivery.failed("chat_invalid_text")
    async with db_session(write=True) as session:
        actor = await session.get(AdminUser, operator_id)
        row = await session.get(Message, message_id)
        if not await can(actor, 'tickets', edit=True, session=session):
            return Delivery.failed("chat_edit_forbidden")
        if row is None or row.ticket_id != ticket_id:
            return Delivery.failed("e_message_not_found")
        if actor.role not in {"owner", "admin"} and row.operator_id != actor.id:
            return Delivery.failed("chat_edit_forbidden")
        ticket = await session.get(Ticket, ticket_id)
        user = await session.get(User, ticket.user_id)
        channel = channel_of(user)
        if row.revision != expected_revision:
            return Delivery.failed("chat_edit_conflict")
        if not can_edit(row, channel):
            return Delivery.failed("chat_edit_unsupported")
        if channel == "telegram" and row.file_id and len(text) > 1024:
            return Delivery.failed("chat_invalid_text")
        if row.text == text:
            return Delivery.sent(row.channel_ref)
        change = MessageRevision(message_id=row.id, operator_id=actor.id,
                                 kind="edit", before_text=row.text or '', after_text=text, status="pending")
        session.add(change)
        row.mutation_status = "pending"
        await session.flush()
        change_id = change.id
        local = row.is_internal or channel == "widget"
        address, ref, caption = address_of(user), row.channel_ref, bool(row.file_id)
        external_text = text
        if channel == 'slack' and row.reply_to_id:
            parent = await session.get(Message,row.reply_to_id)
            if parent and parent.ticket_id == ticket_id and not parent.is_internal and not parent.deleted_at:
                from src.channels.conversation_policy import quote_reply
                quote = quote_reply(parent.sender_label or parent.sender_type,parent.text or parent.file_name or '')
                external_text = quote.replace('&','&amp;').replace('<','&lt;').replace('>','&gt;') + text
    try:
        if local:
            result = Delivery.sent(ref)
        else:
            adapter = get_channel(channel)
            async with asyncio.timeout(30):
                result = await adapter.edit_message(address, ref, external_text, caption=caption)
    except Exception:
        result = Delivery.failed("chat_edit_uncertain")
    async with db_session(write=True) as session:
        row = await session.get(Message, message_id)
        change = await session.get(MessageRevision, change_id)
        if row is None or change is None:
            return Delivery.failed("chat_edit_uncertain")
        if result.ok:
            row.text = text
            row.edited_at = datetime.utcnow()
            row.revision += 1
            row.mutation_status = None
            change.status = "applied"
        else:
            uncertain = result.error_key in {"chat_edit_uncertain", "e_delivery_uncertain"}
            row.mutation_status = "uncertain" if uncertain else None
            change.status = "uncertain" if uncertain else "failed"
    return result


async def recover_stale_edits() -> None:
    """A killed request must never masquerade as a confirmed external edit."""
    async with db_session(write=True) as session:
        changes = (await session.execute(select(MessageRevision).where(
            MessageRevision.status == "pending",
            MessageRevision.created_at < datetime.utcnow() - timedelta(minutes=2),
        ))).scalars().all()
        for change in changes:
            change.status = "uncertain"
            row = await session.get(Message, change.message_id)
            if row is not None:
                row.mutation_status = "uncertain"


async def reply_context(session, ticket_id: int, parent_id: int | None) -> Message | None:
    if not parent_id:
        return None
    parent = await session.get(Message, parent_id)
    if (parent is None or parent.ticket_id != ticket_id or parent.is_internal
            or parent.deleted_at or parent.delivery_status != "delivered"):
        raise ValueError("Invalid or private reply target")
    return parent


async def reconcile_mutation(ticket_id: int, message_id: int, operator_id: int,
                             expected_revision: int, applied: bool) -> Delivery:
    """Owner/admin attests to native channel history; never sends a second request."""
    async with db_session(write=True) as session:
        actor = await session.get(AdminUser, operator_id)
        row = await session.get(Message, message_id)
        if not actor or not actor.is_active or actor.role not in {'owner','admin'}:
            return Delivery.failed('chat_edit_forbidden')
        if not row or row.ticket_id != ticket_id:
            return Delivery.failed('e_message_not_found')
        if row.revision != expected_revision or row.mutation_status != 'uncertain':
            return Delivery.failed('chat_edit_conflict')
        change = (await session.execute(select(MessageRevision).where(
            MessageRevision.message_id == message_id, MessageRevision.status == 'uncertain',
            MessageRevision.kind.in_(['edit','delete']),
        ).order_by(MessageRevision.id.desc()).limit(1))).scalar_one_or_none()
        if not change:
            return Delivery.failed('chat_edit_conflict')
        if applied:
            if change.kind == 'delete': row.deleted_at = datetime.utcnow()
            else: row.text, row.edited_at = change.after_text, datetime.utcnow()
        row.revision += 1
        row.mutation_status = None
        change.status = 'reconciled_applied' if applied else 'reconciled_unchanged'
        session.add(MessageRevision(message_id=row.id,operator_id=actor.id,kind='reconcile',
            before_text=change.before_text or '',after_text=row.text or '',status=change.status))
    return Delivery.sent(row.channel_ref)
