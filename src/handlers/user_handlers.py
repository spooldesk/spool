"""Handlers for processing user messages and ticket creation."""

import asyncio
import logging
import traceback
from typing import Dict

from aiogram import Bot, Router, F
from aiogram.enums import ChatType
from aiogram.filters import Command
from aiogram.exceptions import (
    TelegramMigrateToChat,
    TelegramBadRequest,
    TelegramAPIError,
    TelegramForbiddenError,
    TelegramNotFound,
    TelegramRetryAfter,
    TelegramNetworkError,
)
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
from src.database.db import db_session
from src.database.models import Message as MessageRecord, Ticket, User, Team
from src.utils.file_processor import AttachmentData, extract_attachment, send_attachment
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

settings = load_settings()

# Category keys (localized names are in i18n.py)
CATEGORY_KEYS = {
    "vpn": "vpn",
    "server": "server",
    "password": "password",
    "other": "other",
}

# Temporary storage for pending category selections (user_id -> category_name)
pending_categories: Dict[int, str] = {}

# Temporary storage for pending language selections (user_id -> language_code)
pending_languages: Dict[int, str] = {}

# Temporary storage for users who entered password but haven't selected language yet
# (user_id -> username)
pending_password_verified: Dict[int, str] = {}

# Timeout for automatic follow-up question (15 minutes in seconds)
# FOLLOW_UP_TIMEOUT = 15 * 60  # 15 minutes  # DISABLED


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
    """When `user` (a work account) appears, link any personal accounts that were
    waiting for them via pending_work_username.

    Match is by @username (case-insensitive) OR by tg_id (as string). Returns the
    number of personal accounts linked. Safe to call on every identification —
    it's a no-op when nothing is pending.
    """
    candidates = []
    if user.username:
        candidates.append(user.username.strip().lower())
    candidates.append(str(user.tg_id))
    if not candidates:
        return 0

    # Find personals whose pending target matches this user
    result = await session.execute(
        select(User).where(User.pending_work_username.isnot(None))
    )
    pendings = result.scalars().all()
    linked = 0
    for personal in pendings:
        target = (personal.pending_work_username or "").strip().lower()
        if target and target in candidates and personal.id != user.id:
            personal.linked_to_user_id = user.id
            personal.pending_work_username = None
            linked += 1
            logger.info(
                f"Auto-linked personal account #{personal.id} → work account "
                f"#{user.id} (@{user.username or user.tg_id}) via pending match"
            )
    return linked


async def _language_of(tg_id: int) -> str | None:
    """The language this person picked, for messages sent outside a handler that
    already loaded them. None falls back to the instance default in get_text()."""
    try:
        async with db_session() as session:
            return (await session.execute(
                select(User.language).where(User.tg_id == tg_id)
            )).scalar_one_or_none()
    except Exception:
        return None


async def _build_link_line(session, user: User) -> str:
    """If this user is a personal account linked to a work one, return a header
    line revealing the work identity. Otherwise empty string.

    Lets admins instantly see "this is Vasya writing from his personal TG" when
    someone messages because their work account is down.
    """
    if not user.linked_to_user_id:
        return ""
    work = (await session.execute(
        select(User).where(User.id == user.linked_to_user_id)
    )).scalar_one_or_none()
    if work is None:
        return ""
    wlabel = f"@{work.username}" if work.username else f"id {work.tg_id}"
    work_extra = f" / {work.work_username}" if work.work_username else ""
    return f'{_op("op_writes_from_personal")}{wlabel}{work_extra}'


def _ticket_action_keyboard(ticket_id: int) -> InlineKeyboardMarkup:
    """Inline keyboard attached to each forwarded user message in the admin chat.

    Lets admins pick a reply template, ask AI for help, or close the ticket —
    without leaving the thread. All buttons carry ticket_id so handlers don't
    need to re-derive it from reply chains.
    """
    from src.utils.callback_data import build_callback_data
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text=_op("op_btn_templates"),
                callback_data=build_callback_data("admin", "templates", "pick", {"t": ticket_id}),
            ),
            InlineKeyboardButton(
                text=_op("op_btn_ai"),
                callback_data=build_callback_data("admin", "ai", "ask", {"t": ticket_id}),
            ),
        ],
        [
            InlineKeyboardButton(
                text=_op("op_btn_close"),
                callback_data=build_callback_data("admin", "ticket", "close", {"i": ticket_id}),
            ),
        ],
    ])


