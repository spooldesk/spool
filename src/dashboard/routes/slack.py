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
import json
import logging
import time
import urllib.parse

from fastapi import APIRouter, Request, Response, status
from fastapi.responses import JSONResponse, PlainTextResponse
from starlette.background import BackgroundTask

from src.channels.slack import NAME as SLACK, is_configured, signing_secret
from src.database.db import db_session
from src.services import intake_service, inbound_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/slack", tags=["slack"])

#: Slack's own recommendation. Long enough to survive a slow network, short
#: enough that a captured request is worthless by the time it is useful.
_MAX_AGE_SECONDS = 60 * 5

_MAX_EVENT_BYTES = 1024 * 1024


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


@router.post("/interactive")
async def interactive(request: Request) -> Response:
    """Somebody clicked a button we sent into Slack.

    Slack posts these as a form field holding URL-encoded JSON, not as a JSON
    body — a detail with no reason behind it that costs an afternoon the
    first time.

    The signature is the security boundary here, not the payload: it proves
    Slack sent this. What the payload then has to prove is that the button
    belongs to the conversation it claims, which is what the channel check
    below is for — otherwise anybody in any Slack channel the app is in could
    rate somebody else's ticket by replaying a value.
    """
    raw = await request.body()
    if not _signature_ok(
        raw,
        request.headers.get("X-Slack-Request-Timestamp", ""),
        request.headers.get("X-Slack-Signature", ""),
    ):
        return Response(status_code=status.HTTP_401_UNAUTHORIZED)

    form = urllib.parse.parse_qs(raw.decode("utf-8", "replace"))
    try:
        body = json.loads((form.get("payload") or ["{}"])[0])
    except json.JSONDecodeError:
        return Response(status_code=status.HTTP_400_BAD_REQUEST)

    actions = body.get("actions") or []
    if not actions:
        # A shortcut, a modal, a view submission — none of which this
        # instance sends, so none of which it has to answer.
        return Response(status_code=status.HTTP_200_OK)

    action = actions[0]
    payload = (action.get("value") or action.get("action_id") or "").strip()
    conversation = ((body.get("channel") or {}).get("id") or "").strip()
    message_ts = (
        (body.get("message") or {}).get("ts")
        or (body.get("container") or {}).get("message_ts")
        or ""
    ).strip()

    if not (payload and conversation and message_ts):
        return Response(status_code=status.HTTP_200_OK)

    await _apply_slack_choice(conversation, message_ts, payload)
    # Always 200, and quickly. Slack shows the user a red warning on anything
    # else, and "your rating was refused" is not a useful thing to tell
    # somebody who clicked a star.
    return Response(status_code=status.HTTP_200_OK)


async def _apply_slack_choice(conversation: str, message_ts: str, payload: str) -> None:
    """Honour a click, if the message and the conversation agree about whose
    it is."""
    from sqlalchemy import select

    from src.channels.identity import address_of
    from src.database.db import db_session
    from src.database.models import Message as MessageRecord, Ticket, User
    from src.services import choice_service

    try:
        async with db_session() as session:
            row = (await session.execute(
                select(MessageRecord)
                .where(MessageRecord.channel_ref == message_ts)
                .order_by(MessageRecord.id.desc())
            )).scalars().first()
            if row is None or not row.options:
                # Already answered, or a message from before this existed.
                return

            ticket = await session.get(Ticket, row.ticket_id)
            customer = await session.get(User, ticket.user_id) if ticket else None
            if customer is None:
                return

            if address_of(customer) != conversation:
                # The button was clicked somewhere other than the conversation
                # it belongs to. Slack's signature proves the request is real,
                # not that the person is entitled to this ticket.
                logger.warning(
                    f"Slack choice rejected: message #{row.id} belongs to "
                    f"another conversation"
                )
                return

            outcome = await choice_service.apply(
                session, message_id=row.id, payload=payload,
                user_tg_id=customer.tg_id,
            )
            await session.commit()

        if outcome.ok:
            logger.info(f"Slack choice honoured on ticket #{ticket.id}")
    except Exception as e:  # noqa: BLE001 — never fail a Slack callback
        logger.error(f"Could not apply a Slack choice: {e}", exc_info=True)


