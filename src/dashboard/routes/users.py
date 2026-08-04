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
    import json as _json
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

    # Full user list for the search box (everyone, not only those with tickets)
    async with db_session() as session:
        users = (await session.execute(select(User).order_by(User.username.asc()))).scalars().all()
        search_users = [
            {"id": u.id,
             "label": (f"@{u.username}" if u.username else f"id {u.tg_id}")}
            for u in users
        ]

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
            "search_users_json": _json.dumps(search_users, ensure_ascii=False),
        },
    )


@router.get("/users/{user_db_id}", response_class=HTMLResponse)
async def user_detail_page(request: Request, user_db_id: int, sent: str | None = None, err: str | None = None):
    templates = request.app.state.templates
    detail = await queries.user_detail(user_db_id)
    if detail is None:
        raise HTTPException(status_code=404, detail=tr("e_user_not_found"))
    return templates.TemplateResponse(
        request,
        "user_detail.html",
        {
            "active_page": "users",
            "u": detail,
            "fmt_seconds": _fmt_seconds,
            "flash_sent": sent,
            "flash_err": err,
        },
    )


def _back(uid: int, *, sent: str | None = None, err: str | None = None) -> RedirectResponse:
    q = f"?sent={sent}" if sent else (f"?err={quote(err)}" if err else "")
    return RedirectResponse(f"/users/{uid}{q}", status_code=303)


@router.post("/users/{user_db_id}/block")
async def user_block(user_db_id: int):
    from src.dashboard.bot_client import get_bot
    from src.utils.i18n import get_text
    try:
        async with db_session() as session:
            tg_id, lang = await user_service.set_active(session, user_db_id, active=False)
            await session.commit()
        try:
            await get_bot().send_message(tg_id, get_text("access_restricted", lang))
        except Exception:
            pass
    except user_service.UserServiceError as e:
        return _back(user_db_id, err=str(e))
    return _back(user_db_id, sent="blocked")


@router.post("/users/{user_db_id}/remove")
async def user_remove(user_db_id: int):
    """Retire a user: close their open tickets, cut account links, revoke access.

    No notification is sent. Blocking tells someone they were blocked; retiring
    is bookkeeping after they have already left, and a message would be noise
    to a person who no longer works there.
    """
    try:
        async with db_session() as session:
            await user_service.remove(session, user_db_id)
            await session.commit()
    except user_service.UserServiceError as e:
        return _back(user_db_id, err=str(e))
    return _back(user_db_id, sent="removed")


@router.post("/users/{user_db_id}/unblock")
async def user_unblock(user_db_id: int):
    from src.dashboard.bot_client import get_bot
    from src.utils.i18n import get_text
    from src.services import settings_service
    try:
        async with db_session() as session:
            tg_id, lang = await user_service.set_active(session, user_db_id, active=True)
            await session.commit()
        try:
            brand = await settings_service.get_brand_name()
            await get_bot().send_message(tg_id, get_text("access_restored", lang, brand=brand))
        except Exception:
            pass
    except user_service.UserServiceError as e:
        return _back(user_db_id, err=str(e))
    return _back(user_db_id, sent="unblocked")


@router.post("/users/{user_db_id}/team")
async def user_change_team(user_db_id: int, team_id: int = Form(...)):
    from src.dashboard.bot_client import get_bot
    try:
        async with db_session() as session:
            res = await user_service.change_team(session, user_db_id, team_id)
            await session.commit()
        if not res.is_change:  # first assignment → notify the user
            try:
                from src.utils.i18n import get_text
                await get_bot().send_message(
                    res.user_tg_id,
                    get_text("account_approved", res.user_language),
                )
            except Exception:
                pass
    except user_service.UserServiceError as e:
        return _back(user_db_id, err=str(e))
    return _back(user_db_id, sent="team")


@router.post("/users/{user_db_id}/link")
async def user_link(user_db_id: int, identifier: str = Form(...)):
    async with db_session() as session:
        res = await user_service.link_account(session, user_db_id, identifier)
        await session.commit()
    if res.status == "error":
        return _back(user_db_id, err=res.message)
    return _back(user_db_id, sent="linked")


@router.post("/users/{user_db_id}/unlink")
async def user_unlink(user_db_id: int):
    try:
        async with db_session() as session:
            await user_service.unlink_account(session, user_db_id)
            await session.commit()
    except user_service.UserServiceError as e:
        return _back(user_db_id, err=str(e))
    return _back(user_db_id, sent="unlinked")


@router.post("/users/{user_db_id}/unlink-personals")
async def user_unlink_personals(user_db_id: int):
    async with db_session() as session:
        await user_service.unlink_personals_of(session, user_db_id)
        await session.commit()
    return _back(user_db_id, sent="unlinked")


@router.post("/users/{user_db_id}/message")
async def user_message(user_db_id: int, text: str = Form(...)):
    """Send a one-off direct message to the user from the web admin (not a ticket)."""
    from sqlalchemy import select
    from src.dashboard.bot_client import get_bot
    from src.database.models import User

    text = (text or "").strip()
    if not text:
        return _back(user_db_id, err=tr("e_empty_message"))

    async with db_session() as session:
        user = await session.get(User, user_db_id)
        if user is None:
            return _back(user_db_id, err=tr("e_user_not_found"))
        tg_id = user.tg_id

    try:
        await get_bot().send_message(tg_id, text)
    except Exception as e:
        return _back(user_db_id, err=tr("e_not_delivered_x", what=type(e).__name__))
    return _back(user_db_id, sent="messaged")
