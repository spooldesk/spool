"""Short-lived presence shared by dashboard processes through PostgreSQL."""
from datetime import datetime, timedelta
from sqlalchemy import delete, select
from src.database.db import db_session
from src.database.models import ConversationPresence, Ticket


async def heartbeat(ticket_id: int, participant: str, name: str, *, operator_id=None,
                    typing=False, public=True, leaving=False) -> list[dict]:
    now = datetime.utcnow()
    async with db_session(write=True) as session:
        ticket = await session.get(Ticket, ticket_id)
        if ticket is None:
            return []
        await session.execute(delete(ConversationPresence).where(
            ConversationPresence.seen_at < now - timedelta(seconds=20)))
        row = await session.get(ConversationPresence, (ticket_id, participant))
        if leaving:
            if row:
                await session.delete(row)
        else:
            if row is None:
                row = ConversationPresence(ticket_id=ticket_id, participant=participant)
                session.add(row)
            row.operator_id, row.name, row.seen_at = operator_id, name[:120], now
            row.typing_until = now + timedelta(seconds=6) if typing else None
            row.public_typing = bool(public and typing)
        await session.flush()
        rows = (await session.execute(select(ConversationPresence).where(
            ConversationPresence.ticket_id == ticket_id,
            ConversationPresence.participant != participant,
            ConversationPresence.seen_at >= now - timedelta(seconds=15),
        ))).scalars().all()
        # Each browser tab has its own lease. Hide all of the caller's own tabs,
        # and show a colleague only once, preferring an actively composed reply.
        people = {}
        for r in rows:
            if operator_id is not None and r.operator_id == operator_id:
                continue
            active = bool(r.typing_until and r.typing_until > now)
            person = {"id": r.operator_id, "name": r.name,
                      "customer": r.operator_id is None, "typing": active,
                      "replying": bool(active and r.public_typing)}
            key = ("operator", r.operator_id) if r.operator_id is not None else ("visitor", r.participant)
            old = people.get(key)
            if old is None or (person["replying"], person["typing"]) > (old["replying"], old["typing"]):
                people[key] = person
        return sorted(people.values(), key=lambda p: (not p["typing"], p["name"], p["id"] or 0))


async def public_typists(ticket_id: int) -> list[str]:
    now = datetime.utcnow()
    async with db_session() as session:
        return list((await session.execute(select(ConversationPresence.name).distinct().where(
            ConversationPresence.ticket_id == ticket_id,
            ConversationPresence.operator_id.is_not(None),
            ConversationPresence.typing_until > now,
            ConversationPresence.public_typing == True,
        ))).scalars().all())
