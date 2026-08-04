"""Working-hours schedule logic: boundaries, weekdays, overnight, timezone."""

from datetime import datetime, timezone

from src.services import work_hours_service as wh


def _cfg(**overrides) -> dict:
    base = {
        "enabled": True,
        "days": [0, 1, 2, 3, 4],   # Mon–Fri
        "start": "09:00",
        "end": "18:00",
        "tz": "UTC",               # UTC in tests → no local-tz surprises
        "message": "нерабочее время",
    }
    base.update(overrides)
    return base


def _utc(y, m, d, hh, mm=0) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=timezone.utc)


def test_inside_and_outside_hours():
    # 2026-07-08 is a Wednesday
    assert wh.is_working_now(_cfg(), _utc(2026, 7, 8, 12))
    assert not wh.is_working_now(_cfg(), _utc(2026, 7, 8, 20))
    assert not wh.is_working_now(_cfg(), _utc(2026, 7, 8, 6))


def test_boundaries_start_inclusive_end_exclusive():
    assert wh.is_working_now(_cfg(), _utc(2026, 7, 8, 9, 0))
    assert not wh.is_working_now(_cfg(), _utc(2026, 7, 8, 18, 0))
    assert wh.is_working_now(_cfg(), _utc(2026, 7, 8, 17, 59))


def test_weekend_excluded():
    # 2026-07-11 Sat, 2026-07-12 Sun
    assert not wh.is_working_now(_cfg(), _utc(2026, 7, 11, 12))
    assert not wh.is_working_now(_cfg(), _utc(2026, 7, 12, 12))
    # ... unless Saturday is a working day
    assert wh.is_working_now(_cfg(days=[5]), _utc(2026, 7, 11, 12))


def test_timezone_shift():
    # Kyiv is UTC+3 in July. 07:00 UTC = 10:00 Kyiv → working;
    # 16:00 UTC = 19:00 Kyiv → off hours.
    cfg = _cfg(tz="Europe/Kyiv")
    assert wh.is_working_now(cfg, _utc(2026, 7, 8, 7))
    assert not wh.is_working_now(cfg, _utc(2026, 7, 8, 16))


def test_overnight_window():
    # Night shift 22:00–06:00, working days Mon–Fri.
    cfg = _cfg(start="22:00", end="06:00")
    # Wednesday 23:00 → inside (evening part of Wednesday's window)
    assert wh.is_working_now(cfg, _utc(2026, 7, 8, 23))
    # Thursday 05:00 → inside (spill-over of Wednesday's window)
    assert wh.is_working_now(cfg, _utc(2026, 7, 9, 5))
    # Thursday 12:00 → outside
    assert not wh.is_working_now(cfg, _utc(2026, 7, 9, 12))
    # Saturday 05:00 → inside (spill-over of FRIDAY's window)
    assert wh.is_working_now(cfg, _utc(2026, 7, 11, 5))
    # Sunday 05:00 → outside (Saturday isn't a working day)
    assert not wh.is_working_now(cfg, _utc(2026, 7, 12, 5))


def test_disabled_means_always_working():
    assert wh.is_working_now(_cfg(enabled=False), _utc(2026, 7, 12, 3))


def test_bad_timezone_falls_back():
    cfg = _cfg(tz="Mars/Olympus_Mons")
    # Must not raise; falls back to the default tz
    assert isinstance(wh.is_working_now(cfg, _utc(2026, 7, 8, 12)), bool)


async def test_config_roundtrip_and_note(initialized_db):
    await wh.set_config(enabled=True, days=[0, 6], start="10:00", end="19:30",
                        tz="Europe/Kyiv", message="  Мы спим. Ответим утром!  ")
    cfg = await wh.get_config()
    assert cfg["enabled"] is True
    assert cfg["days"] == [0, 6]
    assert cfg["start"] == "10:00" and cfg["end"] == "19:30"
    assert cfg["tz"] == "Europe/Kyiv"
    assert cfg["message"] == "Мы спим. Ответим утром!"

    # Invalid tz is rejected at save time
    await wh.set_config(enabled=True, days=[0], start="09:00", end="18:00",
                        tz="Not/AZone", message="x")
    assert (await wh.get_config())["tz"] == wh.DEFAULT_TZ

    # Feature off → note is None regardless of time
    await wh.set_config(enabled=False, days=[0], start="09:00", end="18:00",
                        tz="UTC", message="closed")
    assert await wh.off_hours_note() is None


