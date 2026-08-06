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

# Fallback list for the tags dropdown, used only when the category catalog is
# empty. Mirrors the defaults seeded in db.py so a fresh install shows the same
# names in both places.
TAG_CATEGORIES = ["Access", "Software", "Hardware", "Network", "Other"]


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


# ==================== Tags (category → admin mentions) ====================


@router.get("/manage/tags", response_class=HTMLResponse)
async def tags_page(request: Request, sent: str | None = None):
    templates = request.app.state.templates
    tags = await settings_service.get_category_admin_tags()
    items = [{"category": k, "tags": v} for k, v in tags.items()]
    # Offer the editable ticket categories plus any already-mapped custom ones.
    # Falls back to a small preset if the catalog is empty.
    async with db_session() as session:
        cat_rows = await catalog_service.list_all(session)
    cats = [c.name for c in cat_rows] or list(TAG_CATEGORIES)
    for k in tags.keys():
        if k not in cats:
            cats.append(k)
    return templates.TemplateResponse(request, "tags_manage.html", {
        "active_page": "tags", "items": items, "categories": cats, "flash_sent": sent,
    })


@router.post("/manage/tags")
async def tags_set(category: str = Form(...), tags: str = Form(...)):
    """Add the given mentions to a category WITHOUT dropping existing ones.

    Tokens already present aren't duplicated; new ones are appended in order.
    """
    category = (category or "").strip()
    new_raw = (tags or "").strip()
    if category and new_raw:
        mapping = await settings_service.get_category_admin_tags()
        existing = (mapping.get(category, "") or "").split()
        result = list(existing)  # preserve order
        for tok in new_raw.split():
            if tok not in result:
                result.append(tok)
        mapping[category] = " ".join(result)
        await settings_service.set_category_admin_tags(mapping, updated_by=0)
    return RedirectResponse("/manage/tags?sent=saved", status_code=303)


@router.post("/manage/tags/delete")
async def tags_delete(category: str = Form(...)):
    mapping = await settings_service.get_category_admin_tags()
    mapping.pop(category, None)
    await settings_service.set_category_admin_tags(mapping, updated_by=0)
    return RedirectResponse("/manage/tags?sent=deleted", status_code=303)


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


@router.get("/manage/settings", response_class=HTMLResponse)
async def settings_page(request: Request, sent: str | None = None):
    templates = request.app.state.templates
    brand_name = await settings_service.get_brand_name()
    rd_enabled, rd_match, rd_target = await settings_service.get_redirect_config()
    # Languages
    from src.utils.i18n import SUPPORTED_LANGUAGES
    enabled_langs = await settings_service.get_enabled_languages()
    default_lang = await settings_service.get_default_language()
    languages = [
        {"code": code, "label": label,
         "enabled": code in enabled_langs, "is_default": code == default_lang}
        for code, label in SUPPORTED_LANGUAGES.items()
    ]
    no_tag = ", ".join(sorted(await settings_service.get_admin_no_tag_usernames()))
    from src.services import sla_service, work_hours_service
    wh = await work_hours_service.get_config()
    from src.dashboard.i18n import SUPPORTED_DASH_LANGUAGES
    dash_lang = await settings_service.get_dash_language()
    return templates.TemplateResponse(request, "settings_manage.html", {
        "dash_lang": dash_lang,
        "dash_languages": SUPPORTED_DASH_LANGUAGES,
        "active_page": "settings",
        "brand_name": brand_name,
        "redirect_enabled": rd_enabled, "redirect_match": rd_match, "redirect_target": rd_target,
        "languages": languages,
        "no_tag_usernames": no_tag,
        "work_hours": wh,
        # The target and the policy sit in one card: they are one
        # decision, and split across two forms people set half of it.
        "sla_minutes": await work_hours_service.get_sla_minutes(),
        "sla_policy": await sla_service.get_policy(),
        "wh_day_labels": work_hours_service.day_labels(),
        # The snippet is built from the request rather than typed in, so the
        # host in it is always the host that served the page. Asking someone
        # to keep the two in agreement is asking for the failure where the
        # bubble appears and nothing behind it works.
        "widget_enabled": await settings_service.get_widget_enabled(),
        "widget_origins": "\n".join(await settings_service.get_widget_origins()),
        "widget_greeting": await settings_service.get_widget_greeting(),
        "widget_snippet": (
            f'<script src="{str(request.base_url).rstrip("/")}/widget/embed.js" async></script>'
        ),
        "flash_sent": sent,
    })


@router.post("/manage/settings/widget")
async def settings_save_widget(
    widget_enabled: str = Form("0"),
    widget_origins: str = Form(""),
    widget_greeting: str = Form(""),
):
    """Turn the public endpoints on or off, and configure what they say.

    Deliberately one form. Enabling the widget without having thought about
    which sites may embed it is the mistake worth making hard, and splitting
    them into two cards is how people end up doing exactly that.
    """
    await settings_service.set_widget_enabled(widget_enabled == "1", updated_by=0)
    await settings_service.set_widget_origins(widget_origins, updated_by=0)
    await settings_service.set_widget_greeting(widget_greeting, updated_by=0)
    return RedirectResponse("/manage/settings?sent=widget", status_code=303)


@router.post("/manage/settings/dash-language")
async def settings_save_dash_language(request: Request, dash_language: str = Form("en")):
    await settings_service.set_dash_language(dash_language, updated_by=0)
    # Apply immediately — refresh the module cache and the Jinja global.
    try:
        from src.dashboard import i18n as dash_i18n
        dash_i18n.set_active_language(dash_language)
        request.app.state.templates.env.globals["T"] = dash_i18n.strings()
    except Exception:
        pass
    return RedirectResponse("/manage/settings?sent=saved", status_code=303)


@router.post("/manage/settings/sla")
async def settings_save_sla(
    sla_minutes: str = Form(""),
    notify: str = Form(""),
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
        notify=bool(notify), return_to_pool=bool(return_to_pool), updated_by=0,
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


@router.post("/manage/settings/redirect")
async def settings_save_redirect(
    redirect_enabled: str = Form("0"),
    redirect_match: str = Form(""),
    redirect_target: str = Form(""),
):
    await settings_service.set_redirect_config(
        enabled=(redirect_enabled == "1"),
        team_match=redirect_match,
        target=redirect_target,
        updated_by=0,
    )
    return RedirectResponse("/manage/settings?sent=saved", status_code=303)


@router.post("/manage/settings/no-tag")
async def settings_save_no_tag(no_tag_usernames: str = Form("")):
    """Save the list of admin usernames that should never be @-tagged in lists."""
    import re
    parts = [p for p in re.split(r"[\s,]+", no_tag_usernames or "") if p]
    await settings_service.set_admin_no_tag_usernames(parts, updated_by=0)
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
