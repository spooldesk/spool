"""Record the board GIF for the README.

A still shows the board; the point of it is that a card moves and the change
reaches everyone else's screen. That needs an animation.

    python scripts/capture_board_gif.py

Drives the real page — the drag goes through the same handlers an operator
uses, and the stage change is a real POST. Content comes from
scripts/seed_demo_data.py, so nothing here is anyone's actual ticket.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "demo_screenshots" / "board.gif"
PORT = 8098
BASE = f"http://127.0.0.1:{PORT}"

# Big enough to read the cards, small enough that GitHub will not choke.
VIEWPORT = {"width": 1180, "height": 620}
FRAME_MS = 90


def _wait_for_port(port: int, timeout: float = 40.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket() as s:
            s.settimeout(0.5)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.3)
    return False


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

    env = {
        **os.environ,
        "TESTING": "1", "DEMO_MODE": "1",
        "DATABASE_URL": f"sqlite+aiosqlite:///{(ROOT / 'demo_support.db').as_posix()}",
        "BOT_TOKEN": "1:gif", "ADMIN_CHAT_ID": "-100",
        "ACCESS_PASSWORD": "admin", "DASH_SECRET_KEY": "gif",
    }
    server = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "src.dashboard.app:app",
         "--host", "127.0.0.1", "--port", str(PORT), "--log-level", "warning"],
        cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    frames_dir = ROOT / ".gif_frames"
    frames_dir.mkdir(exist_ok=True)
    try:
        if not _wait_for_port(PORT):
            print("the dashboard did not come up")
            return 1

        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport=VIEWPORT)
            page.goto(f"{BASE}/login")
            page.evaluate("localStorage.setItem('dash-theme', 'dark')")
            page.fill('input[name="email"]', "admin@local")
            page.fill('input[name="password"]', "admin")
            page.click('button[type="submit"]')
            page.wait_for_load_state("networkidle")
            page.goto(f"{BASE}/board")
            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(600)

            shots: list[Path] = []

            def shot() -> None:
                f = frames_dir / f"{len(shots):03d}.png"
                page.screenshot(path=str(f))
                shots.append(f)

            for _ in range(6):          # settle on the board
                shot()

            card = page.locator('.card[draggable="true"]').first
            target = page.locator('section[data-column="in_progress"] .drop-zone')
            cb, tb = card.bounding_box(), target.bounding_box()
            if not cb or not tb:
                print("could not locate a draggable card")
                return 1

            start = (cb["x"] + cb["width"] / 2, cb["y"] + cb["height"] / 2)
            end = (tb["x"] + tb["width"] / 2, tb["y"] + 90)

            page.mouse.move(*start)
            shot()
            page.mouse.down()
            # Stepped by hand rather than via drag_and_drop: the intermediate
            # positions are the whole animation.
            steps = 12
            for i in range(1, steps + 1):
                page.mouse.move(start[0] + (end[0] - start[0]) * i / steps,
                                start[1] + (end[1] - start[1]) * i / steps)
                page.wait_for_timeout(30)
                shot()
            page.mouse.up()
            page.wait_for_timeout(400)
            for _ in range(10):         # hold on the result
                shot()

            browser.close()

        images = [Image.open(f).convert("P", palette=Image.ADAPTIVE, colors=128)
                  for f in shots]
        images[0].save(OUT, save_all=True, append_images=images[1:],
                       duration=FRAME_MS, loop=0, optimize=True)
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
        frames_dir.rmdir()


if __name__ == "__main__":
    raise SystemExit(main())
