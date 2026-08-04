"""Web admin: send one message to every user, or to one team.

Deliberately its own page rather than a button on the users list: this is the
only action in the dashboard that reaches hundreds of people at once and cannot
be undone, so it gets a recipient count and an explicit confirmation.
"""

from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from src.dashboard import queries
from src.dashboard.i18n import t as tr
from src.services import broadcast_service

router = APIRouter()


def _parse_int(value: str | None) -> int | None:
    try:
        return int(value) if value else None
    except (TypeError, ValueError):
        return None


@router.get("/broadcast", response_class=HTMLResponse)
async def broadcast_page(request: Request, sent: str | None = None, err: str | None = None):
    templates = request.app.state.templates
    teams = await queries.list_teams_for_filter()
    return templates.TemplateResponse(
        request,
        "broadcast.html",
        {
            "active_page": "broadcast",
            "teams": teams,
            "count_all": await broadcast_service.count_recipients("all"),
            "last": broadcast_service.last_result(),
            "flash_sent": sent,
            "flash_err": err,
        },
    )


@router.get("/broadcast/recipients.json", response_class=JSONResponse)
async def broadcast_recipients(audience: str = "all", team_id: str | None = None):
    """Live recipient count for the form — the operator sees the blast radius
    before they type the message, not after they press send."""
    if audience not in ("all", "team"):
        return JSONResponse({"ok": False}, status_code=400)
    n = await broadcast_service.count_recipients(audience, _parse_int(team_id))
    return JSONResponse({"ok": True, "count": n})


@router.get("/broadcast/status.json", response_class=JSONResponse)
async def broadcast_status():
    r = broadcast_service.last_result()
    if r is None:
        return JSONResponse({"ok": True, "running": False, "result": None})
    return JSONResponse({
        "ok": True,
        "running": r.is_running,
        "result": {
            "total": r.total, "processed": r.processed, "sent": r.sent,
            "blocked": r.blocked, "not_found": r.not_found, "failed": r.failed,
            "failures": r.failures,
        },
    })


@router.post("/broadcast")
async def broadcast_send(
    text: str = Form(""),
    audience: str = Form("all"),
    team_id: str = Form(""),
    confirm: str = Form(""),
):
    from src.dashboard.bot_client import get_bot

    if confirm != "yes":
        return RedirectResponse(f"/broadcast?err={quote(tr('e_broadcast_unconfirmed'))}",
                                status_code=303)

    ok, err_key = await broadcast_service.start(
        get_bot(), text=text, audience=audience, team_id=_parse_int(team_id),
    )
    if not ok:
        return RedirectResponse(f"/broadcast?err={quote(tr(err_key))}", status_code=303)
    return RedirectResponse("/broadcast?sent=started", status_code=303)
