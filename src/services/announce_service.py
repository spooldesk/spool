"""Putting a new ticket in front of the operators, whatever channel it came from.

The product's whole promise is that agents answer from Telegram without
learning another inbox. That was only ever true for tickets that arrived
*through* Telegram: the forum-topic header was built inside the bot's own
intake handler, so a widget visitor or a Slack conversation opened a ticket
that existed on the board and nowhere else. The pitch held for one channel out
of three, and the ones we added last made it less true, not more.

This is the announcement, separated from the channel that triggers it.

Two things about the Telegram forum mechanics are worth writing down, because
they look like bugs when you meet them:

Sending the header *without* a `message_thread_id` is what creates the topic.
Passing one would file the ticket under an existing conversation instead.

The topic's id then comes back on the header message itself, and it equals the
header's own message id. That is not a coincidence to code around — it is how
Telegram numbers a topic whose first message created it.
"""

from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from src.channels import DEFAULT as TELEGRAM
from src.channels.identity import channel_of, display_address
from src.database.db import db_session
from src.database.models import Message as MessageRecord, Team, Ticket

logger = logging.getLogger(__name__)


def _op(key: str, **kwargs) -> str:
    """Operator-facing text: the admin group's language, not the customer's."""
    from src.dashboard.i18n import t as dash_t
    return dash_t(key, **kwargs)


def ticket_action_keyboard(ticket_id: int) -> InlineKeyboardMarkup:
    """The buttons under a forwarded message in the admin chat.

    Templates, ask the AI, close — so an operator never has to leave the
    thread. Every button carries the ticket id rather than making the handler
    re-derive it from a reply chain, which breaks the moment somebody replies
    to the wrong message.
    """
    from src.utils.callback_data import build_callback_data

    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text=_op("op_btn_templates"),
                callback_data=build_callback_data(
                    "admin", "templates", "pick", {"t": ticket_id}),
            ),
            InlineKeyboardButton(
                text=_op("op_btn_ai"),
                callback_data=build_callback_data("admin", "ai", "ask", {"t": ticket_id}),
            ),
        ],
        [
            InlineKeyboardButton(
                text=_op("op_btn_close"),
                callback_data=build_callback_data(
                    "admin", "ticket", "close", {"i": ticket_id}),
            ),
        ],
    ])


async def _header_text(ticket: Ticket, team_name: str) -> str:
    """What an operator reads before they read the customer.

    The channel line is the addition. On a Telegram-only instance it says
    Telegram every time and costs a line; on any other it is the difference
    between "reply here" and "this person is not in Telegram at all".
    """
    from src.services import settings_service

    user = ticket.user
    channel = channel_of(user) if user is not None else TELEGRAM
    username = (user.username if user else None) or ""
    who = f"@{username}" if username else _op(f"ch_{channel}", ) or channel

    # Tags are per category and set by the instance; a ticket that arrived with
    # no category (the widget and Slack cannot ask) simply tags nobody.
    tags = await settings_service.get_tags_for_category(ticket.category or "")

    lines = [
        f"{tags + ' ' if tags else ''}#ticket",
        f"Ticket ID: {ticket.id}",
        f"Channel: {_op(f'ch_{channel}') or channel}",
        f"Team: {team_name}",
        f"User: {who} ({display_address(user) if user else '—'})",
        f"Category: {ticket.category or '—'}",
        "Status: Open",
    ]
    return "\n".join(lines)


async def _thread_for(bot: Bot, chat_id: int, header_message_id: int) -> int | None:
    """The topic id, for chats where the header did not report one itself.

    Costs an extra message in the group, so it is only reached when Telegram
    did not hand the id back — an ordinary group rather than a forum, usually,
    where the answer is legitimately None and replies fall back to quoting the
    header.
    """
    try:
        probe = await bot.send_message(
            chat_id=chat_id, reply_to_message_id=header_message_id, text="💬",
        )
        return probe.message_thread_id
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Could not determine a thread id: {e}")
        return None


async def announce_ticket(bot: Bot, ticket_id: int) -> bool:
    """Post a new ticket into the admin group and open its thread.

    Idempotent: a ticket that already has a header is left alone, so a retried
    webhook or a double-submitted widget message cannot produce two topics for
    one conversation.

    Returns whether the operators can now see it. Never raises — a helpdesk
    that refuses a customer's message because the *operators'* chat was
    unreachable has its priorities backwards.
    """
    from src.config import load_settings

    try:
        settings = load_settings()
    except Exception as e:  # noqa: BLE001 — an instance with no bot configured
        logger.debug(f"No Telegram settings, not announcing #{ticket_id}: {e}")
        return False

    async with db_session() as session:
        ticket = (await session.execute(
            select(Ticket).where(Ticket.id == ticket_id)
            .options(selectinload(Ticket.user))
        )).scalar_one_or_none()
        if ticket is None:
            return False
        if ticket.header_message_id is not None:
            # Already announced. Said at debug rather than warning: retries are
            # normal, and Slack in particular resends on any slow response.
            logger.debug(f"Ticket #{ticket_id} already announced")
            return True

        team_name = "—"
        if ticket.team_id:
            team_name = (await session.execute(
                select(Team.name).where(Team.id == ticket.team_id)
            )).scalar_one_or_none() or "—"

        header = await _header_text(ticket, team_name)
        # The opening message, so an operator sees the question and not just
        # that a question exists.
        opening = (await session.execute(
            select(MessageRecord)
            .where(MessageRecord.ticket_id == ticket_id,
                   MessageRecord.sender_type == "user")
            .order_by(MessageRecord.id.asc())
        )).scalars().first()
        opening_text = (opening.text if opening else "") or ""
        opening_file = opening.file_name if opening else None

    try:
        # No message_thread_id on purpose: in a forum group that is what opens
        # a new topic. Passing one would file this under an existing ticket.
        header_msg = await bot.send_message(
            chat_id=settings.admin_chat_id, text=header,
        )
    except Exception as e:  # noqa: BLE001
        logger.error(f"Could not announce ticket #{ticket_id}: {e}")
        return False

    thread_id = header_msg.message_thread_id
    if thread_id is None:
        thread_id = await _thread_for(
            bot, settings.admin_chat_id, header_msg.message_id)

    async with db_session() as session:
        ticket = await session.get(Ticket, ticket_id)
        if ticket is not None:
            ticket.header_message_id = header_msg.message_id
            ticket.thread_id = thread_id
            await session.commit()

    # The customer's words, in the thread, with the buttons that let an
    # operator answer without leaving it.
    body = opening_text
    if opening_file:
        body = f"{body}\n📎 {opening_file}".strip()
    if body:
        kwargs: dict = {
            "chat_id": settings.admin_chat_id,
            "text": body,
            "reply_markup": ticket_action_keyboard(ticket_id),
        }
        if thread_id is not None:
            kwargs["message_thread_id"] = thread_id
        else:
            kwargs["reply_to_message_id"] = header_msg.message_id
        try:
            await bot.send_message(**kwargs)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Ticket #{ticket_id} announced without its first message: {e}")

    logger.info(
        f"Ticket #{ticket_id} announced to the admin chat "
        f"(thread={thread_id}, header={header_msg.message_id})"
    )
    return True
