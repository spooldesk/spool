"""Ticket category catalog — CRUD over the editable TicketCategory table.

Shared by the bot (builds the category keyboard) and the dashboard (CRUD UI).
Categories are single-name; `ticket.category` stores the name as a snapshot,
so renaming/deleting never breaks existing tickets.
"""

from __future__ import annotations

import logging
from typing import Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.database.models import TicketCategory

logger = logging.getLogger(__name__)


class CatalogError(Exception):
    """Validation/conflict error for a category operation."""
def _t(key: str, **kwargs) -> str:
    from src.dashboard.i18n import t as dash_t
    return dash_t(key, **kwargs)


async def list_active(session: AsyncSession) -> Sequence[TicketCategory]:
    """Active categories, ordered for display (used by the bot keyboard)."""
    return (await session.execute(
        select(TicketCategory)
        .where(TicketCategory.is_active.is_(True))
        .order_by(TicketCategory.sort_order.asc(), TicketCategory.name.asc())
    )).scalars().all()


async def list_all(session: AsyncSession) -> Sequence[TicketCategory]:
    """Every category (incl. inactive), for the management UI."""
    return (await session.execute(
        select(TicketCategory).order_by(TicketCategory.sort_order.asc(), TicketCategory.name.asc())
    )).scalars().all()


async def get(session: AsyncSession, category_id: int) -> TicketCategory | None:
    return await session.get(TicketCategory, category_id)


async def create(session: AsyncSession, *, name: str, emoji: str | None) -> TicketCategory:
    name = (name or "").strip()
    emoji = (emoji or "").strip() or None
    if not name:
        raise CatalogError(_t("v_name_empty"))
    if len(name) > 64:
        raise CatalogError(_t("v_name_too_long"))
    existing = (await session.execute(
        select(TicketCategory).where(func.lower(TicketCategory.name) == name.lower())
    )).scalar_one_or_none()
    if existing is not None:
        raise CatalogError(_t("v_duplicate", name=name))
    # Append at the end
    max_order = (await session.execute(select(func.max(TicketCategory.sort_order)))).scalar() or 0
    cat = TicketCategory(name=name, emoji=emoji, sort_order=int(max_order) + 1, is_active=True)
    session.add(cat)
    await session.flush()
    logger.info(f"Category created: {name!r}")
    return cat


async def update(session: AsyncSession, category_id: int, *,
                 name: str | None = None, emoji: str | None = None,
                 is_active: bool | None = None) -> TicketCategory:
    cat = await session.get(TicketCategory, category_id)
    if cat is None:
        raise CatalogError(_t("v_not_found", id=category_id))
    if name is not None:
        name = name.strip()
        if not name or len(name) > 64:
            raise CatalogError(_t("v_name_length"))
        dup = (await session.execute(
            select(TicketCategory).where(
                func.lower(TicketCategory.name) == name.lower(),
                TicketCategory.id != category_id,
            )
        )).scalar_one_or_none()
        if dup is not None:
            raise CatalogError(_t("v_duplicate", name=name))
        cat.name = name
    if emoji is not None:
        cat.emoji = emoji.strip() or None
    if is_active is not None:
        cat.is_active = is_active
    await session.flush()
    return cat


async def delete(session: AsyncSession, category_id: int) -> None:
    cat = await session.get(TicketCategory, category_id)
    if cat is None:
        raise CatalogError(_t("v_not_found", id=category_id))
    await session.delete(cat)
    logger.info(f"Category #{category_id} deleted")


async def move(session: AsyncSession, category_id: int, direction: int) -> None:
    """Swap sort_order with the neighbour above (-1) or below (+1)."""
    cat = await session.get(TicketCategory, category_id)
    if cat is None:
        return
    cats = list(await list_all(session))
    idx = next((i for i, c in enumerate(cats) if c.id == category_id), None)
    if idx is None:
        return
    swap = idx + direction
    if 0 <= swap < len(cats):
        cat.sort_order, cats[swap].sort_order = cats[swap].sort_order, cat.sort_order
        await session.flush()
