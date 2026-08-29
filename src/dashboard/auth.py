"""Dashboard authentication: per-user accounts, signed session cookie.

Phase 2 replaced the single shared ACCESS_PASSWORD with individual operator
accounts (see src.services.admin_user_service). The session cookie now carries
the operator's id, signed with a stable secret so password rotation no longer
logs everyone out.

Bootstrap safety: if NO accounts exist yet (e.g. the owner deleted them all),
the login flow falls back to the shared ACCESS_PASSWORD and issues a special
"bootstrap" session (uid 0) that stays valid only while the accounts table is
empty. The fresh-DB seed normally creates admin@local from ACCESS_PASSWORD, so
the per-user path is the default.
"""

from __future__ import annotations

import hmac
import logging
import time
from typing import Optional

from fastapi import HTTPException, Request, status
from itsdangerous import BadSignature, TimestampSigner

from src.config import load_settings
from src.dashboard.i18n import t as tr

logger = logging.getLogger(__name__)

SESSION_COOKIE = "supbot_dash"
SESSION_MAX_AGE_SEC = 7 * 24 * 3600  # 7 days
_BOOTSTRAP_UID = 0  # sentinel session id for shared-password fallback


# ==================== Signing secret ====================

# Set once at app startup via set_secret_key(). Until then we fall back to a
# password-derived secret so nothing crashes if used before init.
_secret_key: Optional[str] = None
_signer_cache: Optional[TimestampSigner] = None


def set_secret_key(key: str) -> None:
    """Install the stable signing secret (called from app startup)."""
    global _secret_key, _signer_cache
    _secret_key = key
    _signer_cache = None  # rebuild signer on next use


def _signer() -> TimestampSigner:
    global _signer_cache
    if _signer_cache is None:
        if _secret_key:
            secret = f"dash-v2:{_secret_key}".encode("utf-8")
        else:
            # Pre-init fallback — keeps imports/use safe before startup runs.
            secret = f"dash:{load_settings().access_password}".encode("utf-8")
        _signer_cache = TimestampSigner(secret)
    return _signer_cache


# ==================== Shared-password fallback ====================


def verify_shared_password(submitted: str) -> bool:
    """Constant-time compare against the .env ACCESS_PASSWORD (bootstrap only)."""
    settings = load_settings()
    if not settings.access_password:
        return False
    return hmac.compare_digest(
        (submitted or "").encode("utf-8"),
        settings.access_password.encode("utf-8"),
    )


# ==================== Session cookie ====================


def make_session_cookie(user_id: int) -> str:
    """Return a signed value to set in the session cookie for this operator."""
    return _signer().sign(str(int(user_id)).encode("utf-8")).decode("utf-8")


def session_user_id(cookie_value: str | None) -> Optional[int]:
    """Return the signed-in operator id from a cookie, or None if invalid/expired."""
    if not cookie_value:
        return None
    try:
        raw = _signer().unsign(cookie_value.encode("utf-8"), max_age=SESSION_MAX_AGE_SEC)
        return int(raw.decode("utf-8"))
    except (BadSignature, ValueError):
        return None
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Unexpected error verifying session: {e}")
        return None


def is_valid_session(cookie_value: str | None) -> bool:
    """Lightweight validity check (signature/expiry only, no DB lookup)."""
    return session_user_id(cookie_value) is not None


# ==================== Auth dependency ====================


async def _apply_preferences(request: Request, admin_id: int | None) -> None:
    """Stash this operator's preferences and switch the request to their
    language.

    Here rather than in middleware, because middleware runs before
    dependencies and the operator is not known until this one has run.
    Doing it in the wrong place is not a subtle failure — every page renders
    in the instance language no matter what anyone chose — but it is a silent
    one, which is why it survived to be found by hand.
    """
    from src.dashboard.i18n import use_language
    from src.services import preferences_service

    try:
        prefs = await preferences_service.for_operator(admin_id)
    except Exception as e:  # noqa: BLE001 — never fail a page over a preference
        logger.debug(f"Could not load operator preferences: {e}")
        prefs = preferences_service.defaults()

    request.state.prefs = prefs
    use_language(prefs.get("language") or None)


async def require_auth(request: Request) -> None:
    """FastAPI dependency: ensure a valid operator session, else redirect to /login.

    Also loads the current operator into request.state.current_user so routes and
    templates can show who's logged in and (later) enforce roles. A revoked or
    deleted account is rejected on its next request.
    """
    cookie = request.cookies.get(SESSION_COOKIE)
    uid = session_user_id(cookie)
    if uid is None:
        raise _redirect_to_login(request)

    from src.services import admin_user_service

    if uid == _BOOTSTRAP_UID:
        # Bootstrap session: valid only while no real accounts exist.
        if await admin_user_service.count_admins() == 0:
            request.state.current_user = None  # synthetic owner, no row yet
            await _apply_preferences(request, None)
            return
        raise _redirect_to_login(request)

    admin = await admin_user_service.get_by_id(uid)
    if admin is None or not admin.is_active:
        raise _redirect_to_login(request)
    request.state.current_user = admin
    await _apply_preferences(request, admin.id)


def _redirect_to_login(request: Request) -> HTTPException:
    next_url = request.url.path
    if request.url.query:
        next_url += f"?{request.url.query}"
    return HTTPException(
        status_code=status.HTTP_307_TEMPORARY_REDIRECT,
        headers={"Location": f"/login?next={next_url}"},
    )


def current_user(request: Request):
    """Return the AdminUser stashed by require_auth, or None (bootstrap session)."""
    return getattr(request.state, "current_user", None)


def require_role(*roles: str):
    """Dependency factory: allow only operators whose role is in `roles`.

    Must be listed AFTER require_auth in the router dependencies — it reads the
    operator that require_auth stashed on request.state. The bootstrap session
    (shared password while no accounts exist) acts as owner. Insufficient role
    → 403, rendered by the styled error page.
    """
    allowed = set(roles)

    async def _check(request: Request) -> None:
        admin = current_user(request)
        if admin is None:
            return  # bootstrap session — owner-equivalent by definition
        if admin.role not in allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=tr("e_forbidden_section"),
            )

    return _check


# ==================== Login rate limiting ====================

# In-memory, per-IP sliding window. Good enough for a single-process dashboard;
# resets on restart. Successful login clears the counter for that IP.
_MAX_FAILS = 5
_WINDOW_SEC = 300  # 5 minutes
_fail_log: dict[str, list[float]] = {}


def _client_ip(request: Request) -> str:
    """Who to hold responsible for this request.

    Every rate limit here is keyed on the answer, so this is not a detail:
    getting it wrong does not weaken those limits, it removes them.

    No header is read. `request.client` is whatever the socket said, unless
    src/dashboard/proxy.py replaced it — which happens only when the
    deployment has declared how many proxies are in front. Keeping the
    decision in one place is the point: it used to be made here *and* in
    uvicorn's own middleware, and the two disagreed.
    """
    return request.client.host if request.client else "unknown"


def login_rate_limited(request: Request) -> bool:
    """True if this IP has too many recent failed attempts."""
    ip = _client_ip(request)
    now = time.time()
    hits = [t for t in _fail_log.get(ip, []) if now - t < _WINDOW_SEC]
    _fail_log[ip] = hits
    return len(hits) >= _MAX_FAILS


def record_login_failure(request: Request) -> None:
    ip = _client_ip(request)
    now = time.time()
    hits = [t for t in _fail_log.get(ip, []) if now - t < _WINDOW_SEC]
    hits.append(now)
    _fail_log[ip] = hits


def clear_login_failures(request: Request) -> None:
    _fail_log.pop(_client_ip(request), None)
