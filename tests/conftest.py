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


# ---------------------------------------------------------------------------
# Channels
#
# Tests used to hand a fake aiogram Bot to whichever module was about to send.
# That only worked while Telegram was the one way out, and it tested the Bot
# API's shape rather than ours. Since the channel layer landed, the seam a test
# should stand on is the channel: install a recorder in the registry and assert
# on what an *operator* caused, not on which Bot API method got called.


class RecordingChannel:
    """A channel that keeps what it was asked to send instead of sending it.

    `fail_on` makes the Nth send fail, which is how the partial-delivery paths
    get tested — the interesting case is three of five arriving, and there is
    no other way to reach it.
    """

    name = "telegram"

    def __init__(self, *, fail_on: int | None = None, caps=None, name: str | None = None):
        from src.channels.base import Capabilities

        if name:
            self.name = name
        self.caps = caps or Capabilities(
            unsend=True, files=True, buttons=True, typing=True, push=True
        )
        self.fail_on = fail_on
        self.texts: list[tuple[str, str]] = []
        self.files: list[dict] = []
        self.offered: list[list] = []
        # Bytes a test wants `fetch_file` to hand back, keyed by ref.
        self.fetched: dict[str, bytes] = {}
        self.unsent: list[tuple[str, str]] = []
        self.typing_for: list[str] = []
        self._n = 0

    @property
    def sent(self) -> int:
        return len(self.texts) + len(self.files)

    def _next_ref(self) -> str:
        self._n += 1
        return str(9000 + self._n)

    async def send_text(self, address, text, *, options=None):
        from src.channels.base import Delivery

        if self.fail_on is not None and self.sent + 1 == self.fail_on:
            return Delivery.failed("e_send_failed", "channel said no")
        self.texts.append((address, text))
        # Kept separately so a test can assert an unrelated message carried no
        # choices — offering a rating twice is worse than not offering one.
        self.offered.append(list(options or []))
        return Delivery.sent(self._next_ref())

    async def send_file(self, address, *, filename, content, caption=None):
        from src.channels.base import Delivery

        if self.fail_on is not None and self.sent + 1 == self.fail_on:
            return Delivery.failed("e_send_failed", "channel said no")
        self.files.append({"address": address, "filename": filename,
                           "content": content, "caption": caption})
        # A real channel hands back two references: one for the message, one
        # for the file. The fake does the same, or the tests would agree with
        # a shape the adapters do not have.
        return Delivery(ok=True, ref=self._next_ref(),
                        file_ref=f"fid-{len(self.files)}")

    async def fetch_file(self, ref):
        # Whatever was handed over on the way in comes back out. Enough to
        # prove the attachment route asks the channel instead of Telegram.
        for f in self.files:
            if f["filename"] == ref:
                return f["content"]
        return self.fetched.get(ref)

    async def unsend(self, address, ref):
        self.unsent.append((address, ref))
        return True

    async def typing(self, address):
        self.typing_for.append(address)


@pytest.fixture()
def channel():
    """Install a recording channel as Telegram, and take it out again.

    Torn down explicitly: the registry is process-wide, and a fake left behind
    would silently absorb the sends of every test that ran afterwards.
    """
    import src.channels as channels

    recorder = RecordingChannel()
    channels.register(recorder)
    yield recorder
    channels.reset()
