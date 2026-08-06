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
import logging
import secrets
from collections.abc import Sequence

from src.channels.base import Capabilities, Delivery, Option

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


class WidgetChannel:
    """Delivery by transcript. The page asks; there is nothing to push."""

    name = NAME
    caps = Capabilities(
        unsend=True,
        unsend_by_row=True,
        files=False,
        buttons=True,
        typing=False,
        # Nothing reaches a visitor who has closed the tab. Worth declaring
        # rather than discovering: an SLA that assumes a notification landed
        # would be measuring something that never happened.
        push=False,
    )

    async def send_text(
        self, address: str, text: str, *, options: Sequence[Option] | None = None
    ) -> Delivery:
        # The caller writes the row; the page reads it. Returning no ref is
        # correct rather than lazy — there is no second id for this message
        # anywhere, and inventing one would put a fiction in the database.
        return Delivery.sent(None)

    async def send_file(
        self, address: str, *, filename: str, content: bytes, caption: str | None = None
    ) -> Delivery:
        # Declared as unsupported in `caps`, so nothing should reach here.
        # Answering honestly anyway beats a traceback if something does.
        return Delivery.failed("e_no_files_on_channel")

    async def unsend(self, address: str, ref: str) -> bool:
        # `delete_for_user` marks the row, and the page stops rendering it on
        # its next poll. There is no third party to inform.
        return True

    async def typing(self, address: str) -> None:
        pass
