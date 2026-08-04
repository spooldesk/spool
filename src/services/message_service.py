"""Unsending a message an operator already delivered.

Telegram lets a bot delete its own messages for 48 hours. After that the copy
in the customer's chat is permanent no matter what we do here, so the UI has to
stop offering it rather than fail later and look broken.

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
from aiogram.exceptions import TelegramBadRequest
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from src.database.db import db_session
from src.database.models import Message as MessageRecord, Ticket

logger = logging.getLogger(__name__)

# Telegram's own limit for a bot deleting its own message.
DELETE_WINDOW = timedelta(hours=48)


@dataclass(slots=True)
class DeleteResult:
    ok: bool
    error_key: str = ""
    removed_from_telegram: bool = False


def can_delete(message: MessageRecord | dict, now: datetime | None = None) -> bool:
    """Whether the unsend button should be offered for this message.

    Only operator messages we actually delivered, that we recorded an id for,
    that are not already withdrawn, and that are still inside Telegram's window.
    """
    get = message.get if isinstance(message, dict) else lambda k: getattr(message, k, None)
    if get("sender_type") != "admin" or get("is_internal"):
        return False
    if not get("tg_message_id") or get("deleted_at"):
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
            return DeleteResult(True, "", removed_from_telegram=False)
        if not can_delete(row):
            return DeleteResult(False, "e_delete_window_passed")

        user = row.ticket.user if row.ticket else None
        if user is None or not user.tg_id:
            return DeleteResult(False, "e_no_recipient")
        chat_id, tg_message_id = user.tg_id, row.tg_message_id

    try:
        await bot.delete_message(chat_id=chat_id, message_id=tg_message_id)
    except TelegramBadRequest as e:
        # Already gone, or past the window on Telegram's clock rather than
        # ours. Either way the customer no longer sees it or never will stop
        # seeing it; both are terminal, so say so instead of retrying.
        logger.warning(f"delete_message refused for message #{message_id}: {e}")
        return DeleteResult(False, "e_delete_refused")
    except Exception as e:  # noqa: BLE001
        logger.error(f"delete_message failed for message #{message_id}: {e}")
        return DeleteResult(False, "e_delete_failed")

    async with db_session() as session:
        row = await session.get(MessageRecord, message_id)
        if row is not None:
            row.deleted_at = datetime.utcnow()
            await session.commit()

    logger.info(f"Message #{message_id} withdrawn from chat {chat_id}")
    return DeleteResult(True, "", removed_from_telegram=True)
