"""The savings report.

This is a document someone takes to their manager, so the tests are less about
arithmetic than about restraint: an inflated hour here costs the person who
presented it more than the feature is worth.
"""

from datetime import datetime, timedelta

from fastapi.testclient import TestClient

from src.dashboard import report_queries
from src.dashboard.app import app
from src.database.db import db_session
from src.database.models import AICall, Message, Team, Ticket, User
from src.services import settings_service
from src.services.close_reason_service import AI_VALUE
from tests.conftest import next_tg_id

WINDOW_FROM = datetime.utcnow() - timedelta(days=7)
WINDOW_TO = datetime.utcnow() + timedelta(days=1)


def _login(c: TestClient) -> str:
    c.get("/login")
    token = c.cookies.get("supbot_csrf")
    r = c.post("/login", data={"email": "admin@local", "password": "test-password-123",
                               "next": "/", "csrf_token": token}, follow_redirects=False)
    assert r.status_code == 303
    return token


async def _ai_closed_ticket() -> int:
    """A ticket the AI answered and closed with nobody opening it."""
    async with db_session() as session:
        team = Team(name=f"sv-{next_tg_id()}")
        session.add(team)
        await session.flush()
        u = User(tg_id=next_tg_id(), username="sv_user", team_id=team.id,
                 is_verified=True, is_active=True)
        session.add(u)
        await session.flush()
        opened = datetime.utcnow() - timedelta(hours=2)
        t = Ticket(user_id=u.id, team_id=team.id, category="Network", status="closed",
                   created_at=opened, closed_at=datetime.utcnow(),
                   admin_categories=[AI_VALUE])
        session.add(t)
        await session.flush()
        session.add(Message(ticket_id=t.id, sender_type="admin", sender_id=0,
                            text="answered by AI", created_at=datetime.utcnow()))
        session.add(AICall(use_case="deflect", ticket_id=t.id, model="test",
                           success=True, accepted=True, tokens_in=1000, tokens_out=200))
        await session.commit()
        return t.id


async def _accepted_suggestion(tokens_in: int = 500, tokens_out: int = 100) -> None:
    """An operator took a draft the AI wrote — the writing is saved, the rest
    of the ticket still happened."""
    async with db_session() as session:
        session.add(AICall(use_case="suggest_from_history", model="test",
                           success=True, accepted=True,
                           tokens_in=tokens_in, tokens_out=tokens_out))
        await session.commit()


async def test_an_accepted_draft_is_not_worth_a_whole_ticket(initialized_db):
    """The failure mode this whole feature has to avoid. Counting assists at
    full handling time is how a report claims forty hours from thirty drafts
    and stops being believed."""
    await settings_service.set_savings_assumptions(
        price_in=0.30, price_out=2.50, assist_minutes=3, updated_by=0)
    for _ in range(10):
        await _accepted_suggestion()

    s = await report_queries.ai_savings(WINDOW_FROM, WINDOW_TO)
    assert s["assisted"] >= 10
    # Ten drafts at three minutes is half an hour, not ten tickets.
    assert s["assisted_hours"] == round(s["assisted"] * 3 / 60, 1)
    if s["avg_handle_hours"]:
        assert s["assisted_hours"] < s["assisted"] * s["avg_handle_hours"]


async def test_deflection_is_not_counted_twice(initialized_db):
    """A deflected ticket is already a whole ticket saved; its accepted
    ai_call must not also appear as an assist."""
    before = await report_queries.ai_savings(WINDOW_FROM, WINDOW_TO)
    await _ai_closed_ticket()
    after = await report_queries.ai_savings(WINDOW_FROM, WINDOW_TO)

    assert after["deflected"] == before["deflected"] + 1
    assert after["assisted"] == before["assisted"], "deflection leaked into assists"