# ---------- the SLA clock ----------

_MON_FRI_9_18 = {"enabled": True, "days": [0, 1, 2, 3, 4],
                 "start": "09:00", "end": "18:00", "tz": "Europe/Kyiv"}


def _kyiv(y, m, d, hh, mm=0):
    from zoneinfo import ZoneInfo
    return datetime(y, m, d, hh, mm, tzinfo=ZoneInfo("Europe/Kyiv"))


def test_plain_stretch_inside_one_working_day():
    got = wh.working_seconds_between(
        _MON_FRI_9_18, _kyiv(2026, 8, 3, 10), _kyiv(2026, 8, 3, 12, 30))
    assert got == int(2.5 * 3600)


def test_the_night_does_not_count_against_the_team():
    """The whole reason this exists: 18:05 Monday is not late by 09:00 Tuesday."""
    overnight = wh.working_seconds_between(
        _MON_FRI_9_18, _kyiv(2026, 8, 3, 18, 5), _kyiv(2026, 8, 4, 9, 0))
    assert overnight == 0

    # And ten minutes into Tuesday is ten minutes, not fifteen hours.
    assert wh.working_seconds_between(
        _MON_FRI_9_18, _kyiv(2026, 8, 3, 18, 5), _kyiv(2026, 8, 4, 9, 10)) == 600


def test_a_weekend_is_skipped_whole():
    # Friday 17:00 -> Monday 10:00 = 1h Friday + 1h Monday
    got = wh.working_seconds_between(
        _MON_FRI_9_18, _kyiv(2026, 8, 7, 17), _kyiv(2026, 8, 10, 10))
    assert got == 2 * 3600


def test_time_before_opening_is_not_credited():
    """A message at 07:00 starts its clock at 09:00, not at 07:00."""
    got = wh.working_seconds_between(
        _MON_FRI_9_18, _kyiv(2026, 8, 3, 7), _kyiv(2026, 8, 3, 10))
    assert got == 3600


def test_an_overnight_schedule_spans_midnight():
    night = {"enabled": True, "days": [0, 1, 2, 3, 4],
             "start": "22:00", "end": "06:00", "tz": "Europe/Kyiv"}
    # Monday 23:00 -> Tuesday 01:00 is two hours of one Monday-night shift
    assert wh.working_seconds_between(night, _kyiv(2026, 8, 3, 23),
                                      _kyiv(2026, 8, 4, 1)) == 2 * 3600
    # Daytime Tuesday is outside it
    assert wh.working_seconds_between(night, _kyiv(2026, 8, 4, 9),
                                      _kyiv(2026, 8, 4, 17)) == 0


def test_disabled_hours_mean_the_clock_never_stops():
    """No schedule is a claim of round-the-clock cover; hold them to it."""
    off = {**_MON_FRI_9_18, "enabled": False}
    got = wh.working_seconds_between(off, _kyiv(2026, 8, 8, 3), _kyiv(2026, 8, 8, 9))
    assert got == 6 * 3600


def test_a_backwards_interval_is_zero_not_negative():
    assert wh.working_seconds_between(
        _MON_FRI_9_18, _kyiv(2026, 8, 3, 12), _kyiv(2026, 8, 3, 10)) == 0


def test_a_long_gap_stays_bounded_and_correct():
    """Cards can be old; the walk must not blow up or silently truncate."""
    got = wh.working_seconds_between(
        _MON_FRI_9_18, _kyiv(2026, 6, 1, 9), _kyiv(2026, 8, 3, 9))
    # 1 Jun - 3 Aug 2026: whole weeks of Mon-Fri 9h days, no partial ends
    import datetime as _dt
    days = sum(1 for i in range((_dt.date(2026, 8, 3) - _dt.date(2026, 6, 1)).days)
               if (_dt.date(2026, 6, 1) + _dt.timedelta(days=i)).weekday() < 5)
    assert got == days * 9 * 3600
