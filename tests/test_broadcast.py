"""Broadcast: audience selection, delivery accounting, pacing, and the gate.

This is the only action in the dashboard that reaches everyone at once and
cannot be undone, so the tests are mostly about it NOT doing more than asked.
"""

import asyncio

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from src.dashboard.app import app
from src.database.db import db_session
from src.database.models import Team, User
import src.channels as channels
from src.channels.base import Capabilities, Delivery
from src.services import broadcast_service as bc
from tests.conftest import next_tg_id


class FakeChannel:
    """Records what was sent; can be told to fail for specific addresses.

    Written against the channel rather than the Bot API, which is what the
    broadcast loop now speaks. The outcomes it can produce — blocked, gone,
    throttled — are the vocabulary every channel reports in, so the same
    accounting has to hold when the audience is on a widget or in a mailbox.
    """

    name = "telegram"
    caps = Capabilities(unsend=True, files=True, buttons=True, typing=True)

    def __init__(self, blocked=(), missing=(), retry_after=()):
        self.sent: list[tuple[int, str]] = []
        self.blocked = {str(x) for x in blocked}
        self.missing = {str(x) for x in missing}
        self.retry_after = {str(k): v for k, v in dict(retry_after).items()}
        self.attempts: dict[int, int] = {}

    async def send_text(self, address: str, text: str, *, options=None):
        chat = int(address)
        self.attempts[chat] = self.attempts.get(chat, 0) + 1
        if address in self.blocked:
            return Delivery.failed("e_user_blocked_bot", address, blocked=True)
        if address in self.missing:
            return Delivery.failed("e_user_chat_not_found", address, blocked=True)
        left = self.retry_after.get(address, 0)
        if left:
            self.retry_after[address] = left - 1
            return Delivery.failed("e_send_rate_limited", "flood", retry_after=0)
        self.sent.append((chat, text))
        return Delivery.sent(len(self.sent))

    async def send_file(self, address, *, filename, content, caption=None):
        raise AssertionError("broadcast never sends files")

    async def unsend(self, address, ref):
        return False

    async def typing(self, address):
        pass


# The bot argument survives only because `start` still takes one; nothing in
# the broadcast path uses it now that sending goes through the channel.
_BOT = object()


@pytest.fixture(autouse=True)
def _channel(request):
    """Install whichever fake the test built, and take it out again."""
    fake = getattr(request, "param", None) or FakeChannel()
    channels.register(fake)
    yield fake
    channels.reset()


def _use(fake: FakeChannel) -> FakeChannel:
    """Swap in a fake configured for this test."""
    channels.register(fake)
    return fake


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


async def test_blocked_and_unverified_never_receive(initialized_db, _channel):
    ids = await _seed(initialized_db)
    ok, _ = await bc.start(_BOT, text="hi", audience="team", team_id=ids["beta"])
    assert ok
    await _drain()
    assert [c for c, _ in _channel.sent] == ids["beta_tg"]


async def test_delivery_outcomes_are_counted_separately(initialized_db):
    ids = await _seed(initialized_db)
    a1, a2 = ids["alpha_tg"]
    _use(FakeChannel(blocked={a1}, missing={a2}))
    ok, _ = await bc.start(_BOT, text="hi", audience="team", team_id=ids["alpha"])
    assert ok
    await _drain()

    r = bc.last_result()
    assert (r.sent, r.blocked, r.not_found, r.failed) == (0, 1, 1, 0)
    assert r.processed == r.total == 2
    assert len(r.failures) == 2


async def test_flood_control_is_obeyed_and_retried(initialized_db):
    """A rate limit must not silently drop the message.

    The channel names the delay it wants; the broadcast waits it out and tries
    once more. Dropping instead would lose whoever happened to be in the
    audience at the moment the limit was hit, with nothing in the report to
    say so.
    """
    ids = await _seed(initialized_db)
    a1 = ids["alpha_tg"][0]
    fake = _use(FakeChannel(retry_after={a1: 1}))
    ok, _ = await bc.start(_BOT, text="hi", audience="team", team_id=ids["alpha"])
    assert ok
    await _drain()

    assert bc.last_result().sent == 2, "the throttled message should land on retry"
    assert fake.attempts[a1] == 2


async def test_empty_and_overlong_text_rejected(initialized_db, _channel):
    ids = await _seed(initialized_db)
    ok, key = await bc.start(_BOT, text="   ", audience="team", team_id=ids["alpha"])
    assert not ok and key == "e_empty_message"
    ok, key = await bc.start(_BOT, text="x" * 4097, audience="all")
    assert not ok and key == "e_broadcast_too_long"
    assert _channel.sent == []


async def test_team_audience_needs_a_team(initialized_db):
    await _seed(initialized_db)
    ok, key = await bc.start(_BOT, text="hi", audience="team", team_id=None)
    assert not ok and key == "e_broadcast_no_team"


async def test_refuses_to_start_twice(initialized_db):
    ids = await _seed(initialized_db)
    ok, _ = await bc.start(_BOT, text="one", audience="team", team_id=ids["alpha"])
    assert ok
    ok2, key = await bc.start(_BOT, text="two", audience="team", team_id=ids["alpha"])
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
