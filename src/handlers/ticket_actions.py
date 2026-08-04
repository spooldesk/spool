"""Everything an operator can do to a ticket from inside the admin chat.

This is what is left of the bot's operator interface. The inline-button menu it
used to carry — ticket lists, user management, teams, templates CRUD, broadcast,
statistics — is gone: all of it existed in the dashboard too, in a better form,
and maintaining two implementations of the same features was paying twice for
one thing.

What survives is the part the dashboard genuinely cannot do: acting on a ticket
without leaving the conversation. Each user message in the admin chat carries
[Templates] [AI] [Close]; the AI notifier posts suggestions with their own
buttons; and a plain message typed in a ticket's thread is forwarded to the
user by admin_handlers. Anything more than that belongs in the dashboard.

Also here: placing a newly registered user into a team, because those buttons
arrive in the admin chat the moment somebody presses /start and answering them
there is one tap instead of a browser round trip.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters.base import Filter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from src.config import load_settings
from src.database.db import db_session
from src.database.models import Message as MessageRecord, Team, Ticket, User
from src.utils.callback_data import (
    build_callback_data,
    get_payload_value,
    safe_parse_callback_data,
)
from src.utils.i18n import CLOSURE_SYSTEM_TEXT, get_text

logger = logging.getLogger(__name__)
settings = load_settings()

ticket_actions_router = Router(name="ticket_actions")


def _op(key: str, **kwargs) -> str:
    """Operator-facing text.

    These strings are read by the support team, not by their customers, so they
    follow the instance's UI language — the same setting the dashboard uses —
    rather than the per-user language `get_text()` resolves.
    """
    from src.dashboard.i18n import t as dash_t
    return dash_t(key, **kwargs)


class AdminStates(StatesGroup):
    """FSM states still in use.

    Only two remain — both are "the operator is typing something for a specific
    ticket". Everything else the old state machine tracked was menu navigation.
    """

    TICKET_CLOSE_CUSTOM_REASON = State()   # typing a free-text closure reason
    AI_DRAFT_EDIT = State()                # editing an AI draft before sending


# ==================== Callback plumbing ====================


class CallbackDataFilter(Filter):
    """Match a callback by scope/entity/action, tolerating payload differences."""

    def __init__(self, scope: str, entity: str, action: Optional[str] = None):
        self.scope, self.entity, self.action = scope, entity, action

    async def __call__(self, callback: CallbackQuery) -> bool:
        parsed = safe_parse_callback_data(callback.data)
        if not parsed:
            return False
        scope, entity, action, _payload = parsed
        if scope != self.scope or entity != self.entity:
            return False
        return self.action is None or action == self.action


def _filter_callback(scope: str, entity: str, action: str, payload_check=None):
    """Build a filter for `scope:entity:action`, optionally checking the payload."""

    async def _check(callback: CallbackQuery) -> bool:
        parsed = safe_parse_callback_data(callback.data)
        if not parsed:
            return False
        s, e, a, payload = parsed
        if (s, e, a) != (scope, entity, action):
            return False
        return payload_check(payload) if payload_check else True

    return _check


def _cb_ticket(action: str, ticket_id: int, **extra) -> str:
    return build_callback_data("admin", "ticket", action, {"i": ticket_id, **extra})


def _cb_team(action: str, **payload) -> str:
    return build_callback_data("admin", "team", action, payload)


def _cb_noop() -> str:
    return build_callback_data("admin", "noop", "noop")


async def _safe_edit(message: Message, text: str, reply_markup=None) -> None:
    """Edit a message, ignoring Telegram's 'message is not modified'."""
    try:
        await message.edit_text(text, reply_markup=reply_markup)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e).lower():
            logger.debug(f"edit_text failed: {e}")


# ==================== Handlers ====================


async def _build_close_keyboard(ticket_id: int, selected_categories: list[str]):
    """Build the multiselect closure keyboard from the editable CloseReason catalog.

    Returns (keyboard, selected_text). Reason codes are the DB id (str); the
    free-text option uses the CUSTOM_CODE sentinel. Selected values are stored
    as reason *names* (snapshots), so they survive catalog edits.
    """
    from src.services import close_reason_service
    async with db_session() as session:
        reasons = await close_reason_service.list_active(session)
        dmap = await close_reason_service.display_map(session)

    keyboard_buttons = []
    for r in reasons:
        checkbox = "☑" if r.name in selected_categories else "☐"
        keyboard_buttons.append([
            InlineKeyboardButton(
                text=f"{checkbox} {close_reason_service.display_name(r)}",
                callback_data=_cb_ticket("category_toggle", ticket_id=ticket_id, category=str(r.id)),
            )
        ])
    # Free-text option — code sentinel, not a DB row
    checkbox = "☑" if any(
        close_reason_service.is_custom(c) for c in selected_categories
    ) else "☐"
    keyboard_buttons.append([
        InlineKeyboardButton(
            text=f"{checkbox} {close_reason_service.custom_display()}",
            callback_data=_cb_ticket("category_toggle", ticket_id=ticket_id,
                                     category=close_reason_service.CUSTOM_CODE),
        )
    ])
    keyboard_buttons.append([
        InlineKeyboardButton(text=_op("op_close_done_btn"), callback_data=_cb_ticket("close_confirm", ticket_id)),
        InlineKeyboardButton(text=_op("op_cancel"), callback_data=_cb_noop()),
    ])
    keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)

    selected_display = [dmap.get(c, c) for c in selected_categories]
    selected_text = ", ".join(selected_display) if selected_display else _op("op_nothing_selected")
    return keyboard, selected_text


