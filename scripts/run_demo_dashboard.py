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

# The public demo is shared, and every instance setting is editable from inside
# it. Pin the ones the demo depends on at every start, so one visitor cannot
# leave the demo broken (or in a language they chose) for everyone after them.
import sqlite3  # noqa: E402

# The widget is pinned on for the same reason and one more: the landing page
# embeds it, so an instance that came up with it off would show a bubble that
# does nothing. Origins stay empty because the page it is embedded on is a
# GitHub Pages domain that may change, and the rate limits are what actually
# protect the endpoints anyway.
_DEMO_SETTINGS = {
    "dash_language": "en",
    "widget_enabled": "1",
    "widget_origins": "",
    # Said out loud rather than buried in a privacy note nobody opens. The
    # demo is one shared instance; whatever a visitor types is on a dashboard
    # that anyone can open.
    "widget_greeting": (
        "This is a live demo \u2014 write anything and it becomes a real ticket "
        "you can open in the dashboard. The instance is shared, so please don't "
        "type anything private."
    ),
}

try:
    _con = sqlite3.connect(DEMO_DB_PATH)
    for _key, _value in _DEMO_SETTINGS.items():
        _con.execute(
            "INSERT INTO app_settings (key, value, updated_at) VALUES (?,?,datetime('now')) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (_key, _value),
        )
    _con.commit()
    _con.close()
except Exception as _e:  # table may not exist on a brand-new DB — startup seeds it
    print(f"demo: could not pin instance settings ({_e})")

import uvicorn  # noqa: E402

if __name__ == "__main__":
    # Hosting platforms (Render etc.) inject PORT and need 0.0.0.0;
    # local runs stay on localhost:8010.
    port = int(os.getenv("PORT", "8010"))
    host = os.getenv("HOST", "0.0.0.0" if "PORT" in os.environ else "127.0.0.1")
    uvicorn.run("src.dashboard.app:app", host=host, port=port)
