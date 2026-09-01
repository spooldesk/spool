"""Web admin: AI settings — enable/disable, category blacklist, connection status."""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from src.ai import registry
from src.services import secret_store, settings_service

router = APIRouter()

# Categories offered as checkboxes for the blacklist — read from the editable
# catalog so the list always matches what the bot actually offers users.
async def _blacklist_categories() -> list[str]:
    from src.database.db import db_session
    from src.services import catalog_service
    async with db_session() as session:
        return [c.name for c in await catalog_service.list_active(session)]


async def _engine_state() -> dict:
    """What the engine picker draws, and why each piece of it exists.

    The key is never sent to the page — only whether there is one, whether it
    came from the environment, and the last four characters, which is what an
    operator with two accounts needs to tell them apart.
    """
    import os

    provider_id = await settings_service.get_ai_provider()
    spec = registry.provider(provider_id)
    stored = await settings_service.ai_key_is_stored(provider_id)
    usable = await settings_service.get_ai_key(provider_id)
    from_env = bool(not stored and usable)

    return {
        "providers": registry.PROVIDERS,
        "provider": provider_id,
        "provider_label": spec.label if spec else provider_id,
        "needs_key": spec.needs_key if spec else True,
        "console_url": spec.console_url if spec else "",
        # An i18n key rather than a sentence: the catalogue holds facts, and
        # the prose a customer reads is translated like everything else here.
        # Only some providers have a note worth making.
        "provider_note_key": f"ai_note_{provider_id}",
        "host": await settings_service.get_ai_host(),
        "host_hint": spec.host_hint if spec else "",
        "model": await settings_service.get_ai_model(),
        # A model an operator typed that this release has never heard of. Not
        # an error — the provider decides — but worth saying out loud, because
        # a typo and a new release look identical until the first call fails.
        "model_is_listed": registry.model(provider_id, await settings_service.get_ai_model()) is not None,
        "key_present": bool(usable),
        "key_from_env": from_env,
        "key_env_var": spec.env_var if spec else "",
        # Stored but unreadable: a database restored without its secrets key.
        # A different problem from having no key, and a different fix.
        "key_unreadable": stored and not usable,
        "key_hint": secret_store.masked(usable) if usable and not from_env else "",
    }


@router.get("/manage/ai", response_class=HTMLResponse)
async def ai_settings_page(request: Request, sent: str | None = None,
                           err: str | None = None):
    templates = request.app.state.templates
    enabled = await settings_service.get_ai_enabled()
    deflection = await settings_service.get_ai_deflection_enabled()
    blacklist = await settings_service.get_ai_blacklist()
    price_in, price_out = await settings_service.get_ai_pricing()

    return templates.TemplateResponse(request, "ai_settings.html", {
        "active_page": "ai_settings",
        "enabled": enabled,
        "deflection": deflection,
        **await _engine_state(),
        "flash_err": err,
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


@router.post("/manage/ai/engine")
async def ai_engine_save(
    provider: str = Form(...),
    model: str = Form(""),
    host: str = Form(""),
    api_key: str = Form(""),
    clear_key: str = Form(""),
):
    """Which LLM answers, as which model, with whose credential.

    An empty api_key means "leave it alone", not "clear it" — the field is
    drawn empty on every load because the key is never sent to the page, and
    a save that wiped it would delete a working credential every time somebody
    changed the model.
    """
    if not registry.is_known(provider):
        return RedirectResponse("/manage/ai?err=provider", status_code=303)

    # Prices follow the model inside set_ai_engine, where the rule holds for
    # every caller rather than only for this one.
    await settings_service.set_ai_engine(
        provider=provider, model=model, host=host, updated_by=0)

    if clear_key:
        await settings_service.set_ai_key(provider, "", updated_by=0)
    elif api_key.strip():
        await settings_service.set_ai_key(provider, api_key, updated_by=0)

    return RedirectResponse("/manage/ai?sent=saved", status_code=303)


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
