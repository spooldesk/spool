"""Handlers for processing user messages and ticket creation."""

import asyncio
import logging
import traceback
from typing import Dict

from aiogram import Bot, Router, F
from aiogram.enums import ChatType
from aiogram.filters import Command
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    ReplyKeyboardMarkup,
    KeyboardButton,
)
from sqlalchemy import select
from datetime import datetime

from src.config import load_settings
from src.database.db import acquire_write_lock, db_session
from src.database.models import Message as MessageRecord, Ticket, User, Team
from src.utils.file_processor import extract_attachment
from src.utils.i18n import get_text, get_language_keyboard, SUPPORTED_LANGUAGES

logger = logging.getLogger(__name__)


def _op(key: str, **kwargs) -> str:
    """Text posted into the ADMIN chat.

    Follows the instance UI language, not the customer's — these lines are
    read by the support team. Customer-facing text uses get_text().
    """
    from src.dashboard.i18n import t as dash_t
    return dash_t(key, **kwargs)

user_router = Router()
user_router.message.filter(F.chat.type == ChatType.PRIVATE)


@user_router.edited_message(F.chat.type == ChatType.PRIVATE)
async def customer_edited_message(message: Message):
    if not message.from_user or message.from_user.is_bot:
        return
    from src.services.channel_events import update_customer_message
    async with db_session(write=True) as session:
        await update_customer_message(session, channel="telegram", address=str(message.chat.id),
            ref=str(message.message_id), text=message.text or message.caption or '')


async def _reply_parent(session, ticket_id: int, message: Message) -> int | None:
    parent = message.reply_to_message
    if parent is None:
        return None
    return (await session.execute(select(MessageRecord.id).where(
        MessageRecord.ticket_id == ticket_id, MessageRecord.channel_ref == str(parent.message_id),
        MessageRecord.is_internal == False, MessageRecord.deleted_at.is_(None),
    ).order_by(MessageRecord.id).limit(1))).scalar_one_or_none()

from src.config import runtime_settings as settings

# Temporary storage for pending category selections (user_id -> category_name)
pending_categories: Dict[int, str] = {}

# Temporary storage for pending language selections (user_id -> language_code)
pending_languages: Dict[int, str] = {}

# Temporary storage for users who entered password but haven't selected language yet
# (user_id -> username)
pending_password_verified: Dict[int, str] = {}

def _main_keyboard(language: str | None = None) -> ReplyKeyboardMarkup:
    """Create main keyboard with 'new request' button."""
    text = get_text("new_request", language)
    keyboard = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=text)]],
        resize_keyboard=True,
        one_time_keyboard=False,
    )
    return keyboard


async def _category_keyboard(session) -> InlineKeyboardMarkup:
    """Inline keyboard with category buttons, built from the editable DB catalog.

    Two buttons per row. Callback is cat:<id> (dynamic, no static codes).
    """
    from src.services import catalog_service
    cats = await catalog_service.list_active(session)
    rows, row = [], []
    for c in cats:
        label = f"{c.emoji + ' ' if c.emoji else ''}{c.name}"
        row.append(InlineKeyboardButton(text=label, callback_data=f"cat:{c.id}"))
        if len(row) == 2:
            rows.append(row); row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _language_keyboard() -> InlineKeyboardMarkup:
    """Inline keyboard with language buttons, limited to the enabled languages.

    The set of offered languages is configurable from the dashboard, so a
    single-language deployment shows just one button (and white-label installs
    can hide languages they don't support).
    """
    from src.services import settings_service
    lang_labels = get_language_keyboard()
    enabled = await settings_service.get_enabled_languages()
    rows, row = [], []
    for code in enabled:
        row.append(InlineKeyboardButton(text=lang_labels.get(code, code), callback_data=f"lang:{code}"))
        if len(row) == 2:
            rows.append(row); row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def resolve_pending_account_links(session, user: User) -> int:
    """Legacy hook kept inert: account links now require dashboard confirmation."""
    return 0


