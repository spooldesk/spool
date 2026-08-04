"""CSAT: rating write rules — ownership, closed-only, immutability, bounds."""

from datetime import datetime

from src.database.db import db_session
from src.database.models import Team, Ticket, User
from src.services import csat_service


async def _make_ticket(*, status: str = "closed", tg_id: int) -> tuple[int, int]:
    """Create a user + ticket, return (ticket_id, user_tg_id)."""
    async with db_session() as session:
        team = Team(name=f"csat-team-{tg_id}")
        session.add(team)
        await session.flush()
        user = User(tg_id=tg_id, username=f"u{tg_id}", team_id=team.id,
                    is_verified=True, is_active=True)
        session.add(user)
        await session.flush()
        ticket = Ticket(
            user_id=user.id, team_id=team.id, category="VPN", status=status,
            closed_at=datetime.utcnow() if status == "closed" else None,
        )
        session.add(ticket)
        await session.flush()
        tid = ticket.id
        await session.commit()
    return tid, tg_id


async def _rating_of(ticket_id: int):
    async with db_session() as session:
        t = await session.get(Ticket, ticket_id)
        return t.csat_rating, t.csat_rated_at


async def test_rating_saved_once(initialized_db):
    tid, tg = await _make_ticket(tg_id=910001)
    assert await csat_service.save_rating(ticket_id=tid, user_tg_id=tg, rating=5) == "ok"
    rating, rated_at = await _rating_of(tid)
    assert rating == 5 and rated_at is not None

    # Second attempt doesn't overwrite — honest metric
    assert await csat_service.save_rating(ticket_id=tid, user_tg_id=tg, rating=1) == "already"
    rating, _ = await _rating_of(tid)
    assert rating == 5


async def test_only_owner_can_rate(initialized_db):
    tid, _ = await _make_ticket(tg_id=910002)
    assert await csat_service.save_rating(
        ticket_id=tid, user_tg_id=999999999, rating=4,
    ) == "not_found"
    rating, _ = await _rating_of(tid)
    assert rating is None


async def test_active_ticket_cannot_be_rated(initialized_db):
    tid, tg = await _make_ticket(status="active", tg_id=910003)
    assert await csat_service.save_rating(ticket_id=tid, user_tg_id=tg, rating=5) == "not_found"


async def test_rating_bounds(initialized_db):
    tid, tg = await _make_ticket(tg_id=910004)
    assert await csat_service.save_rating(ticket_id=tid, user_tg_id=tg, rating=0) == "not_found"
    assert await csat_service.save_rating(ticket_id=tid, user_tg_id=tg, rating=6) == "not_found"
    assert await csat_service.save_rating(ticket_id=tid, user_tg_id=tg, rating=3) == "ok"


async def test_missing_ticket(initialized_db):
    assert await csat_service.save_rating(
        ticket_id=98765432, user_tg_id=1, rating=5,
    ) == "not_found"


def test_keyboard_shape():
    kb = csat_service.build_rating_keyboard(123)
    row = kb.inline_keyboard[0]
    assert len(row) == 5
    assert [b.callback_data for b in row] == [f"csat:123:{n}" for n in range(1, 6)]
