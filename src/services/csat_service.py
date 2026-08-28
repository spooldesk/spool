"""CSAT — user satisfaction rating after ticket close.

One place owns the whole feature: the close-notification (text + 1–5 star
inline keyboard) and the rating write. Every close path (bot admin UI, legacy
/close command, dashboard) calls send_close_notification; the bot's callback
handler calls save_rating. The rating is immutable once set — an honest metric
beats a redraftable one.
"""

from __future__ import annotations

import logging
from datetime import datetime

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select

from src.database.db import db_session
from src.database.models import Ticket
from src.utils.i18n import get_text

from src.channels import get as get_channel  # noqa: E402
from src.channels.base import Option  # noqa: E402

logger = logging.getLogger(__name__)

CSAT_CB_PREFIX = "csat"  # callback_data: "csat:{ticket_id}:{rating}"

STARS = {1: "1 ⭐", 2: "2 ⭐", 3: "3 ⭐", 4: "4 ⭐", 5: "5 ⭐"}


def build_rating_keyboard(ticket_id: int) -> InlineKeyboardMarkup:
    """One row of five rating buttons for this ticket.

    Still here for the Telegram handlers that build a keyboard directly. New
    code should use `rating_options` and let the channel decide how a choice
    is presented.
    """
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text=str(n) + "⭐",
            callback_data=f"{CSAT_CB_PREFIX}:{ticket_id}:{n}",
        )
        for n in range(1, 6)
    ]])


def rating_options(ticket_id: int) -> list[Option]:
    """The same five choices, in a form any channel can render.

    The payload is byte-for-byte what the Telegram callback handler already
    parses, so nothing downstream had to learn a second format.
    """
    return [
        Option(label=f"{n}⭐", payload=f"{CSAT_CB_PREFIX}:{ticket_id}:{n}")
        for n in range(1, 6)
    ]


async def send_close_notification(
    bot: Bot, *, user_tg_id: int, ticket_id: int, lang: str | None,
    channel: str | None = None, address: str | None = None,
) -> None:
    """Tell the customer their ticket is closed and ask how it went.

    One message: closure text, the invite, and the five stars. Never raises —
    a rating request that fails to send must not leave a ticket half-closed.

    `channel`/`address` describe where to send; without them this falls back
    to the Telegram id, which is what every caller inside the bot passes and
    what every ticket predating the channel layer used.
    """
    text = (
        get_text("ticket_closed_user", lang, id=ticket_id)
        + "\n\n"
        + get_text("csat_invite", lang)
    )
    options = rating_options(ticket_id)

    # Where to send, when the caller did not say. Every caller has the
    # customer and none of them passed this, so the fallback was "Telegram,
    # addressed by the routing key" — correct for a Telegram customer, whose
    # key is their chat id, and undeliverable for everyone else.
    if channel is None or address is None:
        resolved_channel, resolved_address = await _reach(user_tg_id)
        channel = channel or resolved_channel
        address = address or resolved_address

    try:
        pipe = get_channel(channel)
    except LookupError:
        logger.error(f"CSAT: no channel {channel!r} for ticket #{ticket_id}")
        return

    # A channel that cannot draw a keyboard still has to keep the question.
    # See the Channel protocol: dropping the options is worse than an ugly
    # message, because what gets dropped is the whole point of sending it.
    body = text if pipe.caps.buttons else _spelled_out(text, options)

    try:
        delivery = await pipe.send_text(
            address or str(user_tg_id), body, options=options,
        )
        if not delivery.ok:
            logger.error(
                f"CSAT: could not ask user {user_tg_id} about ticket #{ticket_id}: "
                f"{delivery.error_key} {delivery.error_detail or ''}"
            )
            return
    except Exception as e:
        logger.error(f"CSAT: failed to notify user {user_tg_id} for ticket #{ticket_id}: {e}")
        return

    await _record(ticket_id, user_tg_id, body)


def _spelled_out(text: str, options) -> str:
    """The choices, written into the message, for channels with no buttons."""
    labels = [o.label for o in (options or []) if o.label]
    return f"{text}\n\n{'  ·  '.join(labels)}" if labels else text


async def _reach(user_tg_id: int) -> tuple[str | None, str | None]:
    """Which pipe this customer is on, and their address on it."""
    from src.channels.identity import address_of, channel_of
    from src.database.models import User

    try:
        async with db_session() as session:
            user = (await session.execute(
                select(User).where(User.tg_id == user_tg_id)
            )).scalar_one_or_none()
            if user is None:
                return None, None
            return channel_of(user), address_of(user)
    except Exception as e:  # noqa: BLE001 — never break a close over this
        logger.warning(f"CSAT: could not resolve where to reach {user_tg_id}: {e}")
        return None, None


async def _record(ticket_id: int, user_tg_id: int, text: str) -> None:
    """Put the closing message in the ticket's own transcript.

    Not bookkeeping. On a channel that *is* a transcript — the widget — this
    is the only way the customer ever sees it, because nothing else writes
    the row. On Telegram it means what an operator reads back matches what
    the customer was actually sent.
    """
    from src.database.models import Message as MessageRecord

    try:
        async with db_session() as session:
            session.add(MessageRecord(
                ticket_id=ticket_id, sender_type="admin", sender_id=0,
                text=text,
            ))
            await session.commit()
    except Exception as e:  # noqa: BLE001 — the customer already has it
        logger.warning(f"CSAT: sent the close notice but did not record it: {e}")


async def save_rating(*, ticket_id: int, user_tg_id: int, rating: int) -> str:
    """Store the user's rating. Returns one of:

    "ok"        — saved
    "already"   — ticket was rated before (first rating stands)
    "not_found" — no such closed ticket belonging to this user, or bad rating
    """
    if rating < 1 or rating > 5:
        return "not_found"
    async with db_session() as session:
        ticket = (await session.execute(
            select(Ticket).where(Ticket.id == ticket_id)
        )).scalar_one_or_none()
        if ticket is None or ticket.status != "closed":
            return "not_found"
        # The rating callback arrives in the user's own DM, but verify ownership
        # anyway — callback_data is client-controlled.
        from src.database.models import User
        owner = await session.get(User, ticket.user_id)
        if owner is None or owner.tg_id != user_tg_id:
            return "not_found"
        if ticket.csat_rating is not None:
            return "already"
        ticket.csat_rating = rating
        ticket.csat_rated_at = datetime.utcnow()
        await session.commit()
    logger.info(f"CSAT: ticket #{ticket_id} rated {rating}/5 by tg_id={user_tg_id}")
    return "ok"
