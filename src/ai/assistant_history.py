"""Operator-owned history. Stored text is encrypted and permissions are rechecked."""
from datetime import datetime, timedelta
import json
import secrets

from sqlalchemy import delete, func, select, update

from src.ai.assistant_access import AssistantError, recheck
from src.ai.scrubber import scrub
from src.database.assistant_models import AssistantConversation as Conversation, AssistantTurn as Turn, AssistantUpload as Upload, AssistantAction
from src.database.db import db_session
from src.database.models import Ticket
from src.services import secret_store


def scope(access):
    return {"grants": access.grants, "blacklist": list(access.blacklist)}


def pack(value):
    return secret_store.encrypt(json.dumps(value, ensure_ascii=False))


def unpack(value):
    raw = secret_store.decrypt(value)
    if raw is None:
        raise AssistantError("assistant_history_unavailable", 409)
    return json.loads(raw)


async def owned(session, access, conversation_id):
    row = await session.get(Conversation, conversation_id)
    if not row or row.operator_id != access.actor_id:
        raise AssistantError("assistant_history_unavailable", 404)
    if row.scope != scope(access):
        raise AssistantError("assistant_history_scope", 403)
    return row


async def tickets_visible(session, access, ticket_ids):
    if not ticket_ids:
        return True
    rows = (await session.execute(select(Ticket.id, Ticket.category).where(Ticket.id.in_(ticket_ids)))).all()
    return (access.can("tickets") and len(rows) == len(set(ticket_ids))
            and all(access.permits_category(c) for _, c in rows))


async def visible_turn(session, access, turn):
    if not await tickets_visible(session, access, turn.ticket_ids):
        return {"id":turn.id, "status":"redacted", "error":"assistant_history_scope"}
    data = unpack(turn.payload)
    if turn.status == "running" and turn.created_at < datetime.utcnow()-timedelta(minutes=3):
        return {"id":turn.id, **data, "status":"failed", "error":"assistant_provider_timeout"}
    result = data.get("result")
    if result and result.get("action"):
        action = await session.get(AssistantAction, result["action"]["id"])
        state = action.status if action else "expired"
        if action and state == "pending" and (action.expires_at < datetime.utcnow() or action.session_version != access.generation):
            state = "expired"
        result["action"]["status"] = state
    return {"id":turn.id, "status":turn.status, **data}


async def list_chats(access):
    await recheck(access)
    async with db_session() as session:
        rows = (await session.execute(select(Conversation).where(Conversation.operator_id == access.actor_id)
            .order_by(Conversation.updated_at.desc()).limit(100))).scalars()
        return [{"id":r.id, "title":r.title, "updated_at":r.updated_at.isoformat()}
                for r in rows if r.scope == scope(access)]


async def get_chat(access, conversation_id):
    await recheck(access)
    async with db_session() as session:
        row = await owned(session, access, conversation_id)
        turns = list((await session.execute(select(Turn).where(Turn.conversation_id == row.id)
            .order_by(Turn.created_at, Turn.id).limit(100))).scalars())
        data = {"id":row.id, "title":row.title,
                "turns":[await visible_turn(session, access, turn) for turn in turns]}
    await recheck(access)
    return data


async def remove_chat(access, conversation_id):
    await recheck(access)
    async with db_session(write=True) as session:
        await owned(session, access, conversation_id)
        # Do not race an in-flight completion (its answer must remain discoverable).
        running = (await session.execute(select(Turn.id).where(Turn.conversation_id == conversation_id,
            Turn.status == "running", Turn.created_at > datetime.utcnow()-timedelta(minutes=3)))).first()
        if running:
            raise AssistantError("assistant_busy", 409)
        for turn in (await session.execute(select(Turn).where(Turn.conversation_id == conversation_id))).scalars():
            action = unpack(turn.payload).get("result", {}).get("action")
            if action:
                await session.execute(update(AssistantAction).where(AssistantAction.id == action["id"],
                    AssistantAction.operator_id == access.actor_id, AssistantAction.status == "pending").values(status="cancelled"))
        await session.execute(delete(Conversation).where(Conversation.id == conversation_id))


