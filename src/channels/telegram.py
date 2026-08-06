"""Telegram, as one channel among others.

This is where every `TelegramForbiddenError` in the codebase is supposed to
end up. Nothing above this file should import an aiogram exception to decide
what an operator gets told — the mapping from "what the Bot API raised" to
"what went wrong" lives here and only here.

The bot object is passed in rather than imported. Two processes construct one
(the poller and the dashboard), and a module-level singleton would quietly
pick whichever imported first.
"""

from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNotFound,
    TelegramRetryAfter,
)
from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup

from collections.abc import Sequence

from src.channels.base import Capabilities, Delivery, Option

logger = logging.getLogger(__name__)

#: Above this, a single row of inline buttons wraps badly on a phone. Five is
#: what a star rating needs, which is the widest thing we offer.
_MAX_PER_ROW = 5


def _keyboard(options: Sequence[Option] | None) -> InlineKeyboardMarkup | None:
    """Choices as inline buttons, or nothing at all when there are none.

    Passing an empty markup instead of None removes any keyboard the customer
    already had on screen, which is not what "this message has no options"
    should mean.
    """
    if not options:
        return None
    rows = [
        [InlineKeyboardButton(text=o.label, callback_data=o.payload)
         for o in options[i:i + _MAX_PER_ROW]]
        for i in range(0, len(options), _MAX_PER_ROW)
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


class TelegramChannel:
    """Sends to a Telegram chat id, given as a string like every address."""

    name = "telegram"
    caps = Capabilities(unsend=True, files=True, buttons=True, typing=True, push=True)

    def __init__(self, bot: Bot) -> None:
        self._bot = bot

    # -- address handling -------------------------------------------------
    #
    # Addresses cross the seam as strings, but the Bot API wants an int and
    # rejects a numeric string in ways that read like an unrelated failure.
    @staticmethod
    def _chat(address: str) -> int:
        return int(address)

    # -- outbound ---------------------------------------------------------

    async def send_text(
        self, address: str, text: str, *, options: Sequence[Option] | None = None
    ) -> Delivery:
        try:
            sent = await self._bot.send_message(
                chat_id=self._chat(address), text=text,
                reply_markup=_keyboard(options),
            )
        except TelegramRetryAfter as e:
            # Flood control. Telegram names the delay it wants; the caller
            # decides whether waiting that long is worth it, which for one
            # reply it usually is not and for a broadcast always is.
            return Delivery.failed("e_send_rate_limited", str(e),
                                   retry_after=float(e.retry_after))
        except TelegramForbiddenError:
            # The customer blocked the bot. Nothing will reach them until they
            # unblock it, so this is worth surfacing rather than retrying.
            return Delivery.failed("e_user_blocked_bot", address, blocked=True)
        except TelegramNotFound:
            return Delivery.failed("e_user_chat_not_found", address, blocked=True)
        except TelegramBadRequest as e:
            return Delivery.failed("e_send_rejected", str(e))
        except (TelegramAPIError, ValueError) as e:
            return Delivery.failed("e_send_failed", f"{type(e).__name__}: {e}")
        return Delivery.sent(sent.message_id)

    async def send_file(
        self, address: str, *, filename: str, content: bytes, caption: str | None = None
    ) -> Delivery:
        try:
            sent = await self._bot.send_document(
                chat_id=self._chat(address),
                document=BufferedInputFile(content, filename=filename),
                caption=caption,
            )
        except TelegramForbiddenError:
            return Delivery.failed("e_user_blocked_bot", address, blocked=True)
        except TelegramNotFound:
            return Delivery.failed("e_user_chat_not_found", address, blocked=True)
        except TelegramBadRequest as e:
            return Delivery.failed("e_send_rejected", str(e))
        except (TelegramAPIError, ValueError) as e:
            return Delivery.failed("e_send_failed", f"{type(e).__name__}: {e}")

        # The file_id is what lets the same upload be mirrored into the admin
        # thread without sending the bytes a second time, so it is worth more
        # here than the message id an unsend would use.
        file_ref = sent.document.file_id if sent.document else None
        d = Delivery.sent(sent.message_id)
        return Delivery(ok=True, ref=d.ref, error_detail=file_ref)

    async def fetch_file(self, ref: str) -> bytes | None:
        """A file_id back into bytes. Two calls, because Telegram says so:
        one to turn the id into a path, one to fetch the path."""
        try:
            described = await self._bot.get_file(ref)
            buffer = await self._bot.download_file(described.file_path)
        except Exception as e:  # noqa: BLE001 — the caller cannot act on which
            logger.warning(f"Could not fetch Telegram file {ref}: {e}")
            return None
        return buffer.read() if hasattr(buffer, "read") else buffer

    async def unsend(self, address: str, ref: str) -> bool:
        try:
            await self._bot.delete_message(
                chat_id=self._chat(address), message_id=int(ref)
            )
        except TelegramBadRequest as e:
            # Already gone, or past Telegram's own window rather than ours.
            # Both are terminal; a retry would fail identically.
            logger.warning(f"delete_message refused for {address}/{ref}: {e}")
            return False
        except Exception as e:  # noqa: BLE001
            logger.error(f"delete_message failed for {address}/{ref}: {e}")
            return False
        return True

    async def typing(self, address: str) -> None:
        try:
            await self._bot.send_chat_action(chat_id=self._chat(address), action="typing")
        except Exception:  # noqa: BLE001
            # A hint that failed to appear is not worth a word to anyone.
            pass
