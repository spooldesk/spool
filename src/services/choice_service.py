"""Somebody picked one of the choices we offered them.

Telegram has a callback handler per payload prefix, each reached by a
decorator, each knowing what its own strings mean. That works while Telegram
is the only channel that can draw a button. It stops working the moment a
second one can, because the second channel would need its own copy of every
one of those handlers, and the copies would drift.

So the payload strings stay exactly as they are — the Telegram handlers keep
parsing them, byte for byte — and this is the one place that knows what they
mean for anybody else.

The important rule is at the bottom of `apply`: a payload is only honoured if
it is one the message actually offered. Without that, a visitor can post
`csat:999:5` and rate a stranger's ticket, and the ownership checks further
down would not all catch it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.database.models import Message as MessageRecord, Ticket

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Outcome:
    ok: bool
    #: What to say back, as an i18n key for the *customer's* catalogue.
    reply_key: str | None = None
    #: True when the choice ended the conversation, so a panel can stop
    #: offering a composer without asking again.
    closed: bool = False


async def offer(session: AsyncSession, message_id: int, options) -> None:
    """Attach choices to a message that has already been written.

    Separate from writing the row because the caller that knows the text and
    the caller that knows the options are frequently not the same one, and
    threading `options` through every send path to serve two channels would
    be worse than one extra statement here.
    """
    stored = [{"label": o.label, "payload": o.payload} for o in (options or []) if o.label]
    if not stored:
        return
    row = await session.get(MessageRecord, message_id)
    if row is not None:
        row.options = stored


def _offered(row: MessageRecord) -> list[str]:
    return [o.get("payload") for o in (row.options or []) if isinstance(o, dict)]


async def apply(session: AsyncSession, *, message_id: int, payload: str,
                user_tg_id: int) -> Outcome:
    """Honour a choice, if it is one that was actually offered.

    `user_tg_id` is the routing key of whoever is claiming it — the caller has
    already established who that is from their session. This checks that the
    message belongs to them and that the payload is one of the message's own,
    then hands off to whichever service owns that prefix.
    """
    row = await session.get(MessageRecord, message_id)
    if row is None or not row.options:
        # Either it never offered anything or somebody has already answered.
        # Both are "no", and saying which would let a stranger learn things
        # about a ticket by asking twice.
        return Outcome(False)

    ticket = await session.get(Ticket, row.ticket_id)
    if ticket is None:
        return Outcome(False)

    owner = (await session.execute(
        select(Ticket.user_id).where(Ticket.id == ticket.id)
    )).scalar_one_or_none()
    if owner is None:
        return Outcome(False)

    from src.database.models import User

    user = await session.get(User, owner)
    if user is None or user.tg_id != user_tg_id:
        # Not this person's conversation. Ticket ids and message ids are small
        # integers, so without this the endpoint is a way to rate and close
        # other people's tickets by counting.
        return Outcome(False)

    if payload not in _offered(row):
        # The decisive check. A payload invented by the caller must not reach
        # a handler, however plausible it looks — the handlers were written
        # when the only source of these strings was a button we drew.
        logger.warning(
            f"Rejected a choice that was never offered on message #{message_id}"
        )
        return Outcome(False)

    outcome = await _dispatch(payload, ticket_id=ticket.id, user_tg_id=user_tg_id)
    if outcome.ok:
        # The question is now closed, for every viewer of this transcript.
        row.options = None
    return outcome


async def _dispatch(payload: str, *, ticket_id: int, user_tg_id: int) -> Outcome:
    from src.services import csat_service

    parts = payload.split(":")

    if parts[0] == csat_service.CSAT_CB_PREFIX and len(parts) == 3:
        try:
            rating = int(parts[2])
        except ValueError:
            return Outcome(False)
        result = await csat_service.save_rating(
            ticket_id=ticket_id, user_tg_id=user_tg_id, rating=rating)
        if result == "ok":
            return Outcome(True, "csat_thanks")
        if result == "already":
            # Not a failure: the first rating stands, and telling somebody
            # their second tap did nothing is more honest than pretending.
            return Outcome(True, "csat_already")
        return Outcome(False)

    if parts[0] == "deflect" and len(parts) == 4:
        from src.ai import deflect

        verdict, _tid, raw_call = parts[1], parts[2], parts[3]
        try:
            ai_call_id = int(raw_call)
        except ValueError:
            return Outcome(False)

        if verdict == "ok":
            await deflect.mark_outcome(ai_call_id, True)
            closed = await deflect.close_ticket_by_ai(ticket_id, user_tg_id)
            return Outcome(True, "deflect_solved_ack", closed=closed)
        if verdict == "no":
            await deflect.mark_outcome(ai_call_id, False)
            return Outcome(True, "deflect_operator_wait")
        return Outcome(False)

    logger.info(f"No handler for a choice payload starting {parts[0]!r}")
    return Outcome(False)
