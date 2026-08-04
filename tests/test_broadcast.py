"""Broadcast: audience selection, delivery accounting, pacing, and the gate.

This is the only action in the dashboard that reaches everyone at once and
cannot be undone, so the tests are mostly about it NOT doing more than asked.
"""

import asyncio

import pytest
from aiogram.exceptions import TelegramForbiddenError, TelegramNotFound, TelegramRetryAfter
from fastapi.testclient import TestClient
from sqlalchemy import select

from src.dashboard.app import app
from src.database.db import db_session
from src.database.models import Team, User
from src.services import broadcast_service as bc
from tests.conftest import next_tg_id


class FakeBot:
    """Records what was sent; can be told to fail for specific chats."""

    def __init__(self, blocked=(), missing=(), retry_after=()):
        self.sent: list[tuple[int, str]] = []
        self.blocked = set(blocked)
        self.missing = set(missing)
        self.retry_after = dict(retry_after)   # chat_id -> times to raise
        self.attempts: dict[int, int] = {}

    async def send_message(self, chat_id: int, text: str, **kw):
        self.attempts[chat_id] = self.attempts.get(chat_id, 0) + 1
        if chat_id in self.blocked:
            raise TelegramForbiddenError(method=None, message="blocked")
        if chat_id in self.missing:
            raise TelegramNotFound(method=None, message="not found")
        left = self.retry_after.get(chat_id, 0)
        if left:
            self.retry_after[chat_id] = left - 1
            raise TelegramRetryAfter(method=None, message="flood", retry_after=0)
        self.sent.append((chat_id, text))


@pytest.fixture(autouse=True)
def _reset_state():
    bc._last = None
    yield
    bc._last = None


@pytest.fixture(autouse=True)
def _fast_sends(monkeypatch):
    """Tests should not spend real seconds pacing sends."""
    monkeypatch.setattr(bc, "_SEND_INTERVAL_SEC", 0)


_IDS: dict = {}


async def _seed(initialized_db) -> dict:
    """Two teams. Alpha: 2 reachable. Beta: 1 reachable + 1 blocked + 1 unverified."""
    if _IDS:
        return _IDS

    async with db_session() as session:
        existing = (await session.execute(
            select(Team).where(Team.name == "bc-alpha")
        )).scalar_one_or_none()
        if existing:
            beta = (await session.execute(
                select(Team).where(Team.name == "bc-beta"))).scalar_one()
            reachable = sorted(
                u.tg_id for u in (await session.execute(
                    select(User).where(User.team_id == existing.id))).scalars().all()
            )
            _IDS.update({"alpha": existing.id, "beta": beta.id,
                         "alpha_tg": reachable, "beta_tg": _IDS.get("beta_tg", [])})
            return _IDS

        alpha, beta = Team(name="bc-alpha"), Team(name="bc-beta")
        session.add_all([alpha, beta])
        await session.flush()

        a1, a2, b1 = next_tg_id(), next_tg_id(), next_tg_id()
        session.add_all([
            User(tg_id=a1, username=f"bc_{a1}", team_id=alpha.id,
                 is_verified=True, is_active=True),
            User(tg_id=a2, username=f"bc_{a2}", team_id=alpha.id,
                 is_verified=True, is_active=True),
            User(tg_id=b1, username=f"bc_{b1}", team_id=beta.id,
                 is_verified=True, is_active=True),
            # inactive => blocked by an operator, must never receive a broadcast
            User(tg_id=next_tg_id(), username="bc_blocked", team_id=beta.id,
                 is_verified=True, is_active=False),
            # never finished registration
            User(tg_id=next_tg_id(), username="bc_unverified", team_id=beta.id,
                 is_verified=False, is_active=True),
        ])
        await session.commit()
        _IDS.update({"alpha": alpha.id, "beta": beta.id,
                     "alpha_tg": [a1, a2], "beta_tg": [b1]})
        return _IDS


async def _drain():
    """Let the background send task finish."""
    for _ in range(200):
        if bc.last_result() and not bc.last_result().is_running:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("broadcast did not finish")


async def test_counts_only_reachable_users(initialized_db):
    ids = await _seed(initialized_db)
    assert await bc.count_recipients("team", ids["alpha"]) == 2
    # beta has 3 rows but only one is verified AND active
    assert await bc.count_recipients("team", ids["beta"]) == 1


async def test_blocked_and_unverified_never_receive(initialized_db):
    ids = await _seed(initialized_db)
    bot = FakeBot()
    ok, _ = await bc.start(bot, text="hi", audience="team", team_id=ids["beta"])
    assert ok
    await _drain()
    assert [c for c, _ in bot.sent] == ids["beta_tg"]