async def _notify_admins_new_user(bot: Bot, user_tg_id: int, username: str | None, session) -> None:
    """Notify admins about new user and ask to assign team."""
    try:
        from src.utils.callback_data import build_callback_data
        
        # Get all teams for validation
        result = await session.execute(select(Team))
        teams = result.scalars().all()
        team_names = [team.name for team in teams]
        
        # Get user from DB to get user.id
        result = await session.execute(select(User).where(User.tg_id == user_tg_id))
        user = result.scalar_one_or_none()
        if not user:
            logger.error(f"User {user_tg_id} not found after creation")
            return
        
        username_display = f"@{username}" if username else f"id {user_tg_id}"
        notification_text = _op(
            "op_new_user",
            tg=user_tg_id, username=username_display, uid=user.id,
        )
        
        if not settings.admin_chat_id:
            return
        await bot.send_message(
            chat_id=settings.admin_chat_id,
            text=notification_text,
        )
        logger.info(f"Sent new user notification to admins for user {user_tg_id} (user_id={user.id})")
    except Exception as e:
        logger.error(f"Failed to notify admins about new user {user_tg_id}: {e}")
        logger.error(f"Full error: {traceback.format_exc()}")


@user_router.message(Command("start"))
async def handle_start(message: Message, bot: Bot) -> None:
    """Handle /start command - ask for password first, then language, then create user."""

    user_tg_id = message.from_user.id
    username = message.from_user.username

    async with db_session() as session:
        result = await session.execute(
            select(User).where(User.tg_id == user_tg_id)
        )
        user = result.scalar_one_or_none()

        if not user:
            # New user - ask for password first (in Russian)
            await message.reply(get_text("enter_password", "ru"))
            return

        # Keep username fresh (people rename) and auto-resolve any personal
        # accounts that were waiting to be linked to this (work) account.
        if username and user.username != username:
            user.username = username
        if await resolve_pending_account_links(session, user):
            await session.commit()


        # Check if user is blocked
        if not user.is_active:
            await message.reply(get_text("account_blocked", user.language))
            return

        # Check if user needs password verification (for existing users who haven't verified yet)
        if not user.is_verified:
            await message.reply(get_text("enter_password", "ru"))  # Use Russian for password request
            return

        # Check if user needs language selection
        if user.language is None:
            await message.reply(
                get_text("select_language", "ru"),  # Use Russian for language selection
                reply_markup=await _language_keyboard(),
            )
            return

        # Check if user has team assigned
        if user.team_id is None:
            await message.reply(
                get_text("account_not_linked", user.language)
            )
            return

        # User is verified, has language and team - prompt to create ticket with button
        await message.reply(
            get_text("authorized", user.language),
            reply_markup=_main_keyboard(user.language),
        )


async def _handle_new_ticket_request(message: Message, bot: Bot) -> None:
    """Handle new ticket request (from button or command) - create a new ticket after category selection."""

    user_tg_id = message.from_user.id
    username = message.from_user.username

    async with db_session() as session:
        result = await session.execute(
            select(User).where(User.tg_id == user_tg_id)
        )
        user = result.scalar_one_or_none()

        if not user:
            await message.reply(get_text("enter_password", None))
            return


        # Check if user is blocked
        if not user.is_active:
            await message.reply(get_text("account_blocked", user.language))
            return

        if not user.is_verified:
            await message.reply(get_text("enter_password", user.language))
            return

        # Check if user needs language selection (for existing users without language)
        if user.language is None:
            # Ask for language selection
            await message.reply(
                get_text("select_language", None),
                reply_markup=await _language_keyboard(),
            )
            return

        # Check if user already has an active ticket
        result = await session.execute(
            select(Ticket)
            .where(Ticket.user_id == user.id, Ticket.status == "active")
            .order_by(Ticket.id.desc())
            .limit(1)
        )
        active_ticket = result.scalar_one_or_none()

        if active_ticket:
            await message.reply(
                get_text("active_ticket_exists", user.language, ticket_id=active_ticket.id)
            )
            return

        # Category step is optional — controlled from the dashboard settings.
        # When off, the user describes the problem directly and an admin
        # assigns the category later from the ticket card.
        from src.services import catalog_service, settings_service
        ask_category = await settings_service.get_ask_category()
        active_cats = await catalog_service.list_active(session) if ask_category else []

        if ask_category and active_cats:
            await message.reply(
                get_text("select_category", user.language),
                reply_markup=await _category_keyboard(session),
            )
        else:
            # No category asked (toggle off, or no categories configured):
            # mark this user's next message as a ticket with empty category.
            pending_categories[user_tg_id] = ""
            await message.reply(get_text("describe_problem", user.language))


@user_router.message(Command("newticket"))
async def handle_newticket(message: Message, bot: Bot) -> None:
    """Handle /newticket command - kept for backward compatibility."""
    await _handle_new_ticket_request(message, bot)


