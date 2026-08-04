"""Presentation helpers exposed to Jinja.

These live here rather than in each route module because they used to be
copy-pasted: three separate `_fmt_seconds` existed, only one of which had been
localised, so the same duration read "54min" on the ticket list and "54м" on
the users page.
"""

from __future__ import annotations

from src.dashboard.i18n import t

EMPTY = "—"


def fmt_seconds(s: int | float | None) -> str:
    """Compact duration: 45s / 54min / 3.2h / 1.5d, in the UI language.

    Clamped at zero. The bot and the dashboard can run on different hosts, and
    a second of clock skew is enough to make a fresh ticket's age negative —
    "-8075s" on a card is a bug report waiting to happen.
    """
    if s is None:
        return EMPTY
    s = max(0, int(s))
    if s < 60:
        return f"{s}{t('sec_short')}"
    if s < 3600:
        return f"{s // 60}{t('min_short')}"
    if s < 86400:
        return f"{s / 3600:.1f}{t('hour_short')}"
    return f"{s / 86400:.1f}{t('day_short')}"


def fmt_ms(n: int | float | None) -> str:
    """Latency: 340ms / 1.2s."""
    if n is None:
        return EMPTY
    if n < 1000:
        return f"{int(n)}{t('ms_short')}"
    return f"{n / 1000:.1f}{t('sec_short')}"
