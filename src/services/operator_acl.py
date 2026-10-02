"""Authorization shared by Telegram commands, callbacks and FSM steps.

Membership of the operators' group does not grant application permissions.
Every action checks the current linked account, so disabling an account or
changing its role takes effect on the next message, including mid-FSM.
"""

from __future__ import annotations

from functools import wraps
import logging

from sqlalchemy import select

from src.config import load_settings
from src.database.db import db_session
from src.database.models import AdminUser

logger = logging.getLogger(__name__)

TICKET_ROLES = frozenset({"owner", "admin", "agent"})
MANAGE_ROLES = frozenset({"owner", "admin"})


async def actor_for(event, *, roles=TICKET_ROLES) -> AdminUser | None:
    """The active operator represented by this event, or a closed boundary."""
    # A callback is authorized against the message holding its keyboard.
    # Inline messages and callbacks without a chat are never operator actions.
    message = getattr(event, "message", None) if hasattr(event, "data") else event
    chat = getattr(message, "chat", None)
    sender = getattr(event, "from_user", None)
    group_id = load_settings().admin_chat_id
    if (not group_id or chat is None or chat.id != group_id or sender is None
            or getattr(sender, "is_bot", False)
            or getattr(message, "sender_chat", None) is not None):
        return None
    try:
        async with db_session() as session:
            actor = (await session.execute(
                select(AdminUser).where(
                    AdminUser.tg_id == sender.id,
                    AdminUser.is_active == True,  # noqa: E712
                )
            )).scalar_one_or_none()
        return actor if actor and actor.role in roles else None
    except Exception:
        # A database outage cannot turn an unknown sender into an operator.
        logger.exception("Could not authorize a Telegram operator action")
        return None


def operator_only(*roles: str):
    """Guard the actual callable as well as the aiogram registration.

    `wraps` preserves aiogram's dependency-injection signature. Keeping the
    guard on the function also protects direct calls from other entry points.
    """
    allowed = frozenset(roles) if roles else TICKET_ROLES

    def decorate(handler):
        @wraps(handler)
        async def guarded(event, *args, **kwargs):
            if await actor_for(event, roles=allowed) is None:
                if hasattr(event, "data"):
                    try:
                        await event.answer()
                    except Exception:
                        pass
                return None
            return await handler(event, *args, **kwargs)

        guarded.operator_roles = allowed
        return guarded

    return decorate