@user_router.callback_query(F.data.startswith("lang:"))
async def handle_language_selection(callback: CallbackQuery, bot: Bot) -> None:
    """Handle language selection."""
    
    language_code = callback.data.split(":", 1)[1]
    if language_code not in SUPPORTED_LANGUAGES:
        await callback.answer(get_text("invalid_language"), show_alert=True)
        return
    
    user_tg_id = callback.from_user.id
    username = callback.from_user.username
    
    async with db_session() as session:
        result = await session.execute(
            select(User).where(User.tg_id == user_tg_id)
        )
        user = result.scalar_one_or_none()
        
        # If user doesn't exist, create user after password verification
        if not user:
            # Check if user entered password
            if user_tg_id not in pending_password_verified:
                await callback.answer(get_text("enter_password_first", None), show_alert=True)
                return
            
            # Create user with selected language
            user = User(
                tg_id=user_tg_id,
                username=username,
                is_verified=True,  # Password already verified
                is_active=True,
                team_id=None,
                local_user_id=None,
                language=language_code
            )
            session.add(user)
            await session.flush()  # assign user.id before resolving links

            # If a personal account was waiting for this work account, link now.
            await resolve_pending_account_links(session, user)
            await session.commit()

            # Remove from pending
            del pending_password_verified[user_tg_id]
            
            # Notify admins about new user with team selection buttons
            await _notify_admins_new_user(bot, user_tg_id, username, session)
            
            logger.info(f"New user {user_tg_id} created with language: {language_code}")
            
            await callback.answer(get_text("language_set", language_code))
            await callback.message.edit_text(
                get_text("language_set", language_code)
            )
            
            # Tell user to wait for admin
            await bot.send_message(
                chat_id=user_tg_id,
                text=get_text("account_created", language_code)
            )
            return
        
        # Existing user - just save language
        user.language = language_code
        await session.commit()
        
        logger.info(f"User {user_tg_id} selected language: {language_code}")
        
        await callback.answer(get_text("language_set", language_code))
        await callback.message.edit_text(
            get_text("language_set", language_code)
        )
        
        # If user is verified and has team, show main menu
        if user.is_verified and user.team_id:
            await bot.send_message(
                chat_id=user_tg_id,
                text=get_text("authorized", language_code),
                reply_markup=_main_keyboard(language_code),
            )


@user_router.callback_query(F.data.startswith("cat:"))
async def handle_category_selection(callback: CallbackQuery, bot: Bot) -> None:
    """Handle category selection - ask for ticket description."""
    from src.services import catalog_service

    raw = callback.data.split(":", 1)[1]
    user_tg_id = callback.from_user.id

    async with db_session() as session:
        result = await session.execute(
            select(User).where(User.tg_id == user_tg_id)
        )
        user = result.scalar_one_or_none()
        language = user.language if user else None

        # Resolve category by DB id (catalog is fully editable from the dashboard)
        try:
            cat = await catalog_service.get(session, int(raw))
        except (TypeError, ValueError):
            cat = None
        if cat is None:
            await callback.answer(get_text("category_unavailable", language), show_alert=True)
            return
        category_name = cat.name

    # Store category for this user
    pending_categories[user_tg_id] = category_name

    await callback.answer()
    await callback.message.edit_text(
        get_text("category_selected", language, category=category_name),
        parse_mode="Markdown",
    )


# Note: the team assignment callback is handled in ticket_actions.py


@user_router.callback_query(F.data.startswith("csat:"))
async def handle_csat_rating(callback: CallbackQuery, bot: Bot) -> None:
    """User tapped a 1–5 star button after their ticket was closed.

    callback_data: "csat:{ticket_id}:{rating}". The rating is written once;
    later taps get a polite "already rated". The message is edited in place so
    the keyboard disappears and the chosen score stays visible.
    """
    from src.services import csat_service

    user_tg_id = callback.from_user.id
    try:
        _, raw_ticket_id, raw_rating = (callback.data or "").split(":", 2)
        ticket_id, rating = int(raw_ticket_id), int(raw_rating)
    except (ValueError, AttributeError):
        await callback.answer()
        return

    # Resolve the user's language for localized responses
    user_lang = None
    try:
        async with db_session() as session:
            u = (await session.execute(
                select(User).where(User.tg_id == user_tg_id)
            )).scalar_one_or_none()
            user_lang = u.language if u else None
    except Exception:
        pass

    result = await csat_service.save_rating(
        ticket_id=ticket_id, user_tg_id=user_tg_id, rating=rating,
    )

    if result == "ok":
        await callback.answer(get_text("csat_thanks", user_lang))
        try:
            await callback.message.edit_text(
                get_text("ticket_closed_user", user_lang, id=ticket_id)
                + "\n\n" + get_text("csat_thanks", user_lang)
                + " " + "⭐" * rating,
            )
        except Exception:
            pass
    elif result == "already":
        await callback.answer(get_text("csat_already", user_lang), show_alert=False)
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
    else:
        await callback.answer()


