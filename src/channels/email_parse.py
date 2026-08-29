"""Turning a raw email into something a support queue can use.

Kept apart from the transport on purpose. Fetching mail needs a server,
credentials and a network; deciding what a message *says* needs none of
those, and it is the part that is easy to get wrong in ways nobody notices
until a customer's ticket is unreadable.

Three problems, in descending order of how much they matter.

**The quoted reply.** Mail clients include the entire previous conversation
under every answer. Store that verbatim and the third message in a thread is
four screens of your own words with "yes, thanks" at the top. Stripping it is
a heuristic and will occasionally take a line too many, which is why the
original is never discarded — only the text shown to an operator is trimmed.

**HTML.** Plenty of mail has no plain-text part at all. Rendering it is not
an option here: what an operator sees is text, and a `<script>` in a support
ticket is a customer's mail client having been compromised, not a formatting
choice.

**Encodings.** Headers arrive as RFC 2047 word salad and bodies in whatever
charset the sender's decade preferred. `email` handles both, badly documented
and correctly, as long as it is asked properly.
"""

from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass, field
from email.header import decode_header, make_header
from email.message import Message
from email.utils import getaddresses, parsedate_to_datetime

logger = logging.getLogger(__name__)

#: What a body is trimmed to. Long enough for a genuinely detailed report,
#: short enough that a mail loop cannot put a megabyte in a ticket.
MAX_BODY_CHARS = 20_000

#: Attachments bigger than this are dropped with a note rather than stored.
#: A mailbox will happily hand over a 40 MB video; a support ticket does not
#: need one badly enough to fill a disk over it.
MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024


@dataclass
class Attachment:
    filename: str
    content: bytes
    mime_type: str


@dataclass
class Inbound:
    """One email, reduced to what a ticket needs."""

    from_address: str
    from_name: str
    subject: str
    #: The part the sender actually wrote this time, quoted history removed.
    text: str
    #: Everything, including the quotes. Kept because the trimming is a
    #: heuristic and the day it eats something important, the evidence
    #: should still exist.
    full_text: str
    message_id: str
    #: Message-Ids this is a reply to, newest first. Used to thread.
    references: list[str] = field(default_factory=list)
    attachments: list[Attachment] = field(default_factory=list)
    #: True when nothing but quoted history and a signature was left.
    is_empty: bool = False


# ------------------------------------------------------------------ headers


def decode(raw: str | None) -> str:
    """An RFC 2047 header as readable text.

    `=?UTF-8?B?0J/RgNC40LLQtdGC?=` is a perfectly ordinary Subject line, and
    putting it in a ticket as written is how a queue fills up with mojibake.
    """
    if not raw:
        return ""
    try:
        return str(make_header(decode_header(raw))).strip()
    except Exception:  # noqa: BLE001 — malformed headers are common
        return raw.strip()


def sender_of(message: Message) -> tuple[str, str]:
    """(address, display name) of whoever sent this.

    Reply-To wins over From when both are present, because that is what it is
    for: a sender saying "answer somewhere else". Mailing lists and ticketing
    systems both rely on it, and ignoring it means replying into the void.
    """
    for header in ("Reply-To", "From"):
        pairs = getaddresses([message.get(header, "")])
        for name, address in pairs:
            address = (address or "").strip()
            if address:
                return address.lower(), decode(name) or address.split("@")[0]
    return "", ""


def references_of(message: Message) -> list[str]:
    """Every Message-Id this message claims to answer, newest first.

    In-Reply-To is the direct parent and References is the whole ancestry, so
    the parent is checked first and the rest are a fallback for clients that
    only send one of the two.
    """
    found: list[str] = []
    for header in ("In-Reply-To", "References"):
        raw = message.get(header, "") or ""
        for token in re.findall(r"<[^<>@\s]+@[^<>\s]+>", raw):
            if token not in found:
                found.append(token)
    return found


# --------------------------------------------------------------------- body


_HTML_BREAKS = re.compile(r"(?i)</?(?:br|p|div|tr|li|h[1-6])\b[^>]*>")
_HTML_TAG = re.compile(r"<[^>]+>")
_HTML_DROP = re.compile(r"(?is)<(script|style)\b.*?</\1>")


def html_to_text(markup: str) -> str:
    """A rough, safe rendering of an HTML mail part.

    Deliberately not a renderer. Script and style contents are dropped
    outright rather than escaped: their text is never anything an operator
    wants to read, and the only reason a support email contains a script is
    one worth not pasting anywhere.
    """
    text = _HTML_DROP.sub(" ", markup or "")
    text = _HTML_BREAKS.sub("\n", text)
    text = _HTML_TAG.sub("", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t\xa0]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return text.strip()


