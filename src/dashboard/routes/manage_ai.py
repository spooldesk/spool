"""Web admin: AI settings — enable/disable, category blacklist, connection status."""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from src.config import load_settings
from src.services import settings_service

router = APIRouter()

# Categories offered as checkboxes for the blacklist — read from the editable
# catalog so the list always matches what the bot actually offers users.
async def _blacklist_categories() -> list[str]:
    from src.database.db import db_session
    from src.services import catalog_service
    async with db_session() as session:
        return [c.name for c in await catalog_service.list_active(session)]


@router.get("/manage/ai", response_class=HTMLResponse)
async def ai_settings_page(request: Request, sent: str | None = None):
    templates = request.app.state.templates
    cfg = load_settings()
    enabled = await settings_service.get_ai_enabled()
    deflection = await settings_service.get_ai_deflection_enabled()
    blacklist = await settings_service.get_ai_blacklist()

    return templates.TemplateResponse(request, "ai_settings.html", {
        "active_page": "ai_settings",
        "enabled": enabled,
        "deflection": deflection,
        "has_key": bool(cfg.gemini_api_key),
        "model": cfg.gemini_model,
        "categories": await _blacklist_categories(),
        "blacklist": blacklist,
        "flash_sent": sent,
    })


@router.post("/manage/ai")
async def ai_settings_save(
    enabled: str = Form(""),
    deflection: str = Form(""),
    blacklist: list[str] = Form(default=[]),
):
    await settings_service.set_ai_enabled(bool(enabled), updated_by=0)
    await settings_service.set_ai_deflection_enabled(bool(deflection), updated_by=0)
    # Store the chosen categories (lowercased fragments) as the blacklist
    await settings_service.set_ai_blacklist([c for c in blacklist if c], updated_by=0)
    return RedirectResponse("/manage/ai?sent=saved", status_code=303)