@user_router.callback_query(F.data.startswith("defl:"))
async def handle_deflect_verdict(callback: CallbackQuery, bot: Bot) -> None:
    """User's verdict on the AI self-service answer.

    callback_data: "defl:ok:{ticket_id}:{ai_call_id}" — solved, close the ticket
                   "defl:no:{ticket_id}:{ai_call_id}" — wait for an operator
    """
    from src.ai import deflect as deflect_mod

    user_tg_id = callback.from_user.id
    try:
        _, verdict, raw_tid, raw_call = (callback.data or "").split(":", 3)
        ticket_id, ai_call_id = int(raw_tid), int(raw_call)
    except (ValueError, AttributeError):
        await callback.answer()
        return

    # Resolve language for localized responses
    user_lang = None
    try:
        async with db_session() as session:
            u = (await session.execute(
                select(User).where(User.tg_id == user_tg_id)
            )).scalar_one_or_none()
            user_lang = u.language if u else None
    except Exception:
        pass

    if verdict not in {"ok", "no"}:
        await callback.answer()
        return
    from src.services import choice_service
    outcome = await choice_service.apply_deflection_verdict(
        ticket_id=ticket_id, ai_call_id=ai_call_id,
        user_tg_id=user_tg_id, accepted=verdict == "ok",
    )
    await callback.answer()
    if not outcome.ok:
        return
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    if outcome.closed:
        from src.services import csat_service
        await csat_service.send_close_notification(
            bot, user_tg_id=user_tg_id, ticket_id=ticket_id, lang=user_lang,
        )
        asyncio.create_task(deflect_mod.notify_admin_thread_ai_closed(bot, ticket_id))
    elif outcome.reply_key:
        try:
            await bot.send_message(chat_id=user_tg_id,
                                   text=get_text(outcome.reply_key, user_lang))
        except Exception:
            logger.exception("Could not acknowledge AI verdict on ticket %s", ticket_id)


