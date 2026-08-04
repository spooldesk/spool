"""Queue health: 'awaiting reply' count + the poll payload."""

from src.dashboard import queries
from src.database.db import db_session
from src.database.models import Message, Team, Ticket, User


async def _make(status: str, *, with_admin_reply: bool, tg_id: int) -> int:
    async with db_session() as session:
        team = Team(name=f"q-team-{tg_id}")
        session.add(team)
        await session.flush()
        user = User(tg_id=tg_id, username=f"q{tg_id}", team_id=team.id,
                    is_verified=True, is_active=True)
        session.add(user)
        await session.flush()
        t = Ticket(user_id=user.id, team_id=team.id, category="VPN", status=status)
        session.add(t)
        await session.flush()
        session.add(Message(ticket_id=t.id, sender_type="user",
                            sender_id=tg_id, text="помогите"))
        if with_admin_reply:
            session.add(Message(ticket_id=t.id, sender_type="admin",
                                sender_id=1, text="сейчас гляну"))
        tid = t.id
        await session.commit()
    return tid


async def test_awaiting_counts_only_unanswered_active(initialized_db):
    before = (await queries.queue_status())["awaiting"]

    unanswered = await _make("active", with_admin_reply=False, tg_id=930001)
    answered = await _make("active", with_admin_reply=True, tg_id=930002)
    closed_unanswered = await _make("closed", with_admin_reply=False, tg_id=930003)

    status = await queries.queue_status()
    # exactly one new ticket enters the queue: active + no admin reply
    assert status["awaiting"] == before + 1
    assert status["latest_ticket_id"] >= max(unanswered, answered, closed_unanswered)


async def test_overview_stats_exposes_awaiting(initialized_db):
    stats = await queries.overview_stats()
    assert "awaiting_response" in stats
    assert isinstance(stats["awaiting_response"], int)
    assert stats["awaiting_response"] >= 0
