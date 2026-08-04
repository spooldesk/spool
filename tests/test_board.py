"""Kanban board: column grouping, caps, and the drag-and-drop endpoint."""

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from src.dashboard import queries
from src.dashboard.app import app
from src.dashboard.formatting import fmt_seconds
from src.database.db import db_session
from src.database.models import Team, Ticket, User
from src.services import workflow_service as wf


def _login(c: TestClient) -> str:
    c.get("/login")
    token = c.cookies.get("supbot_csrf")
    r = c.post("/login", data={"email": "admin@local", "password": "test-password-123",
                               "next": "/", "csrf_token": token}, follow_redirects=False)
    assert r.status_code == 303, f"login failed: {r.status_code}"
    return c.cookies.get("supbot_csrf") or token


async def _seed_board(initialized_db) -> dict[str, int]:
    """One ticket per column. Returns {column: ticket_id}."""
    async with db_session() as session:
        existing = (await session.execute(
            select(User).where(User.username == "board_user")
        )).scalar_one_or_none()
        if existing:
            rows = (await session.execute(
                select(Ticket).where(Ticket.user_id == existing.id).order_by(Ticket.id)
            )).scalars().all()
            return {"new": rows[0].id, "in_progress": rows[1].id,
                    "waiting": rows[2].id, "closed": rows[3].id}

        team = Team(name="board-team")
        session.add(team)
        await session.flush()
        u = User(tg_id=990001, username="board_user", team_id=team.id,
                 is_verified=True, is_active=True)
        session.add(u)
        await session.flush()

        made: dict[str, int] = {}
        now = datetime.utcnow()
        for column in ("new", "in_progress", "waiting"):
            t = Ticket(user_id=u.id, team_id=team.id, category="Network",
                       status="active", stage=column,
                       created_at=now - timedelta(hours=2))
            session.add(t)
            await session.flush()
            made[column] = t.id
        closed = Ticket(user_id=u.id, team_id=team.id, category="Network",
                        status="closed", stage="in_progress",
                        created_at=now - timedelta(days=1),
                        closed_at=now - timedelta(hours=1))
        session.add(closed)
        await session.flush()
        made["closed"] = closed.id
        await session.commit()
        return made


async def test_every_ticket_lands_in_exactly_one_column(initialized_db):
    ids = await _seed_board(initialized_db)
    board = await queries.board_columns()

    assert set(board) == {"new", "in_progress", "waiting", "closed"}
    for column, ticket_id in ids.items():
        placed = [c for c, data in board.items()
                  if ticket_id in {card["id"] for card in data["cards"]}]
        assert placed == [column], f"#{ticket_id} expected in {column}, found in {placed}"


async def test_closed_column_ignores_stage(initialized_db):
    """The closed ticket carries stage='in_progress' — status must win."""
    ids = await _seed_board(initialized_db)
    board = await queries.board_columns()
    closed_ids = {c["id"] for c in board["closed"]["cards"]}
    assert ids["closed"] in closed_ids
    assert ids["closed"] not in {c["id"] for c in board["in_progress"]["cards"]}


async def test_closed_column_is_windowed(initialized_db):
    """The board shows what just got finished, not the whole archive."""
    ids = await _seed_board(initialized_db)
    async with db_session() as session:
        t = await session.get(Ticket, ids["closed"])
        t.closed_at = datetime.utcnow() - timedelta(days=30)
        await session.commit()

    board = await queries.board_columns(closed_days=7)
    assert ids["closed"] not in {c["id"] for c in board["closed"]["cards"]}

    board_wide = await queries.board_columns(closed_days=60)
    assert ids["closed"] in {c["id"] for c in board_wide["closed"]["cards"]}

    async with db_session() as session:  # restore
        t = await session.get(Ticket, ids["closed"])
        t.closed_at = datetime.utcnow() - timedelta(hours=1)
        await session.commit()


async def test_column_cap_reports_the_hidden_remainder(initialized_db):
    await _seed_board(initialized_db)
    board = await queries.board_columns(per_column=1)
    for column, data in board.items():
        assert len(data["cards"]) <= 1
        assert data["hidden"] == max(0, data["total"] - len(data["cards"]))


