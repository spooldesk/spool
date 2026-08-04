"""AI deflection — answer the USER right after ticket creation.

Flow: ticket is created normally (admins see it as usual) → this module tries
to produce a confident self-service answer from the team's knowledge base
(canned templates + resolved similar tickets). If it gets one, the user
receives it with two buttons:

    [Solved]           -> the ticket auto-closes, CSAT is requested,
                       the admin thread gets a note; AICall.accepted = True.
    [Need an operator] -> nothing changes (operators already have the ticket);
                       AICall.accepted = False.

Every attempt is logged to ai_calls (use_case="deflect") so the accept rate —
the real deflection rate — shows up in AI analytics like any other use case.

Fire-and-forget: never raises into the message handler.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from aiogram import Bot

# Operator-facing text follows the instance UI language.
from src.dashboard.i18n import t as _op
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select

from src.ai.client import LLMUnavailable, get_client
from src.ai.prompts import DEFLECT_SYSTEM, build_deflect_user_prompt
from src.ai.scrubber import scrub
from src.ai.suggest_from_history import _gather_examples
from src.database.db import db_session
from src.database.models import AICall, Message as MessageRecord, ReplyTemplate, Ticket
from src.utils.i18n import CLOSURE_SYSTEM_TEXT, get_text

logger = logging.getLogger(__name__)

DEFLECT_CB_PREFIX = "defl"  # "defl:ok:{ticket_id}:{ai_call_id}" / "defl:no:..."

# Closure reason snapshot for AI-resolved tickets (shows up in dashboards and
# exports). Language-neutral sentinel — see close_reason_service for why.
from src.services.close_reason_service import AI_VALUE as AI_CLOSE_REASON  # noqa: E402

_MIN_QUESTION_CHARS = 10
_MAX_QUESTION_CHARS = 2000
_MAX_KB_TEMPLATES = 6


@dataclass(slots=True)
class DeflectResult:
    answer: Optional[str]
    ai_call_id: Optional[int]


async def try_deflect_for_ticket(ticket_id: int, user_text: str) -> DeflectResult:
    """Gates + LLM call. Returns (answer|None, ai_call_id|None). Never raises."""
    try:
        from src.services import settings_service

        if not (user_text or "").strip() or len(user_text.strip()) < _MIN_QUESTION_CHARS:
            return DeflectResult(None, None)
        if not await settings_service.get_ai_enabled():
            return DeflectResult(None, None)
        if not await settings_service.get_ai_deflection_enabled():
            return DeflectResult(None, None)

        client = get_client()
        if client is None:
            return DeflectResult(None, None)

        async with db_session() as session:
            ticket = (await session.execute(
                select(Ticket).where(Ticket.id == ticket_id)
            )).scalar_one_or_none()
            if ticket is None or ticket.status != "active":
                return DeflectResult(None, None)
            if await settings_service.is_category_ai_blacklisted(ticket.category):
                return DeflectResult(None, None)
            category = ticket.category or ""
            team_id = ticket.team_id

            # Knowledge base: the team's active templates (category first) ...
            tpl_stmt = (
                select(ReplyTemplate)
                .where(ReplyTemplate.is_archived == False)  # noqa: E712
            )
            if team_id:
                tpl_stmt = tpl_stmt.where(ReplyTemplate.team_id == team_id)
            tpls = list((await session.execute(tpl_stmt)).scalars().all())
            tpls.sort(key=lambda t: (t.category != category, -(t.usage_count or 0)))
            templates_kb = [
                {"title": t.title, "body": scrub(t.body)[:600]}
                for t in tpls[:_MAX_KB_TEMPLATES]
            ]

            # ... plus resolved similar tickets (reuses the RAG gatherer)
            examples = []
            if team_id and category:
                examples = await _gather_examples(
                    session, team_id, category, exclude_ticket_id=ticket_id,
                )

        if not templates_kb and not examples:
            return DeflectResult(None, None)  # nothing to answer from

        prompt = build_deflect_user_prompt(
            user_text=scrub(user_text)[:_MAX_QUESTION_CHARS],
            category=category,
            templates=templates_kb,
            examples=examples,
        )

        response = None
        error_msg: Optional[str] = None
        try:
            response = await client.complete(
                system=DEFLECT_SYSTEM,
                user=prompt,
                max_output_tokens=400,
                temperature=0.2,  # low — this goes straight to the user
            )
        except LLMUnavailable as e:
            error_msg = str(e)

        answer: Optional[str] = None
        if response is not None:
            raw = (response.text or "").strip()
            if len(raw) >= 2 and raw[0] in ('"', "'") and raw[-1] == raw[0]:
                raw = raw[1:-1].strip()
            if raw and raw.lower() != "skip" and len(raw) >= 20:
                answer = raw

        ai_call_id: Optional[int] = None
        async with db_session() as session:
            row = AICall(
                use_case="deflect",
                ticket_id=ticket_id,
                model=response.model if response else "n/a",
                tokens_in=response.tokens_in if response else 0,
                tokens_out=response.tokens_out if response else 0,
                latency_ms=response.latency_ms if response else 0,
                success=answer is not None,
                error=error_msg,
                accepted=None,  # set by the user's button
                suggested_text=answer,
            )
            session.add(row)
            await session.flush()
            ai_call_id = row.id
            await session.commit()

        return DeflectResult(answer, ai_call_id)
    except Exception as e:  # noqa: BLE001
        logger.error(f"deflect: failed for ticket #{ticket_id}: {e}", exc_info=True)
        return DeflectResult(None, None)


def _deflect_keyboard(ticket_id: int, ai_call_id: int, lang: str | None) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text=get_text("deflect_solved_btn", lang),
            callback_data=f"{DEFLECT_CB_PREFIX}:ok:{ticket_id}:{ai_call_id}",
        ),
        InlineKeyboardButton(
            text=get_text("deflect_operator_btn", lang),
            callback_data=f"{DEFLECT_CB_PREFIX}:no:{ticket_id}:{ai_call_id}",
        ),
    ]])


async def deflect_and_offer(bot: Bot, ticket_id: int, user_text: str,
                            user_tg_id: int, lang: str | None) -> None:
    """Background entry-point: try to answer; if confident, message the user."""
    result = await try_deflect_for_ticket(ticket_id, user_text)
    if not result.answer:
        return
    try:
        await bot.send_message(
            chat_id=user_tg_id,
            text=(get_text("deflect_prefix", lang) + "\n\n" + result.answer
                  + "\n\n" + get_text("deflect_question", lang)),
            reply_markup=_deflect_keyboard(ticket_id, result.ai_call_id or 0, lang),
        )
        logger.info(f"🤖 deflect: answer offered to user for ticket #{ticket_id} "
                    f"(ai_call={result.ai_call_id})")
    except Exception as e:  # noqa: BLE001
        logger.error(f"deflect: failed to send answer for #{ticket_id}: {e}")


async def mark_outcome(ai_call_id: int, accepted: bool) -> None:
    """Record the user's verdict on the AI answer."""
    if not ai_call_id:
        return
    try:
        async with db_session() as session:
            row = await session.get(AICall, ai_call_id)
            if row is not None and row.accepted is None:
                row.accepted = accepted
                await session.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"deflect: failed to mark outcome for ai_call {ai_call_id}: {e}")


