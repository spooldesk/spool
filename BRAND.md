# Brand assets

Everything the product shows a browser is generated from one file.

```bash
python scripts/build_icons.py
```

Reads `assets/brand/mark.png`, writes `docs/icons/` and
`src/dashboard/static/icons/`. The outputs are committed, because a
deployment has no Pillow and no build step. Run the script after changing
anything in `assets/brand/`, and commit what it produces.

`tests/test_icons.py` checks the result: that the set is complete, that the
`.ico` really holds several sizes, that the icons iOS and Android flatten
carry no alpha, and that every `theme-color` still matches `brand-600` in
`tailwind.config.js`.

## What is where

| File | What it is |
|---|---|
| `assets/brand/mark.png` | The mark, 1228px, transparent. The source everything is built from. |
| `assets/brand/mark_small.png` | **Not yet drawn.** A simplified mark for 16 and 32 pixels — see below. |
| `assets/brand/avatar.png` | Social avatar. Uploaded by hand to GitHub and elsewhere; nothing in the repo serves it. |

## The colour

`#7C3AED` — violet-600, the same value as `brand-600` in
`tailwind.config.js`. It is the `theme-color` on every page and the
`theme_color` in both manifests.

The icon pack as originally generated said `#4F46E5`, which is indigo and is
not used anywhere in this product. `theme-color` paints the browser chrome on
mobile and the PWA splash, so it is the one colour a person sees before any
CSS has loaded — worth keeping honest.

The splash background is `#09090B`, not white. The product is dark, and a PWA
opening on white flashes white first.

## Shared landing and dashboard identity

The landing, interactive preview, default sign-in page and sidebar use the same
spool mark and lowercase wordmark. Deployments with a custom `brand_name` keep
their name and neutral headset; the default Support/Spooldesk workspace shows
Spooldesk. A white-label deployment can replace the icons in `static/icons/`.

`assets/brand/tokens.css` is the shared palette, typography and control-radius
source. Run `python scripts/build_brand.py` to distribute it; the landing build
does this as well. Generated copies are `docs/brand.css` and
`src/dashboard/static/brand.css`. Both themes use neutral surfaces, the existing
violet primary action and a lighter violet for text on dark surfaces. The site
uses a system font stack, with no font downloads. The dashboard keeps its
existing typography and controls. The landing and new features follow that
dashboard design; they must not recolor its existing pages. In particular, the
dark canvas remains black and the sidebar uses its existing near-black surface,
not the gray panel fill rejected by the user on 2026-10-01.

---

# Still needed: a mark for 16 and 32 pixels

**This is the one thing the icon set is missing, and it is drawing work, not
code.** `scripts/build_icons.py` picks up `assets/brand/mark_small.png`
automatically the moment it exists, uses it for 16 and 32, and changes
nothing else.

## The problem

The full mark has three nested levels: a rounded square, a ring forming the
speech bubble, and inside it a spool wound with thin stripes. Sixteen pixels
cannot hold three levels.

- **48px** — works. The stripes read, the tail reads.
- **32px** — the bubble and its tail survive; the stripes merge into flat
  grey, so the spool reads as a cylinder, a bucket, a tin.
- **16px** — nothing survives. A pale blob on violet.

16 and 32 are the two sizes a browser tab actually uses. So the sizes that
work are the ones nobody looks at, and the sizes everyone looks at are the
ones that do not.

This is normal and not a fault in the drawing. A favicon is not a shrunk
logo; it is a separate drawing of the same idea.

## What to draw

A file named `mark_small.png`, in `assets/brand/`.

**Canvas**

- 512×512 or larger, square, PNG.
- The artwork must **touch all four edges of the canvas** — no margin.
  The full mark has 8% empty margin on every side, which at 16 pixels throws
  away a sixth of the width before the browser draws anything. The build
  script trims transparent margin automatically, but do not rely on it:
  design it edge to edge.
- Transparent background, with the rounded square as part of the artwork
  (the same violet gradient as now, or flat `#7C3AED` — see below).

**What to keep, in this order**

1. **The spool.** It is the name. If only one thing survives, this is it.
2. **The speech-bubble tail.** One triangular notch is enough to say
   "conversation" and it costs almost nothing at small size.

**What to drop**

1. **The outer bubble ring.** At 16px it is a one-pixel outline that turns
   into grey haze and eats the space the spool needs. The tail alone carries
   the meaning.
2. **The thin winding stripes.** Keep **two or three thick bands**, not
   eight thin ones. At 16px each band must be at least 2 physical pixels.
3. **The gradient**, probably. Consider flat `#7C3AED`. A gradient across
   sixteen pixels is four shades of noise.
4. **The thread end** sticking out on the right. Invisible below 48px.

**Contrast**

White or near-white artwork on violet, as now. Test it against a **white** tab
bar and a **dark** one — browsers use both, and a mark that only works on one
is half-broken.

## How to check it before sending

Do not judge it at full size. Export at 16 and 32, then:

1. Open the 16px file and zoom to 800%. Look at the actual pixels.
2. Shrink your design canvas view to 16px and look at it from arm's length.
3. Best test: put it in a real browser tab next to five other tabs, and see
   whether you can find it without reading the titles. That is the entire job
   of a favicon.

If you cannot tell what it is, it is not finished — regardless of how good it
looks at 512.

## When it is done

Drop the file at `assets/brand/mark_small.png` and run:

```bash
python scripts/build_icons.py
```

The script will say it found it, regenerate both directories, and the tests
will confirm the set is intact. Nothing else needs changing.
