"""Unsending a message an operator already delivered.

Not every channel can. Telegram lets a bot delete its own messages for 48
hours; an email cannot be recalled at all. Both facts are declared on the
channel's capabilities so the UI stops offering the button rather than failing
after the click, when the operator has already been told it worked.

Deleting is soft on our side. A support transcript that silently loses messages
is worse than one that says a message was withdrawn: the next operator needs to
know something was said and taken back, and the reports counting admin replies
have to keep adding up.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from aiogram import Bot
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from src.channels import caps, get as get_channel
from src.channels.identity import address_of, channel_of
from src.database.db import db_session
from src.database.models import Message as MessageRecord, Ticket

logger = logging.getLogger(__name__)

# Telegram's own limit for a bot deleting its own message.
DELETE_WINDOW = timedelta(hours=48)


@dataclass(slots=True)
class DeleteResult:
    ok: bool
    error_key: str = ""
    removed_from_channel: bool = False


def can_delete(
    message: MessageRecord | dict,
    now: datetime | None = None,
    *,
    channel: str | None = None,
) -> bool:
    """Whether the unsend button should be offered for this message.

    Only operator messages we actually delivered, on a channel that can take
    one back, that we recorded an id for, that are not already withdrawn, and
    that are still inside the window.

    `channel` is passed in rather than read off the message, because it lives
    on the customer and a message row does not carry one. Omitting it assumes
    Telegram, which is what every row predating the channel columns is.
    """
    get = message.get if isinstance(message, dict) else lambda k: getattr(message, k, None)
    if get("sender_type") != "admin" or get("is_internal"):
        return False
    if get("deleted_at"):
        return False
    # Asked per message because the answer is per customer: the same operator,
    # the same minute, can unsend to one person and not to another.
    channel_caps = caps(channel)
    if not channel_caps.unsend:
        return False

    if channel_caps.unsend_by_row:
        # Nothing is sitting in anyone's chat history, so nothing expires and
        # there is no platform id to have recorded. The row is the message.
        return bool(get("id"))

    # Elsewhere we can only address what we wrote down at the time, and only
    # until the platform stops accepting the request.
    # The fallback covers a row written before the backfill, which is a
    # partial upgrade rather than a state anything should be left in.
    if not (get("channel_ref") or get("tg_message_id")):
        return False
    created = get("created_at")
    if not created:
        return False
    return (now or datetime.utcnow()) - created < DELETE_WINDOW


async def delete_for_user(bot: Bot, message_id: int) -> DeleteResult:
    """Withdraw a delivered message from the customer's chat.

    The database is only updated once Telegram confirms, so a failure leaves
    the transcript honest: the customer can still see it, and so can we.
    """
    async with db_session() as session:
        row = (await session.execute(
            select(MessageRecord)
            .where(MessageRecord.id == message_id)
            .options(selectinload(MessageRecord.ticket).selectinload(Ticket.user))
        )).scalar_one_or_none()

        if row is None:
            return DeleteResult(False, "e_message_not_found")
        if row.deleted_at:
            return DeleteResult(True, "", removed_from_channel=False)

        user = row.ticket.user if row.ticket else None
        if user is None or not user.tg_id:
            return DeleteResult(False, "e_no_recipient")
        channel, address = channel_of(user), address_of(user)

        # Two different noes, told apart because the operator can act on one
        # of them: "too late" means next time, "this channel can't" means
        # never, and hearing the first when it was the second wastes an hour.
        if not caps(channel).unsend:
            return DeleteResult(False, "e_delete_unsupported")
        if not can_delete(row, channel=channel):
            return DeleteResult(False, "e_delete_window_passed")
        ref = str(
            row.id if caps(channel).unsend_by_row
            else (row.channel_ref or row.tg_message_id)
        )

    # The adapter decides what "could not be withdrawn" means on its own
    # medium and reports it the same way regardless.
    if not await get_channel(channel).unsend(address, ref):
        return DeleteResult(False, "e_delete_refused")

    async with db_session() as session:
        row = await session.get(MessageRecord, message_id)
        if row is not None:
            row.deleted_at = datetime.utcnow()
            await session.commit()

    logger.info(f"Message #{message_id} withdrawn from {channel}:{address}")
    return DeleteResult(True, "", removed_from_channel=True)
