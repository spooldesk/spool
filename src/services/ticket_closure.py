"""Shared transactional closure, including the durable customer notification."""
from datetime import datetime

from src.database.models import Message
from src.services import csat_service
from src.utils.i18n import CLOSURE_SYSTEM_TEXT


async def close_in_session(session, ticket, user, *, operator_id=None,
                           categories=(), custom_reason="", reason_prefix=""):
    if ticket.status != "active":
        return None
    now = datetime.utcnow()
    if ticket.created_at:
        ticket.processing_time_seconds = int((now - ticket.created_at).total_seconds())
    ticket.status, ticket.closed_at, ticket.notification_sent = "closed", now, False
    ticket.admin_categories = list(categories) or None
    ticket.custom_reason = custom_reason or None
    reason = ", ".join(categories)
    if custom_reason:
        reason = (reason + " — " if reason else "") + custom_reason
    session.add(Message(ticket_id=ticket.id, sender_type="admin", sender_id=0,
        operator_id=operator_id, text=CLOSURE_SYSTEM_TEXT + (reason_prefix + reason if reason else "")))
    return await csat_service.enqueue_close_notification(session, ticket, user)
