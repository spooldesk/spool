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


class SlackChannel:
    """Posts into a Slack conversation over the Web API."""

    name = NAME
    caps = Capabilities(
        # `chat.delete` works for the bot's own messages with no time limit,
        # which is more than Telegram allows.
        unsend=True,
        # Uploading needs a second, quite different API call and a scope most
        # installs will not have granted. Declared false rather than failing
        # at the moment an operator attaches something.
        files=False,
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
        return Delivery.failed("e_no_files_on_channel")

    async def unsend(self, address: str, ref: str) -> bool:
        ok, data = await self._post("chat.delete", {"channel": address, "ts": ref})
        if not ok:
            logger.warning(
                f"chat.delete refused for {address}/{ref}: {data.get('error')}"
            )
        return ok

    async def typing(self, address: str) -> None:
        pass
