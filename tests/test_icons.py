"""The icon set, and the ways it silently breaks.

An icon is the one asset nobody notices is wrong. A missing favicon is a
blank sheet in a tab, a transparent apple-touch-icon is black wedges on a
home screen, a manifest whose icons 404 is an install prompt that never
appears, and a theme colour that drifted from the brand is a violet product
with an indigo browser chrome. None of them fail a page load, none of them
appear in a log, and every one of them was in the set as delivered.

The files are committed rather than built at deploy time — a deployment has
no Pillow — so these check the artefacts themselves.
"""

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LANDING = ROOT / "docs" / "icons"
STATIC = ROOT / "src" / "dashboard" / "static" / "icons"
ICON_DIRS = [LANDING, STATIC]

#: Pages a browser draws a tab for, and which therefore need an icon.
PAGES = (
    "docs/index.html",
    "src/dashboard/templates/app_base.html",
    "src/dashboard/templates/login.html",
    "src/dashboard/templates/_help_base.html",
)

#: Must be opaque. iOS composites a transparent icon onto black and then
#: applies its own rounding, so transparency in the corners comes back as
#: black wedges around the artwork.
OPAQUE = (
    "apple-touch-icon.png",
    "android-chrome-192x192.png",
    "android-chrome-512x512.png",
)

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
ICO_SIGNATURE = b"\x00\x00\x01\x00"


# The headers are read directly rather than through Pillow. Pillow is only in
# requirements-screenshots.txt, which CI does not install, so an importorskip
# here would have made these checks skip silently in the one place they are
# meant to run — the same shape of failure they exist to catch.


def _png_header(path: Path) -> tuple[int, int, int]:
    """(width, height, colour_type) from a PNG's IHDR chunk."""
    raw = path.read_bytes()
    assert raw[:8] == PNG_SIGNATURE, f"{path.name} is not a PNG"
    return (
        int.from_bytes(raw[16:20], "big"),
        int.from_bytes(raw[20:24], "big"),
        raw[25],  # 2 = RGB, 3 = palette, 4 = grey+alpha, 6 = RGBA
    )


def _png_has_alpha(path: Path) -> bool:
    """An alpha channel, or a palette carrying a tRNS chunk."""
    if _png_header(path)[2] in (4, 6):
        return True
    return b"tRNS" in path.read_bytes()


def _ico_sizes(path: Path) -> list[tuple[int, int]]:
    """Every size the .ico actually contains."""
    raw = path.read_bytes()
    assert raw[:4] == ICO_SIGNATURE, f"{path.name} is not an ICO"
    count = int.from_bytes(raw[4:6], "little")
    # One 16-byte directory entry per image; a 0 in a size byte means 256,
    # because the field is only one byte wide.
    return sorted(
        (raw[6 + i * 16] or 256, raw[6 + i * 16 + 1] or 256) for i in range(count)
    )


@pytest.mark.parametrize("directory", ICON_DIRS, ids=lambda p: p.parent.name)
def test_the_set_is_complete(directory):
    """Every file a template or a manifest names has to be there."""
    required = {
        "favicon.ico", "favicon-16x16.png", "favicon-32x32.png",
        "favicon-48x48.png", "favicon-96x96.png",
        "apple-touch-icon.png",
        "android-chrome-192x192.png", "android-chrome-512x512.png",
        "site.webmanifest",
    }
    missing = required - {p.name for p in directory.iterdir()}
    assert not missing, (
        f"{directory.relative_to(ROOT)} is missing {sorted(missing)} — "
        f"run `python scripts/build_icons.py`"
    )


@pytest.mark.parametrize("directory", ICON_DIRS, ids=lambda p: p.parent.name)
def test_what_a_platform_flattens_has_no_transparency(directory):
    for name in OPAQUE:
        assert not _png_has_alpha(directory / name), (
            f"{name} has an alpha channel. iOS fills transparency with black "
            f"before applying its own mask, so the corners come back as black "
            f"wedges around the artwork. scripts/build_icons.py renders these "
            f"onto the mark's own gradient for exactly this reason."
        )


