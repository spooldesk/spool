"""Handlers for processing admin-side commands and replies in ticket threads."""

import logging
import re
import traceback
from datetime import datetime

from aiogram import Bot, Router, F
from aiogram.filters import Command, CommandObject
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNotFound,
    TelegramRetryAfter,
    TelegramUnauthorizedError,
)
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from src.config import load_settings
from src.database.db import db_session
from src.database.models import Message as MessageRecord, Ticket, User
from src.services import template_service
from src.utils.callback_data import build_callback_data
from src.utils.file_processor import extract_attachment, send_attachment
from src.utils.i18n import get_text, CLOSURE_SYSTEM_TEXT

logger = logging.getLogger(__name__)


def _op(key: str, **kwargs) -> str:
    """Text the support team reads in the admin chat — instance language."""
    from src.dashboard.i18n import t as dash_t
    return dash_t(key, **kwargs)

admin_router = Router()

settings = load_settings()


async def _find_ticket_by_thread_id(session, thread_id: int) -> Ticket | None:
    """Find an active ticket by thread_id."""
    if not thread_id:
        return None
    result = await session.execute(
        select(Ticket)
        .where(Ticket.thread_id == thread_id, Ticket.status == "active")
        .options(selectinload(Ticket.user))
    )
    return result.scalar_one_or_none()


async def _find_ticket_by_message_id(session, message_id: int) -> Ticket | None:
    """Find an active ticket by header_message_id (most reliable method)."""
    if not message_id:
        return None
    result = await session.execute(
        select(Ticket)
        .where(Ticket.header_message_id == message_id, Ticket.status == "active")
        .options(selectinload(Ticket.user))
    )
    return result.scalar_one_or_none()


async def _find_ticket_by_reply(session, replied_message: Message) -> Ticket | None:
    """
    Find ticket by analyzing the replied message.
    Uses multiple methods in priority order:
    1. header_message_id (most reliable - direct match)
    2. Parse ticket header text (contains "Ticket ID: X")
    """
    if not replied_message:
        return None

    # Method 1: Direct match by message_id (most reliable)
    # This works for any message in the ticket thread, not just the header
    ticket = await _find_ticket_by_message_id(session, replied_message.message_id)
    if ticket:
        logger.debug(f"Found ticket #{ticket.id} by header_message_id={replied_message.message_id}")
        return ticket

    # Method 2: Parse ticket header text (fallback for old tickets or edge cases)
    replied_text = replied_message.text or replied_message.caption or ""
    match = re.search(r'Ticket ID:\s*(\d+)', replied_text)
    if match:
        ticket_id = int(match.group(1))
        result = await session.execute(
            select(Ticket)
            .where(Ticket.id == ticket_id, Ticket.status == "active")
            .options(selectinload(Ticket.user))
        )
        ticket = result.scalar_one_or_none()
        if ticket:
            logger.debug(f"Found ticket #{ticket.id} by parsing header text")
            return ticket

    return None


async def _find_ticket_by_text(session, text: str) -> Ticket | None:
    """Extract ticket ID from message text (e.g., "#123" or "123")."""
    if not text:
        return None

    # Try patterns like "#123", "# 123", or just "123" at the start
    match = re.search(r'#?\s*(\d+)', text)
    if match:
        try:
            ticket_id = int(match.group(1))
            result = await session.execute(
                select(Ticket)
                .where(Ticket.id == ticket_id, Ticket.status == "active")
                .options(selectinload(Ticket.user))
            )
            return result.scalar_one_or_none()
        except ValueError:
            pass

    return None


async def _find_ticket_for_admin_message(
    session, message: Message
) -> Ticket | None:
    """
    Find the ticket associated with an admin message using multiple methods in priority order:
    1. message_thread_id (if in a Forum thread) - highest priority for Forum groups
    2. reply_to_message.message_id (direct match by header_message_id) - most reliable for regular groups
    3. reply_to_message text parsing (fallback for edge cases)
    4. ticket ID in message text (e.g., "#123") - last resort
    
    This ensures tickets are found reliably in both Forum groups and regular groups.
    """
    # Method 1: Check thread_id (highest priority for Forum groups)
    if message.message_thread_id:
        ticket = await _find_ticket_by_thread_id(session, message.message_thread_id)
        if ticket:
            logger.debug(f"Found ticket #{ticket.id} by thread_id={message.message_thread_id}")
            return ticket

    # Method 2: Check reply_to_message (most reliable for regular groups)
    # This works by matching reply_to_message.message_id with ticket.header_message_id
    if message.reply_to_message:
        ticket = await _find_ticket_by_reply(session, message.reply_to_message)
        if ticket:
            logger.debug(
                f"Found ticket #{ticket.id} by reply_to_message_id={message.reply_to_message.message_id}"
            )
            return ticket

    # Method 3: Extract ticket ID from message text (fallback)
    text_content = message.text or message.caption or ""
    if text_content:
        ticket = await _find_ticket_by_text(session, text_content)
        if ticket:
            logger.debug(f"Found ticket #{ticket.id} by parsing message text")
            return ticket

    return None


