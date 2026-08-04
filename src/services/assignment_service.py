"""Ticket assignment — which operator owns which ticket.

Rules the whole product follows:
  - Manual assignment from the ticket card wins over everything.
  - First responder auto-claims an UNASSIGNED ticket (dashboard reply, or a
    Telegram reply from an admin whose tg_id is linked to an operator account).
    An already-assigned ticket is never silently re-assigned.
"""

from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy import select

from src.database.db import db_session
from src.database.models import AdminUser, Ticket

logger = logging.getLogger(__name__)


async def assign_ticket(ticket_id: int, admin_id: Optional[int]) -> bool:
    """Set (or clear, with None) the ticket's operator. Returns False if the
    ticket doesn't exist or admin_id points at a missing/disabled account."""
    async with db_session() as session:
        ticket = await session.get(Ticket, ticket_id)
        if ticket is None:
            return False
        if admin_id is not None:
            admin = await session.get(AdminUser, admin_id)
            if admin is None or not admin.is_active:
                return False
        ticket.assigned_admin_id = admin_id
        await session.commit()
    logger.info(f"Ticket #{ticket_id} assigned to operator {admin_id}")
    return True


async def auto_assign(ticket_id: int, admin_id: int) -> bool:
    """First-responder claim: assign only if the ticket is still unassigned.
    Returns True if this call set the assignee."""
    async with db_session() as session:
        ticket = await session.get(Ticket, ticket_id)
        if ticket is None or ticket.assigned_admin_id is not None:
            return False
        admin = await session.get(AdminUser, admin_id)
        if admin is None or not admin.is_active:
            return False
        ticket.assigned_admin_id = admin_id
        await session.commit()
    logger.info(f"Ticket #{ticket_id} auto-assigned to operator {admin_id} (first responder)")
    return True


async def auto_assign_by_tg(ticket_id: int, tg_id: int) -> bool:
    """Telegram-side first-responder claim: resolve the admin's tg_id to an
    operator account (if linked) and auto-assign. No-op when unlinked."""
    if not tg_id:
        return False
    async with db_session() as session:
        admin = (await session.execute(
            select(AdminUser).where(AdminUser.tg_id == tg_id, AdminUser.is_active == True)  # noqa: E712
        )).scalar_one_or_none()
    if admin is None:
        return False
    return await auto_assign(ticket_id, admin.id)


async def list_active_operators() -> list[AdminUser]:
    """Operators offered in assignment dropdowns."""
    async with db_session() as session:
        return list((await session.execute(
            select(AdminUser).where(AdminUser.is_active == True)  # noqa: E712
            .order_by(AdminUser.name.asc(), AdminUser.email.asc())
        )).scalars().all())
