"""Seeing that a colleague already has this ticket open.

The failure being prevented is two operators answering the same customer at
once, so the tests care most about presence expiring honestly: a stale "Ivan is
here" is worse than nothing, because it stops somebody from replying at all.
"""

import time

import pytest
from fastapi.testclient import TestClient

from src.dashboard import presence
from src.dashboard.app import app


@pytest.fixture(autouse=True)
def _clean():
    presence.reset()
    yield
    presence.reset()


def _login(c: TestClient, email="admin@local", password="test-password-123") -> str:
    c.get("/login")
    token = c.cookies.get("supbot_csrf")
    r = c.post("/login", data={"email": email, "password": password,
                               "next": "/", "csrf_token": token}, follow_redirects=False)
    assert r.status_code == 303, f"login failed for {email}"
    return c.cookies.get("supbot_csrf") or token


# ---------- the store ----------

def test_you_are_never_shown_to_yourself():
    presence.touch(1, operator_id=7, name="Ivan")
    assert presence.others(1, operator_id=7) == []
    assert [o["name"] for o in presence.others(1, operator_id=9)] == ["Ivan"]


def test_presence_expires_when_the_heartbeat_stops(monkeypatch):
    presence.touch(1, operator_id=7, name="Ivan")
    assert presence.others(1, 9)

    # Jump past the TTL rather than sleeping through it.
    later = time.monotonic() + presence.PRESENCE_TTL_SEC + 1
    monkeypatch.setattr(presence.time, "monotonic", lambda: later)
    assert presence.others(1, 9) == [], "a closed tab must stop claiming the ticket"


def test_typing_fades_faster_than_presence(monkeypatch):
    """A stale "typing…" is a lie about what is happening right now."""
    presence.touch(1, operator_id=7, name="Ivan", typing=True)
    assert presence.others(1, 9)[0]["typing"] is True

    between = time.monotonic() + presence.TYPING_TTL_SEC + 1
    assert between - time.monotonic() < presence.PRESENCE_TTL_SEC
    monkeypatch.setattr(presence.time, "monotonic", lambda: between)

    still_here = presence.others(1, 9)
    assert still_here[0]["typing"] is False, "typing should lapse"
    assert still_here[0]["name"] == "Ivan", "but they are still on the ticket"


def test_leaving_is_immediate():
    presence.touch(1, operator_id=7, name="Ivan")
    presence.leave(1, 7)
    assert presence.others(1, 9) == []


def test_tickets_do_not_bleed_into_each_other():
    presence.touch(1, operator_id=7, name="Ivan")
    assert presence.others(2, 9) == []


# ---------- the endpoint ----------

async def test_heartbeat_reports_the_other_operator(initialized_db):
    """Two browsers, two operators, one ticket."""
    from src.services import admin_user_service as svc

    try:
        await svc.create_admin(email="second@t.dev", password="secondpass123",
                               name="Second", role="admin")
    except ValueError:
        pass  # left over from an earlier run in this session

    with TestClient(app, base_url="http://testserver") as a, \
         TestClient(app, base_url="http://testserver") as b:
        token_a = _login(a)
        token_b = _login(b, "second@t.dev", "secondpass123")

        first = a.post("/api/tickets/500/presence", data={"csrf_token": token_a})
        assert first.json() == {"ok": True, "others": []}

        b.post("/api/tickets/500/presence", data={"csrf_token": token_b, "typing": "1"})

        seen = a.post("/api/tickets/500/presence", data={"csrf_token": token_a}).json()

    assert [o["name"] for o in seen["others"]] == ["Second"]
    assert seen["others"][0]["typing"] is True


def test_heartbeat_needs_a_session():
    """Asserting the outcome, not the status code — being turned away is the
    point, and whether that arrives as a redirect, a 403 or a rejected body is
    the framework's business."""
    with TestClient(app, base_url="http://testserver") as c:
        c.get("/login")
        token = c.cookies.get("supbot_csrf")
        r = c.post("/api/tickets/777/presence",
                   data={"csrf_token": token}, follow_redirects=False)

    assert r.status_code != 200, "an anonymous caller must not be let in"
    assert presence.others(777, operator_id=0) == [], "and must not be recorded"


def test_typing_never_breaks_the_heartbeat(monkeypatch):
    """The Telegram nudge is decoration; the answer must arrive regardless."""
    def _explode():
        raise RuntimeError("Telegram is down")

    monkeypatch.setattr("src.dashboard.bot_client.get_bot", _explode)

    with TestClient(app, base_url="http://testserver") as c:
        token = _login(c)
        r = c.post("/api/tickets/501/presence",
                   data={"csrf_token": token, "typing": "1"})
    assert r.status_code == 200 and r.json()["ok"] is True
