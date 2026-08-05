"""Use case: pick the most relevant reply template for a fresh ticket.

Called fire-and-forget from the ticket-creation flow. Returns the template_id
to suggest, or None if AI is unavailable / nothing fits / the category is
blacklisted. Every call is logged into `ai_calls` for cost/quality tracking.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import select

from src.ai.client import LLMUnavailable, get_client
from src.ai.prompts import (
    SUGGEST_TEMPLATE_SYSTEM,
    build_suggest_template_user_prompt,
)
from src.ai.scrubber import scrub
from src.database.db import db_session
from src.database.models import AICall, ReplyTemplate, Ticket
from src.services import template_service

logger = logging.getLogger(__name__)

# Tickets in these categories never reach the LLM.
# Match is substring, case-insensitive — handles localized variants like
# "1pass/gmail/telegram" (uk default) regardless of exact spelling.
# Category blacklist now lives in settings (editable from the web dashboard) and
# is enforced once in ai/notifier.py before this use case runs. Kept empty here
# to avoid a second, conflicting source of truth.
_BLACKLISTED_CATEGORY_FRAGMENTS: list[str] = []

# Cap input text to avoid huge prompts on free tier
_MAX_USER_TEXT_CHARS = 2000


@dataclass(slots=True)
class SuggestResult:
    """What the caller needs to act on the suggestion."""

    template_id: Optional[int]
    ai_call_id: Optional[int]  # for later updates (accepted/dismissed)


def _is_category_blacklisted(category: str | None) -> bool:
    if not category:
        return False
    low = category.lower()
    return any(frag in low for frag in _BLACKLISTED_CATEGORY_FRAGMENTS)


def _parse_template_id(raw: str, valid_ids: set[int]) -> Optional[int]:
    """Extract a template id from a free-form LLM response.

    Accepts: "42", "42.", "Template 42", " 42 ", "none", "None".
    Rejects values not in `valid_ids` — the model sometimes invents.
    """
    if not raw:
        return None
    text = raw.strip().lower()
    if text in ("none", "null", "n/a", "-", ""):
        return None
    # Find first integer in the string
    digits = ""
    for ch in text:
        if ch.isdigit():
            digits += ch
        elif digits:
            break
    if not digits:
        return None
    try:
        candidate = int(digits)
    except ValueError:
        return None
    if candidate not in valid_ids:
        logger.warning(f"LLM picked template_id={candidate} which is not in candidate set")
        return None
    return candidate


async def suggest_template_for_ticket(
    ticket_id: int,
    user_text: str,
) -> SuggestResult:
    """Suggest the best template for a freshly-created ticket.

    Safe to call as a fire-and-forget task — never raises, always logs.
    """
    client = get_client()
    if client is None:
        return SuggestResult(template_id=None, ai_call_id=None)

    # Load ticket + candidate templates (one round-trip)
    try:
        async with db_session() as session:
            ticket = (await session.execute(
                select(Ticket).where(Ticket.id == ticket_id)
            )).scalar_one_or_none()
            if ticket is None or ticket.status != "active":
                return SuggestResult(template_id=None, ai_call_id=None)

            if _is_category_blacklisted(ticket.category):
                logger.info(
                    f"Ticket #{ticket_id} category {ticket.category!r} is blacklisted "
                    f"for AI — skipping suggest"
                )
                return SuggestResult(template_id=None, ai_call_id=None)

            templates = await template_service.list_for_ticket(session, ticket, limit=12)
    except Exception as e:
        logger.error(f"AI suggest: failed to load ticket #{ticket_id}: {e}", exc_info=True)
        return SuggestResult(template_id=None, ai_call_id=None)

    if not templates:
        logger.info(f"Ticket #{ticket_id}: no templates for team — nothing to suggest")
        return SuggestResult(template_id=None, ai_call_id=None)

    # Build candidate list for the prompt
    candidates = [(t.id, t.title, t.body) for t in templates]
    valid_ids = {t.id for t in templates}

    safe_user_text = scrub(user_text or "")[:_MAX_USER_TEXT_CHARS]
    user_prompt = build_suggest_template_user_prompt(
        user_text=safe_user_text,
        category=ticket.category or "",
        templates=candidates,
    )

    # Call the LLM (timeouts/errors handled by provider → LLMUnavailable)
    response = None
    error_msg: Optional[str] = None
    try:
        response = await client.complete(
            system=SUGGEST_TEMPLATE_SYSTEM,
            user=user_prompt,
            max_output_tokens=64,  # we only need a number, but 2.5-flash thinking can eat budget
            temperature=0.1,       # near-deterministic
        )
    except LLMUnavailable as e:
        error_msg = str(e)
        logger.info(f"AI suggest for ticket #{ticket_id} unavailable: {e}")

    # Parse + persist audit log
    picked_id: Optional[int] = None
    if response is not None:
        picked_id = _parse_template_id(response.text, valid_ids)
        logger.info(
            f"AI suggest ticket #{ticket_id}: raw={response.text!r} → picked={picked_id}"
        )

    ai_call_id: Optional[int] = None
    try:
        async with db_session() as session:
            from src.services import settings_service as _ss
            _shadow = await _ss.get_ai_shadow_mode()
            row = AICall(
                use_case="suggest_template",
                ticket_id=ticket_id,
                suggested_template_id=picked_id,
                model=response.model if response else "n/a",
                tokens_in=response.tokens_in if response else 0,
                tokens_out=response.tokens_out if response else 0,
                latency_ms=response.latency_ms if response else 0,
                success=response is not None,
                error=error_msg,
                accepted=None,  # filled when admin clicks Apply/Dismiss,
                # Shadow calls are measured, never delivered.
                shadow=_shadow
            )
            session.add(row)
            await session.flush()
            ai_call_id = row.id
            await session.commit()
    except Exception as e:
        logger.error(f"AI suggest: failed to log call for ticket #{ticket_id}: {e}")

    return SuggestResult(template_id=picked_id, ai_call_id=ai_call_id)


async def mark_ai_call_outcome(
    ai_call_id: int,
    *,
    accepted: bool,
    accepted_by: int | None = None,
) -> None:
    """Update the audit row once the admin decides — for accept-rate metrics.

    `accepted_by` is the tg_id of the admin who clicked Apply/Edit/Dismiss.
    Only meaningful when accepted=True (we set it for accepted=False too just
    so analytics can tell who rejected, but per-admin AI-stats only counts
    accepted=True rows).
    """
    if not ai_call_id:
        return
    try:
        async with db_session() as session:
            row = (await session.execute(
                select(AICall).where(AICall.id == ai_call_id)
            )).scalar_one_or_none()
            if row is None:
                return
            row.accepted = accepted
            if accepted_by:
                row.accepted_by = accepted_by
            await session.commit()
    except Exception as e:
        logger.error(f"Failed to mark ai_call #{ai_call_id} outcome: {e}")
