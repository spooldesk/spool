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
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from src.dashboard.auth import (
    SESSION_COOKIE,
    SESSION_MAX_AGE_SEC,
    _BOOTSTRAP_UID,
    clear_login_failures,
    current_user,
    is_current_session,
    login_rate_limited,
    make_session_cookie,
    record_login_failure,
    require_auth,
    safe_next_path,
    session_claims,
    set_secret_key,
    verify_shared_password,
)
from src.dashboard.csrf import CSRFMiddleware, csrf_context, csrf_protect
from src.dashboard.proxy import ProxyHeaders
from src.dashboard.routes import admin_users as admin_users_routes
from src.dashboard.routes import ai as ai_routes
from src.dashboard.routes import broadcast as broadcast_routes
from src.dashboard.routes import manage_ai as manage_ai_routes
from src.dashboard.routes import manage_settings as manage_settings_routes
from src.dashboard.routes import manage_articles as manage_articles_routes
from src.dashboard.routes import manage_templates as manage_templates_routes
from src.dashboard.routes import overview as overview_routes
from src.dashboard.routes import preferences as preferences_routes
from src.dashboard.routes import reports as reports_routes
from src.dashboard.routes import help as help_routes
from src.dashboard.routes import slack as slack_routes
from src.dashboard.routes import templates_page as templates_routes
from src.dashboard.routes import tickets as tickets_routes
from src.dashboard.routes import users as users_routes
from src.dashboard.routes import widget as widget_routes
from src.utils.logger import setup_logging

logger = logging.getLogger(__name__)

# Initialise logging once on import — same config the bot uses, but separate file
setup_logging(log_level="INFO", log_file="logs/dashboard.log")

_PKG_DIR = Path(__file__).parent


def _current_user_context(request: Request) -> dict:
    """Expose the logged-in operator to every template (footer, etc.)."""
    from src.config import load_settings

    from src.services.permissions import section_for
    grants = getattr(request.state, "section_permissions", {})
    actor = current_user(request)
    def can_view(path):
        if actor and actor.role in {"agent", "viewer"} and path.startswith("/manage/"):
            return False
        return grants.get(section_for(path)) in {"view", "edit"}
    return {"current_user": actor, "has_telegram": load_settings().has_telegram,
            "assistant_enabled": getattr(request.state, "assistant_enabled", False),
            "assistant_context_available": section_for(request.url.path) in {
                "tickets", "board", "users", "templates", "articles", "categories", "close_reasons",
                "teams", "settings", "ai_settings", "overview", "reports", "ai", "integrations", "operators", "roles"},
            "can_view": can_view,
            "section_editable": grants.get(section_for(request.url.path)) == "edit",
            "section_permissions": grants}


def _preferences_context(request: Request) -> dict:
    """This operator's own settings, and the strings in their language.

    `T` is also a Jinja global, set once from the instance language. A context
    variable of the same name shadows it, so a page rendered for someone who
    chose Russian is Russian without anything global changing — which under
    asyncio it must not, since two requests share the process.
    """
    from src.dashboard.i18n import strings

    prefs = getattr(request.state, "prefs", None) or {}
    context = {"prefs": prefs}
    if prefs.get("language"):
        context["T"] = strings(prefs["language"])
    return context


TEMPLATES = Jinja2Templates(
    directory=str(_PKG_DIR / "templates"),
    context_processors=[csrf_context, _current_user_context, _preferences_context],
)

# Include every CSS/JS asset, including conversation and workspace changes.
# Content hashes also work when a build preserves file timestamps.
from src.dashboard.assets import asset_version

TEMPLATES.env.globals["g_asset_v"] = asset_version(_PKG_DIR / "static")

app = FastAPI(title="Spooldesk", docs_url=None, redoc_url=None)
# Outermost, because everything below it — CSRF, the rate limits, the base URL
# the embed snippet is built from — reads a client address and a scheme that
# this is responsible for having corrected first. Uvicorn's own version of
# this is switched off (`--no-proxy-headers`); two of them disagreeing is how
# the widget's per-address limit quietly stopped working.
app.add_middleware(ProxyHeaders)
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
    next = safe_next_path(next)
    # If already logged in, skip the form
    if await is_current_session(request.cookies.get(SESSION_COOKIE)):
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
        generation = admin.session_version if admin else 0
    else:
        uid = _BOOTSTRAP_UID if verify_shared_password(password) else None
        generation = 0

    if uid is None:
        record_login_failure(request)
        return _fail(dash_t("login_bad_credentials"), status.HTTP_401_UNAUTHORIZED)

    clear_login_failures(request)
    cookie = make_session_cookie(uid, generation)
    safe_next = safe_next_path(next)
    if uid != _BOOTSTRAP_UID:
        from src.services.permissions import for_role, section_for
        grants = await for_role(admin.role)
        path = safe_next.split('?', 1)[0]
        if (not grants.get(section_for(path)) or
                (admin.role in {'agent', 'viewer'} and path.startswith('/manage/'))):
            safe_next = '/'
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


