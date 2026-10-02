"""Telegram is an optional operator interface with the same account boundary.

Use real Bot API models; no network, production database or attachments.
"""

from datetime import datetime, timezone
from io import BytesIO
from unittest.mock import AsyncMock

import pytest
from aiogram.dispatcher.event.bases import UNHANDLED
from aiogram.filters import CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Chat, File, Message as TelegramMessage, Sticker, User as TelegramUser
from sqlalchemy import select

import src.channels as channels
from src.channels.identity import create_customer
from src.config import load_settings
from src.database.db import db_session
from src.database.models import AdminUser, AICall, Message, Ticket
from src.handlers import admin_handlers, ticket_actions
from src.services import operator_acl
from src.utils.file_processor import AttachmentData, extract_attachment
from tests.conftest import RecordingChannel, next_tg_id


def _message(tg_id, *, text="hello", message_id=None, chat_id=None, thread=None, reply=None, **extra):
    return TelegramMessage(
        message_id=message_id or next_tg_id(), date=datetime.now(timezone.utc),
        chat=Chat(id=chat_id or load_settings().admin_chat_id, type="supergroup"),
        from_user=TelegramUser(id=tg_id, is_bot=False, first_name="Operator"),
        text=text, message_thread_id=thread, reply_to_message=reply, **extra,
    )


def _callback(tg_id, *, message=None, data="admin:noop:x"):
    return CallbackQuery(id=str(next_tg_id()), chat_instance="test-instance",
                         from_user=TelegramUser(id=tg_id, is_bot=False, first_name="Operator"),
                         message=message, data=data)


async def _operator(*, role="agent", active=True):
    tg = next_tg_id()
    async with db_session() as session:
        row = AdminUser(email=f"acl-{tg}@example.test", password_hash="not-used",
                        tg_id=tg, role=role, is_active=active)
        session.add(row)
        await session.commit()
    return row


async def _ticket(*, channel="telegram", thread=None, header=None):
    address = str(next_tg_id()) if channel == "telegram" else f"visitor-{next_tg_id()}"
    async with db_session() as session:
        user = await create_customer(session, channel=channel, address=address)
        row = Ticket(user_id=user.id, category="", thread_id=thread, header_message_id=header)
        session.add(row)
        await session.commit()
    return row, user


def _state(tg_id):
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1,
                      chat_id=load_settings().admin_chat_id, user_id=tg_id))


@pytest.mark.parametrize("role,active,allowed", [
    ("owner", True, True), ("admin", True, True), ("agent", True, True),
    ("viewer", True, False), ("unknown", True, False), ("owner", False, False),
])
async def test_roles_are_checked_for_messages_and_callbacks(initialized_db, role, active, allowed):
    actor = await _operator(role=role, active=active)
    message = _message(actor.tg_id)
    for event in (message, _callback(actor.tg_id, message=message)):
        result = await operator_acl.actor_for(event)
        assert bool(result) is allowed
        if allowed:
            assert result.id == actor.id


async def test_a_group_member_or_wrong_chat_does_not_grant_access(initialized_db):
    linked = await _operator()
    assert await operator_acl.actor_for(_message(next_tg_id())) is None
    assert await operator_acl.actor_for(_message(linked.tg_id, chat_id=-42)) is None
    assert await operator_acl.actor_for(_callback(linked.tg_id)) is None
    anonymous = _message(linked.tg_id, sender_chat=Chat(id=-123, type="supergroup"))
    assert await operator_acl.actor_for(anonymous) is None


async def test_acl_rechecks_an_existing_fsm_after_revocation(initialized_db):
    actor = await _operator()
    state = _state(actor.tg_id)
    await state.set_state(ticket_actions.AdminStates.AI_DRAFT_EDIT)
    await state.update_data(ai_ticket_id=13, ai_prompt_id=123)
    async with db_session(write=True) as session:
        (await session.get(AdminUser, actor.id)).is_active = False
    # No attempt to read/send the draft, and no mutation of this operator's FSM.
    await ticket_actions.handle_ai_draft_edited(_message(actor.tg_id), state, None)
    assert await state.get_state() == ticket_actions.AdminStates.AI_DRAFT_EDIT.state
    assert (await state.get_data())["ai_ticket_id"] == 13


def test_every_registered_operator_handler_has_an_account_guard():
    for router in (admin_handlers.admin_router, ticket_actions.ticket_actions_router):
        for observer in (router.message, router.callback_query):
            for registered in observer.handlers:
                assert registered.callback.operator_roles <= operator_acl.TICKET_ROLES
    assert ticket_actions.assign_team_execute.operator_roles == operator_acl.MANAGE_ROLES
    assert ticket_actions.assign_team_legacy.operator_roles == operator_acl.MANAGE_ROLES


