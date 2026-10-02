"""Web admin: teams CRUD, category→tags mapping, instance settings.

Reuses team_service + settings_service — the same logic the bot uses, so changes
made here are immediately live in the bot (services read the DB on each use).
"""

from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from src.database.db import db_session
from src.services import catalog_service, close_reason_service, settings_service, team_service

router = APIRouter()

# ==================== Teams ====================


@router.get("/manage/teams", response_class=HTMLResponse)
async def teams_page(request: Request, sent: str | None = None, err: str | None = None):
    templates = request.app.state.templates
    async with db_session() as session:
        rows = await team_service.list_teams(session)
        items = [{"id": r.id, "name": r.name, "users": r.user_count, "templates": r.template_count}
                 for r in rows]
    return templates.TemplateResponse(request, "teams_manage.html", {
        "active_page": "teams", "items": items, "flash_sent": sent, "flash_err": err,
    })


@router.post("/manage/teams/new")
async def team_create(name: str = Form(...)):
    try:
        async with db_session() as session:
            await team_service.create_team(session, name)
            await session.commit()
    except team_service.TeamError as e:
        return RedirectResponse(f"/manage/teams?err={quote(str(e))}", status_code=303)
    return RedirectResponse("/manage/teams?sent=created", status_code=303)


@router.post("/manage/teams/{team_id}/delete")
async def team_delete(team_id: int):
    try:
        async with db_session() as session:
            await team_service.delete_team(session, team_id)
            await session.commit()
    except team_service.TeamError as e:
        return RedirectResponse(f"/manage/teams?err={quote(str(e))}", status_code=303)
    return RedirectResponse("/manage/teams?sent=deleted", status_code=303)


# ==================== Categories (editable ticket catalog) ====================


@router.get("/manage/categories", response_class=HTMLResponse)
async def categories_page(request: Request, sent: str | None = None, err: str | None = None):
    templates = request.app.state.templates
    async with db_session() as session:
        cats = await catalog_service.list_all(session)
        items = [
            {"id": c.id, "name": c.name, "emoji": c.emoji or "",
             "is_active": c.is_active, "sort_order": c.sort_order}
            for c in cats
        ]
    ask_category = await settings_service.get_ask_category()
    return templates.TemplateResponse(request, "categories_manage.html", {
        "active_page": "categories", "items": items,
        "ask_category": ask_category, "flash_sent": sent, "flash_err": err,
    })


@router.post("/manage/categories/ask")
async def categories_set_ask(ask_category: str = Form("0")):
    """Toggle whether the bot asks the user to pick a category up front."""
    await settings_service.set_ask_category(ask_category == "1", updated_by=0)
    return RedirectResponse("/manage/categories?sent=saved", status_code=303)


@router.post("/manage/categories/new")
async def category_create(name: str = Form(...), emoji: str = Form("")):
    try:
        async with db_session() as session:
            await catalog_service.create(session, name=name, emoji=emoji)
            await session.commit()
    except catalog_service.CatalogError as e:
        return RedirectResponse(f"/manage/categories?err={quote(str(e))}", status_code=303)
    return RedirectResponse("/manage/categories?sent=created", status_code=303)


@router.post("/manage/categories/{category_id}/edit")
async def category_edit(category_id: int, name: str = Form(...), emoji: str = Form("")):
    try:
        async with db_session() as session:
            await catalog_service.update(session, category_id, name=name, emoji=emoji)
            await session.commit()
    except catalog_service.CatalogError as e:
        return RedirectResponse(f"/manage/categories?err={quote(str(e))}", status_code=303)
    return RedirectResponse("/manage/categories?sent=saved", status_code=303)


@router.post("/manage/categories/{category_id}/toggle")
async def category_toggle(category_id: int, is_active: str = Form("0")):
    try:
        async with db_session() as session:
            await catalog_service.update(session, category_id, is_active=(is_active == "1"))
            await session.commit()
    except catalog_service.CatalogError as e:
        return RedirectResponse(f"/manage/categories?err={quote(str(e))}", status_code=303)
    return RedirectResponse("/manage/categories?sent=saved", status_code=303)