async def _resolve_close_code(code: str) -> str | None:
    """Map a closure-keyboard code back to the stored reason name (or CUSTOM)."""
    from src.services import close_reason_service
    if code == close_reason_service.CUSTOM_CODE:
        return close_reason_service.CUSTOM_VALUE
    try:
        async with db_session() as session:
            r = await close_reason_service.get(session, int(code))
        return r.name if r else None
    except (TypeError, ValueError):
        return None


@ticket_actions_router.callback_query(_filter_callback("admin", "templates", "pick"))
async def show_template_picker(callback: CallbackQuery, state: FSMContext) -> None:
    """Show top-N templates for a ticket — opened by the [📝 Шаблоны] button under a user message."""
    logger.info(f"🔔 show_template_picker: {callback.data}")
    try:
        await callback.answer()
        parsed = safe_parse_callback_data(callback.data)
        if not parsed:
            return
        _, _, _, payload = parsed
        ticket_id = get_payload_value(payload, "t", type_hint=int)
        if ticket_id is None:
            await callback.answer(_op("op_no_ticket_id"), show_alert=True)
            return

        async with db_session() as session:
            ticket = (await session.execute(
                select(Ticket).where(Ticket.id == ticket_id)
            )).scalar_one_or_none()
            if ticket is None:
                await callback.answer(_op("op_ticket_not_found"), show_alert=True)
                return
            if ticket.status != "active":
                await callback.answer(_op("op_ticket_already_closed", id=ticket_id), show_alert=True)
                return
            templates = await template_service.list_for_ticket(session, ticket, limit=8)

        if not templates:
            await callback.answer(
                _op("op_templates_none"),
                show_alert=True,
            )
            return

        rows = [
            [InlineKeyboardButton(
                text=t.title,
                callback_data=build_callback_data(
                    "admin", "templates", "apply", {"t": ticket_id, "i": t.id},
                ),
            )]
            for t in templates
        ]
        rows.append([InlineKeyboardButton(
            text=_op("op_close_list_btn"),
            callback_data=build_callback_data(
                "admin", "templates", "dismiss", {"t": ticket_id},
            ),
        )])

        # Send a NEW message into the thread (don't edit the user's forwarded message,
        # since the inline-keyboard there must stay attached for future use).
        try:
            kwargs = {
                "chat_id": settings.admin_chat_id,
                "text": _op("op_templates_for", id=ticket_id),
                "reply_markup": InlineKeyboardMarkup(inline_keyboard=rows),
            }
            # Mirror the message into the same thread / reply chain as the original
            src_msg = callback.message
            if src_msg is not None:
                if src_msg.message_thread_id is not None:
                    kwargs["message_thread_id"] = src_msg.message_thread_id
                kwargs["reply_to_message_id"] = src_msg.message_id
            await callback.bot.send_message(**kwargs)
        except Exception as e:
            logger.error(f"Failed to send template picker: {e}", exc_info=True)
            await callback.answer(_op("op_error"), show_alert=True)
    except Exception as e:
        logger.error(f"Error in show_template_picker: {e}", exc_info=True)
        try:
            await callback.answer(_op("op_error"), show_alert=True)
        except Exception:
            pass


@ticket_actions_router.callback_query(_filter_callback("admin", "templates", "apply"))
async def apply_template_from_picker(callback: CallbackQuery, state: FSMContext) -> None:
    """Apply a template to a ticket: send rendered text to user + mirror + log."""
    logger.info(f"🔔 apply_template_from_picker: {callback.data}")
    try:
        parsed = safe_parse_callback_data(callback.data)
        if not parsed:
            await callback.answer("⚠️ Bad callback", show_alert=True)
            return
        _, _, _, payload = parsed
        ticket_id = get_payload_value(payload, "t", type_hint=int)
        template_id = get_payload_value(payload, "i", type_hint=int)
        if ticket_id is None or template_id is None:
            await callback.answer(_op("op_missing_params"), show_alert=True)
            return

        admin = callback.from_user
        async with db_session() as session:
            result = await template_service.apply(
                session, callback.bot,
                template_id=template_id,
                ticket_id=ticket_id,
                admin_tg_id=admin.id if admin else 0,
                admin_chat_id=settings.admin_chat_id,
                admin_first_name=admin.first_name if admin else None,
            )
            await session.commit()

        if not result.success:
            await callback.answer(f"⚠️ {result.error}", show_alert=True)
            return

        await callback.answer(_op("op_sent"))
        # Remove the picker message — its job is done
        try:
            await callback.message.delete()
        except Exception:
            try:
                await callback.message.edit_text(
                    _op("op_template_applied", id=ticket_id),
                    reply_markup=None,
                )
            except Exception:
                pass
    except Exception as e:
        logger.error(f"Error in apply_template_from_picker: {e}", exc_info=True)
        try:
            await callback.answer(f"⚠️ {type(e).__name__}", show_alert=True)
        except Exception:
            pass


