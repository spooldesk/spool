"""Both generated landing pages must use classes present in the shipped CSS.

The old landing silently relied on absent Tailwind utilities. The new landing
uses a handwritten stylesheet and a shared bilingual template, but an undefined
class still silently loses its layout. Keep the original regression guard.
"""

import re
from pathlib import Path

import pytest

LANDING = Path("docs/index.html")
STYLESHEET = Path("docs/style.css")

#: Classes that are deliberately not utilities and have no rule of their own.
NOT_UTILITIES = {
    # A marker for `group-open:` and friends; Tailwind emits no `.group` rule.
    "group",
    # Tabler's icon font, which arrives from its own stylesheet.
    "ti",
}


def _escape(cls: str) -> str:
    """A class name as it appears in a CSS selector.

    Tailwind escapes the characters that are not valid in an identifier, so
    `sm:grid-cols-3` is written `.sm\\:grid-cols-3` and `bg-black/80` is
    `.bg-black\\/80`. Checking for the raw name would find nothing at all.
    """
    for char in (":", "/", "[", "]", ".", "%", "(", ")", ","):
        cls = cls.replace(char, "\\" + char)
    return cls


def _classes_used(landing=LANDING) -> set[str]:
    html = landing.read_text(encoding="utf-8")
    used: set[str] = set()
    for attr in re.findall(r'class="([^"]*)"', html):
        for cls in attr.split():
            if not cls or cls.startswith("ti-") or cls in NOT_UTILITIES:
                continue
            used.add(cls)
    return used


@pytest.mark.parametrize("landing", [LANDING, Path("docs/ru/index.html")])
def test_the_landing_uses_only_classes_its_stylesheet_has(landing):
    assert landing.exists() and STYLESHEET.exists()

    css = STYLESHEET.read_text(encoding="utf-8")
    used = _classes_used(landing)
    assert len(used) > 40, "the class scraper stopped matching anything"

    missing = sorted(
        cls for cls in used
        # A selector is followed by `{`, or by `,`/`:`/space when it is part of
        # a longer rule — `.group[open] .group-open\:rotate-180{…}`.
        if not re.search(r"\." + re.escape(_escape(cls)) + r"[{,:\s\[>]", css)
    )

    assert not missing, (
        "classes on the landing page that its committed CSS does not define — "
        "they do nothing, silently:\n  " + "\n  ".join(missing)
    )


@pytest.mark.parametrize("known_absent", ["sm:grid-cols-3", "whitespace-nowrap"])
def test_the_check_would_have_caught_the_ones_that_got_through(known_absent):
    """Guarding the guard. If the escaping ever breaks, the test above starts
    passing for everything and stops being a test at all."""
    css = STYLESHEET.read_text(encoding="utf-8")
    assert not re.search(r"\." + re.escape(_escape(known_absent)) + r"[{,:\s]", css), (
        f"{known_absent} is in the stylesheet now — if the build was "
        f"regenerated this parametrisation is stale, and that is fine, but "
        f"pick another absent class so the escaping stays covered"
    )


@pytest.mark.parametrize("landing", [LANDING, Path("docs/ru/index.html")])
def test_no_template_syntax_survives_on_the_landing(landing):
    """The landing is served raw, so a Jinja tag is just text on the page.

    The application's src/dashboard/templates/ files use Jinja, and a
    comment copied from there can render as a paragraph of
    developer notes in the middle of the pricing table. It did — between the
    self-hosted feature list and its button, in production font size.
    """
    html = landing.read_text(encoding="utf-8")
    strays = re.findall(r"\{[#{%].{0,50}", html, re.S)
    assert not strays, (
        "docs/index.html is static HTML served as-is, but it contains template "
        "syntax that will render as visible text: "
        + "; ".join(s.replace(chr(10), " ")[:60] for s in strays)
        + " — use an HTML comment instead."
    )
