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
import re
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

from src.channels import get as get_channel  # noqa: E402
from src.channels.base import Option  # noqa: E402

logger = logging.getLogger(__name__)

DEFLECT_CB_PREFIX = "defl"  # "defl:ok:{ticket_id}:{ai_call_id}" / "defl:no:..."

# Closure reason snapshot for AI-resolved tickets (shows up in dashboards and
# exports). Language-neutral sentinel — see close_reason_service for why.
from src.services.close_reason_service import AI_VALUE as AI_CLOSE_REASON  # noqa: E402

_MIN_QUESTION_CHARS = 10
_MAX_QUESTION_CHARS = 2000
_MAX_KB_TEMPLATES = 6
#: Fewer than the templates, and deliberately: an article is longer, and
#: four good ones beat eight that were dragged in by a common word.
_MAX_KB_ARTICLES = 4


@dataclass(slots=True)
class DeflectResult:
    answer: Optional[str]
    ai_call_id: Optional[int]


#: The bookkeeping line the model is asked to put first. Stripped before
#: anything reaches a customer — see `split_source`.
_SOURCE_LINE = re.compile(r"^[\s>*_#-]*SOURCE\s*[:=]\s*(\S+).*$", re.I)


def split_source(raw: str) -> tuple[Optional[int], str]:
    """(article number, answer) from what the model returned.

    The marker must never reach a customer, so this strips *any* leading line
    that looks like one rather than only the exact format asked for — a model
    that decorates it with a bullet or a bold marker has still marked it, and
    "SOURCE: 2" arriving at the top of a support answer is worse than losing
    the attribution.
    """
    lines = (raw or "").splitlines()
    number: Optional[int] = None

    while lines:
        match = _SOURCE_LINE.match(lines[0].strip())
        if not match:
            break
        # Decoration too: a model that writes "**SOURCE: 2**" has still
        # named the article, and losing the credit over two asterisks
        # would be a silent hole in the only number an article has.
        token = match.group(1).strip().strip("*_`'\".,;:()[]")
        if token.isdigit():
            number = int(token)
        lines.pop(0)

    return number, "\n".join(lines).strip()


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

            # ... published help articles, which are the best of the three:
            # written for the person asking rather than for an operator to
            # send or as a transcript of two people working it out.
            from src.services import article_service

            found = await article_service.candidates_for(
                user_text, category=category, limit=_MAX_KB_ARTICLES)
            articles_kb = [
                {"n": i, "id": a.id, "title": a.title,
                 "body": scrub(a.body)[:900]}
                for i, a in enumerate(found, start=1)
            ]

            # ... plus resolved similar tickets (reuses the RAG gatherer)
            examples = []
            if team_id and category:
                examples = await _gather_examples(
                    session, team_id, category, exclude_ticket_id=ticket_id,
                )

        if not templates_kb and not examples and not articles_kb:
            return DeflectResult(None, None)  # nothing to answer from

        prompt = build_deflect_user_prompt(
            user_text=scrub(user_text)[:_MAX_QUESTION_CHARS],
            category=category,
            templates=templates_kb,
            examples=examples,
            articles=articles_kb,
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
        article_id: Optional[int] = None
        if response is not None:
            raw = (response.text or "").strip()
            if len(raw) >= 2 and raw[0] in ('"', "'") and raw[-1] == raw[0]:
                raw = raw[1:-1].strip()
            source_n, raw = split_source(raw)
            if raw and raw.lower() != "skip" and len(raw) >= 20:
                answer = raw
                # The model names an article by its position in the prompt, so
                # an out-of-range number means it invented one and is credited
                # to nothing rather than to whichever row happens to be there.
                if source_n and 1 <= source_n <= len(articles_kb):
                    article_id = articles_kb[source_n - 1]["id"]

        ai_call_id: Optional[int] = None
        async with db_session() as session:
            from src.services import settings_service as _ss
            _shadow = await _ss.get_ai_shadow_mode()
            row = AICall(
                use_case="deflect",
                ticket_id=ticket_id,
                article_id=article_id,
                model=response.model if response else "n/a",
                tokens_in=response.tokens_in if response else 0,
                tokens_out=response.tokens_out if response else 0,
                latency_ms=response.latency_ms if response else 0,
                success=answer is not None,
                error=error_msg,
                accepted=None,  # set by the user's button
                suggested_text=answer,
                # Shadow calls are measured, never delivered.
                shadow=_shadow
            )
            session.add(row)
            await session.flush()
            ai_call_id = row.id
            await session.commit()

        return DeflectResult(answer, ai_call_id)
    except Exception as e:  # noqa: BLE001
        logger.error(f"deflect: failed for ticket #{ticket_id}: {e}", exc_info=True)
        return DeflectResult(None, None)


def _deflect_options(ticket_id: int, ai_call_id: int, lang: str | None) -> list[Option]:
    """"That solved it" / "I still need a person", in the customer's language.

    The payloads match what the Telegram callback handler already parses, so
    a channel that renders these as links or chips needs no second scheme.
    """
    return [
        Option(get_text("deflect_solved_btn", lang),
               f"{DEFLECT_CB_PREFIX}:ok:{ticket_id}:{ai_call_id}"),
        Option(get_text("deflect_operator_btn", lang),
               f"{DEFLECT_CB_PREFIX}:no:{ticket_id}:{ai_call_id}"),
    ]


async def deflect_and_offer(bot: Bot, ticket_id: int, user_text: str,
                            user_tg_id: int, lang: str | None,
                            channel: str | None = None,
                            address: str | None = None) -> None:
    """Background entry-point: try to answer; if confident, message the user.

    An answer written by a machine and sent without an operator reading it is
    the riskiest thing this system does, so where it goes is explicit: pass
    the channel and address, or get Telegram by the customer's id.
    """
    result = await try_deflect_for_ticket(ticket_id, user_text)
    if not result.answer:
        return
    # Shadow mode reaches furthest here: this is the path that writes straight
    # to a customer, so it is the one people are right to be afraid of. The
    # answer is generated and recorded; it simply never leaves.
    from src.services import settings_service
    if await settings_service.get_ai_shadow_mode():
        logger.info(f"deflect: answer withheld (shadow mode) for ticket #{ticket_id}")
        return
    # Sent *and* recorded. On a channel whose transcript is what the customer
    # reads, an answer that is sent without being recorded reached nobody —
    # which is what happened to every widget visitor the assistant ever
    # answered, invisibly, because sending and showing are the same act there.
    from src.services import outbound

    options = _deflect_options(ticket_id, result.ai_call_id or 0, lang)
    pipe_can_draw = True
    try:
        pipe_can_draw = get_channel(channel).caps.buttons
    except Exception:  # noqa: BLE001 — fall back to spelling them out
        pipe_can_draw = False

    body = (get_text("deflect_prefix", lang) + "\n\n" + result.answer
            + "\n\n" + get_text("deflect_question", lang))
    if not pipe_can_draw:
        body = f"{body}\n\n" + "  ·  ".join(o.label for o in options if o.label)

    delivery = await outbound.say(
        ticket_id=ticket_id, channel=channel,
        address=address or str(user_tg_id), text=body, options=options,
    )
    if delivery.ok:
        logger.info(f"🤖 deflect: answer offered to user for ticket #{ticket_id} "
                    f"(ai_call={result.ai_call_id})")
    else:
        logger.error(f"deflect: could not send answer for #{ticket_id}: "
                     f"{delivery.error_key}")


async def mark_outcome(ai_call_id: int, accepted: bool) -> None:
    """Record the user's verdict on the AI answer."""
    if not ai_call_id:
        return
    credit: Optional[int] = None
    try:
        async with db_session() as session:
            row = await session.get(AICall, ai_call_id)
            if row is not None and row.accepted is None:
                row.accepted = accepted
                credit = row.article_id if accepted else None
                await session.commit()

        # Only on acceptance, and only after the verdict is stored. Counting
        # the offer instead would make every article look useful, which is
        # the same as measuring nothing.
        if credit:
            from src.services import article_service

            await article_service.credit_deflection(credit)
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
