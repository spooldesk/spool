"""FastAPI app for the dashboard.

Launch:
    uvicorn src.dashboard.app:app --port 8000 --reload
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from src.dashboard.auth import (
    SESSION_COOKIE,
    SESSION_MAX_AGE_SEC,
    _BOOTSTRAP_UID,
    clear_login_failures,
    current_user,
    is_valid_session,
    login_rate_limited,
    make_session_cookie,
    record_login_failure,
    require_auth,
    set_secret_key,
    verify_shared_password,
)
from src.dashboard.csrf import CSRFMiddleware, csrf_context, csrf_protect
from src.dashboard.routes import admin_users as admin_users_routes
from src.dashboard.routes import ai as ai_routes
from src.dashboard.routes import broadcast as broadcast_routes
from src.dashboard.routes import manage_ai as manage_ai_routes
from src.dashboard.routes import manage_settings as manage_settings_routes
from src.dashboard.routes import manage_templates as manage_templates_routes
from src.dashboard.routes import overview as overview_routes
from src.dashboard.routes import reports as reports_routes
from src.dashboard.routes import templates_page as templates_routes
from src.dashboard.routes import tickets as tickets_routes
from src.dashboard.routes import users as users_routes
from src.utils.logger import setup_logging

logger = logging.getLogger(__name__)

# Initialise logging once on import — same config the bot uses, but separate file
setup_logging(log_level="INFO", log_file="logs/dashboard.log")

_PKG_DIR = Path(__file__).parent


def _current_user_context(request: Request) -> dict:
    """Expose the logged-in operator to every template (footer, etc.)."""
    return {"current_user": current_user(request)}


TEMPLATES = Jinja2Templates(
    directory=str(_PKG_DIR / "templates"),
    context_processors=[csrf_context, _current_user_context],
)

# Cache-busting version for static CSS: browsers cache /static/*.css heuristically,
# so a deploy that changes styles would otherwise leave users on stale CSS.
try:
    TEMPLATES.env.globals["g_asset_v"] = str(int(max(
        (_PKG_DIR / "static" / "tw.css").stat().st_mtime,
        (_PKG_DIR / "static" / "app.css").stat().st_mtime,
    )))
except Exception:
    TEMPLATES.env.globals["g_asset_v"] = "1"

app = FastAPI(title="Spool", docs_url=None, redoc_url=None)
# CSRF: issues the token cookie and exposes it to templates. Validation happens
# per-route via the csrf_protect dependency on authenticated routers + /login.
app.add_middleware(CSRFMiddleware)


@app.middleware("http")
async def _no_store(request: Request, call_next):
    """Keep authenticated pages out of the browser cache.

    Without this, logging out and pressing Back re-renders the last page from
    the back-forward cache — session cookie gone, ticket conversation still on
    screen for whoever uses the machine next. It also makes /login behave: the
    browser re-requests it instead of serving a stale form to somebody who is
    already signed in, which is what made Back look like it "jumped home".

    Static assets are exempt; they are versioned and safe to cache.
    """
    response = await call_next(request)
    if not request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        response.headers["Pragma"] = "no-cache"
    return response


app.mount("/static", StaticFiles(directory=str(_PKG_DIR / "static")), name="static")

# Expose templates to route modules so they can render with the same setup
app.state.templates = TEMPLATES


# ==================== Public routes (no auth) ====================


@app.get("/login", response_class=HTMLResponse)
async def login_form(request: Request, next: str = "/", error: str | None = None):
    # If already logged in, skip the form
    if is_valid_session(request.cookies.get(SESSION_COOKIE)):
        return RedirectResponse(url=next or "/", status_code=status.HTTP_302_FOUND)
    return TEMPLATES.TemplateResponse(
        request,
        "login.html",
        {"next": next, "error": error},
    )


@app.post("/login")
async def login_submit(
    request: Request,
    email: str = Form(""),
    password: str = Form(...),
    next: str = Form("/"),
    _csrf: None = Depends(csrf_protect),
):
    from src.services import admin_user_service

    def _fail(msg: str, code: int):
        return TEMPLATES.TemplateResponse(
            request,
            "login.html",
            {"next": next, "error": msg, "email": email},
            status_code=code,
        )

    from src.dashboard.i18n import t as dash_t

    # Throttle brute-force before doing any (expensive) hashing.
    if login_rate_limited(request):
        return _fail(dash_t("login_rate_limited"), status.HTTP_429_TOO_MANY_REQUESTS)

    # Per-user path when accounts exist; bootstrap (shared password) only while
    # the accounts table is empty.
    if await admin_user_service.count_admins() > 0:
        admin = await admin_user_service.authenticate(email, password)
        uid = admin.id if admin else None
    else:
        uid = _BOOTSTRAP_UID if verify_shared_password(password) else None

    if uid is None:
        record_login_failure(request)
        return _fail(dash_t("login_bad_credentials"), status.HTTP_401_UNAUTHORIZED)

    clear_login_failures(request)
    cookie = make_session_cookie(uid)
    safe_next = next if next.startswith("/") else "/"
    resp = RedirectResponse(url=safe_next, status_code=status.HTTP_303_SEE_OTHER)
    resp.set_cookie(
        SESSION_COOKIE,
        cookie,
        max_age=SESSION_MAX_AGE_SEC,
        httponly=True,
        samesite="lax",
        # secure=True when serving over HTTPS (Cloudflare, nginx, etc).
        # Set DASH_COOKIE_SECURE=true in .env on the server.
        secure=os.getenv("DASH_COOKIE_SECURE", "false").lower() == "true",
    )
    logger.info("Dashboard login OK (uid=%s)", uid)
    return resp


@app.post("/logout")
async def logout():
    resp = RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    resp.delete_cookie(SESSION_COOKIE)
    return resp


# Convenience GET for logout link in nav
@app.get("/logout")
async def logout_get():
    return await logout()


# ==================== Health ====================


@app.get("/healthz")
async def healthz():
    return {"ok": True}


# ==================== Error pages ====================

from starlette.exceptions import HTTPException as StarletteHTTPException  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402


def _wants_json(request: Request) -> bool:
    """JSON for fetch()/API-style endpoints, HTML page for normal navigation."""
    if ".json" in request.url.path or request.url.path.endswith("/ai-suggest"):
        return True
    return "application/json" in request.headers.get("accept", "")


@app.exception_handler(StarletteHTTPException)
async def _http_exception_handler(request: Request, exc: StarletteHTTPException):
    # Auth flow raises 307 with a Location header — pass redirects through untouched.
    headers = exc.headers or {}
    if 300 <= exc.status_code < 400 and "Location" in headers:
        return RedirectResponse(headers["Location"], status_code=exc.status_code)
    if _wants_json(request):
        return JSONResponse({"ok": False, "error": str(exc.detail)}, status_code=exc.status_code)
    return TEMPLATES.TemplateResponse(
        request,
        "error.html",
        {"status_code": exc.status_code,
         "detail": exc.detail if isinstance(exc.detail, str) else ""},
        status_code=exc.status_code,
        headers={k: v for k, v in headers.items() if k.lower() != "location"},
    )


@app.exception_handler(OverflowError)
async def _oversized_id_handler(request: Request, exc: OverflowError):
    """An id too wide for SQLite is not a server fault, it is a bad address.

    FastAPI happily parses any number of digits into a Python int, and the
    driver only objects when it tries to bind it — by then we are inside the
    query and it surfaces as a 500. Every route with an int path parameter had
    this, so it is caught in one place: a row that cannot be addressed cannot
    be found.
    """
    logger.info(f"Out-of-range id on {request.method} {request.url.path}")
    if _wants_json(request):
        return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
    return TEMPLATES.TemplateResponse(
        request, "error.html", {"status_code": 404, "detail": ""}, status_code=404,
    )


@app.exception_handler(Exception)
async def _unhandled_exception_handler(request: Request, exc: Exception):
    logger.exception(f"Unhandled error on {request.method} {request.url.path}: {exc}")
    if _wants_json(request):
        return JSONResponse({"ok": False, "error": "internal error"}, status_code=500)
    return TEMPLATES.TemplateResponse(
        request, "error.html", {"status_code": 500, "detail": ""}, status_code=500,
    )


# ==================== Authenticated routes ====================

# Each route module registers its own paths. All require a valid operator
# session (require_auth) + CSRF on unsafe methods. RBAC on top:
#   any operator  — tickets, users, analytics
#   admin & owner — Configure section (templates/catalogs/settings/AI)
#   owner only    — operator management
from src.dashboard.auth import require_role  # noqa: E402

_ANY_ROLE = [Depends(require_auth), Depends(csrf_protect)]
_ADMIN_UP = [Depends(require_auth), Depends(csrf_protect),
             Depends(require_role("admin", "owner"))]
_OWNER_ONLY = [Depends(require_auth), Depends(csrf_protect),
               Depends(require_role("owner"))]

app.include_router(overview_routes.router, dependencies=_ANY_ROLE)
app.include_router(tickets_routes.router, dependencies=_ANY_ROLE)
app.include_router(users_routes.router, dependencies=_ANY_ROLE)
app.include_router(ai_routes.router, dependencies=_ANY_ROLE)
app.include_router(templates_routes.router, dependencies=_ANY_ROLE)
app.include_router(reports_routes.router, dependencies=_ADMIN_UP)
# Broadcast reaches every user at once and cannot be undone — admin+ only.
app.include_router(broadcast_routes.router, dependencies=_ADMIN_UP)
app.include_router(manage_templates_routes.router, dependencies=_ADMIN_UP)
app.include_router(manage_settings_routes.router, dependencies=_ADMIN_UP)
app.include_router(manage_ai_routes.router, dependencies=_ADMIN_UP)
app.include_router(admin_users_routes.router, dependencies=_OWNER_ONLY)


# Root now lands on the management side (tickets), not analytics
@app.get("/", include_in_schema=False)
async def root():
    return RedirectResponse(url="/tickets", status_code=status.HTTP_302_FOUND)


# Analytics entry point — the old overview dashboard lives under this alias now
@app.get("/analytics", include_in_schema=False)
async def analytics_root():
    return RedirectResponse(url="/overview", status_code=status.HTTP_302_FOUND)


@app.on_event("startup")
async def _startup() -> None:
    # Ensure the DB schema is up to date even if the dashboard is started before
    # (or without) the bot. init_db runs create_all + idempotent migrations.
    try:
        from src.database.db import init_db
        await init_db()
        logger.info("Dashboard: DB schema ensured on startup")
        # Apply the configured fallback language to this process.
        from src.services import settings_service
        from src.utils.i18n import set_runtime_default_language
        set_runtime_default_language(await settings_service.get_default_language())
    except Exception as e:
        logger.error(f"Dashboard startup migration failed: {e}", exc_info=True)

    # Which pipes this process can reach customers through. Telegram is
    # registered against the send-only client the dashboard already builds;
    # channels added later register themselves here too.
    try:
        from src.channels import ensure as ensure_channel
        from src.channels.telegram import TelegramChannel
        from src.dashboard.bot_client import get_bot
        ensure_channel(TelegramChannel(get_bot()))
    except Exception as e:
        # A missing token should not stop the dashboard from serving pages;
        # the reply box will report the failure when someone actually sends.
        logger.warning(f"Telegram channel unavailable in this process: {e}")

    # Expose brand_name as a Jinja2 global so every template can reference
    # {{ g_brand_name }} without each route having to pass it explicitly.
    # Updated via _refresh_brand_global() when admin saves a new brand name.
    try:
        from src.services import settings_service as _ss
        TEMPLATES.env.globals["g_brand_name"] = await _ss.get_brand_name()
        logger.info(f"Dashboard: brand_name global set to '{TEMPLATES.env.globals['g_brand_name']}'")
    except Exception as e:
        TEMPLATES.env.globals["g_brand_name"] = "Support"
        logger.warning(f"Dashboard: could not load brand_name global, using default: {e}")

    # Install the stable secret used to sign session cookies. Falls back to a
    # password-derived secret inside auth.py if this fails.
    try:
        from src.services import settings_service as _ss2
        set_secret_key(await _ss2.get_or_create_secret_key())
        logger.info("Dashboard: session secret key installed")
    except Exception as e:
        logger.warning(f"Dashboard: could not load session secret key, using fallback: {e}")

    # Dashboard UI language: instance-level, en by default (global product).
    # T is the translations dict every template reads; refreshed on save.
    try:
        from src.dashboard import i18n as dash_i18n
        from src.services import settings_service as _ss3
        dash_i18n.set_active_language(await _ss3.get_dash_language())
        TEMPLATES.env.globals["T"] = dash_i18n.strings()
        logger.info(f"Dashboard: UI language = {dash_i18n.active_language()}")
    except Exception as e:
        from src.dashboard import i18n as dash_i18n
        TEMPLATES.env.globals["T"] = dash_i18n.strings("en")
        logger.warning(f"Dashboard: could not load UI language, using en: {e}")

    # Demo badge (set by scripts/run_demo_dashboard.py and the hosted demo).
    TEMPLATES.env.globals["g_demo_mode"] = os.getenv("DEMO_MODE") == "1"



@app.on_event("shutdown")
async def _shutdown() -> None:
    from src.dashboard.bot_client import close_bot
    await close_bot()
