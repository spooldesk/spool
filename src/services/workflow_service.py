"""Ticket workflow: board stages and internal operator notes.

Stage vs status — two different questions:
    status : is the ticket open or closed?  (lifecycle; the bot depends on it)
    stage  : where is it on the board?      (new / in_progress / waiting)

Keeping them separate means the board can gain granularity without touching
the open/closed logic that every bot handler and query already relies on.
Closed tickets ignore stage entirely — the board derives their column from
status.

Internal notes are Message rows with is_internal=True. They are stored in the
ticket like any other message so the timeline stays chronological, but they are
never sent to the user and never feed AI answers that reach a user.
"""

from __future__ import annotations

import logging
from typing import Optional

from src.database.db import db_session
from src.database.models import Message as MessageRecord, Ticket

logger = logging.getLogger(__name__)

# Board stages for ACTIVE tickets, in board order.
STAGES = ("new", "in_progress", "waiting")
DEFAULT_STAGE = "new"

# Pseudo-stage used by the board for closed tickets (derived, never stored).
CLOSED_COLUMN = "closed"
BOARD_COLUMNS = (*STAGES, CLOSED_COLUMN)


def is_valid_stage(stage: str | None) -> bool:
    return stage in STAGES


def board_column(status: str, stage: str | None) -> str:
    """Which board column a ticket belongs to. Closed always wins."""
    if status == "closed":
        return CLOSED_COLUMN
    return stage if stage in STAGES else DEFAULT_STAGE


async def set_stage(ticket_id: int, stage: str) -> bool:
    """Move an ACTIVE ticket to another stage. Closed tickets are not moved —
    reopening is a separate action, not a side effect of dragging a card."""
    async with db_session(write=True) as session:
        if not await set_stage_in_session(session, ticket_id, stage):
            return False
    logger.info(f"Ticket #{ticket_id} stage -> {stage}")
    return True


async def set_stage_in_session(session, ticket_id: int, stage: str) -> bool:
    """Participate in a caller-owned approval transaction without an early commit."""
    ticket = await session.get(Ticket, ticket_id)
    if not is_valid_stage(stage) or ticket is None or ticket.status != "active":
        return False
    ticket.stage = stage
    return True


async def add_internal_note(
    ticket_id: int, *, text: str, author_tg_id: int = 0,
    operator_id: int | None = None,
) -> Optional[int]:
    """Store an operator-only note on the ticket. Returns the message id."""
    text = (text or "").strip()
    if not text:
        return None
    async with db_session() as session:
        ticket = await session.get(Ticket, ticket_id)
        if ticket is None:
            return None
        note = MessageRecord(
            ticket_id=ticket_id,
            sender_type="admin",
            sender_id=author_tg_id,
            operator_id=operator_id,
            text=text,
            is_internal=True,
        )
        session.add(note)
        await session.flush()
        note_id = note.id
        await session.commit()
    logger.info(f"Internal note added to ticket #{ticket_id}")
    return note_id
