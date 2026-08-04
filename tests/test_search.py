"""Ticket search: Cyrillic case-insensitivity, username, #id, filter combos."""

from datetime import datetime

from src.dashboard import queries
from src.database.db import db_session
from src.database.models import Message, Team, Ticket, User


async def _seed_searchable(initialized_db):
    """Create a small, distinctive dataset once (idempotent per session)."""
    async with db_session() as session:
        from sqlalchemy import select
        already = (await session.execute(
            select(User).where(User.username == "search_kate")
        )).scalar_one_or_none()
        if already:
            return
        team = Team(name="search-team")
        session.add(team)
        await session.flush()

        kate = User(tg_id=920001, username="search_kate", work_username="kate.w",
                    team_id=team.id, is_verified=True, is_active=True)
        pavlo = User(tg_id=920002, username="search_pavlo", work_username="pavlo.w",
                     team_id=team.id, is_verified=True, is_active=True)
        session.add_all([kate, pavlo])
        await session.flush()

        t1 = Ticket(user_id=kate.id, team_id=team.id, category="VPN",
                    status="closed", closed_at=datetime.utcnow())
        t2 = Ticket(user_id=pavlo.id, team_id=team.id, category="Другое",
                    status="active")
        session.add_all([t1, t2])
        await session.flush()

        session.add_all([
            Message(ticket_id=t1.id, sender_type="user", sender_id=kate.tg_id,
                    text="Сломался ПРИНТЕР на третьем этаже"),
            Message(ticket_id=t1.id, sender_type="admin", sender_id=1,
                    text="Уже чиним, попробуйте перезагрузить"),
            Message(ticket_id=t2.id, sender_type="user", sender_id=pavlo.tg_id,
                    text="Need access to Grafana dashboards"),
        ])
        await session.commit()


async def _search_ids(**kwargs) -> set[int]:
    rows, _total = await queries.list_tickets(**kwargs)
    return {r["id"] for r in rows}


async def test_cyrillic_case_insensitive(initialized_db):
    await _seed_searchable(initialized_db)
    # lowercase query must match the UPPERCASE "ПРИНТЕР" in the message
    ids = await _search_ids(q="принтер")
    assert len(ids) >= 1
    rows, _ = await queries.list_tickets(q="принтер")
    assert any("search_kate" in r["user_label"] for r in rows)


async def test_latin_search_and_username(initialized_db):
    await _seed_searchable(initialized_db)
    # message text, mixed case
    ids_text = await _search_ids(q="grafana")
    assert len(ids_text) >= 1
    # by username, with and without @
    ids_user = await _search_ids(q="@search_pavlo")
    assert ids_text & ids_user, "username search should find pavlo's ticket"
    ids_user2 = await _search_ids(q="search_pavlo")
    assert ids_user == ids_user2


async def test_search_by_id(initialized_db):
    await _seed_searchable(initialized_db)
    rows, _ = await queries.list_tickets(q="принтер")
    tid = rows[0]["id"]
    assert await _search_ids(q=f"#{tid}") >= {tid}
    assert await _search_ids(q=str(tid)) >= {tid}


async def test_search_combines_with_filters(initialized_db):
    await _seed_searchable(initialized_db)
    # "принтер" ticket is closed → filtering active must exclude it
    ids = await _search_ids(q="принтер", status="active")
    rows_all, _ = await queries.list_tickets(q="принтер")
    closed_ids = {r["id"] for r in rows_all if r["status"] == "closed"}
    assert closed_ids and not (ids & closed_ids)


async def test_search_no_results(initialized_db):
    ids = await _search_ids(q="цгфрбшяхьшгсц-нет-такого")
    assert ids == set()


async def test_export_respects_search(initialized_db):
    await _seed_searchable(initialized_db)
    rows = await queries.list_tickets_for_export(q="grafana")
    all_rows = await queries.list_tickets_for_export()
    assert rows, "export search should find the Grafana ticket"
    assert len(rows) < len(all_rows), "export must be filtered by q"
