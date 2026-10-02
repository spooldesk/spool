"""Private bounded text attachments. Original uploaded bytes are never persisted."""
from datetime import datetime, timedelta
from pathlib import PurePath
import secrets

from sqlalchemy import delete, func, select

from src.ai.assistant_access import AssistantError, recheck
from src.ai.assistant_history import owned
from src.ai.company_document import DocumentError, extract_document, MAX_UPLOAD_BYTES
from src.ai.scrubber import scrub
from src.database.assistant_models import AssistantUpload as Upload
from src.database.db import db_session
from src.services import secret_store

EXTENSIONS = {".txt", ".md", ".csv", ".json", ".log", ".docx"}


def extract(filename, content):
    filename = PurePath(filename.replace("\\", "/")).name
    if not filename or len(filename) > 180 or any(ord(c) < 32 for c in filename):
        raise AssistantError("assistant_file_format", 400)
    suffix = PurePath(filename).suffix.lower()
    if suffix not in EXTENSIONS:
        raise AssistantError("assistant_file_format", 400)
    try:
        value = extract_document("document.docx" if suffix == ".docx" else "document.txt", content)
    except DocumentError as exc:
        key = "assistant_file_size" if str(exc) in {"document_size", "document_text_size"} else "assistant_file_invalid"
        raise AssistantError(key, 400) from None
    return scrub(filename), scrub(value)


async def save(access, filename, content):
    import asyncio
    await recheck(access)
    filename, value = await asyncio.to_thread(extract, filename, content)
    async with db_session(write=True) as session:
        await session.execute(delete(Upload).where(Upload.conversation_id.is_(None),
            Upload.created_at < datetime.utcnow()-timedelta(days=1)))
        count = (await session.execute(select(func.count()).select_from(Upload)
            .where(Upload.operator_id == access.actor_id))).scalar_one()
        if count >= 300:
            raise AssistantError("assistant_history_full", 409)
        row = Upload(id=secrets.token_hex(16), operator_id=access.actor_id,
                     filename=filename, content=secret_store.encrypt(value), size=len(content))
        session.add(row)
    await recheck(access)
    return {"id":row.id, "filename":filename, "size":len(content), "characters":len(value)}


async def get(access, ident):
    await recheck(access)
    async with db_session() as session:
        row = await session.get(Upload, ident)
        if not row or row.operator_id != access.actor_id:
            raise AssistantError("assistant_file_unavailable", 404)
        if row.conversation_id:
            await owned(session, access, row.conversation_id)
        value = secret_store.decrypt(row.content)
        if value is None:
            raise AssistantError("assistant_file_unavailable", 409)
        return {"filename":row.filename, "text":value}