async def _forward_admin_message_to_user(
    bot: Bot, user_tg_id: int, text_content: str, attachment, ticket_id: int = None,
    channel: str | None = None, address: str | None = None,
) -> None:
    """Forward an operator's message from the admin chat to the customer.

    Sends either text OR the attachment with the text as its caption, never
    both separately.

    The customer is not necessarily on Telegram. `user_tg_id` is the routing
    key, which for anyone who arrived elsewhere is a synthetic negative number
    and not a chat that exists — so the text goes through their channel, and
    attachments are refused where the channel cannot carry them rather than
    failing somewhere that looks like a Telegram outage.

    Args:
        bot: Bot instance, for the Telegram-native attachment path
        user_tg_id: internal routing key, used for logging and as the Telegram
            address when no other is given
        text_content: Text content of the message
        attachment: Attachment data if any
        ticket_id: Optional ticket ID for logging context
        channel: the customer's channel; Telegram when omitted
        address: the customer's address on it; their tg_id when omitted
    """
    from src.channels import caps as channel_caps, get as get_channel

    ticket_info = f" (Ticket #{ticket_id})" if ticket_id else ""
    
    try:
        # Log attempt to send message
        if attachment:
            if not channel_caps(channel).files:
                # Forwarding by file_id is a Telegram trick and nothing else
                # understands it. Say so to the operator instead of throwing
                # an error that reads like the customer's chat is broken.
                logger.warning(
                    f"Attachment not forwarded to user {user_tg_id}{ticket_info}: "
                    f"channel {channel or 'telegram'} does not carry files"
                )
                raise ValueError("channel cannot carry attachments")
            logger.info(
                f"Attempting to send attachment to user {user_tg_id}{ticket_info}. "
                f"Type: {attachment.content_type}, File: {attachment.file_name or 'N/A'}"
            )
            await send_attachment(
                bot,
                user_tg_id,
                attachment,
                caption=text_content if text_content else None,
            )
            logger.info(f"Successfully sent attachment to user {user_tg_id}{ticket_info}")
        elif text_content:
            logger.info(
                f"Attempting to send text message to user {user_tg_id}{ticket_info}. "
                f"Length: {len(text_content)} chars"
            )
            delivery = await get_channel(channel).send_text(
                address or str(user_tg_id), text_content,
            )
            if not delivery.ok:
                raise RuntimeError(delivery.error_key or "send failed")
            logger.info(f"Successfully sent text message to user {user_tg_id}{ticket_info}")
        else:
            logger.warning(f"No content to send to user {user_tg_id}{ticket_info}")
            return
    
    except TelegramForbiddenError as e:
        # User blocked the bot or chat doesn't exist
        error_msg = (
            f"❌ FAILED to send message to user {user_tg_id}{ticket_info}: "
            f"User blocked the bot or chat not found. Error: {e}"
        )
        logger.error(error_msg)
        logger.error(f"Full error details: {traceback.format_exc()}")
        raise
    
    except TelegramNotFound as e:
        # Chat/user not found
        error_msg = (
            f"❌ FAILED to send message to user {user_tg_id}{ticket_info}: "
            f"Chat or user not found. Error: {e}"
        )
        logger.error(error_msg)
        logger.error(f"Full error details: {traceback.format_exc()}")
        raise
    
    except TelegramBadRequest as e:
        # Invalid request (e.g., message too long, invalid format)
        error_msg = (
            f"❌ FAILED to send message to user {user_tg_id}{ticket_info}: "
            f"Bad request - {e}. Message: {text_content[:100] if text_content else 'N/A'}"
        )
        logger.error(error_msg)
        logger.error(f"Full error details: {traceback.format_exc()}")
        raise
    
    except TelegramRetryAfter as e:
        # Rate limit exceeded
        error_msg = (
            f"⚠️ Rate limit exceeded when sending to user {user_tg_id}{ticket_info}: "
            f"Retry after {e.retry_after} seconds"
        )
        logger.warning(error_msg)
        raise
    
    except TelegramUnauthorizedError as e:
        # Bot token invalid
        error_msg = (
            f"❌ CRITICAL: Bot unauthorized when sending to user {user_tg_id}{ticket_info}: {e}"
        )
        logger.critical(error_msg)
        logger.critical(f"Full error details: {traceback.format_exc()}")
        raise
    
    except TelegramAPIError as e:
        # Other Telegram API errors
        error_msg = (
            f"❌ FAILED to send message to user {user_tg_id}{ticket_info}: "
            f"Telegram API error: {type(e).__name__}: {e}"
        )
        logger.error(error_msg)
        logger.error(f"Full error details: {traceback.format_exc()}")
        raise
    
    except Exception as e:
        # Unexpected errors
        error_msg = (
            f"❌ UNEXPECTED ERROR when sending message to user {user_tg_id}{ticket_info}: "
            f"{type(e).__name__}: {e}"
        )
        logger.error(error_msg)
        logger.error(f"Full error details: {traceback.format_exc()}")
        raise


