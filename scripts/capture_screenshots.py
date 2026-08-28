"""Regenerate the README/landing screenshots from the demo database.

The shop window goes stale fastest of anything in the repo: a feature ships,
the screenshot showing the old layout stays. Running this is cheaper than
remembering, so it is a script rather than a note.

    python scripts/capture_screenshots.py    # reseeds, serves, shoots

It reseeds first on purpose. The seeded queue ages in real time — an hour
after seeding every waiting card has slipped past its target and the board is
uniformly red, which is honest behaviour and a useless photograph.

Every frame comes from scripts/seed_demo_data.py, which is generated from word
lists — no real ticket, name or company ever passes through here.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "demo_screenshots"
PORT = 8099
BASE = f"http://127.0.0.1:{PORT}"

# (filename, path, theme, viewport height). The board comes first because it is
# the frame that goes at the top of the README.
SHOTS = [
    ("01_board.png",              "/board",           "dark",  900),
    ("02_board_light.png",        "/board",           "light", 900),
    ("03_ticket_detail.png",      "/tickets/12",      "dark",  900),
    ("04_tickets_list.png",       "/tickets",         "dark", 1150),
    ("05_overview_kpi.png",       "/overview",        "dark", 1250),
    ("06_overview_light.png",     "/overview",        "light", 1250),
    ("07_ai_analytics.png",       "/ai",              "dark", 1050),
    ("08_templates_analytics.png", "/templates",      "dark", 1050),
    ("09_users.png",              "/users",           "dark", 1050),
    ("10_reports.png",            "/reports",         "dark", 1150),
    ("11_settings.png",           "/manage/settings", "dark", 1250),
    ("12_login.png",              "/login",           "dark",  760),
]

# The panel's real size on a desktop, from the loader's own stylesheet. Shot
# at the size it is actually used at rather than a convenient one — a widget
# photographed at 1440px wide is a photograph of something nobody has.
WIDGET_VIEWPORT = {"width": 400, "height": 680}

#: One of the visitors scripts/seed_demo_data.py creates. Their session token
#: is the string their address was hashed from, so the panel can be handed one
#: and will find the conversation already waiting.
DEMO_VISITOR = "demo-session-Marta Kern"


def _enable_widget(db) -> None:
    """Turn the widget on in the demo database.

    It is off until someone chooses it, which is right for a real instance and
    unhelpful for a camera: the endpoints answer 404 and the panel is a blank
    page. Written straight to the settings table because the alternative is
    booting the whole application twice.
    """
    import sqlite3

    con = sqlite3.connect(db)
    try:
        con.execute(
            "INSERT INTO app_settings (key, value, updated_at) "
            "VALUES ('widget_enabled', '1', datetime('now')) "
            "ON CONFLICT(key) DO UPDATE SET value = '1'"
        )
        con.commit()
    finally:
        con.close()


def _shoot_widget(browser, base: str, out) -> list[str]:
    """The three screens a visitor sees, in the order they meet them."""
    page = browser.new_page(viewport=WIDGET_VIEWPORT, device_scale_factor=2)
    written: list[str] = []
    try:
        # The token has to be on the origin before the panel loads, or it
        # starts as a first-time visitor and there is nothing to photograph.
        page.goto(f"{base}/widget/frame")
        page.evaluate(
            "t => localStorage.setItem('spooldesk.session', t)", DEMO_VISITOR
        )

        for name, view in (
            ("13_widget_home.png", "home"),
            ("14_widget_messages.png", "list"),
            ("15_widget_chat.png", "chat"),
        ):
            page.goto(f"{base}/widget/frame")
            page.wait_for_load_state("networkidle")
            # The panel decides its own opening view from whether there is a
            # session, so each frame is reached by clicking the way a visitor
            # would rather than by forcing state it would never be in.
            if view == "home":
                page.evaluate("document.getElementById('tabHome').click()")
            elif view == "list":
                page.evaluate("document.getElementById('tabList').click()")
            else:
                page.evaluate("document.getElementById('tabList').click()")
                page.wait_for_timeout(400)
                page.evaluate("document.querySelector('#convos .convo').click()")
            # Long enough for the conversation list, and for an attachment to
            # come back through the endpoint that checks the session and be
            # turned into a blob.
            page.wait_for_timeout(1200)
            page.screenshot(path=str(out / name))
            written.append(name)
            print(f"  {name:<28} widget  ({view})")
    finally:
        page.close()
    return written


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
    from playwright.sync_api import sync_playwright

    print("seeding…")
    seeded = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "seed_demo_data.py")],
        cwd=ROOT, capture_output=True, text=True,
    )
    if seeded.returncode != 0:
        print(seeded.stderr[-1500:])
        return 1

    db = ROOT / "demo_support.db"
    if not db.exists():
        print("the seed did not produce demo_support.db")
        return 1
    _enable_widget(db)

    env = {
        **os.environ,
        "TESTING": "1",
        "DEMO_MODE": "1",
        "DATABASE_URL": f"sqlite+aiosqlite:///{db.as_posix()}",
        "BOT_TOKEN": "1:screenshot",
        "ADMIN_CHAT_ID": "-100",
        "ACCESS_PASSWORD": "admin",
        "DASH_SECRET_KEY": "screenshots",
    }
    server = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "src.dashboard.app:app",
         "--host", "127.0.0.1", "--port", str(PORT), "--log-level", "warning"],
        cwd=ROOT, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        if not _wait_for_port(PORT):
            print("the dashboard did not come up")
            return 1

        OUT.mkdir(exist_ok=True)
        with sync_playwright() as p:
            browser = p.chromium.launch()
            for name, path, theme, height in SHOTS:
                page = browser.new_page(
                    viewport={"width": 1440, "height": height},
                    device_scale_factor=2,   # readable when GitHub scales it down
                )
                # The theme is read from localStorage before first paint, so it
                # has to be set on the origin before the page is ever loaded.
                page.goto(f"{BASE}/login")
                page.evaluate(f"localStorage.setItem('dash-theme', '{theme}')")

                if path != "/login":
                    page.fill('input[name="email"]', "admin@local")
                    page.fill('input[name="password"]', "admin")
                    page.click('button[type="submit"]')
                    page.wait_for_load_state("networkidle")
                    page.goto(f"{BASE}{path}")
                else:
                    page.reload()

                page.wait_for_load_state("networkidle")
                page.wait_for_timeout(700)   # charts and the widget pass
                page.screenshot(path=str(OUT / name))
                page.close()
                print(f"  {name:<28} {path}  ({theme})")
            widget = _shoot_widget(browser, BASE, OUT)
            browser.close()
        print(f"\nwrote {len(SHOTS) + len(widget)} files to {OUT.relative_to(ROOT)}")
        return 0
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()


if __name__ == "__main__":
    raise SystemExit(main())
