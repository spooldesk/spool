"""Reading the widget's own files off disk.

They live outside `static/` deliberately. Everything under that mount is
served to anyone who asks, and these two are supposed to stop existing when
the widget is switched off — a loader that keeps answering after an operator
turns the thing off is a promise the instance is no longer keeping.

Read once and kept, because they change when the process restarts and not
before, and re-reading two files on every page view of a customer's site is
a cost with nothing on the other side of it.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

_ASSETS = Path(__file__).resolve().parent.parent / "widget_assets"

#: Substituted rather than configured. The endpoint an embed talks to is a
#: fact about where it was served from, and asking a site owner to keep the
#: two in agreement is asking for the failure where the button appears and
#: nothing behind it works.
_BASE_MARKER = "__SPOOL_BASE__"


@lru_cache(maxsize=1)
def _loader_template() -> str:
    return (_ASSETS / "loader.js").read_text(encoding="utf-8")


@lru_cache(maxsize=1)
def _frame_template() -> str:
    return (_ASSETS / "frame.html").read_text(encoding="utf-8")


def loader_js(base_url: str) -> str:
    """The embed script, pointed at the instance that served it."""
    return _loader_template().replace(_BASE_MARKER, base_url.rstrip("/"))


def frame_html() -> str:
    """The panel. Talks to its own origin, so nothing to substitute."""
    return _frame_template()
