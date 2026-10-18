"""Durable customer turns, shared by every channel. No model/network I/O under locks."""
import asyncio
from dataclasses import asdict
from datetime import datetime, timedelta
import logging
import re
import hashlib
import json

from sqlalchemy import select
from src.ai import autonomy_settings, policy
from src.channels.identity import address_of, channel_of
from src.database.ai_models import AIAutoTurn
from src.database.db import db_session
from src.database.delivery_models import OutboundJob
from src.database.models import AICall, AppSetting, Message, Ticket, User
from src.services import settings_service

logger = logging.getLogger(__name__)
_HUMAN = re.compile(
    r"^\s*(?:operator|human|live agent|оператор[ау]?|человек|людина)[.!?\s]*$"
    r"|\b(?:speak|talk|connect|transfer|need|want|ask|request).{0,45}\b(?:human|operator|live agent|real person)\b"
    r"|(?:хочу|нужен|позов|соедин|свяж|передай|поклич|потріб|з'єдна).{0,45}(?:оператор|человек|людин|співробітник)", re.I)
_HANDOFF = {"needs_operator", "customer_requested_operator", "turn_limit", "human_takeover"}


async def enqueue(session, message: Message) -> None:
    """Called inside intake's transaction. Existing messages are never backfilled."""
    if message.sender_type != "user" or message.is_internal or message.deleted_at:
        return
    master = await session.get(AppSetting, settings_service.KEY_AI_ENABLED)
    auto = await session.get(AppSetting, settings_service.KEY_AI_DEFLECTION)
    if (master and master.value != "1") or not auto or auto.value != "1":
        return
    existing = (await session.execute(select(AIAutoTurn.id).where(
        AIAutoTurn.message_id == message.id))).scalar_one_or_none()
    if existing is None:
        session.add(AIAutoTurn(ticket_id=message.ticket_id, message_id=message.id))


async def _state(session, turn, config, *, delivering=False):
    ticket = await session.get(Ticket, turn.ticket_id)
    message = await session.get(Message, turn.message_id)
    user = await session.get(User, ticket.user_id) if ticket else None
    if not ticket or ticket.status != "active" or not user or not user.is_active:
        return "closed_or_blocked", None
    if not await policy.allows(ticket, deflection=True):
        return "policy_blocked", None
    if not message or message.deleted_at or message.is_internal:
        return "message_removed", None
    if ticket.assigned_admin_id:
        return "human_takeover", None
    human = (await session.execute(select(Message.id).where(
        Message.ticket_id == ticket.id, Message.sender_type == "admin",
        Message.is_internal.is_(False), Message.deleted_at.is_(None),
        (Message.operator_id.is_not(None) | (Message.sender_id != 0)),
        Message.delivery_status.not_in(["failed", "cancelled"]),
    ).limit(1))).scalar_one_or_none()
    if human:
        return "human_takeover", None
    latest = (await session.execute(select(Message.id).where(
        Message.ticket_id == ticket.id, Message.sender_type == "user",
        Message.deleted_at.is_(None), Message.is_internal.is_(False),
    ).order_by(Message.id.desc()).limit(1))).scalar_one_or_none()
    if latest != message.id:
        return "superseded", None
    if _HUMAN.search(message.text or ""):
        return "customer_requested_operator", None
    rejected = (await session.execute(select(AICall.id).where(
        AICall.ticket_id == ticket.id, AICall.use_case == "deflect",
        AICall.shadow.is_(False), AICall.accepted.is_(False),
    ).limit(1))).scalar_one_or_none()
    previous = list((await session.execute(select(AIAutoTurn).where(
        AIAutoTurn.ticket_id == ticket.id, AIAutoTurn.id != turn.id,
        AIAutoTurn.message_id < turn.message_id,
    ))).scalars())
    if rejected or any(row.reason in _HANDOFF for row in previous):
        return "customer_handoff", None
    limit = 1 if config.mode == "single" else config.max_turns
    if sum(row.ai_call_id is not None for row in previous) >= limit:
        return "single_reply_limit" if config.mode == "single" else "turn_limit", None
    unfinished = (await session.execute(select(OutboundJob.id).join(Message).where(
        Message.ticket_id == ticket.id, OutboundJob.purpose.like("ai_turn:%"),
        OutboundJob.status.in_(["pending", "sending", "retry", "uncertain", "failed"]),
        OutboundJob.id != (turn.outbound_job_id or 0),
    ).limit(1))).scalar_one_or_none()
    if unfinished:
        return "delivery_needs_attention", None
    rows = list((await session.execute(select(Message).where(
        Message.ticket_id == ticket.id, Message.deleted_at.is_(None),
        Message.is_internal.is_(False), Message.delivery_status == "delivered",
        Message.id <= message.id,
    ).order_by(Message.id.desc()).limit(config.history_messages))).scalars())[::-1]
    from src.ai.company_context import load as company_load
    company = await company_load()
    stamp = {"team": ticket.team_id, "category": ticket.category,
             "message_revision": message.revision,
             "history": [[r.id, r.revision] for r in rows],
             "policy": hashlib.sha256(json.dumps(asdict(config), sort_keys=True).encode()).hexdigest(),
             "company": [company.revision, company.enabled],
             "shadow": await settings_service.get_ai_shadow_mode()}
    if delivering and {key: (turn.snapshot or {}).get(key) for key in stamp} != stamp:
        return "context_changed", None
    if delivering:
        from src.ai.reference_guard import current
        if not await current(session, (turn.snapshot or {}).get("references", [])):
            return "sources_changed", None
    return None, (message, user, rows, stamp)


