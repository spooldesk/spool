"""Report queries: period boundaries, operator/team breakdowns, presets."""

from datetime import datetime, timedelta

from src.dashboard import report_queries
from src.dashboard.routes.reports import _delta_pct, _period_bounds
from src.database.db import db_session
from src.database.models import Message, Team, Ticket, User
from src.services import admin_user_service

# A stable, far-future window so rows from other test modules (created "now")
# can't leak into these assertions.
BASE = datetime(2031, 3, 10, 12, 0, 0)   # inside the window
FROM = datetime(2031, 3, 9)
TO = datetime(2031, 3, 16)


async def _seed(initialized_db):
    async with db_session() as session:
        from sqlalchemy import select
        if (await session.execute(
            select(Team).where(Team.name == "report-team"))
        ).scalar_one_or_none():
            return
        team = Team(name="report-team")
        session.add(team)
        await session.flush()
        user = User(tg_id=960001, username="rep_user", team_id=team.id,
                    is_verified=True, is_active=True)
        session.add(user)
        await session.flush()

    op = await admin_user_service.create_admin(
        email="rep-op@t.dev", password="reportpass1", name="Отчётный", role="agent")

    async with db_session() as session:
        from sqlalchemy import select
        team = (await session.execute(
            select(Team).where(Team.name == "report-team"))).scalar_one()
        user = (await session.execute(
            select(User).where(User.tg_id == 960001))).scalar_one()

        # Closed inside the window, rated, assigned, resolved in ~2h
        t1 = Ticket(user_id=user.id, team_id=team.id, category="VPN",
                    status="closed", created_at=BASE,
                    closed_at=BASE + timedelta(hours=2),
                    csat_rating=5, csat_rated_at=BASE + timedelta(hours=3),
                    assigned_admin_id=op.id)
        # Closed by AI inside the window
        t2 = Ticket(user_id=user.id, team_id=team.id, category="VPN",
                    status="closed", created_at=BASE + timedelta(hours=1),
                    closed_at=BASE + timedelta(hours=1, minutes=10),
                    admin_categories=["Решено ИИ"])
        # Outside the window entirely — must not be counted
        t3 = Ticket(user_id=user.id, team_id=team.id, category="VPN",
                    status="closed", created_at=BASE - timedelta(days=30),
                    closed_at=BASE - timedelta(days=30) + timedelta(hours=1))
        session.add_all([t1, t2, t3])
        await session.flush()
        session.add_all([
            Message(ticket_id=t1.id, sender_type="user", sender_id=960001,
                    text="vpn упал", created_at=BASE),
            Message(ticket_id=t1.id, sender_type="admin", sender_id=1,
                    text="перезапустите туннель", created_at=BASE + timedelta(minutes=30)),
        ])
        await session.commit()


async def test_summary_counts_only_period(initialized_db):
    await _seed(initialized_db)
    s = await report_queries.report_summary(FROM, TO)
    assert s["created"] == 2          # t1 + t2 (t3 вне окна)
    assert s["closed"] == 2
    assert s["csat_count"] == 1 and s["csat_avg"] == 5.0
    assert s["ai_closed"] == 1
    assert s["ai_closed_pct"] == 50.0
    assert s["avg_first_response_min"] is not None
    assert 25 <= s["avg_first_response_min"] <= 35   # ~30 мин


async def test_operator_breakdown(initialized_db):
    await _seed(initialized_db)
    ops = await report_queries.report_by_operator(FROM, TO)
    row = next((o for o in ops if o["label"] == "Отчётный"), None)
    assert row is not None
    assert row["closed"] == 1
    assert row["csat_avg"] == 5.0 and row["csat_count"] == 1
    assert row["avg_close_hours"] is not None and 1.5 <= row["avg_close_hours"] <= 2.5


async def test_team_breakdown(initialized_db):
    await _seed(initialized_db)
    teams = await report_queries.report_by_team(FROM, TO)
    row = next((t for t in teams if t["name"] == "report-team"), None)
    assert row is not None
    assert row["created"] == 2 and row["closed"] == 2
    assert row["csat_avg"] == 5.0


def test_period_bounds_presets():
    from datetime import date
    today = date(2031, 3, 12)  # Wednesday
    df, dt, p = _period_bounds("last_week", None, None, today=today)
    assert p == "last_week"
    assert df == datetime(2031, 3, 3) and dt == datetime(2031, 3, 10)  # Mon..Mon

    df, dt, p = _period_bounds("this_month", None, None, today=today)
    assert df == datetime(2031, 3, 1) and dt == datetime(2031, 3, 13)

    df, dt, p = _period_bounds("last_month", None, None, today=today)
    assert df == datetime(2031, 2, 1) and dt == datetime(2031, 3, 1)

    # Custom inclusive dates → exclusive upper bound
    df, dt, p = _period_bounds(None, "2031-03-01", "2031-03-07", today=today)
    assert p == "custom" and dt == datetime(2031, 3, 8)

    # Garbage falls back to the default preset
    df, dt, p = _period_bounds("nonsense", "bad", "worse", today=today)
    assert p == "7d"


def test_delta_pct():
    assert _delta_pct(120, 100) == 20.0
    assert _delta_pct(80, 100) == -20.0
    assert _delta_pct(None, 100) is None
    assert _delta_pct(100, 0) is None
    assert _delta_pct(100, None) is None