def _team_selection_keyboard(user_id: int) -> InlineKeyboardMarkup:
    """Create inline keyboard with team selection buttons for new user."""
    from src.utils.callback_data import build_callback_data
    
    buttons = []
    # Get all teams dynamically
    async def _get_teams():
        from src.database.db import db_session
        from src.database.models import Team
        async with db_session() as session:
            result = await session.execute(select(Team))
            teams = result.scalars().all()
            return teams
    
    # We'll build buttons in the notification function where we have access to teams
    # For now, return empty - will be populated in _notify_admins_new_user
    return InlineKeyboardMarkup(inline_keyboard=[])


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
        
        # Build keyboard with teams using new callback_data format
        buttons = []
        for team in teams:
            callback_data = build_callback_data("admin", "team", "assign", {"u": user.id, "n": team.name})
            buttons.append([InlineKeyboardButton(text=team.name, callback_data=callback_data)])
        
        keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
        
        await bot.send_message(
            chat_id=settings.admin_chat_id,
            text=notification_text,
            reply_markup=keyboard,
        )
        logger.info(f"Sent new user notification to admins for user {user_tg_id} (user_id={user.id})")
    except Exception as e:
        logger.error(f"Failed to notify admins about new user {user_tg_id}: {e}")
        logger.error(f"Full error: {traceback.format_exc()}")


async def _maybe_redirect_team_user(message: Message, bot: Bot, user: User, session) -> bool:
    """
    Optionally redirect users of a configured team to another destination.

    Fully configurable from the dashboard (Настройки → Перенаправление):
    enabled flag, team-name substring to match, and the message/link to show.
    Off by default, so a fresh white-label install does nothing here.
    Returns True when the redirect flow handled the message (caller should stop).
    """
    if not user.team_id:
        return False

    from src.services import settings_service
    enabled, team_match, target = await settings_service.get_redirect_config()
    if not enabled or not team_match or not target:
        return False

    result = await session.execute(select(Team).where(Team.id == user.team_id))
    team = result.scalar_one_or_none()
    if not team:
        return False

    team_name = (team.name or "").strip().lower()
    if team_match.lower() not in team_name:
        return False

    redirect_text = target
    username_display = f"@{user.username}" if user.username else f"id {user.tg_id}"
    user_message_preview = (message.text or message.caption or "").strip()
    if not user_message_preview:
        user_message_preview = _op("op_attachment_no_text")
    if len(user_message_preview) > 300:
        user_message_preview = f"{user_message_preview[:297]}..."

    redirect_ok = False
    redirect_error = ""
    try:
        await message.reply(redirect_text)
        redirect_ok = True
    except Exception as e:
        redirect_error = str(e)
        logger.error(f"Failed to send team redirect to user {user.tg_id}: {e}")

    status_icon = "✅" if redirect_ok else "❌"
    admin_text = (
        f"{status_icon} Redirect\n"
        f"user_id={user.id}\n"
        f"tg_id={user.tg_id}\n"
        f"username={username_display}\n"
        f"team={team.name}\n"
        f"user_message={user_message_preview}\n"
        f"status={'success' if redirect_ok else 'failed'}"
    )
    if redirect_error:
        admin_text += f"\nerror={redirect_error}"

    try:
        await bot.send_message(chat_id=settings.admin_chat_id, text=admin_text)
    except Exception as e:
        logger.error(f"Failed to notify admin chat about team redirect: {e}")

    return True


