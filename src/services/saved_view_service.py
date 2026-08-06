"""Named filter sets, per operator.

Setting up "my overdue, unassigned, this team" takes four controls and gets
done twenty times a week. Naming it once is the whole feature.
"""

from __future__ import annotations

import logging
from urllib.parse import parse_qsl, urlencode

from sqlalchemy import delete, select

from src.database.db import db_session
from src.database.models import SavedView

logger = logging.getLogger(__name__)

# Where a view may be saved from. Anywhere else and the stored query would
# mean nothing when applied.
ALLOWED_PATHS = ("/tickets", "/board")

MAX_PER_OPERATOR = 20
MAX_NAME = 60


def normalise_query(raw: str) -> str:
    """Keep only real filters, in a stable order.

    Dropping `page` matters: a view is a filter, and one that remembers it was
    saved on page 4 sends the operator somewhere confusing every time. Sorting
    means the same filters saved in a different order are the same view.
    """
    pairs = [(k, v) for k, v in parse_qsl(raw or "", keep_blank_values=False)
             if k not in ("page", "csrf_token") and v]
    return urlencode(sorted(pairs))


async def list_for(admin_id: int, path: str | None = None) -> list[SavedView]:
    async with db_session() as session:
        stmt = select(SavedView).where(SavedView.admin_id == admin_id)
        if path:
            stmt = stmt.where(SavedView.path == path)
        return list((await session.execute(
            stmt.order_by(SavedView.created_at.asc())
        )).scalars().all())


async def save(admin_id: int, *, name: str, path: str, query: str) -> SavedView | None:
    """Create or overwrite by name. Returns None if the input is unusable.

    Overwriting rather than erroring on a duplicate name: someone typing the
    same name again means "update this", and making them delete first to
    achieve that is a worse answer than doing it.
    """
    name = (name or "").strip()[:MAX_NAME]
    if not name or path not in ALLOWED_PATHS:
        return None

    query = normalise_query(query)[:500]

    async with db_session() as session:
        existing = (await session.execute(
            select(SavedView).where(
                SavedView.admin_id == admin_id, SavedView.name == name,
            )
        )).scalar_one_or_none()

        if existing is not None:
            existing.path = path
            existing.query = query
            await session.commit()
            return existing

        count = len((await session.execute(
            select(SavedView.id).where(SavedView.admin_id == admin_id)
        )).all())
        if count >= MAX_PER_OPERATOR:
            logger.info(f"operator {admin_id} is at the saved-view limit")
            return None

        view = SavedView(admin_id=admin_id, name=name, path=path, query=query)
        session.add(view)
        await session.commit()
        await session.refresh(view)
        return view


async def remove(admin_id: int, view_id: int) -> bool:
    """Delete one of your own. Scoped by owner so an id from elsewhere does
    not reach into someone else's list."""
    async with db_session() as session:
        result = await session.execute(
            delete(SavedView).where(
                SavedView.id == view_id, SavedView.admin_id == admin_id,
            )
        )
        await session.commit()
        return bool(result.rowcount)
