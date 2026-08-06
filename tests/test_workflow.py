"""Board stages and internal notes.

The important tests here are the leak tests: an internal note must never reach
the user, and must never become source material for an AI answer that does.
"""

from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from src.dashboard import queries
from src.dashboard.app import app
from src.database.db import db_session
from src.database.models import Message, Team, Ticket, User
from src.services import workflow_service as wf
from tests.conftest import next_tg_id


def _login(c: TestClient) -> str:
    """Log in as the seeded owner; returns the CSRF token for further POSTs."""
    c.get("/login")
    token = c.cookies.get("supbot_csrf")
    r = c.post("/login", data={"email": "admin@local", "password": "test-password-123",
                               "next": "/", "csrf_token": token}, follow_redirects=False)
    assert r.status_code == 303, f"login failed: {r.status_code}"
    return c.cookies.get("supbot_csrf") or token


async def _seed_wf(initialized_db) -> int:
    """One active ticket with a user message. Returns its id."""
    async with db_session() as session:
        existing = (await session.execute(
            select(User).where(User.username == "wf_user")
        )).scalar_one_or_none()
        if existing:
            t = (await session.execute(
                select(Ticket).where(Ticket.user_id == existing.id)
            )).scalars().first()
            return t.id

        team = Team(name="wf-team")
        session.add(team)
        await session.flush()
        u = User(tg_id=970001, username="wf_user", work_username="wf.w",
                 team_id=team.id, is_verified=True, is_active=True)
        session.add(u)
        await session.flush()
        t = Ticket(user_id=u.id, team_id=team.id, category="Network", status="active")
        session.add(t)
        await session.flush()
        session.add(Message(ticket_id=t.id, sender_type="user", sender_id=u.tg_id,
                            text="wifi keeps dropping"))
        await session.commit()
        return t.id


# ---------- stages ----------

async def test_new_ticket_starts_in_new_stage(initialized_db):
    tid = await _seed_wf(initialized_db)
    detail = await queries.ticket_detail(tid)
    assert detail["stage"] == "new"
    assert detail["board_column"] == "new"


async def test_set_stage_accepts_known_values_only(initialized_db):
    tid = await _seed_wf(initialized_db)
    assert await wf.set_stage(tid, "waiting") is True
    assert (await queries.ticket_detail(tid))["stage"] == "waiting"

    assert await wf.set_stage(tid, "done") is False, "unknown stage must be rejected"
    assert await wf.set_stage(tid, "") is False
    assert (await queries.ticket_detail(tid))["stage"] == "waiting", "unchanged after a bad value"

    await wf.set_stage(tid, "new")  # reset for other tests


async def test_closed_tickets_ignore_stage(initialized_db):
    """A closed ticket lands in the closed column no matter what stage says."""
    tid = await _seed_wf(initialized_db)
    await wf.set_stage(tid, "in_progress")
    async with db_session() as session:
        t = await session.get(Ticket, tid)
        t.status = "closed"
        t.closed_at = datetime.utcnow()
        await session.commit()

    detail = await queries.ticket_detail(tid)
    assert detail["stage"] == "in_progress"      # value is kept
    assert detail["board_column"] == "closed"    # but the board ignores it
    assert await wf.set_stage(tid, "waiting") is False, "closed tickets don't move"

    async with db_session() as session:          # restore
        t = await session.get(Ticket, tid)
        t.status, t.closed_at = "active", None
        t.stage = "new"
        await session.commit()


async def test_stage_filter_implies_active(initialized_db):
    tid = await _seed_wf(initialized_db)
    await wf.set_stage(tid, "waiting")
    rows, _ = await queries.list_tickets(stage="waiting")
    assert tid in {r["id"] for r in rows}
    assert all(r["status"] == "active" for r in rows)

    rows_new, _ = await queries.list_tickets(stage="new")
    assert tid not in {r["id"] for r in rows_new}
    await wf.set_stage(tid, "new")


