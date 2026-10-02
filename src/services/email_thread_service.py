"""Message-ID links shared by inbound IMAP and outbound SMTP."""

from __future__ import annotations

import hashlib

from sqlalchemy import select

from src.channels.email_parse import normalise_message_id
from src.database.inbound_models import EmailMessageReference


def mailbox_key(settings) -> str:
    identity = [settings.email_imap_host.lower(), str(settings.email_imap_port),
                settings.email_user.lower(), settings.email_address.lower(), "INBOX"]
    return hashlib.sha256("\0".join(identity).encode()).hexdigest()


def reference_key(mailbox: str, address: str, message_id: str) -> str:
    return hashlib.sha256("\0".join((mailbox, address.lower(), normalise_message_id(message_id))).encode()).hexdigest()


async def find(session, mailbox: str, address: str, message_id: str):
    if not normalise_message_id(message_id):
        return None
    return await session.get(EmailMessageReference, reference_key(mailbox, address, message_id))


async def remember(session, *, mailbox: str, address: str, message_id: str,
                   conversation_key: str, ticket_id: int, subject: str = "",
                   message_row_id: int | None = None, direction: str = "inbound") -> None:
    message_id = normalise_message_id(message_id)
    if not message_id:
        return
    key = reference_key(mailbox, address, message_id)
    if await session.get(EmailMessageReference, key) is None:
        session.add(EmailMessageReference(
            key=key, mailbox=mailbox, address=address.lower(), message_id=message_id,
            conversation_key=conversation_key, ticket_id=ticket_id, subject=subject[:998],
            message_row_id=message_row_id, direction=direction,
        ))


async def conversation_for(session, inbound, *, mailbox: str, fallback: str) -> str:
    # An address may claim any References header. A link is honoured only for
    # the same correspondent; another customer's thread cannot be hijacked.
    for message_id in [*inbound.references, inbound.message_id]:
        reference = await find(session, mailbox, inbound.from_address, message_id)
        if reference is not None:
            return reference.conversation_key
    root = next((normalise_message_id(ref) for ref in reversed(inbound.references)
                 if normalise_message_id(ref)), "") or normalise_message_id(inbound.message_id) or fallback
    return "email:" + hashlib.sha256(root.encode()).hexdigest()


async def latest(session, *, mailbox: str, address: str, conversation_key: str):
    return (await session.execute(select(EmailMessageReference).where(
        EmailMessageReference.mailbox == mailbox,
        EmailMessageReference.address == address.lower(),
        EmailMessageReference.conversation_key == conversation_key,
    ).order_by(EmailMessageReference.created_at.desc(), EmailMessageReference.key.desc()).limit(1))).scalar_one_or_none()