@ticket_actions_router.callback_query(_filter_callback("admin", "templates", "dismiss"))
async def dismiss_template_picker(callback: CallbackQuery) -> None:
    """Close the template picker message."""
    try:
        await callback.answer()
        try:
            await callback.message.delete()
        except Exception:
            pass
    except Exception as e:
        logger.error(f"Error in dismiss_template_picker: {e}", exc_info=True)


@ticket_actions_router.callback_query(_filter_callback("admin", "ai", "ask"))
async def ai_ask_button(callback: CallbackQuery) -> None:
    """Admin clicked [🤖 ИИ] under a user message — generate a suggestion on demand."""
    logger.info(f"🔔 ai_ask_button: {callback.data}")
    try:
        await callback.answer(_op("op_ai_thinking"))
        parsed = safe_parse_callback_data(callback.data)
        if not parsed:
            return
        _, _, _, payload = parsed
        ticket_id = get_payload_value(payload, "t", type_hint=int)
        if ticket_id is None:
            await callback.answer(_op("op_no_ticket_id"), show_alert=True)
            return

        # Run the same notifier the auto-flow uses, in manual mode:
        # bypasses debounce and reports back if nothing fits.
        from src.ai.notifier import suggest_and_post
        await suggest_and_post(callback.bot, ticket_id, user_text="", manual=True)
    except Exception as e:
        logger.error(f"Error in ai_ask_button: {e}", exc_info=True)
        try:
            await callback.answer(f"⚠️ {type(e).__name__}", show_alert=True)
        except Exception:
            pass


@ticket_actions_router.callback_query(_filter_callback("admin", "ai", "accept"))
async def ai_suggestion_accept(callback: CallbackQuery) -> None:
    """Admin clicked [✅ Применить] on an AI-suggested template."""
    logger.info(f"🔔 ai_suggestion_accept: {callback.data}")
    try:
        parsed = safe_parse_callback_data(callback.data)
        if not parsed:
            await callback.answer("⚠️ Bad callback", show_alert=True)
            return
        _, _, _, payload = parsed
        ticket_id = get_payload_value(payload, "t", type_hint=int)
        template_id = get_payload_value(payload, "i", type_hint=int)
        ai_call_id = get_payload_value(payload, "a", default=0, type_hint=int)
        if ticket_id is None or template_id is None:
            await callback.answer(_op("op_missing_params"), show_alert=True)
            return

        admin = callback.from_user
        async with db_session() as session:
            result = await template_service.apply(
                session, callback.bot,
                template_id=template_id,
                ticket_id=ticket_id,
                admin_tg_id=admin.id if admin else 0,
                admin_chat_id=settings.admin_chat_id,
                admin_first_name=admin.first_name if admin else None,
            )
            await session.commit()

        # Record accept-rate regardless of send success — admin DID choose to accept
        if ai_call_id:
            from src.ai.suggest_template import mark_ai_call_outcome
            await mark_ai_call_outcome(
                ai_call_id, accepted=True,
                accepted_by=admin.id if admin else None,
            )

        if not result.success:
            await callback.answer(f"⚠️ {result.error}", show_alert=True)
            return

        await callback.answer(_op("op_sent"))
        try:
            await callback.message.edit_text(
                _op("op_template_applied_named", preview=result.rendered_text[:80], id=ticket_id),
                reply_markup=None,
            )
        except TelegramBadRequest:
            try:
                await callback.message.delete()
            except Exception:
                pass
    except Exception as e:
        logger.error(f"Error in ai_suggestion_accept: {e}", exc_info=True)
        try:
            await callback.answer(f"⚠️ {type(e).__name__}", show_alert=True)
        except Exception:
            pass


@ticket_actions_router.callback_query(_filter_callback("admin", "ai", "accept_text"))
async def ai_suggestion_accept_text(callback: CallbackQuery) -> None:
    """Admin clicked [✅ Отправить] on an AI history-based draft — send as-is."""
    logger.info(f"🔔 ai_suggestion_accept_text: {callback.data}")
    try:
        parsed = safe_parse_callback_data(callback.data)
        if not parsed:
            await callback.answer("⚠️ Bad callback", show_alert=True)
            return
        _, _, _, payload = parsed
        ticket_id = get_payload_value(payload, "t", type_hint=int)
        ai_call_id = get_payload_value(payload, "a", default=0, type_hint=int)
        if ticket_id is None or not ai_call_id:
            await callback.answer(_op("op_missing_params"), show_alert=True)
            return

        # Fetch the stored draft text
        from src.database.models import AICall
        async with db_session() as session:
            ai_row = (await session.execute(
                select(AICall).where(AICall.id == ai_call_id)
            )).scalar_one_or_none()
            if ai_row is None or not ai_row.suggested_text:
                await callback.answer(_op("op_ai_draft_missing"), show_alert=True)
                return
            draft_text = ai_row.suggested_text

        admin = callback.from_user
        async with db_session() as session:
            result = await template_service.apply_text(
                session, callback.bot,
                ticket_id=ticket_id,
                text=draft_text,
                admin_tg_id=admin.id if admin else 0,
                admin_chat_id=settings.admin_chat_id,
                source_label=_op("src_ai_asis"),
            )
            await session.commit()

        from src.ai.suggest_template import mark_ai_call_outcome
        await mark_ai_call_outcome(
            ai_call_id, accepted=True,
            accepted_by=admin.id if admin else None,
        )

        if not result.success:
            await callback.answer(f"⚠️ {result.error}", show_alert=True)
            return

        await callback.answer(_op("op_sent"))
        try:
            await callback.message.edit_text(
                _op("op_ai_sent", id=ticket_id),
                reply_markup=None,
            )
        except TelegramBadRequest:
            try:
                await callback.message.delete()
            except Exception:
                pass
    except Exception as e:
        logger.error(f"Error in ai_suggestion_accept_text: {e}", exc_info=True)
        try:
            await callback.answer(f"⚠️ {type(e).__name__}", show_alert=True)
        except Exception:
            pass


