"""Slack's Events API, arriving at a public URL.

The widget's endpoints protect themselves with rate limits, because anyone
may legitimately talk to them. This one is the opposite: exactly one caller
is legitimate, and it proves it cryptographically. Everything that fails the
proof is refused before a single row is read.

Three things are easy to get wrong here and expensive to get wrong quietly:

Signature verification has to cover the raw body, byte for byte. Parsing the
JSON first and re-serialising it to check the signature would pass for
well-formed forgeries and fail for legitimate messages containing anything
Python spells differently from Go.

A timestamp check is not optional. Without it a signature stays valid
forever, so one captured request can be replayed at any point in the future.

Our own messages must never come back in. Slack delivers every message in a
conversation, including the ones we posted, so an adapter that answers
anything it sees will answer itself until somebody notices the bill.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import time
from collections import OrderedDict

from fastapi import APIRouter, Request, Response, status
from fastapi.responses import JSONResponse, PlainTextResponse

from src.channels.slack import NAME as SLACK, is_configured, signing_secret
from src.database.db import db_session
from src.services import intake_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/slack", tags=["slack"])

#: Slack's own recommendation. Long enough to survive a slow network, short
#: enough that a captured request is worthless by the time it is useful.
_MAX_AGE_SECONDS = 60 * 5

#: Slack retries anything it does not get a prompt 2xx for, so the same
#: message can arrive three times. Kept in memory rather than a table: the
#: retries land within seconds, and a table for that would be a migration and
#: a cleanup job in exchange for closing a window measured in milliseconds.
#: The cost of losing it in a restart is one duplicated message inside an
#: existing ticket — not a duplicated ticket, since intake appends.
_SEEN_LIMIT = 2000
_seen: "OrderedDict[str, float]" = OrderedDict()


def _already_handled(event_id: str) -> bool:
    if not event_id:
        return False
    if event_id in _seen:
        return True
    _seen[event_id] = time.time()
    while len(_seen) > _SEEN_LIMIT:
        _seen.popitem(last=False)
    return False


def _signature_ok(body: bytes, timestamp: str, signature: str) -> bool:
    """Whether this request really came from Slack.

    `body` is the raw bytes off the wire. Re-serialising parsed JSON here
    would be a different string, and the signature would stop matching for
    reasons nobody could reproduce.
    """
    secret = signing_secret()
    if not (secret and timestamp and signature):
        return False

    try:
        age = abs(time.time() - int(timestamp))
    except ValueError:
        return False
    if age > _MAX_AGE_SECONDS:
        # A replayed request. The signature is still perfectly valid, which is
        # exactly why the clock has to be part of the check.
        logger.warning(f"Slack request rejected: timestamp {age:.0f}s old")
        return False

    expected = "v0=" + hmac.new(
        secret.encode(),
        b"v0:" + timestamp.encode() + b":" + body,
        hashlib.sha256,
    ).hexdigest()
    # Constant-time, so a wrong signature cannot be improved a byte at a time
    # by watching how long the answer takes.
    return hmac.compare_digest(expected, signature)


async def _speaker(event: dict) -> str | None:
    """A name for whoever wrote this.

    Three sources, cheapest first. Slack sometimes inlines the profile, in
    which case nothing needs asking. Otherwise the adapter looks the id up
    and remembers it. If the app was never granted `users:read` the answer is
    the raw id — which is not a name, but it still tells two people in a
    shared channel apart, and that is the label's actual job.
    """
    profile = (event.get("user_profile") or {})
    for key in ("display_name", "real_name"):
        value = (profile.get(key) or "").strip()
        if value:
            return value[:80]

    user = (event.get("user") or "").strip()
    if not user:
        return None
    try:
        from src.channels import get as get_channel

        resolved = await get_channel(SLACK).display_name(user)
    except (LookupError, AttributeError):
        resolved = None
    return (resolved or user)[:80]


def _attachments(event: dict) -> list[intake_service.Attachment]:
    """Files that came with the message.

    `url_private` is stored rather than the file id, because that is what the
    fetch needs later and Slack will not turn one into the other without
    another call. It is not public despite being a URL — it needs the bot
    token — so keeping it is no more exposure than keeping the id.
    """
    out = []
    for f in (event.get("files") or []):
        url = (f.get("url_private") or "").strip()
        if not url:
            # An external or hidden file: Slack shows a card, we get no bytes.
            continue
        out.append(intake_service.Attachment(
            ref=url,
            name=(f.get("name") or "file")[:512],
            mime_type=(f.get("mimetype") or None),
            size=f.get("size"),
            kind="document",
        ))
    return out


def _is_ours(event: dict) -> bool:
    """Whether we said this.

    Both checks matter: `bot_id` catches messages posted by any app, and
    `bot_message` catches the subtype Slack uses in some conversation types.
    Missing either one is an infinite loop with a bill attached.
    """
    return bool(event.get("bot_id")) or event.get("subtype") == "bot_message"


@router.post("/events")
async def events(request: Request) -> Response:
    """Everything Slack sends: the setup handshake and then real messages."""
    if not is_configured():
        # 404 rather than 403: an instance with no Slack app installed should
        # look like it has no Slack endpoint.
        return JSONResponse({"error": "not_configured"},
                            status_code=status.HTTP_404_NOT_FOUND)

    body = await request.body()
    if not _signature_ok(
        body,
        request.headers.get("x-slack-request-timestamp", ""),
        request.headers.get("x-slack-signature", ""),
    ):
        return JSONResponse({"error": "bad_signature"},
                            status_code=status.HTTP_401_UNAUTHORIZED)

    try:
        payload = await request.json()
    except ValueError:
        return JSONResponse({"error": "bad_json"},
                            status_code=status.HTTP_400_BAD_REQUEST)

    # The one-time handshake when the URL is entered in the Slack app config.
    # Answered as plain text because that is what Slack's checker looks for.
    if payload.get("type") == "url_verification":
        return PlainTextResponse(payload.get("challenge", ""))

    if payload.get("type") != "event_callback":
        return JSONResponse({"ok": True})

    # Answered before anything else is examined. Slack decides an endpoint is
    # unhealthy on slow replies and starts retrying, which is how one message
    # becomes three.
    if _already_handled(payload.get("event_id", "")):
        return JSONResponse({"ok": True})

    event = payload.get("event") or {}
    if event.get("type") != "message" or _is_ours(event):
        return JSONResponse({"ok": True})
    # Edits, deletions, joins and thread broadcasts all arrive as messages
    # with a subtype. Treating an edit as a new message would show an
    # operator the same question twice with one word changed.
    if event.get("subtype"):
        return JSONResponse({"ok": True})

    conversation = (event.get("channel") or "").strip()
    text = (event.get("text") or "").strip()
    files = _attachments(event)
    if not conversation or not (text or files):
        return JSONResponse({"ok": True})

    speaker = await _speaker(event)
    # A direct message is one person, so they are the customer and repeating
    # their name on every line is noise. A shared channel is a room: the
    # customer is the room, and every line needs an author.
    is_dm = event.get("channel_type") == "im"

    def inbound(body: str, attachment=None) -> intake_service.Inbound:
        return intake_service.Inbound(
            channel=SLACK,
            # The conversation, not the person. A DM and a channel shared with
            # a client's whole team are the same thing to Slack, and making
            # them the same thing here is what lets one adapter serve both.
            address=conversation,
            text=body,
            display_name=speaker if is_dm else f"Slack {conversation}",
            speaker=None if is_dm else speaker,
            attachment=attachment,
        )

    async with db_session() as session:
        # One Slack message can carry several files. Recorded as one row each,
        # with the text on the first — which is how Slack itself shows them,
        # and it keeps every attachment individually openable.
        result = await intake_service.receive(
            session, inbound(text, files[0] if files else None)
        )
        for extra in files[1:]:
            await intake_service.receive(session, inbound("", extra))
        await session.commit()

    if result.rejected:
        logger.info(f"Slack message from {conversation} not accepted: {result.rejected}")
    else:
        logger.info(
            f"Slack message on ticket #{result.ticket_id}"
            f"{' (new conversation)' if result.opened_ticket else ''}"
        )
    # Always 200. Telling Slack an event failed makes it send the event again,
    # and nothing above is worth repeating — a refusal is a decision, not an
    # outage.
    return JSONResponse({"ok": True})
