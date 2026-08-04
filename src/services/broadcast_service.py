"""Send one message to many users.

Two things the bot's version got wrong and this one does not:

1. It sent in a tight loop. Telegram throttles bots at roughly 30 messages a
   second and starts returning 429; a broadcast to a few hundred people would
   silently lose the tail. Here sends are paced.
2. It ran inside the callback handler, so a large broadcast blocked the caller
   with no way to tell whether it finished. Here the send runs as a background
   task and reports progress the page can read.

Delivery is best-effort by nature: a user who blocked the bot cannot be
reached, and that is not an error worth aborting the run for. Every outcome is
counted so the operator sees exactly who did not get it.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Optional

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramNotFound, TelegramRetryAfter
from sqlalchemy import func, select

from src.database.db import db_session
from src.database.models import Team, User

logger = logging.getLogger(__name__)

Audience = Literal["all", "team"]

# Telegram allows ~30 messages/second to different chats. Stay well under it:
# a broadcast is never urgent enough to be worth a 429 and a lost tail.
_SEND_INTERVAL_SEC = 0.05          # 20/sec
_MAX_TEXT_CHARS = 4096             # Telegram's own limit


@dataclass
class BroadcastResult:
    """Outcome of one run. Kept in memory — it is a progress readout, not a record."""
    started_at: datetime
    audience: str
    team_name: Optional[str]
    total: int
    sent: int = 0
    blocked: int = 0            # user blocked the bot
    not_found: int = 0          # chat no longer exists
    failed: int = 0             # anything else
    finished_at: Optional[datetime] = None
    failures: list[str] = field(default_factory=list)   # labels, capped

    @property
    def is_running(self) -> bool:
        return self.finished_at is None

    @property
    def processed(self) -> int:
        return self.sent + self.blocked + self.not_found + self.failed


# Last run, so the page can show progress. Resets when the process restarts —
# acceptable: this is a status readout, and the send itself is already done.
_last: Optional[BroadcastResult] = None


def last_result() -> Optional[BroadcastResult]:
    return _last


def is_running() -> bool:
    return _last is not None and _last.is_running


async def _recipients(session, audience: Audience, team_id: int | None):
    """Verified, active users in scope. Blocked users are excluded on purpose —
    someone you blocked should not get an announcement."""
    stmt = select(User).where(User.is_verified.is_(True), User.is_active.is_(True))
    if audience == "team":
        stmt = stmt.where(User.team_id == team_id)
    return (await session.execute(stmt)).scalars().all()


async def count_recipients(audience: Audience, team_id: int | None = None) -> int:
    """How many people a broadcast would reach — shown before sending."""
    async with db_session() as session:
        stmt = select(func.count(User.id)).where(
            User.is_verified.is_(True), User.is_active.is_(True)
        )
        if audience == "team":
            stmt = stmt.where(User.team_id == team_id)
        return int((await session.execute(stmt)).scalar_one() or 0)


async def start(
    bot: Bot, *, text: str, audience: Audience, team_id: int | None = None,
) -> tuple[bool, str]:
    """Kick off a broadcast in the background.

    Returns (ok, error_key). The error key is an i18n key, not a message —
    the route decides how to show it.
    """
    global _last

    text = (text or "").strip()
    if not text:
        return False, "e_empty_message"
    if len(text) > _MAX_TEXT_CHARS:
        return False, "e_broadcast_too_long"
    if audience not in ("all", "team"):
        return False, "e_broadcast_bad_audience"
    if audience == "team" and not team_id:
        return False, "e_broadcast_no_team"
    if is_running():
        return False, "e_broadcast_running"

    team_name = None
    async with db_session() as session:
        if audience == "team":
            team = await session.get(Team, team_id)
            if team is None:
                return False, "e_team_not_found_id"
            team_name = team.name
        users = await _recipients(session, audience, team_id)
        targets = [
            (u.tg_id, f"@{u.username}" if u.username else f"id {u.tg_id}")
            for u in users if u.tg_id
        ]

    if not targets:
        return False, "e_broadcast_no_recipients"

    _last = BroadcastResult(
        started_at=datetime.utcnow(),
        audience=audience,
        team_name=team_name,
        total=len(targets),
    )
    asyncio.create_task(_run(bot, text, targets, _last))
    logger.info(f"Broadcast started: {len(targets)} recipients, audience={audience}")
    return True, ""


async def _run(bot: Bot, text: str, targets: list[tuple[int, str]], result: BroadcastResult) -> None:
    for tg_id, label in targets:
        try:
            await bot.send_message(chat_id=tg_id, text=text)
            result.sent += 1
        except TelegramRetryAfter as e:
            # Telegram told us exactly how long to wait — obey, then retry once.
            await asyncio.sleep(e.retry_after + 1)
            try:
                await bot.send_message(chat_id=tg_id, text=text)
                result.sent += 1
            except Exception:
                result.failed += 1
                _note(result, label, "retry failed")
        except TelegramForbiddenError:
            result.blocked += 1
            _note(result, label, "blocked the bot")
        except TelegramNotFound:
            result.not_found += 1
            _note(result, label, "chat not found")
        except Exception as e:  # noqa: BLE001 — one bad chat must not stop the run
            result.failed += 1
            _note(result, label, type(e).__name__)
        await asyncio.sleep(_SEND_INTERVAL_SEC)

    result.finished_at = datetime.utcnow()
    logger.info(
        f"Broadcast finished: sent={result.sent} blocked={result.blocked} "
        f"not_found={result.not_found} failed={result.failed}"
    )


def _note(result: BroadcastResult, label: str, reason: str) -> None:
    """Keep a short sample of who did not get it — enough to act on, not a dump."""
    if len(result.failures) < 20:
        result.failures.append(f"{label} — {reason}")
