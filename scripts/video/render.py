"""Narrated walkthrough: the real demo, the voice, a quiet music bed.

    <tts-venv>/python scripts/video/render.py --preview   # stills, audio, subtitles
    <tts-venv>/python scripts/video/render.py             # and the video, master and web copy

Inputs: build/captures/ (capture.py: the actual dashboard at /demo/, fictional
data), narration/*.flac, report.json and words.json (narrate.py, cues.py;
committed, so this needs no GPU). A scene lasts as long as its narration plus a
short lead and tail. Clicks, cuts and camera moves are placed on the words that
describe them, using Whisper's timings, so the picture follows the voice
instead of a guessed schedule.

Writes build/export/ (the master, SRT and VTT subtitles, poster, storyboard)
and build/web/ (what docs/video/ serves, under the landing's file names).
ffmpeg comes from the FFMPEG environment variable or PATH. The text is set in
Segoe UI, from the Windows fonts folder or VIDEO_FONTS.
"""
import argparse
import json
import os
import shutil
import subprocess
import wave
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from timing import split_cue, srt, vtt

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
NARRATION = HERE / "narration"
BUILD = HERE / "build"
CAPTURES = BUILD / "captures"
OUT = BUILD / "export"
WEB = BUILD / "web"
NAME = "Spooldesk-walkthrough-EN"
WEB_NAME = "spooldesk-walkthrough"             # the landing's file names in docs/video/
WEB_LIMIT = 25 * 1024 * 1024                   # Cloudflare Pages refuses larger files
FONT_DIR = Path(os.environ.get("VIDEO_FONTS") or Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts")

W, H, FPS = 1920, 1080, 30
VX, VY, VW, VH = 176, 134, 1568, 882           # where the dashboard sits
CW, CH = 2560, 1440                            # capture size
LEAD, TAIL, XF = 0.6, 0.8, 0.45                # voice offset, hold after, scene cross-fade
BG, WHITE, MUTED = (5, 5, 7), (247, 246, 251), (165, 160, 179)
VIOLET, LINE, PANEL = (146, 105, 255), (48, 41, 65), (10, 8, 15)

FONTS = {}


def font(size, weight="regular"):
    key = (size, weight)
    if key not in FONTS:
        file = {"regular": "segoeui.ttf", "semibold": "seguisb.ttf", "bold": "segoeuib.ttf",
                "symbol": "seguisym.ttf"}[weight]
        FONTS[key] = ImageFont.truetype(str(FONT_DIR / file), size)
    return FONTS[key]


def ffmpeg():
    path = os.environ.get("FFMPEG") or shutil.which("ffmpeg")
    if not path:
        raise SystemExit("ffmpeg not found: put it on PATH or set FFMPEG to the executable")
    return path


def ease(v):
    v = max(0.0, min(1.0, v))
    return v * v * (3 - 2 * v)


def text(d, xy, value, size, fill=WHITE, weight="regular", anchor="la", spacing=8):
    if "\n" in value:
        d.multiline_text(xy, value, font=font(size, weight), fill=fill, anchor=anchor, spacing=spacing)
    else:
        d.text(xy, value, font=font(size, weight), fill=fill, anchor=anchor)


# ── inputs ────────────────────────────────────────────────────────────────

if not (CAPTURES / "manifest.json").exists():
    raise SystemExit(f"no captures in {CAPTURES}: run scripts/video/capture.py first")
MANIFEST = json.loads((CAPTURES / "manifest.json").read_text(encoding="utf-8"))
CAP, TG = {}, {}
for m in MANIFEST:
    img = Image.open(CAPTURES / f"{m['name']}.png").convert("RGB")
    if img.size != (CW, CH):
        raise ValueError(f"{m['name']}: {img.size} is not {CW}x{CH}")
    CAP[m["name"]] = img
    TG[m["name"]] = {k: (b["x"], b["y"], b["width"], b["height"]) for k, b in m["targets"].items()}

WORDS = json.loads((NARRATION / "words.json").read_text(encoding="utf-8"))
REPORT = {r["name"]: r for r in json.loads((NARRATION / "report.json").read_text(encoding="utf-8"))}
for key, r in REPORT.items():
    if r["score"] < 0.95:
        raise ValueError(f"narration {key} scored {r['score']:.3f}; regenerate it before rendering")

LOGO = Image.open(REPO / "docs" / "icons" / "android-chrome-512x512.png").convert("RGBA")
LOGOS = {}


def logo(size):
    if size not in LOGOS:
        LOGOS[size] = LOGO.resize((size, size), Image.Resampling.LANCZOS)
    return LOGOS[size]


def at(name, key, dx=0, dy=0):
    """Centre of a recorded target, in capture pixels."""
    x, y, w, h = TG[name][key]
    return (x + w / 2 + dx, y + h / 2 + dy)


def norm(word):
    return word.lower().strip(".,?!;:—–\"'")


def cue_maker(key, lead):
    words = [(norm(w["w"]), w["start"]) for w in WORDS[key]["words"]]

    def c(q, n=0):
        hits = [s for w, s in words if w == q]
        if len(hits) <= n:
            raise KeyError(f"{key}: no word {q!r} #{n}")
        return lead + hits[n]
    return c


FULL = (CW / 2, CH / 2, 1.0)
# The ticket page's middle column, x 447..1987 in capture pixels: framed
# without the sidebar or the properties panel.
HEADER = (1217, 525, 1.66)
COMPOSER = (1220, 1100, 1.66)


# ── scenes ────────────────────────────────────────────────────────────────
# Each builder gets c(word) -> scene time of that word, and the scene length.
# Shots: {"t", "frames": [(t, capture, fade)], "cam": [(t, (cx, cy, zoom))], "fade"}.
# Cursor: [(t, point or None)] in capture pixels; it glides between points.

def s_queue(c, D):
    q = "02-queue"
    search = TG[q]["search"]
    s_pt = (search[0] + 240, search[1] + search[3] / 2)
    apply = at(q, "apply")
    filters = (1450, 700, 1.3)
    tf, ts = c("filter"), c("search")
    return dict(
        shots=[dict(t=0, frames=[(0, q, 0), (ts + 0.3, "02b-search", 0), (ts + 0.95, "02c-filtered", 0.3)],
                    cam=[(0.4, FULL), (tf - 0.6, (1400, 900, 1.07)), (tf + 0.3, filters),
                         (ts + 1.1, filters), (ts + 1.8, FULL)])],
        cursor=[(tf - 0.1, at(q, "team_id", 120, 260)), (c("team") + 0.15, at(q, "team_id")),
                (c("category") + 0.15, at(q, "category")), (c("operator") + 0.15, at(q, "assigned")),
                (ts + 0.05, s_pt), (ts + 0.4, s_pt), (ts + 0.8, apply), (D, apply)],
        clicks=[ts + 0.1, ts + 0.85],
    )


def s_reply(c, D):
    q, t = "03a-ticket", "03t-"
    typing = sorted(n for n in CAP if n.startswith(t))
    t_open, t_type, t_send = c("open"), c("type"), c("send")
    first, last = t_type + 0.25, t_send - 0.5
    step = (last - first) / (len(typing) + 1)
    frames = [(t_open + 0.35, q, 0.25)] + [(first + i * step, n, 0) for i, n in enumerate(typing)]
    frames += [(last, "03c-reply", 0), (t_send + 0.12, "03d-sent", 0.25)]
    context, composer, header = (1493, 560, 1.2), COMPOSER, HEADER
    return dict(
        shots=[dict(t=0, frames=[(0, "02c-filtered", 0)], cam=[(0, FULL)]),
               dict(t=t_open + 0.35, fade=0.25, frames=frames,
                    cam=[(t_open + 0.5, FULL), (t_open + 2.0, context), (t_type - 0.4, context),
                         (t_type + 0.3, composer), (c("waiting") - 0.1, composer), (c("waiting") + 0.6, header)])],
        cursor=[(0, at("02-queue", "apply")), (0.2, at("02-queue", "apply")),
                (t_open + 0.15, at("02c-filtered", "row61")), (t_open + 0.6, at("02c-filtered", "row61")),
                (t_open + 0.61, None),
                (t_type - 0.25, at(q, "composer", 260, 160)), (first, at(q, "composer")),
                (last, at(q, "composer")), (t_send - 0.05, at(q, "send")), (t_send + 0.6, at(q, "send")),
                (t_send + 0.61, None)],
        clicks=[t_open + 0.2, first - 0.05, t_send],
        callouts=[(c("waiting") + 0.3, D + 1, "Stage changed: Waiting for customer")],
    )


def s_note(c, D):
    t = "04t-"
    typing = sorted(n for n in CAP if n.startswith(t) and n != "04t-00")
    t_tab = c("leave") + 0.05
    first, last = t_tab + 0.3, c("your") - 0.25
    step = (last - first) / (len(typing) + 1)
    frames = [(0, "03d-sent", 0), (t_tab + 0.1, "04t-00", 0.15)]
    frames += [(first + i * step, n, 0) for i, n in enumerate(typing)]
    t_add = last + 0.35
    frames += [(last, "04a-note", 0), (t_add + 0.12, "04b-noted", 0.25)]
    header, composer, noted = HEADER, COMPOSER, (1220, 760, 1.66)
    tab, submit = at("03d-sent", "tab"), at("04a-note", "submit")
    return dict(
        shots=[dict(t=0, frames=frames,
                    cam=[(0.25, header), (1.0, composer), (t_add + 0.2, composer), (t_add + 0.85, noted)])],
        cursor=[(0.7, (tab[0] + 300, tab[1] + 220)), (t_tab - 0.1, tab), (last, tab),
                (t_add - 0.05, submit), (t_add + 0.55, submit), (t_add + 0.56, None)],
        clicks=[t_tab, t_add],
        callouts=[(c("customer") - 0.1, D + 1, "Internal note: never sent to the customer")],
    )


def s_board(c, D):
    b = "05-board"

    # Two whole columns at a time (each is about 632 capture pixels wide):
    # New + In progress, then In progress + Waiting. The spoken stage is marked.
    nx, ny = TG[b]["new"][:2]
    wx = TG[b]["waiting"][0]
    left_pair = (nx - 70 + CW / 4, ny + 302, 2.0)
    right_pair = (wx + 572 - CW / 4, ny + 302, 2.0)
    card = at(b, "card", 150, 40)      # on the card, beside its number, not over it
    return dict(
        shots=[dict(t=0, frames=[(0, b, 0)],
                    cam=[(0.3, FULL), (c("new") - 0.35, (1280, 760, 1.06)), (c("new") + 0.15, left_pair),
                         (c("waiting") - 0.15, left_pair), (c("waiting") + 0.35, right_pair),
                         (c("one") + 0.2, right_pair), (c("one") + 1.1, FULL)])],
        marks=[(c("new") - 0.05, c("progress") + 0.1, b, "new"),
               (c("progress") - 0.05, c("waiting") + 0.1, b, "progress"),
               (c("waiting") + 0.15, c("one") + 0.5, b, "waiting")],
        cursor=[(c("waiting") + 0.25, (card[0] + 200, card[1] + 170)), (c("waiting") + 0.7, card),
                (c("one") + 1.3, card), (c("one") + 1.31, None)],
    )


def s_templates(c, D):
    p = "06b-picker"
    t_shot = c("one") - 0.15
    t_click = t_shot + 0.65
    picker = (1220, 1000, 1.66)
    return dict(
        shots=[dict(t=0, frames=[(0, "06a-templates", 0)], cam=[(0.3, FULL), (t_shot - 0.2, (1100, 760, 1.25))]),
               dict(t=t_shot, fade=0.35, frames=[(t_shot, p, 0), (t_click + 0.12, "06c-inserted", 0.2)],
                    cam=[(t_shot, picker)])],
        cursor=[(t_shot + 0.15, at(p, "picker")), (t_click - 0.1, at(p, "choice")),
                (t_click + 0.9, at(p, "choice")), (t_click + 0.91, None)],
        clicks=[t_click],
    )


def s_help(c, D):
    a = "07a-articles"
    t_click = c("customers") - 0.2
    link = at(a, "link")
    return dict(
        shots=[dict(t=0, frames=[(0, a, 0)], cam=[(0.3, FULL), (2.0, (1515, 700, 1.22))]),
               dict(t=t_click + 0.12, fade=0.35, frames=[(0, "07b-article", 0)],
                    cam=[(t_click, (1280, 650, 1.38)), (D, (1280, 650, 1.46))])],
        cursor=[(2.3, (link[0] + 320, link[1] + 220)), (3.0, link), (t_click + 0.45, link), (t_click + 0.46, None)],
        clicks=[t_click],
        callouts=[(c("link") - 0.1, D + 1, "Public link: /help/how-do-i-get-a-refund")],
    )


def s_channels(c, D):
    i = "08-integrations"
    fx, fy, fw, fh = TG[i]["first_tab"]
    lx, ly, lw, lh = TG[i]["last_tab"]
    cx, cy, cw, ch = TG[i]["checklist"]
    tabs = ((fx + lx + lw) / 2, fy + 330, 1.5)
    check = (cx + 700, cy + 160, 1.6)
    return dict(
        shots=[dict(t=0, frames=[(0, i, 0)],
                    cam=[(0.3, FULL), (1.8, tabs), (c("each") - 0.2, tabs), (c("checklist") + 0.1, check)])],
        cursor=[(c("connect") - 0.1, at(i, "first_tab", 40, 160)), (c("connect") + 0.3, at(i, "first_tab")),
                (c("uses"), at(i, "last_tab")), (c("each") + 0.1, at(i, "last_tab")),
                (c("checklist") + 0.1, at(i, "checklist")), (D - 0.3, at(i, "checklist")), (D - 0.29, None)],
    )


def s_assistant(c, D):
    t_answer = c("and") - 0.1
    t_propose = c("suggest") + 0.1
    t_confirm = c("confirm")
    px, py, pw, ph = TG["09t-typed"]["prompt"]
    prompt = (px + pw / 2, 1000, 1.55)   # down to the bottom edge: the "scripted examples" notice
    answer = (1700, 760, 1.3)
    confirm = at("09b-propose", "confirm")
    propose = (confirm[0] + 300, confirm[1] - 230, 1.55)
    return dict(
        shots=[dict(t=0, frames=[(0, "09t-typed", 0)], cam=[(0.4, FULL), (c("ask") + 0.2, prompt)]),
               dict(t=t_answer, fade=0.4, frames=[(0, "09a-ask", 0)], cam=[(0, answer)]),
               dict(t=t_propose, fade=0.4, frames=[(0, "09b-propose", 0), (t_confirm + 0.12, "09c-confirmed", 0.25)],
                    cam=[(0, propose)])],
        cursor=[(t_propose + 1.0, (confirm[0] + 260, confirm[1] + 200)), (t_propose + 2.2, confirm),
                (t_confirm + 0.5, confirm), (t_confirm + 0.51, None)],
        clicks=[t_confirm],
    )


def s_agent(c, D):
    a, b = "10a-agent", "10b-limits"
    t_choose, t_set, t_steps = c("choose"), c("set"), c("steps")
    mode = (1270, 470, 1.6)
    lx, ly, lw, lh = TG[b]["limits"]
    limits = (lx + 800, 600, 1.55)
    stops = (1330, at(b, "stops")[1], 1.6)
    return dict(
        shots=[dict(t=0, frames=[(0, a, 0)], cam=[(0.3, FULL), (2.0, (1280, 520, 1.2)),
                                                  (t_choose - 0.2, (1280, 520, 1.2)), (t_choose + 0.35, mode)]),
               dict(t=t_set + 0.05, fade=0.4, frames=[(0, b, 0)],
                    cam=[(0, limits), (t_steps - 0.1, limits), (t_steps + 0.6, stops)])],
        cursor=[(t_choose, at(a, "mode", 300, 160)), (t_choose + 0.5, at(a, "mode")),
                (t_set, at(a, "mode")), (t_set + 0.01, None)],
        callouts=[(t_choose + 0.4, t_set + 0.15, "One reply, then an operator  ·  or the whole conversation")],
        marks=[(t_steps + 0.3, D + 1, b, "stops")],
    )


def s_reports(c, D):
    t_tables = c("by") - 0.3
    return dict(
        shots=[dict(t=0, frames=[(0, "11a-overview", 0)], cam=[(0.3, FULL), (c("response"), (1510, 680, 1.2))]),
               dict(t=t_tables, fade=0.4, frames=[(0, "11b-reports", 0)],
                    cam=[(0, (1493, 860, 1.2)), (D, (1493, 870, 1.22))])],
    )


def card_intro(im, d, tau, c):
    shift = int((1 - ease((tau - 0.15) / 0.7)) * 26)
    a = ease((tau - 0.15) / 0.6)
    if a > 0:
        text(d, (VX, 214 + shift), "Your customers write from everywhere.", 64,
             tuple(int(BG[i] + (WHITE[i] - BG[i]) * a) for i in range(3)), "bold")
    labels = [("@", "Email", c("email")), ("○", "Website chat", c("website")),
              ("#", "Slack", c("slack")), ("➤", "Telegram", c("telegram"))]
    cw, gap, y0 = 368, 32, 400
    for i, (sym, label, t_on) in enumerate(labels):
        u = ease((tau - t_on + 0.15) / 0.45)
        if u <= 0:
            continue
        x, y = VX + i * (cw + gap), y0 + int((1 - u) * 30)
        layer = Image.new("RGBA", (cw, 120), (0, 0, 0, 0))
        ld = ImageDraw.Draw(layer)
        ld.rounded_rectangle((0, 0, cw - 1, 119), radius=18, fill=PANEL + (255,), outline=LINE + (255,), width=2)
        ld.rounded_rectangle((24, 34, 76, 86), radius=12, fill=(20, 14, 32, 255), outline=LINE + (255,))
        ld.text((50, 60), sym, font=font(26, "symbol"), fill=VIOLET + (255,), anchor="mm")
        ld.text((98, 60), label, font=font(32, "bold"), fill=WHITE + (255,), anchor="lm")
        layer.putalpha(Image.eval(layer.getchannel("A"), lambda v: int(v * u)))
        im.paste(layer, (x, y), layer)
    t_hub = c("brings")
    u = ease((tau - t_hub) / 0.9)
    hub = (960 - 380, 660, 960 + 380, 800)
    if u > 0:
        for i in range(4):
            x = VX + i * (cw + gap) + cw // 2
            pts = [(x, 520), (x, 590), (960, 590), (960, 660)]
            total = sum(abs(pts[k + 1][0] - pts[k][0]) + abs(pts[k + 1][1] - pts[k][1]) for k in range(3))
            left = total * u
            for k in range(3):
                (x0, y0), (x1, y1) = pts[k], pts[k + 1]
                seg = abs(x1 - x0) + abs(y1 - y0)
                if left <= 0:
                    break
                f = min(1.0, left / seg) if seg else 1.0
                d.line((x0, y0, x0 + (x1 - x0) * f, y0 + (y1 - y0) * f), fill=(84, 58, 124), width=3)
                left -= seg
    v = ease((tau - t_hub - 0.5) / 0.5)
    if v > 0:
        layer = Image.new("RGBA", (hub[2] - hub[0], hub[3] - hub[1]), (0, 0, 0, 0))
        ld = ImageDraw.Draw(layer)
        ld.rounded_rectangle((0, 0, layer.width - 1, layer.height - 1), radius=22,
                             fill=(25, 15, 43, 255), outline=VIOLET + (255,), width=2)
        lg = logo(64)
        layer.paste(lg, (150, 38), lg)
        ld.text((236, 70), "One shared queue", font=font(44, "bold"), fill=WHITE + (255,), anchor="lm")
        layer.putalpha(Image.eval(layer.getchannel("A"), lambda p: int(p * v)))
        im.paste(layer, hub[:2], layer)
    w = ease((tau - c("server") + 0.1) / 0.45)
    if w > 0:
        label = "On a server you control"
        tw = font(28, "bold").getlength(label)
        x0, y0 = 960 - tw / 2 - 30, 850 + int((1 - w) * 14)
        col = tuple(int(BG[i] + (VIOLET[i] - BG[i]) * w) for i in range(3))
        d.rounded_rectangle((x0, y0, x0 + tw + 60, y0 + 62), radius=31, outline=col, width=2,
                            fill=tuple(int(BG[i] + ((20, 14, 32)[i] - BG[i]) * w) for i in range(3)))
        text(d, (960, y0 + 31), label, 28, tuple(int(BG[i] + (WHITE[i] - BG[i]) * w) for i in range(3)),
             "bold", anchor="mm")


def card_outro(im, d, tau, c):
    def fade(col, u):
        return tuple(int(BG[i] + (col[i] - BG[i]) * u) for i in range(3))
    u = ease((tau - 0.1) / 0.6)
    s = int((1 - u) * 24)
    text(d, (VX, 214 + s), "SELF-HOSTED", 22, fade(VIOLET, u), "bold")
    text(d, (VX - 4, 252 + s), "Your server.\nYour data.", 104, fade(WHITE, u), "bold", spacing=0)
    v = ease((tau - c("try") + 0.1) / 0.5)
    if v > 0:
        text(d, (VX, 560 + int((1 - v) * 20)), "Try the live demo", 34, fade(MUTED, v), "semibold")
    v = ease((tau - c("spooldesk", 1) + 0.1) / 0.5)
    if v > 0:
        text(d, (VX - 4, 600 + int((1 - v) * 20)), "spooldesk.net", 112, fade(VIOLET, v), "bold")
    v = ease((tau - c("install") + 0.1) / 0.5)
    if v > 0:
        label = "Install it yourself  ·  github.com/spooldesk/spool"
        tw = font(27, "bold").getlength(label)
        y = 800 + int((1 - v) * 16)
        d.rounded_rectangle((VX, y, VX + tw + 56, y + 64), radius=32, fill=fade((238, 233, 250), v))
        text(d, (VX + 28, y + 32), label, 27, fade((16, 10, 25), v), "bold", anchor="lm")
    lg = logo(300)
    a = ease((tau - 0.3) / 0.8)
    layer = lg.copy()
    layer.putalpha(Image.eval(lg.getchannel("A"), lambda p: int(p * a)))
    im.paste(layer, (1444, 330), layer)


SCENES = [
    dict(key="intro", card=card_intro, lead=0.9, tail=0.7, label="PRODUCT WALKTHROUGH", foot="SELF-HOSTED HELPDESK"),
    dict(key="queue", build=s_queue, title="The ticket queue", tail=1.3),
    dict(key="reply", build=s_reply, title="Replying"),
    dict(key="note", build=s_note, title="Internal notes", tail=1.0),
    dict(key="board", build=s_board, title="The board"),
    dict(key="templates", build=s_templates, title="Templates"),
    dict(key="help", build=s_help, title="Help centre"),
    dict(key="channels", build=s_channels, title="Channels"),
    dict(key="assistant", build=s_assistant, title="AI assistant", foot="SCRIPTED AI EXAMPLE · NO MODEL CALLED"),
    dict(key="agent", build=s_agent, title="Customer AI agent", tail=0.9),
    dict(key="reports", build=s_reports, title="Reports", tail=1.0),
    dict(key="outro", card=card_outro, tail=3.2, label="PRODUCT WALKTHROUGH", foot="SELF-HOSTED HELPDESK"),
]

start, chapter = 0.0, 0
for sc in SCENES:
    sc.setdefault("lead", LEAD)
    sc.setdefault("tail", TAIL)
    sc.setdefault("foot", "ACTUAL DASHBOARD · FICTIONAL DATA")
    sc["voice"] = WORDS[sc["key"]]["seconds"]
    sc["D"] = sc["lead"] + sc["voice"] + sc["tail"]
    sc["start"] = start
    start += sc["D"]
    sc["c"] = cue_maker(sc["key"], sc["lead"])
    if "build" in sc:
        chapter += 1
        sc["chapter"] = chapter
        sc.update(dict(shots=[], cursor=[], clicks=[], callouts=[], marks=[]) | sc["build"](sc["c"], sc["D"]))
TOTAL = start


# ── drawing ───────────────────────────────────────────────────────────────

MASK = Image.new("L", (VW, VH), 0)
ImageDraw.Draw(MASK).rounded_rectangle((0, 0, VW - 1, VH - 1), radius=16, fill=255)


def base(sc):
    if "base" in sc:
        return sc["base"].copy()
    im = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(im)
    lg = logo(42)
    im.paste(lg, (VX, 42), lg)
    text(d, (VX + 56, 63), "spooldesk", 34, WHITE, "bold", anchor="lm")
    text(d, (VX + 56 + font(34, "bold").getlength("spooldesk"), 63), ".", 34, VIOLET, "bold", anchor="lm")
    if "chapter" in sc:
        title_w = font(30, "bold").getlength(sc["title"])
        text(d, (VX + VW, 63), sc["title"], 30, WHITE, "bold", anchor="rm")
        text(d, (VX + VW - title_w - 18, 63), f"{sc['chapter']:02d}", 22, VIOLET, "bold", anchor="rm")
        d.rounded_rectangle((VX - 10, VY - 10, VX + VW + 10, VY + VH + 10), radius=24,
                            fill=(8, 7, 12), outline=(69, 50, 103), width=2)
    else:
        text(d, (VX + VW, 63), sc["label"], 20, MUTED, "bold", anchor="rm")
    d.line((VX, 1040, VX + VW, 1040), fill=LINE, width=2)
    text(d, (VX, 1058), "SPOOLDESK.NET", 16, MUTED, "bold", anchor="lm")
    text(d, (VX + VW, 1058), sc["foot"], 16, MUTED, anchor="rm")
    sc["base"] = im
    return im.copy()


def lerp_cam(keys, tau):
    if tau <= keys[0][0]:
        return keys[0][1]
    for (t0, v0), (t1, v1) in zip(keys, keys[1:]):
        if t0 <= tau < t1:
            u = ease((tau - t0) / (t1 - t0)) if t1 > t0 else 1
            return tuple(a + (b - a) * u for a, b in zip(v0, v1))
    return keys[-1][1]


def view_rect(cam):
    cx, cy, z = cam
    w, h = CW / z, CH / z
    left = min(max(cx - w / 2, 0), CW - w)
    top = min(max(cy - h / 2, 0), CH - h)
    return left, top, w, h


def frame_at(frames, tau):
    """The capture showing at tau, and the one fading out under it."""
    idx = max(i for i, f in enumerate(frames) if f[0] <= tau or i == 0)
    t, name, fade = frames[idx]
    if idx and fade and tau - t < fade:
        return name, frames[idx - 1][1], ease((tau - t) / fade)
    return name, None, 1.0


def shot_image(shot, tau):
    rect = view_rect(lerp_cam(shot["cam"], tau))
    box = (rect[0], rect[1], rect[0] + rect[2], rect[1] + rect[3])
    name, prev, u = frame_at(shot["frames"], tau)
    img = CAP[name].resize((VW, VH), Image.Resampling.BICUBIC, box=box)
    if prev:
        img = Image.blend(CAP[prev].resize((VW, VH), Image.Resampling.BICUBIC, box=box), img, u)
    scale = VW / rect[2]
    return img, (lambda p: (VX + (p[0] - rect[0]) * scale, VY + (p[1] - rect[1]) * scale)), scale


def cursor_at(track, tau):
    if not track or tau < track[0][0]:
        return None
    for (t0, p0), (t1, p1) in zip(track, track[1:]):
        if t0 <= tau < t1:
            if p0 is None:
                return None
            if p1 is None:
                return p0
            u = ease((tau - t0) / (t1 - t0)) if t1 > t0 else 1
            return (p0[0] + (p1[0] - p0[0]) * u, p0[1] + (p1[1] - p0[1]) * u)
    return track[-1][1]


ARROW = [(0, 0), (0, 31), (8, 24), (15, 38), (22, 35), (15, 21), (27, 20)]


def draw_cursor(im, p, ripple=None):
    x, y = p
    pad = 60
    layer = Image.new("RGBA", (2 * pad, 2 * pad), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    if ripple is not None:
        r = 10 + 38 * ripple
        d.ellipse((pad - r, pad - r, pad + r, pad + r), outline=VIOLET + (int(220 * (1 - ripple)),), width=3)
    d.polygon([(pad + a, pad + b) for a, b in ARROW], fill=(255, 255, 255, 255), outline=(15, 12, 24, 255), width=2)
    im.paste(layer, (int(x) - pad, int(y) - pad), layer)


def draw_callout(im, label, u):
    f = font(26, "bold")
    tw = f.getlength(label)
    w, h = int(tw + 74), 60
    layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    d.rounded_rectangle((0, 0, w - 1, h - 1), radius=30, fill=(16, 12, 26, 240), outline=VIOLET + (255,), width=2)
    d.ellipse((24, h / 2 - 6, 36, h / 2 + 6), fill=VIOLET + (255,))
    d.text((50, h / 2), label, font=f, fill=WHITE + (255,), anchor="lm")
    layer.putalpha(Image.eval(layer.getchannel("A"), lambda v: int(v * u)))
    x = VX + (VW - w) // 2
    y = VY + VH - h - 30 + int((1 - u) * 14)
    im.paste(layer, (x, y), layer)


def scene_frame(i, tau):
    sc = SCENES[i]
    im = base(sc)
    d = ImageDraw.Draw(im)
    if "card" in sc:
        sc["card"](im, d, tau, sc["c"])
        return im
    shots = sc["shots"]
    k = max(j for j, s in enumerate(shots) if s["t"] <= tau or j == 0)
    img, to_screen, scale = shot_image(shots[k], tau)
    fade = shots[k].get("fade", 0)
    if k and fade and tau - shots[k]["t"] < fade:
        prev, _, _ = shot_image(shots[k - 1], tau)
        img = Image.blend(prev, img, ease((tau - shots[k]["t"]) / fade))
    im.paste(img, (VX, VY), MASK)
    for t0, t1, name, key in sc["marks"]:
        u = min(ease((tau - t0) / 0.4), ease((t1 - tau) / 0.3))
        if u > 0:
            x, y, w, h = TG[name][key]
            (x0, y0), (x1, y1) = to_screen((x - 14, y - 10)), to_screen((x + w + 14, y + h + 10))
            layer = Image.new("RGBA", (int(x1 - x0) + 8, int(y1 - y0) + 8), (0, 0, 0, 0))
            ImageDraw.Draw(layer).rounded_rectangle((2, 2, layer.width - 3, layer.height - 3), radius=12,
                                                    outline=VIOLET + (int(255 * u),), width=3,
                                                    fill=VIOLET + (int(26 * u),))
            im.paste(layer, (int(x0) - 4, int(y0) - 4), layer)
    for t0, t1, label in sc["callouts"]:
        u = min(ease((tau - t0) / 0.35), ease((t1 - tau) / 0.3))
        if u > 0:
            draw_callout(im, label, u)
    p = cursor_at(sc["cursor"], tau)
    if p is not None:
        ripple = next(((tau - t) / 0.6 for t in sc["clicks"] if 0 <= tau - t < 0.6), None)
        draw_cursor(im, to_screen(p), ripple)
    return im


LAST = {}


def scene_index(t):
    return next((i for i, s in enumerate(SCENES) if s["start"] <= t < s["start"] + s["D"]), len(SCENES) - 1)


def render(t):
    i = scene_index(t)
    sc = SCENES[i]
    tau = t - sc["start"]
    im = scene_frame(i, tau)
    if i and tau < XF:
        if i - 1 not in LAST:
            LAST[i - 1] = scene_frame(i - 1, SCENES[i - 1]["D"] - 1e-3)
        im = Image.blend(LAST[i - 1], im, ease(tau / XF))
    d = ImageDraw.Draw(im)
    d.line((VX, 1040, VX + VW * min(t / TOTAL, 1), 1040), fill=VIOLET, width=3)
    black = Image.new("RGB", (W, H), BG)
    if t < 0.5:
        im = Image.blend(black, im, ease(t / 0.5))
    if t > TOTAL - 0.9:
        im = Image.blend(im, black, ease((t - TOTAL + 0.9) / 0.9))
    return im


# ── sound ─────────────────────────────────────────────────────────────────

SR = 48000


def pad(seconds):
    """A slow four-chord pad, the same voicing as the short tour's."""
    n = int(seconds * SR)
    out = np.zeros((n, 2), dtype=np.float64)
    chords = [(146.83, 220, 293.66, 349.23), (130.81, 196, 261.63, 329.63),
              (174.61, 220, 261.63, 349.23), (130.81, 196, 293.66, 392)]
    for k, s in enumerate(range(0, int(seconds) + 1, 8)):
        length = min(10.0, seconds - s)
        if length <= 0:
            break
        t = np.arange(int(length * SR)) / SR
        env = np.minimum(t / 1.8, 1) * np.minimum((length - t) / 2.2, 1)
        for j, f in enumerate(chords[k % 4]):
            for ch, detune in ((0, 1.0), (1, 1.0015)):
                tone = np.sin(2 * np.pi * f * detune * t + 0.35 * j + ch) * (1 + 0.12 * np.sin(2 * np.pi * 0.21 * t + j))
                tone += 0.2 * np.sin(2 * np.pi * f * 2.001 * detune * t)
                out[int(s * SR):int(s * SR) + len(t), ch] += 0.01 * tone * env
    return out


def click_sound():
    rng = np.random.default_rng(7)
    n = int(0.035 * SR)
    t = np.arange(n) / SR
    noise = np.diff(rng.standard_normal(n + 1)) * np.exp(-t * 260)
    tock = np.sin(2 * np.pi * 1850 * t) * np.exp(-t * 180)
    return 0.05 * noise + 0.06 * tock


def moving_average(x, k):
    c = np.cumsum(np.concatenate(([0.0], x)))
    out = (c[k:] - c[:-k]) / k
    head = k // 2
    return np.concatenate((np.full(head, out[0]), out, np.full(len(x) - len(out) - head, out[-1])))


def build_audio(path):
    import pyloudnorm as pyln
    import torchaudio as ta
    n = int((TOTAL + 0.5) * SR)
    voice = np.zeros(n)
    for idx, sc in enumerate(SCENES, 1):
        wav, sr = ta.load(str(NARRATION / f"{idx:02d}-{sc['key']}.flac"))
        x = ta.functional.resample(wav, sr, SR)[0].numpy().astype(np.float64)
        a = int((sc["start"] + sc["lead"]) * SR)
        voice[a:a + len(x)] += x[:n - a]
    meter = pyln.Meter(SR)
    music = pad(TOTAL + 0.5)
    music = pyln.normalize.loudness(music, meter.integrated_loudness(music), -33.0)
    # Duck the music a little while someone is speaking.
    hop = int(0.05 * SR)
    rms = np.sqrt(moving_average(voice ** 2, hop))
    talking = moving_average((rms > 0.01).astype(np.float64), int(0.5 * SR))
    music *= (1 - 0.4 * np.clip(talking, 0, 1))[:, None]
    t = np.arange(n) / SR
    music *= (np.minimum(t / 1.5, 1) * np.clip((TOTAL - t) / 2.5, 0, 1))[:, None]
    mix = music + voice[:, None]
    tick = click_sound()
    for sc in SCENES:
        for c in sc.get("clicks", []):
            a = int((sc["start"] + c) * SR)
            mix[a:a + len(tick)] += tick[:, None]
    mix = pyln.normalize.loudness(mix, meter.integrated_loudness(mix), -16.0)
    knee = 0.85
    over = np.abs(mix) > knee
    mix[over] = np.sign(mix[over]) * (knee + (1 - knee) * np.tanh((np.abs(mix[over]) - knee) / (1 - knee)))
    with wave.open(str(path), "wb") as f:
        f.setnchannels(2)
        f.setsampwidth(2)
        f.setframerate(SR)
        f.writeframes((np.clip(mix, -1, 1) * 32767).astype("<i2").tobytes())
    print(f"audio: {TOTAL:.1f}s, {meter.integrated_loudness(mix):.1f} LUFS, peak {np.abs(mix).max():.2f}")


# ── subtitles ─────────────────────────────────────────────────────────────

RU = {
    "intro": ["Клиенты пишут отовсюду — на почту, на сайт, в Slack, в Telegram.",
              "Spooldesk собирает всё это в одном месте, на сервере, которым управляете вы.",
              "Сейчас покажу, как это работает."],
    "queue": ["Это очередь обращений.",
              "Сюда попадает каждый разговор, из какого бы канала он ни пришёл.",
              "Фильтруйте по команде, категории или оператору — или просто найдите то, что помните."],
    "reply": ["Открываете обращение — и видите всю историю: кто пишет, что пишет и откуда.",
              "Ответ пишете прямо здесь.",
              "После отправки обращение само переходит в «ждём клиента» — следить вручную не нужно."],
    "note": ["Нужна помощь коллеги? Оставьте внутреннюю заметку.",
             "Команда её видит. Клиент — никогда."],
    "board": ["На доске та же работа, разложенная по стадиям: новые, в работе, ждём клиента.",
              "Один взгляд — и ясно, за что браться дальше."],
    "templates": ["Ответы, которые вы даёте каждый день, становятся шаблонами.",
                  "Один клик — и не нужно в сороковой раз печатать одно и то же."],
    "help": ["А ответ, который стоит сохранить, добавьте в базу знаний.",
             "Клиенты найдут его сами, а у каждой статьи есть ссылка, которую можно отправить."],
    "channels": ["Каналы включаются в разделе «Интеграции».",
                 "Подключайте только нужное — у каждого канала есть чек-лист, и вы точно знаете, что он работает."],
    "assistant": ["А дальше — ИИ-ассистент.",
                  "Спросите обычными словами: каким обращениям нужен ответ? — ответ придёт за секунды.",
                  "Он может и предложить изменение, например перевести обращение дальше.",
                  "Но ничего не произойдёт, пока вы не подтвердите."],
    "agent": ["ИИ можно разрешить отвечать клиентам и напрямую.",
              "Выберите один ответ или целый диалог и задайте ограничения.",
              "ИИ уступает место, как только подключается человек или клиент просит оператора."],
    "reports": ["Отчёты: время ответа и решения, оценки клиентов — по командам и операторам."],
    "outro": ["Spooldesk работает на вашем сервере — данные остаются под вашим контролем.",
              "Попробуйте демо на spooldesk.net, а когда будете готовы — установите у себя."],
}


def subtitles(lang):
    cues = []
    for sc in SCENES:
        sentences = WORDS[sc["key"]]["sentences"]
        texts = RU[sc["key"]] if lang == "RU" else [s["text"].replace("spooldesk dot net", "spooldesk.net") for s in sentences]
        if len(texts) != len(sentences):
            raise ValueError(f"{lang} {sc['key']}: {len(texts)} lines for {len(sentences)} sentences")
        for s, line in zip(sentences, texts):
            a = sc["start"] + sc["lead"] + s["start"]
            b = sc["start"] + sc["lead"] + s["end"]
            parts = split_cue(line)
            total = sum(len(p) for p in parts)
            for p in parts:
                share = (b - a) * len(p) / total
                cues.append([a, a + share, p])
                a += share
    for k, cue in enumerate(cues):
        nxt = cues[k + 1][0] if k + 1 < len(cues) else TOTAL
        cue[1] = min(cue[1] + 0.7, nxt - 0.05)

    (OUT / f"Spooldesk-walkthrough-{lang}.srt").write_text(srt(cues), encoding="utf-8")
    (OUT / f"Spooldesk-walkthrough-{lang}.vtt").write_text(vtt(cues), encoding="utf-8", newline="\n")
    return len(cues)


# ── output ────────────────────────────────────────────────────────────────

def moments():
    """A frame from the middle of every shot and after every click."""
    out = []
    for sc in SCENES:
        s = sc["start"]
        if "card" in sc:
            out.append(s + sc["D"] - 0.8)
            continue
        for shot in sc["shots"]:
            out.append(s + shot["t"] + 0.6)
        out += [s + c + 0.5 for c in sc["clicks"]]
        out.append(s + sc["D"] - 0.1)
    return sorted(set(round(t, 2) for t in out))


def previews():
    times = moments()
    cols = 5
    rows = (len(times) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * 384, rows * 216), (18, 18, 20))
    folder = BUILD / "preview"
    folder.mkdir(exist_ok=True)
    for old in folder.glob("*.jpg"):
        old.unlink()
    for n, t in enumerate(times):
        frame = render(t)
        frame.save(folder / f"{t:06.2f}.jpg", quality=90)
        sheet.paste(frame.resize((384, 216), Image.Resampling.LANCZOS), ((n % cols) * 384, (n // cols) * 216))
    sheet.save(OUT / "Storyboard.jpg", quality=92)
    reply = SCENES[2]
    render(reply["start"] + reply["c"]("send") - 0.3).save(OUT / "Spooldesk-walkthrough-poster.jpg", quality=95)
    print(f"{len(times)} preview frames")


def web_copy(encoder, master):
    """What docs/video/ serves: a lighter encode of the master (flat screens
    and slow pans suit -tune animation), a 720p poster and the WebVTT files."""
    WEB.mkdir(exist_ok=True)
    target = WEB / f"{WEB_NAME}.mp4"
    subprocess.run([encoder, "-hide_banner", "-loglevel", "warning", "-y", "-i", str(master),
                    "-c:v", "libx264", "-preset", "slow", "-tune", "animation", "-crf", "23", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(target)], check=True)
    size = target.stat().st_size
    if size >= WEB_LIMIT:
        raise RuntimeError(f"{target.name} is {size / 2**20:.1f} MiB; Cloudflare Pages takes under 25 MiB")
    poster = Image.open(OUT / "Spooldesk-walkthrough-poster.jpg").resize((1280, 720), Image.Resampling.LANCZOS)
    poster.save(WEB / f"{WEB_NAME}-poster.jpg", quality=85, optimize=True)
    for lang in ("EN", "RU"):
        shutil.copyfile(OUT / f"Spooldesk-walkthrough-{lang}.vtt", WEB / f"{WEB_NAME}-{lang.lower()}.vtt")
    print(f"web copy: {size / 2**20:.1f} MiB in {WEB}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--preview", action="store_true", help="stills, audio and subtitles; no video")
    args = ap.parse_args()
    encoder = None if args.preview else ffmpeg()
    OUT.mkdir(parents=True, exist_ok=True)
    for sc in SCENES:
        print(f"{sc['key']:10s} {sc['start']:6.2f}  +{sc['D']:5.2f}")
    print(f"total {TOTAL:.2f}s")
    print("subtitles:", subtitles("EN"), "EN cues,", subtitles("RU"), "RU cues")
    audio = BUILD / "mix.wav"
    build_audio(audio)
    previews()
    if args.preview:
        return
    target = OUT / f"{NAME}.mp4"
    cmd = [encoder, "-hide_banner", "-loglevel", "warning", "-y",
           "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS), "-i", "pipe:0",
           "-i", str(audio), "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", "-t", f"{TOTAL:.3f}", str(target)]
    frames = int(TOTAL * FPS)
    with (BUILD / "render.log").open("w") as log:
        p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=log)
        try:
            for n in range(frames):
                p.stdin.write(render(n / FPS).tobytes())
                if n % (FPS * 10) == 0:
                    print(f"rendered {n // FPS}/{int(TOTAL)}s", flush=True)
            p.stdin.close()
            if p.wait():
                raise RuntimeError(f"encoding failed; see {BUILD / 'render.log'}")
        finally:
            if p.poll() is None:
                p.kill()
    print("finished", target)
    web_copy(encoder, target)


if __name__ == "__main__":
    main()