@ticket_actions_router.callback_query(_filter_callback("admin", "ai", "edit_text"))
async def ai_suggestion_edit_text(callback: CallbackQuery, state: FSMContext) -> None:
    """Admin clicked [✏️ Редактировать] — enter FSM to edit draft before sending."""
    logger.info(f"🔔 ai_suggestion_edit_text: {callback.data}")
    try:
        await callback.answer()
        parsed = safe_parse_callback_data(callback.data)
        if not parsed:
            return
        _, _, _, payload = parsed
        ticket_id = get_payload_value(payload, "t", type_hint=int)
        ai_call_id = get_payload_value(payload, "a", default=0, type_hint=int)
        if ticket_id is None or not ai_call_id:
            await callback.answer(_op("op_missing_params"), show_alert=True)
            return

        from src.database.models import AICall
        async with db_session() as session:
            ai_row = (await session.execute(
                select(AICall).where(AICall.id == ai_call_id)
            )).scalar_one_or_none()
            if ai_row is None or not ai_row.suggested_text:
                await callback.answer(_op("op_ai_draft_missing"), show_alert=True)
                return
            draft_text = ai_row.suggested_text

        await state.set_state(AdminStates.AI_DRAFT_EDIT)
        await state.update_data(ai_ticket_id=ticket_id, ai_call_id=ai_call_id)
        try:
            await callback.message.edit_text(
                _op("op_ai_current_draft", id=ticket_id, draft=draft_text),
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(
                        text=_op("op_cancel"),
                        callback_data=build_callback_data(
                            "admin", "ai", "edit_cancel", {"a": ai_call_id},
                        ),
                    ),
                ]]),
            )
        except TelegramBadRequest:
            pass
    except Exception as e:
        logger.error(f"Error in ai_suggestion_edit_text: {e}", exc_info=True)


@ticket_actions_router.callback_query(_filter_callback("admin", "ai", "edit_cancel"))
async def ai_suggestion_edit_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    """Cancel AI draft edit — clear state, keep audit row untouched."""
    try:
        await callback.answer(_op("op_cancelled"))
        await state.set_state(AdminStates.MAIN_MENU)
        try:
            await callback.message.delete()
        except Exception:
            try:
                await callback.message.edit_text(_op("op_ai_edit_cancelled"), reply_markup=None)
            except Exception:
                pass
    except Exception as e:
        logger.error(f"Error in ai_suggestion_edit_cancel: {e}", exc_info=True)


@ticket_actions_router.message(AdminStates.AI_DRAFT_EDIT)
async def handle_ai_draft_edited(message: Message, state: FSMContext, bot: Bot) -> None:
    """Admin typed the final edited text — send it to the user."""
    edited_text = (message.text or message.caption or "").strip()
    if not edited_text:
        await message.reply(_op("op_ai_empty"))
        return
    if len(edited_text) > 4096:
        await message.reply(_op("op_ai_too_long", length=len(edited_text)))
        return

    data = await state.get_data()
    ticket_id = data.get("ai_ticket_id")
    ai_call_id = data.get("ai_call_id")
    if not ticket_id:
        await message.reply(_op("op_ai_session_lost"))
        await state.set_state(AdminStates.MAIN_MENU)
        return

    admin = message.from_user
    async with db_session() as session:
        result = await template_service.apply_text(
            session, bot,
            ticket_id=ticket_id,
            text=edited_text,
            admin_tg_id=admin.id if admin else 0,
            admin_chat_id=settings.admin_chat_id,
            source_label=_op("src_ai_edited"),
        )
        await session.commit()

    if ai_call_id:
        from src.ai.suggest_template import mark_ai_call_outcome
        # Accepted-with-edit still counts as accepted for accept-rate metrics
        await mark_ai_call_outcome(
            ai_call_id, accepted=True,
            accepted_by=admin.id if admin else None,
        )

    await state.set_state(AdminStates.MAIN_MENU)

    if not result.success:
        await message.reply(f"⚠️ {result.error}")
        return
    await message.reply(_op("op_ai_sent", id=ticket_id))


@ticket_actions_router.callback_query(_filter_callback("admin", "ai", "dismiss"))
async def ai_suggestion_dismiss(callback: CallbackQuery) -> None:
    """Admin clicked [❌ Не то] on an AI suggestion."""
    try:
        await callback.answer(_op("op_ai_noted"))
        parsed = safe_parse_callback_data(callback.data)
        if parsed:
            _, _, _, payload = parsed
            ai_call_id = get_payload_value(payload, "a", default=0, type_hint=int)
            if ai_call_id:
                from src.ai.suggest_template import mark_ai_call_outcome
                admin = callback.from_user
                await mark_ai_call_outcome(
                    ai_call_id, accepted=False,
                    accepted_by=admin.id if admin else None,
                )
        try:
            await callback.message.delete()
        except Exception:
            try:
                await callback.message.edit_text(_op("op_ai_dismissed"), reply_markup=None)
            except Exception:
                pass
    except Exception as e:
        logger.error(f"Error in ai_suggestion_dismiss: {e}", exc_info=True)