@app.post("/logout", dependencies=[Depends(csrf_protect)])
async def logout(request: Request):
    from src.services import admin_user_service

    claims = session_claims(request.cookies.get(SESSION_COOKIE))
    if claims and claims[0] != _BOOTSTRAP_UID:
        # Compare-and-revoke: replaying an old cookie cannot sign out a newer
        # login. Logging out revokes this operator's entire current generation.
        await admin_user_service.revoke_sessions(claims[0], expected_version=claims[1])
    resp = RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    resp.delete_cookie(SESSION_COOKIE)
    return resp


# Older bookmarks remain safe: a GET must never revoke an operator's sessions.
@app.get("/logout")
async def logout_get():
    return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)


# ==================== Health ====================


@app.get("/healthz")
async def healthz():
    """Whether this instance is working, not merely running.

    It answered `{"ok": true}` unconditionally, which meant the container
    healthcheck — the thing whose entire job is to notice — asked a question
    that could not fail. A process with no database answered the same as a
    healthy one.

    503 rather than 200 when something is wrong, because that is the only
    part any monitor reads. The body says what, for whoever looks after it.

    Public and unauthenticated, like the widget's endpoints: a healthcheck
    behind a login cannot be used by the container that needs it. It reports
    which subsystem is unhappy and nothing about what is in them.
    """
    from src.services import health

    result = await health.check()
    return JSONResponse(result.as_dict(),
                        status_code=200 if result.ok else 503)


# ==================== Error pages ====================

from starlette.exceptions import HTTPException as StarletteHTTPException  # noqa: E402


def _wants_json(request: Request) -> bool:
    """JSON for fetch()/API-style endpoints, HTML page for normal navigation."""
    if ".json" in request.url.path or request.url.path.endswith("/ai-suggest"):
        return True
    return (request.headers.get("x-requested-with") == "Spooldesk"
            or "application/json" in request.headers.get("accept", ""))


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
from src.dashboard.routes import assistant as assistant_routes
app.include_router(assistant_routes.router, dependencies=_ANY_ROLE)
app.include_router(templates_routes.router, dependencies=_ANY_ROLE)
app.include_router(reports_routes.router, dependencies=_ADMIN_UP)
# Broadcast reaches every user at once and cannot be undone — admin+ only.
app.include_router(broadcast_routes.router, dependencies=_ADMIN_UP)
app.include_router(manage_templates_routes.router, dependencies=_ADMIN_UP)
# Everything a customer can read is written behind this router, so it sits
# with the other admin-and-up pages rather than with the operator ones.
app.include_router(manage_articles_routes.router, dependencies=_ADMIN_UP)
app.include_router(manage_settings_routes.router, dependencies=_ADMIN_UP)
app.include_router(manage_ai_routes.router, dependencies=_ADMIN_UP)
app.include_router(admin_users_routes.router, dependencies=_OWNER_ONLY)
from src.dashboard.routes import workspace as workspace_routes
app.include_router(workspace_routes.router, dependencies=_OWNER_ONLY)
from src.dashboard.routes import invitations as invitation_routes
app.include_router(invitation_routes.router)
# Personal settings: authenticated, but no role check. Nothing here affects
# anybody else, so an agent may choose their own language and shortcuts.
app.include_router(preferences_routes.router, dependencies=_ANY_ROLE)

# The only router with no auth and no CSRF dependency, which is the point of
# it: these endpoints answer a stranger's browser on somebody else's website.
# What stands in for a login is a bearer token in a header, and what stands in
# for CSRF protection is the fact that a header cannot be forged by a form on
# another site. See src/dashboard/routes/widget.py.
app.include_router(widget_routes.router)

# Slack's Events API. Public like the widget's, and unauthenticated in the
# same sense, but protected differently: exactly one caller is legitimate and
# it proves that with a signature over the raw body. See routes/slack.py.
app.include_router(slack_routes.router)

# The help centre, read by customers rather than operators. Public like the
# widget's endpoints and for the same reason — a person looking for an answer
# has not signed in to anything — but plainer: server-rendered HTML, no token,
# no session, nothing to forge. Published articles only; see routes/help.py.
app.include_router(help_routes.router)


