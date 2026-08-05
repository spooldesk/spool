"""Managerial report queries — period-scoped, comparison-friendly.

Deliberately DISJOINT from the Overview page: Overview is the operational
"what's happening now" dashboard (snapshots, trends, heatmap). This module
answers the manager's question "how did the period go, and versus the
previous one?" — per-operator and per-team breakdowns based on ticket
assignment, plus the AI contribution for the period.

All ranges are [date_from, date_to) — from inclusive, to exclusive.
"""

from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import text

from src.dashboard.queries import _MEANINGFUL_MSG_FILTER
from src.database.db import db_session

logger = logging.getLogger(__name__)

# Snapshot closure-reason markers written by the AI deflection flow. Read from
# the service rather than redeclared: a duplicate literal here silently zeroed
# the "closed by AI" number whenever the other copy changed.
from src.services.close_reason_service import AI_VALUE, LEGACY_AI_VALUES  # noqa: E402

_AI_REASONS = (AI_VALUE, *LEGACY_AI_VALUES)


async def report_summary(date_from: datetime, date_to: datetime) -> dict:
    """Headline numbers for the period."""
    params = {"df": date_from, "dt": date_to}
    async with db_session() as session:
        created = (await session.execute(text(
            "SELECT COUNT(id) FROM tickets WHERE created_at >= :df AND created_at < :dt"
        ), params)).scalar_one()

        closed = (await session.execute(text(
            "SELECT COUNT(id) FROM tickets WHERE closed_at >= :df AND closed_at < :dt"
        ), params)).scalar_one()

        # Avg first response (minutes) — tickets CREATED in the period
        avg_first_min = (await session.execute(text("""
            SELECT AVG((julianday(fa.first_at) - julianday(t.created_at)) * 24 * 60)
            FROM tickets t
            JOIN (
                SELECT ticket_id, MIN(created_at) AS first_at
                FROM messages WHERE sender_type = 'admin' AND is_internal = 0
                GROUP BY ticket_id
            ) fa ON fa.ticket_id = t.id
            WHERE t.created_at >= :df AND t.created_at < :dt
        """), params)).scalar_one()

        # Avg resolution (hours) — tickets CLOSED in the period, measured to the
        # last meaningful message (same semantics as the Overview KPI).
        avg_close_hours = (await session.execute(text(f"""
            SELECT AVG(sec) / 3600 FROM (
                SELECT (julianday(MAX(m.created_at)) - julianday(t.created_at)) * 24 * 3600 AS sec
                FROM tickets t
                JOIN messages m ON m.ticket_id = t.id
                WHERE t.closed_at >= :df AND t.closed_at < :dt
                  AND {_MEANINGFUL_MSG_FILTER}
                GROUP BY t.id
            )
        """), params)).scalar_one()

        # CSAT — ratings GIVEN in the period
        csat_avg, csat_count = (await session.execute(text("""
            SELECT AVG(csat_rating), COUNT(csat_rating)
            FROM tickets
            WHERE csat_rated_at >= :df AND csat_rated_at < :dt
        """), params)).one()

        # AI contribution. admin_categories is a JSON array; the ORM stores it
        # with \uXXXX escapes (json.dumps default), so a naive LIKE on the raw
        # text never matches non-ASCII — json_each() decodes properly. Legacy
        # markers are matched too, so reports over old data stay correct.
        ai_placeholders = ", ".join(f":r{i}" for i in range(len(_AI_REASONS)))
        ai_closed = (await session.execute(text(f"""
            SELECT COUNT(t.id) FROM tickets t
            WHERE t.closed_at >= :df AND t.closed_at < :dt
              AND t.admin_categories IS NOT NULL
              AND EXISTS (
                  SELECT 1 FROM json_each(t.admin_categories) je
                  WHERE je.value IN ({ai_placeholders})
              )
        """), {**params, **{f"r{i}": v for i, v in enumerate(_AI_REASONS)}})).scalar_one()

        deflect_offered, deflect_accepted = (await session.execute(text("""
            SELECT
                SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END),
                SUM(CASE WHEN accepted = 1 THEN 1 ELSE 0 END)
            FROM ai_calls
            WHERE use_case = 'deflect' AND created_at >= :df AND created_at < :dt
        """), params)).one()

    closed_i = int(closed or 0)
    ai_closed_i = int(ai_closed or 0)
    return {
        "created": int(created or 0),
        "closed": closed_i,
        "avg_first_response_min": float(avg_first_min) if avg_first_min is not None else None,
        "avg_close_hours": float(avg_close_hours) if avg_close_hours is not None else None,
        "csat_avg": float(csat_avg) if csat_avg is not None else None,
        "csat_count": int(csat_count or 0),
        "ai_closed": ai_closed_i,
        "ai_closed_pct": (ai_closed_i / closed_i * 100) if closed_i else None,
        "deflect_offered": int(deflect_offered or 0),
        "deflect_accepted": int(deflect_accepted or 0),
    }