async def test_an_agent_cannot_assign_a_customer_team(initialized_db, monkeypatch):
    actor = await _operator(role="agent")
    change = AsyncMock()
    monkeypatch.setattr(ticket_actions, "_assign_team_to_user", change)
    callback = _callback(actor.tg_id, message=_message(actor.tg_id), data="admin:team:assign:i1")
    await ticket_actions.assign_team_execute(callback, _state(actor.tg_id))
    await ticket_actions.assign_team_legacy(callback, _state(actor.tg_id))
    change.assert_not_awaited()


@pytest.mark.parametrize("text", ["Meeting in {id} minutes", "{id} okay", "Ticket ID: {id}", "#{id}x", "0{id}"])
async def test_numbers_in_group_chatter_are_not_customer_addresses(initialized_db, text):
    ticket, _ = await _ticket()
    async with db_session() as session:
        assert await admin_handlers._find_ticket_for_admin_message(
            session, _message(next_tg_id(), text=text.format(id=ticket.id))) is None


async def test_explicit_prefix_and_stored_header_or_mirror_route_exactly(initialized_db):
    header, mirror = next_tg_id(), next_tg_id()
    ticket, _ = await _ticket(header=header)
    async with db_session() as session:
        session.add(Message(ticket_id=ticket.id, sender_type="user", sender_id=1,
                            text="incoming", admin_message_id=mirror))
        await session.commit()
    async with db_session() as session:
        events = [_message(1, text=f"#{ticket.id} Here is your answer")]
        events += [_message(1, reply=_message(1, message_id=ref)) for ref in (header, mirror)]
        for event in events:
            found = await admin_handlers._find_ticket_for_admin_message(session, event)
            assert found.id == ticket.id


async def test_a_forged_quoted_label_does_not_route_even_in_a_known_topic(initialized_db):
    topic = next_tg_id()
    ticket, _ = await _ticket(thread=topic)
    forged = _message(1, text=f"Ticket ID: {ticket.id}")
    async with db_session() as session:
        assert await admin_handlers._find_ticket_for_admin_message(
            session, _message(1, text=f"#{ticket.id} private aside", thread=topic, reply=forged)) is None


async def test_general_and_ambiguous_legacy_topics_are_never_routed(initialized_db):
    topic = next_tg_id()
    await _ticket(thread=1)
    await _ticket(thread=topic)
    await _ticket(thread=topic)
    async with db_session() as session:
        assert await admin_handlers._find_ticket_by_thread_id(session, 1) is None
        assert await admin_handlers._find_ticket_by_thread_id(session, topic) is None


@pytest.mark.parametrize("bad_argument", ["{id} extra", "##{id}", "{id}x", "0{id}"])
async def test_malformed_close_argument_cannot_close_the_current_topic(initialized_db, monkeypatch, bad_argument):
    actor = await _operator()
    topic = next_tg_id()
    ticket, _ = await _ticket(thread=topic)
    monkeypatch.setattr(TelegramMessage, "reply", AsyncMock())
    command = CommandObject(prefix="/", command="close", args=bad_argument.format(id=ticket.id))
    await admin_handlers.handle_close_ticket(_message(actor.tg_id, thread=topic), None, command)
    async with db_session() as session:
        assert (await session.get(Ticket, ticket.id)).status == "active"


async def test_unlinked_operator_cannot_close_or_forward(initialized_db, monkeypatch):
    topic = next_tg_id()
    ticket, _ = await _ticket(thread=topic)
    relay = AsyncMock()
    monkeypatch.setattr(admin_handlers, "_forward_admin_message_to_user", relay)
    message = _message(next_tg_id(), thread=topic)
    await admin_handlers.handle_close_ticket(message, None, CommandObject(prefix="/", command="close"))
    await admin_handlers.handle_admin_reply(message, None)
    relay.assert_not_awaited()
    async with db_session() as session:
        assert (await session.get(Ticket, ticket.id)).status == "active"


async def test_a_topic_reply_preserves_ordinary_leading_numbers_and_records_operator(initialized_db, channel):
    actor = await _operator()
    topic = next_tg_id()
    ticket, user = await _ticket(thread=topic)
    message = _message(actor.tg_id, text="12 minutes remaining", thread=topic)
    await admin_handlers.handle_admin_reply(message, None)
    assert channel.texts == [(str(user.tg_id), "12 minutes remaining")]
    async with db_session() as session:
        rows = list((await session.execute(select(Message).where(Message.ticket_id == ticket.id))).scalars())
    assert len(rows) == 1
    assert rows[0].operator_id == actor.id
    assert rows[0].delivery_status == "delivered"
    assert rows[0].admin_message_id == message.message_id


