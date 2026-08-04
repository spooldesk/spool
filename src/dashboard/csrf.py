"""CSRF protection for the dashboard (Phase 2).

Mechanism: a signed-random token lives in an httpOnly cookie. Every rendered
page embeds the same token (hidden form field + <meta> tag), and unsafe requests
must echo it back — either as a hidden `csrf_token` form field or an
`X-CSRF-Token` header (used by fetch() calls). The server compares the submitted
token against the cookie in constant time. Because JS cannot read the httpOnly
cookie, a cross-site attacker cannot forge a matching token.

Three pieces wire together in app.py:
  - CSRFMiddleware     — issues/refreshes the cookie, exposes the token on
                         request.state for templates.
  - csrf_context       — Jinja context processor injecting `csrf_token`.
  - csrf_protect       — FastAPI dependency that validates unsafe requests.
"""

from __future__ import annotations

import hmac
import os
import secrets

from fastapi import HTTPException, Request, status
from starlette.middleware.base import BaseHTTPMiddleware

CSRF_COOKIE = "supbot_csrf"
CSRF_FIELD = "csrf_token"
CSRF_HEADER = "x-csrf-token"
_MAX_AGE = 7 * 24 * 3600

_SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}


def _cookie_secure() -> bool:
    return os.getenv("DASH_COOKIE_SECURE", "false").lower() == "true"


class CSRFMiddleware(BaseHTTPMiddleware):
    """Ensure every client carries a CSRF token cookie; expose it to templates."""

    async def dispatch(self, request: Request, call_next):
        token = request.cookies.get(CSRF_COOKIE)
        issue = token is None
        if issue:
            token = secrets.token_urlsafe(32)
        # Make the token available to the context processor for this request.
        request.state.csrf_token = token

        response = await call_next(request)

        if issue:
            response.set_cookie(
                CSRF_COOKIE,
                token,
                max_age=_MAX_AGE,
                httponly=True,
                samesite="lax",
                secure=_cookie_secure(),
            )
        return response


def csrf_context(request: Request) -> dict:
    """Jinja context processor: expose the current token to templates."""
    token = getattr(request.state, "csrf_token", "") or request.cookies.get(CSRF_COOKIE, "")
    return {"csrf_token": token}


async def csrf_protect(request: Request) -> None:
    """FastAPI dependency: validate the CSRF token on unsafe requests.

    Header (X-CSRF-Token) is checked first so AJAX/JSON requests aren't forced
    through form parsing. Falls back to the hidden form field for normal POSTs;
    reading the form here is cached and reused by the endpoint's Form params.
    """
    if request.method in _SAFE_METHODS:
        return

    cookie_token = request.cookies.get(CSRF_COOKIE)
    submitted = request.headers.get(CSRF_HEADER)

    if not submitted:
        ctype = request.headers.get("content-type", "")
        if ctype.startswith("application/x-www-form-urlencoded") or ctype.startswith("multipart/form-data"):
            try:
                form = await request.form()
                submitted = form.get(CSRF_FIELD)
            except Exception:  # noqa: BLE001
                submitted = None

    if not cookie_token or not submitted or not hmac.compare_digest(str(cookie_token), str(submitted)):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="CSRF token missing or invalid",
        )
