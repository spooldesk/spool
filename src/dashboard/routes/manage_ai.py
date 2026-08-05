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
    price_in, price_out = await settings_service.get_ai_pricing()

    return templates.TemplateResponse(request, "ai_settings.html", {
        "active_page": "ai_settings",
        "enabled": enabled,
        "deflection": deflection,
        "has_key": bool(cfg.gemini_api_key),
        "model": cfg.gemini_model,
        "categories": await _blacklist_categories(),
        "blacklist": blacklist,
        "flash_sent": sent,
        "shadow_on": await settings_service.get_ai_shadow_mode(),
        # The savings report is a document someone takes to their
        # manager; its assumptions have to be theirs to correct.
        "price_in": price_in,
        "price_out": price_out,
        "assist_minutes": await settings_service.get_ai_assist_minutes(),
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


@router.post("/manage/ai/savings")
async def savings_assumptions_save(
    price_in: float = Form(0.0),
    price_out: float = Form(0.0),
    assist_minutes: float = Form(0.0),
):
    """Negatives are clamped in the service — a report cannot be worth less
    than nothing, and a typed minus should not silently invert a number
    somebody is about to present."""
    await settings_service.set_savings_assumptions(
        price_in=price_in, price_out=price_out,
        assist_minutes=assist_minutes, updated_by=0,
    )
    return RedirectResponse("/manage/ai?sent=saved#savings", status_code=303)


@router.post("/manage/ai/shadow")
async def ai_shadow_save(shadow: str = Form("")):
    """Turning shadow ON is the safe direction, so it needs no ceremony.
    Turning it OFF means the AI starts reaching customers — which is a
    decision made on the evidence page, not here."""
    await settings_service.set_ai_shadow_mode(bool(shadow), updated_by=0)
    return RedirectResponse("/manage/ai?sent=saved#shadow", status_code=303)
