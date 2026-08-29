"""Joining the rating to the thing that earned it.

A ticket carries a CSAT score, a message carries the template it was sent from,
an ai_call carries whether its suggestion was accepted — and all three sat in
separate reports. These tests are mostly about the honesty of the join: small
samples must not be dressed up as findings.
"""

from datetime import datetime, timedelta

from fastapi.testclient import TestClient

from src.dashboard import queries
from src.dashboard.app import app
from src.database.db import db_session
from src.database.models import AICall, Message, ReplyTemplate, Team, Ticket, User
from tests.conftest import next_tg_id


def _login(c: TestClient) -> str:
    c.get("/login")
    token = c.cookies.get("supbot_csrf")
    r = c.post("/login", data={"email": "admin@local", "password": "test-password-123",
                               "next": "/", "csrf_token": token}, follow_redirects=False)
    assert r.status_code == 303
    return token


async def _rated_ticket(*, rating: int, template_id: int | None = None,
                        ai_accepted: bool | None = None, category: str = "Network") -> int:
    """A closed, rated ticket, optionally answered from a template and/or with
    an AI suggestion the operator accepted."""
    async with db_session() as session:
        team = Team(name=f"q-{next_tg_id()}")
        session.add(team)
        await session.flush()
        u = User(tg_id=next_tg_id(), username="q_user", team_id=team.id,
                 is_verified=True, is_active=True)
        session.add(u)
        await session.flush()
        t = Ticket(user_id=u.id, team_id=team.id, category=category, status="closed",
                   closed_at=datetime.utcnow(), csat_rating=rating)
        session.add(t)
        await session.flush()
        session.add(Message(ticket_id=t.id, sender_type="admin", sender_id=1,
                            text="answered", template_id=template_id))
        if ai_accepted is not None:
            session.add(AICall(use_case="suggest_template", ticket_id=t.id,
                               model="test", success=True, accepted=ai_accepted))
        await session.commit()
        return t.id


async def _template(title: str) -> int:
    async with db_session() as session:
        team = Team(name=f"qt-{next_tg_id()}")
        session.add(team)
        await session.flush()
        tpl = ReplyTemplate(team_id=team.id, title=title, body="body", created_by=1)
        session.add(tpl)
        await session.flush()
        tid = tpl.id
        await session.commit()
        return tid


async def test_a_templates_rating_is_the_average_of_its_tickets(initialized_db):
    tpl = await _template("VPN reset")
    for rating in (5, 4, 3):
        await _rated_ticket(rating=rating, template_id=tpl)

    stats = (await queries.template_csat())[tpl]
    assert stats["rated"] == 3
    assert stats["avg"] == 4.0


async def test_two_ratings_are_not_a_finding(initialized_db):
    """An average of two is a coin toss. Withhold the number, keep the count —
    saying '4.5' from two ratings invites a decision the data cannot support."""
    tpl = await _template("Too new to judge")
    await _rated_ticket(rating=5, template_id=tpl)
    await _rated_ticket(rating=2, template_id=tpl)

    stats = (await queries.template_csat())[tpl]
    assert stats["rated"] == 2
    assert stats["avg"] is None


async def test_one_ticket_counts_once_even_if_the_template_was_used_twice(initialized_db):
    """Sending the same canned reply twice must not double that ticket's vote."""
    tpl = await _template("Sent twice")
    for rating in (5, 5):
        await _rated_ticket(rating=rating, template_id=tpl)
    tid = await _rated_ticket(rating=1, template_id=tpl)

    async with db_session() as session:
        session.add(Message(ticket_id=tid, sender_type="admin", sender_id=1,
                            text="again", template_id=tpl))
        await session.commit()

    stats = (await queries.template_csat())[tpl]
    assert stats["rated"] == 3, "the repeat must not become a fourth vote"
    assert stats["avg"] == round((5 + 5 + 1) / 3, 2)


async def test_an_unused_template_has_no_entry(initialized_db):
    tpl = await _template("Never used")
    assert tpl not in await queries.template_csat()


async def test_ai_is_compared_against_the_tickets_it_did_not_touch(initialized_db):
    for _ in range(3):
        await _rated_ticket(rating=3, ai_accepted=True)
    for _ in range(3):
        await _rated_ticket(rating=5, ai_accepted=None)

    q = await queries.ai_vs_human_csat()
    assert q["ai_rated"] >= 3 and q["human_rated"] >= 3
    assert q["ai_avg"] < q["human_avg"]
    assert q["delta"] < 0, "a worse AI must produce a negative number, not a hidden one"


async def test_a_dismissed_suggestion_is_not_an_ai_answer(initialized_db):
    """Offered and rejected says nothing about what the customer received."""
    tid = await _rated_ticket(rating=5, ai_accepted=False)

    async with db_session() as session:
        from sqlalchemy import text as sql
        touched = (await session.execute(sql("""
            SELECT EXISTS (SELECT 1 FROM ai_calls a
                            WHERE a.ticket_id = :t AND a.accepted = true)
        """), {"t": tid})).scalar_one()
    assert not touched


async def test_a_number_appears_only_when_the_sample_earns_it(initialized_db):
    """Stated as an invariant rather than a scenario: these are aggregates over
    the whole database, so a test that seeds two tickets and expects to see
    exactly two only passes when it runs first."""
    await _rated_ticket(rating=5, ai_accepted=True)
    q = await queries.ai_vs_human_csat()

    enough = queries.MIN_RATINGS_TO_JUDGE
    assert (q["ai_avg"] is None) == (q["ai_rated"] < enough)
    assert (q["human_avg"] is None) == (q["human_rated"] < enough)
    assert (q["delta"] is None) == (q["ai_avg"] is None or q["human_avg"] is None)


async def test_a_category_needs_both_sides_to_appear(initialized_db):
    for _ in range(3):
        await _rated_ticket(rating=4, ai_accepted=True, category="OnlyAI")

    rows = await queries.ai_csat_by_category()
    assert "OnlyAI" not in [r["category"] for r in rows]


async def test_the_pages_render_the_numbers(initialized_db):
    tpl = await _template("Rendered")
    for rating in (5, 4, 3):
        await _rated_ticket(rating=rating, template_id=tpl)
    for _ in range(3):
        await _rated_ticket(rating=2, ai_accepted=True)

    with TestClient(app, base_url="http://testserver") as c:
        _login(c)
        templates_page = c.get("/templates")
        ai_page = c.get("/ai")

    assert templates_page.status_code == 200 and ai_page.status_code == 200
    assert "CSAT" in templates_page.text
    assert "Answer quality" in ai_page.text
