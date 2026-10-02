"""Authorized Telegram operator commands and channel-neutral customer replies."""

from datetime import datetime
import logging
import re

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from src.channels.base import Delivery
from src.channels.identity import address_of, channel_of
from src.config import load_settings
from src.database.db import db_session
from src.database.models import Message as MessageRecord, Ticket
from src.services import template_service
from src.services.operator_acl import actor_for, operator_only
from src.utils.callback_data import build_callback_data
from src.utils.file_processor import attachment_filename, extract_attachment, ATTACHMENT_SIZE_LIMIT
from src.utils.i18n import CLOSURE_SYSTEM_TEXT

logger = logging.getLogger(__name__)
from src.config import runtime_settings as settings


def _in_admin_chat(message: Message) -> bool:
    return bool(settings.admin_chat_id and message.chat.id == settings.admin_chat_id)

admin_router = Router()
_EXPLICIT_TICKET = re.compile(r"^#([1-9][0-9]*)(?=\s|$)")


def _op(key: str, **kwargs) -> str:
    from src.dashboard.i18n import t
    return t(key, **kwargs)


async def _find_ticket_by_thread_id(session, thread_id: int) -> Ticket | None:
    # General (1) is shared, never a customer conversation. Older releases
    # stored this shared id; do not guess a recipient from those rows.
    if not thread_id or thread_id == 1:
        return None
    rows = list((await session.execute(
        select(Ticket).where(Ticket.thread_id == thread_id, Ticket.status == "active")
        .options(selectinload(Ticket.user)).limit(2)
    )).scalars())
    return rows[0] if len(rows) == 1 else None


async def _find_ticket_by_message_id(session, message_id: int) -> Ticket | None:
    if not message_id:
        return None
    rows = list((await session.execute(
        select(Ticket).where(
            Ticket.status == "active",
            (Ticket.header_message_id == message_id) | Ticket.id.in_(
                select(MessageRecord.ticket_id).where(MessageRecord.admin_message_id == message_id)
            ),
        ).options(selectinload(Ticket.user)).limit(2)
    )).scalars())
    return rows[0] if len(rows) == 1 else None


async def _find_ticket_by_reply(session, replied_message: Message) -> Ticket | None:
    # Never parse somebody's quoted text as a routing instruction. A known
    # Telegram message id is the server-side association to a ticket.
    if replied_message is None:
        return None
    return await _find_ticket_by_message_id(session, replied_message.message_id)


async def _find_ticket_by_text(session, text: str) -> Ticket | None:
    match = _EXPLICIT_TICKET.match(text or "")
    if match is None:
        return None
    return (await session.execute(
        select(Ticket).where(Ticket.id == int(match[1]), Ticket.status == "active")
        .options(selectinload(Ticket.user))
    )).scalar_one_or_none()


async def _find_ticket_for_admin_message(session, message: Message) -> Ticket | None:
    # A direct reply is strongest, especially for pre-upgrade General-topic
    # tickets. An unknown reply must not redirect a private aside into a
    # different thread's customer conversation.
    if message.reply_to_message:
        return await _find_ticket_by_reply(session, message.reply_to_message)
    if _EXPLICIT_TICKET.match(message.text or message.caption or ""):
        return await _find_ticket_by_text(session, message.text or message.caption or "")
    return await _find_ticket_by_thread_id(session, message.message_thread_id)


async def _forward_admin_message_to_user(
    bot: Bot, user_tg_id: int, text_content: str, attachment, ticket_id: int = None,
    channel: str | None = None, address: str | None = None,
    *, sender_id: int = 0, operator_id: int | None = None,
    admin_message_id: int | None = None,
) -> Delivery:
    """Persist a delivery intent and send using the customer's adapter."""
    from src.services import outbound
    from src.channels import get as get_channel
    from src.channels.telegram import TelegramChannel

    if ticket_id is None:
        return Delivery.failed("e_ticket_not_found")
    target = address or str(user_tg_id)
    if attachment is None:
        return await outbound.say(
            ticket_id=ticket_id, channel=channel, address=target,
            text=text_content, sender_id=sender_id, operator_id=operator_id,
            admin_message_id=admin_message_id,
        )
    try:
        if not get_channel(channel).caps.files:
            return Delivery.failed("e_no_files_on_channel")
        if attachment.file_size and attachment.file_size > ATTACHMENT_SIZE_LIMIT:
            return Delivery.failed("e_send_rejected", "attachment exceeds the size limit")
        content = await TelegramChannel(bot).fetch_file(attachment.file_id)
        if not content or len(content) > ATTACHMENT_SIZE_LIMIT:
            return Delivery.failed("e_send_failed", "attachment could not be downloaded")
        return await outbound.say_file(
            ticket_id=ticket_id, channel=channel, address=target,
            filename=attachment_filename(attachment), content=content,
            caption=text_content or None, sender_id=sender_id,
            operator_id=operator_id, mime_type=attachment.mime_type,
            admin_message_id=admin_message_id,
        )
    except Exception:
        logger.exception("Could not relay operator attachment on ticket %s", ticket_id)
        return Delivery.failed("e_send_failed")


