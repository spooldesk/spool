"""Lightweight PII / secret scrubber for text sent to external LLMs.

Defense-in-depth, NOT a complete solution. Primary defense remains the
category whitelist (1pass tickets never reach the AI at all).
"""

from __future__ import annotations

import re

_REDACTED = "[REDACTED]"

# Order matters — more specific patterns first.
_PATTERNS = [
    # Telegram bot tokens: "1234567:ABC...." (39+ chars after colon)
    (re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{30,}\b"), _REDACTED),
    # JWT-like tokens (3 base64url segments separated by dots, starts with eyJ)
    (re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"), _REDACTED),
    # "password: X" / "пароль: X" / "пароль X" — until whitespace/EOL
    (re.compile(r"(?i)\b(?:password|пароль|пасс|pwd)\s*[:=]?\s*(\S{4,})"), r"\g<0>".replace("\\g<0>", "")),
]

# The "password:" pattern above is special — we want to keep the label but
# nuke the value. Do it with a function.
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
