"""Safe display metadata, shared by initial HTML and subsequent polls."""
from sqlalchemy import select
from src.database.models import AdminUser, Message
from src.services import message_service
from src.services.conversation_service import can_edit


async def metadata(session, rows, channel: str) -> dict[int, dict]:
    ids = {m.reply_to_id for m in rows if m.reply_to_id}
    parents = {m.id: m for m in (await session.execute(select(Message).where(
        Message.id.in_(ids), Message.is_internal == False,
    ))).scalars().all()} if ids else {}
    operators = {m.operator_id for m in rows if m.operator_id}
    names = {a.id: a.name or a.email.split('@')[0] for a in (await session.execute(
        select(AdminUser).where(AdminUser.id.in_(operators))
    )).scalars().all()} if operators else {}
    out = {}
    for m in rows:
        parent = parents.get(m.reply_to_id)
        reply = None
        if parent and parent.ticket_id == m.ticket_id:
            reply = {"id": parent.id, "text": (parent.text or parent.file_name or '')[:240]
                     if not parent.deleted_at else '', "deleted": bool(parent.deleted_at),
                     "author": parent.sender_label or ('Support' if parent.sender_type == 'admin' else 'Customer')}
        out[m.id] = {"reply": reply, "reply_to_id": m.reply_to_id,
                     "revision": m.revision, "edited_at": m.edited_at.isoformat() if m.edited_at else None,
                     "mutation_status": m.mutation_status, "operator_id": m.operator_id,
                     "operator_name": names.get(m.operator_id, m.sender_label or 'Support'),
                     "can_edit": can_edit(m, channel),
                     "can_delete": message_service.can_delete(m, channel=channel),
                     "can_reply": not m.is_internal and not m.deleted_at and m.delivery_status == 'delivered'}
    return out


def live_message(m, meta: dict, *, sensitive=False) -> dict:
    result = {"id": m.id, "sender_type": m.sender_type, "sender_label": m.sender_label,
            "text": m.text or '', "is_template": m.template_id is not None,
            "is_internal": bool(m.is_internal),
            "deleted_at": m.deleted_at.isoformat() if m.deleted_at else None,
            "has_attachment": bool(m.file_id), "file_name": m.file_name,
            "attachment_url": f"/tickets/attachment/{m.id}" if m.file_id else None,
            "is_image": bool(m.file_id) and ('photo' in (m.file_type or '').lower()
                 or (m.mime_type or '').startswith('image/')
                 or (m.file_name or '').lower().endswith(('.jpg','.jpeg','.png','.webp','.gif'))),
            "created_at": m.created_at.strftime('%m-%d %H:%M') if m.created_at else '',
            **meta}
    if sensitive:
        result.update(text='••••••', attachment_url=None, file_name=None, is_image=False,
                      reply=None, can_edit=False, can_reply=False)
    return result
