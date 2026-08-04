"""AI deflection: safety gates, AI-close path, outcome logging.

No LLM is configured in tests (GEMINI_API_KEY removed in conftest), so
try_deflect_for_ticket must always bail out silently — that's the exact
degradation path production takes without a key.
"""

from datetime import datetime

from sqlalchemy import select

from src.ai import deflect
from src.database.db import db_session
from src.database.models import AICall, Message, Team, Ticket, User
from src.services import settings_service


async def _make_ticket(tg_id: int, *, status: str = "active") -> int:
    async with db_session() as session:
        team = Team(name=f"defl-team-{tg_id}")
        session.add(team)
        await session.flush()
        user = User(tg_id=tg_id, username=f"d{tg_id}", team_id=team.id,
                    is_verified=True, is_active=True)
        session.add(user)
        await session.flush()
        t = Ticket(user_id=user.id, team_id=team.id, category="VPN", status=status,
                   created_at=datetime.utcnow())
        session.add(t)
        await session.flush()
        session.add(Message(ticket_id=t.id, sender_type="user", sender_id=tg_id,
                            text="Не подключается VPN, ошибка 809"))
        tid = t.id
        await session.commit()
    return tid


async def test_no_llm_key_means_silent_skip(initialized_db):
    """Without GEMINI_API_KEY the gate must return None and log nothing."""
    await settings_service.set_ai_deflection_enabled(True, updated_by=0)
    tid = await _make_ticket(950001)
    res = await deflect.try_deflect_for_ticket(tid, "Не подключается VPN, ошибка 809")
    assert res.answer is None and res.ai_call_id is None
    await settings_service.set_ai_deflection_enabled(False, updated_by=0)


async def test_setting_off_means_skip(initialized_db):
    await settings_service.set_ai_deflection_enabled(False, updated_by=0)
    tid = await _make_ticket(950002)
    res = await deflect.try_deflect_for_ticket(tid, "Длинный осмысленный вопрос про VPN")
    assert res.answer is None


async def test_short_text_skipped(initialized_db):
    await settings_service.set_ai_deflection_enabled(True, updated_by=0)
    tid = await _make_ticket(950003)
    res = await deflect.try_deflect_for_ticket(tid, "вэпэн")
    assert res.answer is None
    await settings_service.set_ai_deflection_enabled(False, updated_by=0)


async def test_close_by_ai_owner_only(initialized_db):
    tid = await _make_ticket(950004)
    # Wrong user cannot close
    assert not await deflect.close_ticket_by_ai(tid, 999999999)
    # Owner closes: status, reason snapshot, processing time, closure marker
    assert await deflect.close_ticket_by_ai(tid, 950004)
    async with db_session() as session:
        t = await session.get(Ticket, tid)
        assert t.status == "closed"
        assert t.admin_categories == [deflect.AI_CLOSE_REASON]
        assert t.closed_at is not None
        assert t.processing_time_seconds is not None
        marker = (await session.execute(
            select(Message).where(Message.ticket_id == tid,
                                  Message.sender_type == "admin")
        )).scalars().all()
        assert any(deflect.AI_CLOSE_REASON in (m.text or "") for m in marker)
    # Second close attempt (already closed) → False
    assert not await deflect.close_ticket_by_ai(tid, 950004)


async def test_mark_outcome_once(initialized_db):
    async with db_session() as session:
        row = AICall(use_case="deflect", model="test", success=True,
                     suggested_text="ответ")
        session.add(row)
        await session.flush()
        call_id = row.id
        await session.commit()

    await deflect.mark_outcome(call_id, accepted=True)
    async with db_session() as session:
        assert (await session.get(AICall, call_id)).accepted is True

    # A second (contradictory) verdict must not overwrite the first
    await deflect.mark_outcome(call_id, accepted=False)
    async with db_session() as session:
        assert (await session.get(AICall, call_id)).accepted is True


def test_keyboard_callback_shape():
    kb = deflect._deflect_keyboard(12, 34, "ru")
    row = kb.inline_keyboard[0]
    assert [b.callback_data for b in row] == ["defl:ok:12:34", "defl:no:12:34"]
