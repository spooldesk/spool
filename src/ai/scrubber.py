"""Lightweight PII / secret scrubber for text sent to external LLMs.

Defence in depth, NOT a complete solution. The primary defence is the
category blocklist: a ticket in a blocked category is never sent to the AI
at all, so nothing in it reaches a third party to be scrubbed.
"""

from __future__ import annotations

import re

_REDACTED = "[REDACTED]"

# Keep the password label but redact its value using a replacement function.
_PASSWORD_LABEL = re.compile(
    r"(?i)\b(?P<label>password|пароль|пасс|pwd)\s*[:=]?\s*(?P<value>\S{4,})"
)


def scrub(text: str) -> str:
    """Return `text` with likely secrets replaced by [REDACTED].

    Conservative — prefers false positives over leaking. The aim is to keep
    obvious credentials out of LLM prompts, not to defeat a determined attacker
    (those tickets should not be in this code path anyway — see category whitelist).
    """
    if not text:
        return text

    out = text

    # Telegram bot tokens
    out = re.sub(r"\b\d{6,}:[A-Za-z0-9_-]{30,}\b", _REDACTED, out)
    # JWTs
    out = re.sub(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b", _REDACTED, out)
    # Labelled passwords — keep the label, redact the value
    out = _PASSWORD_LABEL.sub(lambda m: f"{m.group('label')}: {_REDACTED}", out)
    # Long opaque tokens — 40+ continuous chars from base64 alphabet (no spaces)
    # Skips short words and URLs (which have / and :). False positives possible
    # on long file ids, but those are not user secrets.
    out = re.sub(r"(?<![A-Za-z0-9])[A-Za-z0-9_\-]{40,}(?![A-Za-z0-9])", _REDACTED, out)

    return out
