"""All dashboard read queries in one place.

Everything is read-only against the same `support.db` the bot writes to.
WAL mode (set in init_db) lets us read without blocking the bot's writes.

Functions return plain Python structures (lists/dicts/tuples) so route
handlers can hand them straight to Jinja or json.dumps.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import bindparam, exists, func, or_, select, text

from sqlalchemy.orm import selectinload

from src.database.db import db_session
from src.database.models import (
    AdminUser,
    AICall,
    Message as MessageRecord,
    ReplyTemplate,
    Team,
    Ticket,
    User,
)
from src.channels import DEFAULT as CHANNEL_DEFAULT
from src.channels.identity import channel_of, display_address
from src.dashboard.formatting import EMPTY
from src.dashboard.i18n import t as _dash_t
from src.services import close_reason_service, message_service, work_hours_service
from src.services.workflow_service import board_column
from src.utils.i18n import CLOSURE_SYSTEM_TEXT

logger = logging.getLogger(__name__)


# Categories whose ticket bodies must NEVER be displayed in the dashboard
# (they contain credentials). Only the header/metadata is shown.
#
# Currently EMPTY: the 1Pass/Gmail/Telegram category was opened up because creds
# are shared via the team's own secret-link tool (no plaintext credentials ever
# appear in messages), so there's nothing to mask. Re-add fragments here if a
# truly sensitive category is introduced later.
_SENSITIVE_CATEGORY_FRAGMENTS: tuple[str, ...] = ()


def is_sensitive_category(category: str | None) -> bool:
    """True if the ticket category should be masked in the dashboard.

    Kept as a function (not removed) so masking can be re-enabled by simply
    repopulating _SENSITIVE_CATEGORY_FRAGMENTS, without touching call sites.
    """
    if not category or not _SENSITIVE_CATEGORY_FRAGMENTS:
        return False
    low = category.lower()
    return any(frag in low for frag in _SENSITIVE_CATEGORY_FRAGMENTS)


# Filter clause used everywhere we compute "effective close time": we want the
# timestamp of the last MEANINGFUL message in a ticket, not the "/close" command
# the admin sent or the system "Заявка закрита..." marker we write on closure.
# That gives a much more honest "time to resolution" metric.
_MEANINGFUL_MSG_FILTER = f"""
    m.is_internal = false
    AND NOT (
        m.sender_type = 'admin'
        AND (
            m.text LIKE '/close%'
            OR m.text LIKE '{CLOSURE_SYSTEM_TEXT}%'
        )
    )
