"""A Bot instance for the web admin process.

The dashboard runs as a SEPARATE process from the polling bot, but it still
needs to SEND Telegram messages (replying to users, closing tickets, etc).
A bot token can be used to send from any number of processes — only *polling*
must be singular. So the web app constructs its own Bot and uses it purely for
outgoing `send_message` / `send_*` calls. It never polls.

The instance is created lazily and cached for the lifetime of the process.
"""

from __future__ import annotations

import logging
from typing import Optional

from aiogram import Bot

from src.config import load_settings

logger = logging.getLogger(__name__)

_bot: Optional[Bot] = None


def get_bot() -> Optional[Bot]:
    """A process-wide Bot for outgoing sends, or None on an instance with no
    Telegram configured.

    None rather than a raise: every caller here is doing something for the
    operators — mirroring a reply into the admin group, announcing a ticket,
    telling a blocked user — and none of it should take down the request that
    triggered it just because this instance has no Telegram.
    """
    global _bot
    if _bot is None:
        settings = load_settings()
        if not settings.has_telegram:
            return None
        _bot = Bot(token=settings.bot_token)
        logger.info("Web admin Bot instance created (send-only, no polling)")
    return _bot


async def close_bot() -> None:
    """Close the bot's HTTP session — call on app shutdown."""
    global _bot
    if _bot is not None:
        try:
            await _bot.session.close()
        except Exception as e:
            logger.warning(f"Error closing web bot session: {e}")
        _bot = None
