"""Post AI suggestions into the admin thread.

Hybrid strategy:
    1. Ask `suggest_template` — if it picks a known template, post it with
       [✅ Применить] / [❌ Не то].
    2. Otherwise, ask `suggest_from_history` (RAG over closed tickets) for a
       free-form draft. If we get one, post it with
       [✅ Отправить] / [✏️ Редактировать] / [❌ Не то].
    3. If both come back empty — silent.

Kept separate from use cases so they stay pure (DB + LLM only).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import func, select

from src.ai.suggest_from_history import suggest_from_history_for_ticket
from src.ai.suggest_template import suggest_template_for_ticket
from src.ai import policy
from src.config import load_settings
# Module level: the shadow gate runs in helpers that had no local import,
# and a guard that raises NameError is a guard that is not there.
from src.services import settings_service
# Posted into the operators' chat, so it follows the instance UI language.
from src.dashboard.i18n import t as _op
from src.database.db import db_session
from src.database.models import AICall, Message as MessageRecord, ReplyTemplate, Ticket
from src.utils.callback_data import build_callback_data

logger = logging.getLogger(__name__)

# Min interval between AI suggestions for the same ticket — prevents spamming
# the admin thread when a user posts a burst of messages back-to-back.
_SUGGEST_DEBOUNCE_SEC = 30


async def _should_skip_for_debounce(ticket_id: int) -> bool:
    """True if we recently posted an AI suggestion for this ticket."""
    try:
        cutoff = datetime.utcnow() - timedelta(seconds=_SUGGEST_DEBOUNCE_SEC)
        async with db_session() as session:
            recent = (await session.execute(
                select(AICall.id)
                .where(AICall.ticket_id == ticket_id, AICall.created_at >= cutoff)
                .limit(1)
            )).scalar_one_or_none()
        return recent is not None
    except Exception as e:
        logger.warning(f"debounce check failed: {e}")
        return False  # fail-open — better to occasionally double-post than skip


async def _count_user_messages(ticket_id: int) -> int:
    """How many user messages this ticket has so far (incl. the one just saved)."""
    try:
        async with db_session() as session:
            n = (await session.execute(
                select(func.count(MessageRecord.id))
                .where(
                    MessageRecord.ticket_id == ticket_id,
                    MessageRecord.sender_type == "user",
                )
            )).scalar_one()
        return int(n or 0)
    except Exception as e:
        logger.warning(f"count user messages failed: {e}")
        return 0


def _thread_kwargs(ticket: Ticket) -> dict:
    """Build thread/reply kwargs for posting into the ticket's thread."""
    kwargs: dict = {}
    if ticket.thread_id is not None:
        kwargs["message_thread_id"] = ticket.thread_id
        if ticket.header_message_id is not None:
            kwargs["reply_to_message_id"] = ticket.header_message_id
    elif ticket.header_message_id is not None:
        kwargs["reply_to_message_id"] = ticket.header_message_id
    return kwargs


async def _post_template_suggestion(
    bot: Bot, ticket: Ticket, template: ReplyTemplate, ai_call_id: int | None,
) -> None:
    # In shadow mode the call is still made, measured and logged — only
    # the posting stops. That is the whole trick: the evidence needs the
    # suggestion to exist, and the operator needs never to see it.
    if await settings_service.get_ai_shadow_mode():
        return
    if (not await policy.ticket_allowed(ticket.id)
            or template.team_id != ticket.team_id
            or await settings_service.is_category_ai_blacklisted(template.category)):
        return
    settings = load_settings()
    text = (
        f"🤖 {_op('ai_looks_like_template', title=template.title)}\n\n"
        f"{_op('ai_preview')}\n{template.body[:400]}"
        + ("…" if len(template.body) > 400 else "")
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text=_op("ai_btn_apply"),
            callback_data=build_callback_data(
                "admin", "ai", "accept",
                {"t": ticket.id, "i": template.id, "a": ai_call_id or 0},
            ),
        ),
        InlineKeyboardButton(
            text=_op("ai_btn_dismiss"),
            callback_data=build_callback_data(
                "admin", "ai", "dismiss", {"a": ai_call_id or 0},
            ),
        ),
    ]])
    await bot.send_message(
        chat_id=settings.admin_chat_id, text=text, reply_markup=keyboard,
        **_thread_kwargs(ticket),
    )