@router.post("/manage/categories/{category_id}/move")
async def category_move(category_id: int, direction: str = Form(...)):
    dir_val = -1 if direction == "up" else 1
    async with db_session() as session:
        await catalog_service.move(session, category_id, dir_val)
        await session.commit()
    return RedirectResponse("/manage/categories?sent=saved", status_code=303)


@router.post("/manage/categories/{category_id}/delete")
async def category_delete(category_id: int):
    try:
        async with db_session() as session:
            await catalog_service.delete(session, category_id)
            await session.commit()
    except catalog_service.CatalogError as e:
        return RedirectResponse(f"/manage/categories?err={quote(str(e))}", status_code=303)
    return RedirectResponse("/manage/categories?sent=deleted", status_code=303)


# ==================== Close reasons (editable closure catalog) ====================


@router.get("/manage/close-reasons", response_class=HTMLResponse)
async def close_reasons_page(request: Request, sent: str | None = None, err: str | None = None):
    templates = request.app.state.templates
    async with db_session() as session:
        rows = await close_reason_service.list_all(session)
        items = [
            {"id": r.id, "name": r.name, "emoji": r.emoji or "",
             "is_active": r.is_active, "sort_order": r.sort_order}
            for r in rows
        ]
    return templates.TemplateResponse(request, "close_reasons_manage.html", {
        "active_page": "close_reasons", "items": items, "flash_sent": sent, "flash_err": err,
    })


@router.post("/manage/close-reasons/new")
async def close_reason_create(name: str = Form(...), emoji: str = Form("")):
    try:
        async with db_session() as session:
            await close_reason_service.create(session, name=name, emoji=emoji)
            await session.commit()
    except close_reason_service.CloseReasonError as e:
        return RedirectResponse(f"/manage/close-reasons?err={quote(str(e))}", status_code=303)
    return RedirectResponse("/manage/close-reasons?sent=created", status_code=303)


@router.post("/manage/close-reasons/{reason_id}/edit")
async def close_reason_edit(reason_id: int, name: str = Form(...), emoji: str = Form("")):
    try:
        async with db_session() as session:
            await close_reason_service.update(session, reason_id, name=name, emoji=emoji)
            await session.commit()
    except close_reason_service.CloseReasonError as e:
        return RedirectResponse(f"/manage/close-reasons?err={quote(str(e))}", status_code=303)
    return RedirectResponse("/manage/close-reasons?sent=saved", status_code=303)


@router.post("/manage/close-reasons/{reason_id}/toggle")
async def close_reason_toggle(reason_id: int, is_active: str = Form("0")):
    try:
        async with db_session() as session:
            await close_reason_service.update(session, reason_id, is_active=(is_active == "1"))
            await session.commit()
    except close_reason_service.CloseReasonError as e:
        return RedirectResponse(f"/manage/close-reasons?err={quote(str(e))}", status_code=303)
    return RedirectResponse("/manage/close-reasons?sent=saved", status_code=303)


@router.post("/manage/close-reasons/{reason_id}/move")
async def close_reason_move(reason_id: int, direction: str = Form(...)):
    dir_val = -1 if direction == "up" else 1
    async with db_session() as session:
        await close_reason_service.move(session, reason_id, dir_val)
        await session.commit()
    return RedirectResponse("/manage/close-reasons?sent=saved", status_code=303)


@router.post("/manage/close-reasons/{reason_id}/delete")
async def close_reason_delete(reason_id: int):
    try:
        async with db_session() as session:
            await close_reason_service.delete(session, reason_id)
            await session.commit()
    except close_reason_service.CloseReasonError as e:
        return RedirectResponse(f"/manage/close-reasons?err={quote(str(e))}", status_code=303)
    return RedirectResponse("/manage/close-reasons?sent=deleted", status_code=303)


# ==================== Settings ====================


from src.channels import slack as slack_channel  # noqa: E402