async def start(access, request):
    await recheck(access)
    now = datetime.utcnow()
    async with db_session(write=True) as session:
        # A lost first response must not create a second conversation/model charge.
        if request.request_id and not request.conversation_id:
            found = (await session.execute(select(Turn.conversation_id).join(Conversation)
                .where(Turn.request_id == request.request_id, Conversation.operator_id == access.actor_id))).scalar_one_or_none()
            if found:
                request.conversation_id = found
        if request.conversation_id:
            conversation = await owned(session, access, request.conversation_id)
        else:
            count = (await session.execute(select(func.count()).select_from(Conversation)
                .where(Conversation.operator_id == access.actor_id))).scalar_one()
            if count >= 100:
                raise AssistantError("assistant_history_full", 409)
            conversation = Conversation(id=secrets.token_hex(16), operator_id=access.actor_id,
                title=scrub(request.message)[:100], scope=scope(access), created_at=now, updated_at=now)
            session.add(conversation)
            await session.flush()
        request_id = request.request_id or secrets.token_hex(16)
        previous = (await session.execute(select(Turn).where(Turn.conversation_id == conversation.id,
            Turn.request_id == request_id))).scalar_one_or_none()
        if previous:
            data = await visible_turn(session, access, previous)
            if previous.status == "completed" and data.get("result"):
                return {"conversation_id":conversation.id, "turn_id":previous.id, "replay":data["result"]}
            raise AssistantError(data.get("error", "assistant_busy"), 409)
        recent = list((await session.execute(select(Turn).where(Turn.conversation_id == conversation.id)
            .order_by(Turn.created_at.desc(), Turn.id.desc()).limit(100))).scalars())
        if len(recent) >= 100:
            raise AssistantError("assistant_history_full", 409)
        if any(t.status == "running" and t.created_at > now-timedelta(minutes=3) for t in recent):
            raise AssistantError("assistant_busy", 409)
        # Request IDs are globally unique so a lost first response can be replayed.
        # A collision with another chat/operator must not expose it or produce a 500.
        if (await session.execute(select(Turn.id).where(Turn.request_id == request_id))).first():
            raise AssistantError("assistant_busy", 409)
        memory, previous_attachments, dependencies = [], [], set()
        for turn in reversed(recent[:3]):
            data = await visible_turn(session, access, turn)
            if data.get("status") != "completed":
                continue
            memory.append({"question":data.get("question", "")[:2000],
                           "answer":data.get("result", {}).get("answer", "")[:1600]})
            dependencies.update(turn.ticket_ids)
            previous_attachments.extend(a["id"] for a in data.get("attachments", []))
        selected = list(dict.fromkeys(request.attachment_ids or previous_attachments[-3:]))
        documents, attachments = [], []
        remaining = 24000
        for ident in selected:
            upload = await session.get(Upload, ident)
            if (not upload or upload.operator_id != access.actor_id
                    or upload.conversation_id not in {None, conversation.id}):
                raise AssistantError("assistant_file_unavailable", 404)
            raw = secret_store.decrypt(upload.content)
            if raw is None:
                raise AssistantError("assistant_file_unavailable", 409)
            limit = min(remaining, 24000 // max(1, len(selected)))
            documents.append({"filename":upload.filename, "text":raw[:limit],
                              "truncated":len(raw) > limit, "total_characters":len(raw)})
            remaining -= limit
            attachments.append({"id":upload.id, "filename":upload.filename, "size":upload.size})
            upload.conversation_id = conversation.id
        turn = Turn(id=secrets.token_hex(16), conversation_id=conversation.id, request_id=request_id,
                    payload=pack({"question":scrub(request.message), "attachments":attachments}), created_at=now)
        session.add(turn)
        conversation.updated_at = now
    return {"conversation_id":conversation.id, "turn_id":turn.id, "memory":memory,
            "documents":documents, "ticket_ids":sorted(dependencies)}


async def finish(access, state, *, result=None, error=None):
    async with db_session(write=True) as session:
        row = await session.get(Turn, state["turn_id"])
        if not row:
            return
        data = unpack(row.payload)
        if result:
            # Derived answers inherit dependencies from the history sent to the model.
            # Otherwise a follow-up could reveal a ticket after its access was revoked.
            row.ticket_ids = sorted(set(result.pop("_ticket_ids", [])) | set(state.get("ticket_ids", [])))
            if not await tickets_visible(session, access, row.ticket_ids):
                raise AssistantError("assistant_history_scope", 403)
            data["result"] = result
        if error:
            data["error"] = error
        row.payload = pack(data)
        row.status = "failed" if error else "completed"
        conversation = await session.get(Conversation, row.conversation_id)
        if conversation:
            conversation.updated_at = datetime.utcnow()
