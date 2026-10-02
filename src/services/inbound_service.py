"""Persist before acknowledging, and commit a receipt with its ticket messages.

Workers hold the database write lock only during local intake, never during
network calls. A crash rolls back both the message and processed receipt;
another worker can safely retry. Raw pending input survives restarts. Finished
payloads are retained for up to seven days for parser diagnostics, and evicted
earlier to enforce the byte quota; small dedupe receipts remain.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import defer

from src.database.db import db_session
from src.database.inbound_models import InboundEvent, InboundMirror

logger = logging.getLogger(__name__)

MAX_EVENT_BYTES = 25 * 1024 * 1024
MAX_STORED_BYTES = 256 * 1024 * 1024
MAX_PENDING = 2000
POLL_SECONDS = 2
_RETAIN_DAYS = 7


class InboxFull(ValueError):
    """Backpressure: the provider must retry instead of losing accepted data."""


@dataclass(frozen=True)
class ProcessResult:
    status: str
    ticket_id: int | None = None
    message_id: int | None = None
    opened_ticket: bool = False
    error: str | None = None


@dataclass(frozen=True)
class Recorded:
    ticket_id: int = 0
    message_id: int = 0
    opened_ticket: bool = False
    rejected: str | None = None
    message_ids: tuple[int, ...] = ()


async def enqueue(*, channel: str, source_key: str, payload: bytes,
                  context: dict | None = None) -> int:
    if not source_key or len(source_key) > 255 or channel not in ("email", "slack"):
        raise ValueError("invalid inbound identity")
    if len(payload) > MAX_EVENT_BYTES:
        raise InboxFull("inbound message exceeds the configured size limit")
    async with db_session(write=True) as session:
        previous = (await session.execute(select(InboundEvent.id).where(
            InboundEvent.channel == channel, InboundEvent.source_key == source_key,
        ))).scalar_one_or_none()
        if previous is not None:
            return previous
        pending = (await session.execute(select(func.count(InboundEvent.id)).where(
            InboundEvent.status == "pending",
        ))).scalar_one()
        if pending >= MAX_PENDING:
            raise InboxFull("inbound queue is full; processing needs attention")
        used = (await session.execute(select(func.coalesce(func.sum(InboundEvent.payload_size), 0)))).scalar_one()
        cutoff = datetime.utcnow() - timedelta(days=_RETAIN_DAYS)
        finished = (await session.execute(select(InboundEvent).options(defer(InboundEvent.payload)).where(
            InboundEvent.status != "pending", InboundEvent.payload_size > 0,
        ).order_by(InboundEvent.processed_at))).scalars().all()
        for old in finished:
            if used + len(payload) <= MAX_STORED_BYTES and old.processed_at and old.processed_at >= cutoff:
                break
            used -= old.payload_size
            old.payload, old.payload_size = b"", 0
        if used + len(payload) > MAX_STORED_BYTES:
            raise InboxFull("inbound payload quota reached; processing needs attention")
        row = InboundEvent(channel=channel, source_key=source_key, payload=payload,
                           payload_size=len(payload), context=context or {})
        session.add(row)
        await session.flush()
        return row.id


async def process(event_id: int, *, force: bool = False) -> ProcessResult:
    """One recoverable attempt. No provider calls are made under the write lock."""
    try:
        async with db_session() as session:
            row = await session.get(InboundEvent, event_id)
            if row is None:
                return ProcessResult("missing")
            if row.status != "pending":
                return ProcessResult(row.status, row.ticket_id, row.message_id)
            if not force and row.next_attempt_at > datetime.utcnow():
                return ProcessResult("pending")
            channel, payload, context = row.channel, row.payload, dict(row.context)

        if channel == "slack":
            from src.dashboard.routes.slack import prepare_event, record_event
        else:
            from src.services.email_ingest import prepare_event, record_event
        prepared = await prepare_event(payload, context)

        async with db_session(write=True) as session:
            # BEGIN IMMEDIATE / the shared PostgreSQL advisory lock serializes
            # writers; the second worker rechecks after acquiring it. There is
            # no committed "processing" flag or lease that can strand a crash.
            row = await session.get(InboundEvent, event_id, with_for_update=True,
                                    options=[defer(InboundEvent.payload)])
            if row is None:
                return ProcessResult("missing")
            if row.status != "pending":
                return ProcessResult(row.status, row.ticket_id, row.message_id)
            if not force and row.next_attempt_at > datetime.utcnow():
                return ProcessResult("pending")
            result = await record_event(session, prepared, context, event_id=event_id)
            row.status = "ignored" if result.rejected else "processed"
            row.ticket_id = result.ticket_id or None
            row.message_id = result.message_id or None
            row.attempts += 1
            row.last_error = None
            row.processed_at = datetime.utcnow()
            session.add_all(InboundMirror(event_id=event_id, message_id=message_id)
                            for message_id in result.message_ids)
            status = row.status
            # Exiting this context commits both receipt and ALL attached rows.

        # InboundMirror was committed alongside the message. Its independent
        # recovery worker owns Telegram I/O, so an optional operator surface
        # cannot delay email polling or the next accepted Slack event.
        return ProcessResult(status, result.ticket_id or None, result.message_id or None,
                             result.opened_ticket)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        # Keep payload and pending state. Do not expose provider exception text
        # in the public health endpoint or persist SQL parameters/user content.
        error = type(exc).__name__
        logger.warning("Inbound event %s failed (%s); retained for retry", event_id, error)
        try:
            async with db_session(write=True) as session:
                row = await session.get(InboundEvent, event_id, options=[defer(InboundEvent.payload)])
                if row is not None and row.status == "pending":
                    row.attempts += 1
                    row.last_error = error
                    row.next_attempt_at = datetime.utcnow() + timedelta(seconds=min(300, 2 ** min(row.attempts, 8)))
        except Exception:
            logger.warning("Could not record inbound retry state")
        return ProcessResult("pending", error=error)


async def run_once(*, channel: str | None = None, limit: int = 25) -> dict:
    async with db_session() as session:
        query = select(InboundEvent.id).where(
            InboundEvent.status == "pending", InboundEvent.next_attempt_at <= datetime.utcnow(),
        ).order_by(InboundEvent.id).limit(limit)
        if channel:
            query = query.where(InboundEvent.channel == channel)
        ids = (await session.execute(query)).scalars().all()
    done = failures = 0
    for event_id in ids:
        result = await process(event_id)
        done += result.status in ("processed", "ignored")
        failures += result.error is not None
    return {"processed": done, "failed": failures}


async def has_failed_pending(*, channel: str) -> bool:
    """A retry delay must not make a persistently broken intake look healthy."""
    async with db_session() as session:
        return (await session.execute(select(InboundEvent.id).where(
            InboundEvent.channel == channel, InboundEvent.status == "pending",
            InboundEvent.last_error.is_not(None),
        ).limit(1))).scalar_one_or_none() is not None


async def poll_loop() -> None:
    while True:
        try:
            await run_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Inbound queue pass failed; will retry")
        await asyncio.sleep(POLL_SECONDS)