@ticket_actions_router.callback_query(_filter_callback("admin", "ticket", "close"))
async def close_ticket_confirm(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    """Show confirmation for closing ticket."""
    logger.info(f"🔔 close_ticket_confirm called with callback_data: {callback.data}")
    try:
        # Answer immediately to prevent "Loading..." state
        await callback.answer()
        
        # Parse callback_data
        parsed = safe_parse_callback_data(callback.data)
        if parsed:
            scope, entity, action, payload = parsed
            if scope == "admin" and entity == "ticket" and action == "close":
                ticket_id = get_payload_value(payload, "i", type_hint=int) or get_payload_value(payload, "id", type_hint=int)
                if ticket_id is None:
                    logger.error(f"Failed to get ticket_id from payload: {payload}, callback_data: {callback.data}")
                    return
            else:
                logger.error(f"Unexpected callback format: scope={scope}, entity={entity}, action={action}")
                return
        else:
            # Fallback to old format
            logger.warning(f"Failed to parse callback_data, using fallback: {callback.data}")
            try:
                ticket_id = int(callback.data.split(":")[-1])
            except (ValueError, IndexError) as e:
                logger.error(f"Failed to parse ticket_id from old format: {e}, callback_data: {callback.data}")
                return
        
        await state.set_state(AdminStates.TICKET_CLOSE_CATEGORIES)
        await state.update_data(ticket_id=ticket_id, selected_categories=[])
        
        # Extract all data while session is open
        user_data = None
        async with db_session() as session:
            result = await session.execute(
                select(Ticket)
                .where(Ticket.id == ticket_id, Ticket.status == "active")
                .options(selectinload(Ticket.user).selectinload(User.team))
            )
            ticket = result.scalar_one_or_none()
            
            if not ticket:
                return
            
            user = ticket.user
            team = user.team if user else None
            user_data = {
                'team_name': team.name if team else None,
                'local_user_id': user.local_user_id if user else None,
                'tg_id': user.tg_id if user else None,
            }
        
        # Build user display outside of session
        if user_data['team_name'] and user_data['local_user_id']:
            user_id_display = f"{user_data['team_name']}:{user_data['local_user_id']}"
        else:
            user_id_display = f"tg_id:{user_data['tg_id']}" if user_data['tg_id'] else "N/A"
        
        # Save user_data in state for later use
        await state.update_data(user_data={"user_id_display": user_id_display})

        # Build keyboard from the editable closure-reason catalog (multiselect)
        keyboard, _ = await _build_close_keyboard(ticket_id, [])

        try:
            await callback.message.edit_text(
                _op("op_close_header", id=ticket_id, user=user_id_display),
                reply_markup=keyboard
            )
        except TelegramBadRequest:
            pass  # Message unchanged
    except Exception as e:
        logger.error(f"Error in close_ticket_confirm: {e}", exc_info=True)
        try:
            await callback.answer(_op("op_error"), show_alert=True)
        except:
            pass


@ticket_actions_router.callback_query(_filter_callback("admin", "ticket", "category_toggle"))
async def toggle_ticket_category(callback: CallbackQuery, state: FSMContext) -> None:
    """Toggle admin category selection for ticket closure."""
    logger.info(f"🔔 toggle_ticket_category called with callback_data: {callback.data}")
    try:
        await callback.answer()
        
        parsed = safe_parse_callback_data(callback.data)
        if not parsed:
            logger.error(f"❌ Failed to parse callback_data: {callback.data}")
            await callback.answer(_op("op_error_format"), show_alert=True)
            return
        
        scope, entity, action, payload = parsed
        logger.info(f"📋 Parsed: scope={scope}, entity={entity}, action={action}, payload={payload}")
        
        if scope != "admin" or entity != "ticket" or action != "category_toggle":
            logger.error(f"❌ Unexpected callback format: scope={scope}, entity={entity}, action={action}")
            await callback.answer(_op("op_error_format"), show_alert=True)
            return
        
        ticket_id = get_payload_value(payload, "i", type_hint=int)
        category_code = get_payload_value(payload, "c", type_hint=str) or get_payload_value(payload, "category", type_hint=str)
        
        logger.info(f"📋 Parsed category toggle: ticket_id={ticket_id}, category_code={category_code}, payload={payload}")
        
        if not ticket_id or not category_code:
            logger.error(f"❌ Missing ticket_id or category_code: ticket_id={ticket_id}, category_code={category_code}")
            await callback.answer(_op("op_missing_params"), show_alert=True)
            return
        
        # Convert keyboard code → stored reason name (or the CUSTOM sentinel)
        category_value = await _resolve_close_code(category_code)
        if category_value is None:
            logger.warning(f"⚠️ Unknown closure code: {category_code}")
            await callback.answer(_op("op_reason_unavailable"), show_alert=True)
            return

        # Get current state data
        data = await state.get_data()
        selected_categories: List[str] = data.get("selected_categories", [])

        # Toggle category (store full value, not code)
        if category_value in selected_categories:
            selected_categories.remove(category_value)
        else:
            selected_categories.append(category_value)

        await state.update_data(selected_categories=selected_categories)

        # Rebuild keyboard + selected-line from the editable catalog
        keyboard, selected_text = await _build_close_keyboard(ticket_id, selected_categories)

        # Get user data for display
        user_data = data.get("user_data", {})
        user_id_display = user_data.get("user_id_display", f"#{ticket_id}")

        try:
            await callback.message.edit_text(
                _op("op_close_header", id=ticket_id, user=user_id_display)
                + _op("op_close_picked", picked=selected_text),
                reply_markup=keyboard
            )
        except TelegramBadRequest:
            pass
    except Exception as e:
        logger.error(f"Error in toggle_ticket_category: {e}", exc_info=True)
        try:
            await callback.answer(_op("op_error"), show_alert=True)
        except:
            pass


@ticket_actions_router.callback_query(_filter_callback("admin", "ticket", "close_confirm"))
async def close_ticket_execute(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    """Execute ticket closure."""
    logger.info(f"🔔 close_ticket_execute called with callback_data: {callback.data}")
    try:
        # Answer immediately to prevent "Loading..." state
        await callback.answer()
        
        # Parse callback_data
        parsed = safe_parse_callback_data(callback.data)
        if parsed:
            scope, entity, action, payload = parsed
            if scope == "admin" and entity == "ticket" and action == "close_confirm":
                ticket_id = get_payload_value(payload, "i", type_hint=int) or get_payload_value(payload, "id", type_hint=int)
                if ticket_id is None:
                    logger.error(f"Failed to get ticket_id from payload: {payload}, callback_data: {callback.data}")
                    return
            else:
                logger.error(f"Unexpected callback format: scope={scope}, entity={entity}, action={action}")
                return
        else:
            # Fallback to old format
            logger.warning(f"Failed to parse callback_data, using fallback: {callback.data}")
            try:
                ticket_id = int(callback.data.split(":")[-1])
            except (ValueError, IndexError) as e:
                logger.error(f"Failed to parse ticket_id from old format: {e}, callback_data: {callback.data}")
                return
        
        # Get selected categories from state
        data = await state.get_data()
        selected_categories: List[str] = data.get("selected_categories", [])
        custom_reason: str | None = data.get("custom_reason")
        
        # Validate: at least one category must be selected
        if not selected_categories:
            await callback.answer(_op("op_pick_one_reason"), show_alert=True)
            return
        
        # If CUSTOM (free-text) is selected, custom_reason must be provided
        from src.services.close_reason_service import is_custom
        if any(is_custom(c) for c in selected_categories) and not custom_reason:
            await state.set_state(AdminStates.TICKET_CLOSE_CUSTOM_REASON)
            keyboard_buttons = [
                [InlineKeyboardButton(text=_op("op_cancel"), callback_data=_cb_noop())]
            ]
            keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)

            prompt_msg = None
            try:
                prompt_msg = await callback.message.edit_text(
                    _op("op_enter_reason", id=ticket_id),
                    reply_markup=keyboard
                )
            except TelegramBadRequest:
                pass
            # Remember WHICH message the admin must reply to. Without this, in a
            # group chat the FSM would swallow every message the admin sends next
            # (e.g. replies to other tickets) as if it were the closure reason.
            prompt_id = prompt_msg.message_id if prompt_msg else callback.message.message_id
            await state.update_data(
                ticket_id=ticket_id,
                selected_categories=selected_categories,
                custom_reason_prompt_id=prompt_id,
            )
            return
        
        # Extract user_tg_id and calculate processing time while session is open
        user_tg_id = None
        processing_time_seconds = None
        async with db_session() as session:
            result = await session.execute(
                select(Ticket)
                .where(Ticket.id == ticket_id, Ticket.status == "active")
                .options(selectinload(Ticket.user))
            )
            ticket = result.scalar_one_or_none()
            
            if not ticket:
                return
            
            # Extract user_tg_id + language before closing ticket
            user_tg_id = ticket.user.tg_id if ticket.user else None
            user_lang = ticket.user.language if ticket.user else None

            # Calculate processing time
            if ticket.created_at:
                processing_time = datetime.utcnow() - ticket.created_at
                processing_time_seconds = int(processing_time.total_seconds())

            # Close ticket and save admin categories
            ticket.status = "closed"
            ticket.closed_at = datetime.utcnow()
            ticket.notification_sent = False
            ticket.admin_categories = selected_categories
            ticket.custom_reason = custom_reason
            ticket.processing_time_seconds = processing_time_seconds

            # Save closure marker (stable internal sentinel, not user-facing)
            admin_tg_id = callback.from_user.id if callback.from_user else 0
            session.add(
                MessageRecord(
                    ticket_id=ticket.id,
                    sender_type="admin",
                    sender_id=admin_tg_id,
                    text=CLOSURE_SYSTEM_TEXT,
                )
            )
            await session.commit()

        # Notify user outside of session (in their language) + ask for a CSAT rating
        if user_tg_id:
            from src.services import csat_service
            await csat_service.send_close_notification(
                bot, user_tg_id=user_tg_id, ticket_id=ticket_id, lang=user_lang,
            )

        try:
            await callback.message.edit_text(_op("op_ticket_closed", id=ticket_id))
        except TelegramBadRequest:
            pass  # Message unchanged

        # Nothing to go back to — the operator stays in the thread.
        await state.clear()
    except Exception as e:
        logger.error(f"Error in close_ticket_execute: {e}", exc_info=True)
        try:
            await callback.answer(_op("op_error"), show_alert=True)
        except:
            pass


@ticket_actions_router.message(AdminStates.TICKET_CLOSE_CUSTOM_REASON)
async def handle_custom_reason_input(message: Message, state: FSMContext, bot: Bot) -> None:
    """Handle custom reason text input for ticket closure.

    Guarded: only a REPLY to the closure prompt counts as the reason. Any other
    message (e.g. the admin answering a different ticket while still 'in' this
    state) exits the closure flow and is forwarded as a normal admin reply,
    instead of being swallowed as the reason. This fixes the group-chat bug
    where one admin starting a close-with-custom-reason would hijack all their
    subsequent messages.
    """
    logger.info(f"🔔 handle_custom_reason_input called")
    try:
        data = await state.get_data()
        prompt_id = data.get("custom_reason_prompt_id")
        replied_id = message.reply_to_message.message_id if message.reply_to_message else None

        # Not a reply to the prompt → admin isn't entering a reason. Bail out of
        # the close flow and let the normal admin-reply path handle this message.
        if not prompt_id or replied_id != prompt_id:
            logger.info(
                f"custom_reason input is not a reply to prompt {prompt_id} "
                f"(replied_to={replied_id}) — leaving close flow, forwarding as admin reply"
            )
            await state.clear()
            try:
                from src.handlers.admin_handlers import handle_admin_reply
                await handle_admin_reply(message, bot)
            except Exception as fwd_err:
                logger.error(f"Failed to forward after leaving close flow: {fwd_err}", exc_info=True)
            return

        custom_reason = message.text.strip() if message.text else ""

        if not custom_reason:
            await message.reply(_op("op_reason_empty"))
            return
        
        if len(custom_reason) > 1000:
            await message.reply(_op("op_reason_too_long"))
            return
        
        # Get state data
        data = await state.get_data()
        ticket_id = data.get("ticket_id")
        selected_categories = data.get("selected_categories", [])
        
        if not ticket_id:
            await message.reply(_op("op_reason_no_ticket"))
            return
        
        # Update state with custom reason
        await state.update_data(custom_reason=custom_reason)
        
        # Now proceed with ticket closure
        # We'll call close_ticket_execute logic here
        user_tg_id = None
        processing_time_seconds = None
        
        async with db_session() as session:
            result = await session.execute(
                select(Ticket)
                .where(Ticket.id == ticket_id, Ticket.status == "active")
                .options(selectinload(Ticket.user))
            )
            ticket = result.scalar_one_or_none()
            
            if not ticket:
                await message.reply(_op("op_ticket_gone"))
                return
            
            user_tg_id = ticket.user.tg_id if ticket.user else None
            user_lang = ticket.user.language if ticket.user else None

            if ticket.created_at:
                processing_time = datetime.utcnow() - ticket.created_at
                processing_time_seconds = int(processing_time.total_seconds())

            ticket.status = "closed"
            ticket.closed_at = datetime.utcnow()
            ticket.notification_sent = False
            ticket.admin_categories = selected_categories
            ticket.custom_reason = custom_reason
            ticket.processing_time_seconds = processing_time_seconds

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

        # Notify user (in their language) + ask for a CSAT rating
        if user_tg_id:
            from src.services import csat_service
            await csat_service.send_close_notification(
                bot, user_tg_id=user_tg_id, ticket_id=ticket_id, lang=user_lang,
            )

        await message.reply(_op("op_ticket_closed_reason", id=ticket_id, reason=custom_reason))

        await state.clear()
    except Exception as e:
        logger.error(f"Error in handle_custom_reason_input: {e}", exc_info=True)
        await message.reply(_op("op_error"))


@ticket_actions_router.callback_query(_filter_callback("admin", "team", "assign"))
async def assign_team_execute(callback: CallbackQuery, state: FSMContext) -> None:
    """Execute team assignment with new format."""
    logger.info(f"🔔 assign_team_execute called with callback_data: {callback.data}")
    try:
        # Answer immediately to prevent "Loading..." state
        await callback.answer()
        
        # Parse callback_data
        parsed = safe_parse_callback_data(callback.data)
        if parsed:
            scope, entity, action, payload = parsed
            if scope == "admin" and entity == "team" and action == "assign":
                user_id = get_payload_value(payload, "u", type_hint=int) or get_payload_value(payload, "user_id", type_hint=int)
                team_name = get_payload_value(payload, "n", type_hint=str) or get_payload_value(payload, "team_name", type_hint=str)
                if user_id and team_name:
                    logger.info(f"Processing team assignment (new format): user_id={user_id}, team_name={team_name}")
                    await _assign_team_to_user(callback, user_id, team_name, state)
                    return
                else:
                    logger.error(f"Missing user_id or team_name in payload: {payload}, callback_data: {callback.data}")
                    return
            else:
                logger.error(f"Unexpected callback format: scope={scope}, entity={entity}, action={action}")
                return
        else:
            # Fallback to old format parsing
            logger.warning(f"Failed to parse callback_data, using fallback: {callback.data}")
            parts = callback.data.split(":", 3)
            if len(parts) < 4:
                logger.error(f"Invalid admin:team:assign format: {callback.data}, parts: {parts}")
                return
            
            try:
                user_id = int(parts[2])
                team_name = parts[3]
                logger.info(f"Processing team assignment (old format): user_id={user_id}, team_name={team_name}")
                await _assign_team_to_user(callback, user_id, team_name, state)
            except (ValueError, IndexError) as e:
                logger.error(f"Error parsing admin:team:assign callback: {callback.data}, error: {e}")
    except Exception as e:
        logger.error(f"Error in assign_team_execute: {e}", exc_info=True)
        try:
            await callback.answer(_op("op_error"), show_alert=True)
        except:
            pass


@ticket_actions_router.callback_query(CallbackDataFilter("team", "assign", None))
async def assign_team_legacy(callback: CallbackQuery, state: FSMContext) -> None:
    """Handle team assignment with legacy format: team:assign:user_id:team_name (backward compatibility)."""
    logger.info(f"🔔 assign_team_legacy called with callback_data: {callback.data}")
    try:
        # Answer immediately to prevent "Loading..." state
        await callback.answer()
        
        # Legacy format: team:assign:user_id:team_name
        # Parse legacy format directly
        parts = callback.data.split(":", 3)
        if len(parts) < 4:
            logger.error(f"Invalid team:assign format: {callback.data}, parts: {parts}, len: {len(parts)}")
            return
        
        try:
            user_id = int(parts[2])
            team_name = parts[3]
            logger.info(f"Processing team assignment (legacy format): user_id={user_id}, team_name={team_name}")
        except (ValueError, IndexError) as e:
            logger.error(f"Error parsing team:assign callback: {callback.data}, error: {e}, parts: {parts}")
            return
        
        await _assign_team_to_user(callback, user_id, team_name, state)
    except Exception as e:
        logger.error(f"Error in assign_team_legacy: {e}", exc_info=True)
        try:
            await callback.answer(_op("op_error"), show_alert=True)
        except:
            pass


@ticket_actions_router.callback_query(_filter_callback("admin", "noop", "noop"))
async def noop_handler(callback: CallbackQuery) -> None:
    """Handle no-op callbacks (e.g., page number display)."""
    await callback.answer()


@ticket_actions_router.callback_query()
async def fallback_callback_handler(callback: CallbackQuery) -> None:
    """
    Fallback handler for any callback_query that wasn't handled by other handlers.
    This ensures that every callback gets an answer, preventing "Loading..." state.
    
    IMPORTANT: This handler is registered with no filters, so it will catch ALL callbacks
    that weren't handled by previous handlers. It MUST be registered last.
    """
    logger.warning(
        f"⚠️ Unhandled callback_query:\n"
        f"  - Raw callback_data: {callback.data}\n"
        f"  - From user: {callback.from_user.id}\n"
        f"  - Chat: {callback.message.chat.id if callback.message else 'N/A'}"
    )
    
    # Try to parse to provide more info
    parsed = safe_parse_callback_data(callback.data)
    if parsed:
        scope, entity, action, payload = parsed
        logger.warning(
            f"  - Parsed: scope={scope}, entity={entity}, action={action}, payload={payload}"
        )
    
    # Always answer to prevent "Loading..." state
    try:
        await callback.answer(_op("op_retired_menu"), show_alert=True)
    except Exception as e:
        logger.error(f"Failed to answer fallback callback: {e}", exc_info=True)
        # Try empty answer as last resort
        try:
            await callback.answer()
        except Exception as e2:
            logger.error(f"Failed to send empty answer: {e2}", exc_info=True)


async def _assign_team_to_user(
    callback: CallbackQuery, user_id: int, team_name: str, state: FSMContext = None,
) -> None:
    """Place a user in a team, from the buttons that arrive when they register.

    Rewritten onto user_service.change_team rather than carried over from the
    menu: the old copy duplicated the same assignment logic the dashboard uses,
    down to a second hardcoded copy of the approval message. One implementation
    means the two paths cannot drift apart.
    """
    from src.services import user_service

    try:
        async with db_session() as session:
            team = (await session.execute(
                select(Team).where(Team.name == team_name)
            )).scalar_one_or_none()
            if team is None:
                await callback.answer(_op("e_team_not_found"), show_alert=True)
                return
            try:
                result = await user_service.change_team(session, user_id, team.id)
            except user_service.UserServiceError as e:
                await callback.answer(str(e), show_alert=True)
                return
            await session.commit()

        # Only on FIRST placement. Moving somebody between teams is internal
        # bookkeeping — they do not need to be told twice that they are approved.
        if not result.is_change:
            try:
                await callback.bot.send_message(
                    chat_id=result.user_tg_id,
                    text=get_text("account_approved", result.user_language),
                )
            except Exception as e:
                logger.warning(f"Failed to notify user {result.user_tg_id}: {e}")

        await callback.answer(
            _op("op_team_assigned", team=team_name, num=result.local_user_id),
            show_alert=False,
        )
        await _safe_edit(
            callback.message,
            _op("op_team_assigned", team=team_name, num=result.local_user_id),
        )
    except Exception as e:
        logger.error(f"Error in _assign_team_to_user: {e}", exc_info=True)
        try:
            await callback.answer(_op("op_error"), show_alert=True)
        except Exception:
            pass
