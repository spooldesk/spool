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


@lru_cache(maxsize=4)
def _cached(path: Path, _mtime: float) -> str:
    # The mtime is part of the key and is otherwise unused. In production it
    # never changes, so this is the read-once cache it was; in development it
    # means editing the widget no longer needs a restart to be visible. The
    # cost is one stat() per request, against a mistake that costs ten
    # minutes of wondering why a change did nothing.
    return path.read_text(encoding="utf-8")


def _read(name: str) -> str:
    path = _ASSETS / name
    try:
        stamp = path.stat().st_mtime
    except OSError:
        stamp = 0.0
    return _cached(path, stamp)


def _loader_template() -> str:
    return _read("loader.js")


def _frame_template() -> str:
    return _read("frame.html")


def loader_js(base_url: str) -> str:
    """The embed script, pointed at the instance that served it."""
    return _loader_template().replace(_BASE_MARKER, base_url.rstrip("/"))


def frame_html() -> str:
    """Bundle shared controls into the panel so no stale static file can break it."""
    html = _frame_template()
    static = _ASSETS.parent / "static"
    for name in ("message-menu.css", "message-menu.js", "auto-grow.js"):
        path = static / name
        source = _cached(path, path.stat().st_mtime)
        html = html.replace(f"__SPOOL_ASSET_{name}__", source)
    return html