@router.get("/manage/settings", response_class=HTMLResponse)
async def settings_page(request: Request, sent: str | None = None):
    templates = request.app.state.templates
    brand_name = await settings_service.get_brand_name()
    # Languages
    from src.utils.i18n import SUPPORTED_LANGUAGES
    enabled_langs = await settings_service.get_enabled_languages()
    default_lang = await settings_service.get_default_language()
    languages = [
        {"code": code, "label": label,
         "enabled": code in enabled_langs, "is_default": code == default_lang}
        for code, label in SUPPORTED_LANGUAGES.items()
    ]
    from src.services import sla_service, work_hours_service
    wh = await work_hours_service.get_config()
    return templates.TemplateResponse(request, "settings_manage.html", {
        "active_page": "settings",
        "brand_name": brand_name,
        "languages": languages,
        "work_hours": wh,
        # The target and the policy sit in one card: they are one
        # decision, and split across two forms people set half of it.
        "sla_minutes": await work_hours_service.get_sla_minutes(),
        "sla_policy": await sla_service.get_policy(),
        "wh_day_labels": work_hours_service.day_labels(),
        "flash_sent": sent,
    })


@router.post("/manage/settings/widget")
async def settings_save_widget(request: Request):
    from src.dashboard.routes.workspace import integration_save
    return await integration_save(request, 'widget')


@router.post("/manage/settings/sla")
async def settings_save_sla(
    sla_minutes: str = Form(""),
    return_to_pool: str = Form(""),
):
    """The target and what to do when it is missed, saved together — they are
    one decision and splitting them across two forms invites half of it."""
    from src.services import sla_service, work_hours_service

    try:
        minutes = int(sla_minutes)
    except (TypeError, ValueError):
        minutes = work_hours_service.DEFAULT_SLA_MINUTES
    await settings_service.set_value(
        work_hours_service.KEY_SLA_MINUTES,
        str(max(1, minutes)), updated_by=0,
    )
    await sla_service.set_policy(
        notify=False, return_to_pool=bool(return_to_pool), updated_by=0,
    )
    return RedirectResponse("/manage/settings?sent=saved", status_code=303)


@router.post("/manage/settings/work-hours")
async def settings_save_work_hours(
    wh_enabled: str = Form(""),
    wh_days: list[str] = Form(default=[]),
    wh_start: str = Form("09:00"),
    wh_end: str = Form("18:00"),
    wh_tz: str = Form(""),
    wh_message: str = Form(""),
):
    from src.services import work_hours_service
    await work_hours_service.set_config(
        enabled=bool(wh_enabled),
        days=[int(d) for d in wh_days if d.isdigit()],
        start=wh_start,
        end=wh_end,
        tz=wh_tz,
        message=wh_message,
        updated_by=0,
    )
    return RedirectResponse("/manage/settings?sent=saved", status_code=303)


@router.post("/manage/settings/brand")
async def settings_save_brand(request: Request, brand_name: str = Form(...)):
    await settings_service.set_brand_name(brand_name, updated_by=0)
    # Keep the Jinja2 global in sync so the sidebar updates without restart.
    try:
        request.app.state.templates.env.globals["g_brand_name"] = brand_name
    except Exception:
        pass
    return RedirectResponse("/manage/settings?sent=saved", status_code=303)


@router.post("/manage/settings/languages")
async def settings_save_languages(
    languages: list[str] = Form(default=[]),
    default_language: str = Form(""),
):
    """Save the offered languages + the fallback language."""
    await settings_service.set_enabled_languages(languages, updated_by=0)
    # Ensure the default is among the enabled set (service guards this on read too)
    enabled = await settings_service.get_enabled_languages()
    if default_language not in enabled:
        default_language = enabled[0]
    await settings_service.set_default_language(default_language, updated_by=0)
    # Apply immediately to this (dashboard) process
    from src.utils.i18n import set_runtime_default_language
    set_runtime_default_language(default_language)
    return RedirectResponse("/manage/settings?sent=saved", status_code=303)
