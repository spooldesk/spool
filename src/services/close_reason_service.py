"""Ticket-closure reason catalog — CRUD over the editable CloseReason table.

Shared by the bot (builds the multiselect close keyboard) and the dashboard
(CRUD UI + ticket close form). Reason *names* are stored as a snapshot on
Ticket.admin_categories, so renaming/deleting never breaks closed tickets.

The free-text "custom" option is NOT a DB row — it's a code-level sentinel
(CUSTOM_VALUE / CUSTOM_CODE) handled by the close flow.
"""

from __future__ import annotations

import logging
from typing import Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.database.models import CloseReason

logger = logging.getLogger(__name__)

# Sentinel for the free-text closure option. This value is SNAPSHOTTED into
# Ticket.admin_categories, so it must never be a human language: an English
# instance would otherwise show a Russian reason on every custom closure, and
# the export would carry it too. Display is resolved at render time.
CUSTOM_VALUE = "__custom__"
CUSTOM_CODE = "c"

# Same reasoning for the reason the AI deflection flow writes when it resolves
# a ticket on its own. Single source of truth: the reports query and deflect.py
# both read it from here — they used to carry duplicate literals, so changing
# one silently zeroed the "closed by AI" metric.
AI_VALUE = "__ai__"

# What the sentinels used to be, before they became language-neutral. Old rows
# are rewritten by the migration in db.py; these stay as a read-side safety net
# for databases restored from an older backup.
LEGACY_CUSTOM_VALUES = ("Свой вариант",)
LEGACY_AI_VALUES = ("Решено ИИ", "Solved by AI")


def is_custom(name: str | None) -> bool:
    return name == CUSTOM_VALUE or name in LEGACY_CUSTOM_VALUES


def is_ai(name: str | None) -> bool:
    return name == AI_VALUE or name in LEGACY_AI_VALUES


def ai_display() -> str:
    from src.dashboard.i18n import t as dash_t
    return f"🤖 {dash_t('close_reason_ai')}"


def custom_display() -> str:
    """Label for the free-text option, in the instance's UI language."""
    from src.dashboard.i18n import t as dash_t
    return f"✏️ {dash_t('close_reason_custom')}"


def display_stored(name: str) -> str:
    """Render a value snapshotted on Ticket.admin_categories.

    Only the sentinels need translating — every other value is a reason name
    the operator typed themselves, and we show those verbatim.
    """
    if is_custom(name):
        return custom_display()
    if is_ai(name):
        return ai_display()
    return name


class CloseReasonError(Exception):
    """Validation/conflict error for a close-reason operation.

    The message is already localised — routes flash it to the operator as-is.
    """


def _t(key: str, **kwargs) -> str:
    from src.dashboard.i18n import t as dash_t
    return dash_t(key, **kwargs)


def _is_reserved(name: str) -> bool:
    """A catalog reason must not shadow the free-text option.

    Checked against the sentinel and the custom label in EVERY supported UI
    language, not just the active one — otherwise an operator could create
    "Свой вариант" while the UI is English and get two identical-looking rows
    the moment someone switches the language.
    """
    from src.dashboard.i18n import SUPPORTED_DASH_LANGUAGES, strings
    low = name.strip().lower()
    if low == CUSTOM_VALUE.lower() or low in {v.lower() for v in LEGACY_CUSTOM_VALUES}:
        return True
    return any(
        low == strings(lang).get("close_reason_custom", "").lower()
        for lang in SUPPORTED_DASH_LANGUAGES
    )


def display_name(reason: CloseReason) -> str:
    """`emoji name` for keyboards/UI."""
    return f"{reason.emoji + ' ' if reason.emoji else ''}{reason.name}".strip()


async def list_active(session: AsyncSession) -> Sequence[CloseReason]:
    return (await session.execute(
        select(CloseReason)
        .where(CloseReason.is_active.is_(True))
        .order_by(CloseReason.sort_order.asc(), CloseReason.name.asc())
    )).scalars().all()


async def list_all(session: AsyncSession) -> Sequence[CloseReason]:
    return (await session.execute(
        select(CloseReason).order_by(CloseReason.sort_order.asc(), CloseReason.name.asc())
    )).scalars().all()


async def get(session: AsyncSession, reason_id: int) -> CloseReason | None:
    return await session.get(CloseReason, reason_id)


async def display_map(session: AsyncSession) -> dict[str, str]:
    """{name -> 'emoji name'} for all reasons, to render stored snapshot values.

    Includes the CUSTOM sentinel so historical custom closures display nicely.
    Unknown names (deleted reasons) fall back to the raw stored string at call site.
    """
    out = {v: custom_display() for v in (CUSTOM_VALUE, *LEGACY_CUSTOM_VALUES)}
    out.update({v: ai_display() for v in (AI_VALUE, *LEGACY_AI_VALUES)})
    for r in await list_all(session):
        out[r.name] = display_name(r)
    return out


async def create(session: AsyncSession, *, name: str, emoji: str | None) -> CloseReason:
    name = (name or "").strip()
    emoji = (emoji or "").strip() or None
    if not name:
        raise CloseReasonError(_t("v_name_empty"))
    if len(name) > 64:
        raise CloseReasonError(_t("v_name_too_long"))
    if _is_reserved(name):
        raise CloseReasonError(_t("cr_err_reserved"))
    existing = (await session.execute(
        select(CloseReason).where(func.lower(CloseReason.name) == name.lower())
    )).scalar_one_or_none()
    if existing is not None:
        raise CloseReasonError(_t("v_duplicate", name=name))
    max_order = (await session.execute(select(func.max(CloseReason.sort_order)))).scalar() or 0
    r = CloseReason(name=name, emoji=emoji, sort_order=int(max_order) + 1, is_active=True)
    session.add(r)
    await session.flush()
    logger.info(f"Close reason created: {name!r}")
    return r


async def update(session: AsyncSession, reason_id: int, *,
                 name: str | None = None, emoji: str | None = None,
                 is_active: bool | None = None) -> CloseReason:
    r = await session.get(CloseReason, reason_id)
    if r is None:
        raise CloseReasonError(_t("v_not_found", id=reason_id))
    if name is not None:
        name = name.strip()
        if not name or len(name) > 64:
            raise CloseReasonError(_t("v_name_length"))
        if _is_reserved(name):
            raise CloseReasonError(_t("cr_err_reserved"))
        dup = (await session.execute(
            select(CloseReason).where(
                func.lower(CloseReason.name) == name.lower(),
                CloseReason.id != reason_id,
            )
        )).scalar_one_or_none()
        if dup is not None:
            raise CloseReasonError(_t("v_duplicate", name=name))
        r.name = name
    if emoji is not None:
        r.emoji = emoji.strip() or None
    if is_active is not None:
        r.is_active = is_active
    await session.flush()
    return r


async def delete(session: AsyncSession, reason_id: int) -> None:
    r = await session.get(CloseReason, reason_id)
    if r is None:
        raise CloseReasonError(_t("v_not_found", id=reason_id))
    await session.delete(r)
    logger.info(f"Close reason #{reason_id} deleted")


async def move(session: AsyncSession, reason_id: int, direction: int) -> None:
    """Swap sort_order with the neighbour above (-1) or below (+1)."""
    r = await session.get(CloseReason, reason_id)
    if r is None:
        return
    rows = list(await list_all(session))
    idx = next((i for i, x in enumerate(rows) if x.id == reason_id), None)
    if idx is None:
        return
    swap = idx + direction
    if 0 <= swap < len(rows):
        r.sort_order, rows[swap].sort_order = rows[swap].sort_order, r.sort_order
        await session.flush()
