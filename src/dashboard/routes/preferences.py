"""One operator's own settings.

Deliberately not part of `/manage/settings`, which is admin-and-up and
configures the instance. Nothing here affects anybody else, so nothing here
needs a role: an agent may choose their own language and their own shortcuts.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from src.dashboard.auth import current_user
from src.services import preferences_service

logger = logging.getLogger(__name__)

router = APIRouter()


def _operator_id(request: Request) -> int | None:
    me = current_user(request)
    return me.id if me else None


@router.get("/me", response_class=HTMLResponse)
async def preferences_page(request: Request, sent: str | None = None):
    from src.dashboard.i18n import SUPPORTED_DASH_LANGUAGES
    from src.services import settings_service

    templates = request.app.state.templates
    prefs = await preferences_service.for_operator(_operator_id(request))
    return templates.TemplateResponse(request, "preferences.html", {
        "active_page": "me",
        "prefs": prefs,
        "languages": SUPPORTED_DASH_LANGUAGES,
        # Shown next to "follow the instance", so the choice is not a guess.
        "instance_language": SUPPORTED_DASH_LANGUAGES.get(
            await settings_service.get_dash_language(), "English"),
        "themes": preferences_service.THEMES,
        "densities": preferences_service.DENSITIES,
        "landings": preferences_service.LANDINGS,
        "max_signature": preferences_service.MAX_SIGNATURE,
        # Whoever is on the shared bootstrap password has no row to save
        # against. Saying so beats a form that quietly forgets.
        "anonymous": _operator_id(request) is None,
        "flash_sent": sent,
    })


@router.post("/me")
async def preferences_save(
    request: Request,
    language: str = Form(""),
    theme: str = Form("system"),
    density: str = Form("comfortable"),
    landing: str = Form("/tickets"),
    signature: str = Form(""),
    # Unchecked boxes send nothing at all, which is why the whole set is
    # written on every save rather than merged into what was there.
    queue_advance: str = Form(""),
    notify_new: str = Form(""),
    hotkeys: str = Form(""),
):
    operator_id = _operator_id(request)
    if operator_id is None:
        return RedirectResponse("/me?sent=anonymous", status_code=303)

    await preferences_service.save(operator_id, {
        "language": language,
        "theme": theme,
        "density": density,
        "landing": landing,
        "signature": signature,
        "queue_advance": queue_advance == "1",
        "notify_new": notify_new == "1",
        "hotkeys": hotkeys == "1",
    })
    return RedirectResponse("/me?sent=saved", status_code=303)