@admin_router.message(_in_admin_chat, Command("close"))
@operator_only()
async def handle_close_ticket(message: Message, bot: Bot, command: CommandObject) -> None:
    from src.services import csat_service
    async with db_session(write=True) as session:
        # An explicit command argument wins; reject malformed arguments rather
        # than closing the thread's ticket despite a typo in the command.
        if command.args:
            argument = command.args.strip()
            ticket = (await _find_ticket_by_text(session, "#" + argument.removeprefix("#"))
                      if re.fullmatch(r"#?[1-9][0-9]*", argument) else None)
        else:
            ticket = await _find_ticket_for_admin_message(session, message)
        if ticket is None:
            await message.reply(_op("op_close_usage"))
            return
        ticket.status = "closed"
        ticket.closed_at = datetime.utcnow()
        ticket.notification_sent = False
        ticket.processing_time_seconds = max(0, int((ticket.closed_at - ticket.created_at).total_seconds()))
        ticket_id, user_tg_id, language = ticket.id, ticket.user.tg_id, ticket.user.language
        session.add(MessageRecord(ticket_id=ticket.id, sender_type="admin",
                                  sender_id=message.from_user.id, text=CLOSURE_SYSTEM_TEXT))
        job = await csat_service.enqueue_close_notification(session, ticket, ticket.user)
        closing_job_id = job.id
        await session.commit()
    await message.reply(_op("op_closed", id=ticket_id))
    await csat_service.send_close_notification(bot, user_tg_id=user_tg_id,
                                               ticket_id=ticket_id, lang=language, job_id=closing_job_id)


@admin_router.message(_in_admin_chat, Command("t"))
@operator_only()
async def handle_template_command(message: Message, bot: Bot) -> None:
    async with db_session() as session:
        ticket = await _find_ticket_for_admin_message(session, message)
        if ticket is None:
            await message.reply(_op("op_t_usage"))
            return
        templates = await template_service.list_for_ticket(session, ticket, limit=8)
    if not templates:
        await message.reply(_op("op_templates_none"))
        return
    rows = [[InlineKeyboardButton(text=t.title, callback_data=build_callback_data(
        "admin", "templates", "apply", {"t": ticket.id, "i": t.id}))] for t in templates]
    rows.append([InlineKeyboardButton(text=_op("op_close_list_btn"), callback_data=build_callback_data(
        "admin", "templates", "dismiss", {"t": ticket.id}))])
    await message.reply(_op("op_templates_for", id=ticket.id),
                        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@admin_router.message(_in_admin_chat)
@operator_only()
async def handle_admin_reply(message: Message, bot: Bot) -> None:
    text = message.text or message.caption or ""
    if text.startswith("/"):
        return
    attachment = extract_attachment(message)
    if not text and attachment is None:
        return
    actor = await actor_for(message)
    if actor is None:
        return
    async with db_session() as session:
        ticket = await _find_ticket_for_admin_message(session, message)
        if ticket is None or ticket.user is None:
            return
        ticket_id = ticket.id
        user_tg_id = ticket.user.tg_id
        channel, address = channel_of(ticket.user), address_of(ticket.user)
    match = _EXPLICIT_TICKET.match(text)
    # Never strip a number from ordinary customer-facing text.
    if match and int(match[1]) == ticket_id:
        text = text[match.end():].lstrip()
    if not text and attachment is None:
        return
    delivery = await _forward_admin_message_to_user(
        bot, user_tg_id, text, attachment, ticket_id=ticket_id,
        channel=channel, address=address, sender_id=message.from_user.id,
        operator_id=actor.id,
        admin_message_id=message.message_id,
    )
    if not delivery.ok:
        await message.reply(_op("op_deliver_failed", tg=user_tg_id, id=ticket_id,
                                reason=_op(delivery.error_key or "e_send_failed")))
        return
    from src.services import assignment_service, workflow_service
    await assignment_service.auto_assign(ticket_id, actor.id)