@user_router.message()
async def handle_user_message(message: Message, bot: Bot) -> None:
    """
    Handle all user messages:
    - Password verification
    - Ticket creation (after /newticket and category selection)
    - Continuing existing active ticket conversation
    """

    user_tg_id = message.from_user.id
    username = message.from_user.username
    text_content = message.text or message.caption or ""
    attachment = extract_attachment(message)

    async with db_session() as session:
        # Get or create user
        result = await session.execute(
            select(User).where(User.tg_id == user_tg_id)
        )
        user = result.scalar_one_or_none()

        if not user:
            # New user - check if password was entered
            if text_content == settings.access_password:
                # Password correct - ask for language selection (in Russian)
                pending_password_verified[user_tg_id] = username
                await message.reply(
                    get_text("password_accepted", "ru"),
                    reply_markup=await _language_keyboard(),
                )
                return
            else:
                # Ask for password (in Russian)
                await message.reply(get_text("enter_password", "ru"))
                return


        # Check if user is blocked
        if not user.is_active:
            await message.reply(get_text("account_blocked", user.language))
            return

        # Check if user needs password verification (for existing users who haven't verified yet)
        if not user.is_verified:
            if text_content == settings.access_password:
                user.is_verified = True
                await session.commit()
                # After password verification, check if language is needed
                if user.language is None:
                    await message.reply(
                        get_text("password_accepted", "ru"),
                        reply_markup=await _language_keyboard(),
                    )
                else:
                    await message.reply(
                        get_text("verification_success", user.language),
                        reply_markup=_main_keyboard(user.language),
                    )
                return
            else:
                await message.reply(get_text("wrong_password", "ru"))  # Use Russian for password errors
                return

        # Check if user needs language selection (for existing users without language)
        if user.language is None:
            # Ask for language selection (in Russian)
            await message.reply(
                get_text("select_language", "ru"),
                reply_markup=await _language_keyboard(),
            )
            return

        # Check if user has team assigned
        if user.team_id is None:
            await message.reply(
                get_text("account_not_linked", user.language)
            )
            return

        # Serialize deciding whether this message extends or opens a ticket.
        # Concurrent updates from one Telegram user must share the active row.
        await acquire_write_lock(session)
        # Check if user has an active ticket
        result = await session.execute(
            select(Ticket)
            .where(Ticket.user_id == user.id, Ticket.status == "active")
            .order_by(Ticket.id.desc())
            .limit(1)
        )
        active_ticket = result.scalar_one_or_none()

        if active_ticket:
            record = MessageRecord(
                ticket_id=active_ticket.id, sender_type="user", sender_id=user_tg_id,
                channel_ref=str(message.message_id),
                reply_to_id=await _reply_parent(session, active_ticket.id, message),
                text=text_content or (get_text("attachment_only", user.language) if attachment else ""),
                file_id=attachment.file_id if attachment else None,
                file_type=attachment.content_type if attachment else None,
                file_name=attachment.file_name if attachment else None,
                file_size=attachment.file_size if attachment else None,
                mime_type=attachment.mime_type if attachment else None,
            )
            if not record.text and attachment is None:
                return
            session.add(record)
            if active_ticket.stage == "waiting":
                active_ticket.stage = "in_progress"
            await session.flush()
            ticket_id, message_id = active_ticket.id, record.id
            # Intake survives a Telegram administrator-chat outage.
            await session.commit()
            from src.services import announce_service
            try:
                await announce_service.announce_message(bot, ticket_id, message_id)
            except Exception:
                logger.exception("Could not mirror message %s", message_id)
            return

        # No active ticket - check if user has pending category selection (from /newticket).
        # NOTE: "" is a valid value here (ask-category toggle off → category unset,
        # admin assigns it later), so distinguish it from None (no pending intent).
        category = pending_categories.pop(user_tg_id, None)

        if category is None:
            # Check if user clicked the "new request" button (check in all languages)
            new_request_texts = [
                get_text("new_request", "uk").lower(),
                get_text("new_request", "ru").lower(),
                get_text("new_request", "en").lower(),
            ]
            if text_content.strip().lower() in new_request_texts:
                # User clicked the button - handle new ticket request
                await session.commit()
                await _handle_new_ticket_request(message, bot)
                return
            
            # No category selected and not button click - prompt to use button
            await session.commit()
            await message.reply(
                get_text("create_new_request", user.language),
                reply_markup=_main_keyboard(user.language),
            )
            return

        # Validate that user sent something
        if not text_content and not attachment:
            await session.commit()
            await message.reply(get_text("send_text_or_file", user.language))
            # Restore category
            pending_categories[user_tg_id] = category
            return

        # Create new ticket
        ticket = Ticket(
            user_id=user.id,
            team_id=user.team_id,  # Store team_id for quick access
            category=category,
            status="active",
            thread_id=None,
            header_message_id=None,
            notification_sent=False,
            ticket_code="",
            telegram_id=user.tg_id,
        )
        session.add(ticket)
        await session.flush()
        # Set ticket_code after we have the ID
        ticket.ticket_code = f"#{ticket.id}"

        record = MessageRecord(
            ticket_id=ticket.id, sender_type="user", sender_id=user_tg_id,
            channel_ref=str(message.message_id),
            text=text_content or (get_text("attachment_only", user.language) if attachment else ""),
            file_id=attachment.file_id if attachment else None,
            file_type=attachment.content_type if attachment else None,
            file_name=attachment.file_name if attachment else None,
            file_size=attachment.file_size if attachment else None,
            mime_type=attachment.mime_type if attachment else None,
        )
        session.add(record)
        await session.flush()
        ticket_id, message_id, language = ticket.id, record.id, user.language
        await session.commit()

    from src.services import announce_service, settings_service, work_hours_service
    try:
        await announce_service.announce_message(bot, ticket_id, message_id)
    except Exception:
        logger.exception("Ticket %s saved but not announced", ticket_id)
    try:
        await message.reply(get_text("message_sent", language,
                                    brand=await settings_service.get_brand_name()))
        note = await work_hours_service.off_hours_note()
        if note:
            await message.reply(note)
    except Exception:
        logger.exception("Could not acknowledge ticket %s", ticket_id)
    from src.ai.deflect import deflect_and_offer
    asyncio.create_task(deflect_and_offer(bot, ticket_id, text_content,
                                         user_tg_id, language))
