"""Build every icon the product serves, from one source file.

    python scripts/build_icons.py

Run this after changing anything in assets/brand/. The outputs are committed —
a deployment has no Pillow and no build step — so the point of the script is
that they can be regenerated identically instead of being a set of binaries
nobody knows how to reproduce.

Two things it does that a favicon generator does not:

**It uses the whole canvas.** The source mark is drawn with 8% empty margin on
every side. That is right for a press kit and wrong for a favicon: at sixteen
pixels it throws away a sixth of the width before the browser has drawn
anything. Everything here is generated from the mark's own bounding box.

**It flattens what must not be transparent.** iOS composites a transparent
apple-touch-icon onto black and then applies its own rounding, so transparent
corners come back as black wedges around the artwork. The touch icon and the
Android icons are rendered onto the mark's own gradient instead, full bleed,
and the platform rounds them itself.

SMALL SIZES ARE STILL A COMPROMISE. 16 and 32 are a shrunk logo, and a mark
with three nested levels — rounded square, bubble ring, spool — cannot show
three levels in sixteen pixels. When assets/brand/mark_small.png exists (a
simplified drawing, see BRAND.md) this script uses it for 16 and 32
instead, and nothing else changes.
"""

from __future__ import annotations

import json
import pathlib
import sys

try:
    from PIL import Image
except ImportError:  # pragma: no cover
    sys.exit("Pillow is needed to rebuild icons: pip install Pillow")

ROOT = pathlib.Path(__file__).resolve().parents[1]
BRAND = ROOT / "assets" / "brand"

#: Where the landing looks for them (Cloudflare Pages serves docs/).
LANDING = ROOT / "docs" / "icons"
#: ...and where the dashboard, the widget and the help centre do.
STATIC = ROOT / "src" / "dashboard" / "static" / "icons"

#: brand-600 in tailwind.config.js. The one colour a browser is told about
#: before it has any CSS, so it has to be the same violet as everything else —
#: the generated manifest said #4F46E5, which is indigo and belongs to nothing
#: in this product.
BRAND_VIOLET = "#7C3AED"
#: The dark the dashboard actually paints. A PWA splash screen uses this, and
#: white would flash before a black application.
BRAND_INK = "#09090B"


def _trimmed(path: pathlib.Path) -> Image.Image:
    """The mark with its transparent margin removed."""
    im = Image.open(path).convert("RGBA")
    box = im.getchannel("A").getbbox()
    return im.crop(box) if box else im


def _on_gradient(mark: Image.Image, size: int) -> Image.Image:
    """The mark full-bleed on its own gradient, with no alpha anywhere.

    Sampled from the artwork rather than hard-coded, so a redrawn mark with a
    different gradient still produces corners that match it.
    """
    w, h = mark.size
    start = mark.convert("RGB").getpixel((w // 10, h // 10))
    end = mark.convert("RGB").getpixel((w - w // 10, h - h // 10))

    bg = Image.new("RGB", (size, size))
    px = bg.load()
    for y in range(size):
        for x in range(size):
            # Diagonal, matching the direction the artwork runs.
            t = (x + y) / (2 * (size - 1))
            px[x, y] = tuple(
                round(start[i] + (end[i] - start[i]) * t) for i in range(3)
            )

    out = bg.convert("RGBA")
    out.alpha_composite(mark.resize((size, size), Image.LANCZOS))
    return out.convert("RGB")  # no alpha channel at all, which is the point


def build() -> None:
    mark = _trimmed(BRAND / "mark.png")
    print(f"source: mark.png, trimmed to {mark.size[0]}x{mark.size[1]}")

    # A simplified drawing for the sizes where the full mark turns to mush.
    small_path = BRAND / "mark_small.png"
    small = _trimmed(small_path) if small_path.exists() else mark
    if small_path.exists():
        print(f"        mark_small.png, trimmed to {small.size[0]}x{small.size[1]}"
              " — used for 16 and 32")
    else:
        print("        mark_small.png NOT PRESENT — 16 and 32 fall back to the"
              " full mark and will read as a blob. See BRAND.md.")

    for out in (LANDING, STATIC):
        out.mkdir(parents=True, exist_ok=True)

        def src(px: int) -> Image.Image:
            return small if px <= 32 else mark

        # Transparent PNGs: a browser tab draws these on its own background,
        # light or dark, and the rounded corners should stay corners.
        for px in (16, 32, 48, 96):
            src(px).resize((px, px), Image.LANCZOS).save(out / f"favicon-{px}x{px}.png")

        # One .ico holding three sizes, because that is what the file is for
        # and what every browser falls back to when it ignores the link tags.
        src(16).resize((256, 256), Image.LANCZOS).save(
            out / "favicon.ico", sizes=[(16, 16), (32, 32), (48, 48)]
        )

        # Opaque, full bleed, rounded by the platform. See the module docstring.
        _on_gradient(mark, 180).save(out / "apple-touch-icon.png")
        for px in (192, 512):
            _on_gradient(mark, px).save(out / f"android-chrome-{px}x{px}.png")

        (out / "site.webmanifest").write_text(json.dumps({
            "name": "Spooldesk",
            "short_name": "Spooldesk",
            # Relative to the manifest's own URL, not to the site root —
            # which is why these are bare filenames. Written as
            # "icons/android-…" they resolved to /icons/icons/… and 404'd,
            # silently: a manifest with unreachable icons is not an error,
            # it is just an install prompt that never appears.
            "icons": [
                {"src": "android-chrome-192x192.png",
                 "sizes": "192x192", "type": "image/png", "purpose": "any"},
                {"src": "android-chrome-512x512.png",
                 "sizes": "512x512", "type": "image/png", "purpose": "any"},
            ],
            "theme_color": BRAND_VIOLET,
            "background_color": BRAND_INK,
            "display": "standalone",
            "start_url": "/",
        }, indent=2) + "\n", encoding="utf-8")

        rel = out.relative_to(ROOT).as_posix()
        print(f"  wrote {len(list(out.iterdir()))} files to {rel}/")


if __name__ == "__main__":
    build()
