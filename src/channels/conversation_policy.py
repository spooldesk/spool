"""Per-channel conversation semantics: how a reply anchors, what can change
after it is sent, and what typing signals are honest to show.

Kept separate from `Capabilities` in base.py: capabilities describe transport
features (can this channel send buttons, files, unsend), while a
`ConversationPolicy` describes conversation shape (how a reply is addressed,
whether an edit is even meaningful) — a distinction the sending/UI layer
needs but the channel adapter itself does not.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class ConversationPolicy:
    """Declares reply anchoring, mutability and typing honesty for one channel."""

    #: How a reply is addressed on this channel: native_reply (Telegram,
    #: targets a message id), thread (Slack, targets a root ts), email_thread
    #: (Message-ID/References), local_reply (widget row) or quote (textual
    #: fallback used when no native anchoring is known to exist).
    reply_style: str
    edit_text: bool
    edit_caption: bool
    delete_sent: bool
    customer_typing: bool
    outbound_typing: bool


_UNKNOWN = ConversationPolicy(
    reply_style="quote",
    edit_text=False,
    edit_caption=False,
    delete_sent=False,
    customer_typing=False,
    outbound_typing=False,
)

_POLICIES: dict[str, ConversationPolicy] = {
    "telegram": ConversationPolicy(
        reply_style="native_reply",
        edit_text=True,
        edit_caption=True,
        delete_sent=True,
        customer_typing=False,
        outbound_typing=True,
    ),
    "slack": ConversationPolicy(
        reply_style="thread",
        edit_text=True,
        edit_caption=False,
        delete_sent=True,
        customer_typing=False,
        outbound_typing=False,
    ),
    "email": ConversationPolicy(
        reply_style="email_thread",
        edit_text=False,
        edit_caption=False,
        delete_sent=False,
        customer_typing=False,
        outbound_typing=False,
    ),
    "widget": ConversationPolicy(
        reply_style="local_reply",
        edit_text=True,
        edit_caption=False,
        delete_sent=True,
        customer_typing=True,
        outbound_typing=True,
    ),
}


def policy_for(channel: str) -> ConversationPolicy:
    """The conversation policy for a named channel.

    Unknown names get `_UNKNOWN` rather than raising: a policy lookup sits on
    UI and send paths where a typo or a not-yet-wired channel should degrade
    to "assume nothing is possible" instead of taking the request down.
    """
    return _POLICIES.get(channel, _UNKNOWN)


_WHITESPACE_RE = re.compile(r"\s+")


def _normalize_whitespace(value: str) -> str:
    return _WHITESPACE_RE.sub(" ", value).strip()


def quote_reply(author: str, text: str, limit: int = 240) -> str:
    """A textual quote fallback for channels with no native reply anchoring.

    Renders as one '> ' line per part (author, then snippet) followed by a
    blank line, so it reads correctly when simply prepended to a plain-text
    reply body. Whitespace is collapsed rather than preserved because the
    source may be a Slack thread root or an email body with arbitrary
    formatting, and this quote is a plain-text fallback, not a copy.
    """
    if limit <= 0:
        raise ValueError("limit must be positive")

    snippet = _normalize_whitespace(text)[:limit]
    if not snippet:
        return ""

    lines: list[str] = []
    author_line = _normalize_whitespace(author)[:60]
    if author_line:
        lines.append(f"> {author_line}")
    lines.append(f"> {snippet}")
    return "\n".join(lines) + "\n\n"
