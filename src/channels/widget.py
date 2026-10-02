"""The chat bubble on a customer's own website.

The first channel that is not a messaging platform, and the differences are
instructive about what the seam actually has to absorb.

There is nothing to send to. Telegram has an API you push a message into;
the widget has a browser that asks. So delivery here is the transcript
itself: an operator's reply becomes a row, the page fetches rows, and
`send_text` has almost nothing to do. It still returns a Delivery, because
the reply path is not going to grow a special case for "channels where
sending is free".

That inverts unsend. Telegram gives 48 hours and then the copy in someone's
chat is permanent; the widget has no copy anywhere, so hiding the row hides
the message, forever, immediately. The one channel that could do this best
was also the one the old check refused, because it looked for a message id
the platform hands back and this platform has none.

Identity is a token the visitor's browser keeps. What is stored here is its
hash: a session token is a bearer credential, and a database that holds them
in the clear lets anyone who reads it walk into strangers' conversations.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
from collections.abc import Sequence

from src.channels.base import Capabilities, Delivery, Option
from src.services import file_store

logger = logging.getLogger(__name__)

NAME = "widget"

#: Long enough that guessing one is not a strategy. This is the only thing
#: standing between a visitor's conversation and anybody else's browser.
_TOKEN_BYTES = 32


def new_session_token() -> str:
    """A fresh bearer token for a visitor. Never stored as-is."""
    return secrets.token_urlsafe(_TOKEN_BYTES)


def address_for(token: str) -> str:
    """The stored address for a session token.

    A hash, so the column can be read without becoming a list of live
    credentials. The visitor keeps the token; we keep only enough to
    recognise it when it comes back.
    """
    return hashlib.sha256(token.encode()).hexdigest()


#: Legacy signature scope. New hosts should sign the complete v2 payload;
#: legacy proofs remain scoped only to this selected identifier.
def identity_subject(email: str | None, external_id: str | None) -> str:
    """The one string a signature is over.

    The host's own id wins when there is one: it is stable, and an email
    address changes. A site that passes both signs the id.
    """
    return (external_id or "").strip() or (email or "").strip().lower()


def sign_identity(secret: str, subject: str) -> str:
    """The signature a host's *server* computes. Never their browser.

    Published in the setup instructions as the one line a customer adds to
    their backend. If this is ever computed in page JavaScript the secret is
    in the page, and the whole mechanism becomes an elaborate way of
    believing whatever a visitor says.
    """
    return hmac.new(secret.encode(), subject.encode(), hashlib.sha256).hexdigest()


def verify_identity(secret: str, claimed: str | None, subject: str) -> bool:
    """Whether `claimed` really is this instance's signature over `subject`.

    Fails closed on every missing piece. No secret means no host has agreed
    to vouch for anyone, and answering True there would turn "we are not
    using this feature" into "everyone is verified".
    """
    if (not secret or not claimed or not subject
            or len(claimed.strip()) != 64
            or any(c not in "0123456789abcdef" for c in claimed.strip())):
        return False
    return hmac.compare_digest(sign_identity(secret, subject), claimed.strip())


def identity_payload_v2(*, name: str | None = None, email: str | None = None,
                        external_id: str | None = None) -> str:
    """Canonical UTF-8 JSON array signed by the host backend (never the page).

    Field order, normalization and limits are part of the wire contract.
    The prefix separates this signature from the old untyped subject HMAC.
    """
    return json.dumps([
        "spooldesk-identity-v2", (external_id or "").strip()[:120],
        (email or "").strip()[:320].lower(), (name or "").strip()[:60],
    ], ensure_ascii=False, separators=(",", ":"))


def sign_identity_v2(secret: str, *, name: str | None = None,
                     email: str | None = None, external_id: str | None = None) -> str:
    return "v2:" + sign_identity(secret, identity_payload_v2(
        name=name, email=email, external_id=external_id,
    ))


def verified_identity_fields(secret: str, claimed: str | None, *,
                             name: str | None, email: str | None,
                             external_id: str | None) -> tuple[str, ...]:
    """Precisely which supplied values the signature actually covers.

    Legacy hex signatures remain usable, but prove only their selected
    identifier. They cannot authorize changes to a name or a neighbouring
    email address. v2 binds all three fields together.
    """
    if not secret or not claimed or not identity_subject(email, external_id):
        return ()
    if claimed.startswith("v2:"):
        expected = sign_identity_v2(secret, name=name, email=email, external_id=external_id)
        if (len(claimed.strip()) != len(expected) or not claimed.isascii()
                or not hmac.compare_digest(expected, claimed.strip())):
            return ()
        return tuple(key for key, value in (
            ("name", name), ("email", email), ("external_id", external_id),
        ) if value)
    if verify_identity(secret, claimed, identity_subject(email, external_id)):
        return ("external_id",) if external_id else ("email",)
    return ()


def looks_like_email(value: str) -> bool:
    """A shape check, not a validity check, and the distinction is the point.

    Nothing here is confirming the address belongs to anyone — that is what
    the signature is for. This only keeps a paragraph of prose out of a field
    an operator will read as an address and possibly click.
    """
    value = (value or "").strip()
    if not value or len(value) > 320 or any(c.isspace() for c in value):
        return False
    local, sep, domain = value.rpartition("@")
    return bool(sep and local and "." in domain and not domain.startswith("."))


class WidgetChannel:
    """Delivery by transcript. The page asks; there is nothing to push."""

    name = NAME
    caps = Capabilities(
        unsend=True,
        unsend_by_row=True,
        # We keep the bytes ourselves. Every other channel hands back a
        # reference and stores the file for us; a browser cannot, so this is
        # the one adapter that owns a store — see src/services/file_store.py.
        files=True,
        # True, and this time the panel really can. The choices are stored on
        # the message that offered them (Message.options) and the panel draws
        # them from the transcript, which is the only memory a channel like
        # this has — Telegram keeps a keyboard attached to a message and
        # remembers which button was which; a list of rows does not.
        #
        # It said True once before while `send_text` silently dropped the
        # options, which is the combination this flag exists to prevent: a
        # message asking for a rating with every way of giving one hidden.
        buttons=True,
        typing=False,
        # Nothing reaches a visitor who has closed the tab. Worth declaring
        # rather than discovering: an SLA that assumes a notification landed
        # would be measuring something that never happened.
        push=False,
    )

    async def send_text(
        self, address: str, text: str, *, options: Sequence[Option] | None = None, reply_ref: str | None = None
    ) -> Delivery:
        # The caller writes the row; the page reads it. Returning no ref is
        # correct rather than lazy — there is no second id for this message
        # anywhere, and inventing one would put a fiction in the database.
        return Delivery.sent(None)

    async def send_file(
        self, address: str, *, filename: str, content: bytes, caption: str | None = None, reply_ref: str | None = None
    ) -> Delivery:
        """Keep the bytes; the page will come and ask for them.

        Like `send_text`, nothing is pushed. The difference is that a file has
        to survive until the visitor next opens the panel, which may be
        tomorrow, so it goes to disk rather than being handed to an API that
        would have held it for us.
        """
        try:
            ref = file_store.put(content)
        except OSError as e:  # noqa: BLE001 — a full or read-only disk
            logger.error(f"Could not store an attachment for {address}: {e}")
            return Delivery.failed("e_send_failed", str(e))
        # No message ref: there is no second copy of this anywhere to address.
        # The file ref is what `fetch_file` will want back.
        return Delivery(ok=True, ref=None, file_ref=ref)

    async def fetch_file(self, ref: str) -> bytes | None:
        return file_store.get(ref)

    async def unsend(self, address: str, ref: str) -> bool:
        # `delete_for_user` marks the row, and the page stops rendering it on
        # its next poll. There is no third party to inform.
        return True

    async def typing(self, address: str) -> None:
        pass
