"""SLA policies: what happens when a customer has waited too long.

The clock already existed — the board colours a card amber past the response
target and red past twice it. But a colour only works on someone who is
looking, and the person who should be looking is by definition busy with a
different ticket. A policy acts without being watched.

Two actions, both opt-in:

- **Notify** the operators' chat once, when a ticket first crosses the breach
  line. Once, not every check — an alert that repeats every five minutes is an
  alert people mute, and a muted alert is worse than none.
- **Return to the pool** — clear the assignment so the ticket goes back to
  everyone's queue. On a small team that is what escalation actually means:
  the person who claimed it has not got to it, so stop hiding it from the
  others.

Everything runs on working time, so a ticket that arrived at 18:05 does not
escalate overnight to nobody.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import text

from src.database.db import db_session
from src.services import settings_service, work_hours_service

logger = logging.getLogger(__name__)

KEY_NOTIFY = "sla_notify_enabled"      # "1"/"0" — post to the operators' chat
KEY_RETURN = "sla_return_to_pool"      # "1"/"0" — clear the assignment on breach


async def get_policy() -> dict:
    return {
        "notify": (await settings_service.get_value(KEY_NOTIFY, "0")) == "1",
        "return_to_pool": (await settings_service.get_value(KEY_RETURN, "0")) == "1",
    }


async def set_policy(*, notify: bool, return_to_pool: bool, updated_by: int) -> None:
    await settings_service.set_value(KEY_NOTIFY, "1" if notify else "0",
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
    breach_seconds = sla_minutes * 60 * 2   # same line the board paints red

    async with db_session() as session:
        rows = (await session.execute(text("""
            SELECT t.id, t.assigned_admin_id, t.category, u.username, u.tg_id,
                   (SELECT MIN(m.created_at) FROM messages m
                     WHERE m.ticket_id = t.id
                       AND m.is_internal = 0
                       AND m.sender_type = 'user'
                       AND m.id > COALESCE((SELECT MAX(a.id) FROM messages a
                                             WHERE a.ticket_id = t.id
                                               AND a.is_internal = 0
                                               AND a.sender_type = 'admin'), 0)
                   ) AS waiting_since
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


async def clear_on_reply(ticket_id: int) -> None:
    """An answered ticket starts clean: if the customer writes again and is
    made to wait again, that is a new breach and deserves to be announced."""
    async with db_session() as session:
        await session.execute(
            text("UPDATE tickets SET sla_breached_at = NULL WHERE id = :i"),
            {"i": ticket_id},
        )
        await session.commit()


async def run_once(bot=None) -> dict:
    """One pass of the policy. Returns what it did, for logs and for tests."""
    policy = await get_policy()
    if not policy["notify"] and not policy["return_to_pool"]:
        return {"breached": 0, "notified": 0, "returned": 0}

    breaches = await find_new_breaches()
    if not breaches:
        return {"breached": 0, "notified": 0, "returned": 0}

    ids = [b["id"] for b in breaches]
    notified = 0

    if policy["notify"] and bot is not None:
        from src.config import load_settings
        from src.dashboard.formatting import fmt_seconds
        from src.dashboard.i18n import t as _op

        chat_id = load_settings().admin_chat_id
        for b in breaches:
            try:
                await bot.send_message(
                    chat_id=chat_id,
                    text=_op("sla_breach_alert",
                             id=b["id"], who=b["user_label"],
                             waited=fmt_seconds(b["waited_seconds"])),
                )
                notified += 1
            except Exception as e:  # noqa: BLE001 — one failure must not stop the rest
                logger.warning(f"SLA alert for #{b['id']} not delivered: {e}")

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