async def test_a_linked_operator_can_close_using_an_explicit_id(initialized_db, monkeypatch):
    from src.services import csat_service
    actor = await _operator()
    ticket, _ = await _ticket()
    monkeypatch.setattr(TelegramMessage, "reply", AsyncMock())
    notification = AsyncMock()
    monkeypatch.setattr(csat_service, "send_close_notification", notification)
    await admin_handlers.handle_close_ticket(
        _message(actor.tg_id), None, CommandObject(prefix="/", command="close", args=str(ticket.id)))
    async with db_session() as session:
        closed = await session.get(Ticket, ticket.id)
    assert closed.status == "closed" and closed.closed_at
    notification.assert_awaited_once()


async def test_an_ai_edit_only_consumes_a_reply_to_its_own_prompt(initialized_db):
    actor = await _operator()
    state = _state(actor.tg_id)
    await state.set_state(ticket_actions.AdminStates.AI_DRAFT_EDIT)
    await state.update_data(ai_ticket_id=1, ai_prompt_id=next_tg_id())
    assert await ticket_actions.handle_ai_draft_edited(_message(actor.tg_id), state, None) is UNHANDLED


async def test_an_ai_edit_cannot_send_a_draft_accepted_while_typing(initialized_db, monkeypatch):
    actor = await _operator()
    ticket, _ = await _ticket()
    async with db_session() as session:
        call = AICall(ticket_id=ticket.id, use_case="suggest_from_history", model="test",
                      success=True, shadow=False, suggested_text="draft", accepted=True)
        session.add(call)
        await session.commit()
    prompt = _message(actor.tg_id)
    state = _state(actor.tg_id)
    await state.update_data(ai_ticket_id=ticket.id, ai_call_id=call.id, ai_prompt_id=prompt.message_id)
    send = AsyncMock()
    monkeypatch.setattr(ticket_actions.template_service, "apply_text", send)
    monkeypatch.setattr(TelegramMessage, "reply", AsyncMock())
    await ticket_actions.handle_ai_draft_edited(_message(actor.tg_id, reply=prompt), state, None)
    send.assert_not_awaited()
    assert await state.get_data() == {}


@pytest.mark.parametrize("animated,video,name,mime", [
    (False, False, "sticker.webp", "image/webp"),
    (True, False, "sticker.tgs", "application/x-tgsticker"),
    (False, True, "sticker.webm", "video/webm"),
])
def test_real_sticker_schema_is_accepted(animated, video, name, mime):
    sticker = Sticker(file_id="opaque-file", file_unique_id="opaque-unique", type="regular",
                      width=512, height=512, is_animated=animated, is_video=video, file_size=19)
    attachment = extract_attachment(_message(1, text=None, sticker=sticker))
    assert attachment.file_name == name
    assert attachment.mime_type == mime


@pytest.mark.parametrize("target", ["relay", "widget"])
async def test_operator_file_is_downloaded_then_delivered_on_the_customer_channel(
    initialized_db, monkeypatch, tmp_path, target,
):
    monkeypatch.setenv("ATTACHMENTS_DIR", str(tmp_path))
    pipe = RecordingChannel(name=target)
    channels.register(pipe)
    try:
        ticket, user = await _ticket(channel=target)
        bot = AsyncMock()
        bot.get_file.return_value = File(file_id="source-tg-file", file_unique_id="uid",
                                        file_size=8, file_path="documents/log.txt")
        bot.download_file.return_value = BytesIO(b"test-log")
        attachment = AttachmentData("document", "source-tg-file", "uid", "log.txt", 8, "text/plain")
        result = await admin_handlers._forward_admin_message_to_user(
            bot, user.tg_id, "Here is the log", attachment, ticket_id=ticket.id,
            channel=target, address=user.channel_user_id,
        )
        assert result.ok
        bot.get_file.assert_awaited_once_with("source-tg-file")
        bot.download_file.assert_awaited_once_with("documents/log.txt")
        bot.send_document.assert_not_awaited()
        async with db_session() as session:
            rows = list((await session.execute(select(Message).where(Message.ticket_id == ticket.id))).scalars())
        assert len(rows) == 1
        assert rows[0].delivery_status == "delivered" and rows[0].file_id != "source-tg-file"
        if target == "widget":
            from src.services import file_store
            assert file_store.get(rows[0].file_id) == b"test-log"
        else:
            assert pipe.files == [{"address": user.channel_user_id, "filename": "log.txt",
                                   "content": b"test-log", "caption": "Here is the log"}]
    finally:
        channels.reset()