@admin_router.message(F.chat.id == settings.admin_chat_id, Command("close"))
async def handle_close_ticket(message: Message, bot: Bot, command: CommandObject) -> None:
    """Handle /close command to close a ticket."""

    async with db_session() as session:
        # Try to find ticket using multiple methods
        ticket = await _find_ticket_for_admin_message(session, message)

        # If not found and command has args, try by ticket ID
        if not ticket and command.args:
            try:
                ticket_id = int(command.args.strip())
                result = await session.execute(
                    select(Ticket)
                    .where(Ticket.id == ticket_id, Ticket.status == "active")
                    .options(selectinload(Ticket.user))
                )
                ticket = result.scalar_one_or_none()
            except ValueError:
                pass

        if not ticket:
            if message.message_thread_id:
                await message.reply(_op("op_close_not_found"))
            else:
                await message.reply(
                    _op("op_close_usage")
                )
            return

        # Close the ticket
        ticket.status = "closed"
        ticket.closed_at = datetime.utcnow()
        ticket.notification_sent = False  # Reset notification flag for potential new ticket
        user_tg_id = ticket.user.tg_id
        user_lang = ticket.user.language
        ticket_id = ticket.id

        # Save closure marker (stable internal sentinel, not user-facing)
        admin_tg_id = message.from_user.id if message.from_user else 0
        session.add(
            MessageRecord(
                ticket_id=ticket.id,
                sender_type="admin",
                sender_id=admin_tg_id,
                text=CLOSURE_SYSTEM_TEXT,
            )
        )
        await session.commit()

    # Notify admin
    await message.reply(_op("op_closed", id=ticket_id))

    # Notify user (in their language) + ask for a CSAT rating.
    #
    # This used to assemble and send its own copy of the closing message, which
    # meant two places had to agree on what a closed ticket says and on which
    # buttons come with it. They did not stay in agreement.
    from src.services import csat_service
    await csat_service.send_close_notification(
        bot, user_tg_id=user_tg_id, ticket_id=ticket_id, lang=user_lang,
    )


