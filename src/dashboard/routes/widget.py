"""The public face of the helpdesk: a chat bubble on someone else's website.

Everything else in this application is behind a login. These endpoints are
not, and that changes what has to be true of them:

Authentication is a bearer token in a header, never a cookie. That is not a
style choice — a cookie-authenticated public POST endpoint is a CSRF hole by
construction, and one carrying a header cannot be forged by a form on
another site. It also keeps the token out of URLs, and therefore out of
access logs, referrer headers and browser history.

Rate limiting is the actual protection, and the origin allowlist is not.
Browsers enforce CORS; curl does not. Anyone who wants to spam the helpdesk
will not be using a browser, so the limits below are what stands in the way,
and they are deliberately tighter on the expensive action — opening a
session creates a customer and a ticket, while a follow-up message appends a
row.

Nothing here trusts the page it is embedded in. The visitor's name, the page
URL, anything the host site passes: it is all a stranger's input, stored as
text and shown to an operator as text.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from fastapi import APIRouter, Header, Request, Response, status
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

from src.channels.widget import NAME as WIDGET, address_for, new_session_token
from src.dashboard.auth import _client_ip
from src.database.db import db_session
from src.services import intake_service, settings_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/widget", tags=["widget"])

# ---------------------------------------------------------------- limits

#: A visitor's whole conversation hangs off one of these, so opening one is
#: the costly action: a customer row, a ticket, and a card on someone's board.
_NEW_SESSIONS_PER_IP = 5
_NEW_SESSION_WINDOW = 3600.0

#: Follow-ups are cheap, but a stuck loop in a host page could still write
#: thousands of rows before anyone noticed.
_MESSAGES_PER_SESSION = 12
_MESSAGE_WINDOW = 60.0

#: Longer than anybody types into a chat bubble, short enough that a paste
#: bomb is not a storage problem.
_MAX_MESSAGE_CHARS = 4000
_MAX_NAME_CHARS = 60

#: In memory, like the login throttle, and honest about what that means: a
#: restart forgives everyone, and a second instance keeps its own count.
#: Good enough for a self-hosted helpdesk; a deployment that needs more
#: should be putting a proper limiter in front of this anyway.
_hits: dict[str, list[float]] = {}


def _too_many(bucket: str, limit: int, window: float) -> bool:
    now = time.time()
    recent = [t for t in _hits.get(bucket, []) if now - t < window]
    _hits[bucket] = recent
    if len(recent) >= limit:
        return True
    recent.append(now)
    return False


def _forget(bucket: str) -> None:
    _hits.pop(bucket, None)


# ---------------------------------------------------------------- CORS


async def _allowed_origin(request: Request) -> str | None:
    """The value for Access-Control-Allow-Origin, or None to say nothing.

    Echoing the request's own origin rather than "*" because a browser
    refuses "*" alongside credentials, and a later change that adds any is
    then one that silently breaks every embed.
    """
    origin = (request.headers.get("origin") or "").strip().rstrip("/")
    if not origin:
        return None
    allowed = await settings_service.get_widget_origins()
    if not allowed:
        return origin
    return origin if origin in allowed else None


def _cors(response: Response, origin: str | None) -> Response:
    if origin:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Vary"] = "Origin"
    response.headers["Access-Control-Allow-Headers"] = "content-type, x-spooldesk-session"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Access-Control-Max-Age"] = "86400"
    return response


def _json(payload: dict[str, Any], origin: str | None, code: int = 200) -> JSONResponse:
    return _cors(JSONResponse(payload, status_code=code), origin)


@router.options("/api/{rest:path}")
async def preflight(request: Request, rest: str) -> Response:
    return _cors(Response(status_code=204), await _allowed_origin(request))


# ---------------------------------------------------------------- helpers


async def _off(origin: str | None) -> JSONResponse | None:
    """Refuse everything while the widget is switched off.

    Checked on every request rather than at startup: turning it off has to
    take effect for the visitor who is mid-conversation, not at the next
    restart.
    """
    if not await settings_service.get_widget_enabled():
        return _json({"error": "disabled"}, origin, status.HTTP_404_NOT_FOUND)
    return None


def _clean(text: str | None, limit: int) -> str:
    return (text or "").strip()[:limit]


async def _transcript(ticket_id: int) -> list[dict[str, Any]]:
    """What the visitor is allowed to see of their own conversation.

    Goes through the intake service's filter rather than reading messages
    directly, so the exclusion of internal notes lives in one place. This is
    the endpoint where getting that wrong would show a customer what an
    operator wrote about them.
    """
    async with db_session() as session:
        rows = await intake_service.conversation(session, ticket_id)
        return [
            {
                "id": m.id,
                "from": "you" if m.sender_type == "user" else "agent",
                "text": m.text or "",
                "at": m.created_at.isoformat() if m.created_at else None,
            }
            for m in rows
        ]


# ---------------------------------------------------------------- endpoints


@router.post("/api/message")
async def post_message(
    request: Request,
    x_spooldesk_session: str | None = Header(default=None),
) -> JSONResponse:
    """A visitor said something. Opens the conversation if this is the first.

    One endpoint for both, because the widget cannot usefully tell them
    apart: a returning visitor with an expired session and a brand new one
    look identical from the page, and making the page decide would put the
    decision in the least trustworthy place.
    """
    origin = await _allowed_origin(request)
    if (off := await _off(origin)) is not None:
        return off

    body = await request.json()
    text = _clean(body.get("text"), _MAX_MESSAGE_CHARS)
    if not text:
        return _json({"error": "empty"}, origin, status.HTTP_400_BAD_REQUEST)

    token = (x_spooldesk_session or "").strip()
    issued: str | None = None

    if token:
        bucket = f"w:{address_for(token)}"
        if _too_many(bucket, _MESSAGES_PER_SESSION, _MESSAGE_WINDOW):
            return _json({"error": "slow_down"}, origin,
                         status.HTTP_429_TOO_MANY_REQUESTS)
    else:
        ip_bucket = f"ip:{_client_ip(request)}"
        if _too_many(ip_bucket, _NEW_SESSIONS_PER_IP, _NEW_SESSION_WINDOW):
            return _json({"error": "slow_down"}, origin,
                         status.HTTP_429_TOO_MANY_REQUESTS)
        issued = token = new_session_token()

    async with db_session() as session:
        result = await intake_service.receive(session, intake_service.Inbound(
            channel=WIDGET,
            address=address_for(token),
            text=text,
            display_name=_clean(body.get("name"), _MAX_NAME_CHARS) or None,
            language=_clean(body.get("lang"), 8) or None,
        ))
        await session.commit()

    if result.rejected:
        # "blocked" is deliberately not spelled out. Someone who has been
        # blocked learning exactly that just opens a new session; someone
        # having a genuine problem gets the same generic failure either way.
        return _json({"error": "rejected"}, origin, status.HTTP_403_FORBIDDEN)

    logger.info(
        f"Widget message on ticket #{result.ticket_id}"
        f"{' (new conversation)' if result.opened_ticket else ''}"
    )
    payload: dict[str, Any] = {
        "ticket": result.ticket_id,
        "messages": await _transcript(result.ticket_id),
    }
    if issued:
        # Returned exactly once, at the moment it is created. After this the
        # page holds the only copy; the database has a hash.
        payload["session"] = issued
    return _json(payload, origin)


@router.get("/api/poll")
async def poll(
    request: Request,
    x_spooldesk_session: str | None = Header(default=None),
) -> JSONResponse:
    """Whatever the operator has said since the visitor last looked.

    The session token comes in a header rather than the query string on
    purpose: a query string ends up in access logs and browser history, and
    this one is a credential.
    """
    origin = await _allowed_origin(request)
    if (off := await _off(origin)) is not None:
        return off

    token = (x_spooldesk_session or "").strip()
    if not token:
        return _json({"error": "no_session"}, origin, status.HTTP_401_UNAUTHORIZED)

    from src.channels.identity import find_customer

    async with db_session() as session:
        user = await find_customer(session, channel=WIDGET, address=address_for(token))
        if user is None:
            # An unknown token is answered the same way as a missing one. A
            # different response here would turn this into an oracle for
            # checking guesses.
            return _json({"error": "no_session"}, origin, status.HTTP_401_UNAUTHORIZED)
        ticket = await intake_service.open_ticket_of(session, user.id)

    if ticket is None:
        return _json({"ticket": None, "messages": []}, origin)
    return _json({"ticket": ticket.id, "messages": await _transcript(ticket.id)}, origin)


@router.get("/api/hello")
async def hello(request: Request) -> JSONResponse:
    """What the bubble needs before anyone has said anything.

    Fetched on load, so it must be cheap and must not create anything: a
    visitor who opens a page and never types has cost one row of nothing.
    """
    origin = await _allowed_origin(request)
    if (off := await _off(origin)) is not None:
        return off

    from src.dashboard.i18n import t as dash_t

    greeting = await settings_service.get_widget_greeting()
    # Every string the panel can ever need, in one response. It has no second
    # chance to ask: the moment it needs the "could not send" line is the
    # moment the network is already failing.
    return _json({
        "brand": await settings_service.get_brand_name(),
        "greeting": greeting or dash_t("wg_greeting"),
        "placeholder": dash_t("wg_placeholder"),
        "sending": dash_t("wg_sending"),
        "offline": dash_t("wg_offline"),
        "slow": dash_t("wg_slow"),
        "replied": dash_t("wg_replied"),
        "launcher": dash_t("wg_launcher"),
    }, origin)


@router.get("/embed.js")
async def embed_js(request: Request) -> Response:
    """The one line a site owner pastes into their page.

    Served from here rather than from /static so it can be refused while the
    widget is off, and so the endpoint it talks to is filled in from the
    request instead of being configured by hand — the commonest way an embed
    silently fails is pointing at the wrong host.
    """
    if not await settings_service.get_widget_enabled():
        return PlainTextResponse(
            "/* Spooldesk widget is not enabled on this instance. */",
            status_code=status.HTTP_404_NOT_FOUND,
            media_type="application/javascript",
        )

    from src.dashboard.routes.widget_asset import loader_js

    base = str(request.base_url).rstrip("/")
    return Response(
        loader_js(base),
        media_type="application/javascript",
        headers={
            # Short enough that turning the widget off takes effect the same
            # day, long enough that it is not re-fetched on every page view.
            "Cache-Control": "public, max-age=900",
        },
    )


@router.get("/frame", response_class=HTMLResponse)
async def frame(request: Request) -> Response:
    """The panel itself, served into an iframe.

    An iframe rather than injected DOM because the host page's CSS is not
    ours to fight. Every chat widget that renders inline eventually meets a
    site with `* { box-sizing: content-box }` and a `div { line-height: 3 }`,
    and loses.
    """
    if not await settings_service.get_widget_enabled():
        return HTMLResponse("", status_code=status.HTTP_404_NOT_FOUND)

    from src.dashboard.routes.widget_asset import frame_html

    response = HTMLResponse(frame_html())
    # Only the sites allowed to embed may frame it. Without this any page
    # could put the panel in an invisible overlay and read what a visitor
    # types into what they believe is their own site.
    allowed = await settings_service.get_widget_origins()
    response.headers["Content-Security-Policy"] = (
        "frame-ancestors " + (" ".join(allowed) if allowed else "*")
    )
    return response