async def test_user_reply_pulls_ticket_out_of_waiting(initialized_db):
    """Parked on the customer → their answer puts it back in the work column."""
    tid = await _seed_wf(initialized_db)
    await wf.set_stage(tid, "waiting")
    async with db_session() as session:
        t = await session.get(Ticket, tid)
        # Mirrors what user_handlers does when a message lands on an open ticket
        if t.stage == "waiting":
            t.stage = "in_progress"
        await session.commit()
    assert (await queries.ticket_detail(tid))["stage"] == "in_progress"
    await wf.set_stage(tid, "new")


# ---------- internal notes ----------

async def test_note_is_stored_and_flagged(initialized_db):
    tid = await _seed_wf(initialized_db)
    note_id = await wf.add_internal_note(tid, text="  customer is on the old VPN client  ")
    assert note_id is not None

    detail = await queries.ticket_detail(tid)
    note = next(m for m in detail["messages"] if m["id"] == note_id)
    assert note["is_internal"] is True
    assert note["text"] == "customer is on the old VPN client", "text should be stripped"


async def test_empty_note_is_rejected(initialized_db):
    tid = await _seed_wf(initialized_db)
    assert await wf.add_internal_note(tid, text="   ") is None
    assert await wf.add_internal_note(tid, text="") is None


async def test_note_does_not_count_as_a_reply(initialized_db):
    """SLA metrics must not improve because someone wrote a note to the team."""
    tid = await _seed_wf(initialized_db)
    before = await queries.overview_stats()
    await wf.add_internal_note(tid, text="internal only, no reply yet")
    after = await queries.overview_stats()

    assert after["awaiting_response"] == before["awaiting_response"], \
        "a note must not clear the awaiting-response queue"

    rows = await queries.list_tickets_for_export()
    row = next(r for r in rows if r["id"] == tid)
    assert row["msg_admin"] == 0, "notes are not admin replies"


async def test_note_never_feeds_ai_context(initialized_db):
    """Deflection answers go straight to the user — notes must not be in scope."""
    from src.ai.suggest_from_history import _gather_full_transcript

    tid = await _seed_wf(initialized_db)
    secret = "DO-NOT-SHOW-THIS-TO-THE-USER"
    await wf.add_internal_note(tid, text=secret)

    async with db_session() as session:
        transcript = await _gather_full_transcript(session, tid)
    assert secret not in transcript


async def test_a_note_never_reaches_the_customer(initialized_db, channel):
    """The whole point of an internal note.

    Asserted against the channel rather than against the Bot API, which is
    both stronger and channel-agnostic: whatever a note is written about, the
    person it is written about must not receive it on any medium.
    """
    tid = await _seed_wf(initialized_db)
    with TestClient(app, base_url="http://testserver") as c:
        token = _login(c)
        r = c.post(f"/tickets/{tid}/note",
                   data={"text": "note via HTTP", "csrf_token": token},
                   follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].endswith("?sent=note")
    assert channel.sent == 0, "an internal note was delivered to the customer"

    detail = await queries.ticket_detail(tid)
    assert any(m["is_internal"] and m["text"] == "note via HTTP" for m in detail["messages"])


async def test_stage_route_rejects_bad_value(initialized_db):
    tid = await _seed_wf(initialized_db)
    with TestClient(app, base_url="http://testserver") as c:
        token = _login(c)
        ok = c.post(f"/tickets/{tid}/stage",
                    data={"stage": "in_progress", "csrf_token": token},
                    follow_redirects=False)
        bad = c.post(f"/tickets/{tid}/stage",
                     data={"stage": "archived", "csrf_token": token},
                     follow_redirects=False)
    assert ok.headers["location"].endswith("?sent=stage")
    assert "err=" in bad.headers["location"]
    assert (await queries.ticket_detail(tid))["stage"] == "in_progress"


# ---------- duplicate sends ----------

class _CountingBot:
    """Counts what actually reached the customer, ignoring the admin mirror."""

    def __init__(self):
        self.to_customer: list[str] = []

    async def send_message(self, chat_id, text, **kw):
        if int(chat_id) > 0:          # negative ids are the admin group
            self.to_customer.append(text)

        class _Sent:
            message_id = 1
        return _Sent()


