"""Dashboard operator management — list / create / edit / disable / delete.

Per-user accounts that replace the old shared password (Phase 2). Guards against
foot-guns: you can't delete yourself or remove the last remaining account.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from src.dashboard.auth import current_user
from src.services import admin_user_service
from src.dashboard.i18n import t as tr

logger = logging.getLogger(__name__)

router = APIRouter()

_ROLES = ("owner", "admin", "agent")


def _redirect(msg: str = "", err: str = "") -> RedirectResponse:
    q = []
    if msg:
        q.append(f"sent={msg}")
    if err:
        from urllib.parse import quote
        q.append(f"err={quote(err)}")
    suffix = ("?" + "&".join(q)) if q else ""
    return RedirectResponse(f"/manage/admins{suffix}", status_code=303)


@router.get("/manage/admins", response_class=HTMLResponse)
async def admins_list(request: Request, sent: str = "", err: str = ""):
    templates = request.app.state.templates
    admins = await admin_user_service.list_admins()
    me = current_user(request)
    rows = [
        {
            "id": a.id,
            "email": a.email,
            "name": admin_user_service.display_name(a),
            "role": a.role,
            "is_active": a.is_active,
            "last_login_at": a.last_login_at,
            "created_at": a.created_at,
            "is_self": bool(me and me.id == a.id),
        }
        for a in admins
    ]
    return templates.TemplateResponse(
        request,
        "admins_manage.html",
        {
            "active_page": "admins",
            "rows": rows,
            "roles": _ROLES,
            "flash_sent": sent,
            "flash_err": err,
            "bootstrap_mode": me is None,
        },
    )


@router.post("/manage/admins/new")
async def admins_create(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    name: str = Form(""),
    role: str = Form("admin"),
):
    try:
        await admin_user_service.create_admin(
            email=email, password=password, name=name, role=role,
        )
    except ValueError as e:
        return _redirect(err=str(e))
    except Exception as e:  # noqa: BLE001
        logger.exception("admin create failed")
        return _redirect(err=tr("e_create_failed", what=e))
    return _redirect(msg="created")


@router.post("/manage/admins/{admin_id}/password")
async def admins_set_password(request: Request, admin_id: int, password: str = Form(...)):
    try:
        ok = await admin_user_service.set_password(admin_id, password)
    except ValueError as e:
        return _redirect(err=str(e))
    return _redirect(msg="password" if ok else "", err="" if ok else tr("e_user_not_found"))


@router.post("/manage/admins/{admin_id}/role")
async def admins_set_role(request: Request, admin_id: int, role: str = Form(...)):
    await admin_user_service.update_profile(admin_id, role=role)
    return _redirect(msg="saved")


@router.post("/manage/admins/{admin_id}/toggle")
async def admins_toggle(request: Request, admin_id: int, is_active: str = Form(...)):
    me = current_user(request)
    want_active = is_active == "1"
    # Don't let an operator disable their own account.
    if me and me.id == admin_id and not want_active:
        return _redirect(err=tr("e_cannot_disable_self"))
    # Don't disable the last active account (lockout guard).
    if not want_active:
        admins = await admin_user_service.list_admins()
        active_others = [a for a in admins if a.is_active and a.id != admin_id]
        if not active_others:
            return _redirect(err=tr("e_cannot_disable_last"))
    await admin_user_service.set_active(admin_id, want_active)
    return _redirect(msg="saved")


@router.post("/manage/admins/{admin_id}/delete")
async def admins_delete(request: Request, admin_id: int):
    me = current_user(request)
    if me and me.id == admin_id:
        return _redirect(err=tr("e_cannot_delete_self"))
    admins = await admin_user_service.list_admins()
    if len(admins) <= 1:
        return _redirect(err=tr("e_cannot_delete_last"))
    ok = await admin_user_service.delete_admin(admin_id)
    return _redirect(msg="deleted" if ok else "", err="" if ok else tr("e_user_not_found"))
