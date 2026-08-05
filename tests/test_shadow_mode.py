"""Shadow mode: the AI works and nobody sees it.

One property matters more than all the others here, and every other test in
this file is subordinate to it: while the trial is running, nothing the AI
produces reaches a customer or an operator. If that ever breaks, the feature
has done the exact thing it was built to protect people from.
"""

from datetime import datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select

from src.dashboard import queries
from src.dashboard.app import app
from src.database.db import db_session
from src.database.models import AICall, Message, Team, Ticket, User
from src.services import settings_service
from tests.conftest import next_tg_id


def _login(c: TestClient) -> str:
    c.get("/login")
    token = c.cookies.get("supbot_csrf")
    r = c.post("/login", data={"email": "admin@local", "password": "test-password-123",
                               "next": "/", "csrf_token": token}, follow_redirects=False)
    assert r.status_code == 303
    return token


class _Bot:
    """Anything that lands here escaped the trial."""

    def __init__(self):
        self.sent: list[tuple] = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text))


async def _ticket_with_shadow_call(*, suggested: str, human: str | None = None) -> int:
    async with db_session() as session:
        team = Team(name=f"sh-{next_tg_id()}")
        session.add(team)
        await session.flush()
        u = User(tg_id=next_tg_id(), username="sh_user", team_id=team.id,
                 is_verified=True, is_active=True)
        session.add(u)
        await session.flush()
        t = Ticket(user_id=u.id, team_id=team.id, category="Network", status="active")
        session.add(t)
        await session.flush()
        drafted = datetime.utcnow() - timedelta(minutes=10)
        session.add(AICall(use_case="suggest_from_history", ticket_id=t.id, model="test",
                           success=True, shadow=True, suggested_text=suggested,
                           tokens_in=400, tokens_out=90, latency_ms=1200,
                           created_at=drafted))
        if human is not None:
            session.add(Message(ticket_id=t.id, sender_type="admin", sender_id=1,
                                text=human, created_at=drafted + timedelta(minutes=3)))
        await session.commit()
        return t.id


async def test_nothing_reaches_the_customer_while_the_trial_runs(initialized_db):
    """The property the whole feature exists for."""
    from src.ai import deflect

    await settings_service.set_ai_shadow_mode(True, updated_by=0)
    bot = _Bot()

    async def _confident(ticket_id, user_text):
        return deflect.DeflectResult(answer="Restart the VPN client.", ai_call_id=1)

    original = deflect.try_deflect_for_ticket
    deflect.try_deflect_for_ticket = _confident
    try:
        await deflect.deflect_and_offer(bot, 1, "vpn broken", 555, "en")
    finally:
        deflect.try_deflect_for_ticket = original

    assert bot.sent == [], "an answer escaped to the customer during the trial"


async def test_operators_are_not_interrupted_either(initialized_db):
    """A suggestion posted into the operators' chat is still the AI being
    used, and the point of the trial is that it is only being watched."""
    from src.ai import notifier
    from src.database.models import ReplyTemplate

    await settings_service.set_ai_shadow_mode(True, updated_by=0)
    bot = _Bot()

    async with db_session() as session:
        team = Team(name=f"sht-{next_tg_id()}")
        session.add(team)
        await session.flush()
        tpl = ReplyTemplate(team_id=team.id, title="VPN", body="Reinstall", created_by=1)
        u = User(tg_id=next_tg_id(), username="x", team_id=team.id,
                 is_verified=True, is_active=True)
        session.add_all([tpl, u])
        await session.flush()
        t = Ticket(user_id=u.id, team_id=team.id, category="Network", status="active")
        session.add(t)
        await session.flush()
        ticket, template = t, tpl
        await session.commit()

        await notifier._post_template_suggestion(bot, ticket, template, None)
        await notifier._post_history_suggestion(bot, ticket, "a draft", None)

    assert bot.sent == [], "a suggestion was posted while the trial was running"