async def _post_history_suggestion(
    bot: Bot, ticket: Ticket, draft_text: str, ai_call_id: int | None,
) -> None:
    # In shadow mode the call is still made, measured and logged — only
    # the posting stops. That is the whole trick: the evidence needs the
    # suggestion to exist, and the operator needs never to see it.
    if await settings_service.get_ai_shadow_mode():
        return
    if not await policy.ticket_allowed(ticket.id):
        return
    settings = load_settings()
    text = (
        f"🤖 {_op('ai_draft_from_history')}\n\n"
        f"{draft_text}\n\n"
        f"{_op('ai_draft_check')}"
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text=_op("ai_btn_send"),
                callback_data=build_callback_data(
                    "admin", "ai", "accept_text",
                    {"t": ticket.id, "a": ai_call_id or 0},
                ),
            ),
        ],
        [
            InlineKeyboardButton(
                text=_op("ai_btn_edit"),
                callback_data=build_callback_data(
                    "admin", "ai", "edit_text",
                    {"t": ticket.id, "a": ai_call_id or 0},
                ),
            ),
            InlineKeyboardButton(
                text=_op("ai_btn_dismiss"),
                callback_data=build_callback_data(
                    "admin", "ai", "dismiss", {"a": ai_call_id or 0},
                ),
            ),
        ],
    ])
    await bot.send_message(
        chat_id=settings.admin_chat_id, text=text, reply_markup=keyboard,
        **_thread_kwargs(ticket),
    )


async def _post_no_suggestion_notice(bot: Bot, ticket: Ticket, reason: str) -> None:
    """Post a brief notice into the thread when the admin asked AI but nothing came back."""
    settings = load_settings()
    try:
        await bot.send_message(
            chat_id=settings.admin_chat_id,
            text=f"🤖 {_op('ai_notice_prefix')} {reason}",
            **_thread_kwargs(ticket),
        )
    except Exception as e:
        logger.warning(f"Failed to post 'no suggestion' notice: {e}")


async def suggest_and_post(
    bot: Bot,
    ticket_id: int,
    user_text: str,
    *,
    manual: bool = False,
) -> None:
    """Background entry-point — never raises, safe for asyncio.create_task.

    Strategy:
      - 1st user message in the ticket → try template classifier first,
        fall back to history RAG if no template fits.
      - 2nd+ user messages → straight to history RAG (template match on a
        short follow-up like "ещё лагает" is unreliable).
      - Auto-mode: debounced (skip if another suggestion within 30s).
      - Manual mode (admin clicked the 🤖 button): bypass debounce; if nothing
        comes back, post a "nothing to suggest" notice so the admin knows the
        button worked.
    """
    try:
        # Global AI on/off switch (editable from the web dashboard).
        if not await settings_service.get_ai_enabled():
            logger.info(f"AI suggest for ticket #{ticket_id} skipped — AI disabled in settings")
            return

        # Debounce auto-triggered calls; manual button always runs.
        if not manual and await _should_skip_for_debounce(ticket_id):
            logger.info(
                f"AI suggest for ticket #{ticket_id} debounced "
                f"(another within {_SUGGEST_DEBOUNCE_SEC}s)"
            )
            return

        async with db_session() as session:
            ticket = (await session.execute(
                select(Ticket).where(Ticket.id == ticket_id)
            )).scalar_one_or_none()
        if ticket is None or ticket.status != "active":
            if manual and ticket is not None:
                await _post_no_suggestion_notice(bot, ticket, _op("ai_skip_closed"))
            return

        # Category blacklist (editable from web). If the ticket's category is
        # blacklisted, skip AI entirely.
        if await settings_service.is_category_ai_blacklisted(ticket.category):
            logger.info(f"AI suggest for ticket #{ticket_id} skipped — category blacklisted")
            if manual:
                await _post_no_suggestion_notice(bot, ticket, _op("ai_skip_category"))
            return

        user_msg_count = await _count_user_messages(ticket_id)
        # On manual ask we always go straight to history RAG — the admin is
        # asking because there IS context to consider, not at ticket creation.
        is_first_message = (not manual) and user_msg_count <= 1

        # ---- Path 1: template classifier (auto + 1st message only) ----
        if is_first_message:
            template_result = await suggest_template_for_ticket(ticket_id, user_text)
            if template_result.template_id is not None:
                async with db_session() as session:
                    template = (await session.execute(
                        select(ReplyTemplate).where(ReplyTemplate.id == template_result.template_id)
                    )).scalar_one_or_none()
                if template is not None:
                    await _post_template_suggestion(
                        bot, ticket, template, template_result.ai_call_id,
                    )
                    logger.info(
                        f"🤖 Posted TEMPLATE suggestion for ticket #{ticket_id}: "
                        f"template={template.id} ai_call={template_result.ai_call_id}"
                    )
                    return
            logger.info(
                f"No template fit for ticket #{ticket_id} (first msg), trying history-based draft"
            )

        # ---- Path 2: RAG over history (fallback / manual / follow-ups) ----
        history_result = await suggest_from_history_for_ticket(ticket_id, user_text)
        if not history_result.suggested_text:
            logger.info(f"history suggest for #{ticket_id} returned nothing")
            if manual:
                await _post_no_suggestion_notice(
                    bot, ticket,
                    _op("ai_no_match"),
                )
            return

        await _post_history_suggestion(
            bot, ticket, history_result.suggested_text, history_result.ai_call_id,
        )
        logger.info(
            f"🤖 Posted HISTORY draft for ticket #{ticket_id}: "
            f"ai_call={history_result.ai_call_id} chars={len(history_result.suggested_text)} "
            f"manual={manual}"
        )
    except Exception as e:
        logger.error(f"AI notifier failed for ticket #{ticket_id}: {e}", exc_info=True)