def _decoded_payload(part: Message) -> str:
    raw = part.get_payload(decode=True)
    if raw is None:
        return ""
    charset = part.get_content_charset() or "utf-8"
    for attempt in (charset, "utf-8", "cp1251", "latin-1"):
        try:
            return raw.decode(attempt)
        except (LookupError, UnicodeDecodeError):
            continue
    return raw.decode("utf-8", "replace")


def body_of(message: Message) -> str:
    """The message text, preferring a plain part and falling back to HTML."""
    plain: list[str] = []
    markup: list[str] = []

    for part in message.walk():
        if part.get_content_maintype() == "multipart":
            continue
        if (part.get_content_disposition() or "") == "attachment":
            continue
        kind = part.get_content_type()
        if kind == "text/plain":
            plain.append(_decoded_payload(part))
        elif kind == "text/html":
            markup.append(_decoded_payload(part))

    if any(p.strip() for p in plain):
        return "\n".join(plain).strip()
    return html_to_text("\n".join(markup))


# ------------------------------------------------------------------- quotes

#: Lines that mean "everything below is the previous message". Each is
#: matched against the start of a stripped line. They cover the clients
#: people actually use; a client that invents a new one costs a stale quote
#: in one ticket, not a broken parser.
_QUOTE_MARKERS = (
    re.compile(r"^>"),
    re.compile(r"^-{2,}\s*Original Message\s*-{2,}", re.I),
    re.compile(r"^-{2,}\s*Forwarded message\s*-{2,}", re.I),
    re.compile(r"^_{10,}$"),
    # "On Tuesday, 3 June 2025 at 14:02, Ann <ann@x> wrote:" and its
    # translations. Matched by shape rather than by wording, because the
    # wording is localised — but the shape is not quite universal either:
    # English puts the verb last ("… Ann wrote:") and German puts it second
    # ("Am 02.06.2025 schrieb Ann:"). So the verb may sit anywhere, and it is
    # the trailing colon that closes the line.
    re.compile(
        r"^(On|Am|Le|El|Il|В|Вт)\b.{0,200}?"
        r"\b(wrote|schrieb|a écrit|escribió|ha scritto|написал[аи]?|писав)\b"
        r".{0,80}:\s*$",
        re.I,
    ),
    re.compile(r"^From:\s.+", re.I),
    re.compile(r"^Sent from my \w+", re.I),
)

#: The signature separator, which by convention is exactly "-- ".
_SIGNATURE = re.compile(r"^--\s*$")


def strip_quotes(text: str) -> str:
    """What the sender wrote this time.

    Cuts at the first quote marker and drops a trailing signature. Everything
    below the cut is kept by the caller in `full_text`: this is a heuristic
    about presentation, and a heuristic must never be the only copy.
    """
    lines = (text or "").splitlines()
    kept: list[str] = []

    for line in lines:
        probe = line.strip()
        if any(marker.match(probe) for marker in _QUOTE_MARKERS):
            break
        if _SIGNATURE.match(line):
            break
        kept.append(line)

    # Trailing blank lines left by the cut.
    while kept and not kept[-1].strip():
        kept.pop()
    return "\n".join(kept).strip()


# -------------------------------------------------------------- attachments


def attachments_of(message: Message) -> list[Attachment]:
    out: list[Attachment] = []
    for part in message.walk():
        if part.get_content_maintype() == "multipart":
            continue
        disposition = (part.get_content_disposition() or "").lower()
        name = decode(part.get_filename())
        # Inline images with a filename count: a screenshot pasted into the
        # body of a mail is exactly the attachment support most wants.
        if disposition not in ("attachment", "inline") and not name:
            continue
        if not name and disposition != "attachment":
            continue
        content = part.get_payload(decode=True) or b""
        if not content:
            continue
        if len(content) > MAX_ATTACHMENT_BYTES:
            logger.info(f"Dropped an oversized mail attachment: {name} ({len(content)} bytes)")
            continue
        out.append(Attachment(
            filename=name or "attachment",
            content=content,
            mime_type=part.get_content_type() or "application/octet-stream",
        ))
    return out


# ------------------------------------------------------------------- parse


def parse(message: Message) -> Inbound:
    """One email, reduced to what a ticket needs."""
    address, name = sender_of(message)
    full = body_of(message)[:MAX_BODY_CHARS]
    trimmed = strip_quotes(full)

    attachments = attachments_of(message)
    return Inbound(
        from_address=address,
        from_name=name,
        subject=decode(message.get("Subject")),
        # A reply that is nothing but quoted history still means something
        # when it carries a file. Falling back to the untrimmed text would
        # put four screens of quotes in the ticket instead.
        text=trimmed,
        full_text=full,
        message_id=(message.get("Message-ID") or "").strip(),
        references=references_of(message),
        attachments=attachments,
        is_empty=not trimmed and not attachments,
    )


def received_at(message: Message):
    """When the sender says they sent it, or None."""
    try:
        return parsedate_to_datetime(message.get("Date"))
    except Exception:  # noqa: BLE001 — Date is frequently malformed
        return None
