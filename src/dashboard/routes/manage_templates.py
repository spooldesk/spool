"""Web admin: reply-template CRUD. Reuses template_service (same logic the bot uses)."""

from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from src.database.db import db_session
from src.database.models import ReplyTemplate, Team
from src.services import template_service

router = APIRouter()

async def _form_categories(session) -> list[str]:
    """Categories offered in the template form — read from the editable catalog
    so they always match what the bot actually offers users ("" = no category)."""
    from src.services import catalog_service
    return [""] + [c.name for c in await catalog_service.list_active(session)]


async def _all_teams(session):
    return (await session.execute(select(Team).order_by(Team.name.asc()))).scalars().all()


@router.get("/manage/templates", response_class=HTMLResponse)
async def templates_list(request: Request, team: str | None = None, sent: str | None = None):
    templates = request.app.state.templates
    team_id = None
    try:
        team_id = int(team) if team else None
    except ValueError:
        team_id = None

    async with db_session() as session:
        teams = await _all_teams(session)
        stmt = (
            select(ReplyTemplate)
            .where(ReplyTemplate.is_archived.is_(False))
            .options(selectinload(ReplyTemplate.team))
            .order_by(ReplyTemplate.usage_count.desc(), ReplyTemplate.title.asc())
        )
        if team_id:
            stmt = stmt.where(ReplyTemplate.team_id == team_id)
        rows = (await session.execute(stmt)).scalars().all()
        items = [{
            "id": t.id, "title": t.title, "category": t.category or "",
            "team": t.team.name if t.team else "—", "usage": t.usage_count,
            "preview": (t.body or "")[:80],
        } for t in rows]
        teams_list = [(tm.id, tm.name) for tm in teams]

    return templates.TemplateResponse(request, "templates_manage.html", {
        "active_page": "templates",
        "items": items,
        "teams": teams_list,
        "filter_team": team_id,
        "flash_sent": sent,
    })


@router.get("/manage/templates/new", response_class=HTMLResponse)
async def template_new_form(request: Request, err: str | None = None):
    templates = request.app.state.templates
    async with db_session() as session:
        teams = [(t.id, t.name) for t in await _all_teams(session)]
        categories = await _form_categories(session)
    return templates.TemplateResponse(request, "template_form.html", {
        "active_page": "templates",
        "mode": "new",
        "teams": teams,
        "categories": categories,
        "tpl": None,
        "err": err,
    })


@router.post("/manage/templates/new")
async def template_create(
    team_id: int = Form(...),
    title: str = Form(...),
    body: str = Form(...),
    category: str = Form(""),
):
    try:
        async with db_session() as session:
            await template_service.create(
                session, team_id=team_id, title=title, body=body,
                category=(category or None), created_by=0,
            )
            await session.commit()
    except template_service.TemplateError as e:
        return RedirectResponse(f"/manage/templates/new?err={quote(str(e))}", status_code=303)
    return RedirectResponse("/manage/templates?sent=created", status_code=303)


@router.get("/manage/templates/{tpl_id}/edit", response_class=HTMLResponse)
async def template_edit_form(request: Request, tpl_id: int, err: str | None = None):
    templates = request.app.state.templates
    async with db_session() as session:
        t = await template_service.get_by_id(session, tpl_id)
        if t is None:
            return RedirectResponse("/manage/templates", status_code=303)
        teams = [(tm.id, tm.name) for tm in await _all_teams(session)]
        categories = await _form_categories(session)
        tpl = {"id": t.id, "title": t.title, "body": t.body,
               "category": t.category or "", "team_id": t.team_id}
    return templates.TemplateResponse(request, "template_form.html", {
        "active_page": "templates",
        "mode": "edit",
        "teams": teams,
        "categories": categories,
        "tpl": tpl,
        "err": err,
    })


@router.post("/manage/templates/{tpl_id}/edit")
async def template_update(
    tpl_id: int,
    title: str = Form(...),
    body: str = Form(...),
    category: str = Form(""),
):
    try:
        async with db_session() as session:
            await template_service.update(
                session, tpl_id, title=title, body=body, category=(category or ""),
            )
            await session.commit()
    except template_service.TemplateError as e:
        return RedirectResponse(f"/manage/templates/{tpl_id}/edit?err={quote(str(e))}", status_code=303)
    return RedirectResponse("/manage/templates?sent=updated", status_code=303)


@router.post("/manage/templates/{tpl_id}/archive")
async def template_archive(tpl_id: int):
    try:
        async with db_session() as session:
            await template_service.archive(session, tpl_id)
            await session.commit()
    except template_service.TemplateError:
        pass
    return RedirectResponse("/manage/templates?sent=archived", status_code=303)