async def test_cost_follows_the_price_you_set(initialized_db):
    await _accepted_suggestion(tokens_in=1_000_000, tokens_out=1_000_000)

    await settings_service.set_savings_assumptions(
        price_in=1.0, price_out=1.0, assist_minutes=3, updated_by=0)
    cheap = await report_queries.ai_savings(WINDOW_FROM, WINDOW_TO)

    await settings_service.set_savings_assumptions(
        price_in=2.0, price_out=2.0, assist_minutes=3, updated_by=0)
    dear = await report_queries.ai_savings(WINDOW_FROM, WINDOW_TO)

    assert dear["cost_usd"] > cheap["cost_usd"]
    assert abs(dear["cost_usd"] - cheap["cost_usd"] * 2) < 0.02


async def test_the_assumptions_come_back_with_the_answer(initialized_db):
    """They are printed next to the result. A number whose inputs the reader
    cannot see is worth nothing in a meeting."""
    await settings_service.set_savings_assumptions(
        price_in=0.42, price_out=1.23, assist_minutes=7, updated_by=0)

    s = await report_queries.ai_savings(WINDOW_FROM, WINDOW_TO)
    assert s["price_in"] == 0.42
    assert s["price_out"] == 1.23
    assert s["assist_minutes"] == 7.0


async def test_a_broken_price_falls_back_instead_of_crashing(initialized_db):
    await settings_service.set_value("ai_price_per_1m_in", "not a number")
    await settings_service.set_value("ai_minutes_per_assist", "-5")

    price_in, _ = await settings_service.get_ai_pricing()
    assert price_in == 0.30
    assert await settings_service.get_ai_assist_minutes() == 3.0


async def test_no_measured_handling_time_means_no_hours_claimed(initialized_db):
    """Rather than substituting an industry average nobody here has verified."""
    empty_from = datetime(2020, 1, 1)
    empty_to = datetime(2020, 1, 2)

    s = await report_queries.ai_savings(empty_from, empty_to)
    assert s["deflected"] == 0
    assert s["deflected_hours"] is None
    assert s["total_hours"] is None


async def test_the_report_shows_the_rating_cost_beside_the_saving(initialized_db):
    """Without it the page is a sales sheet: the reader is entitled to see
    what the saved hours cost in satisfaction."""
    with TestClient(app, base_url="http://testserver") as c:
        _login(c)
        page = c.get("/reports").text

    assert "What the AI cost, and what it saved" in page
    assert "Rating cost" in page
    assert "Assumes" in page, "the assumptions must be printed on the report"


async def test_the_assumptions_can_actually_be_changed_from_the_page(initialized_db):
    """The report links here saying "change these" — so the form behind that
    link has to exist and has to work, or the promise is decoration."""
    with TestClient(app, base_url="http://testserver") as c:
        token = _login(c)
        page = c.get("/manage/ai")
        assert 'action="/manage/ai/savings"' in page.text
        for field in ("price_in", "price_out", "assist_minutes"):
            assert f'name="{field}"' in page.text

        r = c.post("/manage/ai/savings",
                   data={"price_in": "1.25", "price_out": "9.75",
                         "assist_minutes": "8", "csrf_token": token},
                   follow_redirects=False)
        assert r.status_code == 303

    assert await settings_service.get_ai_pricing() == (1.25, 9.75)
    assert await settings_service.get_ai_assist_minutes() == 8.0

    s = await report_queries.ai_savings(WINDOW_FROM, WINDOW_TO)
    assert s["assist_minutes"] == 8.0
    assert s["assisted_hours"] == round(s["assisted"] * 8 / 60, 1)


async def test_a_negative_assumption_cannot_invert_the_report(initialized_db):
    """A typed minus must not turn a saving into a loss on a page someone is
    about to present."""
    with TestClient(app, base_url="http://testserver") as c:
        token = _login(c)
        c.post("/manage/ai/savings",
               data={"price_in": "-5", "price_out": "-5",
                     "assist_minutes": "-30", "csrf_token": token},
               follow_redirects=False)

    price_in, price_out = await settings_service.get_ai_pricing()
    assert price_in >= 0 and price_out >= 0
    assert await settings_service.get_ai_assist_minutes() >= 0

    s = await report_queries.ai_savings(WINDOW_FROM, WINDOW_TO)
    assert s["cost_usd"] >= 0
    assert s["assisted_hours"] >= 0
