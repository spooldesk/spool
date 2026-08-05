"""What happens when an answer is overdue.

The clock existed already. These tests are about the policy on top of it, and
mostly about restraint again: an alert that repeats is an alert people mute,
and a muted alert is worse than none at all.
"""

from datetime import datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select

from src.dashboard.app import app
from src.database.db import db_session
from src.database.models import Message, Team, Ticket, User
from src.services import settings_service, sla_service, work_hours_service, workflow_service
from tests.conftest import next_tg_id


def _login(c: TestClient) -> str:
    c.get("/login")
    token = c.cookies.get("supbot_csrf")
    r = c.post("/login", data={"email": "admin@local", "password": "test-password-123",
                               "next": "/", "csrf_token": token}, follow_redirects=False)
    assert r.status_code == 303
    return token


class _Bot:
    """Records what would have reached the operators' chat."""

    def __init__(self, fail: bool = False):
        self.sent: list[str] = []
        self.fail = fail

    async def send_message(self, chat_id, text, **kw):
        if self.fail:
            raise RuntimeError("Telegram said no")
        self.sent.append(text)


async def _overdue_ticket(*, minutes_ago: int, assigned_to: int | None = None) -> int:
    async with db_session() as session:
        team = Team(name=f"sla-{next_tg_id()}")
        session.add(team)
        await session.flush()
        u = User(tg_id=next_tg_id(), username="sla_user", team_id=team.id,
                 is_verified=True, is_active=True)
        session.add(u)
        await session.flush()
        t = Ticket(user_id=u.id, team_id=team.id, category="Network", status="active",
                   assigned_admin_id=assigned_to)
        session.add(t)
        await session.flush()
        session.add(Message(ticket_id=t.id, sender_type="user", sender_id=u.tg_id,
                            text="still waiting",
                            created_at=datetime.utcnow() - timedelta(minutes=minutes_ago)))
        await session.commit()
        return t.id


async def _arm(*, notify=True, return_to_pool=False, target_minutes=60):
    await settings_service.set_value(work_hours_service.KEY_SLA_MINUTES,
                                     str(target_minutes))
    await sla_service.set_policy(notify=notify, return_to_pool=return_to_pool,
                                 updated_by=0)


async def test_an_overdue_ticket_is_announced_once(initialized_db):
    """The whole point of the record on the ticket: this runs every five
    minutes, and an alert that repeats every five minutes gets muted."""
    await _arm()
    tid = await _overdue_ticket(minutes_ago=500)
    bot = _Bot()

    first = await sla_service.run_once(bot)
    second = await sla_service.run_once(bot)

    assert first["notified"] >= 1
    assert any(f"#{tid}" in m for m in bot.sent)
    assert second["breached"] == 0, "the second pass announced it again"
    assert len(bot.sent) == first["notified"]


async def test_inside_the_target_nothing_happens(initialized_db):
    await _arm(target_minutes=60)
    await _overdue_ticket(minutes_ago=10)
    bot = _Bot()

    # Only tickets past twice the target count; ten minutes is not one of them.
    await sla_service.run_once(bot)
    assert all("still waiting" not in m for m in bot.sent)


async def test_a_ticket_waiting_on_the_customer_never_breaches(initialized_db):
    """They owe us nothing, so nothing is overdue."""
    await _arm()
    tid = await _overdue_ticket(minutes_ago=900)
    async with db_session() as session:
        session.add(Message(ticket_id=tid, sender_type="admin", sender_id=1,
                            text="answered", created_at=datetime.utcnow()))
        await session.commit()

    breaches = await sla_service.find_new_breaches()
    assert tid not in [b["id"] for b in breaches]


async def test_returning_to_the_pool_unassigns(initialized_db):
    """On a small team that is what escalation means: whoever claimed it has
    not got to it, so stop hiding it from everyone else."""
    await _arm(notify=False, return_to_pool=True)
    tid = await _overdue_ticket(minutes_ago=900, assigned_to=4242)

    result = await sla_service.run_once(None)
    assert result["returned"] >= 1

    async with db_session() as session:
        assigned = (await session.execute(
            select(Ticket.assigned_admin_id).where(Ticket.id == tid)
        )).scalar_one()
    assert assigned is None


async def test_answering_arms_it_again(initialized_db):
    """If the customer comes back and is made to wait again, that is a new
    breach and deserves announcing rather than being swallowed as a repeat."""
    await _arm()
    tid = await _overdue_ticket(minutes_ago=900)
    bot = _Bot()
    await sla_service.run_once(bot)
    assert await sla_service.run_once(bot) == {"breached": 0, "notified": 0, "returned": 0}

    await workflow_service.advance_on_admin_reply(tid)

    async with db_session() as session:
        marked = (await session.execute(
            select(Ticket.sla_breached_at).where(Ticket.id == tid)
        )).scalar_one()
    assert marked is None, "a reply must clear the record"


async def test_switched_off_it_does_nothing_at_all(initialized_db):
    await _arm(notify=False, return_to_pool=False)
    await _overdue_ticket(minutes_ago=900)
    bot = _Bot()

    assert await sla_service.run_once(bot) == {"breached": 0, "notified": 0, "returned": 0}
    assert bot.sent == []


async def test_one_failed_alert_does_not_stop_the_others(initialized_db):
    """Telegram refusing one message must not cost the rest of the pass."""
    await _arm()
    for _ in range(3):
        await _overdue_ticket(minutes_ago=900)

    result = await sla_service.run_once(_Bot(fail=True))
    assert result["breached"] >= 3
    assert result["notified"] == 0, "every send failed, and it kept going"


async def test_the_policy_is_editable_from_settings(initialized_db):
    with TestClient(app, base_url="http://testserver") as c:
        token = _login(c)
        page = c.get("/manage/settings")
        assert 'action="/manage/settings/sla"' in page.text

        r = c.post("/manage/settings/sla",
                   data={"sla_minutes": "45", "notify": "1",
                         "return_to_pool": "1", "csrf_token": token},
                   follow_redirects=False)
        assert r.status_code == 303

    assert await work_hours_service.get_sla_minutes() == 45
    assert await sla_service.get_policy() == {"notify": True, "return_to_pool": True}


async def test_a_nonsense_target_falls_back(initialized_db):
    with TestClient(app, base_url="http://testserver") as c:
        token = _login(c)
        c.post("/manage/settings/sla",
               data={"sla_minutes": "zero", "csrf_token": token},
               follow_redirects=False)
        assert await work_hours_service.get_sla_minutes() > 0

        c.post("/manage/settings/sla",
               data={"sla_minutes": "-10", "csrf_token": token},
               follow_redirects=False)
        assert await work_hours_service.get_sla_minutes() > 0
