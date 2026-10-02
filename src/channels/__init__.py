"""The channel registry: which pipes exist, and how to reach one.

Callers ask for a channel by the name stored on the customer's row, never by
importing an adapter. That indirection is the whole point: adding email means
registering one more adapter, not editing the reply path, the CSAT prompt,
the broadcast loop and the AI auto-answer to teach each of them about mail.

Registration happens once per process at startup, because the Telegram
adapter needs a Bot and the two processes build their own. A process that
forgets gets a lazily-constructed one rather than a crash — a missed
registration should not take down a reply that would otherwise have worked.
"""

from __future__ import annotations

import logging

from src.channels.base import Capabilities, Channel, Delivery

logger = logging.getLogger(__name__)

#: The channel a row belongs to when nothing said otherwise. Every customer
#: predating this seam arrived through Telegram, so the default is what makes
#: the migration a no-op rather than a rewrite.
DEFAULT = "telegram"

_registry: dict[str, Channel] = {}


def register(channel: Channel) -> None:
    """Install a channel under its name, replacing whatever was there.

    The deliberate verb. Tests install fakes with it, and a process that knows
    exactly which client it wants uses it too.
    """
    _registry[channel.name] = channel
    logger.info(f"Channel registered: {channel.name}")


def ensure(channel: Channel) -> None:
    """Install a channel only if that name is still free.

    What process startup calls. Startup runs after a test has already put a
    fake in place — the dashboard's does, on every TestClient — and a startup
    hook quietly replacing a fake with a real Telegram client is a test that
    passes by talking to the network.
    """
    if channel.name not in _registry:
        register(channel)


def get(name: str | None) -> Channel:
    """The adapter for a channel name, building Telegram's on demand.

    Raises for an unknown name rather than falling back to Telegram: sending
    a customer's reply down the wrong pipe is worse than not sending it.
    """
    key = (name or DEFAULT).strip().lower()
    if key in _registry:
        return _registry[key]

    if key == DEFAULT:
        from src.channels.telegram import TelegramChannel
        from src.dashboard.bot_client import get_bot

        bot = get_bot()
        if bot is not None:
            register(TelegramChannel(bot))
            return _registry[key]
        # An instance with no Telegram configured has no Telegram channel, and
        # says so the same way it would for any other name it does not know.
        raise LookupError(
            "Telegram is not configured on this instance (BOT_TOKEN is unset)"
        )

    raise LookupError(f"No adapter registered for channel {key!r}")


def available() -> list[str]:
    """Names of channels this process can currently send through."""
    from src.config import load_settings
    return sorted(set(_registry) | ({DEFAULT} if load_settings().has_telegram else set()))


def caps(name: str | None) -> Capabilities:
    """Capabilities without forcing a client to be constructed.

    The dashboard asks this to decide whether to offer "delete for the
    customer" on a message, and it renders pages for tickets on channels that
    this process may never send through.
    """
    key = (name or DEFAULT).strip().lower()
    if key in _registry:
        return _registry[key].caps
    if key == DEFAULT:
        from src.channels.telegram import TelegramChannel
        return TelegramChannel.caps
    return Capabilities()


def reset() -> None:
    """Drop every registration. For tests, which install fakes."""
    _registry.clear()


def unregister(name: str) -> None:
    _registry.pop(name, None)


__all__ = [
    "Capabilities",
    "Channel",
    "DEFAULT",
    "Delivery",
    "available",
    "caps",
    "get",
    "register",
    "reset",
]