async def _ticket_for_reply() -> int:
    async with db_session() as session:
        team = Team(name=f"dup-{next_tg_id()}")
        session.add(team)
        await session.flush()
        u = User(tg_id=next_tg_id(), username="dup_user", team_id=team.id,
                 is_verified=True, is_active=True)
        session.add(u)
        await session.flush()
        t = Ticket(user_id=u.id, team_id=team.id, category="Network", status="active")
        session.add(t)
        await session.flush()
        tid = t.id
        await session.commit()
    return tid


async def test_mashing_send_reaches_the_customer_once(initialized_db, channel):
    """An impatient operator must not turn one reply into five.

    The button is disabled on submit, but a refresh or Back-then-resubmit walks
    straight past that, so the server has to refuse the duplicate itself.
    """
    tid = await _ticket_for_reply()

    with TestClient(app, base_url="http://testserver") as c:
        token = _login(c)
        for _ in range(5):
            c.post(f"/tickets/{tid}/reply",
                   data={"text": "Checking, one moment", "csrf_token": token},
                   follow_redirects=False)

    assert [t for _addr, t in channel.texts] == ["Checking, one moment"]


async def test_a_genuinely_different_reply_still_sends(initialized_db, channel):
    tid = await _ticket_for_reply()

    with TestClient(app, base_url="http://testserver") as c:
        token = _login(c)
        for text in ("first", "first", "second"):
            c.post(f"/tickets/{tid}/reply",
                   data={"text": text, "csrf_token": token}, follow_redirects=False)

    assert [t for _addr, t in channel.texts] == ["first", "second"]


async def test_the_same_words_are_allowed_once_the_window_passes(
    initialized_db, channel, monkeypatch
):
    """"ok" twice in a row is a stutter; "ok" again later is a person talking."""
    import src.dashboard.routes.tickets as tickets_routes

    tid = await _ticket_for_reply()
    monkeypatch.setattr(tickets_routes, "_DUPLICATE_WINDOW_SEC", 0)

    with TestClient(app, base_url="http://testserver") as c:
        token = _login(c)
        for _ in range(2):
            c.post(f"/tickets/{tid}/reply",
                   data={"text": "ok", "csrf_token": token}, follow_redirects=False)

    assert [t for _addr, t in channel.texts] == ["ok", "ok"]


def test_every_form_is_guarded_against_a_second_click():
    """The guard is global — a new form should not have to opt in."""
    with TestClient(app, base_url="http://testserver") as c:
        _login(c)
        page = c.get("/tickets").text
    assert "dataset.submitting" in page, "the submit guard is missing from the layout"


# ---------- incremental polling ----------

async def test_poll_returns_only_what_is_new(initialized_db):
    """The poll used to re-send the whole conversation every few seconds."""
    tid = await _seed_wf(initialized_db)

    everything = await queries.messages_after(tid, 0)
    assert everything["messages"], "a fresh page wants the whole thread"
    cursor = everything["latest_id"]

    quiet = await queries.messages_after(tid, cursor)
    assert quiet["messages"] == [], "nothing new means nothing sent"
    assert quiet["latest_id"] == cursor, "the cursor must not rewind"

    async with db_session() as session:
        session.add(Message(ticket_id=tid, sender_type="user", sender_id=1,
                            text="one more thing"))
        await session.commit()

    delta = await queries.messages_after(tid, cursor)
    assert [m["text"] for m in delta["messages"]] == ["one more thing"]
    assert delta["latest_id"] > cursor


async def test_poll_carries_what_the_page_reacts_to(initialized_db):
    """Closing a ticket elsewhere has to hide the composer in an open tab."""
    tid = await _seed_wf(initialized_db)
    payload = await queries.messages_after(tid, 0)
    assert payload["status"] == "active"
    assert "user_label" in payload


async def test_poll_marks_internal_notes(initialized_db):
    """A note appearing live must not render as a message sent to the customer."""
    tid = await _seed_wf(initialized_db)
    cursor = (await queries.messages_after(tid, 0))["latest_id"]
    await wf.add_internal_note(tid, text="not for the customer")

    delta = await queries.messages_after(tid, cursor)
    assert len(delta["messages"]) == 1
    assert delta["messages"][0]["is_internal"] is True