# Root now lands on the management side (tickets), not analytics
# Authenticated, unlike the other routes on the app itself: it has to know
# who is asking before it can honour their landing preference, and a logged-out
# visitor lands on /login one hop sooner than it used to.
@app.get("/", include_in_schema=False, dependencies=[Depends(require_auth)])
async def root(request: Request):
    """Where the logo takes you — the operator's choice, tickets by default.

    Read from what `require_auth` stashed rather than queried again: this runs
    on every visit to the bare host, which for someone who keeps the dashboard
    open is several times a day.
    """
    prefs = getattr(request.state, "prefs", None) or {}
    from src.services.permissions import SECTIONS, section_for
    grants = getattr(request.state, "section_permissions", {})
    destination = prefs.get("landing") or "/tickets"
    if not grants.get(section_for(destination)):
        destination = next((path for section, (_, path) in SECTIONS.items() if grants.get(section)), "/me")
    return RedirectResponse(url=destination,
                            status_code=status.HTTP_302_FOUND)


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
        from src.services import integrations
        await integrations.refresh()
        logger.info("Dashboard: DB schema ensured on startup")
        # Apply the configured fallback language to this process.
        from src.services import settings_service
        from src.utils.i18n import set_runtime_default_language
        set_runtime_default_language(await settings_service.get_default_language())
    except Exception as e:
        logger.error(f"Dashboard startup migration failed: {e}", exc_info=True)
        raise

    # Which pipes this process can reach customers through. Telegram is
    # registered against the send-only client the dashboard already builds;
    # channels added later register themselves here too.
    try:
        from src.channels import ensure as ensure_channel
        from src.channels.telegram import TelegramChannel
        from src.dashboard.bot_client import get_bot

        # Optional, like Gemini. An instance serving only the website widget
        # needs no bot token, and should not be told off for not having one.
        bot = get_bot()
        if bot is not None:
            ensure_channel(TelegramChannel(bot))
        else:
            logger.info("No BOT_TOKEN — running without a Telegram channel")
        from src.channels.widget import WidgetChannel
        ensure_channel(WidgetChannel())
        # Registered only when it has credentials: an adapter that
        # cannot authenticate would answer `get("slack")` and then fail
        # every send, which reads like an outage rather than a setting
        # nobody filled in.
        from src.channels.slack import SlackChannel, is_configured
        if is_configured():
            ensure_channel(SlackChannel())
        from src.channels.email import EmailChannel, is_configured as mail_ready
        if mail_ready():
            ensure_channel(EmailChannel())
    except Exception as e:
        # A missing token should not stop the dashboard from serving pages;
        # the reply box will report the failure when someone actually sends.
        logger.warning(f"Telegram channel unavailable in this process: {e}")

    # The mailbox is polled from here rather than from the bot, because the
    # bot needs a Telegram token and an instance may not have one. A helpdesk
    # on the widget and email alone is a supported configuration; a mail loop
    # in a process that might never start would quietly make it not one.
    try:
        from src.channels.email import is_configured as mail_ready
        if os.getenv("TESTING") != "1":
            import asyncio

            from src.services import email_ingest
            app.state.mail_task = asyncio.create_task(email_ingest.poll_loop())
        else:
            logger.info("No mailbox configured — running without an email channel")
    except Exception as e:  # noqa: BLE001 — never block startup over this
        logger.warning(f"Could not start mailbox polling: {e}")

    # Somebody has to be told when this stops working. Errors go to a log
    # file on a server that nobody opens, which is how a mailbox loop can
    # fail for three days and be discovered by a customer who did not get an
    # answer. The watchdog puts that in the admin group instead.
    try:
        import asyncio

        from src.services import watchdog
        if os.getenv("TESTING") != "1":
            app.state.watchdog_task = asyncio.create_task(watchdog.loop())
        else:
            logger.info(
                "No Telegram configured — health alerts are off. /healthz "
                "still reports; point an external uptime check at it."
            )
    except Exception as e:  # noqa: BLE001 — a watchdog must never be the outage
        logger.warning(f"Could not start the health watchdog: {e}")

    if os.getenv("TESTING") != "1":
        import asyncio
        from src.services import channel_runtime
        app.state.channel_config_task = asyncio.create_task(channel_runtime.refresh_loop())
        app.state.telegram_task = asyncio.create_task(channel_runtime.telegram_loop())
        from src.services import announce_service, inbound_service, outbound, sla_service
        from src.dashboard.bot_client import get_bot
        app.state.inbound_task = asyncio.create_task(inbound_service.poll_loop())
        app.state.outbound_task = asyncio.create_task(outbound.poll_loop())
        app.state.sla_task = asyncio.create_task(sla_service.poll_loop())
        app.state.mirror_task = asyncio.create_task(announce_service.poll_loop())

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

    # Install the stable session-signing secret; refuse startup on failure.
    try:
        from src.services import settings_service as _ss2
        set_secret_key(await _ss2.get_or_create_secret_key())
        logger.info("Dashboard: session secret key installed")
    except Exception as e:
        logger.error("Dashboard: could not load session secret key")
        raise

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
    import asyncio

    # Cancel the background loops before closing what they use, so a pass
    # already in flight does not fail against a bot session that has gone.
    # Neither was cancelled before, which is where "Task was destroyed but it
    # is pending" on every restart came from.
    for name in ("channel_config_task", "telegram_task", "mail_task", "watchdog_task", "inbound_task", "outbound_task", "sla_task", "mirror_task"):
        task = getattr(app.state, name, None)
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    from src.dashboard.bot_client import close_bot
    await close_bot()