@pytest.mark.parametrize("directory", ICON_DIRS, ids=lambda p: p.parent.name)
def test_the_ico_holds_more_than_one_size(directory):
    """A .ico is a container. Half the generators on the internet write a
    single PNG, rename it, and leave the browser scaling one bitmap to every
    size it needs."""
    sizes = _ico_sizes(directory / "favicon.ico")
    assert (16, 16) in sizes and (32, 32) in sizes, (
        f"favicon.ico holds {sizes}; it should carry at least 16 and 32"
    )


@pytest.mark.parametrize("directory", ICON_DIRS, ids=lambda p: p.parent.name)
def test_the_png_sizes_are_the_sizes_they_claim(directory):
    for path in sorted(directory.glob("*.png")):
        claimed = re.search(r"(\d+)x(\d+)", path.name)
        if not claimed:
            continue
        want = (int(claimed.group(1)), int(claimed.group(2)))
        got = _png_header(path)[:2]
        assert got == want, f"{path.name} is {got[0]}x{got[1]}, not {want[0]}x{want[1]}"


@pytest.mark.parametrize("directory", ICON_DIRS, ids=lambda p: p.parent.name)
def test_the_manifest_icons_actually_resolve(directory):
    """A manifest icon path is relative to the manifest, not to the site root.

    Written as "icons/android-chrome-192x192.png" from a manifest that itself
    lives in icons/, every path resolved to /icons/icons/... and 404'd. A
    manifest with unreachable icons raises nothing and logs nothing; the
    install prompt simply never appears, which is not a thing anyone notices
    is missing.
    """
    manifest = json.loads((directory / "site.webmanifest").read_text(encoding="utf-8"))
    for icon in manifest["icons"]:
        src = icon["src"]
        assert not src.startswith("/"), (
            f"{src} is absolute, which breaks a deployment served under a path "
            f"prefix; make it relative to the manifest"
        )
        assert (directory / src).exists(), (
            f"{src} resolves to {directory / src}, which does not exist"
        )


def test_the_theme_colour_is_the_brand_colour():
    """The one colour a browser is told before it has any CSS.

    The generated pack shipped #4F46E5 — indigo, and a colour used nowhere in
    this product. theme-color paints the browser chrome on mobile and the PWA
    splash, so the drift showed up exactly where a first impression happens.
    """
    config = (ROOT / "tailwind.config.js").read_text(encoding="utf-8")
    brand = re.search(r"600:\s*'(#[0-9a-fA-F]{6})'", config)
    assert brand, "brand-600 not found in tailwind.config.js"
    expected = brand.group(1).upper()

    for directory in ICON_DIRS:
        manifest = json.loads(
            (directory / "site.webmanifest").read_text(encoding="utf-8"))
        assert manifest["theme_color"].upper() == expected, (
            f"{directory.relative_to(ROOT)}/site.webmanifest says "
            f"{manifest['theme_color']}, but brand-600 is {expected}"
        )

    for rel in PAGES:
        html = (ROOT / rel).read_text(encoding="utf-8")
        found = re.search(r'name="theme-color"\s+content="(#[0-9a-fA-F]{6})"', html)
        assert found, f"{rel} declares no theme-color"
        assert found.group(1).upper() == expected, (
            f"{rel} says {found.group(1)}, but brand-600 is {expected}"
        )


def test_the_splash_background_is_not_white():
    """The product is dark. A PWA opening on white flashes white first."""
    for directory in ICON_DIRS:
        manifest = json.loads(
            (directory / "site.webmanifest").read_text(encoding="utf-8"))
        assert manifest["background_color"].upper() != "#FFFFFF", (
            f"{directory.relative_to(ROOT)}/site.webmanifest opens on white"
        )


@pytest.mark.parametrize("rel", PAGES)
def test_every_page_a_browser_gets_declares_an_icon(rel):
    """The dashboard had none at all: an operator with six tickets open had
    six identical blank tabs and no way to tell them apart."""
    html = (ROOT / rel).read_text(encoding="utf-8")
    assert 'rel="icon"' in html, f"{rel} has no favicon, so its tab is blank"
    assert "apple-touch-icon" in html, f"{rel} has no touch icon"


def test_the_source_the_icons_are_built_from_is_committed():
    """Without it the set cannot be regenerated, and a committed binary
    nobody can reproduce is a binary nobody dares change."""
    assert (ROOT / "assets" / "brand" / "mark.png").exists()
    assert (ROOT / "scripts" / "build_icons.py").exists()
