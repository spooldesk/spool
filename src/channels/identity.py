"""Who a customer is, once they are not necessarily a Telegram account.

`User.tg_id` was the identity of a customer for the whole life of this
project: unique, indexed, non-null, and read in something like fifty places
as a plain integer. Making it nullable to admit customers who have no
Telegram account would have turned all fifty into places that must remember
to handle None, and the ones that forgot would fail somewhere far from here.

So it keeps its type and its constraint, and changes meaning: it is the
internal routing key, not the Telegram id. Telegram ids are always positive,
so customers from every other channel are allocated a negative one, and the
two ranges can never collide. The address the customer is actually reachable
at lives in `channel_user_id` — a string, because an email address and a
widget session token are not numbers.

Read `tg_id` when you need a stable key. Read `address_of()` when you are
about to send something.
"""

from __future__ import annotations

import logging

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.channels import DEFAULT
from src.database.models import User

logger = logging.getLogger(__name__)


def address_of(user: User) -> str:
    """Where to send this customer's next message.

    Falls back to the routing key for rows written before the channel columns
    existed, where the key and the address were the same number.
    """
    return (user.channel_user_id or "").strip() or str(user.tg_id)


def channel_of(user: User) -> str:
    return (user.channel or DEFAULT).strip().lower() or DEFAULT


def is_telegram(user: User) -> bool:
    return channel_of(user) == DEFAULT


def display_address(user: User) -> str:
    """What to show an operator. Never the synthetic key.

    A negative number in a field labelled "tg_id" reads as corruption. For a
    customer who never had a Telegram account, the honest answer is the
    address they wrote from.
    """
    if is_telegram(user):
        return str(user.tg_id)
    addr = address_of(user)
    # Widget sessions are opaque tokens; the tail is enough to tell two
    # visitors apart without pretending the whole thing means something.
    return addr if len(addr) <= 32 else f"…{addr[-12:]}"


async def allocate_routing_key(session: AsyncSession) -> int:
    """A fresh negative routing key for a customer arriving off-Telegram.

    Derived from the current minimum rather than a counter table: there is
    exactly one writer, the value only has to be unique, and a second table
    would be a migration and a failure mode in exchange for nothing. The
    unique index is the real guard — see `create_customer` for the retry.
    """
    lowest = (await session.execute(select(func.min(User.tg_id)))).scalar()
    return min(-1, (lowest or 0) - 1)


async def create_customer(
    session: AsyncSession,
    *,
    channel: str,
    address: str,
    username: str | None = None,
    language: str | None = None,
    team_id: int | None = None,
) -> User:
    """Register a customer reachable on `channel` at `address`.

    Telegram keeps its own path: there the address *is* the routing key, and
    rewriting that would change the identity of every existing customer.
    """
    channel = (channel or DEFAULT).strip().lower()
    address = (address or "").strip()
    if not address:
        raise ValueError("a customer needs an address on their channel")

    if channel == DEFAULT:
        user = User(
            tg_id=int(address), channel=DEFAULT, channel_user_id=address,
            username=username, language=language, team_id=team_id,
            is_verified=True, is_active=True,
        )
        session.add(user)
        await session.flush()
        return user

    # Retried because the key is derived from a read: two customers arriving
    # in the same instant would compute the same one, and the unique index —
    # not this function — is what guarantees they do not share it.
    for attempt in range(5):
        key = await allocate_routing_key(session)
        user = User(
            tg_id=key - attempt, channel=channel, channel_user_id=address,
            username=username, language=language, team_id=team_id,
            is_verified=True, is_active=True,
        )
        session.add(user)
        try:
            await session.flush()
        except Exception:  # noqa: BLE001 — the DB tells us which failures exist
            await session.rollback()
            continue
        return user

    raise RuntimeError("could not allocate a routing key for a new customer")


async def find_customer(
    session: AsyncSession, *, channel: str, address: str
) -> User | None:
    """The existing customer at this address, if we have met them before."""
    channel = (channel or DEFAULT).strip().lower()
    address = (address or "").strip()

    row = (await session.execute(
        select(User).where(User.channel == channel, User.channel_user_id == address)
    )).scalar_one_or_none()
    if row is not None:
        return row

    # Customers who predate the channel columns have neither set, and their
    # address was always the routing key.
    if channel == DEFAULT and address.lstrip("-").isdigit():
        return (await session.execute(
            select(User).where(User.tg_id == int(address))
        )).scalar_one_or_none()
    return None