async def test_with_the_trial_off_the_answer_goes_out(initialized_db):
    """The gate has to be a gate, not a wall — otherwise this test passes for
    the wrong reason and the one above proves nothing."""
    from src.ai import deflect

    await settings_service.set_ai_shadow_mode(False, updated_by=0)
    bot = _Bot()

    async def _confident(ticket_id, user_text):
        return deflect.DeflectResult(answer="Restart the VPN client.", ai_call_id=1)

    original = deflect.try_deflect_for_ticket
    deflect.try_deflect_for_ticket = _confident
    try:
        await deflect.deflect_and_offer(bot, 1, "vpn broken", 555, "en")
    finally:
        deflect.try_deflect_for_ticket = original

    assert len(bot.sent) == 1, "the gate is stuck shut"


async def test_the_pair_is_the_draft_and_the_reply_that_followed(initialized_db):
    await _ticket_with_shadow_call(
        suggested="Reissue the VPN profile and reconnect.",
        human="I reissued your VPN profile, try connecting now.",
    )
    rows = await queries.shadow_comparisons(limit=50)
    assert rows, "the trial recorded nothing"

    row = rows[0]
    assert row["suggested"] and row["human"]
    assert row["answered"] is True


async def test_no_reply_yet_is_not_the_ai_being_wrong(initialized_db):
    await _ticket_with_shadow_call(suggested="Try turning it off and on.")
    rows = await queries.shadow_comparisons(limit=50)
    unanswered = [r for r in rows if not r["answered"]]
    assert unanswered, "an unanswered draft should still be listed"
    assert unanswered[0]["overlap"] is None, "there is nothing to overlap with"


async def test_live_calls_stay_out_of_the_trial(initialized_db):
    """Mixing them would make both numbers meaningless: a shadow suggestion
    cannot be accepted, and a live one was not part of the experiment."""
    async with db_session() as session:
        session.add(AICall(use_case="suggest_template", model="test", success=True,
                           shadow=False, suggested_text="a live suggestion"))
        await session.commit()

    rows = await queries.shadow_comparisons(limit=100)
    assert "a live suggestion" not in [r["suggested"] for r in rows]


async def test_overlap_is_a_sort_order_not_a_verdict(initialized_db):
    """Documented as crude, and it is: identical wording scores 1.0 while the
    same meaning in other words scores near zero. That is exactly why the page
    shows the pairs and not this number on its own."""
    same = queries._word_overlap("reissue the vpn profile", "reissue the vpn profile")
    assert same == 1.0

    reworded = queries._word_overlap("issued you a fresh certificate",
                                     "reissue the vpn profile")
    assert reworded is not None and reworded < 0.5


async def test_the_page_reports_what_it_cost_and_what_escaped(initialized_db):
    await _ticket_with_shadow_call(suggested="draft", human="reply")
    await settings_service.set_ai_shadow_mode(True, updated_by=0)

    with TestClient(app, base_url="http://testserver") as c:
        _login(c)
        page = c.get("/ai/shadow")

    assert page.status_code == 200
    assert "Shadow trial" in page.text
    assert "Reached a customer" in page.text
    assert "The AI would have written" in page.text


async def test_the_trial_can_be_started_and_stopped_from_settings(initialized_db):
    with TestClient(app, base_url="http://testserver") as c:
        token = _login(c)
        assert 'action="/manage/ai/shadow"' in c.get("/manage/ai").text

        c.post("/manage/ai/shadow", data={"shadow": "1", "csrf_token": token},
               follow_redirects=False)
        assert await settings_service.get_ai_shadow_mode() is True

        c.post("/manage/ai/shadow", data={"csrf_token": token},
               follow_redirects=False)
        assert await settings_service.get_ai_shadow_mode() is False


async def test_new_calls_are_stamped_with_the_mode_they_ran_in(initialized_db):
    """Without the stamp the accept rate lies: a shadow suggestion cannot be
    accepted, so it would read as operators rejecting everything."""
    from pathlib import Path

    for module in ("deflect.py", "suggest_from_history.py", "suggest_template.py"):
        src = Path("src/ai") / module
        text = src.read_text(encoding="utf-8")
        assert "shadow=_shadow" in text, f"{module} logs calls without the mode"
