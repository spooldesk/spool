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

from fastapi import APIRouter, File, Form, Header, Request, Response, UploadFile, status
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    StreamingResponse,
)

from src.channels.widget import (
    NAME as WIDGET,
    address_for,
    identity_subject,
    looks_like_email,
    new_session_token,
    verify_identity,
)
from src.dashboard.auth import _client_ip
from src.database.db import db_session
from src.services import (
    article_service,
    file_store,
    intake_service,
    settings_service,
)

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
#: Matches the column. Long enough for any real address, short enough that
#: the field is not a place to store things.
_MAX_EMAIL_CHARS = 320
_MAX_EXTERNAL_ID_CHARS = 120

#: Half what an operator may send, because the directions are not
#: symmetrical: an operator is a known colleague on a machine you control,
#: and a visitor is a stranger with a disk of ours to fill. Ten megabytes is
#: a generous screenshot and a small video, which is what support gets.
_MAX_UPLOAD_BYTES = 10 * 1024 * 1024

#: Read in pieces so the cap is enforced *during* the upload. Checking the
#: length after reading it all means a stranger decides how much memory this
#: process uses, and Content-Length is a claim, not a fact.
_UPLOAD_CHUNK = 64 * 1024

#: Disk is the scarce thing here, so this is stricter than the message limit.
_UPLOADS_PER_SESSION = 10
_UPLOAD_WINDOW = 300.0

#: Help is public documentation, so it is meant to be read freely — this is
#: not access control, it is a ceiling on how fast one address can make the
#: database answer. Generous, because a visitor typing into a search box
#: fires one of these per keystroke pause.
_HELP_PER_IP = 90
_HELP_WINDOW = 60.0

#: What the browser is allowed to be told a file is. Never the Content-Type
#: the uploader sent: a stored file that comes back as text/html is stored
#: XSS on our own origin, and one that comes back as a script is worse. An
#: extension not on this list is served as a download of unknown bytes.
_INLINE_TYPES = {
    "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
    "gif": "image/gif", "webp": "image/webp", "avif": "image/avif",
}
_DOWNLOAD_TYPES = {
    "pdf": "application/pdf", "txt": "text/plain; charset=utf-8",
    "csv": "text/csv; charset=utf-8", "log": "text/plain; charset=utf-8",
    "json": "application/json", "zip": "application/zip",
    "doc": "application/msword", "xls": "application/vnd.ms-excel",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "mp4": "video/mp4", "mov": "video/quicktime", "webm": "video/webm",
    "mp3": "audio/mpeg", "ogg": "audio/ogg", "wav": "audio/wav",
    "heic": "image/heic", "svg": "image/svg+xml",
}


def _extension(name: str) -> str:
    _, dot, ext = (name or "").rpartition(".")
    return ext.lower() if dot and len(ext) <= 8 else ""


def _is_image(name: str) -> bool:
    """Whether this may be shown rather than downloaded.

    SVG is deliberately absent: it is an image that can contain script, and
    rendering one inline from our own origin would hand a visitor a way to
    run code on the page an operator is reading.
    """
    return _extension(name) in _INLINE_TYPES


def _content_type(name: str) -> str:
    ext = _extension(name)
    return _INLINE_TYPES.get(ext) or _DOWNLOAD_TYPES.get(ext) or "application/octet-stream"


def _safe_filename(name: str) -> str:
    """A name fit to put in a header and show to an operator.

    Nothing is built out of this — the stored path is a reference this server
    invented — so the job here is only to keep control characters, quotes and
    directory separators out of a Content-Disposition header and a ticket
    page.
    """
    name = (name or "").strip().replace("\\", "/").rpartition("/")[2]
    name = "".join(c for c in name if c.isprintable() and c not in '"\r\n')
    return name[:120] or "attachment"

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


