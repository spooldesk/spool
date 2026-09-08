"""Whether this instance is actually working, rather than merely running.

`/healthz` used to answer `{"ok": true}` without looking at anything. A
process that has lost its database, or whose mailbox has been refusing the
password for three days, answered exactly the same as a healthy one — and
the container healthcheck, the thing whose whole job is to notice, asks that
endpoint.

That is tolerable for a self-hosted install with an administrator beside it.
It is not tolerable in a product sold as "we run it for you", where the
promise is precisely that somebody notices before the customer does.

Two consumers, one set of checks, so they cannot drift:

  * `/healthz`, hit every thirty seconds by the container and by whatever
    external monitor an operator points at it;
  * the watchdog in `watchdog.py`, which tells the admin group when an
    answer changes.

**What this cannot do.** If the machine is off, the network is gone, or the
process is dead, nothing here runs and nothing is sent. Self-reporting always
has that hole and closing it needs something outside the box — an external
uptime check on this same endpoint. That is five minutes of somebody else's
free tier, and it is the right second layer rather than a bigger first one.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)

#: A mailbox pass is due every POLL_SECONDS. Missing one is nothing — a slow
#: IMAP server, a retried connection. Missing this many in a row is a loop
#: that is not coming back on its own.
_MAIL_MISSED_PASSES = 5

#: How long after start a mailbox is allowed to have produced nothing yet.
#: The loop sleeps before its first pass, so without this every restart
#: reports a broken mailbox for the first minute.
_MAIL_GRACE_SECONDS = 180.0


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    ok: bool
    #: Shown to an operator, so it says what to do rather than what happened.
    detail: str = ""


@dataclass(frozen=True, slots=True)
class Health:
    checks: tuple[Check, ...]

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks)

    @property
    def failing(self) -> tuple[Check, ...]:
        return tuple(c for c in self.checks if not c.ok)

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "checks": {c.name: {"ok": c.ok, "detail": c.detail} for c in self.checks},
        }

    def summary(self) -> str:
        """One line naming what is wrong, for a message somebody reads on a
        phone."""
        if self.ok:
            return "everything is answering"
        return "; ".join(f"{c.name}: {c.detail}" for c in self.failing)


async def _database() -> Check:
    """The one check that decides everything else.

    A dashboard whose database is gone serves pages, accepts replies and
    loses them. There is no degraded mode worth having here.
    """
    from sqlalchemy import text

    from src.database.db import db_session

    try:
        async with db_session() as session:
            await session.execute(text("SELECT 1"))
        return Check("database", True)
    except Exception as e:  # noqa: BLE001 — any failure is the same failure
        return Check("database", False, f"unreachable ({type(e).__name__})")


def _mailbox() -> Check | None:
    """None when this instance has no mailbox, which is a supported shape.

    Reports on the loop rather than on the mail server: what matters is
    whether passes are still completing, not whether one of them was slow.
    """
    from src.channels.email import is_configured
    from src.services import email_ingest

    if not is_configured():
        return None

    if email_ingest.started_at is None:
        # Configured, and the loop never started. Almost always an exception
        # during startup that the log has and nobody read.
        return Check("mailbox", False, "polling never started")

    now = time.monotonic()
    if email_ingest.last_success is None:
        if now - email_ingest.started_at < _MAIL_GRACE_SECONDS:
            return Check("mailbox", True, "starting")
        return Check("mailbox", False,
                     f"no successful pass since start — {email_ingest.last_error or 'no detail'}")

    silent_for = now - email_ingest.last_success
    if silent_for > _MAIL_MISSED_PASSES * email_ingest.POLL_SECONDS:
        return Check("mailbox", False,
                     f"no successful pass for {int(silent_for // 60)} min — "
                     f"{email_ingest.last_error or 'no detail'}")
    return Check("mailbox", True)


async def check() -> Health:
    """Every check this instance can answer, cheaply.

    Cheap matters: this runs every thirty seconds forever. One trivial query
    and some arithmetic over values another loop already recorded.
    """
    checks = [await _database()]
    if (mail := _mailbox()) is not None:
        checks.append(mail)
    return Health(tuple(checks))
