"""Support by email: the oldest channel, and the one with the fewest promises.

What email cannot do shapes this adapter more than what it can. There is no
unsend — a message is on somebody's server the moment it is accepted. There
are no buttons, so the CSAT rating is spelled into the text like the widget's.
There is no typing indicator and never will be.

What it does have, uniquely here, is *threading in the customer's own client*.
Nothing routes on it — a reply is matched to a ticket by who sent it, the same
way every channel here works — but a customer whose mail client shows five
disconnected messages instead of one conversation thinks less of the company,
and the fix is three headers.

Both directions run in a worker thread. `smtplib` is synchronous and blocking
an event loop on somebody else's SMTP server, which may take thirty seconds to
answer, is how one slow mail host stops a dashboard from serving pages.
"""

from __future__ import annotations

import asyncio
import logging
import re
import smtplib
import ssl
import uuid
from collections.abc import Sequence
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

from src.channels.base import Capabilities, Delivery, Option
from src.services import file_store

logger = logging.getLogger(__name__)

NAME = "email"

#: How long to wait on a mail server before giving up. Generous, because some
#: are genuinely slow, and bounded, because this runs in a thread that a
#: shutdown wants back.
_TIMEOUT = 30.0


def is_configured() -> bool:
    """Whether this instance has a mailbox to work from."""
    from src.config import load_settings

    return load_settings().has_email


def thread_id(ticket_id: int, domain: str) -> str:
    """A Message-ID that says which conversation it belongs to.

    The ticket number is in the identifier on purpose. Nothing routes on it
    today — the sender's address already decides which conversation a reply
    joins — but it means a mailbox can be read months later and every message
    still says what it was about, without the database.
    """
    return f"<spooldesk-{ticket_id}-{uuid.uuid4().hex[:12]}@{domain}>"


def ticket_in(message_id: str) -> int | None:
    """The ticket a Message-ID of ours refers to, if it is one of ours."""
    match = re.match(r"^<spooldesk-(\d+)-[0-9a-f]+@", (message_id or "").strip())
    return int(match.group(1)) if match else None


def spell_options(text: str, options: Sequence[Option] | None) -> str:
    """The choices, written out, because email has no buttons.

    Required rather than polite: the Channel protocol says a channel without
    `caps.buttons` must still honour options, and a closing message that
    quietly loses its rating request is worse than an ugly one that keeps it.
    """
    labels = [o.label for o in (options or []) if o.label]
    return f"{text}\n\n{'  ·  '.join(labels)}" if labels else text


class EmailChannel:
    """SMTP out, and the file store for anything that came in."""

    name = NAME
    caps = Capabilities(
        # A sent email is on somebody else's server. There is no taking it
        # back, and pretending otherwise would put a delete button in the UI
        # that lies.
        unsend=False,
        unsend_by_row=False,
        files=True,
        buttons=False,
        typing=False,
        # Mail arrives on its own once the mailbox is being polled — nobody
        # has to open a page for it, which is what separates this from the
        # widget.
        push=True,
    )

    async def send_text(
        self, address: str, text: str, *, options: Sequence[Option] | None = None
    ) -> Delivery:
        return await self._send(address, spell_options(text, options))

    async def send_file(
        self, address: str, *, filename: str, content: bytes, caption: str | None = None
    ) -> Delivery:
        try:
            ref = file_store.put(content)
        except OSError as e:  # noqa: BLE001 — a full or read-only disk
            logger.error(f"Could not store an outbound mail attachment: {e}")
            return Delivery.failed("e_send_failed", str(e))

        delivery = await self._send(
            address, caption or "", attachment=(filename, content))
        if not delivery.ok:
            file_store.delete(ref)
            return delivery
        # The store keeps our copy so the ticket page can show what was sent;
        # the customer has theirs in their inbox.
        return Delivery(ok=True, ref=delivery.ref, file_ref=ref)

    async def fetch_file(self, ref: str) -> bytes | None:
        # Inbound attachments are written to the same store the widget uses —
        # an IMAP server will not hand the same MIME part back on request, so
        # keeping it is the only way it survives.
        return file_store.get(ref)

    async def unsend(self, address: str, ref: str) -> bool:
        return False

    async def typing(self, address: str) -> None:
        return None

    # ------------------------------------------------------------ internals

    async def _send(self, address: str, text: str,
                    *, attachment: tuple[str, bytes] | None = None,
                    ticket_id: int | None = None,
                    subject: str | None = None,
                    in_reply_to: str | None = None) -> Delivery:
        from src.config import load_settings

        settings = load_settings()
        if not settings.has_email:
            return Delivery.failed("e_no_mailbox")

        address = (address or "").strip()
        if "@" not in address:
            return Delivery.failed("e_send_rejected", f"not an address: {address!r}")

        domain = settings.email_address.rpartition("@")[2] or "localhost"
        message = EmailMessage()
        message["From"] = formataddr((settings.email_from_name, settings.email_address))
        message["To"] = address
        message["Subject"] = subject or settings.email_subject
        message["Message-ID"] = (
            thread_id(ticket_id, domain) if ticket_id else make_msgid(domain=domain)
        )
        if in_reply_to:
            # Both, because clients disagree about which they honour, and a
            # customer whose thread falls apart blames the company rather
            # than their mail client.
            message["In-Reply-To"] = in_reply_to
            message["References"] = in_reply_to
        message.set_content(text or "")

        if attachment is not None:
            filename, content = attachment
            message.add_attachment(
                content, maintype="application", subtype="octet-stream",
                filename=filename,
            )

        try:
            await asyncio.to_thread(_deliver, settings, message)
        except smtplib.SMTPRecipientsRefused:
            # The address does not exist, or the server will not take it.
            # Terminal, and worth marking the customer rather than retrying.
            return Delivery.failed("e_user_blocked_bot", address, blocked=True)
        except smtplib.SMTPAuthenticationError as e:
            logger.error(f"Mail credentials rejected: {e}")
            return Delivery.failed("e_send_failed", "authentication refused")
        except Exception as e:  # noqa: BLE001 — smtplib raises a wide family
            logger.warning(f"Could not send mail to {address}: {e}")
            return Delivery.failed("e_send_failed", f"{type(e).__name__}: {e}")

        return Delivery.sent(message["Message-ID"])


def _deliver(settings, message: EmailMessage) -> None:
    """The blocking half, run in a thread.

    Port 465 is implicit TLS and everything else is STARTTLS, which is the
    convention every provider follows and none of them document in the same
    place.
    """
    if settings.email_smtp_port == 465:
        with smtplib.SMTP_SSL(settings.email_smtp_host, settings.email_smtp_port,
                              timeout=_TIMEOUT,
                              context=ssl.create_default_context()) as server:
            server.login(settings.email_user, settings.email_password)
            server.send_message(message)
        return

    with smtplib.SMTP(settings.email_smtp_host, settings.email_smtp_port,
                      timeout=_TIMEOUT) as server:
        server.ehlo()
        server.starttls(context=ssl.create_default_context())
        server.ehlo()
        server.login(settings.email_user, settings.email_password)
        server.send_message(message)
