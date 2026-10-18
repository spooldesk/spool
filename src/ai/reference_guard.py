"""Revoke a generated answer when any supplied reference changes."""
import hashlib
import json
from src.database.models import Article, ReplyTemplate


def stamp(row):
    fields = [row.title, row.body, row.category]
    fields += [row.team_id, row.is_archived] if isinstance(row, ReplyTemplate) else [row.is_published]
    return hashlib.sha256(json.dumps(fields, ensure_ascii=False).encode()).hexdigest()


async def current(session, references):
    from src.ai.policy import permitted_sources
    for kind, ident, fingerprint in references or []:
        row = await session.get(ReplyTemplate if kind == "template" else Article, ident)
        if row is None or stamp(row) != fingerprint or not await permitted_sources([row]):
            return False
    return True
