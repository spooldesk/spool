"""Owner-controlled integrations and role catalogue."""
from urllib.parse import quote
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from src.dashboard.auth import current_user
from src.database.db import db_session
from src.database.workspace_models import CustomRole
from src.services import connection_center, integrations, permissions, settings_service

router = APIRouter()


def actor_id(request):
    actor = current_user(request)
    if actor is None:
        raise HTTPException(403, "Create an owner account first")
    return actor.id


@router.get('/manage/integrations', response_class=HTMLResponse)
async def integration_page(request: Request, sent: str = '', err: str = ''):
    center = await connection_center.snapshot()
    states = {c: center['channels'][c] for c in integrations.FIELDS}
    slack_base = center['evidence']['slack']['public_url']
    widget_base = center['evidence']['widget']['public_url']
    return request.app.state.templates.TemplateResponse(request, 'integrations.html', {
        'center': center,
        'active_page': 'integrations', 'channels': states, 'fields': integrations.FIELDS,
        'secret_fields': integrations.SECRETS, 'flash_sent': sent, 'flash_err': err,
        'slack_url': slack_base + '/slack/events' if slack_base else '',
        'slack_interactive_url': slack_base + '/slack/interactive' if slack_base else '',
        'widget_enabled': await settings_service.get_widget_enabled(),
        'widget_origins': '\n'.join(await settings_service.get_widget_origins()),
        'widget_greeting': await settings_service.get_widget_greeting(),
        'widget_ask_identity': await settings_service.get_widget_ask_identity(),
        'widget_secret_present': bool(await settings_service.get_widget_identity_secret()),
        'widget_public_url': center['widget_public_url'],
        'widget_snippet': f'<script src="{widget_base}/widget/embed.js" async></script>' if widget_base else '',
    })


@router.post('/manage/integrations/{channel}')
async def integration_save(request: Request, channel: str):
    form = await request.form()
    try:
        if channel == 'widget':
            await integrations.save_widget(dict(form), actor_id=actor_id(request))
        else:
            await integrations.save(channel, dict(form), enabled=form.get('enabled') == '1', actor_id=actor_id(request))
            from src.services import channel_runtime
            await channel_runtime.refresh()
    except ValueError as exc:
        return RedirectResponse('/manage/integrations?err=' + quote(str(exc)), status_code=303)
    return RedirectResponse('/manage/integrations?sent=saved#' + quote(channel), status_code=303)


@router.post('/manage/integrations/{channel}/check')
async def integration_check(request: Request, channel: str):
    try:
        message = await connection_center.check_connection(channel, actor_id=actor_id(request))
    except ValueError as exc:
        return RedirectResponse('/manage/integrations?err=' + quote(str(exc)) + '#' + quote(channel), status_code=303)
    return RedirectResponse('/manage/integrations?sent=' + quote(message) + '#' + quote(channel), status_code=303)


@router.get('/manage/roles', response_class=HTMLResponse)
async def roles_page(request: Request, sent: str = '', err: str = ''):
    async with db_session() as session:
        roles = list((await session.execute(select(CustomRole).order_by(CustomRole.name))).scalars())
    return request.app.state.templates.TemplateResponse(request, 'roles_manage.html', {
        'active_page': 'roles', 'roles': roles, 'sections': permissions.SECTIONS,
        'flash_sent': sent, 'flash_err': err,
    })


@router.post('/manage/roles/save')
async def role_save(request: Request):
    form = await request.form()
    grants = {s: str(form.get(s, 'none')) for s in permissions.SECTIONS}
    try:
        await permissions.save(actor_id(request), str(form.get('name', '')), grants, str(form.get('key', '')))
    except ValueError as exc:
        return RedirectResponse('/manage/roles?err=' + quote(str(exc)), status_code=303)
    return RedirectResponse('/manage/roles?sent=saved', status_code=303)


@router.post('/manage/roles/{key}/delete')
async def role_delete(request: Request, key: str):
    try:
        await permissions.remove(actor_id(request), key)
    except ValueError as exc:
        return RedirectResponse('/manage/roles?err=' + quote(str(exc)), status_code=303)
    return RedirectResponse('/manage/roles?sent=saved', status_code=303)
