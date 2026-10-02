"""The work queue.

The problem it exists for: an operator answers three people, comes back, and
has to decide again which of twenty tickets to open. The board shows what is
going on; it never said what to do next.
"""

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select, update

from src.dashboard import queries
from src.dashboard.app import app
from src.database.db import db_session
from src.database.models import AdminUser, Message, Team, Ticket, User
from tests.conftest import next_tg_id


@pytest.fixture(autouse=True)
async def isolated_open_queue(initialized_db):
    # Test the actual first result, independent of older fixtures in the shared DB.
    async with db_session(write=True) as session:
        before = dict((await session.execute(select(Ticket.id, Ticket.status))).all())
        await session.execute(update(Ticket).values(status='closed'))
    yield
    async with db_session(write=True) as session:
        await session.execute(delete(Ticket).where(Ticket.id.not_in(before)))
        for status in set(before.values()):
            ids = [ticket_id for ticket_id, previous in before.items() if previous == status]
            await session.execute(update(Ticket).where(Ticket.id.in_(ids)).values(status=status))


def _login(c: TestClient) -> str:
    c.get("/login")
    token = c.cookies.get("supbot_csrf")
    r = c.post("/login", data={"email": "admin@local", "password": "test-password-123",
                               "next": "/", "csrf_token": token}, follow_redirects=False)
    assert r.status_code == 303
    return token


async def _owner_id() -> int:
    async with db_session() as session:
        return (await session.execute(
            select(AdminUser.id).where(AdminUser.email == "admin@local")
        )).scalar_one()


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


async def _waiting_ticket(*, minutes_ago: int, assigned_to: int | None = None,
                          answered: bool = False) -> int:
    """A ticket whose last message is from the customer, i.e. owed an answer."""
    async with db_session() as session:
        team = Team(name=f"wq-{next_tg_id()}")
        session.add(team)
        await session.flush()
        u = User(tg_id=next_tg_id(), username="wq_user", team_id=team.id,
                 is_verified=True, is_active=True)
        session.add(u)
        await session.flush()
        t = Ticket(user_id=u.id, team_id=team.id, category="Network", status="active",
                   assigned_admin_id=assigned_to)
        session.add(t)
        await session.flush()
        asked = datetime.utcnow() - timedelta(minutes=minutes_ago)
        session.add(Message(ticket_id=t.id, sender_type="user", sender_id=u.tg_id,
                            text="please help", created_at=asked))
        if answered:
            session.add(Message(ticket_id=t.id, sender_type="admin", sender_id=1,
                                text="on it", created_at=asked + timedelta(minutes=1)))
        await session.commit()
        return t.id


async def test_the_longest_wait_comes_first(initialized_db):
    op = await _owner_id()
    recent = await _waiting_ticket(minutes_ago=5, assigned_to=op)
    oldest = await _waiting_ticket(minutes_ago=500, assigned_to=op)

    ordered = [t for t, _ in await queries._queue_rows(op, "mine")]
    assert ordered.index(oldest) < ordered.index(recent)
    assert await queries.queue_next(op, "mine") == oldest


async def test_a_ticket_waiting_on_the_customer_is_not_in_the_queue(initialized_db):
    """The distinction the whole feature rests on: if they owe us nothing,
    there is nothing to pick up."""
    op = await _owner_id()
    answered = await _waiting_ticket(minutes_ago=300, assigned_to=op, answered=True)

    assert answered not in [t for t, _ in await queries._queue_rows(op, "mine")]


async def test_a_colleagues_ticket_is_not_yours_to_take(initialized_db):
    """Offering it is how a queue starts causing the collisions it exists to
    prevent."""
    op = await _owner_id()
    theirs = await _waiting_ticket(minutes_ago=900, assigned_to=await _another_operator())

    assert theirs not in [t for t, _ in await queries._queue_rows(op, "open")]
    assert theirs in [t for t, _ in await queries._queue_rows(op, "all")]


async def test_unclaimed_and_mine_are_both_open_to_me(initialized_db):
    op = await _owner_id()
    mine = await _waiting_ticket(minutes_ago=100, assigned_to=op)
    free = await _waiting_ticket(minutes_ago=200)

    open_ids = [t for t, _ in await queries._queue_rows(op, "open")]
    assert mine in open_ids and free in open_ids


