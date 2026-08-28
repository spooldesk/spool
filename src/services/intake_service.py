"""One door into the helpdesk, whichever channel knocked on it.

An adapter's job ends at "someone at this address said this". Deciding
whether that starts a ticket or continues one, who they are, and what the
operators get told is the same decision every time, and it is made here.

Note what this does *not* do. It does not reply, greet, ask which category
they meant, or verify anyone — those are conversations, and conversations
differ per channel. Telegram asks a customer to pick a category with a
keyboard; a widget shows a dropdown before the first message; email has
nowhere to ask at all and the operator sorts it out later. Baking any one of
those in here would be baking Telegram in a second time under a new name.

Telegram deliberately still runs its own intake. Its path carries a
verification flow, a category state machine and a forum-topic header built
over two years, and rewriting all of it to prove a point about symmetry would
risk the channel that actually has users to make the new ones look tidier.
What it shares with this one is the schema and the services underneath.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.channels import DEFAULT
from src.channels.identity import create_customer, find_customer
from src.database.models import Message as MessageRecord, Ticket, User

logger = logging.getLogger(__name__)

#: Where a ticket lands when the channel had no way to ask. Empty rather than
#: invented: an operator seeing a blank category knows to set one, while a
#: wrong guess looks like an answer and gets filtered on.
UNSORTED = ""


@dataclass(frozen=True)
class Attachment:
    """A file that arrived with an inbound message.

    `ref` is whatever the channel needs to fetch it again — a Telegram
    file_id, an object key, a URL. Opaque here; only the adapter that produced
    it knows how to turn it back into bytes.
    """

    ref: str
    name: str | None = None
    mime_type: str | None = None
    size: int | None = None
    kind: str | None = None


@dataclass(frozen=True)
class Inbound:
    """What an adapter hands over: an address, and something they said."""

    channel: str
    address: str
    text: str = ""
    display_name: str | None = None
    language: str | None = None
    #: Contact details, where the channel has any. On the widget these come
    #: from a form the visitor filled in or from the site they were logged
    #: into, and those are not the same claim — see `identity_verified`.
    email: str | None = None
    #: The customer's id in the host's own system. Answers the question
    #: support actually has: which of your users is this?
    external_id: str | None = None
    #: True only when the *host's server* signed the identity with a shared
    #: secret. Anything a page passed unsigned is a stranger's typing, no
    #: matter how official it looks, and an operator acting on it — resetting
    #: an account, quoting a balance — is the incident this exists to stop.
    identity_verified: bool = False
    category: str | None = None
    attachment: Attachment | None = None
    #: Who wrote this, when that is not the same as whose conversation it is.
    #: A Slack channel shared with a client's team is one conversation with
    #: several people in it; without this the transcript reads as one person
    #: contradicting themselves. Left empty for one-to-one channels, where the
    #: speaker and the customer are the same and repeating it is noise.
    speaker: str | None = None


@dataclass(frozen=True)
class IntakeResult:
    ticket_id: int
    message_id: int
    #: True when this message opened the ticket rather than continuing one.
    #: The caller uses it to decide between "we've got it" and silence.
    opened_ticket: bool
    #: True when this address had never written before.
    new_customer: bool
    #: Set when nothing was recorded, so the adapter can say why.
    rejected: str | None = None


async def open_ticket_of(session: AsyncSession, user_id: int) -> Ticket | None:
    """The customer's one open ticket, if they have one.

    Ordered newest-first because a database that somehow holds two open
    tickets for one person should append to the live one, not the oldest
    forgotten one. `status` is the lifecycle field the whole system agrees
    on; `stage` is board detail and irrelevant to whether a ticket is open.
    """
    return (await session.execute(
        select(Ticket)
        .where(Ticket.user_id == user_id, Ticket.status == "active")
        .order_by(Ticket.id.desc())
    )).scalars().first()


async def receive(session: AsyncSession, inbound: Inbound) -> IntakeResult:
    """Record an inbound message, opening a ticket if there isn't one.

    The caller commits. Intake is almost never the whole of what a channel
    does with a message — it usually wants to answer in the same breath — and
    committing here would split that into two transactions where a crash
    between them leaves a ticket nobody was told about.
    """
    channel = (inbound.channel or DEFAULT).strip().lower()
    text = (inbound.text or "").strip()
    if not text and inbound.attachment is None:
        return IntakeResult(0, 0, False, False, rejected="empty")

    user = await find_customer(session, channel=channel, address=inbound.address)
    new_customer = user is None
    if user is None:
        user = await create_customer(
            session, channel=channel, address=inbound.address,
            username=inbound.display_name, language=inbound.language,
            email=inbound.email, external_id=inbound.external_id,
            identity_verified=inbound.identity_verified,
        )
    else:
        _adopt_identity(user, inbound)

    if not user.is_active:
        # Blocked customers are refused at the door on every channel. Letting
        # a ticket through on a new one would make blocking a Telegram-only
        # feature by accident.
        return IntakeResult(0, 0, False, new_customer, rejected="blocked")

    ticket = await open_ticket_of(session, user.id)
    opened = ticket is None
    if ticket is None:
        ticket = Ticket(
            user_id=user.id,
            team_id=user.team_id,
            category=inbound.category if inbound.category is not None else UNSORTED,
            status="active",
            stage="new",
            notification_sent=False,
            ticket_code="",
            telegram_id=user.tg_id,
        )
        session.add(ticket)
        await session.flush()
        ticket.ticket_code = f"#{ticket.id}"

    att = inbound.attachment
    record = MessageRecord(
        ticket_id=ticket.id,
        sender_type="user",
        # The routing key, not the address: every existing query joins these
        # to users on a number, and a widget token would not match.
        sender_id=user.tg_id,
        text=text,
        sender_label=inbound.speaker,
        file_id=att.ref if att else None,
        file_type=att.kind if att else None,
        file_name=att.name if att else None,
        file_size=att.size if att else None,
        mime_type=att.mime_type if att else None,
    )
    session.add(record)
    await session.flush()

    logger.info(
        f"Intake[{channel}] {'opened' if opened else 'appended to'} "
        f"ticket #{ticket.id} for user #{user.id}"
    )
    return IntakeResult(
        ticket_id=ticket.id, message_id=record.id,
        opened_ticket=opened, new_customer=new_customer,
    )


def _adopt_identity(user: User, inbound: Inbound) -> None:
    """Update who we think a returning customer is.

    Channels learn identity late: a widget visitor types their email into the
    third message, a mail server puts a name in a header we only parse on the
    reply. So blanks get filled as we learn things.

    Overwriting is a different act, and only a signed identity may do it.
    Otherwise the sequence is: someone writes in as themselves, an operator
    starts helping, and a later unsigned message quietly rewrites the name at
    the top of the ticket to someone else's. Once an identity is proved, no
    unsigned claim can touch it again — every field it set is non-empty, and
    only trust unlocks the overwrite.
    """
    trusted = bool(inbound.identity_verified)
    if inbound.display_name and (trusted or not user.username):
        user.username = inbound.display_name
    if inbound.email and (trusted or not user.email):
        user.email = inbound.email
    if inbound.external_id and (trusted or not user.external_id):
        user.external_id = inbound.external_id
    if trusted and not user.identity_verified:
        user.identity_verified = True


async def conversation(session: AsyncSession, ticket_id: int) -> list[MessageRecord]:
    """The customer-visible transcript of a ticket.

    Internal notes are excluded here rather than at each call site: a channel
    that renders a conversation back to the customer — the widget does, an
    email thread does — must never be the thing that leaks an operator's note
    about them.
    """
    return list((await session.execute(
        select(MessageRecord)
        .where(
            MessageRecord.ticket_id == ticket_id,
            MessageRecord.is_internal == False,  # noqa: E712 — SQL, not Python
            MessageRecord.deleted_at.is_(None),
        )
        .order_by(MessageRecord.id)
        .options(selectinload(MessageRecord.ticket))
    )).scalars().all())