"""


async def _effective_close_seconds_map(session, ticket_ids: list[int]) -> dict[int, float]:
    """For each given closed ticket, return seconds from creation to last
    meaningful message. Empty dict if `ticket_ids` is empty.
    """
    if not ticket_ids:
        return {}
    # Build inline IN clause — SQLite parameter expansion for IN is awkward
    # via SQLAlchemy text() with tuples on some versions; safer to inline ints.
    ids_csv = ",".join(str(int(i)) for i in ticket_ids)
    rows = (await session.execute(text(f"""
        SELECT
            t.id,
            {_sql_seconds('MAX(m.created_at)', 't.created_at')} AS sec
        FROM tickets t
        JOIN messages m ON m.ticket_id = t.id
        WHERE t.id IN ({ids_csv})
          AND {_MEANINGFUL_MSG_FILTER}
        GROUP BY t.id
    """))).all()
    return {int(r[0]): float(r[1]) for r in rows if r[1] is not None}


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


def _sql_text(expression: str) -> str:
    from src.database.db import settings as _s
    from src.database.dialect import as_text

    return as_text(expression, url=_s.database_url)



# ==================== Overview KPIs ====================


async def overview_stats() -> dict:
    """Single round-trip for the four headline KPIs on the Overview page."""
    async with db_session() as session:
        total = (await session.execute(
            select(func.count(Ticket.id))
        )).scalar_one()

        active = (await session.execute(
            select(func.count(Ticket.id)).where(Ticket.status == "active")
        )).scalar_one()

        # Avg close time — computed from the last MEANINGFUL message in each
        # closed ticket, ignoring the "/close" command and system closure marker.
        # This gives the actual "time to resolution" instead of "time to admin's
        # closure click", which often happens minutes/hours after the real fix.
        avg_close_seconds = (await session.execute(text(f"""
            SELECT AVG(close_sec) FROM (
                SELECT
                    {_sql_seconds('MAX(m.created_at)', 't.created_at')} AS close_sec
                FROM tickets t
                JOIN messages m ON m.ticket_id = t.id
                WHERE t.status = 'closed'
                  AND {_MEANINGFUL_MSG_FILTER}
                GROUP BY t.id
            )
        """))).scalar_one()

        # Avg time-to-first-admin-reply, in minutes. How the elapsed time is
        # expressed differs per engine — see _sql_minutes.
        avg_first_response_min = (await session.execute(text(f"""
            SELECT AVG({_sql_minutes('first_admin.first_at', 't.created_at')})
            FROM tickets t
            JOIN (
                SELECT ticket_id, MIN(created_at) AS first_at
                FROM messages
                WHERE sender_type = 'admin' AND is_internal = false
                GROUP BY ticket_id
            ) first_admin ON first_admin.ticket_id = t.id
        """))).scalar_one()

        # CSAT: average rating + how many closed tickets got rated
        csat_avg, csat_count = (await session.execute(text("""
            SELECT AVG(csat_rating), COUNT(csat_rating)
            FROM tickets
            WHERE csat_rating IS NOT NULL
        """))).one()

        awaiting = await _awaiting_response_count(session)

    return {
        "total_tickets": int(total or 0),
        "active_tickets": int(active or 0),
        "awaiting_response": awaiting,
        "avg_close_hours": (float(avg_close_seconds) / 3600) if avg_close_seconds else None,
        "avg_first_response_minutes": float(avg_first_response_min) if avg_first_response_min else None,
        "csat_avg": float(csat_avg) if csat_avg is not None else None,
        "csat_count": int(csat_count or 0),
    }


async def _awaiting_response_count(session) -> int:
    """Active tickets that have no admin reply yet — the 'queue health' number.

    Internal notes do not count: writing a note to your colleagues is not
    answering the person who is still waiting.
    """
    val = (await session.execute(text("""
        SELECT COUNT(t.id) FROM tickets t
        WHERE t.status = 'active'
          AND NOT EXISTS (
              SELECT 1 FROM messages m
              WHERE m.ticket_id = t.id
                AND m.sender_type = 'admin' AND m.is_internal = false
          )
    """))).scalar_one()
    return int(val or 0)


async def queue_status() -> dict:
    """Small payload for the sidebar badge + new-ticket notifications poll."""
    async with db_session() as session:
        awaiting = await _awaiting_response_count(session)
        latest_id = (await session.execute(
            select(func.max(Ticket.id))
        )).scalar_one()
    return {"awaiting": awaiting, "latest_ticket_id": int(latest_id or 0)}


async def tickets_version() -> str:
    """A cheap token that changes when the ticket list would look different.

    The list and the board used to re-render the whole page on a timer to stay
    live, which is ~80 KB of HTML whether or not anything moved. They now ask
    for this first and re-render only when it differs.

    Aggregates only, no scan: a new message, a new ticket, a close or reopen,
    a stage move, a re-assignment. The assignment sum is the one approximation
    here — two swaps that cancel out exactly would go unnoticed — which is why
    callers still keep a slow full refresh as a backstop.
    """
    async with db_session() as session:
        row = (await session.execute(text(f"""
            SELECT (SELECT COALESCE(MAX(id), 0) FROM messages),
                   (SELECT COALESCE(MAX(id), 0) FROM tickets),
                   (SELECT COALESCE({_sql_text('MAX(closed_at)')}, '') FROM tickets),
                   (SELECT COUNT(*) FROM tickets WHERE status = 'active'),
                   (SELECT COUNT(*) FROM tickets
                     WHERE status = 'active' AND stage = 'new'),
                   (SELECT COUNT(*) FROM tickets
                     WHERE status = 'active' AND stage = 'in_progress'),
                   (SELECT COALESCE(SUM(COALESCE(assigned_admin_id, 0)), 0)
                      FROM tickets WHERE status = 'active')
        """))).first()
    return "-".join(str(v) for v in row)


# ==================== Trends & distributions ====================


async def tickets_per_day(days: int = 30) -> list[tuple[str, int]]:
    """Tickets created per day for the last N days. Fills zero-count days."""
    async with db_session() as session:
        cutoff = datetime.utcnow() - timedelta(days=days)
        rows = (await session.execute(text(f"""
            SELECT {_sql_day('created_at')} AS day, COUNT(*) AS cnt
            FROM tickets
            WHERE created_at >= :cutoff
            GROUP BY day
            ORDER BY day
        """), {"cutoff": cutoff})).all()

    counts = {str(r[0]): int(r[1]) for r in rows}
    today = datetime.utcnow().date()
    out: list[tuple[str, int]] = []
    for i in range(days):
        d = (today - timedelta(days=days - 1 - i)).isoformat()
        out.append((d, counts.get(d, 0)))
    return out


async def avg_first_response_per_day(days: int = 30) -> list[tuple[str, float | None]]:
    """For each day in the window, average minutes from ticket creation to the
    first admin reply. Days without data return None so the line chart breaks
    instead of dropping to zero (which would lie about the metric).
    """
    async with db_session() as session:
        cutoff = datetime.utcnow() - timedelta(days=days)
        rows = (await session.execute(text(f"""
            SELECT
                {_sql_day('t.created_at')} AS day,
                AVG({_sql_minutes('first_admin.first_at', 't.created_at')}) AS avg_min
            FROM tickets t
            JOIN (
                SELECT ticket_id, MIN(created_at) AS first_at
                FROM messages
                WHERE sender_type = 'admin' AND is_internal = false
                GROUP BY ticket_id
            ) first_admin ON first_admin.ticket_id = t.id
            WHERE t.created_at >= :cutoff
            GROUP BY day
            ORDER BY day
        """), {"cutoff": cutoff})).all()

    by_day = {str(r[0]): float(r[1]) for r in rows if r[1] is not None}
    today = datetime.utcnow().date()
    out: list[tuple[str, float | None]] = []
    for i in range(days):
        d = (today - timedelta(days=days - 1 - i)).isoformat()
        out.append((d, by_day.get(d)))
    return out


async def avg_close_time_per_day(days: int = 30) -> list[tuple[str, float | None]]:
    """For each day in the window (bucketed by ticket creation date), average
    HOURS from creation to last meaningful message — same definition as the
    KPI on top of the page. Days without closed tickets return None.
    """
    async with db_session() as session:
        cutoff = datetime.utcnow() - timedelta(days=days)
        rows = (await session.execute(text(f"""
            SELECT
                {_sql_day('sub.created_at')} AS day,
                AVG(sub.close_sec) / 3600.0 AS avg_hours
            FROM (
                SELECT
                    t.id,
                    t.created_at,
                    {_sql_seconds('MAX(m.created_at)', 't.created_at')} AS close_sec
                FROM tickets t
                JOIN messages m ON m.ticket_id = t.id
                WHERE t.status = 'closed'
                  AND t.created_at >= :cutoff
                  AND {_MEANINGFUL_MSG_FILTER}
                GROUP BY t.id
            ) sub
            GROUP BY {_sql_day('sub.created_at')}
            ORDER BY {_sql_day('sub.created_at')}
        """), {"cutoff": cutoff})).all()

    by_day = {str(r[0]): float(r[1]) for r in rows if r[1] is not None}
    today = datetime.utcnow().date()
    out: list[tuple[str, float | None]] = []
    for i in range(days):
        d = (today - timedelta(days=days - 1 - i)).isoformat()
        out.append((d, by_day.get(d)))
    return out


async def tickets_by_category() -> list[tuple[str, int]]:
    """Distribution of tickets across user-selected categories."""
    async with db_session() as session:
        rows = (await session.execute(
            select(Ticket.category, func.count(Ticket.id))
            .group_by(Ticket.category)
            .order_by(func.count(Ticket.id).desc())
        )).all()
    return [((r[0] or "—"), int(r[1])) for r in rows]


# ==================== Admins & activity ====================


async def admins_by_replies(limit: int | None = None) -> list[tuple[str, int]]:
    """All admins by reply count, descending. Pass `limit` to cap.

    Display label: @username if we know it from the users table, else `id:NNN`.
    There's no separate admin table — admins are tg_ids appearing as
    message.sender_id with sender_type='admin'.
    """
    async with db_session() as session:
        stmt = (
            select(MessageRecord.sender_id, func.count(MessageRecord.id))
            .where(MessageRecord.sender_type == "admin", MessageRecord.sender_id != 0)
            .group_by(MessageRecord.sender_id)
            .order_by(func.count(MessageRecord.id).desc())
        )
        if limit is not None:
            stmt = stmt.limit(limit)
        rows = (await session.execute(stmt)).all()

        sender_ids = [int(r[0]) for r in rows]
        # Look up display names from the users table where possible
        usernames: dict[int, str] = {}
        if sender_ids:
            ur = (await session.execute(
                select(User.tg_id, User.username).where(User.tg_id.in_(sender_ids))
            )).all()
            usernames = {int(t): (u or "") for t, u in ur}

    out: list[tuple[str, int]] = []
    for tg_id, cnt in rows:
        tg_id = int(tg_id)
        uname = usernames.get(tg_id, "")
        label = f"@{uname}" if uname else f"id:{tg_id}"
        out.append((label, int(cnt)))
    return out


async def activity_heatmap() -> tuple[list[list[int]], int]:
    """7×24 matrix of ticket creation counts. Returns (matrix, max_value).

    Rows: Mon..Sun (0..6), cols: hours 0..23.
    Max value is returned alongside so the template can normalise opacity.
    """
    async with db_session() as session:
        rows = (await session.execute(text(f"""
            SELECT
                {_sql_weekday('created_at')} AS dow,
                {_sql_hour('created_at')} AS hour,
                COUNT(*) AS cnt
            FROM tickets
            GROUP BY dow, hour
        """))).all()

    matrix = [[0] * 24 for _ in range(7)]
    max_value = 0
    for dow, hour, cnt in rows:
        # SQLite %w: 0=Sun, 1=Mon, ..., 6=Sat. We want Mon=0..Sun=6.
        iso_dow = (int(dow) - 1) % 7
        h = int(hour)
        v = int(cnt)
        matrix[iso_dow][h] = v
        if v > max_value:
            max_value = v
    return matrix, max_value


# ==================== Tickets list & detail ====================


async def list_teams_for_filter() -> list[tuple[int, str]]:
    """All teams as (id, name), ordered by name. For the filter dropdown."""
    async with db_session() as session:
        rows = (await session.execute(
            select(Team.id, Team.name).order_by(Team.name.asc())
        )).all()
    return [(int(i), n) for i, n in rows]


async def list_categories_for_filter() -> list[str]:
    """Distinct user-side categories present in any ticket."""
    async with db_session() as session:
        rows = (await session.execute(
            select(Ticket.category)
            .where(Ticket.category.isnot(None), Ticket.category != "")
            .distinct()
            .order_by(Ticket.category.asc())
        )).all()
    return [r[0] for r in rows if r[0]]


def _ticket_filter_conditions(
    *,
    status: str | None,
    team_id: int | None,
    category: str | None,
    date_from: datetime | None,
    date_to: datetime | None,
    q: str | None = None,
    assigned: int | str | None = None,
    stage: str | None = None,
) -> list:
    """Shared WHERE conditions for the ticket list, export and count queries.

    `assigned`: operator id (int), "none" for unassigned, None = no filter.
    `stage`: board stage; only meaningful for active tickets, so it implies
    status='active' — a "waiting" filter that returned closed tickets would be
    nonsense to the operator reading the board.
    """
    conds: list = []
    if status:
        conds.append(Ticket.status == status)
    if stage:
        conds.append(Ticket.stage == stage)
        if not status:
            conds.append(Ticket.status == "active")
    if team_id:
        conds.append(Ticket.team_id == team_id)
    if category:
        conds.append(Ticket.category == category)
    if date_from:
        conds.append(Ticket.created_at >= date_from)
    if date_to:
        conds.append(Ticket.created_at < date_to)
    if assigned == "none":
        conds.append(Ticket.assigned_admin_id.is_(None))
    elif isinstance(assigned, int):
        conds.append(Ticket.assigned_admin_id == assigned)
    if q and q.strip():
        conds.append(_search_condition(q.strip()))
    return conds


def _contains(column, needle: str):
    """Case-insensitive "contains", in whichever engine this instance runs."""
    from src.database.db import settings as _db_settings
    from src.database.dialect import contains_ci

    return contains_ci(column, needle, url=_db_settings.database_url)


def _search_condition(q: str):
    """Full-text-ish search: ticket id, message text, author username, category.

    Uses the custom ci_contains() SQLite function (registered in db.py) for
    proper Unicode case-insensitive matching — SQLite's own LIKE/lower() only
    fold ASCII, which would break Cyrillic search entirely. PostgreSQL folds
    Unicode in ILIKE already; which of the two is used is decided in
    src/database/dialect.py, not here.
    """
    parts: list = []

    # "#82" or "82" → direct id hit
    num = q.lstrip("#")
    if num.isdigit():
        parts.append(Ticket.id == int(num))

    # Author: username / work_username ("@kate" and "kate" both work)
    q_user = q.lstrip("@")
    if q_user:
        parts.append(exists(
            select(User.id).where(
                User.id == Ticket.user_id,
                or_(
                    _contains(User.username, q_user),
                    _contains(User.work_username, q_user),
                ),
            )
        ))

    # Message text (Unicode case-insensitive)
    parts.append(exists(
        select(MessageRecord.id).where(
            MessageRecord.ticket_id == Ticket.id,
            _contains(MessageRecord.text, q),
        )
    ))

    # Category name
    parts.append(_contains(Ticket.category, q))

    return or_(*parts)


async def list_tickets(
    *,
    status: str | None = None,
    team_id: int | None = None,
    category: str | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    q: str | None = None,
    assigned: int | str | None = None,
    stage: str | None = None,
    page: int = 0,
    page_size: int = 25,
) -> tuple[list[dict], int]:
    """Filtered & paginated ticket list.

    Returns (rows, total_count). Each row is a dict with everything the
    template needs — no lazy-loading surprises in Jinja.
    """
    conds = _ticket_filter_conditions(
        status=status, team_id=team_id, category=category,
        date_from=date_from, date_to=date_to, q=q, assigned=assigned,
        stage=stage,
    )
    async with db_session() as session:
        stmt = select(Ticket).options(
            selectinload(Ticket.user),
        )
        if conds:
            stmt = stmt.where(*conds)

        # Count via a separate small query — cheaper than loading all rows
        count_stmt = select(func.count(Ticket.id))
        if conds:
            count_stmt = count_stmt.where(*conds)
        total = int((await session.execute(count_stmt)).scalar_one())

        stmt = stmt.order_by(Ticket.id.desc()).offset(page * page_size).limit(page_size)
        tickets = (await session.execute(stmt)).scalars().all()

        # Resolve team names in one extra round-trip
        team_ids = {t.team_id for t in tickets if t.team_id}
        team_names: dict[int, str] = {}
        if team_ids:
            rows = (await session.execute(
                select(Team.id, Team.name).where(Team.id.in_(team_ids))
            )).all()
            team_names = {int(i): n for i, n in rows}

        # Effective close time for closed tickets in this page (batched)
        closed_ids = [t.id for t in tickets if t.status == "closed"]
        close_secs = await _effective_close_seconds_map(session, closed_ids)

        # Resolve "work account" labels for users writing from a personal account
        work_ids = {u.linked_to_user_id for t in tickets if (u := t.user) and u.linked_to_user_id}
        work_labels: dict[int, str] = {}
        if work_ids:
            wr = (await session.execute(
                select(User.id, User.username, User.tg_id).where(User.id.in_(work_ids))
            )).all()
            for wid, wuname, wtg in wr:
                work_labels[int(wid)] = f"@{wuname}" if wuname else f"id {wtg}"

        # Operator names for the "Оператор" column (one query for the page)
        assigned_ids = {t.assigned_admin_id for t in tickets if t.assigned_admin_id}
        operator_names: dict[int, str] = {}
        if assigned_ids:
            ar = (await session.execute(
                select(AdminUser.id, AdminUser.name, AdminUser.email)
                .where(AdminUser.id.in_(assigned_ids))
            )).all()
            operator_names = {int(i): (n or e.split("@")[0]) for i, n, e in ar}

    out: list[dict] = []
    for t in tickets:
        u = t.user
        username = (u.username if u else None) or ""
        eff_seconds = close_secs.get(t.id) if t.status == "closed" else None
        work_label = work_labels.get(u.linked_to_user_id) if (u and u.linked_to_user_id) else None
        out.append({
            "id": t.id,
            "status": t.status,
            "stage": t.stage,
            "board_column": board_column(t.status, t.stage),
            "category": t.category,
            "team": team_names.get(t.team_id, "—") if t.team_id else "—",
            "user_label": (f"@{username}" if username else f"id:{display_address(u)}") if u else "—",
            "user_tg_id": u.tg_id if u else None,
            "channel": channel_of(u) if u else CHANNEL_DEFAULT,
            "user_db_id": u.id if u else None,
            "local_user_id": u.local_user_id if u else None,
            "linked_work": work_label,  # set → this ticket is from a personal account
            "created_at": t.created_at,
            "closed_at": t.closed_at,
            "processing_time_seconds": int(eff_seconds) if eff_seconds is not None else None,
            "is_sensitive": is_sensitive_category(t.category),
            "csat_rating": t.csat_rating,
            "assigned_admin_id": t.assigned_admin_id,
            "operator": operator_names.get(t.assigned_admin_id) if t.assigned_admin_id else None,
        })
    return out, total


def _initials(name: str | None) -> str:
    """Up to two letters for an avatar: 'Liam Novak' -> LN, 'ryan.db' -> R."""
    if not name:
        return ""
    parts = [p for p in name.replace(".", " ").replace("_", " ").split() if p]
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0][:1].upper()
    return (parts[0][:1] + parts[1][:1]).upper()


async def board_columns(
    *,
    team_id: int | None = None,
    assigned: int | str | None = None,
    q: str | None = None,
    per_column: int = 50,
    closed_days: int = 7,
) -> dict:
    """Tickets grouped into the four board columns.

    Each column is capped at `per_column` cards — a board is for triage, not
    for browsing history, and rendering 4000 closed tickets would help nobody.
    `total` per column is the real count so the UI can say "+N more".

    The closed column is additionally limited to the last `closed_days`, for
    the same reason: it exists to show what just got finished, not an archive.
    """
    from src.services.workflow_service import BOARD_COLUMNS, CLOSED_COLUMN

    base = _ticket_filter_conditions(
        status=None, team_id=team_id, category=None,
        date_from=None, date_to=None, q=q, assigned=assigned,
    )
    closed_cutoff = datetime.utcnow() - timedelta(days=closed_days)

    columns: dict[str, dict] = {}
    async with db_session() as session:
        for column in BOARD_COLUMNS:
            if column == CLOSED_COLUMN:
                conds = [*base, Ticket.status == "closed",
                         Ticket.closed_at >= closed_cutoff]
                order = Ticket.closed_at.desc()
            else:
                conds = [*base, Ticket.status == "active", Ticket.stage == column]
                # Oldest first: the card that has been waiting longest is the
                # one that needs attention, so it belongs at the top.
                order = Ticket.created_at.asc()

            total = (await session.execute(
                select(func.count(Ticket.id)).where(*conds)
            )).scalar_one()

            tickets = (await session.execute(
                select(Ticket)
                .options(selectinload(Ticket.user))
                .where(*conds)
                .order_by(order)
                .limit(per_column)
            )).scalars().all()

            columns[column] = {"tickets": tickets, "total": int(total or 0)}

        # Names for the operator avatars, one query for the whole board
        assigned_ids = {
            t.assigned_admin_id
            for col in columns.values() for t in col["tickets"]
            if t.assigned_admin_id
        }
        operator_names: dict[int, str] = {}
        if assigned_ids:
            rows = (await session.execute(
                select(AdminUser.id, AdminUser.name, AdminUser.email)
                .where(AdminUser.id.in_(assigned_ids))
            )).all()
            operator_names = {int(i): (n or e.split("@")[0]) for i, n, e in rows}

        # Who the ball is with. A card in "in progress" looks the same whether
        # it is waiting on us or on the customer, and that is the single thing
        # an operator most needs to see. The answer is the oldest customer
        # message that no reply has landed after; NULL means it is their move.
        active_ids = [
            t.id for key, col in columns.items() if key != CLOSED_COLUMN
            for t in col["tickets"]
        ]
        awaiting_since: dict[int, datetime] = {}
        if active_ids:
            rows = (await session.execute(text("""
                SELECT t.id, MIN(m.created_at)
                  FROM tickets t
                  JOIN messages m ON m.ticket_id = t.id
                 WHERE t.id IN :ids
                   AND m.is_internal = false
                   AND m.sender_type = 'user'
                   AND m.id > COALESCE((SELECT MAX(a.id) FROM messages a
                                         WHERE a.ticket_id = t.id
                                           AND a.is_internal = false
                                           AND a.sender_type = 'admin'), 0)
                 GROUP BY t.id
            """).bindparams(bindparam("ids", expanding=True)), {"ids": active_ids})).all()
            for tid, since in rows:
                if since is not None:
                    awaiting_since[int(tid)] = (
                        since if isinstance(since, datetime)
                        else datetime.fromisoformat(str(since))
                    )

        # How long each card has been sitting where it is
        now = datetime.utcnow()

    # The SLA clock runs on working time, so an overnight wait is not a breach.
    hours_config = await work_hours_service.get_config()
    sla_minutes = await work_hours_service.get_sla_minutes()
    now_aware = now.replace(tzinfo=timezone.utc)

    out: dict[str, dict] = {}
    for column, data in columns.items():
        cards = []
        for t in data["tickets"]:
            u = t.user
            username = (u.username if u else None) or ""
            since = t.closed_at if column == CLOSED_COLUMN else t.created_at
            operator = operator_names.get(t.assigned_admin_id) if t.assigned_admin_id else None
            pending = awaiting_since.get(t.id)
            waiting = work_hours_service.working_seconds_between(
                hours_config, pending.replace(tzinfo=timezone.utc), now_aware,
            ) if pending else None
            cards.append({
                "waiting_seconds": waiting,
                "sla_state": work_hours_service.sla_state(waiting, sla_minutes),
                "id": t.id,
                "status": t.status,
                "stage": t.stage,
                "category": t.category or "",
                "user_label": (f"@{username}" if username else f"id:{u.tg_id}") if u else "—",
                "operator": operator,
                "initials": _initials(operator),
                "age_seconds": int((now - since).total_seconds()) if since else None,
                "csat_rating": t.csat_rating,
                "is_sensitive": is_sensitive_category(t.category),
            })
        out[column] = {
            "cards": cards,
            "total": data["total"],
            "hidden": max(0, data["total"] - len(cards)),
        }
    return out


async def list_tickets_for_export(
    *,
    status: str | None = None,
    team_id: int | None = None,
    category: str | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    q: str | None = None,
    stage: str | None = None,
) -> list[dict]:
    """Same filters as `list_tickets` but no pagination, with extra columns
    suitable for an Excel report (message counts, closure summary, etc).
    """
    conds = _ticket_filter_conditions(
        status=status, team_id=team_id, category=category,
        date_from=date_from, date_to=date_to, q=q, stage=stage,
    )
    async with db_session() as session:
        stmt = select(Ticket).options(selectinload(Ticket.user))
        if conds:
            stmt = stmt.where(*conds)
        stmt = stmt.order_by(Ticket.id.desc())
        tickets = (await session.execute(stmt)).scalars().all()

        # Team names
        team_ids = {t.team_id for t in tickets if t.team_id}
        team_names: dict[int, str] = {}
        if team_ids:
            tr = (await session.execute(
                select(Team.id, Team.name).where(Team.id.in_(team_ids))
            )).all()
            team_names = {int(i): n for i, n in tr}

        # Effective close time for closed tickets
        closed_ids = [t.id for t in tickets if t.status == "closed"]
        close_secs = await _effective_close_seconds_map(session, closed_ids)

        # Message counts per ticket (single query)
        msg_counts: dict[int, dict[str, int]] = {}
        if tickets:
            ids_csv = ",".join(str(t.id) for t in tickets)
            mc = (await session.execute(text(f"""
                SELECT
                    ticket_id,
                    SUM(CASE WHEN sender_type='user' THEN 1 ELSE 0 END) AS u,
                    SUM(CASE WHEN sender_type='admin' AND is_internal=false THEN 1 ELSE 0 END) AS a,
                    SUM(CASE WHEN template_id IS NOT NULL THEN 1 ELSE 0 END) AS via_tpl
                FROM messages
                WHERE ticket_id IN ({ids_csv})
                GROUP BY ticket_id
            """))).all()
            for tid, u_cnt, a_cnt, tpl_cnt in mc:
                msg_counts[int(tid)] = {
                    "user": int(u_cnt or 0),
                    "admin": int(a_cnt or 0),
                    "via_template": int(tpl_cnt or 0),
                }

        # Operator names
        assigned_ids = {t.assigned_admin_id for t in tickets if t.assigned_admin_id}
        operator_names: dict[int, str] = {}
        if assigned_ids:
            ar = (await session.execute(
                select(AdminUser.id, AdminUser.name, AdminUser.email)
                .where(AdminUser.id.in_(assigned_ids))
            )).all()
            operator_names = {int(i): (n or e.split("@")[0]) for i, n, e in ar}

    out: list[dict] = []
    for t in tickets:
        u = t.user
        username = (u.username if u else None) or ""
        eff = close_secs.get(t.id) if t.status == "closed" else None
        counts = msg_counts.get(t.id, {"user": 0, "admin": 0, "via_template": 0})
        closure_cats = ", ".join(
            close_reason_service.display_stored(c) for c in t.admin_categories
        ) if t.admin_categories else ""

        out.append({
            "id": t.id,
            "status": t.status,
            "stage": t.stage,
            "team": team_names.get(t.team_id, "") if t.team_id else "",
            "username": username,
            "tg_id": u.tg_id if u else None,
            "local_user_id": u.local_user_id if u else None,
            "category": t.category or "",
            "created_at": t.created_at,
            "closed_at": t.closed_at,
            "resolution_seconds": int(eff) if eff is not None else None,
            "msg_user": counts["user"],
            "msg_admin": counts["admin"],
            "msg_total": counts["user"] + counts["admin"],
            "via_template": counts["via_template"],
            "closure_categories": closure_cats,
            "custom_reason": "" if is_sensitive_category(t.category) else (t.custom_reason or ""),
            "is_sensitive": is_sensitive_category(t.category),
            "operator": operator_names.get(t.assigned_admin_id, "") if t.assigned_admin_id else "",
            "csat_rating": t.csat_rating,
        })
    return out


async def messages_after(ticket_id: int, after_id: int = 0) -> dict | None:
    """Only what the open page has not seen yet, for the live poll.

    The poll used to call ticket_detail(), which loads the author, their linked
    accounts, a category breakdown and the resolution time, then re-serialises
    the entire conversation — several kilobytes every few seconds to answer
    "anything new?". On a long ticket that grew without bound.

    Returns None if the ticket does not exist.
    """
    async with db_session() as session:
        row = (await session.execute(
            select(Ticket.status, Ticket.stage, User.username, User.tg_id)
            .join(User, User.id == Ticket.user_id, isouter=True)
            .where(Ticket.id == ticket_id)
        )).first()
        if row is None:
            return None
        status, stage, username, tg_id = row

        msgs = (await session.execute(
            select(MessageRecord)
            .where(MessageRecord.ticket_id == ticket_id, MessageRecord.id > after_id)
            .order_by(MessageRecord.id.asc())
        )).scalars().all()

    # No separate max() query: an empty batch means nothing exists past the
    # cursor, so the cursor already IS the newest id the client should hold.
    latest_id = msgs[-1].id if msgs else after_id

    sensitive = is_sensitive_category(None)
    return {
        "status": status,
        "stage": stage,
        "latest_id": int(latest_id),
        "user_label": (f"@{username}" if username else f"id:{tg_id}") if tg_id else "—",
        "messages": [
            {
                "id": m.id,
                "sender_type": m.sender_type,
                "sender_label": m.sender_label,
                "text": _dash_t("msg_masked_credentials") if sensitive else (m.text or ""),
                "is_template": m.template_id is not None,
                "is_internal": bool(m.is_internal),
                "deleted_at": m.deleted_at.isoformat() if m.deleted_at else None,
                "has_attachment": bool(m.file_id),
                "file_name": m.file_name,
                "attachment_url": f"/tickets/attachment/{m.id}" if m.file_id else None,
                "is_image": bool(m.file_id) and (
                    "photo" in (m.file_type or "").lower()
                    or (m.file_name or "").lower().endswith(
                        (".jpg", ".jpeg", ".png", ".webp", ".gif"))
                ),
                "created_at": m.created_at.strftime("%m-%d %H:%M") if m.created_at else "",
            }
            for m in msgs
        ],
    }


#: The widest value a row id can hold. Primary keys are declared `Integer`,
#: which is 32 bits in PostgreSQL — so this is not "a big number", it is the
#: real limit of the column being searched. Anything past it cannot name a
#: row, and handing it to the driver produces a 500 rather than a miss.
#:
#: Telegram ids are deliberately not held to this: they live in `BigInteger`
#: and routinely exceed it. See ROW_ID_MAX's use — filters over admin and
#: team ids only.
ROW_ID_MAX = 2 ** 31 - 1


def is_row_id(value) -> bool:
    """Whether this could name a row at all."""
    try:
        return -ROW_ID_MAX <= int(value) <= ROW_ID_MAX
    except (TypeError, ValueError):
        return False


async def ticket_detail(ticket_id: int) -> dict | None:
    """Full ticket card: header, all messages, closure info. Returns None if not found."""
    if not is_row_id(ticket_id):
        # Wider than the column. Not found, rather than a 500 from
        # inside the driver.
        return None

    async with db_session() as session:
        ticket = (await session.execute(
            select(Ticket)
            .where(Ticket.id == ticket_id)
            .options(
                selectinload(Ticket.user),
                selectinload(Ticket.messages),
            )
        )).scalar_one_or_none()
        if ticket is None:
            return None

        team_name = "—"
        if ticket.team_id:
            row = (await session.execute(
                select(Team.name).where(Team.id == ticket.team_id)
            )).scalar_one_or_none()
            if row:
                team_name = row

        # Effective close time (ignores /close & system closure marker)
        eff_close: int | None = None
        if ticket.status == "closed":
            secs_map = await _effective_close_seconds_map(session, [ticket.id])
            v = secs_map.get(ticket.id)
            eff_close = int(v) if v is not None else None

        # Account linking (personal ↔ work) for this ticket's author
        u = ticket.user
        linked_work_label = None
        linked_personals = []
        if u is not None:
            if u.linked_to_user_id:
                w = (await session.execute(
                    select(User).where(User.id == u.linked_to_user_id)
                )).scalar_one_or_none()
                if w:
                    linked_work_label = f"@{w.username}" if w.username else f"id {w.tg_id}"
            ps = (await session.execute(
                select(User).where(User.linked_to_user_id == u.id)
            )).scalars().all()
            linked_personals = [(f"@{p.username}" if p.username else f"id{p.tg_id}") for p in ps]

    sensitive = is_sensitive_category(ticket.category)
    username = (u.username if u else None) or ""
    # Which pipe this conversation runs on decides what the page may offer:
    # the unsend button below, and eventually anything else a channel cannot
    # honour. Resolved once here rather than per message.
    user_channel = channel_of(u) if u is not None else CHANNEL_DEFAULT

    messages = []
    for m in sorted(ticket.messages, key=lambda x: x.created_at):
        text = m.text or ""
        # 1Pass etc. — mask bodies, keep the metadata so admins can audit who said when
        if sensitive:
            text = _dash_t("msg_masked_credentials")
        # Is the attachment an image we can preview inline?
        ftype = (m.file_type or "").lower()
        fname = (m.file_name or "").lower()
        is_image = bool(m.file_id) and not sensitive and (
            "photo" in ftype or ftype.startswith("image")
            or fname.endswith((".jpg", ".jpeg", ".png", ".webp", ".gif"))
        )
        messages.append({
            "id": m.id,
            "sender_type": m.sender_type,
            "sender_id": m.sender_id,
            # Set only when the conversation has more than one person in it —
            # a Slack channel shared with a client's team. Empty everywhere
            # else, where naming the sender on every line is noise.
            "sender_label": m.sender_label,
            "text": text,
            "file_name": m.file_name,
            "file_type": m.file_type,
            "file_size": m.file_size,
            "has_attachment": bool(m.file_id),
            # Attachments are streamed through an endpoint; never expose raw file_id.
            # Sensitive categories get no attachment access at all.
            "attachment_url": (f"/tickets/attachment/{m.id}" if (m.file_id and not sensitive) else None),
            "is_image": is_image,
            "is_template_reply": m.template_id is not None,
            "is_internal": bool(m.is_internal),
            "deleted_at": m.deleted_at,
            "can_delete": message_service.can_delete(m, channel=user_channel),
            "created_at": m.created_at,
        })

    return {
        "id": ticket.id,
        "channel": user_channel,
        "user_address": display_address(u) if u else None,
        "status": ticket.status,
        "stage": ticket.stage,
        "board_column": board_column(ticket.status, ticket.stage),
        "category": ticket.category,
        "team": team_name,
        "user_label": (f"@{username}" if username else f"id:{u.tg_id}") if u else "—",
        # Contact details a customer gave us, and whether anyone vouched for
        # them. The flag travels with the values rather than being looked up
        # separately, because the one place it must never be missing is the
        # place the values are displayed.
        "user_email": (u.email or None) if u else None,
        "user_external_id": (u.external_id or None) if u else None,
        "user_identity_verified": bool(u.identity_verified) if u else False,
        "user_tg_id": u.tg_id if u else None,
        "local_user_id": u.local_user_id if u else None,
        "created_at": ticket.created_at,
        "closed_at": ticket.closed_at,
        "processing_time_seconds": eff_close,
        # Rendered, not raw: the free-text option is stored as a sentinel.
        "admin_categories": [
            close_reason_service.display_stored(c)
            for c in (ticket.admin_categories or [])
        ],
        "custom_reason": ticket.custom_reason if not sensitive else None,
        "is_sensitive": sensitive,
        "linked_work": linked_work_label,     # set → author writes from a personal acct
        "linked_personals": linked_personals, # set → author is a work acct with personals
        "user_db_id": u.id if u else None,
        "csat_rating": ticket.csat_rating,
        "assigned_admin_id": ticket.assigned_admin_id,
        "messages": messages,
    }


# ==================== AI Analytics ====================


async def ai_overview() -> dict:
    """High-level AI metrics: counts by use_case, accept rate, tokens, latency."""
    async with db_session() as session:
        # Counts and accept rate by use_case
        by_use_case = (await session.execute(text("""
            SELECT
                use_case,
                COUNT(*) AS total,
                SUM(CASE WHEN success = true THEN 1 ELSE 0 END) AS successful,
                SUM(CASE WHEN accepted = true THEN 1 ELSE 0 END) AS accepted,
                SUM(CASE WHEN accepted = false THEN 1 ELSE 0 END) AS rejected,
                SUM(tokens_in) AS tokens_in_total,
                SUM(tokens_out) AS tokens_out_total,
                AVG(latency_ms) AS avg_latency
            FROM ai_calls
            GROUP BY use_case
        """))).all()

        # Overall totals
        overall = (await session.execute(text("""
            SELECT
                COUNT(*) AS total,
                SUM(tokens_in) AS ti, SUM(tokens_out) AS to_,
                SUM(CASE WHEN success = true THEN 1 ELSE 0 END) AS ok,
                SUM(CASE WHEN accepted = true THEN 1 ELSE 0 END) AS acc,
                SUM(CASE WHEN accepted IS NOT NULL THEN 1 ELSE 0 END) AS decided
            FROM ai_calls
        """))).one()

        # Latency p50/p95 — SQLite has no PERCENTILE_CONT, approximate via
        # ORDER + OFFSET. For small N this is fine; for huge N we'd switch
        # to a different engine anyway.
        latencies = (await session.execute(text("""
            SELECT latency_ms FROM ai_calls
            WHERE latency_ms > 0
            ORDER BY latency_ms ASC
        """))).all()

    def _pct_rate(num, den):
        if not den:
            return None
        return num / den * 100

    p50 = p95 = None
    if latencies:
        n = len(latencies)
        p50 = int(latencies[int(n * 0.5)][0])
        p95 = int(latencies[min(n - 1, int(n * 0.95))][0])

    return {
        "total": int(overall[0] or 0),
        "tokens_in": int(overall[1] or 0),
        "tokens_out": int(overall[2] or 0),
        "success_rate": _pct_rate(int(overall[3] or 0), int(overall[0] or 0)),
        "accept_rate": _pct_rate(int(overall[4] or 0), int(overall[5] or 0)),
        "decided": int(overall[5] or 0),
        "accepted": int(overall[4] or 0),
        "latency_p50": p50,
        "latency_p95": p95,
        "by_use_case": [
            {
                "use_case": r[0],
                "total": int(r[1] or 0),
                "successful": int(r[2] or 0),
                "accepted": int(r[3] or 0),
                "rejected": int(r[4] or 0),
                "tokens_in": int(r[5] or 0),
                "tokens_out": int(r[6] or 0),
                "avg_latency_ms": int(r[7] or 0),
                "accept_rate": _pct_rate(int(r[3] or 0), (int(r[3] or 0) + int(r[4] or 0))),
            }
            for r in by_use_case
        ],
    }


async def ai_calls_per_day(days: int = 30) -> list[tuple[str, int]]:
    """AI calls per day for the last N days, including zero-filled gaps."""
    async with db_session() as session:
        cutoff = datetime.utcnow() - timedelta(days=days)
        rows = (await session.execute(text(f"""
            SELECT {_sql_day('created_at')} AS day, COUNT(*) AS cnt
            FROM ai_calls
            WHERE created_at >= :cutoff
            GROUP BY day
            ORDER BY day
        """), {"cutoff": cutoff})).all()
    counts = {str(r[0]): int(r[1]) for r in rows}
    today = datetime.utcnow().date()
    out: list[tuple[str, int]] = []
    for i in range(days):
        d = (today - timedelta(days=days - 1 - i)).isoformat()
        out.append((d, counts.get(d, 0)))
    return out


async def ai_recent_rejected(limit: int = 10) -> list[dict]:
    """Most recent rejected AI suggestions — useful to improve prompts.

    Uses ORM so `created_at` comes back as a real datetime, not a string.
    """
    async with db_session() as session:
        stmt = (
            select(
                AICall.id,
                AICall.use_case,
                AICall.ticket_id,
                AICall.model,
                AICall.suggested_template_id,
                AICall.suggested_text,
                AICall.created_at,
                Ticket.category,
            )
            .outerjoin(Ticket, Ticket.id == AICall.ticket_id)
            .where(AICall.accepted.is_(False))
            .order_by(AICall.created_at.desc())
            .limit(limit)
        )
        rows = (await session.execute(stmt)).all()

    out: list[dict] = []
    for r in rows:
        category = r[7] or ""
        sensitive = is_sensitive_category(category)
        preview = r[5]
        if sensitive and preview:
            preview = _dash_t("msg_hidden_short")
        out.append({
            "id": int(r[0]),
            "use_case": r[1],
            "ticket_id": int(r[2]) if r[2] else None,
            "model": r[3],
            "template_id": int(r[4]) if r[4] else None,
            "preview": preview,
            "created_at": r[6],
            "category": category,
        })
    return out


# ==================== Templates analytics ====================


async def templates_with_usage(include_archived: bool = False) -> list[dict]:
    """All templates with their usage_count + last_used date + team."""
    async with db_session() as session:
        stmt = (
            select(
                ReplyTemplate.id,
                ReplyTemplate.title,
                ReplyTemplate.category,
                ReplyTemplate.usage_count,
                ReplyTemplate.is_archived,
                ReplyTemplate.team_id,
                ReplyTemplate.created_at,
            )
            .order_by(ReplyTemplate.usage_count.desc(), ReplyTemplate.title.asc())
        )
        if not include_archived:
            stmt = stmt.where(ReplyTemplate.is_archived.is_(False))
        rows = (await session.execute(stmt)).all()

        # Resolve team names
        team_ids = {int(r[5]) for r in rows if r[5]}
        team_names: dict[int, str] = {}
        if team_ids:
            tr = (await session.execute(
                select(Team.id, Team.name).where(Team.id.in_(team_ids))
            )).all()
            team_names = {int(i): n for i, n in tr}

        # Last used per template
        last_used = (await session.execute(text("""
            SELECT template_id, MAX(created_at)
            FROM messages
            WHERE template_id IS NOT NULL
            GROUP BY template_id
        """))).all()
        last_used_map = {int(r[0]): r[1] for r in last_used}

    out: list[dict] = []
    for r in rows:
        tid = int(r[0])
        out.append({
            "id": tid,
            "title": r[1],
            "category": r[2] or "—",
            "usage_count": int(r[3] or 0),
            "is_archived": bool(r[4]),
            "team": team_names.get(int(r[5]) if r[5] else 0, "—"),
            "created_at": r[6],
            "last_used": last_used_map.get(tid),
        })
    return out


# ==================== Users analytics ====================


# Sortable columns for the users table, mapped to SQL. The mapping is also the
# whitelist: the ORDER BY clause is built by string interpolation, so a column
# name arriving from the query string must never reach it directly.
USER_SORTS = {
    "user": "LOWER(COALESCE(u.username, '')) ",
    "team": "team_name ",
    "total": "total ",
    "active": "active ",
    "closed": "closed ",
    "last": "last_ticket_at ",
}
DEFAULT_USER_SORT = "last"


async def users_with_ticket_stats(
    limit: int | None = None,
    *,
    team_id: int | None = None,
    unassigned_only: bool = False,
    sort: str = DEFAULT_USER_SORT,
    direction: str = "desc",
) -> list[dict]:
    """All users, with stats: total tickets, active/closed, top-3 categories,
    last activity.

    LEFT JOIN, not JOIN: a user who registered but has not opened a ticket yet
    must still be listed — that is exactly the person an operator is looking
    for when they need to assign a team.

    Default order is most recent activity. SQLite sorts NULLs last under DESC,
    so people who have never written sink to the bottom on their own; the id
    tiebreak then puts the newest registrations first, which is the useful
    order when the list is filtered down to those very people.

    `unassigned_only` narrows to users with no team; `team_id` to one team.
    """
    where = []
    if unassigned_only:
        where.append("u.team_id IS NULL")
    elif team_id:
        where.append(f"u.team_id = {int(team_id)}")
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    order_col = USER_SORTS.get(sort, USER_SORTS[DEFAULT_USER_SORT])
    order_dir = "ASC" if str(direction).lower() == "asc" else "DESC"
    order_sql = f"{order_col}{order_dir}, u.id DESC"

    async with db_session() as session:
        # One round-trip: aggregate tickets per user
        rows = (await session.execute(text(f"""
            SELECT
                u.id, u.tg_id, u.username, u.team_id, u.local_user_id,
                COUNT(t.id) AS total,
                SUM(CASE WHEN t.status = 'active' THEN 1 ELSE 0 END) AS active,
                SUM(CASE WHEN t.status = 'closed' THEN 1 ELSE 0 END) AS closed,
                MAX(t.created_at) AS last_ticket_at,
                tm.name AS team_name
            FROM users u
            LEFT JOIN tickets t ON t.user_id = u.id
            LEFT JOIN teams tm ON tm.id = u.team_id
            {where_sql}
            GROUP BY u.id, u.tg_id, u.username, u.team_id, u.local_user_id, tm.name
            ORDER BY {order_sql}
        """))).all()

        if limit is not None:
            rows = rows[:limit]

        # Top categories per user — fetched in one query, grouped in Python
        user_db_ids = [int(r[0]) for r in rows]
        cats_per_user: dict[int, list[tuple[str, int]]] = {uid: [] for uid in user_db_ids}
        if user_db_ids:
            ids_csv = ",".join(str(i) for i in user_db_ids)
            cat_rows = (await session.execute(text(f"""
                SELECT user_id, category, COUNT(*) AS cnt
                FROM tickets
                WHERE user_id IN ({ids_csv})
                GROUP BY user_id, category
                ORDER BY user_id, cnt DESC
            """))).all()
            for uid, cat, cnt in cat_rows:
                cats_per_user[int(uid)].append(((cat or "—"), int(cnt)))

    out: list[dict] = []
    for r in rows:
        u_db_id = int(r[0])
        username = (r[2] or "")
        team_name = r[9] or "—"
        # SQLite via raw text() returns timestamps as strings — normalise to datetime
        last_ticket_at = r[8]
        if isinstance(last_ticket_at, str):
            try:
                last_ticket_at = datetime.fromisoformat(last_ticket_at)
            except ValueError:
                last_ticket_at = None
        out.append({
            "user_db_id": u_db_id,
            "tg_id": int(r[1]),
            "username": username,
            "label": f"@{username}" if username else f"id:{int(r[1])}",
            "team": team_name,
            "local_user_id": r[4],
            "total": int(r[5] or 0),
            "active": int(r[6] or 0),
            "closed": int(r[7] or 0),
            "last_ticket_at": last_ticket_at,
            "top_categories": cats_per_user.get(u_db_id, [])[:3],
        })
    return out


async def user_detail(user_db_id: int) -> dict | None:
    """Full per-user card: profile, all tickets, full category breakdown."""
    if not is_row_id(user_db_id):
        # Wider than the column. Not found, rather than a 500 from
        # inside the driver.
        return None

    async with db_session() as session:
        user = (await session.execute(
            select(User).where(User.id == user_db_id)
        )).scalar_one_or_none()
        if user is None:
            return None

        team_name = "—"
        if user.team_id:
            tn = (await session.execute(
                select(Team.name).where(Team.id == user.team_id)
            )).scalar_one_or_none()
            if tn:
                team_name = tn

        tickets = (await session.execute(
            select(Ticket)
            .where(Ticket.user_id == user_db_id)
            .order_by(Ticket.id.desc())
        )).scalars().all()

        # Effective close time for closed ones (batched)
        closed_ids = [t.id for t in tickets if t.status == "closed"]
        close_secs = await _effective_close_seconds_map(session, closed_ids)

        # Category breakdown
        cat_rows = (await session.execute(text("""
            SELECT category, COUNT(*) AS cnt
            FROM tickets
            WHERE user_id = :uid
            GROUP BY category
            ORDER BY cnt DESC
        """), {"uid": user_db_id})).all()

    categories = [((c or "—"), int(n)) for c, n in cat_rows]

    ticket_rows: list[dict] = []
    for t in tickets:
        eff = close_secs.get(t.id) if t.status == "closed" else None
        ticket_rows.append({
            "id": t.id,
            "status": t.status,
            "category": t.category,
            "created_at": t.created_at,
            "closed_at": t.closed_at,
            "processing_time_seconds": int(eff) if eff is not None else None,
            "is_sensitive": is_sensitive_category(t.category),
        })

    # Account linking info (re-open the session briefly for the lookups)
    async with db_session() as session:
        linked_work = None
        if user.linked_to_user_id:
            w = (await session.execute(
                select(User).where(User.id == user.linked_to_user_id)
            )).scalar_one_or_none()
            if w:
                linked_work = f"@{w.username}" if w.username else f"id {w.tg_id}"
        personals = (await session.execute(
            select(User).where(User.linked_to_user_id == user.id)
        )).scalars().all()
        personals_labels = [
            (f"@{p.username}" if p.username else f"id{p.tg_id}") for p in personals
        ]
        all_teams = [(t.id, t.name) for t in (await session.execute(
            select(Team).order_by(Team.name.asc())
        )).scalars().all()]

    username = user.username or ""
    return {
        "user_db_id": user.id,
        "tg_id": user.tg_id,
        "username": username,
        "label": f"@{username}" if username else f"id:{user.tg_id}",
        "team": team_name,
        "team_id": user.team_id,
        "local_user_id": user.local_user_id,
        "is_verified": user.is_verified,
        "is_active": user.is_active,
        "language": user.language,
        "created_at": user.created_at,
        "tickets": ticket_rows,
        "categories": categories,
        "total": len(ticket_rows),
        "active_count": sum(1 for t in ticket_rows if t["status"] == "active"),
        "closed_count": sum(1 for t in ticket_rows if t["status"] == "closed"),
        # Management extras
        "linked_work": linked_work,
        "pending_work": user.pending_work_username,
        "personals": personals_labels,
        "all_teams": all_teams,
    }


async def template_admin_matrix() -> dict:
    """Returns admin × template usage matrix as a sparse list of cells.

    Returns:
        {
            "admins": [{"label": "@x", "tg_id": 1234}, ...],
            "templates": [{"id": 1, "title": "RDP"}, ...],
            "cells": [{"admin_idx": 0, "template_idx": 3, "count": 5}, ...]
        }
    """
    async with db_session() as session:
        rows = (await session.execute(text("""
            SELECT m.sender_id, m.template_id, COUNT(*) AS cnt
            FROM messages m
            WHERE m.sender_type = 'admin'
              AND m.sender_id != 0
              AND m.template_id IS NOT NULL
            GROUP BY m.sender_id, m.template_id
        """))).all()

        if not rows:
            return {"admins": [], "templates": [], "cells": []}

        admin_ids = sorted({int(r[0]) for r in rows})
        template_ids = sorted({int(r[1]) for r in rows})

        # Resolve usernames & template titles
        usernames: dict[int, str] = {}
        if admin_ids:
            ur = (await session.execute(
                select(User.tg_id, User.username).where(User.tg_id.in_(admin_ids))
            )).all()
            usernames = {int(t): (u or "") for t, u in ur}

        titles: dict[int, str] = {}
        if template_ids:
            tr = (await session.execute(
                select(ReplyTemplate.id, ReplyTemplate.title)
                .where(ReplyTemplate.id.in_(template_ids))
            )).all()
            titles = {int(i): t for i, t in tr}

    admin_idx_map = {tg: i for i, tg in enumerate(admin_ids)}
    template_idx_map = {tid: i for i, tid in enumerate(template_ids)}

    return {
        "admins": [
            {"label": f"@{usernames[tg]}" if usernames.get(tg) else f"id:{tg}", "tg_id": tg}
            for tg in admin_ids
        ],
        "templates": [
            {"id": tid, "title": titles.get(tid, f"#{tid}")}
            for tid in template_ids
        ],
        "cells": [
            {
                "admin_idx": admin_idx_map[int(r[0])],
                "template_idx": template_idx_map[int(r[1])],
                "count": int(r[2]),
            }
            for r in rows
        ],
    }


# ==================== Answer quality: what the rating was for ====================
#
# The rating and the thing that earned it were never joined. A ticket carries a
# CSAT score, a message carries the template it was sent from, an ai_call
# carries whether its suggestion was accepted — and all three sat in separate
# reports. Joined, they answer the question a support lead actually has: which
# of our canned answers make people unhappy, and is the AI worse than us.
#
# Correlation, not proof: a template used on the hardest tickets will score
# lower for reasons that are not the template's fault. That is why the sample
# size travels with every number and why an average below MIN_RATINGS is not
# shown at all — two ratings is a coin toss, not a signal.

MIN_RATINGS_TO_JUDGE = 3


async def template_csat() -> dict[int, dict]:
    """Average CSAT per template, keyed by template id.

    A template used twice in one ticket must not count that ticket's rating
    twice, hence the distinct ticket.
    """
    async with db_session() as session:
        # Dedupe to one row per (template, ticket) FIRST. Grouping the join
        # directly makes COUNT(DISTINCT t.id) right and AVG wrong: the average
        # runs over join rows, so a template sent twice in one ticket counts
        # that ticket's rating twice while the count says otherwise.
        rows = (await session.execute(text("""
            SELECT template_id,
                   COUNT(*)          AS rated,
                   AVG(csat_rating)  AS avg_csat
            FROM (
              SELECT DISTINCT m.template_id, t.id AS ticket_id, t.csat_rating
              FROM messages m
              JOIN tickets t ON t.id = m.ticket_id
              WHERE m.template_id IS NOT NULL
                AND m.is_internal = false
                AND t.csat_rating IS NOT NULL
            )
            GROUP BY template_id
        """))).all()

    out: dict[int, dict] = {}
    for template_id, rated, avg_csat in rows:
        rated = int(rated or 0)
        out[int(template_id)] = {
            "rated": rated,
            # Withheld rather than rounded down to a comforting number.
            "avg": round(float(avg_csat), 2) if rated >= MIN_RATINGS_TO_JUDGE else None,
        }
    return out


async def ai_vs_human_csat() -> dict:
    """How tickets an AI suggestion touched are rated, against the rest.

    "Touched" means an operator accepted a suggestion on that ticket — a
    suggestion that was offered and dismissed says nothing about the answer
    the customer actually received.
    """
    async with db_session() as session:
        row = (await session.execute(text("""
            SELECT
              AVG(CASE WHEN touched THEN csat_rating END),
              SUM(CASE WHEN touched THEN 1 ELSE 0 END),
              AVG(CASE WHEN NOT touched THEN csat_rating END),
              SUM(CASE WHEN NOT touched THEN 1 ELSE 0 END)
            FROM (
              SELECT t.csat_rating,
                     EXISTS (SELECT 1 FROM ai_calls a
                              WHERE a.ticket_id = t.id AND a.accepted = true) AS touched
              FROM tickets t
              WHERE t.csat_rating IS NOT NULL
            )
        """))).one()

    ai_avg, ai_n, human_avg, human_n = row
    ai_n, human_n = int(ai_n or 0), int(human_n or 0)
    comparable = ai_n >= MIN_RATINGS_TO_JUDGE and human_n >= MIN_RATINGS_TO_JUDGE
    return {
        "ai_avg": round(float(ai_avg), 2) if ai_avg is not None and ai_n >= MIN_RATINGS_TO_JUDGE else None,
        "ai_rated": ai_n,
        "human_avg": round(float(human_avg), 2) if human_avg is not None and human_n >= MIN_RATINGS_TO_JUDGE else None,
        "human_rated": human_n,
        # Signed: negative means the AI-touched tickets are rated worse.
        "delta": round(float(ai_avg) - float(human_avg), 2) if comparable else None,
    }


async def ai_csat_by_category() -> list[dict]:
    """The same comparison per ticket category — where the AI actually helps.

    An AI that is a point worse overall may be fine everywhere except one
    category, and that is an actionable finding rather than a reason to switch
    the whole thing off.
    """
    async with db_session() as session:
        rows = (await session.execute(text("""
            SELECT category,
                   AVG(CASE WHEN touched THEN csat_rating END),
                   SUM(CASE WHEN touched THEN 1 ELSE 0 END),
                   AVG(CASE WHEN NOT touched THEN csat_rating END),
                   SUM(CASE WHEN NOT touched THEN 1 ELSE 0 END)
            FROM (
              SELECT t.category, t.csat_rating,
                     EXISTS (SELECT 1 FROM ai_calls a
                              WHERE a.ticket_id = t.id AND a.accepted = true) AS touched
              FROM tickets t
              WHERE t.csat_rating IS NOT NULL
            )
            GROUP BY category
            ORDER BY category
        """))).all()

    out = []
    for category, ai_avg, ai_n, human_avg, human_n in rows:
        ai_n, human_n = int(ai_n or 0), int(human_n or 0)
        if ai_n < MIN_RATINGS_TO_JUDGE or human_n < MIN_RATINGS_TO_JUDGE:
            continue  # nothing to compare against
        out.append({
            "category": category or EMPTY,
            "ai_avg": round(float(ai_avg), 2),
            "ai_rated": ai_n,
            "human_avg": round(float(human_avg), 2),
            "human_rated": human_n,
            "delta": round(float(ai_avg) - float(human_avg), 2),
        })
    return out


# ==================== The work queue ====================
#
# The board answers "what is going on". It does not answer "what do I do next",
# and after three replies in a row that is the only question an operator has.
# Choosing by hand twenty times a shift is work the product should be doing.
#
# Membership is the same signal the board cards carry: the ball is in our
# court. Order is the oldest wait first, in working time — which is the SLA
# order, so the queue and the colours on the board never disagree.

# Everything a customer is waiting on, oldest wait first. Callers narrow it.
_QUEUE_SQL = """
    SELECT t.id,
           (SELECT MIN(m.created_at) FROM messages m
             WHERE m.ticket_id = t.id
               AND m.is_internal = false
               AND m.sender_type = 'user'
               AND m.id > COALESCE((SELECT MAX(a.id) FROM messages a
                                     WHERE a.ticket_id = t.id
                                       AND a.is_internal = false
                                       AND a.sender_type = 'admin'), 0)
           ) AS waiting_since
      FROM tickets t
     WHERE t.status = 'active'
       AND {scope}
