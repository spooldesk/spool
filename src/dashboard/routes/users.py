"""Users: list, per-user card, and management actions (block/team/link) from web."""

from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from src.dashboard import queries
from src.database.db import db_session
from src.services import user_service
from src.dashboard.formatting import fmt_seconds as _fmt_seconds
from src.dashboard.i18n import t as tr

router = APIRouter()


@router.get("/users", response_class=HTMLResponse)
async def users_page(
    request: Request,
    team_id: str | None = None,
    sort: str | None = None,
    dir: str | None = None,
):
    from sqlalchemy import select
    from src.database.models import User

    templates = request.app.state.templates

    # "none" is its own filter, not a missing value: people with no team are
    # the ones waiting to be assigned one, and that is a real queue.
    unassigned_only = team_id == "none"
    team_id_int = None
    if not unassigned_only and team_id and team_id.isdigit():
        team_id_int = int(team_id)
    if not unassigned_only and team_id_int is None:
        team_id = ""

    # Unknown column names fall back to the default rather than 400 — a stale
    # bookmark should still show the page.
    sort = sort if sort in queries.USER_SORTS else queries.DEFAULT_USER_SORT
    direction = "asc" if (dir or "").lower() == "asc" else "desc"

    rows = await queries.users_with_ticket_stats(
        team_id=team_id_int, unassigned_only=unassigned_only,
        sort=sort, direction=direction,
    )

    # Search resolves every linked address to the shared customer card.
    all_customers = await queries.users_with_ticket_stats()
    search_users = [{'id': u['user_db_id'], 'label': u['label'], 'search': u['search']} for u in all_customers]

    return templates.TemplateResponse(
        request,
        "users.html",
        {
            "active_page": "users",
            "rows": rows,
            "teams": await queries.list_teams_for_filter(),
            "filters": {"team_id": team_id or ""},
            "sort": sort,
            "dir": direction,
            "unassigned_count": len(
                await queries.users_with_ticket_stats(unassigned_only=True)
            ),
            "search_users_json": search_users,
        },
    )


@router.get("/users/{user_db_id}", response_class=HTMLResponse)
async def user_detail_page(request: Request, user_db_id: int, sent: str | None = None, err: str | None = None):
    templates = request.app.state.templates
    from src.services import customer_profiles
    import secrets
    detail = await queries.user_detail(user_db_id)
    if detail is None:
        raise HTTPException(status_code=404, detail=tr("e_user_not_found"))
    return templates.TemplateResponse(
        request,
        "user_detail.html",
        {
            "active_page": "users",
            "u": detail,
            "contacts": await customer_profiles.details(user_db_id),
            "message_request_key": secrets.token_hex(16),
            "fmt_seconds": _fmt_seconds,
            "flash_sent": sent,
            "flash_err": err,
        },
    )


def _back(uid: int, *, sent: str | None = None, err: str | None = None) -> RedirectResponse:
    q = f"?sent={sent}" if sent else (f"?err={quote(err)}" if err else "")
    return RedirectResponse(f"/users/{uid}{q}", status_code=303)


async def _profile_action(request, user_db_id, action, team_id=0):
    from src.services import customer_profiles
    from src.dashboard.routes.workspace import actor_id
    try:
        await customer_profiles.update_profile(user_db_id, actor_id(request), action=action, team_id=team_id)
    except (ValueError, user_service.UserServiceError) as exc:
        return _back(user_db_id, err=tr(str(exc)))
    return _back(user_db_id, sent={'block':'blocked','unblock':'unblocked','remove':'removed','team':'team'}[action])


@router.post('/users/{user_db_id}/block')
async def user_block(request: Request, user_db_id: int):
    return await _profile_action(request, user_db_id, 'block')


@router.post('/users/{user_db_id}/unblock')
async def user_unblock(request: Request, user_db_id: int):
    return await _profile_action(request, user_db_id, 'unblock')


@router.post('/users/{user_db_id}/remove')
async def user_remove(request: Request, user_db_id: int):
    return await _profile_action(request, user_db_id, 'remove')


@router.post('/users/{user_db_id}/team')
async def user_change_team(request: Request, user_db_id: int, team_id: int = Form(...)):
    return await _profile_action(request, user_db_id, 'team', team_id)


@router.post('/users/{user_db_id}/accounts/link')
async def user_link(request: Request, user_db_id: int, channel: str = Form(...),
                    address: str = Form(...), confirmed: str = Form('')):
    from src.services import customer_profiles
    from src.dashboard.routes.workspace import actor_id
    try:
        await customer_profiles.link(user_db_id, channel, address, actor_id(request), confirmed=confirmed == '1')
    except ValueError as exc:
        return _back(user_db_id, err=tr(str(exc)))
    return _back(user_db_id, sent='linked')


@router.post('/users/{user_db_id}/accounts/{account_id}/unlink')
async def user_unlink(request: Request, user_db_id: int, account_id: int):
    from src.services import customer_profiles
    from src.dashboard.routes.workspace import actor_id
    try:
        await customer_profiles.unlink(user_db_id, account_id, actor_id(request))
    except ValueError as exc:
        return _back(user_db_id, err=tr(str(exc)))
    return _back(user_db_id, sent='unlinked')


@router.post('/users/{user_db_id}/message')
async def user_message(request: Request, user_db_id: int, text: str = Form(...),
                       account_id: int = Form(...), request_key: str = Form(...)):
    from src.services import customer_profiles, outbound
    from src.dashboard.routes.workspace import actor_id
    try:
        ticket_id, job_id = await customer_profiles.send(user_db_id, account_id, text, actor_id(request), request_key=request_key)
    except (ValueError, LookupError) as exc:
        return _back(user_db_id, err=tr(str(exc)))
    await outbound.deliver(job_id)
    return _back(user_db_id, sent='queued')
