"""CSAT — user satisfaction rating after ticket close.

One place owns the whole feature: the close-notification (text + 1–5 star
inline keyboard) and the rating write. Every close path stages the notification
in its closing transaction and delivers after commit; the bot's callback
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
from src.database.models import Ticket, User
from src.utils.i18n import get_text

from src.channels import caps as channel_caps  # noqa: E402
from src.channels.base import Delivery, Option  # noqa: E402

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


async def enqueue_close_notification(session, ticket: Ticket, user: User, *, lang: str | None = None):
    """Stage the one closing notice in the same transaction as ticket.status.

    Its immutable job purpose survives consumption of the rating options.
    No provider call is made here. An unconfigured channel still leaves a
    durable intent whose failed delivery is visible to the operator.
    """
    from src.channels.identity import address_of, channel_of
    from src.services import outbound

    if ticket.status != "closed" or ticket.user_id != user.id:
        raise ValueError("A closing notice needs a closed ticket and its customer")
    ticket_id = ticket.id
    lang = lang or user.language
    channel, address = channel_of(user), address_of(user)
    text = (
        get_text("ticket_closed_user", lang, id=ticket_id)
        + "\n\n"
        + get_text("csat_invite", lang)
    )
    options = rating_options(ticket_id)

    body = text if channel_caps(channel).buttons else _spelled_out(text, options)
    return await outbound.enqueue(
        session, ticket_id=ticket_id, channel=channel, address=address,
        text=body, options=options, purpose="ticket-close", allow_unconfigured=True,
    )


async def send_close_notification(
    bot: Bot, *, user_tg_id: int, ticket_id: int, lang: str | None,
    channel: str | None = None, address: str | None = None, job_id: int | None = None,
) -> Delivery:
    """Deliver an already durable closing notice, or stage one for legacy callers.

    Must be called after the closing transaction commits. Repeated calls reuse
    the same purpose-bound job even after its rating buttons were consumed.
    """
    from src.channels.identity import address_of, channel_of

    from src.services import outbound

    try:
        async with db_session(write=True) as session:
            ticket = await session.get(Ticket, ticket_id)
            user = await session.get(User, ticket.user_id) if ticket else None
            if ticket is None or ticket.status != "closed" or user is None or user.tg_id != user_tg_id:
                return Delivery.failed("e_ticket_not_found_or_closed")
            if ((channel is not None and channel != channel_of(user))
                    or (address is not None and address != address_of(user))):
                return Delivery.failed("e_send_failed")
            job = await enqueue_close_notification(session, ticket, user, lang=lang)
            if job_id is not None and job_id != job.id:
                return Delivery.failed("e_send_failed")
            job_id = job.id
        delivery = await outbound.deliver(job_id)
        if not delivery.ok:
            logger.warning("Closing notice for ticket %s is not delivered: %s", ticket_id, delivery.error_key)
        return delivery
    except Exception:
        logger.exception("Could not deliver closing notice for ticket %s", ticket_id)
        return Delivery.failed("e_send_failed")


def _spelled_out(text: str, options) -> str:
    """The choices, written into the message, for channels with no buttons."""
    labels = [o.label for o in (options or []) if o.label]
    return f"{text}\n\n{'  ·  '.join(labels)}" if labels else text


async def save_rating(*, ticket_id: int, user_tg_id: int, rating: int) -> str:
    """Store the user's rating. Returns one of:

    "ok"        — saved
    "already"   — ticket was rated before (first rating stands)
    "not_found" — no such closed ticket belonging to this user, or bad rating
    """
    if rating < 1 or rating > 5:
        return "not_found"
    async with db_session(write=True) as session:
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
