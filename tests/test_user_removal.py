"""Retiring a user, and finding the ones with no team.

Both come from the bot's menu, which is about to be deleted. The listing test
matters more than it looks: the users page used an INNER JOIN on tickets, so a
person who registered but had not written yet was invisible — and those are
exactly the people an operator opens the page to find.
"""

from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from src.dashboard import queries
from src.dashboard.app import app
from src.database.db import db_session
from src.database.models import Team, Ticket, User
from src.services import user_service
from tests.conftest import next_tg_id


def _login(c: TestClient) -> str:
    c.get("/login")
    token = c.cookies.get("supbot_csrf")
    r = c.post("/login", data={"email": "admin@local", "password": "test-password-123",
                               "next": "/", "csrf_token": token}, follow_redirects=False)
    assert r.status_code == 303
    return c.cookies.get("supbot_csrf") or token


async def _make_user(*, team_id=None, verified=True, tickets=0) -> int:
    async with db_session() as session:
        u = User(tg_id=next_tg_id(), username=f"rm_{next_tg_id()}", team_id=team_id,
                 is_verified=verified, is_active=True)
        session.add(u)
        await session.flush()
        for conversation in range(tickets):
            session.add(Ticket(user_id=u.id, team_id=team_id, category="Network",
                               status="active", conversation_key=f"thread-{conversation}"))
        uid = u.id
        await session.commit()
    return uid


async def _team(name: str) -> int:
    async with db_session() as session:
        t = (await session.execute(select(Team).where(Team.name == name))).scalar_one_or_none()
        if t is None:
            t = Team(name=name)
            session.add(t)
            await session.flush()
        tid = t.id
        await session.commit()
    return tid


# ---------- listing ----------

async def test_users_without_tickets_are_listed(initialized_db):
    """The whole point of the page for a newly registered person."""
    uid = await _make_user(tickets=0)
    rows = await queries.users_with_ticket_stats()
    row = next((r for r in rows if r["user_db_id"] == uid), None)
    assert row is not None, "a user with no tickets must still appear"
    assert row["total"] == 0


async def test_no_team_filter(initialized_db):
    team_id = await _team("rm-team")
    orphan = await _make_user(team_id=None)
    placed = await _make_user(team_id=team_id)

    unassigned = {r["user_db_id"] for r in
                  await queries.users_with_ticket_stats(unassigned_only=True)}
    assert orphan in unassigned
    assert placed not in unassigned

    in_team = {r["user_db_id"] for r in
               await queries.users_with_ticket_stats(team_id=team_id)}
    assert placed in in_team
    assert orphan not in in_team


async def test_no_team_filter_via_route(initialized_db):
    orphan = await _make_user(team_id=None)
    with TestClient(app, base_url="http://testserver") as c:
        _login(c)
        r = c.get("/users?team_id=none")
    assert r.status_code == 200
    assert f'/users/{orphan}"' in r.text


# ---------- retiring ----------

async def test_remove_closes_open_tickets_and_revokes_access(initialized_db):
    uid = await _make_user(tickets=2)
    async with db_session() as session:
        result = await user_service.remove(session, uid)
        await session.commit()

    assert result.tickets_closed == 2
    async with db_session() as session:
        user = await session.get(User, uid)
        assert user is not None, "the row must survive — reports depend on it"
        assert user.is_verified is False
        open_left = (await session.execute(
            select(Ticket).where(Ticket.user_id == uid, Ticket.status == "active")
        )).scalars().all()
        assert open_left == []


async def test_remove_cuts_account_links_both_ways(initialized_db):
    """A personal account left pointing at a retired work account is what
    produces 'writes from personal' badges naming somebody who is gone."""
    work = await _make_user()
    personal_a = await _make_user()
    personal_b = await _make_user()

    async with db_session() as session:
        for pid in (personal_a, personal_b):
            (await session.get(User, pid)).linked_to_user_id = work
        (await session.get(User, work)).pending_work_username = "someone"
        await session.commit()

    async with db_session() as session:
        result = await user_service.remove(session, work)
        await session.commit()

    assert result.personals_detached == 2
    async with db_session() as session:
        assert (await session.get(User, personal_a)).linked_to_user_id is None
        assert (await session.get(User, personal_b)).linked_to_user_id is None
        w = await session.get(User, work)
        assert w.linked_to_user_id is None and w.pending_work_username is None