async def test_open_columns_are_oldest_first(initialized_db):
    """The card that has waited longest belongs at the top."""
    await _seed_board(initialized_db)
    async with db_session() as session:
        team = (await session.execute(
            select(Team).where(Team.name == "board-team"))).scalar_one()
        u = (await session.execute(
            select(User).where(User.username == "board_user"))).scalar_one()
        old = Ticket(user_id=u.id, team_id=team.id, category="Network",
                     status="active", stage="new",
                     created_at=datetime.utcnow() - timedelta(days=9))
        session.add(old)
        await session.flush()
        old_id = old.id
        await session.commit()

    board = await queries.board_columns()
    assert board["new"]["cards"][0]["id"] == old_id


async def test_board_page_renders(initialized_db):
    await _seed_board(initialized_db)
    with TestClient(app, base_url="http://testserver") as c:
        _login(c)
        r = c.get("/board")
    assert r.status_code == 200
    for label in ("New", "In progress", "Waiting", "Closed"):
        assert label in r.text


async def test_drag_endpoint_moves_the_card(initialized_db):
    ids = await _seed_board(initialized_db)
    with TestClient(app, base_url="http://testserver") as c:
        token = _login(c)
        r = c.post(f"/api/tickets/{ids['new']}/stage",
                   data={"stage": "waiting", "csrf_token": token})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "stage": "waiting"}
    assert (await queries.ticket_detail(ids["new"]))["stage"] == "waiting"
    await wf.set_stage(ids["new"], "new")


async def test_drag_endpoint_refuses_closed_tickets(initialized_db):
    """409 + a message, so the card can be put back where it came from."""
    ids = await _seed_board(initialized_db)
    with TestClient(app, base_url="http://testserver") as c:
        token = _login(c)
        r = c.post(f"/api/tickets/{ids['closed']}/stage",
                   data={"stage": "new", "csrf_token": token})
    assert r.status_code == 409
    body = r.json()
    assert body["ok"] is False and body["error"]
    assert (await queries.ticket_detail(ids["closed"]))["status"] == "closed"


async def test_drag_endpoint_rejects_unknown_stage(initialized_db):
    ids = await _seed_board(initialized_db)
    with TestClient(app, base_url="http://testserver") as c:
        token = _login(c)
        r = c.post(f"/api/tickets/{ids['new']}/stage",
                   data={"stage": "archived", "csrf_token": token})
    assert r.status_code == 409
    assert (await queries.ticket_detail(ids["new"]))["stage"] == "new"


@pytest.mark.parametrize("seconds,expected_prefix", [(-9000, "0"), (None, "—"), (0, "0")])
def test_negative_age_never_renders(seconds, expected_prefix):
    """Clock skew between the bot and dashboard hosts must not print "-8075s"."""
    assert fmt_seconds(seconds).startswith(expected_prefix)


# ---------- whose move it is ----------

async def _card_for(ticket_id: int) -> dict | None:
    cols = await queries.board_columns()
    for col in cols.values():
        for c in col["cards"]:
            if c["id"] == ticket_id:
                return c
    return None


async def _ticket_with(messages: list[tuple[str, int]]) -> int:
    """messages = [(sender_type, minutes_ago), ...] in the order they arrived."""
    from src.database.models import Message
    from tests.conftest import next_tg_id

    async with db_session() as session:
        team = Team(name=f"sla-{next_tg_id()}")
        session.add(team)
        await session.flush()
        u = User(tg_id=next_tg_id(), username="sla_user", team_id=team.id,
                 is_verified=True, is_active=True)
        session.add(u)
        await session.flush()
        t = Ticket(user_id=u.id, team_id=team.id, category="Network", status="active")
        session.add(t)
        await session.flush()
        for sender, mins in messages:
            session.add(Message(ticket_id=t.id, sender_type=sender, sender_id=1,
                                text=f"{sender} said something",
                                created_at=datetime.utcnow() - timedelta(minutes=mins)))
            await session.flush()
        await session.commit()
        return t.id


async def test_an_unanswered_customer_is_marked_as_waiting(initialized_db):
    tid = await _ticket_with([("user", 30)])
    card = await _card_for(tid)
    assert card["waiting_seconds"] is not None
    assert card["waiting_seconds"] >= 29 * 60