async def _identity(body: dict[str, Any]) -> tuple[str | None, str | None, str | None, bool]:
    """Who the page says this visitor is, and whether it can prove it.

    Everything here arrives from a page the visitor can edit, so the only
    question worth asking is which parts are signed. A site that runs

        hash = hmac_sha256(secret, customer_id)

    on its *server* and passes the result is making a claim its own backend
    stands behind; a visitor typing an address into the pre-chat form is
    making one nobody stands behind. Both are useful — most support requests
    are exactly as honest as they look — but they must not arrive at the
    ticket page looking the same, so the flag rides along with the values and
    the page shows the difference.

    Nothing is rejected for being unverified. Refusing an unsigned identity
    would break the ordinary case (a visitor who is not logged in anywhere)
    to defend against one that is already visible.
    """
    name = _clean(body.get("name"), _MAX_NAME_CHARS) or None

    email = _clean(body.get("email"), _MAX_EMAIL_CHARS).lower() or None
    if email and not looks_like_email(email):
        # Dropped rather than stored: an operator who sees something in the
        # email field will treat it as an email, and "call me on 555-0100"
        # sitting there is worse than a blank.
        email = None

    external_id = _clean(body.get("external_id"), _MAX_EXTERNAL_ID_CHARS) or None

    verified = False
    subject = identity_subject(email, external_id)
    if subject:
        secret = await settings_service.get_widget_identity_secret()
        verified = verify_identity(secret, _clean(body.get("hash"), 200), subject)

    return name, email, external_id, verified


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
                # Metadata only. The bytes come from /api/file/{id}, which
                # checks who is asking; putting a link here that did not
                # would make the transcript the authorisation.
                "file": {
                    "name": _safe_filename(m.file_name or "attachment"),
                    "size": m.file_size or 0,
                    "image": _is_image(m.file_name or ""),
                } if m.file_id else None,
            }
            for m in rows
        ]


# ---------------------------------------------------------------- endpoints



async def _announce(ticket_id: int) -> None:
    """Put a new ticket in front of the operators.

    Swallowed on purpose: the visitor's message is already recorded, and
    failing their request because the *operators'* chat was unreachable would
    have the priorities backwards. The ticket is on the board either way.
    """
    try:
        from src.dashboard.bot_client import get_bot
        from src.services import announce_service

        await announce_service.announce_ticket(get_bot(), ticket_id)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Ticket #{ticket_id} not announced: {e}")

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

    name, email, external_id, verified = await _identity(body)

    async with db_session() as session:
        result = await intake_service.receive(session, intake_service.Inbound(
            channel=WIDGET,
            address=address_for(token),
            text=text,
            display_name=name,
            language=_clean(body.get("lang"), 8) or None,
            email=email,
            external_id=external_id,
            identity_verified=verified,
        ))
        await session.commit()

    if result.rejected:
        # "blocked" is deliberately not spelled out. Someone who has been
        # blocked learning exactly that just opens a new session; someone
        # having a genuine problem gets the same generic failure either way.
        return _json({"error": "rejected"}, origin, status.HTTP_403_FORBIDDEN)

    if result.opened_ticket:
        # Into the operators' group, so a widget ticket is answerable from
        # Telegram like any other. After the commit above, never inside it.
        await _announce(result.ticket_id)

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