async def test_delivery_outcomes_are_counted_separately(initialized_db):
    ids = await _seed(initialized_db)
    a1, a2 = ids["alpha_tg"]
    bot = FakeBot(blocked={a1}, missing={a2})
    ok, _ = await bc.start(bot, text="hi", audience="team", team_id=ids["alpha"])
    assert ok
    await _drain()

    r = bc.last_result()
    assert (r.sent, r.blocked, r.not_found, r.failed) == (0, 1, 1, 0)
    assert r.processed == r.total == 2
    assert len(r.failures) == 2


async def test_flood_control_is_obeyed_and_retried(initialized_db):
    """Telegram's 429 must not silently drop the message."""
    ids = await _seed(initialized_db)
    a1 = ids["alpha_tg"][0]
    bot = FakeBot(retry_after={a1: 1})
    ok, _ = await bc.start(bot, text="hi", audience="team", team_id=ids["alpha"])
    assert ok
    await _drain()

    assert bc.last_result().sent == 2, "the throttled message should land on retry"
    assert bot.attempts[a1] == 2


async def test_empty_and_overlong_text_rejected(initialized_db):
    ids = await _seed(initialized_db)
    bot = FakeBot()
    ok, key = await bc.start(bot, text="   ", audience="team", team_id=ids["alpha"])
    assert not ok and key == "e_empty_message"
    ok, key = await bc.start(bot, text="x" * 4097, audience="all")
    assert not ok and key == "e_broadcast_too_long"
    assert bot.sent == []


async def test_team_audience_needs_a_team(initialized_db):
    await _seed(initialized_db)
    ok, key = await bc.start(FakeBot(), text="hi", audience="team", team_id=None)
    assert not ok and key == "e_broadcast_no_team"


async def test_refuses_to_start_twice(initialized_db):
    ids = await _seed(initialized_db)
    ok, _ = await bc.start(FakeBot(), text="one", audience="team", team_id=ids["alpha"])
    assert ok
    ok2, key = await bc.start(FakeBot(), text="two", audience="team", team_id=ids["alpha"])
    assert not ok2 and key == "e_broadcast_running"
    await _drain()


# ---------- the route ----------

def _login(c: TestClient) -> str:
    c.get("/login")
    token = c.cookies.get("supbot_csrf")
    r = c.post("/login", data={"email": "admin@local", "password": "test-password-123",
                               "next": "/", "csrf_token": token}, follow_redirects=False)
    assert r.status_code == 303
    return c.cookies.get("supbot_csrf") or token


async def test_route_requires_the_confirmation_box(initialized_db, monkeypatch):
    """Without the tick, nothing is sent — the bot client is never even built."""
    await _seed(initialized_db)

    def _boom(*a, **kw):
        raise AssertionError("unconfirmed broadcast must not touch the Bot API")

    monkeypatch.setattr("src.dashboard.bot_client.get_bot", _boom)

    with TestClient(app, base_url="http://testserver") as c:
        token = _login(c)
        r = c.post("/broadcast", data={"text": "hello", "audience": "all",
                                       "csrf_token": token}, follow_redirects=False)
    assert r.status_code == 303
    assert "err=" in r.headers["location"]
    assert bc.last_result() is None


async def test_recipient_count_endpoint(initialized_db):
    ids = await _seed(initialized_db)
    with TestClient(app, base_url="http://testserver") as c:
        _login(c)
        r = c.get(f"/broadcast/recipients.json?audience=team&team_id={ids['alpha']}")
        assert r.json() == {"ok": True, "count": 2}
        assert c.get("/broadcast/recipients.json?audience=nonsense").status_code == 400


async def test_changing_the_audience_clears_the_confirmation(initialized_db):
    """The tick confirms one specific audience, and must not survive a change.

    An operator who agreed to "reaches 5 people", then switched the dropdown,
    would otherwise be able to send to everyone without ever agreeing to that
    number. Asserted against the rendered page rather than the JS: the reset
    happens in refreshCount(), which every audience change goes through.
    """
    await _seed(initialized_db)
    with TestClient(app, base_url="http://testserver") as c:
        _login(c)
        page = c.get("/broadcast").text

    # the confirmation must start unticked on every load
    checkbox = page[page.index('name="confirm"') - 200:page.index('name="confirm"') + 200]
    assert "checked" not in checkbox, "the box must never render pre-ticked"

    # and the handler that repopulates the count must clear it
    assert "confirmBox.checked = false;" in page, (
        "refreshCount() no longer resets the confirmation — a stale tick could "
        "authorise a broadcast to a different audience"
    )
