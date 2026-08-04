"""Run the dashboard against the DEMO database (demo_support.db) for screenshots.

The real support.db is untouched: DATABASE_URL is overridden before src.* imports.
Run:  .venv/Scripts/python.exe scripts/run_demo_dashboard.py
"""

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

DEMO_DB_PATH = PROJECT_ROOT / "demo_support.db"

os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{DEMO_DB_PATH.as_posix()}"
os.environ["DEMO_MODE"] = "1"  # shows the DEMO badge in the dashboard topbar
os.environ.setdefault("BOT_TOKEN", "1234567890:DEMO-not-a-real-token")
os.environ.setdefault("ADMIN_CHAT_ID", "-1001234567890")
os.environ.setdefault("ACCESS_PASSWORD", "admin")

if not DEMO_DB_PATH.exists():
    sys.exit("demo_support.db not found — run scripts/seed_demo_data.py first")

# The public demo is shared: any visitor can change instance settings, and the
# UI language is one of them. Pin it to English on every start so a visitor who
# switches to Russian doesn't leave the demo in Russian for everyone after them.
import sqlite3  # noqa: E402

try:
    _con = sqlite3.connect(DEMO_DB_PATH)
    _con.execute(
        "INSERT INTO app_settings (key, value, updated_at) VALUES ('dash_language','en',datetime('now')) "
        "ON CONFLICT(key) DO UPDATE SET value='en'"
    )
    _con.commit()
    _con.close()
except Exception as _e:  # table may not exist on a brand-new DB — startup seeds it
    print(f"demo: could not pin UI language to en ({_e})")

import uvicorn  # noqa: E402

if __name__ == "__main__":
    # Hosting platforms (Render etc.) inject PORT and need 0.0.0.0;
    # local runs stay on localhost:8010.
    port = int(os.getenv("PORT", "8010"))
    host = os.getenv("HOST", "0.0.0.0" if "PORT" in os.environ else "127.0.0.1")
    uvicorn.run("src.dashboard.app:app", host=host, port=port)
