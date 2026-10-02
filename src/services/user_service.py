"""User management logic — block/unblock, team (re)assignment, account linking.

Extracted from the bot's admin_ui handlers so both the bot AND the web admin
can reuse it. Functions are pure DB logic (no Telegram sends); callers do any
notification via their own Bot instance. Caller commits the session.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.database.models import Team, Ticket, User

logger = logging.getLogger(__name__)


class UserServiceError(Exception):
    """Validation / not-found error for a user operation."""
def _t(key: str, **kwargs) -> str:
    from src.dashboard.i18n import t as dash_t
    return dash_t(key, **kwargs)


@dataclass(slots=True)
class TeamChangeResult:
    team_name: str
    local_user_id: int
    is_change: bool          # True = moved from another team, False = first assignment
    user_tg_id: int
    # The user's own language — the approval notice goes to them, not to the
    # operator, so it must not follow the dashboard's UI language.
    user_language: str | None = None


@dataclass(slots=True)
class LinkResult:
    status: str              # 'linked' | 'pending' | 'error'
    message: str
    work_label: Optional[str] = None


# ==================== Block / unblock ====================


async def set_active(session: AsyncSession, user_id: int, active: bool) -> tuple[int, str | None]:
    """Block (active=False) or unblock (active=True) a user.

    Returns (tg_id, language) so the caller can notify the user in their language.
    """
    user = await session.get(User, user_id)
    if user is None:
        raise UserServiceError(_t("e_user_not_found_id", id=user_id))
    user.is_active = active
    logger.info(f"User #{user_id} {'unblocked' if active else 'blocked'}")
    return user.tg_id, user.language


@dataclass(slots=True)
class RemoveResult:
    tickets_closed: int
    personals_detached: int


async def remove(session: AsyncSession, user_id: int) -> RemoveResult:
    """Retire a user: close their open tickets and revoke their access.

    Deliberately not a DELETE. Their tickets are history — the reports and the
    operator stats they contribute to have to keep adding up, so the row stays
    and access is revoked on every channel.

    Account links are cut in BOTH directions. Leaving a personal account
    pointing at a retired work account is what produces "writes from personal"
    badges referring to somebody who no longer exists.
    """
    from src.database.db import acquire_write_lock
    await acquire_write_lock(session)
    user = await session.get(User, user_id)
    if user is None:
        raise UserServiceError(_t("e_user_not_found_id", id=user_id))

    open_tickets = (await session.execute(
        select(Ticket).where(Ticket.user_id == user.id, Ticket.status == "active")
    )).scalars().all()
    for ticket in open_tickets:
        ticket.status = "closed"
        ticket.closed_at = datetime.utcnow()

    personals = (await session.execute(
        select(User).where(User.linked_to_user_id == user.id)
    )).scalars().all()
    for p in personals:
        p.linked_to_user_id = None

    user.linked_to_user_id = None
    user.pending_work_username = None
    user.is_verified = False
    user.is_active = False
    user.identity_verified = False
    user.identity_verified_fields = []

    logger.info(
        f"User #{user_id} removed: closed {len(open_tickets)} tickets, "
        f"detached {len(personals)} personal accounts"
    )
    return RemoveResult(len(open_tickets), len(personals))


# ==================== Team (re)assignment ====================


async def change_team(session: AsyncSession, user_id: int, team_id: int) -> TeamChangeResult:
    """Assign or move a user to a team. Issues a fresh local_user_id in the
    target team (the old one belonged to the previous team's numbering)."""
    user = await session.get(User, user_id)
    if user is None:
        raise UserServiceError(_t("e_user_not_found_id", id=user_id))
    team = await session.get(Team, team_id)
    if team is None:
        raise UserServiceError(_t("e_team_not_found_id", id=team_id))
    if user.team_id == team.id:
        raise UserServiceError(_t("e_already_in_team"))

    is_change = user.team_id is not None
    last_local = (await session.execute(
        select(User.local_user_id)
        .where(User.team_id == team.id)
        .order_by(User.local_user_id.desc())
        .limit(1)
    )).scalar_one_or_none()
    next_local = (last_local or 0) + 1

    user.team_id = team.id
    user.local_user_id = next_local
    logger.info(f"User #{user_id} → team {team.name} (#{next_local}), change={is_change}")
    return TeamChangeResult(team.name, next_local, is_change, user.tg_id, user.language)


# ==================== Account linking (personal ↔ work) ====================


async def link_account(session: AsyncSession, personal_id: int, identifier: str) -> LinkResult:
    """Link a personal account to a work account by @username or tg_id.

    If the work account exists → immediate link. If not → store as pending
    (auto-links when the work account first writes to the bot).
    """
    personal = await session.get(User, personal_id)
    if personal is None:
        return LinkResult("error", _t("e_personal_not_found"))

    raw = (identifier or "").strip().lstrip("@")
    if not raw:
        return LinkResult("error", _t("e_need_identifier"))

    work = None
    if raw.isdigit():
        work = (await session.execute(select(User).where(User.tg_id == int(raw)))).scalar_one_or_none()
    if work is None:
        work = (await session.execute(select(User).where(User.username == raw))).scalar_one_or_none()

    if work is not None:
        if work.id == personal_id:
            return LinkResult("error", _t("e_cannot_link_self"))
        personal.linked_to_user_id = work.id
        personal.pending_work_username = None
        label = f"@{work.username}" if work.username else f"id {work.tg_id}"
        logger.info(f"Linked personal #{personal_id} → work #{work.id}")
        return LinkResult("linked", _t("ok_linked_to", label=label), work_label=label)

    # Work account not in the bot yet → pending
    personal.linked_to_user_id = None
    personal.pending_work_username = raw
    logger.info(f"Personal #{personal_id} pending-linked to {raw!r}")
    return LinkResult("pending", _t("ok_link_pending", label=raw))


async def unlink_account(session: AsyncSession, user_id: int) -> None:
    """Clear both the active link and any pending target on this user."""
    user = await session.get(User, user_id)
    if user is None:
        raise UserServiceError(_t("e_user_not_found_id", id=user_id))
    user.linked_to_user_id = None
    user.pending_work_username = None
    logger.info(f"Unlinked user #{user_id}")


async def unlink_personals_of(session: AsyncSession, work_id: int) -> int:
    """Detach every personal account pointing at this work account. Returns count."""
    personals = (await session.execute(
        select(User).where(User.linked_to_user_id == work_id)
    )).scalars().all()
    for p in personals:
        p.linked_to_user_id = None
    return len(personals)
