"""Whether the instance can tell anybody it has stopped working.

`/healthz` answered `{"ok": true}` unconditionally, and the container
healthcheck asks that endpoint — so the one mechanism whose job is to notice
was asking a question that could not fail. A process with no database
answered exactly the same as a healthy one.

That is survivable when an administrator is beside the server. It is not
survivable in a product sold as "we run it for you", where the promise is
that somebody notices before the customer does.

So the tests that matter here are the ones about *failing*: that a broken
database produces a non-200, that a stalled mailbox loop is distinguishable
from a quiet one, and that the watchdog says something once rather than
either never or every minute.
"""

import time

import pytest
from fastapi.testclient import TestClient

import src.database.db as db_module
from src.dashboard.app import app
from src.services import email_ingest, health, watchdog


@pytest.fixture(autouse=True)
async def _clean(initialized_db):
    before = (email_ingest.started_at, email_ingest.last_success,
              email_ingest.last_error)
    yield
    (email_ingest.started_at, email_ingest.last_success,
     email_ingest.last_error) = before


@pytest.fixture()
def broken_database(monkeypatch):
    """The database gone, the way an outage takes it: anything asking for a
    connection raises."""
    def boom(*args, **kwargs):
        raise OSError("disk I/O error")
    monkeypatch.setattr(db_module, "db_session", boom)


def _health(ok: bool, name: str = "database", detail: str = "unreachable"):
    return health.Health((health.Check(name, ok, "" if ok else detail),))


# ---------------------------------------------------------------- /healthz


async def test_a_working_instance_answers_200():
    with TestClient(app) as c:
        r = c.get("/healthz")
    assert r.status_code == 200
    assert r.json()["ok"] is True


async def test_a_broken_database_answers_503(broken_database):
    """The whole point. A monitor reads the status code and nothing else, so
    an endpoint that cannot return a bad one is decoration."""
    with TestClient(app) as c:
        r = c.get("/healthz")
    assert r.status_code == 503
    assert r.json()["ok"] is False


async def test_the_body_names_what_is_wrong(broken_database):
    """Whoever is woken by this needs to know which subsystem, without
    logging in first."""
    with TestClient(app) as c:
        body = c.get("/healthz").json()
    assert body["checks"]["database"]["ok"] is False
    assert "unreachable" in body["checks"]["database"]["detail"]


async def test_it_needs_no_login():
    """A healthcheck behind a session cannot be used by the container that
    needs it, nor by an external uptime monitor."""
    with TestClient(app) as c:
        assert c.get("/healthz").status_code in (200, 503)
        assert "location" not in {k.lower() for k in c.get("/healthz").headers}


async def test_it_says_nothing_about_what_is_in_the_subsystems(broken_database):
    """It is public, so it reports that the database is unreachable and
    never a connection string, a path or a query."""
    with TestClient(app) as c:
        body = c.get("/healthz").text
    assert "sqlite" not in body.lower()
    assert "password" not in body.lower()
    assert ".db" not in body


# ---------------------------------------------------------------- mailbox


async def test_an_instance_with_no_mailbox_is_not_asked_about_one():
    """Widget and Telegram alone is a supported shape, and an instance that
    was never configured for mail must not report mail as broken."""
    result = await health.check()
    assert "mailbox" not in {c.name for c in result.checks}


async def test_a_mailbox_that_has_never_polled_is_broken(monkeypatch):
    """Configured, and the loop never started — almost always an exception
    during startup that only the log saw."""
    monkeypatch.setattr("src.channels.email.is_configured", lambda: True)
    monkeypatch.setattr(email_ingest, "started_at", None)

    check = health._mailbox()
    assert check is not None and check.ok is False
    assert "never started" in check.detail


async def test_a_mailbox_is_given_time_to_produce_its_first_pass(monkeypatch):
    """The loop sleeps before its first pass. Without a grace period every
    restart reports a broken mailbox for the first minute, which teaches
    everyone to ignore the alert."""
    monkeypatch.setattr("src.channels.email.is_configured", lambda: True)
    monkeypatch.setattr(email_ingest, "started_at", time.monotonic())
    monkeypatch.setattr(email_ingest, "last_success", None)

    assert health._mailbox().ok is True


async def test_a_mailbox_silent_since_start_is_eventually_broken(monkeypatch):
    monkeypatch.setattr("src.channels.email.is_configured", lambda: True)
    monkeypatch.setattr(email_ingest, "started_at",
                        time.monotonic() - health._MAIL_GRACE_SECONDS - 1)
    monkeypatch.setattr(email_ingest, "last_success", None)
    monkeypatch.setattr(email_ingest, "last_error", "AUTHENTICATIONFAILED")

    check = health._mailbox()
    assert check.ok is False
    # The provider's own words. A wrong password and a blocked port are the
    # same symptom and different fixes.
    assert "AUTHENTICATIONFAILED" in check.detail


async def test_one_missed_pass_is_not_an_outage(monkeypatch):
    """A slow IMAP server is normal. Alerting on it is how the alert stops
    meaning anything."""
    monkeypatch.setattr("src.channels.email.is_configured", lambda: True)
    monkeypatch.setattr(email_ingest, "started_at", time.monotonic() - 3600)
    monkeypatch.setattr(email_ingest, "last_success",
                        time.monotonic() - email_ingest.POLL_SECONDS * 2)

    assert health._mailbox().ok is True


