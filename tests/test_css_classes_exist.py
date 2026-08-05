"""Every utility class a template uses must be in the committed stylesheet.

`tw.css` is a compiled, committed Tailwind build so that deployments need no
Node. The cost is that a class which is not already in it **does nothing at
all** — no error, no warning, the element simply renders unstyled. It bit twice
in one day: `max-w-[240px]` on an attachment chip and `gap-8` on the answer
quality block, and neither showed up until someone looked at the page.

This turns that silence into a failing test.
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "src" / "dashboard" / "templates"
STATIC = ROOT / "src" / "dashboard" / "static"

# Prefixes worth policing: layout and sizing utilities where a missing class is
# invisible in code review but obvious on screen. Colour and typography classes
# are generated in bulk and rarely missing.
WATCHED = re.compile(
    r"^(gap|gap-x|gap-y|max-w|min-w|max-h|min-h|w|h|space-x|space-y|"
    r"grid-cols|col-span|inset|top|bottom|left|right|z)-"
)

# Not Tailwind: defined by hand in app.css or used as JavaScript hooks.
OURS = {
    "card", "msg", "drop-zone", "move-btn", "ui-select", "ui-date",
    "scroll-pretty", "sr-only-select", "tabular-nums", "badge-text",
}


def _stylesheet() -> str:
    return ((STATIC / "tw.css").read_text(encoding="utf-8")
            + (STATIC / "app.css").read_text(encoding="utf-8"))


def _class_tokens(html: str):
    """Static class tokens only — anything Jinja builds at render time is out
    of reach of a source-level check."""
    for attr in re.findall(r'class="([^"]*)"', html):
        if "{{" in attr or "{%" in attr:
            continue
        for token in attr.split():
            yield token


def _escaped(token: str) -> str:
    """How Tailwind writes the token as a CSS selector: the characters that
    are special in a selector are backslash-escaped."""
    out = []
    for ch in token:
        if not (ch.isalnum() or ch in "-_"):
            out.append("\\")
        out.append(ch)
    return "".join(out)


@pytest.mark.parametrize(
    "template",
    sorted(p.name for p in TEMPLATES.glob("*.html")),
)
def test_watched_classes_are_in_the_build(template):
    css = _stylesheet()
    missing = set()

    for token in _class_tokens((TEMPLATES / template).read_text(encoding="utf-8")):
        base = token.split(":")[-1]           # strip hover:, md:, dark: …
        if base in OURS or not WATCHED.match(base):
            continue
        if f".{_escaped(token)}" not in css and f".{_escaped(base)}" not in css:
            missing.add(token)

    assert not missing, (
        f"{template} uses classes that are not in the committed build, so they "
        f"render as nothing: {sorted(missing)}\n"
        f"Either rebuild tw.css (see CONTRIBUTING.md) or use a class that is "
        f"already there."
    )