@router.post("/api/upload")
async def upload(
    request: Request,
    file: UploadFile = File(...),
    text: str = Form(""),
    lang: str = Form(""),
    name: str = Form(""),
    email: str = Form(""),
    external_id: str = Form(""),
    hash: str = Form(""),
    x_spooldesk_session: str | None = Header(default=None),
) -> JSONResponse:
    """A visitor attached something.

    Shaped like `post_message` rather than like a separate upload step, and
    for the same reason that endpoint takes both new and continuing
    conversations: a file *is* a message. Splitting them would mean a visitor
    whose first contact is a screenshot has to send an empty text message
    first to get a ticket, which is the sort of thing that shows up as "the
    widget did nothing when I dragged a file onto it".
    """
    origin = await _allowed_origin(request)
    if (off := await _off(origin)) is not None:
        return off

    token = (x_spooldesk_session or "").strip()
    issued: str | None = None
    if token:
        if _too_many(f"u:{address_for(token)}", _UPLOADS_PER_SESSION, _UPLOAD_WINDOW):
            return _json({"error": "slow_down"}, origin,
                         status.HTTP_429_TOO_MANY_REQUESTS)
    else:
        if _too_many(f"ip:{_client_ip(request)}", _NEW_SESSIONS_PER_IP,
                     _NEW_SESSION_WINDOW):
            return _json({"error": "slow_down"}, origin,
                         status.HTTP_429_TOO_MANY_REQUESTS)
        issued = token = new_session_token()

    filename = _safe_filename(file.filename or "")

    # Read to the cap and one byte past it. That byte is the whole point: it
    # is how an oversized upload is refused without first being accepted.
    chunks: list[bytes] = []
    size = 0
    while True:
        chunk = await file.read(_UPLOAD_CHUNK)
        if not chunk:
            break
        size += len(chunk)
        if size > _MAX_UPLOAD_BYTES:
            return _json({"error": "too_large", "limit": _MAX_UPLOAD_BYTES},
                         origin, status.HTTP_413_REQUEST_ENTITY_TOO_LARGE)
        chunks.append(chunk)
    content = b"".join(chunks)
    if not content:
        return _json({"error": "empty"}, origin, status.HTTP_400_BAD_REQUEST)

    try:
        ref = file_store.put(content)
    except OSError as e:  # noqa: BLE001 — a full or read-only disk
        logger.error(f"Could not store a widget upload: {e}")
        return _json({"error": "store_failed"}, origin,
                     status.HTTP_503_SERVICE_UNAVAILABLE)

    form = {"name": name, "email": email, "external_id": external_id, "hash": hash}
    who, addr, ext_id, verified = await _identity(form)

    async with db_session() as session:
        result = await intake_service.receive(session, intake_service.Inbound(
            channel=WIDGET,
            address=address_for(token),
            text=_clean(text, _MAX_MESSAGE_CHARS),
            display_name=who,
            language=_clean(lang, 8) or None,
            email=addr,
            external_id=ext_id,
            identity_verified=verified,
            attachment=intake_service.Attachment(
                ref=ref,
                name=filename,
                mime_type=_content_type(filename),
                size=size,
                # What the ticket page reads to decide between a thumbnail and
                # a paperclip, and it is the same word Telegram photos use so
                # that page needs no new case.
                kind="photo" if _is_image(filename) else "document",
            ),
        ))
        await session.commit()

    if result.rejected:
        file_store.delete(ref)
        return _json({"error": "rejected"}, origin, status.HTTP_403_FORBIDDEN)

    if result.opened_ticket:
        await _announce(result.ticket_id)

    logger.info(f"Widget upload ({size} bytes) on ticket #{result.ticket_id}")
    payload: dict[str, Any] = {
        "ticket": result.ticket_id,
        "messages": await _transcript(result.ticket_id),
    }
    if issued:
        payload["session"] = issued
    return _json(payload, origin)