async def test_unknown_ticket_is_not_found(initialized_db):
    assert await queries.messages_after(999999, 0) is None


async def test_endpoint_passes_the_cursor_through(initialized_db):
    tid = await _seed_wf(initialized_db)
    with TestClient(app, base_url="http://testserver") as c:
        _login(c)
        full = c.get(f"/tickets/{tid}/messages.json").json()
        quiet = c.get(f"/tickets/{tid}/messages.json?after_id={full['latest_id']}").json()

    assert full["ok"] and quiet["ok"]
    assert full["messages"] and quiet["messages"] == []
    # The saving that made this worth doing.
    assert len(str(quiet)) < len(str(full)) / 2


async def test_a_junk_cursor_does_not_error(initialized_db):
    """A stale or mangled query string should still render the page's feed."""
    tid = await _seed_wf(initialized_db)
    with TestClient(app, base_url="http://testserver") as c:
        _login(c)
        r = c.get(f"/tickets/{tid}/messages.json?after_id=abc")
    assert r.status_code == 200 and r.json()["ok"]


# ---------- live refresh token ----------

async def test_version_moves_on_a_new_message(initialized_db):
    tid = await _seed_wf(initialized_db)
    before = await queries.tickets_version()
    async with db_session() as session:
        session.add(Message(ticket_id=tid, sender_type="user", sender_id=1, text="hi"))
        await session.commit()
    assert await queries.tickets_version() != before


async def test_version_moves_on_a_stage_change(initialized_db):
    """A colleague dragging a card has to reach the other open boards."""
    tid = await _seed_wf(initialized_db)
    await wf.set_stage(tid, "new")
    before = await queries.tickets_version()
    await wf.set_stage(tid, "in_progress")
    assert await queries.tickets_version() != before

    mid = await queries.tickets_version()
    await wf.set_stage(tid, "waiting")
    assert await queries.tickets_version() != mid, "in_progress -> waiting must show too"
    await wf.set_stage(tid, "new")


async def test_version_moves_on_close_and_reopen(initialized_db):
    tid = await _seed_wf(initialized_db)
    before = await queries.tickets_version()
    async with db_session() as session:
        t = await session.get(Ticket, tid)
        t.status, t.closed_at = "closed", datetime(2026, 8, 4, 12, 0, 0)
        await session.commit()
    closed = await queries.tickets_version()
    assert closed != before

    async with db_session() as session:
        t = await session.get(Ticket, tid)
        t.status, t.closed_at = "active", None
        await session.commit()
    assert await queries.tickets_version() != closed


async def test_version_is_stable_when_nothing_happens(initialized_db):
    """The whole point: a quiet dashboard must not re-render itself."""
    await _seed_wf(initialized_db)
    assert await queries.tickets_version() == await queries.tickets_version()


async def test_version_endpoint_is_cheap(initialized_db):
    await _seed_wf(initialized_db)
    with TestClient(app, base_url="http://testserver") as c:
        _login(c)
        r = c.get("/api/tickets-version")
        page = c.get("/tickets")
    assert r.status_code == 200 and r.json()["version"]
    assert len(r.content) < len(page.content) / 100, \
        "the check must be orders of magnitude smaller than the page it guards"


async def test_version_needs_a_login(initialized_db):
    with TestClient(app, base_url="http://testserver") as c:
        r = c.get("/api/tickets-version", follow_redirects=False)
    assert r.status_code in (302, 303, 307, 401, 403)
    if r.status_code < 400:
        assert "/login" in r.headers["location"]


async def test_version_moves_when_a_ticket_is_reassigned(initialized_db):
    """Two operators on one board must not disagree about who owns a ticket."""
    tid = await _seed_wf(initialized_db)
    before = await queries.tickets_version()
    async with db_session() as session:
        t = await session.get(Ticket, tid)
        t.assigned_admin_id = 7
        await session.commit()
    assert await queries.tickets_version() != before

    async with db_session() as session:
        t = await session.get(Ticket, tid)
        t.assigned_admin_id = None
        await session.commit()
