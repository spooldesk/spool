"""Record the widget GIF for the README and the landing page.

A still of a chat panel is a picture of a chat panel. The thing worth showing
is the round trip: a visitor types on somebody's website, an operator answers
somewhere else entirely, and the answer arrives without the visitor doing
anything. That needs an animation.

    python scripts/capture_widget_gif.py

Both halves are real. The visitor's message goes through the public endpoint
with its rate limits and its session token; the operator's reply is a row
written to the database while the recording runs, and the panel finds it by
polling, exactly as it would in production. Nothing is faked into the DOM.

Content comes from scripts/seed_demo_data.py, which is generated from word
lists, so no real conversation passes through here.
"""

from __future__ import annotations

import os
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "demo_support.db"
OUT = ROOT / "demo_screenshots" / "widget.gif"
PORT = 8097
BASE = f"http://127.0.0.1:{PORT}"

# The panel's real size, from the loader's stylesheet. Scale factor 1, not 2:
# a GIF is not a screenshot, and doubling every pixel quadruples a file that
# has to load on a landing page.
VIEWPORT = {"width": 400, "height": 680}
FRAME_MS = 110

VISITOR_SAYS = "Hi! Checkout hangs on the payment step. Card works elsewhere."
OPERATOR_REPLIES = (
    "That's our payment provider rate-limiting the card, not your card. "
    "I've raised the limit on your account — try once more?"
)


def _wait_for_port(port: int, timeout: float = 40.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket() as s:
            s.settimeout(0.5)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.3)
    return False


def _enable_widget() -> None:
    con = sqlite3.connect(DB)
    try:
        con.execute(
            "INSERT INTO app_settings (key, value, updated_at) "
            "VALUES ('widget_enabled', '1', datetime('now')) "
            "ON CONFLICT(key) DO UPDATE SET value = '1'"
        )
        con.commit()
    finally:
        con.close()


def _newest_widget_ticket() -> int | None:
    """The conversation the recording just opened."""
    con = sqlite3.connect(DB)
    try:
        row = con.execute(
            "SELECT t.id FROM tickets t JOIN users u ON u.id = t.user_id "
            "WHERE u.channel = 'widget' ORDER BY t.id DESC LIMIT 1"
        ).fetchone()
        return row[0] if row else None
    finally:
        con.close()


def _operator_answers(ticket_id: int, text: str) -> None:
    """Write the reply the way the dashboard would, mid-recording.

    A row, not a DOM edit. The panel has to *find* it, which is the part of
    the story the GIF exists to tell.
    """
    con = sqlite3.connect(DB)
    try:
        con.execute(
            "INSERT INTO messages (ticket_id, sender_type, sender_id, text, "
            "is_internal, created_at) VALUES (?, 'admin', 0, ?, 0, datetime('now'))",
            (ticket_id, text),
        )
        con.commit()
    finally:
        con.close()


def main() -> int:
    from PIL import Image
    from playwright.sync_api import sync_playwright

    print("seeding…")
    seeded = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "seed_demo_data.py")],
        cwd=ROOT, capture_output=True, text=True,
    )
    if seeded.returncode != 0:
        print(seeded.stderr[-1500:])
        return 1
    if not DB.exists():
        print("the seed did not produce demo_support.db")
        return 1
    _enable_widget()

    env = {
        **os.environ,
        "TESTING": "1", "DEMO_MODE": "1",
        "DATABASE_URL": f"sqlite+aiosqlite:///{DB.as_posix()}",
        "BOT_TOKEN": "1:gif", "ADMIN_CHAT_ID": "-100",
        "ACCESS_PASSWORD": "admin", "DASH_SECRET_KEY": "gif",
    }
    server = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "src.dashboard.app:app",
         "--host", "127.0.0.1", "--port", str(PORT), "--log-level", "warning"],
        cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    frames_dir = ROOT / ".widget_gif_frames"
    frames_dir.mkdir(exist_ok=True)

    try:
        if not _wait_for_port(PORT):
            print("the dashboard did not come up")
            return 1

        with sync_playwright() as p:
            browser = p.chromium.launch()
            # A visitor who has never been here: no stored session, so the
            # panel opens the way it does for a stranger.
            page = browser.new_page(viewport=VIEWPORT)
            page.goto(f"{BASE}/widget/frame")
            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(700)

            # (frame, milliseconds). A pause is a long duration on one
            # frame, not the same frame several times: Pillow drops a frame
            # identical to its predecessor, so repeats vanish and take the
            # pause with them.
            shots: list[tuple[Path, int]] = []

            def shot(hold: int = FRAME_MS) -> None:
                f = frames_dir / f"{len(shots):03d}.png"
                page.screenshot(path=str(f))
                shots.append((f, hold))

            shot(1200)                   # hold on the home screen

            page.evaluate("document.getElementById('ctaHome').click()")
            page.wait_for_timeout(400)
            shot(500)

            # Typed in pieces so the message appears the way typing looks,
            # rather than arriving whole between two frames.
            box = page.locator("#text")
            box.click()
            step = max(1, len(VISITOR_SAYS) // 14)
            for cut in range(step, len(VISITOR_SAYS) + step, step):
                box.fill(VISITOR_SAYS[:cut])
                page.wait_for_timeout(40)
                shot()

            page.keyboard.press("Enter")
            for _ in range(6):
                page.wait_for_timeout(120)
                shot()
            shot(900)                    # the message has landed

            ticket_id = _newest_widget_ticket()
            if ticket_id is None:
                print("the visitor's message did not open a ticket")
                return 1

            # Somewhere else, an operator answers. The panel is polling; it
            # will find this within a few seconds on its own.
            _operator_answers(ticket_id, OPERATOR_REPLIES)
            print(f"  operator replied on ticket #{ticket_id}; waiting for the panel…")

            arrived = False
            for _ in range(45):
                page.wait_for_timeout(120)
                shot()
                if page.locator("#log .msg.agent").count():
                    arrived = True
                    break
            if not arrived:
                print("the reply never reached the panel")
                return 1

            shot(2600)                   # hold on the answer, then loop

            browser.close()

        images = [Image.open(f).convert("P", palette=Image.ADAPTIVE, colors=96)
                  for f, _ in shots]
        images[0].save(OUT, save_all=True, append_images=images[1:],
                       duration=[ms for _, ms in shots], loop=0, optimize=True)
        size_kb = OUT.stat().st_size / 1024
        print(f"wrote {OUT.relative_to(ROOT)}  —  {len(images)} frames, {size_kb:.0f} KB")
        if size_kb > 4000:
            print("  (over 4 MB — GitHub will be slow to load it)")
        return 0
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
        for f in frames_dir.glob("*.png"):
            f.unlink()
        try:
            frames_dir.rmdir()
        except OSError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
