"""Managerial report queries вЂ” period-scoped, comparison-friendly.

Deliberately DISJOINT from the Overview page: Overview is the operational
"what's happening now" dashboard (snapshots, trends, heatmap). This module
answers the manager's question "how did the period go, and versus the
previous one?" вЂ” per-operator and per-team breakdowns based on ticket
assignment, plus the AI contribution for the period.

All ranges are [date_from, date_to) вЂ” from inclusive, to exclusive.
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


# ---- SQL fragments that differ per engine --------------------------------
#
# These queries are raw strings, so the dialect layer hands back SQL rather
# than SQLAlchemy objects. Every argument is a column name written here; none
# of it comes from a request.

def _sql_minutes(later: str, earlier: str) -> str:
    from src.database.db import settings as _s
    from src.database.dialect import minutes_between

    return minutes_between(later, earlier, url=_s.database_url)


def _sql_seconds(later: str, earlier: str) -> str:
    from src.database.db import settings as _s
    from src.database.dialect import seconds_between

    return seconds_between(later, earlier, url=_s.database_url)


def _sql_hours(later: str, earlier: str) -> str:
    from src.database.db import settings as _s
    from src.database.dialect import hours_between

    return hours_between(later, earlier, url=_s.database_url)


def _sql_day(column: str) -> str:
    from src.database.db import settings as _s
    from src.database.dialect import day_of

    return day_of(column, url=_s.database_url)


def _sql_weekday(column: str) -> str:
    from src.database.db import settings as _s
    from src.database.dialect import weekday_of

    return weekday_of(column, url=_s.database_url)


def _sql_hour(column: str) -> str:
    from src.database.db import settings as _s
    from src.database.dialect import hour_of

    return hour_of(column, url=_s.database_url)


def _sql_json_array(column: str) -> str:
    """A JSON array expanded into rows, as `je(value)`.

    SQLite calls it json_each; PostgreSQL calls the text-producing form
    jsonb_array_elements_text and wants the column cast, because the ORM
    writes these as `json` rather than `jsonb`. Both yield one row per
    element with the column named `value`, which is what the query joins on.
    """
    from src.database.db import settings as _s
    from src.database.dialect import is_postgres

    if is_postgres(_s.database_url):
        return f"jsonb_array_elements_text(CAST({column} AS jsonb))"
    return f"json_each({column})"


def _sql_text(expression: str) -> str:
    from src.database.db import settings as _s
    from src.database.dialect import as_text

    return as_text(expression, url=_s.database_url)



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

        # Avg first response (minutes) вЂ” tickets CREATED in the period
        avg_first_min = (await session.execute(text(f"""
            SELECT AVG({_sql_minutes('fa.first_at', 't.created_at')})
            FROM tickets t
            JOIN (
                SELECT ticket_id, MIN(COALESCE(delivered_at, created_at)) AS first_at
                FROM messages WHERE sender_type = 'admin' AND is_internal = false AND delivery_status = 'delivered'
                GROUP BY ticket_id
            ) fa ON fa.ticket_id = t.id
            WHERE t.created_at >= :df AND t.created_at < :dt
        """), params)).scalar_one()

        # Resolution ends at the actual close timestamp, including waiting time.
        avg_close_hours = (await session.execute(text(f"""
            SELECT AVG({_sql_hours('closed_at', 'created_at')}) FROM tickets
            WHERE closed_at >= :df AND closed_at < :dt AND closed_at >= created_at
        """), params)).scalar_one()

        # CSAT вЂ” ratings GIVEN in the period
        csat_avg, csat_count = (await session.execute(text("""
            SELECT AVG(csat_rating), COUNT(csat_rating)
            FROM tickets
            WHERE csat_rated_at >= :df AND csat_rated_at < :dt
        """), params)).one()

        # AI contribution. admin_categories is a JSON array; the ORM stores it
        # with \uXXXX escapes (json.dumps default), so a naive LIKE on the raw
        # text never matches non-ASCII вЂ” json_each() decodes properly. Legacy
        # markers are matched too, so reports over old data stay correct.
        ai_placeholders = ", ".join(f":r{i}" for i in range(len(_AI_REASONS)))
        ai_closed = (await session.execute(text(f"""
            SELECT COUNT(t.id) FROM tickets t
            WHERE t.closed_at >= :df AND t.closed_at < :dt
              AND t.admin_categories IS NOT NULL
              AND EXISTS (
                  SELECT 1 FROM {_sql_json_array('t.admin_categories')} je
                  WHERE je.value IN ({ai_placeholders})
              )
        """), {**params, **{f"r{i}": v for i, v in enumerate(_AI_REASONS)}})).scalar_one()

        deflect_offered, deflect_accepted = (await session.execute(text("""
            SELECT
                SUM(CASE WHEN success = true THEN 1 ELSE 0 END),
                SUM(CASE WHEN accepted = true THEN 1 ELSE 0 END)
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
    silent operators are omitted вЂ” an empty report row is noise, not signal.
    """
    params = {"df": date_from, "dt": date_to}
    async with db_session() as session:
        # The aggregates are named in a subquery and filtered outside it:
        # PostgreSQL will not let HAVING refer to a SELECT alias, and writing
        # each CASE twice is how two copies drift apart.
        rows = (await session.execute(text(f"""
            SELECT * FROM (
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
                         THEN {_sql_hours('t.closed_at', 't.created_at')} END)
                                                                AS avg_close_hours,
                SUM(CASE WHEN t.status = 'active' THEN 1 ELSE 0 END) AS active_now
            FROM admin_users a
            LEFT JOIN tickets t ON t.assigned_admin_id = a.id
            GROUP BY a.id
            ) per_operator
            WHERE closed_cnt > 0 OR active_now > 0
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
            SELECT * FROM (
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
            ) per_team
            WHERE created_cnt > 0 OR closed_cnt > 0
            ORDER BY created_cnt DESC, name ASC
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
    - **Assisted**: an operator accepted a draft. Only the writing is saved вЂ”
      not the reading, the diagnosis or the follow-up вЂ” so it is counted at a
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

        # Accepted suggestions that were NOT the deflection path вЂ” deflection is
        # already counted as a whole ticket, and counting it twice is the
        # easiest way to produce a number nobody believes.
        assisted = (await session.execute(text("""
            SELECT COUNT(*) FROM ai_calls
            WHERE accepted = true AND use_case != 'deflect'
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
