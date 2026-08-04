"""Test bootstrap: environment must be set BEFORE any `src.*` import.

src/config loads .env and src/database/db creates the engine at import time,
so this file configures a throwaway SQLite database and dummy secrets first.
Every test in this suite runs against that temp DB (fresh per test session).
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

# --- environment first (before importing src anywhere) ---
_TMPDIR = Path(tempfile.mkdtemp(prefix="supbot_tests_"))
os.environ["TESTING"] = "1"  # db.py switches to NullPool
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{(_TMPDIR / 'test.db').as_posix()}"
os.environ["BOT_TOKEN"] = "1000000:TEST-not-a-real-token"
os.environ["ADMIN_CHAT_ID"] = "-1001000000000"
os.environ["ACCESS_PASSWORD"] = "test-password-123"
os.environ["DASH_SECRET_KEY"] = "test-secret-key-for-sessions"
os.environ.pop("GEMINI_API_KEY", None)  # AI stays off in tests
os.environ.pop("DEMO_MODE", None)

import itertools  # noqa: E402

import pytest  # noqa: E402


@pytest.fixture()
async def initialized_db():
    """Ensure schema + seeds exist (idempotent — safe in any test order)."""
    from src.database.db import init_db
    await init_db()
    return None


# Every test file shares one database, so hand-picked tg_id ranges collide the
# moment two files happen to choose the same block — it has already happened
# twice. Start well above every range currently written by hand and hand out
# ids from here instead of inventing new blocks.
_TG_IDS = itertools.count(1_000_000)


def next_tg_id() -> int:
    """A telegram id no other test is using."""
    return next(_TG_IDS)
