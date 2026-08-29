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
# invisible in code review but obvious on screen. Plain colour classes are left
# out because they really are generated in bulk.
WATCHED = re.compile(
    r"^(gap|gap-x|gap-y|max-w|min-w|max-h|min-h|w|h|space-x|space-y|"
    r"grid-cols|col-span|inset|top|bottom|left|right|z)-"
)

# ...but a colour with an opacity modifier is not generated in bulk. Tailwind
# emits `bg-emerald-500/10` only because some template asked for it, so the
# neighbouring `/25` that nobody has used yet does not exist and renders as
# no background at all. That is how a panel ships with its tint missing.
WATCHED_ALPHA = re.compile(r"^(bg|border|text|from|via|to|ring|divide)-[a-z]+-\d+/\d+$")

# Not Tailwind: defined by hand in app.css or used as JavaScript hooks.
OURS = {
    "card", "msg", "drop-zone", "move-btn", "ui-select", "ui-date",
    "scroll-pretty", "sr-only-select", "tabular-nums", "badge-text",
}


def _stylesheet() -> str:
    return ((STATIC / "tw.css").read_text(encoding="utf-8")
            + (STATIC / "app.css").read_text(encoding="utf-8"))


#: A Jinja expression inside a class attribute, and whatever it renders to.
_JINJA = re.compile(r"\{\{.*?\}\}|\{%.*?%\}", re.S)


def _class_tokens(html: str):
    """Every literal class token, including the ones sitting next to Jinja.

    Skipping any attribute containing `{{` or `{%` used to be the rule here,
    and it excused exactly the attributes where this bug hides: a conditional
    class list is written once and only ever seen in one of its branches. So
    the Jinja is cut out and the literal tokens around it are still checked.
    What a tag renders to is still out of reach, and still not checked.
    """
    for attr in re.findall(r'class="([^"]*)"', html, re.S):
        for token in _JINJA.sub(" ", attr).split():
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
        if base in OURS or not (WATCHED.match(base) or WATCHED_ALPHA.match(base)):
            continue
        if f".{_escaped(token)}" not in css and f".{_escaped(base)}" not in css:
            missing.add(token)

    assert not missing, (
        f"{template} uses classes that are not in the committed build, so they "
        f"render as nothing: {sorted(missing)}\n"
        f"Either rebuild tw.css (see CONTRIBUTING.md) or use a class that is "
        f"already there."
    )
