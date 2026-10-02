"""What happens when an answer is overdue.

The clock existed already. These tests are about the policy on top of it, and
mostly about restraint again: an alert that repeats is an alert people mute,
and a muted alert is worse than none at all.
"""

from datetime import datetime, timedelta
import pytest

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


async def _another_operator() -> int:
    """A second, real operator account.

    Tests used to say `assigned_to=op + 999` for "somebody else's ticket",
    which works only because SQLite does not check foreign keys by default.
    PostgreSQL does, and so does this suite now — and a real row says what
    the test means rather than standing in for it.
    """
    from src.database.models import AdminUser

    async with db_session() as session:
        colleague = AdminUser(
            email=f"colleague-{next_tg_id()}@local",
            password_hash="not-a-real-hash",
            name="A Colleague",
            role="operator",
            is_active=True,
        )
        session.add(colleague)
        await session.flush()
        await session.commit()
        return colleague.id


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


async def test_an_overdue_ticket_is_returned_once(initialized_db):
    await _arm(return_to_pool=True)
    tid = await _overdue_ticket(minutes_ago=500, assigned_to=await _another_operator())
    bot = _Bot()
    first = await sla_service.run_once(bot)
    assert first['returned'] >= 1 and first['notified'] == 0
    assert await sla_service.run_once(bot) == {'breached':0, 'notified':0, 'returned':0}
    assert bot.sent == []


async def test_inside_the_target_nothing_happens(initialized_db):
    await _arm(target_minutes=60)
    await _overdue_ticket(minutes_ago=10)
    bot = _Bot()

    # Only tickets past the target count; ten minutes is not one of them.
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
    tid = await _overdue_ticket(minutes_ago=900, assigned_to=await _another_operator())

    result = await sla_service.run_once(None)
    assert result["returned"] >= 1

    async with db_session() as session:
        assigned = (await session.execute(
            select(Ticket.assigned_admin_id).where(Ticket.id == tid)
        )).scalar_one()
    assert assigned is None


async def test_switched_off_it_does_nothing_at_all(initialized_db):
    await _arm(notify=False, return_to_pool=False)
    await _overdue_ticket(minutes_ago=900)
    bot = _Bot()

    assert await sla_service.run_once(bot) == {"breached": 0, "notified": 0, "returned": 0}
    assert bot.sent == []


async def test_legacy_chat_notification_setting_is_ignored(initialized_db):
    await _arm(notify=True)
    await settings_service.set_value(sla_service.KEY_NOTIFY, '1')
    await _overdue_ticket(minutes_ago=900)
    bot = _Bot()
    assert await sla_service.run_once(bot) == {'breached':0, 'notified':0, 'returned':0}
    assert bot.sent == []


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
    assert await sla_service.get_policy() == {"notify": False, "return_to_pool": True}


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


async def test_poll_loop_does_not_create_a_telegram_client(monkeypatch):
    import asyncio
    from src.dashboard import bot_client
    seen = []
    def forbidden_bot():
        raise AssertionError('SLA must not depend on Telegram')
    monkeypatch.setattr(bot_client, 'get_bot', forbidden_bot)

    async def run_once():
        seen.append('pass')

    async def sleep(_):
        if len(seen) == 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(sla_service, 'run_once', run_once)
    monkeypatch.setattr(asyncio, 'sleep', sleep)
    with pytest.raises(asyncio.CancelledError):
        await sla_service.poll_loop()
    assert seen == ['pass', 'pass']