"""


def _queue_scope(operator_id: int | None, source: str) -> tuple[str, dict]:
    """Which tickets this operator may take.

    'open' is the default and means mine plus unclaimed: a ticket already
    assigned to a colleague is not yours to pick up, and offering it is how a
    queue starts producing collisions instead of preventing them.
    """
    if source == "all":
        return "1 = 1", {}
    if source == "unassigned":
        return "t.assigned_admin_id IS NULL", {}
    if source == "mine" and operator_id is not None:
        return "t.assigned_admin_id = :op", {"op": operator_id}
    if operator_id is None:
        return "t.assigned_admin_id IS NULL", {}
    return "(t.assigned_admin_id IS NULL OR t.assigned_admin_id = :op)", {"op": operator_id}


async def _queue_rows(operator_id: int | None, source: str) -> list[tuple[int, int]]:
    """(ticket_id, waiting_seconds) for everything owed, longest wait first."""
    scope, params = _queue_scope(operator_id, source)
    async with db_session() as session:
        rows = (await session.execute(
            text(_QUEUE_SQL.format(scope=scope)), params
        )).all()

    config = await work_hours_service.get_config()
    now = datetime.utcnow().replace(tzinfo=timezone.utc)

    out: list[tuple[int, int]] = []
    for ticket_id, since in rows:
        if since is None:
            continue  # their move, not ours
        if not isinstance(since, datetime):
            since = datetime.fromisoformat(str(since))
        waited = work_hours_service.working_seconds_between(
            config, since.replace(tzinfo=timezone.utc), now,
        )
        out.append((int(ticket_id), waited))

    out.sort(key=lambda pair: pair[1], reverse=True)
    return out


async def queue_size(operator_id: int | None = None, source: str = "open") -> int:
    """How many people are waiting on an answer from this operator."""
    return len(await _queue_rows(operator_id, source))


async def queue_next(operator_id: int | None = None, source: str = "open",
                     exclude: int | None = None) -> int | None:
    """The ticket to open now, or None when the queue is empty.

    `exclude` skips the one just finished: the reply is committed before the
    redirect, so the ticket has already left the queue — but a customer who
    answered instantly would otherwise hand the operator straight back the
    conversation they just closed out of.
    """
    for ticket_id, _ in await _queue_rows(operator_id, source):
        if ticket_id != exclude:
            return ticket_id
    return None


# ==================== Shadow mode: was it any good? ====================
#
# The barrier to switching AI on is not price, it is the fear of it writing
# something wrong to a customer. Every vendor answers "trust us"; this answers
# with the operator's own tickets.
#
# Deliberately NOT an accuracy score. A number would be believed, and there is
# no honest way to compute one: two support answers can say the same thing in
# no shared words, and share most words while meaning the opposite. What is
# offered instead is the pairs, side by side, for someone to read — plus a
# crude word overlap purely as something to sort by, labelled as such.

async def shadow_comparisons(limit: int = 40) -> list[dict]:
    """What the AI would have said, next to what the operator did say."""
    async with db_session() as session:
        rows = (await session.execute(text("""
            SELECT a.id, a.ticket_id, a.use_case, a.suggested_text, a.created_at,
                   a.tokens_in, a.tokens_out, a.latency_ms,
                   (SELECT m.text FROM messages m
                     WHERE m.ticket_id = a.ticket_id
                       AND m.sender_type = 'admin'
                       AND m.is_internal = false
                       AND m.created_at >= a.created_at
                     ORDER BY m.created_at ASC LIMIT 1) AS human_reply
              FROM ai_calls a
             WHERE a.shadow = true
               AND a.success = true
               AND a.suggested_text IS NOT NULL
             ORDER BY a.created_at DESC
             LIMIT :lim
        """), {"lim": limit})).all()

    out = []
    for (call_id, ticket_id, use_case, suggested, created_at,
         tok_in, tok_out, latency, human) in rows:
        out.append({
            "id": int(call_id),
            "ticket_id": int(ticket_id) if ticket_id else None,
            "use_case": use_case,
            "suggested": suggested or "",
            "human": human or "",
            "created_at": created_at,
            "tokens": int(tok_in or 0) + int(tok_out or 0),
            "latency_ms": int(latency or 0),
            "overlap": _word_overlap(suggested or "", human or ""),
            # No human answer yet means there is nothing to compare against —
            # not that the AI was wrong.
            "answered": bool(human),
        })
    return out


def _word_overlap(a: str, b: str) -> float | None:
    """Share of the operator's words the AI also used, 0..1.

    A blunt instrument on purpose. It is offered as a sort order, never as a
    verdict: the same answer written two ways scores low, and two opposite
    answers about the same VPN score high.
    """
    if not a or not b:
        return None
    import re
    words_a = set(re.findall(r"\w+", a.lower()))
    words_b = set(re.findall(r"\w+", b.lower()))
    if not words_b:
        return None
    return round(len(words_a & words_b) / len(words_b), 2)


async def shadow_summary() -> dict:
    """Volume and cost of the trial, so the decision has a price on it."""
    async with db_session() as session:
        row = (await session.execute(text("""
            SELECT COUNT(*),
                   COALESCE(SUM(tokens_in), 0),
                   COALESCE(SUM(tokens_out), 0),
                   MIN(created_at)
              FROM ai_calls
             WHERE shadow = true AND success = true
        """))).one()

    calls, tok_in, tok_out, since = row
    from src.services import settings_service
    price_in, price_out = await settings_service.get_ai_pricing()
    return {
        "calls": int(calls or 0),
        "tokens_in": int(tok_in or 0),
        "tokens_out": int(tok_out or 0),
        "cost_usd": round(int(tok_in or 0) / 1_000_000 * price_in
                          + int(tok_out or 0) / 1_000_000 * price_out, 2),
        "since": since,
    }