def _finish(turn, status, reason=None):
    turn.status, turn.reason, turn.finished_at = status, reason, datetime.utcnow()


async def delivery_guard(session, job) -> bool:
    """Also used on delayed/manual retries: recheck takeover and stale context."""
    turn = (await session.execute(select(AIAutoTurn).where(
        AIAutoTurn.outbound_job_id == job.id))).scalar_one_or_none()
    if turn is None:
        return False
    config = await autonomy_settings.load()
    reason, state = await _state(session, turn, config, delivering=True)
    if not reason and state[3]["shadow"]:
        reason = "shadow_mode"
    if reason:
        _finish(turn, "withheld", reason)
        return False
    return True


async def process(turn_id: int) -> None:
    from src.ai.deflect import try_deflect_for_ticket, _deflect_options
    from src.ai.scrubber import scrub
    from src.services import outbound
    from src.utils.i18n import get_text

    config = await autonomy_settings.load()
    async with db_session(write=True) as session:
        turn = await session.get(AIAutoTurn, turn_id)
        if not turn or turn.status != "pending":
            return
        # Only one model call per ticket at a time, across all processes.
        busy = (await session.execute(select(AIAutoTurn.id).where(
            AIAutoTurn.ticket_id == turn.ticket_id, AIAutoTurn.status == "generating",
        ).limit(1))).scalar_one_or_none()
        if busy:
            return
        reason, state = await _state(session, turn, config)
        if reason:
            _finish(turn, "withheld", reason)
            return
        message, user, rows, stamp = state
        turn.snapshot, turn.status, turn.started_at = stamp, "generating", datetime.utcnow()
        ticket_id, text = turn.ticket_id, message.text or ""
        transcript = "\n".join(f"{r.sender_type}: {scrub(r.text or '[attachment]')[:1500]}" for r in rows)
        channel, address, lang = channel_of(user), address_of(user), user.language

    result = await try_deflect_for_ticket(ticket_id, text, config=config, transcript=transcript)
    async with db_session(write=True) as session:
        turn = await session.get(AIAutoTurn, turn_id)
        if not turn or turn.status != "generating":
            return
        turn.ai_call_id = result.ai_call_id
        reason, state = await _state(session, turn, await autonomy_settings.load(), delivering=True)
        if reason:
            _finish(turn, "withheld", reason)
            return
        if not result.answer:
            failed = result.reason in {"internal_error", "invalid_response", "empty_response"} or (result.reason or "").startswith("provider_")
            _finish(turn, "failed" if failed else "withheld",
                    result.reason or "no_grounded_answer")
            return
        if stamp["shadow"]:
            _finish(turn, "shadow", "shadow_mode")
            return
        turn.snapshot = {**stamp, "references": result.references or []}
        body = get_text("deflect_prefix", lang) + "\n\n" + result.answer
        options = _deflect_options(ticket_id, result.ai_call_id or 0, lang)
        if result.action == "clarify":
            options = options[1:]  # A question cannot be marked as a solved answer.
        try:
            job = await outbound.enqueue(session, ticket_id=ticket_id, channel=channel,
                address=address, text=body, options=options, require_active=True,
                purpose=f"ai_turn:{turn.id}")
        except (LookupError, ValueError):
            _finish(turn, "failed", "channel_unavailable")
            return
        turn.outbound_job_id, turn.status = job.id, "queued"
        row = await session.get(Message, job.message_id)
        row.sender_label = "AI"
        job_id = job.id
    # The outbox owns delivery and retries; analytics reads its actual status.
    await outbound.deliver(job_id)


async def run_once(limit=10) -> None:
    async with db_session(write=True) as session:
        stale = list((await session.execute(select(AIAutoTurn).where(
            AIAutoTurn.status == "generating",
            AIAutoTurn.started_at < datetime.utcnow() - timedelta(minutes=5),
        ))).scalars())
        for turn in stale:
            _finish(turn, "failed", "interrupted")
        ids = list((await session.execute(select(AIAutoTurn.id).where(
            AIAutoTurn.status == "pending").order_by(AIAutoTurn.id).limit(limit))).scalars())
    for ident in ids:
        try:
            await process(ident)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Automatic AI turn %s failed (%s)", ident, type(exc).__name__)
            async with db_session(write=True) as session:
                turn = await session.get(AIAutoTurn, ident)
                if turn and turn.status in {"pending", "generating"}:
                    _finish(turn, "failed", "internal_error")


async def poll_loop() -> None:
    while True:
        try:
            await run_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Customer AI queue unavailable; will check again")
        await asyncio.sleep(2)