async def test_a_loop_that_has_stopped_completing_is_broken(monkeypatch):
    """The failure this exists for: the loop is alive, swallowing every
    error, and has not actually read the mailbox in three days."""
    monkeypatch.setattr("src.channels.email.is_configured", lambda: True)
    monkeypatch.setattr(email_ingest, "started_at", time.monotonic() - 86400)
    monkeypatch.setattr(email_ingest, "last_success", time.monotonic() - 86400)
    monkeypatch.setattr(email_ingest, "last_error", "timed out")

    check = health._mailbox()
    assert check.ok is False
    assert "min" in check.detail


async def test_the_real_loop_records_what_health_reads(monkeypatch):
    """The join between the two halves.

    health.py decides from `last_success` and `last_error`. If poll_loop ever
    stops writing them, every mailbox reads as stalled and the alert fires
    forever — so this drives the actual loop, with only the IMAP conversation
    replaced, and checks the bookkeeping around it.
    """
    import asyncio

    monkeypatch.setattr(email_ingest, "POLL_SECONDS", 0)
    email_ingest.last_success = email_ingest.last_error = None

    calls = {"n": 0}

    async def one_good_then_one_bad():
        calls["n"] += 1
        if calls["n"] == 1:
            return {"fetched": 0, "opened": 0, "skipped": 0}
        raise OSError("AUTHENTICATIONFAILED")

    monkeypatch.setattr(email_ingest, "run_once", one_good_then_one_bad)

    task = asyncio.create_task(email_ingest.poll_loop())
    try:
        while calls["n"] < 1:
            await asyncio.sleep(0)
        assert email_ingest.started_at is not None
        assert email_ingest.last_success is not None
        assert email_ingest.last_error is None

        # ...and a failing pass records why, without clearing the last good
        # one — "worked an hour ago, failing now" is the useful reading.
        good = email_ingest.last_success
        while calls["n"] < 2:
            await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert email_ingest.last_error is not None
        assert "AUTHENTICATIONFAILED" in email_ingest.last_error
        assert email_ingest.last_success == good
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


# --------------------------------------------------------------- watchdog


def test_a_healthy_instance_says_nothing():
    state = watchdog._State()
    assert watchdog.decide(_health(True), state) is None


def test_one_bad_round_is_not_worth_a_message():
    """A database blinking during a backup is real and transient."""
    state = watchdog._State()
    assert watchdog.decide(_health(False), state) is None


def test_a_sustained_failure_produces_exactly_one_message():
    """Once, not every minute. A group that pings every sixty seconds gets
    muted, and the next real message goes into a muted group."""
    state = watchdog._State()
    sent = [watchdog.decide(_health(False), state) for _ in range(10)]
    messages = [m for m in sent if m is not None]

    assert len(messages) == 1
    assert "problem" in messages[0].lower()
    assert "database" in messages[0]


def test_recovery_is_announced_once():
    state = watchdog._State()
    for _ in range(3):
        watchdog.decide(_health(False), state)

    first = watchdog.decide(_health(True), state)
    second = watchdog.decide(_health(True), state)

    assert first is not None and "again" in first
    assert second is None


def test_a_second_outage_after_a_recovery_is_announced_again():
    """The state has to reset, or an instance that breaks twice is only ever
    reported once."""
    state = watchdog._State()
    for _ in range(3):
        watchdog.decide(_health(False), state)
    watchdog.decide(_health(True), state)

    again = [watchdog.decide(_health(False), state) for _ in range(3)]
    assert any(m is not None for m in again)


def test_a_blip_does_not_arm_the_next_one():
    """One bad round, then healthy, then one bad round: neither is an outage
    and the counter must not have carried over."""
    state = watchdog._State()
    watchdog.decide(_health(False), state)
    watchdog.decide(_health(True), state)
    assert watchdog.decide(_health(False), state) is None


def test_the_message_says_which_subsystem_and_where_to_look():
    """Somebody reads this on a phone. It has to name the thing and where to
    look next, without them logging in to find out."""
    state = watchdog._State()
    broken = _health(False, "mailbox", "no successful pass for 42 min")

    assert watchdog.decide(broken, state) is None      # first round: a blip
    message = watchdog.decide(broken, state)           # second: an outage

    assert message is not None
    assert "mailbox" in message
    assert "42 min" in message
    assert "dashboard.log" in message


async def test_alerting_cannot_become_the_outage(monkeypatch):
    """A watchdog that raises while reporting a problem turns one failure
    into two, in a process that was still serving pages."""
    def explode():
        raise RuntimeError("telegram is down too")
    monkeypatch.setattr("src.dashboard.bot_client.get_bot", explode)

    await watchdog._announce("anything")  # must not raise


def test_no_telegram_means_no_watchdog(monkeypatch):
    """It would run forever, find problems, and have nobody to tell."""
    from src import config

    real = config.load_settings()
    monkeypatch.setattr(config, "load_settings",
                        lambda: type(real)(**{**real.__dict__, "bot_token": ""}))
    assert watchdog.should_run() is False