@router.get("/api/file/{message_id}")
async def file_download(
    message_id: int,
    request: Request,
    x_spooldesk_session: str | None = Header(default=None),
) -> Response:
    """One attachment from the visitor's own conversation.

    Authorised by the same bearer token as everything else, which is why the
    panel fetches this into a blob instead of putting it in an `<img src>`:
    an `<img>` cannot send a header, and the alternative — a token in the
    URL — would put a credential in access logs and browser history to save
    six lines of JavaScript.

    The message has to be *in the visitor's own transcript*, not merely
    attached to their ticket. Going through the same filter the transcript
    uses is what keeps a file an operator attached to an internal note from
    being fetchable by guessing message ids.
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
            return _json({"error": "no_session"}, origin, status.HTTP_401_UNAUTHORIZED)

        record = None
        for ticket in await intake_service.tickets_of(session, user.id):
            for m in await intake_service.conversation(session, ticket.id):
                if m.id == message_id and m.file_id:
                    record = m
                    break
            if record is not None:
                break

    if record is None:
        # The same answer for "not yours" and "not there". Anything else lets
        # a stranger map which message ids exist.
        return _json({"error": "not_found"}, origin, status.HTTP_404_NOT_FOUND)

    from src.channels import get as get_channel

    data = await get_channel(WIDGET).fetch_file(record.file_id)
    if not data:
        return _json({"error": "unavailable"}, origin,
                     status.HTTP_502_BAD_GATEWAY)

    import io

    filename = _safe_filename(record.file_name or "attachment")
    inline = _is_image(filename)
    response = StreamingResponse(io.BytesIO(data), media_type=_content_type(filename))
    response.headers["Content-Disposition"] = (
        f'{"inline" if inline else "attachment"}; filename="{filename}"'
    )
    # Without this a browser is free to decide the bytes are HTML no matter
    # what we said they were, which would undo the allowlist above.
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Cache-Control"] = "private, max-age=300"
    return _cors(response, origin)


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
    return _json({"ticket": ticket.id, "open": True,
                  "messages": await _transcript(ticket.id)}, origin)


async def _visitor(token: str):
    """The customer behind a session token, or None.

    Every endpoint below needs this and needs to fail the same way when it is
    missing, so it is one function rather than four copies that could drift.
    """
    from src.channels.identity import find_customer

    async with db_session() as session:
        return await find_customer(session, channel=WIDGET, address=address_for(token))


def _title_of(rows: list[Any]) -> str:
    """What to call a conversation in a list.

    The visitor's own opening line, because that is what they will recognise.
    Not the category — an operator picks that later, and half of them are
    still blank when the visitor comes looking for the thread.
    """
    for m in rows:
        if m.sender_type == "user" and (m.text or "").strip():
            words = " ".join((m.text or "").split())
            return words[:60] + ("…" if len(words) > 60 else "")
    return ""


@router.get("/api/conversations")
async def conversations(
    request: Request,
    x_spooldesk_session: str | None = Header(default=None),
) -> JSONResponse:
    """Everything this visitor has ever asked, newest first."""
    origin = await _allowed_origin(request)
    if (off := await _off(origin)) is not None:
        return off

    token = (x_spooldesk_session or "").strip()
    if not token:
        # Not an error. A first-time visitor has no conversations, and making
        # the panel handle a 401 to learn that would mean showing an error
        # screen to someone who has done nothing wrong.
        return _json({"conversations": []}, origin)

    user = await _visitor(token)
    if user is None:
        return _json({"conversations": []}, origin)

    from src.dashboard.i18n import t as _dash_t

    out: list[dict[str, Any]] = []
    async with db_session() as session:
        for ticket in await intake_service.tickets_of(session, user.id):
            rows = await intake_service.conversation(session, ticket.id)
            if not rows:
                continue
            last = rows[-1]
            out.append({
                "id": ticket.id,
                "title": _title_of(rows) or _dash_t("wg_conversation"),
                "preview": " ".join((last.text or "").split())[:90],
                "from": "you" if last.sender_type == "user" else "agent",
                "at": last.created_at.isoformat() if last.created_at else None,
                "open": ticket.status == "active",
                "attachment": bool(last.file_id),
            })

    # By when they were last spoken in, not by when they were created. Ticket
    # ids run in insertion order, which is usually the same thing and was not
    # here: a conversation that finished last week outranked one from this
    # morning because its row happened to be written second. The list shows a
    # relative time against every row, so getting this wrong is visible.
    out.sort(key=lambda c: c["at"] or "", reverse=True)
    return _json({"conversations": out}, origin)


@router.get("/api/thread/{ticket_id}")
async def thread(
    ticket_id: int,
    request: Request,
    x_spooldesk_session: str | None = Header(default=None),
) -> JSONResponse:
    """One conversation from the list, including closed ones.

    The ownership check is the whole endpoint: ticket ids are small integers,
    so without it this is a way to read strangers' support conversations by
    counting.
    """
    origin = await _allowed_origin(request)
    if (off := await _off(origin)) is not None:
        return off

    token = (x_spooldesk_session or "").strip()
    if not token:
        return _json({"error": "no_session"}, origin, status.HTTP_401_UNAUTHORIZED)

    user = await _visitor(token)
    if user is None:
        return _json({"error": "no_session"}, origin, status.HTTP_401_UNAUTHORIZED)

    async with db_session() as session:
        mine = [t for t in await intake_service.tickets_of(session, user.id)
                if t.id == ticket_id]
    if not mine:
        return _json({"error": "not_found"}, origin, status.HTTP_404_NOT_FOUND)

    return _json({
        "ticket": ticket_id,
        "open": mine[0].status == "active",
        "messages": await _transcript(ticket_id),
    }, origin)


@router.get("/api/help")
async def help_articles(
    request: Request,
    q: str = "",
) -> JSONResponse:
    """Published articles, searched or listed.

    The only endpoint here that answers without a session, because a visitor
    reading documentation has not started a conversation and should not have
    to in order to find out whether they need one.
    """
    origin = await _allowed_origin(request)
    if (off := await _off(origin)) is not None:
        return off

    if _too_many(f"h:{_client_ip(request)}", _HELP_PER_IP, _HELP_WINDOW):
        return _json({"error": "slow_down"}, origin,
                     status.HTTP_429_TOO_MANY_REQUESTS)

    query = _clean(q, 120)
    found = (await article_service.search(query) if query
             else await article_service.published())
    return _json({
        "query": query,
        "articles": [
            {"slug": a.slug, "title": a.title, "summary": a.summary}
            for a in found
        ],
    }, origin)


@router.get("/api/help/{slug}")
async def help_article(slug: str, request: Request) -> JSONResponse:
    """One article, and the fact that somebody read it.

    Addressed by slug rather than id. An id would let anyone walk the table
    by counting, and would say how many drafts there are by how many numbers
    answer 404 — which is not a secret worth much, and is free not to leak.
    """
    origin = await _allowed_origin(request)
    if (off := await _off(origin)) is not None:
        return off

    if _too_many(f"h:{_client_ip(request)}", _HELP_PER_IP, _HELP_WINDOW):
        return _json({"error": "slow_down"}, origin,
                     status.HTTP_429_TOO_MANY_REQUESTS)

    article = await article_service.read(_clean(slug, 120))
    if article is None:
        # Unpublished and non-existent answer identically. An article being
        # written is not something a stranger should be able to detect.
        return _json({"error": "not_found"}, origin, status.HTTP_404_NOT_FOUND)

    return _json({
        "slug": article.slug,
        "title": article.title,
        "summary": article.summary,
        "body": article.body,
    }, origin)


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

    from src.services import work_hours_service

    greeting = await settings_service.get_widget_greeting()

    # Whether anyone is likely to answer soon, from the work-hours config the
    # instance already keeps. A widget that says "we usually reply in a few
    # minutes" at 3am is the one thing here that can make a support desk look
    # worse than having no widget at all.
    hours = await work_hours_service.get_config()
    working = (not hours.get("enabled")) or work_hours_service.is_working_now(hours)
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
        # Whether to put a form in front of the first message. The panel is
        # told rather than deciding: an operator changes this in settings and
        # it has to take effect on the next page view, not the next release.
        "ask_identity": await settings_service.get_widget_ask_identity(),
        "ask_intro": dash_t("wg_ask_intro"),
        "ask_name": dash_t("wg_ask_name"),
        "ask_email": dash_t("wg_ask_email"),
        "ask_start": dash_t("wg_ask_start"),
        "ask_skip": dash_t("wg_ask_skip"),
        "ask_bad_email": dash_t("wg_ask_bad_email"),
        # The panel asks rather than assuming, so a future instance that
        # cannot write to its store can turn this off and get a widget with
        # no paperclip instead of one whose paperclip fails.
        "files": True,
        "max_file_bytes": _MAX_UPLOAD_BYTES,
        "attach": dash_t("wg_attach"),
        "file_too_large": dash_t("wg_file_too_large"),
        "file_failed": dash_t("wg_file_failed"),
        "download": dash_t("wg_download"),
        "online": working,
        "subtitle": dash_t("wg_online" if working else "wg_offline_hours"),
        "conversation": dash_t("wg_conversation"),
        "nav_home": dash_t("wg_nav_home"),
        "nav_messages": dash_t("wg_nav_messages"),
        "contact": dash_t("wg_contact"),
        "recent": dash_t("wg_recent"),
        "no_conversations": dash_t("wg_no_conversations"),
        "closed": dash_t("wg_closed"),
        "new_chat": dash_t("wg_new_chat"),
        "back": dash_t("wg_back"),
        "you": dash_t("wg_you"),
        # Whether to draw the Help tab at all. A tab leading to an empty
        # search box does not read as "nothing written yet", it reads as
        # "the search is broken", so it stays away until there is something
        # to find.
        "help": await article_service.count_published() > 0,
        "nav_help": dash_t("wg_nav_help"),
        "help_search": dash_t("wg_help_search"),
        "help_none": dash_t("wg_help_none"),
        "help_no_hits": dash_t("wg_help_no_hits"),
        "help_still": dash_t("wg_help_still"),
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