@admin_router.message(F.chat.id == settings.admin_chat_id, Command("t"))
async def handle_template_command(message: Message, bot: Bot) -> None:
    """Open template picker for the current ticket thread.

    Usage: send /t inside a ticket thread (or as reply to the ticket header).
    Equivalent to clicking the [📝 Шаблоны] button under a user message.
    """
    async with db_session() as session:
        ticket = await _find_ticket_for_admin_message(session, message)
        if not ticket:
            await message.reply(
                _op("op_t_usage")
            )
            return
        if ticket.status != "active":
            await message.reply(_op("op_ticket_already_closed", id=ticket.id))
            return

        templates = await template_service.list_for_ticket(session, ticket, limit=8)

    if not templates:
        await message.reply(_op("op_templates_none"))
        return

    rows = [
        [InlineKeyboardButton(
            text=t.title,
            callback_data=build_callback_data(
                "admin", "templates", "apply", {"t": ticket.id, "i": t.id},
            ),
        )]
        for t in templates
    ]
    rows.append([InlineKeyboardButton(
        text=_op("op_close_list_btn"),
        callback_data=build_callback_data(
            "admin", "templates", "dismiss", {"t": ticket.id},
        ),
    )])
    await message.reply(
        _op("op_templates_for", id=ticket.id),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@admin_router.message(F.chat.id == settings.admin_chat_id)
async def handle_admin_reply(message: Message, bot: Bot) -> None:
    """
    Forward admin messages to users.
    Detects ticket by:
    - thread_id
    - reply to ticket header
    - ticket ID in message text (e.g., "#123")
    """

    # Skip commands — they are not ticket replies
    if message.text and message.text.startswith("/"):
        return
    if message.caption and message.caption.startswith("/"):
        return

    text_content = message.text or message.caption or ""
    attachment = extract_attachment(message)

    # Must have either text or attachment
    if not text_content and not attachment:
        return

    async with db_session() as session:
        # Log incoming message details for debugging
        logger.info(
            f"📨 Admin message received: "
            f"chat_id={message.chat.id}, "
            f"message_id={message.message_id}, "
            f"reply_to_message={bool(message.reply_to_message)}, "
            f"reply_msg_id={message.reply_to_message.message_id if message.reply_to_message else None}, "
            f"thread_id={message.message_thread_id}, "
            f"text_length={len(text_content) if text_content else 0}, "
            f"has_attachment={bool(attachment)}"
        )
        
        # Find ticket using multiple methods
        ticket = await _find_ticket_for_admin_message(session, message)

        if not ticket:
            # No ticket found - log as warning for debugging
            reply_info = (
                f"Reply to message ID: {message.reply_to_message.message_id}, "
                f"Reply text: {(message.reply_to_message.text or message.reply_to_message.caption or '')[:100]}"
                if message.reply_to_message
                else "No reply"
            )
            logger.warning(
                f"⚠️ Admin message from {message.from_user.id} (username: {message.from_user.username or 'N/A'}) "
                f"did not match any ticket. "
                f"Thread ID: {message.message_thread_id}, "
                f"{reply_info}, "
                f"Message text: {text_content[:100] if text_content else 'None'}, "
                f"Has attachment: {bool(attachment)}"
            )
            # Don't send error to admin for this - might be a regular chat message
            return
        
        logger.info(
            f"✅ Ticket #{ticket.id} found for admin message. "
            f"User: {ticket.user.tg_id}, "
            f"Header message ID: {ticket.header_message_id}, "
            f"Thread ID: {ticket.thread_id}"
        )

        user_tg_id = ticket.user.tg_id
        # Where they actually are. For a Telegram customer these are the id
        # again and "telegram"; for anyone else the id is a routing key that
        # no chat answers to.
        from src.channels.identity import address_of, channel_of
        user_channel, user_address = channel_of(ticket.user), address_of(ticket.user)

        # Clean text content (remove ticket ID prefix if present)
        cleaned_text = text_content
        if text_content:
            # Remove patterns like "#123 " or "123 " from the start
            cleaned_text = re.sub(r'^#?\s*\d+\s+', '', text_content).strip()

        # Save admin message to DB
        admin_tg_id = message.from_user.id if message.from_user else 0
        message_text = cleaned_text if cleaned_text else (_op("msg_file") if attachment else "")
        session.add(
            MessageRecord(
                ticket_id=ticket.id,
                sender_type="admin",
                sender_id=admin_tg_id,
                text=message_text,
                file_id=attachment.file_id if attachment else None,
                file_type=attachment.content_type if attachment else None,
                file_name=attachment.file_name if attachment else None,
                file_size=attachment.file_size if attachment else None,
                mime_type=attachment.mime_type if attachment else None,
            )
        )
        await session.commit()

        # First responder claims the ticket (no-op if already assigned or the
        # admin's tg_id isn't linked to an operator account).
        try:
            from src.services import assignment_service
            await assignment_service.auto_assign_by_tg(ticket.id, admin_tg_id)
        except Exception:
            pass

        # Forward to user (only once - either text OR attachment with caption)
        try:
            await _forward_admin_message_to_user(
                bot, user_tg_id, cleaned_text, attachment, ticket_id=ticket.id,
                channel=user_channel, address=user_address,
            )
            logger.info(
                f"✅ Successfully forwarded admin message to user {user_tg_id} for ticket #{ticket.id}"
            )
        except TelegramForbiddenError as e:
            # User blocked bot
            error_msg = _op(
                "op_deliver_failed", tg=user_tg_id, id=ticket.id,
                reason="blocked the bot",
            )
            logger.error(error_msg)
            await message.reply(error_msg)
        except TelegramNotFound as e:
            # User/chat not found
            error_msg = _op(
                "op_deliver_failed", tg=user_tg_id, id=ticket.id,
                reason="chat not found",
            )
            logger.error(error_msg)
            await message.reply(error_msg)
        except TelegramBadRequest as e:
            # Invalid request
            error_msg = _op(
                "op_deliver_failed", tg=user_tg_id, id=ticket.id,
                reason=f"bad request: {e}",
            )
            logger.error(error_msg)
            await message.reply(error_msg)
        except Exception as e:
            # Other errors
            error_msg = _op(
                "op_deliver_failed", tg=user_tg_id, id=ticket.id,
                reason=f"{type(e).__name__}: {e}",
            )
            logger.error(error_msg)
            logger.error(f"Full traceback: {traceback.format_exc()}")
            await message.reply(
                _op("op_deliver_error", what=f"{type(e).__name__}: {e}")
            )
        
        # Ensure admin's message in admin chat is sent as reply to header (if not already)
        # This ensures proper threading even if admin didn't explicitly reply
        if ticket.header_message_id and not message.reply_to_message:
            # Admin sent a message without replying - we can't modify it, but log it
            logger.debug(
                f"Admin message in ticket {ticket.id} was not sent as reply to header. "
                f"Header message ID: {ticket.header_message_id}"
            )