async def test_remove_keeps_closed_tickets(initialized_db):
    """History is the reason this is not a DELETE."""
    uid = await _make_user(tickets=1)
    async with db_session() as session:
        t = (await session.execute(
            select(Ticket).where(Ticket.user_id == uid))).scalars().first()
        t.status = "closed"
        t.closed_at = datetime.utcnow()
        await session.commit()

    async with db_session() as session:
        await user_service.remove(session, uid)
        await session.commit()

    async with db_session() as session:
        kept = (await session.execute(
            select(Ticket).where(Ticket.user_id == uid))).scalars().all()
    assert len(kept) == 1


async def test_remove_unknown_user_is_an_error(initialized_db):
    async with db_session() as session:
        with pytest.raises(user_service.UserServiceError):
            await user_service.remove(session, 987654321)


async def test_remove_route(initialized_db):
    uid = await _make_user(tickets=1)
    with TestClient(app, base_url="http://testserver") as c:
        token = _login(c)
        r = c.post(f"/users/{uid}/remove", data={"csrf_token": token},
                   follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].endswith("?sent=removed")
    async with db_session() as session:
        assert (await session.get(User, uid)).is_verified is False


# ---------- sorting ----------

async def test_default_order_is_most_recent_activity(initialized_db):
    """Opening the page answers "who wrote lately", not "who writes most"."""
    rows = await queries.users_with_ticket_stats()
    seen = [r["last_ticket_at"] for r in rows if r["last_ticket_at"]]
    assert seen == sorted(seen, reverse=True)


async def test_people_who_never_wrote_sink_to_the_bottom(initialized_db):
    await _make_user(tickets=0)
    rows = await queries.users_with_ticket_stats()
    firsts = [r["last_ticket_at"] is None for r in rows]
    # once the Nones start, they must not stop
    assert firsts == sorted(firsts), "users with no tickets must trail the list"


@pytest.mark.parametrize("column", ["total", "active", "closed"])
async def test_numeric_columns_sort_both_ways(initialized_db, column):
    await _make_user(tickets=3)
    await _make_user(tickets=1)

    desc = [r[column] for r in await queries.users_with_ticket_stats(sort=column)]
    assert desc == sorted(desc, reverse=True)

    asc = [r[column] for r in
           await queries.users_with_ticket_stats(sort=column, direction="asc")]
    assert asc == sorted(asc)


async def test_sorting_by_name_is_alphabetical(initialized_db):
    names = [r["username"].lower() for r in
             await queries.users_with_ticket_stats(sort="user", direction="asc")
             if r["username"]]
    assert names == sorted(names)


async def test_unknown_sort_column_falls_back(initialized_db):
    """A stale bookmark should render the page, not 400 — and never reach SQL.

    The ORDER BY is built by interpolation, so the whitelist is the only thing
    between the query string and the database.
    """
    injected = await queries.users_with_ticket_stats(sort="id; DROP TABLE users")
    default = await queries.users_with_ticket_stats()
    assert [r["user_db_id"] for r in injected] == [r["user_db_id"] for r in default]

    async with db_session() as session:
        assert (await session.execute(select(User))).scalars().first() is not None


async def test_sort_survives_the_team_filter(initialized_db):
    team_id = await _team("sort-team")
    await _make_user(team_id=team_id, tickets=2)
    await _make_user(team_id=team_id, tickets=5)

    rows = await queries.users_with_ticket_stats(team_id=team_id, sort="total")
    assert [r["total"] for r in rows] == sorted([r["total"] for r in rows], reverse=True)
    assert all(r["team"] == "sort-team" for r in rows)


async def test_headers_link_to_every_sortable_column(initialized_db):
    """Each header is a link, and the active one offers the opposite direction."""
    with TestClient(app, base_url="http://testserver") as c:
        _login(c)
        page = c.get("/users").text

    for column in queries.USER_SORTS:
        assert f"sort={column}&amp;dir=" in page, f"{column} header is not clickable"

    # default is "last" descending, so its own header must offer ascending
    assert "sort=last&amp;dir=asc" in page

    # and the filter has to survive a sort click
    with TestClient(app, base_url="http://testserver") as c:
        _login(c)
        filtered = c.get("/users?team_id=none").text
    assert "team_id=none" in filtered
    assert "sort=total&amp;dir=desc&amp;team_id=none" in filtered
