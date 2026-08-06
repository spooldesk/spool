"""Slack as a customer channel.

Not as an operator surface. There is a version of this feature where tickets
get posted into your own team's Slack and operators answer from there —
useful, and a different feature. This one is the other direction: a client
writes to your Slack app, and it becomes a ticket like any other.

The address is a conversation id, not a user id. Slack calls a DM and a
shared channel the same thing, and posting to either means posting to a
conversation. Choosing the conversation as the address makes both work with
one adapter, and it makes the shape people actually want in B2B — a Slack
Connect channel shared with a client's whole team — fall out for free rather
than needing a second design.

It also creates the one problem this file cannot solve alone: five people in
a shared channel are one conversation but five authors. Who said what is
carried on the message (`Message.sender_label`), not on the customer.

Credentials come from the environment, like the Telegram token, and never
from the settings UI. A signing secret shown on a web page is a signing
secret in a screenshot.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Sequence

import httpx

from src.channels.base import Capabilities, Delivery, Option

logger = logging.getLogger(__name__)

NAME = "slack"

_API = "https://slack.com/api"
#: Slack is fast when it is fine and slow when it is not; a reply that hangs
#: for thirty seconds has already failed as far as the operator is concerned.
_TIMEOUT = 10.0
#: Uploads carry bytes over a network the operator is waiting on, so they
#: get room that a text call does not need.
_UPLOAD_TIMEOUT = 60.0


def bot_token() -> str:
    return (os.getenv("SLACK_BOT_TOKEN") or "").strip()


def signing_secret() -> str:
    return (os.getenv("SLACK_SIGNING_SECRET") or "").strip()


def is_configured() -> bool:
    """Both halves or neither.

    A token without a signing secret can send but cannot safely receive, and
    an instance in that state looks like it is working right up until someone
    posts a forged event at it.
    """
    return bool(bot_token() and signing_secret())


def _blocks(text: str, options: Sequence[Option] | None) -> list[dict] | None:
    """Choices as Block Kit buttons.

    `action_id` carries the payload, because that is the field Slack echoes
    back on a click and the one the rest of the system already parses. `value`
    would work too and is capped shorter.
    """
    if not options:
        return None
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": text}},
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": o.label, "emoji": True},
                    "action_id": o.payload,
                    "value": o.payload,
                }
                for o in options[:5]
            ],
        },
    ]


def _is_slack_file(url: str) -> bool:
    """Whether this is somewhere Slack would actually host a file.

    The reference comes out of an event payload, and the fetch carries our
    bearer token — so an unrestricted fetch would be a way to make the server
    reach an address of somebody else's choosing with a credential attached.
    Forging such an event needs the signing secret, which makes this unlikely
    rather than impossible; pinning the host closes it either way, and costs
    nothing because `url_private` is always on Slack's own domain.
    """
    from urllib.parse import urlparse

    parsed = urlparse(url or "")
    if parsed.scheme != "https":
        return False
    host = (parsed.hostname or "").lower()
    return host == "slack.com" or host.endswith(".slack.com")


class SlackChannel:
    """Posts into a Slack conversation over the Web API."""

    name = NAME
    caps = Capabilities(
        # `chat.delete` works for the bot's own messages with no time limit,
        # which is more than Telegram allows.
        unsend=True,
        # Slack can carry files, but only if the app was granted `files:write`
        # — and a scope is a fact about somebody's install, not about the
        # medium. Declared true and demoted on the first refusal; see
        # `_scope_missing`.
        files=True,
        buttons=True,
        # Slack has a typing indicator only over the old RTM protocol, which
        # is deprecated. Saying no is better than pretending.
        typing=False,
        push=True,
    )

    def __init__(self, token: str | None = None) -> None:
        # Read once at construction, so a process that started without a token
        # fails the same way on every send instead of intermittently.
        self._token = token if token is not None else bot_token()
        # Names for user ids, once we have paid for them. Slack ids are stable
        # forever and display names change about as often as people change
        # jobs, so this is worth keeping and not worth expiring carefully.
        self._names: dict[str, str] = {}

    def _scope_missing(self, what: str) -> None:
        """Stop advertising something this install was not permitted to do.

        `caps` is a class attribute; assigning here shadows it on the one
        instance in the registry, which is what `channels.caps("slack")`
        reads. So the attachment button disappears from the composer after
        the first refusal instead of failing again for every operator.
        """
        from dataclasses import replace

        if what == "files" and self.caps.files:
            self.caps = replace(self.caps, files=False)
            logger.warning(
                "Slack app lacks the files:write scope; attachments disabled "
                "until it is granted and the process restarts"
            )

    async def _post(self, method: str, payload: dict) -> tuple[bool, dict]:
        """One Web API call, with Slack's two ways of saying no folded into one.

        Slack answers 200 with `{"ok": false, "error": "..."}` far more often
        than it answers a failure status, so a caller checking only the HTTP
        code would treat almost every error as a success.
        """
        if not self._token:
            return False, {"error": "not_configured"}
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                response = await client.post(
                    f"{_API}/{method}",
                    json=payload,
                    headers={"Authorization": f"Bearer {self._token}"},
                )
        except httpx.HTTPError as e:
            return False, {"error": f"{type(e).__name__}"}

        try:
            data = response.json()
        except ValueError:
            return False, {"error": f"http_{response.status_code}"}
        return bool(data.get("ok")), data

    # -- outbound ---------------------------------------------------------

    async def send_text(
        self, address: str, text: str, *, options: Sequence[Option] | None = None
    ) -> Delivery:
        payload: dict = {"channel": address, "text": text}
        blocks = _blocks(text, options)
        if blocks:
            # `text` stays as well: it is what a notification shows, and a
            # blocks-only message arrives on a phone as a blank line.
            payload["blocks"] = blocks

        ok, data = await self._post("chat.postMessage", payload)
        if ok:
            return Delivery.sent(data.get("ts"))

        error = str(data.get("error") or "unknown")
        # The ones worth telling apart, because they mean different things to
        # whoever has to fix them.
        if error in {"channel_not_found", "not_in_channel", "is_archived"}:
            return Delivery.failed("e_slack_no_conversation", error, blocked=True)
        if error in {"invalid_auth", "not_authed", "token_revoked", "account_inactive"}:
            return Delivery.failed("e_slack_auth", error)
        if error == "ratelimited":
            # Slack puts the delay in a header, which `_post` does not keep.
            # A conservative fixed wait beats guessing zero.
            return Delivery.failed("e_send_rate_limited", error, retry_after=30.0)
        if error == "not_configured":
            return Delivery.failed("e_slack_auth", error)
        return Delivery.failed("e_send_failed", f"slack: {error}")

    async def send_file(
        self, address: str, *, filename: str, content: bytes, caption: str | None = None
    ) -> Delivery:
        """Three calls, because that is what Slack now requires.

        `files.upload` was the one-call way and was retired in 2025. The
        replacement hands out a URL, takes the bytes over plain HTTP, and then
        wants a third call to say where the file belongs. Nothing is visible
        to the customer until that third call, which is the one mercy in it:
        a failure halfway leaves an orphan, not a half-delivered message.
        """
        if not self.caps.files:
            return Delivery.failed("e_no_files_on_channel")

        ok, data = await self._post("files.getUploadURLExternal", {
            "filename": filename, "length": len(content),
        })
        if not ok:
            return self._file_error(data)

        upload_url, file_id = data.get("upload_url"), data.get("file_id")
        if not (upload_url and file_id):
            return Delivery.failed("e_send_failed", "slack: no upload url")

        try:
            async with httpx.AsyncClient(timeout=_UPLOAD_TIMEOUT) as client:
                uploaded = await client.post(upload_url, files={"file": (filename, content)})
            if uploaded.status_code >= 400:
                return Delivery.failed("e_send_failed", f"slack upload: {uploaded.status_code}")
        except httpx.HTTPError as e:
            return Delivery.failed("e_send_failed", type(e).__name__)

        ok, data = await self._post("files.completeUploadExternal", {
            "files": [{"id": file_id, "title": filename}],
            "channel_id": address,
            **({"initial_comment": caption} if caption else {}),
        })
        if not ok:
            return self._file_error(data)

        # The message id, so the reply can be unsent like any other. Slack
        # returns it nested under the file it just shared.
        shared = (data.get("files") or [{}])[0]
        return Delivery.sent(shared.get("timestamp") or data.get("ts"))

    def _file_error(self, data: dict) -> Delivery:
        error = str(data.get("error") or "unknown")
        if error in {"missing_scope", "not_allowed_token_type"}:
            self._scope_missing("files")
            return Delivery.failed("e_slack_files_scope", error)
        if error in {"invalid_auth", "not_authed", "token_revoked"}:
            return Delivery.failed("e_slack_auth", error)
        return Delivery.failed("e_send_failed", f"slack: {error}")

    async def fetch_file(self, ref: str) -> bytes | None:
        """A private Slack URL back into bytes.

        `url_private` is not public despite being a URL: without the bearer
        token Slack answers 200 with an HTML sign-in page, which is the sort
        of failure that gets stored as a corrupt image and puzzled over.
        """
        if not (self._token and _is_slack_file(ref)):
            return None
        try:
            async with httpx.AsyncClient(timeout=_UPLOAD_TIMEOUT) as client:
                response = await client.get(
                    ref, headers={"Authorization": f"Bearer {self._token}"},
                    follow_redirects=True,
                )
        except httpx.HTTPError as e:
            logger.warning(f"Could not fetch Slack file: {type(e).__name__}")
            return None

        if response.status_code >= 400:
            logger.warning(f"Slack refused a file download: {response.status_code}")
            return None
        # The sign-in page, served with a 200. Treated as a failure because
        # that is what it is.
        if response.headers.get("content-type", "").startswith("text/html"):
            logger.warning("Slack returned a sign-in page instead of a file")
            return None
        return response.content

    async def display_name(self, user_id: str) -> str | None:
        """A person's name, if the app was allowed to ask.

        Needs `users:read`. Without it every message is labelled with a raw
        id, which still tells two people apart — the point of the label — so
        a missing scope degrades rather than breaks.
        """
        user_id = (user_id or "").strip()
        if not user_id:
            return None
        if user_id in self._names:
            return self._names[user_id]

        ok, data = await self._post("users.info", {"user": user_id})
        if not ok:
            # Remembered as unresolvable, so a missing scope costs one call
            # per person rather than one call per message.
            self._names[user_id] = user_id
            return None

        profile = (data.get("user") or {}).get("profile") or {}
        name = next(
            (v.strip() for k in ("display_name", "real_name")
             if (v := profile.get(k))),
            "",
        )
        resolved = name[:80] or user_id
        self._names[user_id] = resolved
        return resolved if name else None

    async def unsend(self, address: str, ref: str) -> bool:
        ok, data = await self._post("chat.delete", {"channel": address, "ts": ref})
        if not ok:
            logger.warning(
                f"chat.delete refused for {address}/{ref}: {data.get('error')}"
            )
        return ok

    async def typing(self, address: str) -> None:
        pass