async def test_a_card_waiting_on_the_customer_carries_no_clock(initialized_db):
    """The distinction the board exists to show: their move, not ours."""
    tid = await _ticket_with([("user", 60), ("admin", 30)])
    card = await _card_for(tid)
    assert card["waiting_seconds"] is None
    assert card["sla_state"] is None


async def test_the_clock_starts_at_the_first_unanswered_message(initialized_db):
    """A customer who wrote three times has been waiting since the first."""
    tid = await _ticket_with([("user", 300), ("admin", 240),
                              ("user", 120), ("user", 100), ("user", 90)])
    card = await _card_for(tid)
    # 120 minutes, not 90: the reply is owed since they came back.
    assert card["waiting_seconds"] >= 119 * 60
    assert card["waiting_seconds"] < 130 * 60


async def test_an_internal_note_is_not_an_answer(initialized_db):
    """Writing to your colleagues does not stop the customer's clock."""
    from src.services import workflow_service

    tid = await _ticket_with([("user", 45)])
    await workflow_service.add_internal_note(tid, text="looking into it")
    card = await _card_for(tid)
    assert card["waiting_seconds"] is not None, "a note must not clear the clock"


async def test_closed_cards_have_no_clock(initialized_db):
    tid = await _ticket_with([("user", 200)])
    async with db_session() as session:
        t = await session.get(Ticket, tid)
        t.status, t.closed_at = "closed", datetime.utcnow()
        await session.commit()
    card = await _card_for(tid)
    assert card["waiting_seconds"] is None


def test_sla_state_thresholds():
    from src.services import work_hours_service as wh

    assert wh.sla_state(None, 60) is None
    assert wh.sla_state(30 * 60, 60) is None      # inside the target
    assert wh.sla_state(60 * 60, 60) == "warn"    # at the target
    assert wh.sla_state(119 * 60, 60) == "warn"
    assert wh.sla_state(120 * 60, 60) == "breach"  # twice it


def test_initials_are_readable():
    from src.dashboard.queries import _initials

    assert _initials("Liam Novak") == "LN"
    assert _initials("ryan.db") == "RD"
    assert _initials("support") == "S"
    assert _initials(None) == ""
    assert _initials("   ") == ""


async def test_the_board_page_shows_the_waiting_badge(initialized_db):
    tid = await _ticket_with([("user", 200)])
    with TestClient(app, base_url="http://testserver") as c:
        _login(c)
        html = c.get("/board").text
    assert f'data-ticket-id="{tid}"' in html
    assert "ti-arrow-back-up" in html, "the waiting badge should be rendered"


# ---------- moving a card without a mouse ----------

def test_every_open_card_offers_a_move_control():
    """HTML5 drag fires nothing on a touch screen, so a board with drag as its
    only affordance is read-only on a phone — and the board is the headline
    feature. Closed cards stay without one: closed follows from status, not
    from a stage change."""
    from pathlib import Path

    src = Path("src/dashboard/templates/board.html").read_text(encoding="utf-8")
    assert "move-btn" in src
    assert "{% if key != 'closed' %}" in src, "closed column must not offer a move"
    # Both routes go through one function, so a move behaves the same either way.
    assert "async function moveCard(card, to)" in src
    assert "moveCard(card, zone.dataset.column)" in src, "drop should delegate too"


async def test_the_move_menu_lists_the_other_stages(initialized_db):
    await _ticket_with([("user", 10)])

    with TestClient(app, base_url="http://testserver") as c:
        _login(c)
        html = c.get("/board").text

    # The menu is built client-side from this, so it has to reach the page.
    assert "STAGE_LABELS" in html
    for stage in ("new", "in_progress", "waiting"):
        assert f'"{stage}"' in html, f"{stage} missing from the move menu data"
    assert '"closed"' not in html.split("STAGE_LABELS")[1][:200], \
        "closed is not a stage you can move a card to"


def test_the_hint_does_not_promise_only_dragging():
    """It used to read 'Drag a card to change its stage' on a device where
    dragging does nothing at all."""
    from src.dashboard.i18n import strings

    for lang in ("en", "ru"):
        assert "⋮" in strings(lang)["board_hint"], f"{lang} hint omits the menu"
