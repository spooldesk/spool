"""Response SLA escalation: optionally return an overdue ticket to the pool.

Runs on working time. Operator chat alerts have been retired.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import text

from src.database.db import db_session
from src.services import settings_service, work_hours_service
from src.services.response_clock import WAITING_SINCE_SQL

logger = logging.getLogger(__name__)

KEY_NOTIFY = "sla_notify_enabled"      # Retired setting, always ignored/reset.
KEY_RETURN = "sla_return_to_pool"      # "1"/"0" — clear the assignment on breach


async def get_policy() -> dict:
    return {
        "notify": False,  # Retired: operator alerts belong in the dashboard.
        "return_to_pool": (await settings_service.get_value(KEY_RETURN, "0")) == "1",
    }


async def set_policy(*, notify: bool, return_to_pool: bool, updated_by: int) -> None:
    await settings_service.set_value(KEY_NOTIFY, "0",
                                     updated_by=updated_by)
    await settings_service.set_value(KEY_RETURN, "1" if return_to_pool else "0",
                                     updated_by=updated_by)


async def find_new_breaches() -> list[dict]:
    """Tickets that have just crossed the breach line and not been acted on.

    `sla_breached_at` is the record of having acted, which is what keeps this
    from firing on every pass. Reading it is also how a restart avoids
    re-announcing everything that was already handled.
    """
    sla_minutes = await work_hours_service.get_sla_minutes()
    config = await work_hours_service.get_config()
    now = datetime.utcnow().replace(tzinfo=timezone.utc)
    breach_seconds = sla_minutes * 60   # the configured response deadline

    async with db_session() as session:
        rows = (await session.execute(text(f"""
            SELECT t.id, t.assigned_admin_id, t.category, u.username, u.tg_id,
                   {WAITING_SINCE_SQL} AS waiting_since
              FROM tickets t
              LEFT JOIN users u ON u.id = t.user_id
             WHERE t.status = 'active'
               AND t.sla_breached_at IS NULL
        """))).all()

    breached = []
    for ticket_id, assigned, category, username, tg_id, since in rows:
        if since is None:
            continue
        if not isinstance(since, datetime):
            since = datetime.fromisoformat(str(since))
        waited = work_hours_service.working_seconds_between(
            config, since.replace(tzinfo=timezone.utc), now,
        )
        if waited < breach_seconds:
            continue
        breached.append({
            "id": int(ticket_id),
            "assigned_admin_id": assigned,
            "category": category or "",
            "user_label": f"@{username}" if username else f"id:{tg_id}",
            "waited_seconds": waited,
        })
    return breached


async def mark_handled(ticket_ids: list[int]) -> None:
    """Record that the policy has run for these, so it does not run again."""
    if not ticket_ids:
        return
    async with db_session() as session:
        await session.execute(
            text("UPDATE tickets SET sla_breached_at = :now WHERE id IN :ids")
            .bindparams(__import__("sqlalchemy").bindparam("ids", expanding=True)),
            {"now": datetime.utcnow(), "ids": ticket_ids},
        )
        await session.commit()


async def return_to_pool(ticket_ids: list[int]) -> None:
    """Drop the assignment so the ticket is offered to everyone again."""
    if not ticket_ids:
        return
    async with db_session() as session:
        await session.execute(
            text("UPDATE tickets SET assigned_admin_id = NULL WHERE id IN :ids")
            .bindparams(__import__("sqlalchemy").bindparam("ids", expanding=True)),
            {"ids": ticket_ids},
        )
        await session.commit()


async def _run_once(bot=None) -> dict:
    """One pass of the policy. Returns what it did, for logs and for tests."""
    policy = await get_policy()
    if not policy["notify"] and not policy["return_to_pool"]:
        return {"breached": 0, "notified": 0, "returned": 0}

    breaches = await find_new_breaches()
    if not breaches:
        return {"breached": 0, "notified": 0, "returned": 0}

    ids = [b["id"] for b in breaches]
    notified = 0
    returned = 0
    if policy["return_to_pool"]:
        claimed = [b["id"] for b in breaches if b["assigned_admin_id"]]
        await return_to_pool(claimed)
        returned = len(claimed)

    # Marked last: a crash before this point means the next pass tries again,
    # which is the failure worth having. The opposite loses the alert forever.
    await mark_handled(ids)

    logger.info(f"SLA: {len(ids)} breached, {notified} notified, {returned} returned")
    return {"breached": len(ids), "notified": notified, "returned": returned}


async def run_once(bot=None) -> dict:
    """One bounded pass, exclusive across dashboard workers."""
    import asyncio
    import secrets
    from src.services import worker_lease
    owner = secrets.token_hex(16)
    if not await worker_lease.acquire("sla", owner, seconds=300):
        return {"breached": 0, "notified": 0, "returned": 0}
    try:
        async with asyncio.timeout(240):
            return await _run_once(bot)
    finally:
        await worker_lease.release("sla", owner)


async def poll_loop(bot=None) -> None:
    import asyncio
    while True:
        try:
            # Escalation is local and does not depend on any messaging channel.
            await run_once()
        except Exception:
            logger.exception("SLA policy pass failed")
        await asyncio.sleep(300)
