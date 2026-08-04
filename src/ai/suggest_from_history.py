"""Use case: draft a reply for a new ticket using past tickets of the same team+category.

This is the RAG fallback when no canned template matches. We pull N most recent
CLOSED tickets in the same team and category, feed their user-message + admin
replies into the LLM, and ask it to draft a short reply in the same tone.

Called fire-and-forget. Never raises. Logs every attempt to `ai_calls`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from src.ai.client import LLMUnavailable, get_client
from src.ai.prompts import (
    SUGGEST_FROM_HISTORY_SYSTEM,
    build_suggest_from_history_user_prompt,
)
from src.ai.scrubber import scrub
from src.database.db import db_session
from src.database.models import AICall, Message as MessageRecord, Ticket
from src.utils.i18n import CLOSURE_SYSTEM_PREFIX

logger = logging.getLogger(__name__)

# Blacklist enforced in ai/notifier.py from settings; kept empty to avoid a
# second source of truth.
_BLACKLISTED_CATEGORY_FRAGMENTS: list[str] = []
_MAX_USER_TEXT_CHARS = 2000
_MAX_REPLY_CHARS = 600  # cap each historical admin reply to keep prompt sane
_MAX_EXAMPLES = 5

# Stop-words: messages that are mostly these are acknowledgements, not solutions.
# We strip them out so the LLM sees actual resolutions, not "проверяю" noise.
_ACK_TOKENS = {
    "ок", "ok", "окей", "okay", "хорошо", "добре", "good",
    "проверяю", "проверим", "проверяем", "перевіряю", "перевірю", "перевіримо",
    "гляну", "глянем", "подивлюсь", "подивимось", "посмотрю", "посмотрим",
    "сек", "секунду", "хвилинку", "момент",
    "взял", "взяли", "беру", "берем", "беремо", "приняли", "прийняли",
    "в работе", "в роботі", "работаем", "працюємо",
    "ща", "щас", "зараз", "сейчас",
    "понял", "зрозумів", "зрозуміло",
    "ща глянем", "ща посмотрим", "ща проверим",
    "+", "👍", "🆗", "✅",
}


def _is_acknowledgement(text: str) -> bool:
    """True if the message is just an ack ('проверяю', 'ок', 'гляну'...) with no real content."""
    if not text:
        return True
    t = text.strip().lower()
    # Strip trailing punctuation
    t = t.rstrip(".,!?:;…»").strip()
    if not t:
        return True
    # Single token / short phrase that exactly matches an ack
    if t in _ACK_TOKENS:
        return True
    # Phrase wholly composed of ack tokens (e.g. "ок, гляну")
    parts = [p.strip(",.!?:;») ") for p in t.replace(",", " ").split()]
    parts = [p for p in parts if p]
    if parts and all(p in _ACK_TOKENS for p in parts):
        return True
    # Very short and starts with ack — likely just "проверяю!" or "гляну сейчас"
    if len(t) <= 20 and any(t.startswith(tok) for tok in _ACK_TOKENS):
        return True
    return False


@dataclass(slots=True)
class HistorySuggestResult:
    suggested_text: Optional[str]
    ai_call_id: Optional[int]


def _is_category_blacklisted(category: str | None) -> bool:
    if not category:
        return False
    low = category.lower()
    return any(frag in low for frag in _BLACKLISTED_CATEGORY_FRAGMENTS)


def _clean_reply(text: str) -> str:
    """Strip mirror prefixes and template/system markers from an admin reply."""
    text = (text or "").strip()
    if not text:
        return ""
    # Strip mirror prefix like "📤 Шаблон «...»:\n" if present
    for marker in ("📤 Шаблон", "📤 ответ", "📤 "):
        if text.startswith(marker):
            # Drop the first line — that's the prefix
            nl = text.find("\n")
            if nl >= 0:
                text = text[nl + 1 :].strip()
            else:
                text = ""
            break
    # Drop closure-system messages
    if text.startswith(CLOSURE_SYSTEM_PREFIX):
        return ""
    return text


async def _gather_examples(session, team_id: int, category: str, exclude_ticket_id: int) -> list[dict]:
    """Fetch up to N recent CLOSED tickets in the same team+category with their messages."""
    if not team_id or not category:
        return []

    stmt = (
        select(Ticket)
        .where(
            Ticket.team_id == team_id,
            Ticket.category == category,
            Ticket.status == "closed",
            Ticket.id != exclude_ticket_id,
        )
        .order_by(Ticket.closed_at.desc().nullslast(), Ticket.id.desc())
        .limit(_MAX_EXAMPLES * 2)  # over-fetch — some may have no admin replies
        .options(selectinload(Ticket.messages))
    )
    result = await session.execute(stmt)
    tickets = result.scalars().all()

    examples: list[dict] = []
    for ticket in tickets:
        # First user message = the original question
        user_msgs = sorted(
            (m for m in ticket.messages if m.sender_type == "user"),
            key=lambda m: m.created_at,
        )
        admin_msgs = sorted(
            # Internal notes are excluded on purpose: operators write candid
            # things there ("escalate, this one is trouble"), and this text can
            # end up paraphrased into a reply the USER reads.
            (m for m in ticket.messages
             if m.sender_type == "admin" and not getattr(m, "is_internal", False)),
            key=lambda m: m.created_at,
        )
        if not user_msgs:
            continue

        first_user = scrub(user_msgs[0].text or "")[:600]
        cleaned_admin_replies: list[str] = []
        for m in admin_msgs:
            cleaned = _clean_reply(m.text)
            if not cleaned:
                continue
            # Drop pure acknowledgements — they're noise, not resolutions
            if _is_acknowledgement(cleaned):
                continue
            cleaned_admin_replies.append(scrub(cleaned)[:_MAX_REPLY_CHARS])

        if not cleaned_admin_replies:
            continue  # nothing substantive to learn from

        closure = None
        if ticket.admin_categories:
            try:
                from src.services.close_reason_service import is_custom
                # "__custom__" is a storage sentinel, not a reason — feeding it
                # to the model teaches it nothing.
                named = [c for c in ticket.admin_categories if not is_custom(c)]
                closure = ", ".join(named) or None
            except Exception:
                closure = None

        # Take the LAST 2 substantive admin replies — those usually contain the
        # actual resolution, not the initial "проверяю".
        examples.append({
            "user": first_user,
            "admin_replies": cleaned_admin_replies[-2:],
            "closure": closure,
        })

        if len(examples) >= _MAX_EXAMPLES:
            break

    return examples


async def _gather_full_transcript(session, ticket_id: int) -> str:
    """Concatenate the WHOLE ticket dialogue (user + admin) chronologically.

    Critical for avoiding repeat questions: if the LLM sees that an admin
    already asked "какой сервер?" and the user already replied "ws-12", it
    won't re-ask the same question on the next turn.

    Format example:
        User: не работает удаленка
        Admin: какой сервер?
        User: ws-12
    """
    from src.database.models import Message as MessageRecord
    result = await session.execute(
        select(MessageRecord)
        .where(MessageRecord.ticket_id == ticket_id)
        .order_by(MessageRecord.created_at.asc())
    )
    msgs = result.scalars().all()
    lines: list[str] = []
    for m in msgs:
        # Same reason as in _gather_examples: an operator note must never
        # become source material for text the user ends up reading.
        if getattr(m, "is_internal", False):
            continue
        text = _clean_reply(m.text) if m.sender_type == "admin" else (m.text or "").strip()
        if not text:
            continue
        # Skip system closure messages
        if text.startswith(CLOSURE_SYSTEM_PREFIX):
            continue
        role = "User" if m.sender_type == "user" else "Admin"
        lines.append(f"{role}: {text}")
    return "\n".join(lines)


async def suggest_from_history_for_ticket(
    ticket_id: int,
    user_text: str,
) -> HistorySuggestResult:
    """Generate a draft reply for a fresh ticket using historical examples.

    Safe for fire-and-forget — never raises, always logs.
    """
    client = get_client()
    if client is None:
        return HistorySuggestResult(None, None)

    # Load ticket + gather examples + full user-side transcript in one session
    examples: list[dict] = []
    category: str = ""
    full_user_text: str = user_text or ""
    try:
        async with db_session() as session:
            ticket = (await session.execute(
                select(Ticket).where(Ticket.id == ticket_id)
            )).scalar_one_or_none()
            if ticket is None or ticket.status != "active":
                return HistorySuggestResult(None, None)
            if _is_category_blacklisted(ticket.category):
                return HistorySuggestResult(None, None)

            category = ticket.category or ""
            if not ticket.team_id:
                return HistorySuggestResult(None, None)

            examples = await _gather_examples(
                session, ticket.team_id, category, exclude_ticket_id=ticket_id,
            )
            # Pull the FULL dialogue (user + admin) so the LLM doesn't repeat
            # questions that the user has already answered.
            transcript = await _gather_full_transcript(session, ticket_id)
            if transcript:
                full_user_text = transcript
    except Exception as e:
        logger.error(f"history suggest: failed to load context for #{ticket_id}: {e}", exc_info=True)
        return HistorySuggestResult(None, None)

    if not examples:
        logger.info(
            f"history suggest #{ticket_id}: no closed examples in team+category — skip"
        )
        return HistorySuggestResult(None, None)

    safe_user_text = scrub(full_user_text)[:_MAX_USER_TEXT_CHARS]
    user_prompt = build_suggest_from_history_user_prompt(
        user_text=safe_user_text,
        category=category,
        examples=examples,
    )

    response = None
    error_msg: Optional[str] = None
    try:
        response = await client.complete(
            system=SUGGEST_FROM_HISTORY_SYSTEM,
            user=user_prompt,
            max_output_tokens=400,  # short reply, but allow some headroom
            temperature=0.4,        # a bit of stylistic flexibility
        )
    except LLMUnavailable as e:
        error_msg = str(e)
        logger.info(f"history suggest #{ticket_id} unavailable: {e}")

    draft: Optional[str] = None
    if response is not None:
        raw = (response.text or "").strip()
        # Strip surrounding quotes if model added them despite the rule
        if len(raw) >= 2 and raw[0] in ('"', "'") and raw[-1] == raw[0]:
            raw = raw[1:-1].strip()
        if raw.lower() == "skip" or not raw:
            draft = None
        else:
            draft = raw
        logger.info(
            f"history suggest #{ticket_id}: examples={len(examples)} "
            f"raw_len={len(raw)} → draft={'<text>' if draft else 'None'}"
        )

    # Persist audit row
    ai_call_id: Optional[int] = None
    try:
        async with db_session() as session:
            row = AICall(
                use_case="suggest_from_history",
                ticket_id=ticket_id,
                suggested_template_id=None,
                model=response.model if response else "n/a",
                tokens_in=response.tokens_in if response else 0,
                tokens_out=response.tokens_out if response else 0,
                latency_ms=response.latency_ms if response else 0,
                success=response is not None and draft is not None,
                error=error_msg,
                accepted=None,
                suggested_text=draft,
            )
            session.add(row)
            await session.flush()
            ai_call_id = row.id
            await session.commit()
    except Exception as e:
        logger.error(f"history suggest: failed to log for #{ticket_id}: {e}")

    return HistorySuggestResult(suggested_text=draft, ai_call_id=ai_call_id)
