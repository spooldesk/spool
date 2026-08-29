"""Saying something to a customer, and remembering that we did.

Two things that look like one and are not. On Telegram, sending *is* the
delivery and the transcript is a record kept for the operators' benefit; a
message that was sent but not recorded still reached somebody. On a channel
whose transcript is what the customer reads — the widget — the record is the
delivery, and a send without one reached nobody at all.

Both paths that write straight to a customer had the second half missing.
The closing notice did until it was fixed; the AI's self-service answer still
did, which meant a widget visitor got an answer from the assistant that they
could not see. Putting the pair in one function is how that stops being a
thing each caller has to remember separately.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from src.channels import get as get_channel
from src.channels.base import Delivery, Option
from src.database.db import db_session
from src.database.models import Message as MessageRecord
from src.services import choice_service

logger = logging.getLogger(__name__)


async def say(
    *,
    ticket_id: int,
    channel: str | None,
    address: str,
    text: str,
    options: Sequence[Option] | None = None,
) -> Delivery:
    """Send `text` to the customer and put it in the ticket's transcript.

    The order matters: sent first, recorded second. A record without a send
    is a lie an operator will read as a message the customer received, and
    the reverse — sent but not recorded — is at worst a gap in history on the
    channels where sending is separate from showing.

    Options are attached to the recorded row rather than to the send, because
    a channel that draws its own buttons reads them from there. Channels that
    cannot draw them have already had them spelled into the text by whoever
    built it, which is the Channel protocol's rule.
    """
    try:
        pipe = get_channel(channel)
    except LookupError:
        logger.error(f"No channel {channel!r} to answer ticket #{ticket_id} on")
        return Delivery.failed("e_send_failed", f"unknown channel {channel!r}")

    try:
        delivery = await pipe.send_text(address, text, options=options)
    except Exception as e:  # noqa: BLE001 — an adapter may raise anything
        logger.error(f"Could not answer ticket #{ticket_id}: {e}")
        return Delivery.failed("e_send_failed", f"{type(e).__name__}: {e}")

    if not delivery.ok:
        return delivery

    try:
        async with db_session() as session:
            row = MessageRecord(
                ticket_id=ticket_id, sender_type="admin", sender_id=0,
                text=text, channel_ref=delivery.ref,
            )
            session.add(row)
            await session.flush()
            if options and pipe.caps.buttons:
                await choice_service.offer(session, row.id, options)
            await session.commit()
    except Exception as e:  # noqa: BLE001
        # The customer has it. A missing row is a gap in the operator's view,
        # not a failed delivery, and reporting failure would make a caller
        # send it twice.
        logger.warning(f"Answered ticket #{ticket_id} but did not record it: {e}")

    return delivery
