"""What a channel is, and what the rest of the system may assume about one.

A channel is the pipe between a customer and this helpdesk: Telegram today,
a site widget and email next. Everything above this line — tickets, SLA, AI,
reports, the dashboard — is supposed to work the same whichever pipe a
conversation arrived through.

Two rules keep that true, and both are here rather than in the adapters:

Channel-native errors stop at the adapter. Callers used to catch
`TelegramForbiddenError` by name, which quietly made every one of them a
Telegram caller. A send now returns a `Delivery` describing what happened in
words the rest of the system already speaks, and the adapter is the only
place that knows what the underlying library raises.

Channels are not equal, and the difference is declared rather than
discovered. Telegram can unsend a message; email cannot. Asking and being
told no is fine; finding out from an exception after the operator clicked
"delete" is not. `Capabilities` is what the UI reads to avoid offering
something that will fail.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class Capabilities:
    """What this channel can actually do.

    Deliberately conservative defaults: a new adapter that forgets to declare
    anything gets a channel that only sends text, which degrades the UI rather
    than promising something the adapter cannot honour.
    """

    #: Messages we sent can be withdrawn from the customer's view.
    unsend: bool = False
    #: The channel has no separate copy of a message: it renders the
    #: transcript, so hiding the row hides the message. Such channels are
    #: addressed by the row itself rather than by an id the platform handed
    #: back, and they have no time limit — there is nothing sitting in
    #: somebody's chat history to expire.
    unsend_by_row: bool = False
    #: Attachments can be delivered.
    files: bool = False
    #: Tappable options can be attached to a message — CSAT asks for a rating
    #: this way, and falls back to asking in words where it cannot.
    buttons: bool = False
    #: A "typing…" hint can be shown while an operator writes.
    typing: bool = False
    #: Messages arrive on their own; nothing has to poll or be invited in.
    #: False for email until a mailbox is configured, and for the widget until
    #: someone opens the page.
    push: bool = True


@dataclass(frozen=True)
class Option:
    """A choice offered with a message: a star rating, yes/no, a category.

    `payload` is what comes back when the customer picks it, and it is opaque
    to everything between here and the handler that parses it. Telegram puts
    it in callback_data; a widget posts it as a form field; email would put it
    in a link. Keep it short — Telegram caps callback_data at 64 bytes, which
    is the tightest limit any channel is likely to impose.
    """

    label: str
    payload: str


@dataclass(frozen=True)
class Delivery:
    """The outcome of one outbound message.

    `ref` is the channel's own id for what was sent, as a string, and it is
    the only thing that makes an unsend possible later — most channels address
    a message by (conversation, id) and neither part survives the call.

    `error_key` is an i18n key rather than a sentence, because the same
    failure has to be readable to an operator in either locale. `blocked`
    separates "this customer has shut us out" from "the send failed": the
    first is worth showing on their card, the second is worth retrying.
    """

    ok: bool
    ref: str | None = None
    error_key: str | None = None
    error_detail: str | None = None
    blocked: bool = False
    #: Seconds the channel asked us to wait before trying again. Every
    #: messaging API has a rate limit and most will say how long they want;
    #: guessing instead of obeying is how an account gets throttled harder.
    retry_after: float | None = None

    @staticmethod
    def sent(ref: str | int | None = None) -> "Delivery":
        return Delivery(ok=True, ref=str(ref) if ref is not None else None)

    @staticmethod
    def failed(
        error_key: str,
        detail: str | None = None,
        *,
        blocked: bool = False,
        retry_after: float | None = None,
    ) -> "Delivery":
        return Delivery(ok=False, error_key=error_key, error_detail=detail,
                        blocked=blocked, retry_after=retry_after)


@runtime_checkable
class Channel(Protocol):
    """The pipe to one customer, in one medium.

    Addresses are strings on purpose. A Telegram chat id is a number, an email
    address is not, and a widget session is a token; making the seam carry the
    narrowest of those would push every other channel into a workaround.
    """

    #: Stable identifier, stored on rows and shown in the UI. Never renamed:
    #: it is written into the database next to every customer.
    name: str
    caps: Capabilities

    async def send_text(
        self, address: str, text: str, *, options: Sequence[Option] | None = None
    ) -> Delivery:
        """Send text, optionally offering choices.

        A channel without `caps.buttons` must still honour `options` somehow
        rather than dropping them — a closing message that silently loses its
        rating request is worse than an ugly one that keeps it. Spelling them
        out in the text is the expected fallback.
        """
        ...

    async def send_file(
        self, address: str, *, filename: str, content: bytes, caption: str | None = None
    ) -> Delivery: ...

    async def fetch_file(self, ref: str) -> bytes | None:
        """The bytes behind an attachment a customer sent us, or None.

        Attachments are stored as a reference, not as bytes: a Telegram
        file_id, a Slack private URL. Getting them back is channel work —
        Telegram wants a two-step Bot API call, Slack wants an HTTP GET with
        a bearer token — and the dashboard should not know which.

        None rather than an exception, because the caller's honest answer to
        a failed fetch is the same either way: this attachment cannot be
        shown right now.
        """
        ...

    async def unsend(self, address: str, ref: str) -> bool:
        """Withdraw a message. Returns False when it could not be withdrawn —
        including when this channel never could, so callers need not check
        `caps.unsend` first to stay correct, only to stay polite."""
        ...

    async def typing(self, address: str) -> None:
        """Best effort, always. A failed typing hint is not worth a log line,
        let alone an error shown to the operator mid-sentence."""
        ...