async def _forward_user_message_to_admin(
    bot: Bot,
    admin_chat_id: int,
    thread_id: int | None,
    header_message_id: int | None,
    text_content: str,
    attachment: AttachmentData | None,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> tuple[bool, bool]:
    """
    Forward a user message to admin chat (inside thread if available, or as reply to header).
    Returns (success, should_clear_thread) tuple.
    - success: True if message was sent successfully
    - should_clear_thread: True if thread_id should be cleared (e.g., thread not found)
    Handles thread errors and retries without thread_id if needed.
    """
    try:
        # Prepare kwargs for sending
        kwargs = {"chat_id": admin_chat_id}
        if reply_markup is not None:
            kwargs["reply_markup"] = reply_markup

        # CRITICAL: In Forum groups, we need BOTH message_thread_id AND reply_to_message_id
        # to ensure messages appear in the thread AND show as replies to the header
        # This allows admins to see the full conversation when clicking "View all replies"
        if thread_id is not None:
            kwargs["message_thread_id"] = thread_id
            # Also add reply_to_message_id if we have header_message_id
            # This ensures the message appears as a reply in the thread
            if header_message_id is not None:
                kwargs["reply_to_message_id"] = header_message_id
                logger.info(
                    f"Using BOTH thread_id={thread_id} AND reply_to_message_id={header_message_id} "
                    f"for sending message to admin (Forum group)"
                )
            else:
                logger.info(f"Using thread_id {thread_id} for sending message to admin")
        # If no thread_id, use reply_to_message_id (for regular groups or when thread_id is cleared)
        elif header_message_id is not None:
            kwargs["reply_to_message_id"] = header_message_id
            logger.info(f"Using reply_to_message_id {header_message_id} (no thread_id) for sending message to admin")
        else:
            logger.warning("No thread_id or header_message_id available - sending without thread/reply")
        
        # If we have an attachment, send it with optional caption
        if attachment:
            logger.info(
                f"Sending attachment to admin. "
                f"chat_id={admin_chat_id}, "
                f"message_thread_id={thread_id}, "
                f"reply_to_message_id={header_message_id if thread_id is None else None}, "
                f"attachment_type={attachment.content_type}"
            )
            sent_msg = await send_attachment(
                bot,
                admin_chat_id,
                attachment,
                caption=text_content if text_content else None,
                message_thread_id=thread_id,
                reply_to_message_id=header_message_id,  # Always use reply_to_message_id if available
                reply_markup=reply_markup,
            )
            logger.info(
                f"✅ Successfully sent attachment to admin. "
                f"Sent message ID: {sent_msg.message_id}, "
                f"Sent message_thread_id: {sent_msg.message_thread_id}"
            )
            # Verify that attachment was sent to the correct thread
            if thread_id:
                if sent_msg.message_thread_id is None:
                    # Attachment was sent with thread_id but didn't end up in a thread
                    logger.error(
                        f"❌ CRITICAL: Attachment was sent with message_thread_id={thread_id}, "
                        f"but actual message_thread_id is None! "
                        f"This means the group is NOT a Forum group or topics are disabled. "
                        f"Threading is not working. Will clear thread_id and use reply_to_message_id instead."
                    )
                    # Clear thread_id since it's not working - future messages will use reply_to_message_id
                    return (True, True)  # Success, but clear thread_id
                elif sent_msg.message_thread_id != thread_id:
                    logger.warning(
                        f"⚠️ WARNING: Attachment was sent with thread_id={thread_id}, "
                        f"but actual message_thread_id={sent_msg.message_thread_id}. "
                        f"They don't match! Will update thread_id."
                    )
                    # Update thread_id to the actual one
                    return (True, True)  # Success, but update thread_id
                else:
                    logger.info(
                        f"✅ Attachment successfully sent to thread. "
                        f"thread_id={thread_id}, actual message_thread_id={sent_msg.message_thread_id}"
                    )
            return (True, False)  # Success, no need to clear thread
        
        # If we only have text, send as text message
        if text_content:
            logger.info(
                f"Sending text message to admin. "
                f"chat_id={admin_chat_id}, "
                f"message_thread_id={kwargs.get('message_thread_id', 'None')}, "
                f"reply_to_message_id={kwargs.get('reply_to_message_id', 'None')}, "
                f"text_length={len(text_content)}"
            )
            sent_msg = await bot.send_message(text=text_content, **kwargs)
            logger.info(
                f"✅ Successfully sent text message to admin. "
                f"Sent message ID: {sent_msg.message_id}, "
                f"Sent message_thread_id: {sent_msg.message_thread_id}"
            )
            # Verify that message was sent to the correct thread
            if thread_id:
                if sent_msg.message_thread_id is None:
                    # Message was sent with thread_id but didn't end up in a thread
                    # This means the group is not a Forum group or topics are disabled
                    logger.error(
                        f"❌ CRITICAL: Message was sent with message_thread_id={thread_id}, "
                        f"but actual message_thread_id is None! "
                        f"This means the group is NOT a Forum group or topics are disabled. "
                        f"Threading is not working. Will clear thread_id and use reply_to_message_id instead."
                    )
                    # Clear thread_id since it's not working - future messages will use reply_to_message_id
                    return (True, True)  # Success, but clear thread_id
                elif sent_msg.message_thread_id != thread_id:
                    logger.warning(
                        f"⚠️ WARNING: Message was sent with thread_id={thread_id}, "
                        f"but actual message_thread_id={sent_msg.message_thread_id}. "
                        f"They don't match! Will update thread_id."
                    )
                    # Update thread_id to the actual one
                    return (True, True)  # Success, but update thread_id
                else:
                    logger.info(
                        f"✅ Message successfully sent to thread. "
                        f"thread_id={thread_id}, actual message_thread_id={sent_msg.message_thread_id}"
                    )
            return (True, False)  # Success, no need to clear thread
        
        return (False, False)  # Nothing to send
        
    except TelegramMigrateToChat as e:
        logger.error(
            f"Admin chat was upgraded to supergroup. Old ID: {admin_chat_id}, New ID: {e.migrate_to_chat_id}. "
            f"Please update ADMIN_CHAT_ID in .env file."
        )
        return (False, False)  # Failed, but don't clear thread (chat migrated)
    except TelegramBadRequest as e:
        error_msg = str(e).lower()
        # Check if it's a thread-related error
        if "thread not found" in error_msg or "message thread not found" in error_msg:
            logger.warning(f"Thread {thread_id} not found, will retry without thread_id")
            # Retry without thread_id, but still reply to header if available
            try:
                if attachment:
                    await send_attachment(
                        bot,
                        admin_chat_id,
                        attachment,
                        caption=text_content if text_content else None,
                        message_thread_id=None,
                        reply_to_message_id=header_message_id,
                        reply_markup=reply_markup,
                    )
                elif text_content:
                    kwargs = {"chat_id": admin_chat_id, "text": text_content}
                    if header_message_id is not None:
                        kwargs["reply_to_message_id"] = header_message_id
                    if reply_markup is not None:
                        kwargs["reply_markup"] = reply_markup
                    await bot.send_message(**kwargs)
                # Retry succeeded, but thread_id is invalid and should be cleared
                return (True, True)  # Success, but clear thread_id
            except Exception as retry_error:
                logger.error(f"Failed to send message even without thread_id: {retry_error}")
                return (False, True)  # Failed, but clear thread_id anyway
        else:
            logger.error(f"Failed to forward message to admin: {e}")
            return (False, False)  # Failed, but don't clear thread (might be temporary error)
    except Exception as e:
        logger.error(f"Unexpected error forwarding message to admin: {e}")
        return (False, False)  # Failed, but don't clear thread


async def _create_or_get_thread(
    bot: Bot, admin_chat_id: int, header_message_id: int
) -> int | None:
    """
    Get thread_id by replying to the header message.
    In Telegram Forum, when you reply to a message, message_thread_id should be the topic ID.
    Returns thread_id if topics are enabled, None otherwise.
    """
    try:
        logger.info(
            f"Attempting to get thread_id for header message {header_message_id} in chat {admin_chat_id}"
        )
        # Reply to the header message to get thread_id
        reply_msg = await bot.send_message(
            chat_id=admin_chat_id,
            reply_to_message_id=header_message_id,
            text="💬",  # Dummy message to get thread_id
        )
        # If topics are enabled, message_thread_id will be set
        thread_id = reply_msg.message_thread_id
        
        # In Telegram Forum groups:
        # - If header message was sent WITHOUT message_thread_id, it creates a new topic
        #   and its message_thread_id equals its message_id (this is normal!)
        # - If header message was sent WITH message_thread_id, it's in an existing topic
        #   and message_thread_id is the topic ID
        # - When we reply to a message in a topic, message_thread_id should be the topic ID
        
        if thread_id:
            # Check if thread_id equals header_message_id (normal for Forum when header is first message in topic)
            if thread_id == header_message_id:
                logger.info(
                    f"✅ Got thread_id {thread_id} (equals header_message_id {header_message_id}). "
                    f"This is normal for Forum groups when header is the first message in a new topic."
                )
            else:
                logger.info(
                    f"✅ Got thread_id {thread_id} (different from header_message_id {header_message_id}). "
                    f"Header is in an existing topic."
                )
            
            # Delete the dummy message
            try:
                await bot.delete_message(chat_id=admin_chat_id, message_id=reply_msg.message_id)
                logger.debug(f"Deleted dummy message {reply_msg.message_id}")
            except Exception as del_error:
                logger.warning(f"Could not delete dummy message {reply_msg.message_id}: {del_error}")
            
            return thread_id
        else:
            logger.warning(
                f"⚠️ No thread_id returned (topics may not be enabled). "
                f"Message was sent but message_thread_id is None. "
                f"Reply message ID: {reply_msg.message_id}"
            )
            # Delete the dummy message
            try:
                await bot.delete_message(chat_id=admin_chat_id, message_id=reply_msg.message_id)
            except:
                pass
            return None
    except TelegramNetworkError as e:
        logger.warning(
            f"Network error when getting thread (temporary): {e}. "
            f"Will retry on next message."
        )
        return None
    except Exception as e:
        logger.warning(f"Could not get thread (topics may not be enabled): {e}")
        logger.warning(f"Full error: {traceback.format_exc()}")
        return None


# DISABLED: Automatic follow-up question after 15 minutes
# def _follow_up_keyboard() -> InlineKeyboardMarkup:
#     """Create inline keyboard for follow-up question (Да/Нет)."""
#     buttons = [
#         [
#             InlineKeyboardButton(text="✅ Так", callback_data="followup:yes"),
#             InlineKeyboardButton(text="❌ Ні", callback_data="followup:no"),
#         ],
#     ]
#     return InlineKeyboardMarkup(inline_keyboard=buttons)


# DISABLED: Automatic follow-up question after 15 minutes
# async def _send_follow_up_question(bot: Bot, ticket_id: int, user_tg_id: int) -> None:
#     """
#     Send follow-up question to user after 15 minutes.
#     Check if ticket is still active before sending.
#     """
#     try:
#         # Wait 15 minutes
#         await asyncio.sleep(FOLLOW_UP_TIMEOUT)
#         
#         # Check if ticket is still active
#         async with db_session() as session:
#             result = await session.execute(
#                 select(Ticket).where(Ticket.id == ticket_id)
#             )
#             ticket = result.scalar_one_or_none()
#             
#             if not ticket or ticket.status != "active":
#                 # Ticket was closed, don't send follow-up
#                 logger.info(f"Ticket {ticket_id} is no longer active, skipping follow-up")
#                 return
#             
#             # Send follow-up question
#             logger.info(f"Attempting to send follow-up question to user {user_tg_id} for ticket {ticket_id}")
#             await bot.send_message(
#                 chat_id=user_tg_id,
#                 text="Чи вирішено ваше питання?",
#                 reply_markup=_follow_up_keyboard(),
#             )
#             logger.info(f"✅ Successfully sent follow-up question to user {user_tg_id} for ticket {ticket_id}")
#     except asyncio.CancelledError:
#         logger.info(f"Follow-up task for ticket {ticket_id} was cancelled")
#     except TelegramForbiddenError as e:
#         logger.error(
#             f"❌ FAILED to send follow-up question to user {user_tg_id} for ticket {ticket_id}: "
#             f"User blocked the bot. Error: {e}"
#         )
#         logger.error(f"Full error details: {traceback.format_exc()}")
#     except TelegramNotFound as e:
#         logger.error(
#             f"❌ FAILED to send follow-up question to user {user_tg_id} for ticket {ticket_id}: "
#             f"Chat/user not found. Error: {e}"
#         )
#         logger.error(f"Full error details: {traceback.format_exc()}")
#     except TelegramAPIError as e:
#         logger.error(
#             f"❌ FAILED to send follow-up question to user {user_tg_id} for ticket {ticket_id}: "
#             f"Telegram API error: {type(e).__name__}: {e}"
#         )
#         logger.error(f"Full error details: {traceback.format_exc()}")
#     except Exception as e:
#         logger.error(
#             f"❌ UNEXPECTED ERROR sending follow-up question to user {user_tg_id} for ticket {ticket_id}: "
#             f"{type(e).__name__}: {e}"
#         )
#         logger.error(f"Full error details: {traceback.format_exc()}")


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

        if await _maybe_redirect_team_user(message, bot, user, session):
            return

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

        if await _maybe_redirect_team_user(message, bot, user, session):
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

    if verdict == "ok":
        closed = await deflect_mod.close_ticket_by_ai(ticket_id, user_tg_id)
        await deflect_mod.mark_outcome(ai_call_id, accepted=True)
        await callback.answer()
        # Drop the buttons off the AI answer either way
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        if closed:
            # Standard closure notice + CSAT request — same as an operator close
            from src.services import csat_service
            await csat_service.send_close_notification(
                bot, user_tg_id=user_tg_id, ticket_id=ticket_id, lang=user_lang,
            )
            asyncio.create_task(
                deflect_mod.notify_admin_thread_ai_closed(bot, ticket_id)
            )
    elif verdict == "no":
        await deflect_mod.mark_outcome(ai_call_id, accepted=False)
        await callback.answer()
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        try:
            await bot.send_message(
                chat_id=user_tg_id,
                text=get_text("deflect_operator_wait", user_lang),
            )
        except Exception:
            pass
    else:
        await callback.answer()


# DISABLED: Automatic follow-up question after 15 minutes
# @user_router.callback_query(F.data.startswith("followup:"))
# async def handle_follow_up_response(callback: CallbackQuery, bot: Bot) -> None:
#     """Handle follow-up question response (Yes/No)."""
#     
#     user_tg_id = callback.from_user.id
#     response = callback.data.split(":", 1)[1]  # "yes" or "no"
#     
#     await callback.answer()
#     
#     async with db_session() as session:
#         # Find user's active ticket
#         result = await session.execute(
#             select(User).where(User.tg_id == user_tg_id)
#         )
#         user = result.scalar_one_or_none()
#         
#         if not user:
#             await callback.message.edit_text("Помилка: користувача не знайдено.")
#             return
#         
#         # Find active ticket for this user
#         result = await session.execute(
#             select(Ticket)
#             .where(Ticket.user_id == user.id, Ticket.status == "active")
#             .order_by(Ticket.id.desc())
#             .limit(1)
#         )
#         ticket = result.scalar_one_or_none()
#         
#         if not ticket:
#             await callback.message.edit_text("Активний запит не знайдено.")
#             return
#         
#         if response == "yes":
#             # Close the ticket
#             ticket.status = "closed"
#             ticket.closed_at = datetime.utcnow()
#             
#             # Save closure message to DB
#             session.add(
#                 MessageRecord(
#                     ticket_id=ticket.id,
#                     sender_type="user",
#                     sender_id=user_tg_id,
#                     text="Користувач підтвердив, що питання вирішено.",
#                 )
#             )
#             await session.commit()
#             
#             # Update message
#             await callback.message.edit_text(
#                 "✅ Дякуємо за відповідь! Ваш запит закрито."
#             )
#             
#             # Notify admin
#             try:
#                 notification_text = (
#                     f"✅ Запит №{ticket.id} закрито користувачем.\n"
#                     f"Користувач підтвердив, що питання вирішено."
#                 )
#                 if ticket.thread_id:
#                     await bot.send_message(
#                         chat_id=settings.admin_chat_id,
#                         message_thread_id=ticket.thread_id,
#                         text=notification_text,
#                     )
#                 elif ticket.header_message_id:
#                     await bot.send_message(
#                         chat_id=settings.admin_chat_id,
#                         reply_to_message_id=ticket.header_message_id,
#                         text=notification_text,
#                     )
#             except Exception as e:
#                 logger.error(f"Failed to notify admin about ticket closure: {e}")
#         
#         elif response == "no":
#             # Keep ticket open and notify admin
#             await callback.message.edit_text(
#                 "Дякуємо за відповідь. Ваш запит залишається відкритим."
#             )
#             
#             # Notify admin that problem is not solved
#             try:
#                 notification_text = (
#                     f"⚠️ Увага! Запит №{ticket.id} не вирішено більше 15 хвилин.\n"
#                     f"Користувач підтвердив, що питання ще не вирішено.\n"
#                     f"Прийміть міри!"
#                 )
#                 if ticket.thread_id:
#                     await bot.send_message(
#                         chat_id=settings.admin_chat_id,
#                         message_thread_id=ticket.thread_id,
#                         text=notification_text,
#                     )
#                 elif ticket.header_message_id:
#                     await bot.send_message(
#                         chat_id=settings.admin_chat_id,
#                         reply_to_message_id=ticket.header_message_id,
#                         text=notification_text,
#                     )
#                 else:
#                     # Fallback: send to admin chat without thread
#                     await bot.send_message(
#                         chat_id=settings.admin_chat_id,
#                         text=notification_text,
#                     )
#             except Exception as e:
#                 logger.error(f"Failed to notify admin about unresolved ticket: {e}")


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

        if await _maybe_redirect_team_user(message, bot, user, session):
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

        # Check if user has an active ticket
        result = await session.execute(
            select(Ticket)
            .where(Ticket.user_id == user.id, Ticket.status == "active")
            .order_by(Ticket.id.desc())
            .limit(1)
        )
        active_ticket = result.scalar_one_or_none()

        if active_ticket:
            # User has an active ticket - continue conversation
            # Prepare message content
            message_text = text_content if text_content else (get_text("attachment_only", user.language) if attachment else "")

            # Save message to DB
            session.add(
                MessageRecord(
                    ticket_id=active_ticket.id,
                    sender_type="user",
                    sender_id=user_tg_id,
                    text=message_text,
                    file_id=attachment.file_id if attachment else None,
                    file_type=attachment.content_type if attachment else None,
                    file_name=attachment.file_name if attachment else None,
                    file_size=attachment.file_size if attachment else None,
                    mime_type=attachment.mime_type if attachment else None,
                )
            )
            # The ticket was parked waiting for this person — their reply puts it
            # back into the operator's working column.
            if active_ticket.stage == "waiting":
                active_ticket.stage = "in_progress"
            await session.flush()

            # Forward to admin thread (only once - either text OR attachment, not both)
            logger.info(
                f"Sending user message to admin for ticket #{active_ticket.id}. "
                f"Thread ID: {active_ticket.thread_id}, Header ID: {active_ticket.header_message_id}"
            )
            success, should_clear_thread = await _forward_user_message_to_admin(
                bot,
                settings.admin_chat_id,
                active_ticket.thread_id,
                active_ticket.header_message_id,
                text_content,
                attachment,
                reply_markup=_ticket_action_keyboard(active_ticket.id),
            )

            # Clear thread_id if function indicates it should be cleared (e.g., thread not found)
            if should_clear_thread and active_ticket.thread_id:
                logger.warning(
                    f"⚠️ Clearing invalid thread_id {active_ticket.thread_id} for ticket {active_ticket.id}. "
                    f"Future messages will be sent as replies to header instead."
                )
                active_ticket.thread_id = None
                await session.commit()
                logger.info(f"Cleared invalid thread_id for ticket {active_ticket.id}")
            
            if success:
                logger.info(
                    f"✅ Successfully sent user message to admin for ticket #{active_ticket.id}. "
                    f"Used thread_id: {active_ticket.thread_id if not should_clear_thread else 'None (cleared)'}"
                )
            else:
                logger.error(
                    f"❌ Failed to send user message to admin for ticket #{active_ticket.id}"
                )

            # No automatic AI suggest on follow-up messages — admin pushes the
            # [🤖 ИИ] button under any message if/when they want a hint.

            # Do NOT send notification - user already received it when ticket was created
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
                await _handle_new_ticket_request(message, bot)
                return
            
            # No category selected and not button click - prompt to use button
            await message.reply(
                get_text("create_new_request", user.language),
                reply_markup=_main_keyboard(user.language),
            )
            return

        # Validate that user sent something
        if not text_content and not attachment:
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

        # Save first message to DB
        message_text = text_content if text_content else (get_text("attachment_only", user.language) if attachment else "")
        session.add(
            MessageRecord(
                ticket_id=ticket.id,
                sender_type="user",
                sender_id=user_tg_id,
                text=message_text,
                file_id=attachment.file_id if attachment else None,
                file_type=attachment.content_type if attachment else None,
                file_name=attachment.file_name if attachment else None,
                file_size=attachment.file_size if attachment else None,
                mime_type=attachment.mime_type if attachment else None,
            )
        )
        await session.flush()

        # Format and send ticket header to admin group
        username_display = f"@{username}" if username else f"id {user_tg_id}"
        # Get team name for display
        team_name = "N/A"
        if user.team_id:
            result = await session.execute(select(Team).where(Team.id == user.team_id))
            team = result.scalar_one_or_none()
            if team:
                team_name = team.name
        
        # Display user ID with team context: team_name:local_user_id
        user_id_display = f"{team_name}:{user.local_user_id}" if user.local_user_id else f"id {user_tg_id}"
        
        # Tag admins configured for this category (editable in settings, not hard-coded)
        from src.services import settings_service
        _tags = await settings_service.get_tags_for_category(category)
        tag_admin = f"{_tags} " if _tags else ""
        link_line = await _build_link_line(session, user)
        admin_header = (
            f"{tag_admin}#ticket\n"
            f"Ticket ID: {ticket.id}\n"
            f"Team: {team_name}\n"
            f"User: {user_id_display} ({username_display})\n"
            f"Category: {category or '—'}\n"
            f"Status: Open"
            f"{link_line}"
        )

        try:
            # Check if admin chat is a Forum group
            try:
                chat_info = await bot.get_chat(chat_id=settings.admin_chat_id)
                is_forum = getattr(chat_info, 'is_forum', False)
                logger.info(
                    f"Admin chat info: type={chat_info.type}, is_forum={is_forum}"
                )
            except Exception as e:
                logger.warning(f"Could not get chat info: {e}")
                is_forum = None  # Unknown
            
            # Send ticket header WITHOUT message_thread_id
            # In Forum groups, this will create a new topic automatically
            logger.info(f"Sending ticket header for ticket #{ticket.id} to admin chat {settings.admin_chat_id}")
            header_msg = await bot.send_message(
                chat_id=settings.admin_chat_id,
                text=admin_header,
            )

            # Store header message ID
            ticket.header_message_id = header_msg.message_id
            logger.info(
                f"Ticket #{ticket.id}: Header message sent. "
                f"Message ID: {header_msg.message_id}, "
                f"Header message_thread_id: {header_msg.message_thread_id}, "
                f"Chat is_forum: {is_forum}"
            )
            
            # In Forum groups, if header was sent without message_thread_id,
            # it creates a new topic and header.message_thread_id equals header.message_id
            # We should use header.message_thread_id if it exists, otherwise get it by replying
            if header_msg.message_thread_id:
                # Header is already in a topic (or created one)
                thread_id = header_msg.message_thread_id
                logger.info(
                    f"Ticket #{ticket.id}: Header message has message_thread_id={thread_id}. "
                    f"Using it as thread_id."
                )
            else:
                # Try to get thread_id by replying to header
                logger.info(
                    f"Ticket #{ticket.id}: Header message has no message_thread_id. "
                    f"Attempting to get thread_id by replying..."
                )
                thread_id = await _create_or_get_thread(
                    bot, settings.admin_chat_id, header_msg.message_id
                )
                
                # If still no thread_id and chat is not a forum, we'll use reply_to_message_id instead
                if not thread_id and is_forum is False:
                    logger.warning(
                        f"Ticket #{ticket.id}: Chat is not a Forum group. "
                        f"Will use reply_to_message_id instead of message_thread_id."
                    )
            
            ticket.thread_id = thread_id
            logger.info(
                f"Ticket #{ticket.id}: Saved thread_id={thread_id}, header_message_id={ticket.header_message_id}"
            )
            await session.flush()

            # Forward user's message (text or attachment) inside the thread or as reply to header
            success, should_clear_thread = await _forward_user_message_to_admin(
                bot,
                settings.admin_chat_id,
                thread_id,
                ticket.header_message_id,
                text_content,
                attachment,
                reply_markup=_ticket_action_keyboard(ticket.id),
            )

            # Clear thread_id if needed (shouldn't happen on first send, but handle it)
            if should_clear_thread and ticket.thread_id:
                ticket.thread_id = None

            # Mark notification as sent and commit
            ticket.notification_sent = True
            await session.commit()

            # Send notification to user (only once when ticket is created)
            try:
                from src.services import settings_service as _ss
                await message.reply(
                    get_text("message_sent", user.language, brand=await _ss.get_brand_name())
                )
            except TelegramNetworkError as e:
                # Network error - log but don't fail the ticket creation
                logger.warning(f"Network error when sending notification to user: {e}. Ticket #{ticket.id} was created successfully.")
            except Exception as e:
                logger.error(f"Failed to send notification to user: {e}. Ticket #{ticket.id} was created successfully.")

            # Off-hours auto-reply: warn the user an operator will answer later.
            try:
                from src.services import work_hours_service
                note = await work_hours_service.off_hours_note()
                if note:
                    await message.reply(note)
            except Exception as e:
                logger.warning(f"Off-hours note failed for ticket #{ticket.id}: {e}")

            # Fire-and-forget AI suggestion. Must NOT block user-facing flow.
            try:
                from src.ai.notifier import suggest_and_post
                asyncio.create_task(suggest_and_post(bot, ticket.id, text_content or ""))
            except Exception as e:
                logger.warning(f"Could not schedule AI suggest for ticket #{ticket.id}: {e}")

            # Fire-and-forget AI deflection: try to answer the USER directly
            # (opt-in setting; silent when the KB has no confident answer).
            try:
                from src.ai.deflect import deflect_and_offer
                asyncio.create_task(deflect_and_offer(
                    bot, ticket.id, text_content or "", user_tg_id, user.language,
                ))
            except Exception as e:
                logger.warning(f"Could not schedule AI deflection for ticket #{ticket.id}: {e}")

        except TelegramMigrateToChat as e:
            new_chat_id = e.migrate_to_chat_id
            logger.error(
                f"Admin chat was upgraded to supergroup. Old ID: {settings.admin_chat_id}, New ID: {new_chat_id}."
            )
            # Try again with new chat ID
            try:
                # Get team name for display
                team_name = "N/A"
                if user.team_id:
                    result = await session.execute(select(Team).where(Team.id == user.team_id))
                    team = result.scalar_one_or_none()
                    if team:
                        team_name = team.name
                
                user_id_display = f"{team_name}:{user.local_user_id}" if user.local_user_id else f"id {user_tg_id}"
                
                # Re-format header with configured tags (not hard-coded)
                from src.services import settings_service
                _tags = await settings_service.get_tags_for_category(category)
                tag_admin = f"{_tags} " if _tags else ""
                link_line = await _build_link_line(session, user)
                admin_header_retry = (
                    f"{tag_admin}#ticket\n"
                    f"Ticket ID: {ticket.id}\n"
                    f"Team: {team_name}\n"
                    f"User: {user_id_display} ({username_display})\n"
                    f"Category: {category}\n"
                    f"Status: Open"
                    f"{link_line}"
                )
                header_msg = await bot.send_message(
                    chat_id=new_chat_id,
                    text=admin_header_retry,
                )
                ticket.header_message_id = header_msg.message_id
                
                # In Forum groups, if header was sent without message_thread_id,
                # it creates a new topic and header.message_thread_id equals header.message_id
                if header_msg.message_thread_id:
                    thread_id = header_msg.message_thread_id
                    logger.info(
                        f"Ticket #{ticket.id}: Header message has message_thread_id={thread_id}. "
                        f"Using it as thread_id."
                    )
                else:
                    thread_id = await _create_or_get_thread(
                        bot, new_chat_id, header_msg.message_id
                    )
                ticket.thread_id = thread_id
                await session.flush()

                success, should_clear_thread = await _forward_user_message_to_admin(
                    bot,
                    new_chat_id,
                    thread_id,
                    ticket.header_message_id,
                    text_content,
                    attachment,
                    reply_markup=_ticket_action_keyboard(ticket.id),
                )

                # Clear thread_id if needed
                if should_clear_thread and ticket.thread_id:
                    ticket.thread_id = None

                ticket.notification_sent = True
                await session.commit()

                from src.services import settings_service as _ss
                await message.reply(
                    get_text("message_sent", user.language, brand=await _ss.get_brand_name())
                )

                # Fire-and-forget AI suggestion (same path as the normal flow above)
                try:
                    from src.ai.notifier import suggest_and_post
                    asyncio.create_task(suggest_and_post(bot, ticket.id, text_content or ""))
                except Exception as e:
                    logger.warning(f"Could not schedule AI suggest for ticket #{ticket.id}: {e}")
            except Exception as e:
                logger.error(f"Failed to send ticket to admin chat: {e}")