@router.post("/events")
async def events(request: Request) -> Response:
    """Acknowledge only after raw input has a durable, deduplicated receipt."""
    if not is_configured():
        return JSONResponse({"error": "not_configured"}, status_code=404)
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > _MAX_EVENT_BYTES:
            return JSONResponse({"error": "too_large"}, status_code=413)
        chunks.append(chunk)
    body = b"".join(chunks)
    if not _signature_ok(body, request.headers.get("x-slack-request-timestamp", ""),
                         request.headers.get("x-slack-signature", "")):
        return JSONResponse({"error": "bad_signature"}, status_code=401)
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return JSONResponse({"error": "bad_json"}, status_code=400)
    if not isinstance(payload, dict):
        return JSONResponse({"error": "bad_json"}, status_code=400)
    if payload.get("type") == "url_verification":
        return PlainTextResponse(str(payload.get("challenge", "")))
    if payload.get("type") != "event_callback":
        return JSONResponse({"ok": True})
    event = payload.get("event") or {}
    if not isinstance(event, dict) or event.get("type") != "message" or _is_ours(event):
        return JSONResponse({"ok": True})
    # file_share is an ordinary customer message, including attachment-only
    # messages. Edits/deletions are distinct operations, not new intake.
    mutation = event.get("subtype") in {"message_changed", "message_deleted"}
    if event.get("subtype") not in (None, "", "file_share", "message_changed", "message_deleted"):
        return JSONResponse({"ok": True})
    if not event.get("channel") or not (mutation or event.get("text") or event.get("files")):
        return JSONResponse({"ok": True})
    identity = str(payload.get("event_id") or hashlib.sha256(body).hexdigest())
    source_key = hashlib.sha256((str(payload.get("team_id") or "") + ":" + identity).encode()).hexdigest()
    try:
        event_id = await inbound_service.enqueue(channel=SLACK, source_key=source_key,
                                                  payload=body)
    except Exception:
        # Slack retries this response. A 200 before persistence would lose an
        # event if the process died while a background task was starting.
        logger.warning("Slack event could not be persisted")
        return JSONResponse({"error": "temporarily_unavailable"}, status_code=503)
    return JSONResponse({"ok": True}, background=BackgroundTask(inbound_service.process, event_id))


async def prepare_event(raw: bytes, context: dict) -> dict:
    """Potential network work happens before the processing transaction."""
    payload = json.loads(raw)
    event = payload["event"]
    return {"event": event, "speaker": await _speaker(event), "team": payload.get("team_id") or ""}


async def record_event(session, prepared: dict, context: dict, *, event_id: int):
    """All attachments and the processed receipt share the caller's commit."""
    from src.services.inbound_service import Recorded

    event, speaker = prepared["event"], prepared["speaker"]
    conversation = str(event.get("channel") or "").strip()
    if event.get("subtype") in {"message_changed", "message_deleted"}:
        from src.services.channel_events import update_customer_message
        message = event.get("message") or {}
        ref = str(event.get("deleted_ts") or message.get("ts") or '')
        if _is_ours(message) or not conversation or not ref:
            return Recorded(rejected="ignored")
        mid = await update_customer_message(session, channel=SLACK, address=conversation,
            ref=ref, text=str(message.get("text") or ''),
            deleted=event.get("subtype") == "message_deleted")
        return Recorded(message_id=mid or 0, rejected="mutation")
    text = str(event.get("text") or "").strip()
    files = _attachments(event)
    if not conversation or not (text or files):
        return Recorded(rejected="empty")
    is_dm = event.get("channel_type") == "im"
    # A shared channel's root message starts a ticket; thread replies join it.
    # DMs retain one active conversation, including replies threaded in Slack.
    root_ts = "" if is_dm else (event.get("thread_ts") or event.get("ts") or "")
    conversation_key = "slack:" + str(root_ts) if root_ts else ""

    def inbound(body: str, attachment=None):
        return intake_service.Inbound(
            channel=SLACK, address=conversation, text=body,
            display_name=speaker if is_dm else f"Slack {conversation}",
            speaker=None if is_dm else speaker, attachment=attachment,
            conversation_key=conversation_key,
            channel_ref=str(event.get("ts") or '') or None,
            reply_to_ref=str(event.get("thread_ts") or '') or None,
        )

    result = await intake_service.receive(session, inbound(text, files[0] if files else None))
    if result.rejected:
        return Recorded(rejected=result.rejected)
    ids = [result.message_id]
    for extra in files[1:]:
        additional = await intake_service.receive(session, inbound("", extra))
        if additional.rejected:
            raise RuntimeError("attachment intake refused after first message")
        ids.append(additional.message_id)
    return Recorded(result.ticket_id, result.message_id, result.opened_ticket,
                    message_ids=tuple(ids))