async def report_by_operator(date_from: datetime, date_to: datetime) -> list[dict]:
    """Assignment-based operator performance for the period.

    Rows for operators with any activity (closed in period or active now);
    silent operators are omitted — an empty report row is noise, not signal.
    """
    params = {"df": date_from, "dt": date_to}
    async with db_session() as session:
        rows = (await session.execute(text("""
            SELECT
                a.id,
                COALESCE(NULLIF(a.name, ''), a.email)          AS label,
                a.is_active                                     AS acc_active,
                SUM(CASE WHEN t.closed_at >= :df AND t.closed_at < :dt
                         THEN 1 ELSE 0 END)                     AS closed_cnt,
                AVG(CASE WHEN t.closed_at >= :df AND t.closed_at < :dt
                         THEN t.csat_rating END)                AS csat_avg,
                SUM(CASE WHEN t.closed_at >= :df AND t.closed_at < :dt
                          AND t.csat_rating IS NOT NULL
                         THEN 1 ELSE 0 END)                     AS csat_cnt,
                AVG(CASE WHEN t.closed_at >= :df AND t.closed_at < :dt
                         THEN (julianday(t.closed_at) - julianday(t.created_at)) * 24 END)
                                                                AS avg_close_hours,
                SUM(CASE WHEN t.status = 'active' THEN 1 ELSE 0 END) AS active_now
            FROM admin_users a
            LEFT JOIN tickets t ON t.assigned_admin_id = a.id
            GROUP BY a.id
            HAVING closed_cnt > 0 OR active_now > 0
            ORDER BY closed_cnt DESC, label ASC
        """), params)).all()

    return [{
        "id": int(r[0]),
        "label": r[1],
        "account_active": bool(r[2]),
        "closed": int(r[3] or 0),
        "csat_avg": float(r[4]) if r[4] is not None else None,
        "csat_count": int(r[5] or 0),
        "avg_close_hours": float(r[6]) if r[6] is not None else None,
        "active_now": int(r[7] or 0),
    } for r in rows]


async def report_by_team(date_from: datetime, date_to: datetime) -> list[dict]:
    """Per-team volume + satisfaction for the period."""
    params = {"df": date_from, "dt": date_to}
    async with db_session() as session:
        rows = (await session.execute(text("""
            SELECT
                tm.id,
                tm.name,
                SUM(CASE WHEN t.created_at >= :df AND t.created_at < :dt
                         THEN 1 ELSE 0 END)                     AS created_cnt,
                SUM(CASE WHEN t.closed_at >= :df AND t.closed_at < :dt
                         THEN 1 ELSE 0 END)                     AS closed_cnt,
                AVG(CASE WHEN t.closed_at >= :df AND t.closed_at < :dt
                         THEN t.csat_rating END)                AS csat_avg
            FROM teams tm
            LEFT JOIN tickets t ON t.team_id = tm.id
            GROUP BY tm.id
            HAVING created_cnt > 0 OR closed_cnt > 0
            ORDER BY created_cnt DESC, tm.name ASC
        """), params)).all()

    return [{
        "id": int(r[0]),
        "name": r[1],
        "created": int(r[2] or 0),
        "closed": int(r[3] or 0),
        "csat_avg": float(r[4]) if r[4] is not None else None,
    } for r in rows]


async def ai_savings(date_from: datetime, date_to: datetime) -> dict:
    """What the AI cost and what it saved, in a form that survives a manager.

    Two savings that must never be added into one number, because they are not
    the same claim:

    - **Deflected**: the AI answered and the ticket closed without an operator
      opening it. The whole handling time is saved. Defensible.
    - **Assisted**: an operator accepted a draft. Only the writing is saved —
      not the reading, the diagnosis or the follow-up — so it is counted at a
      configurable few minutes, not at a full ticket.

    Reporting only the second as if it were the first is how these numbers get
    a bad name. The report also carries the quality delta, because a saving
    that costs half a CSAT point is a decision, not a win, and the person
    reading it is entitled to make that decision themselves.
    """
    from src.services import settings_service

    price_in, price_out = await settings_service.get_ai_pricing()
    assist_minutes = await settings_service.get_ai_assist_minutes()

    summary = await report_summary(date_from, date_to)
    params = {"df": date_from, "dt": date_to}

    async with db_session() as session:
        tokens_in, tokens_out, calls = (await session.execute(text("""
            SELECT COALESCE(SUM(tokens_in), 0), COALESCE(SUM(tokens_out), 0), COUNT(*)
            FROM ai_calls
            WHERE created_at >= :df AND created_at < :dt
        """), params)).one()

        # Accepted suggestions that were NOT the deflection path — deflection is
        # already counted as a whole ticket, and counting it twice is the
        # easiest way to produce a number nobody believes.
        assisted = (await session.execute(text("""
            SELECT COUNT(*) FROM ai_calls
            WHERE accepted = 1 AND use_case != 'deflect'
              AND created_at >= :df AND created_at < :dt
        """), params)).scalar_one()

    deflected = summary["ai_closed"]
    assisted = int(assisted or 0)
    handle_hours = summary["avg_close_hours"]

    # No measured handling time means no honest hours figure. Say so rather
    # than substituting an industry average nobody here has verified.
    deflected_hours = deflected * handle_hours if handle_hours is not None else None
    assisted_hours = assisted * assist_minutes / 60
    total_hours = (deflected_hours + assisted_hours) if deflected_hours is not None else None

    cost = (int(tokens_in) / 1_000_000 * price_in) + (int(tokens_out) / 1_000_000 * price_out)

    return {
        "calls": int(calls or 0),
        "tokens_in": int(tokens_in or 0),
        "tokens_out": int(tokens_out or 0),
        "cost_usd": round(cost, 2),

        "deflected": deflected,
        "deflected_hours": round(deflected_hours, 1) if deflected_hours is not None else None,
        "assisted": assisted,
        "assisted_hours": round(assisted_hours, 1),
        "total_hours": round(total_hours, 1) if total_hours is not None else None,

        # The assumptions, returned so the page can print them next to the
        # result instead of leaving the reader to trust it.
        "price_in": price_in,
        "price_out": price_out,
        "assist_minutes": assist_minutes,
        "avg_handle_hours": round(handle_hours, 2) if handle_hours is not None else None,
    }