async def close_ticket_by_ai(ticket_id: int, user_tg_id: int) -> bool:
    """User confirmed the AI answer solved it → close the ticket.

    Ownership + status checked; mirrors the standard closure bookkeeping
    (processing time, closure marker message, snapshot reason).
    """
    async with db_session() as session:
        ticket = (await session.execute(
            select(Ticket).where(Ticket.id == ticket_id, Ticket.status == "active")
        )).scalar_one_or_none()
        if ticket is None:
            return False
        from src.database.models import User
        owner = await session.get(User, ticket.user_id)
        if owner is None or owner.tg_id != user_tg_id:
            return False

        ticket.status = "closed"
        ticket.closed_at = datetime.utcnow()
        ticket.notification_sent = False
        ticket.admin_categories = [AI_CLOSE_REASON]
        if ticket.created_at:
            ticket.processing_time_seconds = int(
                (ticket.closed_at - ticket.created_at).total_seconds()
            )
        session.add(MessageRecord(
            ticket_id=ticket.id, sender_type="admin", sender_id=0,
            text=f"{CLOSURE_SYSTEM_TEXT} {_op('ai_closed_reason')} {AI_CLOSE_REASON}",
        ))
        await session.commit()
    logger.info(f"🤖 deflect: ticket #{ticket_id} closed by AI confirmation")
    return True


async def notify_admin_thread_ai_closed(bot: Bot, ticket_id: int) -> None:
    """Drop a note into the admin thread so operators know why it closed."""
    try:
        from src.config import load_settings
        settings = load_settings()
        async with db_session() as session:
            ticket = await session.get(Ticket, ticket_id)
        if ticket is None:
            return
        kwargs: dict = {}
        if ticket.thread_id is not None:
            kwargs["message_thread_id"] = ticket.thread_id
            if ticket.header_message_id is not None:
                kwargs["reply_to_message_id"] = ticket.header_message_id
        elif ticket.header_message_id is not None:
            kwargs["reply_to_message_id"] = ticket.header_message_id
        await bot.send_message(
            chat_id=settings.admin_chat_id,
            text=_op("ai_closed_by_ai", id=ticket_id),
            **kwargs,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(f"deflect: failed to notify admin thread for #{ticket_id}: {e}")
