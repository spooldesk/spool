"""Apply authenticated native edits/deletions to the matching channel transcript."""
from datetime import datetime
from sqlalchemy import select
from src.database.models import Message, MessageRevision, Ticket, User


async def update_customer_message(session, *, channel: str, address: str, ref: str,
                                  text: str | None = None, deleted=False) -> int | None:
    from src.database.db import acquire_write_lock
    await acquire_write_lock(session)
    rows = (await session.execute(select(Message).join(Ticket, Ticket.id == Message.ticket_id)
        .join(User, User.id == Ticket.user_id).where(
            User.channel == channel, User.channel_user_id == address,
            Message.channel_ref == ref, Message.sender_type == 'user',
            Message.is_internal == False,
        ).order_by(Message.id))).scalars().all()
    # Legacy Telegram rows store their numeric address only in tg_id.
    if not rows and channel == 'telegram' and address.lstrip('-').isdigit():
        rows = (await session.execute(select(Message).join(Ticket, Ticket.id == Message.ticket_id)
            .join(User, User.id == Ticket.user_id).where(User.tg_id == int(address),
                User.channel == 'telegram', Message.channel_ref == ref,
                Message.sender_type == 'user', Message.is_internal == False,
            ).order_by(Message.id))).scalars().all()
    for i, row in enumerate(rows):
        if row.deleted_at or (not deleted and (i > 0 or text is None or row.text == text)):
            continue
        session.add(MessageRevision(message_id=row.id, kind='inbound_delete' if deleted else 'inbound_edit',
                    before_text=row.text or '', after_text='' if deleted else text, status='applied'))
        row.revision += 1
        if deleted:
            row.deleted_at = datetime.utcnow()
        else:
            row.text, row.edited_at = text, datetime.utcnow()
    return rows[0].id if rows else None
