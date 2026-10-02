"""Every closure commits its purpose-bound notification before transport."""

import asyncio
from unittest.mock import AsyncMock

import pytest
from aiogram.filters import CommandObject
from aiogram.types import CallbackQuery, Message as TelegramMessage
from sqlalchemy import select
from starlette.requests import Request

from src.channels.identity import create_customer
from src.database.db import db_session
from src.database.delivery_models import OutboundJob
from src.database.models import AICall, Message, Ticket, User
from src.handlers import admin_handlers, ticket_actions
from src.services import choice_service, csat_service, outbound
from src.utils.callback_data import build_callback_data
from tests.conftest import next_tg_id
from tests.test_telegram_operator_security import _callback, _message, _operator, _state, _ticket


async def _jobs(ticket_id):
    async with db_session() as session:
        return list((await session.execute(select(OutboundJob).join(
            Message, Message.id == OutboundJob.message_id).where(
                Message.ticket_id == ticket_id, OutboundJob.purpose == "ticket-close"
            ))).scalars())


class SimulatedCrash(BaseException):
    pass


@pytest.mark.parametrize("entry", ["dashboard", "command", "callback", "custom"])
async def test_close_survives_a_crash_before_delivery(initialized_db, channel, monkeypatch, entry):
    from src.dashboard import bot_client
    from src.dashboard.routes import tickets as routes
    monkeypatch.setattr(bot_client, "get_bot", lambda: None)
    monkeypatch.setattr(TelegramMessage, "reply", AsyncMock())
    monkeypatch.setattr(TelegramMessage, "edit_text", AsyncMock())
    monkeypatch.setattr(CallbackQuery, "answer", AsyncMock())
    monkeypatch.setattr(csat_service, "send_close_notification", AsyncMock(side_effect=SimulatedCrash))
    actor = await _operator()
    ticket, user = await _ticket()
    state = _state(actor.tg_id)
    prompt = _message(actor.tg_id)
    await state.update_data(ticket_id=ticket.id, selected_categories=["Solved"],
                            custom_reason_prompt_id=prompt.message_id)
    with pytest.raises(SimulatedCrash):
        if entry == "dashboard":
            await routes.ticket_close(Request({"type": "http", "session": {}}), ticket.id,
                                      categories=["Solved"], custom_reason="")
        elif entry == "command":
            await admin_handlers.handle_close_ticket(_message(actor.tg_id), None,
                CommandObject(prefix="/", command="close", args=str(ticket.id)))
        elif entry == "callback":
            callback = _callback(actor.tg_id, message=prompt, data=build_callback_data(
                "admin", "ticket", "close_confirm", {"i": ticket.id}))
            await ticket_actions.close_ticket_execute(callback, state, None)
        else:
            await ticket_actions.handle_custom_reason_input(
                _message(actor.tg_id, text="Resolved together", reply=prompt), state, None)
    async with db_session() as session:
        assert (await session.get(Ticket, ticket.id)).status == "closed"
    jobs = await _jobs(ticket.id)
    assert len(jobs) == 1 and jobs[0].status == "pending"
    assert channel.texts == []
    # A restarted worker can complete the exact committed intent.
    assert (await outbound.deliver(jobs[0].id)).ok
    assert len(channel.texts) == 1 and channel.texts[0][0] == str(user.tg_id)


async def test_ai_accepted_verdict_commits_close_and_notice_together(initialized_db, channel):
    ticket, user = await _ticket()
    async with db_session() as session:
        call = AICall(ticket_id=ticket.id, use_case="deflect", model="test",
                      success=True, shadow=False)
        session.add(call)
        await session.commit()
    result = await choice_service.apply_deflection_verdict(ticket_id=ticket.id, ai_call_id=call.id,
                                                          user_tg_id=user.tg_id, accepted=True)
    assert result.closed
    jobs = await _jobs(ticket.id)
    assert len(jobs) == 1 and jobs[0].status == "pending"
    assert channel.texts == []


async def test_rollback_does_not_leave_half_a_closure(initialized_db, channel):
    ticket, user = await _ticket()
    with pytest.raises(RuntimeError, match="rollback"):
        async with db_session(write=True) as session:
            current = await session.get(Ticket, ticket.id)
            current.status = "closed"
            await csat_service.enqueue_close_notification(session, current, await session.get(User, user.id))
            raise RuntimeError("rollback")
    assert await _jobs(ticket.id) == []
    async with db_session() as session:
        assert (await session.get(Ticket, ticket.id)).status == "active"
    assert channel.texts == []


async def test_concurrent_close_requests_create_one_job_and_one_delivery(initialized_db, channel, monkeypatch):
    from src.dashboard import bot_client
    from src.dashboard.routes import tickets as routes
    monkeypatch.setattr(bot_client, "get_bot", lambda: None)
    ticket, user = await _ticket()
    responses = await asyncio.gather(*[
        routes.ticket_close(Request({"type": "http", "session": {}}), ticket.id,
                            categories=["Solved"], custom_reason="") for _ in range(3)
    ])
    assert all(r.status_code == 303 for r in responses)
    assert len(await _jobs(ticket.id)) == 1
    assert len(channel.texts) == 1
    # An already-consumed keyboard must not erase the notification's purpose.
    job = (await _jobs(ticket.id))[0]
    async with db_session() as session:
        (await session.get(Message, job.message_id)).options = None
    result = await csat_service.send_close_notification(None, user_tg_id=user.tg_id,
                                                       ticket_id=ticket.id, lang="en")
    assert result.ok
    assert len(channel.texts) == 1 and len(await _jobs(ticket.id)) == 1


async def test_an_unconfigured_channel_does_not_discard_the_close(initialized_db):
    async with db_session(write=True) as session:
        user = await create_customer(session, channel="not-configured", address=str(next_tg_id()))
        ticket = Ticket(user_id=user.id, category="", status="closed")
        session.add(ticket)
        await session.flush()
        job = await csat_service.enqueue_close_notification(session, ticket, user)
        job_id, ticket_id = job.id, ticket.id
    delivery = await outbound.deliver(job_id)
    assert not delivery.ok
    async with db_session() as session:
        assert (await session.get(Ticket, ticket_id)).status == "closed"
        job = await session.get(OutboundJob, job_id)
        row = await session.get(Message, job.message_id)
        assert row.delivery_status == job.status == "failed"
        assert row.delivery_error


async def test_competing_ratings_keep_the_first_value(initialized_db):
    ticket, user = await _ticket()
    async with db_session(write=True) as session:
        (await session.get(Ticket, ticket.id)).status = "closed"
    result = await asyncio.gather(*[csat_service.save_rating(
        ticket_id=ticket.id, user_tg_id=user.tg_id, rating=rating) for rating in (5, 1)])
    assert sorted(result) == ["already", "ok"]
