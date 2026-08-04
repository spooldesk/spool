"""Ticket assignment: manual, first-responder auto-claim, tg link, filters."""

from src.dashboard import queries
from src.database.db import db_session
from src.database.models import Team, Ticket, User
from src.services import admin_user_service, assignment_service


async def _ticket(tg_id: int) -> int:
    async with db_session() as session:
        team = Team(name=f"as-team-{tg_id}")
        session.add(team)
        await session.flush()
        user = User(tg_id=tg_id, username=f"as{tg_id}", team_id=team.id,
                    is_verified=True, is_active=True)
        session.add(user)
        await session.flush()
        t = Ticket(user_id=user.id, team_id=team.id, category="VPN", status="active")
        session.add(t)
        await session.flush()
        tid = t.id
        await session.commit()
    return tid


async def _assignee(ticket_id: int):
    async with db_session() as session:
        return (await session.get(Ticket, ticket_id)).assigned_admin_id


async def test_manual_assign_and_clear(initialized_db):
    op = await admin_user_service.create_admin(
        email="op1@as.dev", password="password123", name="Оператор 1")
    tid = await _ticket(940001)

    assert await assignment_service.assign_ticket(tid, op.id)
    assert await _assignee(tid) == op.id
    # Clear
    assert await assignment_service.assign_ticket(tid, None)
    assert await _assignee(tid) is None
    # Missing ticket / missing admin
    assert not await assignment_service.assign_ticket(99999999, op.id)
    assert not await assignment_service.assign_ticket(tid, 99999999)


async def test_auto_assign_only_first_responder(initialized_db):
    op1 = await admin_user_service.create_admin(
        email="op2@as.dev", password="password123", name="Первый")
    op2 = await admin_user_service.create_admin(
        email="op3@as.dev", password="password123", name="Второй")
    tid = await _ticket(940002)

    assert await assignment_service.auto_assign(tid, op1.id)
    # Second responder must NOT steal the ticket
    assert not await assignment_service.auto_assign(tid, op2.id)
    assert await _assignee(tid) == op1.id


async def test_disabled_operator_rejected(initialized_db):
    op = await admin_user_service.create_admin(
        email="op4@as.dev", password="password123", name="Выключен")
    await admin_user_service.set_active(op.id, False)
    tid = await _ticket(940003)
    assert not await assignment_service.assign_ticket(tid, op.id)
    assert not await assignment_service.auto_assign(tid, op.id)


async def test_auto_assign_by_tg(initialized_db):
    op = await admin_user_service.create_admin(
        email="op5@as.dev", password="password123", name="ТГ-связанный")
    await admin_user_service.update_profile(op.id, tg_id=555000111)
    tid = await _ticket(940004)

    # Unlinked tg id → no-op
    assert not await assignment_service.auto_assign_by_tg(tid, 999888777)
    assert await _assignee(tid) is None
    # Linked tg id → claims
    assert await assignment_service.auto_assign_by_tg(tid, 555000111)
    assert await _assignee(tid) == op.id


async def test_list_filter_by_assignee(initialized_db):
    op = await admin_user_service.create_admin(
        email="op6@as.dev", password="password123", name="Фильтр")
    t_mine = await _ticket(940005)
    t_free = await _ticket(940006)
    await assignment_service.assign_ticket(t_mine, op.id)

    rows_mine, _ = await queries.list_tickets(assigned=op.id)
    ids_mine = {r["id"] for r in rows_mine}
    assert t_mine in ids_mine and t_free not in ids_mine
    # Operator name resolved for the column
    assert next(r for r in rows_mine if r["id"] == t_mine)["operator"] == "Фильтр"

    rows_none, _ = await queries.list_tickets(assigned="none")
    ids_none = {r["id"] for r in rows_none}
    assert t_free in ids_none and t_mine not in ids_none
