"""Authenticated assistant chat and separate, CSRF-protected confirmations."""
from fastapi import APIRouter, Form, HTTPException, Request, Query
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import ValidationError

from src.ai import assistant, assistant_history, assistant_files
from src.ai.assistant_access import AssistantError, authorize, enabled, recheck
from src.ai.assistant_actions import confirm
from src.ai.assistant_schema import ChatRequest
from src.dashboard.auth import current_user
from src.dashboard.i18n import active_language, strings, t
from src.services import settings_service

router = APIRouter()


def actor(request):
    user = current_user(request)
    if user is None:
        raise HTTPException(403, detail=t("assistant_forbidden"))
    return user


def failed(exc):
    return JSONResponse({"ok": False, "error": t(exc.code), "code": exc.code}, status_code=exc.status)


@router.get("/assistant", response_class=HTMLResponse)
async def assistant_page(request: Request, context: str = ""):
    actor(request)
    return request.app.state.templates.TemplateResponse(request, "assistant.html", {
        "active_page": "assistant", "enabled": await enabled(), "context_path": context[:200],
        "model": await settings_service.get_ai_model(),
        "assistant_strings": {k: v for k, v in strings().items() if k.startswith("assistant_") or k.startswith("stage_")},
    })


@router.post("/assistant/chat")
async def assistant_chat(request: Request):
    user = actor(request)
    # JSON is small and fixed-shape, never a bulk transcript/file upload.
    raw = bytearray()
    async for part in request.stream():
        raw.extend(part)
        if len(raw) > 32000:
            return failed(AssistantError("assistant_context_large", 413))
    try:
        payload = ChatRequest.model_validate_json(bytes(raw))
    except (ValidationError, ValueError):
        return failed(AssistantError("assistant_invalid_context", 400))
    state = None
    try:
        access = await authorize(user.id, user.session_version)
        state = await assistant_history.start(access, payload)
        if "replay" in state:
            result = state["replay"]
        else:
            # Stored context is fetched server-side; caller-supplied old questions are not history.
            payload.previous_questions = []
            result = await assistant.chat(user.id, user.session_version, payload, active_language(),
                memory=state["memory"], documents=state["documents"])
            await assistant_history.finish(access, state, result=result)
        result.pop("_ticket_ids", None)
        result.update(conversation_id=state["conversation_id"], turn_id=state["turn_id"])
        if result.get('action_error'):
            result['action_error'] = t(result['action_error'])
        await recheck(access)
        return JSONResponse(result)
    except AssistantError as exc:
        if state and "replay" not in state:
            await assistant_history.finish(access, state, error=exc.code)
        return JSONResponse({"ok":False, "error":t(exc.code), "code":exc.code,
            "conversation_id":state["conversation_id"] if state else payload.conversation_id}, status_code=exc.status)


@router.get("/assistant/conversations")
async def assistant_conversations(request: Request):
    user = actor(request)
    try:
        access = await authorize(user.id, user.session_version)
        return JSONResponse({"conversations":await assistant_history.list_chats(access)})
    except AssistantError as exc:
        return failed(exc)


@router.get("/assistant/conversations/{conversation_id}")
async def assistant_conversation(request: Request, conversation_id: str):
    user = actor(request)
    try:
        access = await authorize(user.id, user.session_version)
        return JSONResponse(await assistant_history.get_chat(access, conversation_id))
    except AssistantError as exc:
        return failed(exc)


@router.delete("/assistant/conversations/{conversation_id}")
async def assistant_conversation_delete(request: Request, conversation_id: str):
    user = actor(request)
    try:
        access = await authorize(user.id, user.session_version)
        await assistant_history.remove_chat(access, conversation_id)
        return JSONResponse({"ok":True})
    except AssistantError as exc:
        return failed(exc)


@router.post("/assistant/uploads")
async def assistant_upload(request: Request, filename: str = Query(..., max_length=500)):
    user = actor(request)
    try:
        access = await authorize(user.id, user.session_version)
        content = bytearray()
        async for chunk in request.stream():
            content.extend(chunk)
            if len(content) > assistant_files.MAX_UPLOAD_BYTES:
                raise AssistantError("assistant_file_size", 413)
        return JSONResponse(await assistant_files.save(access, filename, bytes(content)))
    except AssistantError as exc:
        return failed(exc)


@router.get("/assistant/uploads/{upload_id}")
async def assistant_upload_preview(request: Request, upload_id: str):
    user = actor(request)
    try:
        access = await authorize(user.id, user.session_version)
        return JSONResponse(await assistant_files.get(access, upload_id))
    except AssistantError as exc:
        return failed(exc)


@router.post("/assistant/actions/{action_id}")
async def assistant_confirm(request: Request, action_id: str, decision: str = Form(...)):
    user = actor(request)
    if len(action_id) != 32 or decision not in {"confirm", "cancel"}:
        return failed(AssistantError("assistant_invalid_context", 400))
    try:
        access = await authorize(user.id, user.session_version)
        return JSONResponse(await confirm(access, action_id, cancel=decision == "cancel"))
    except AssistantError as exc:
        return failed(exc)