async def test_the_one_just_finished_is_not_handed_straight_back(initialized_db):
    """A customer who replies instantly would otherwise return the operator to
    the conversation they just closed out of."""
    op = await _owner_id()
    first = await _waiting_ticket(minutes_ago=800, assigned_to=op)
    await _waiting_ticket(minutes_ago=400, assigned_to=op)

    assert await queries.queue_next(op, "mine") == first
    assert await queries.queue_next(op, "mine", exclude=first) != first


async def test_taking_next_claims_it(initialized_db):
    """Otherwise two operators are handed the same ticket, which is the
    collision the queue was supposed to remove."""
    unclaimed = await _waiting_ticket(minutes_ago=1000)

    with TestClient(app, base_url="http://testserver") as c:
        token = _login(c)
        r = c.post("/queue/next", data={"source": "unassigned", "csrf_token": token},
                   follow_redirects=False)

    assert r.status_code == 303
    assert f"/tickets/{unclaimed}" in r.headers["location"]

    async with db_session() as session:
        assigned = (await session.execute(
            select(Ticket.assigned_admin_id).where(Ticket.id == unclaimed)
        )).scalar_one()
    assert assigned == await _owner_id(), "the queue handed it over without claiming it"


async def test_an_empty_queue_does_not_pretend(initialized_db):
    with TestClient(app, base_url="http://testserver") as c:
        token = _login(c)
        # Nothing is unassigned and waiting once the earlier ones are claimed;
        # ask for a source that cannot match instead of relying on that.
        r = c.post("/queue/next", data={"source": "nonsense", "csrf_token": token},
                   follow_redirects=False)
    assert r.status_code == 303
    assert "/tickets/" in r.headers["location"] or "queue=empty" in r.headers["location"]


async def test_the_take_next_button_is_a_post(initialized_db):
    """It assigns a ticket. A link that mutates gets fired by a browser
    prefetching it, and the operator arrives to work already claimed."""
    from pathlib import Path

    board = Path("src/dashboard/templates/board.html").read_text(encoding="utf-8")
    assert '<form method="post" action="/queue/next">' in board
    assert 'href="/queue/next"' not in board


async def test_sending_from_the_queue_hands_over_the_next_one(initialized_db):
    """The loop: reply, and the next conversation opens without a detour
    through a list."""
    from pathlib import Path

    ticket = Path("src/dashboard/templates/ticket_detail.html").read_text(encoding="utf-8")
    assert 'name="queue" value="{{ queue_source }}"' in ticket
    assert "q_send_and_next" in ticket, "the button should say where it leads"

    tickets_py = Path("src/dashboard/routes/tickets.py").read_text(encoding="utf-8")
    assert "_queue_redirect" in tickets_py


async def test_the_loop_continues_after_a_plain_text_reply(initialized_db, monkeypatch):
    """The attachment branch and the text branch return separately, and only
    the first was wired — so the queue worked for the rarer path and quietly
    stopped on the one operators actually use."""
    class _Result:
        success = True
        error = None

    async def _fake_apply_text(*a, **kw):
        return _Result()

    monkeypatch.setattr("src.services.template_service.apply_text", _fake_apply_text)
    monkeypatch.setattr("src.dashboard.bot_client.get_bot", lambda: object())

    op = await _owner_id()
    # Older than anything the other tests in this module leave behind, so
    # "the next one" is unambiguous however they are ordered.
    first = await _waiting_ticket(minutes_ago=90_000, assigned_to=op)
    second = await _waiting_ticket(minutes_ago=89_000, assigned_to=op)

    with TestClient(app, base_url="http://testserver") as c:
        token = _login(c)
        r = c.post(f"/tickets/{first}/reply",
                   data={"text": "done", "queue": "mine", "csrf_token": token},
                   follow_redirects=False)

    assert r.status_code == 303
    location = r.headers["location"]
    assert f"/tickets/{first}" not in location, "it stayed on the ticket just answered"
    assert f"/tickets/{second}" in location, "it should hand over the next one"
    assert "queue=mine" in location, "and keep the operator inside the loop"
