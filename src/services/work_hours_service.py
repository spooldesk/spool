"""Working hours — off-hours auto-reply for new tickets.

Config lives in app_settings (runtime-editable from the dashboard):
    - enabled flag
    - working weekdays (0=Mon … 6=Sun)
    - one daily interval "HH:MM"–"HH:MM" (overnight intervals supported:
      start > end means the window crosses midnight, e.g. 22:00–06:00)
    - IANA timezone the schedule is defined in
    - the auto-reply text an admin can fully customize

The bot calls off_hours_note() right after a ticket is created; None means
"working now (or feature off) — say nothing".
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from src.services import settings_service

logger = logging.getLogger(__name__)

KEY_ENABLED = "work_hours_enabled"    # "1"/"0"
KEY_SCHEDULE = "work_hours_schedule"  # JSON {"days":[0..6],"start":"09:00","end":"18:00"}
KEY_TZ = "work_hours_tz"              # IANA tz name
KEY_MESSAGE = "work_hours_message"    # off-hours auto-reply text
KEY_SLA_MINUTES = "sla_response_minutes"  # working minutes before a card warns

# One number, two levels: amber at the target, red at twice it. Anything more
# adjustable is a knob nobody turns, and a board that warns about everything
# warns about nothing.
DEFAULT_SLA_MINUTES = 60

DEFAULT_DAYS = [0, 1, 2, 3, 4]        # Mon–Fri
DEFAULT_START = "09:00"
DEFAULT_END = "18:00"
DEFAULT_TZ = "Europe/Kyiv"
DEFAULT_MESSAGE = (
    "🕐 Support is outside working hours right now.\n\n"
    "Your request has been received and is in the queue — an operator will "
    "reply during working hours."
)

def day_labels() -> list[str]:
    """Weekday chips for the settings UI, in the dashboard's language."""
    from src.dashboard.i18n import t as dash_t
    return dash_t("weekdays").split(",")


def _parse_hhmm(raw: str, fallback: str) -> time:
    try:
        h, m = (raw or fallback).strip().split(":", 1)
        return time(int(h), int(m))
    except (ValueError, AttributeError):
        h, m = fallback.split(":", 1)
        return time(int(h), int(m))


async def get_config() -> dict:
    """Full config with safe fallbacks for any malformed stored value."""
    enabled = (await settings_service.get_value(KEY_ENABLED, "0")) == "1"
    raw = await settings_service.get_value(KEY_SCHEDULE, "")
    days, start, end = DEFAULT_DAYS, DEFAULT_START, DEFAULT_END
    try:
        data = json.loads(raw) if raw else {}
        if isinstance(data.get("days"), list):
            days = sorted({int(d) for d in data["days"] if 0 <= int(d) <= 6})
        start = str(data.get("start") or DEFAULT_START)
        end = str(data.get("end") or DEFAULT_END)
    except (json.JSONDecodeError, ValueError, TypeError):
        logger.warning("work_hours: malformed schedule JSON, using defaults")
    tz = (await settings_service.get_value(KEY_TZ, DEFAULT_TZ)).strip() or DEFAULT_TZ
    message = await settings_service.get_value(KEY_MESSAGE, DEFAULT_MESSAGE) or DEFAULT_MESSAGE
    return {
        "enabled": enabled,
        "days": days,
        "start": start,
        "end": end,
        "tz": tz,
        "message": message,
    }


async def set_config(*, enabled: bool, days: list[int], start: str, end: str,
                     tz: str, message: str, updated_by: int = 0) -> None:
    # Validate tz early so a typo can't break the bot at runtime.
    tz = (tz or "").strip() or DEFAULT_TZ
    try:
        ZoneInfo(tz)
    except Exception:
        logger.warning(f"work_hours: unknown timezone {tz!r}, keeping {DEFAULT_TZ}")
        tz = DEFAULT_TZ
    clean_days = sorted({int(d) for d in days if 0 <= int(d) <= 6})
    # Normalize times through the parser (garbage → defaults)
    start_t = _parse_hhmm(start, DEFAULT_START)
    end_t = _parse_hhmm(end, DEFAULT_END)
    await settings_service.set_value(KEY_ENABLED, "1" if enabled else "0", updated_by=updated_by)
    await settings_service.set_value(KEY_SCHEDULE, json.dumps({
        "days": clean_days,
        "start": start_t.strftime("%H:%M"),
        "end": end_t.strftime("%H:%M"),
    }), updated_by=updated_by)
    await settings_service.set_value(KEY_TZ, tz, updated_by=updated_by)
    await settings_service.set_value(KEY_MESSAGE, (message or "").strip() or DEFAULT_MESSAGE,
                                     updated_by=updated_by)


def is_working_now(config: dict, now_utc: datetime | None = None) -> bool:
    """Pure schedule check (config from get_config; now injectable for tests).

    Overnight windows (start > end, e.g. 22:00–06:00) belong to the weekday
    the window STARTS on: Friday 22:00–06:00 covers Fri 22:00 → Sat 06:00.
    """
    if not config["enabled"]:
        return True  # feature off → always "working", no auto-replies

    try:
        tz = ZoneInfo(config["tz"])
    except Exception:
        tz = ZoneInfo(DEFAULT_TZ)

    now = (now_utc or datetime.now(timezone.utc))
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    local = now.astimezone(tz)

    start = _parse_hhmm(config["start"], DEFAULT_START)
    end = _parse_hhmm(config["end"], DEFAULT_END)
    days = set(config["days"])
    t = local.time()

    if start <= end:
        return local.weekday() in days and start <= t < end
    # Overnight window: today's evening part OR yesterday's spill-over morning
    if local.weekday() in days and t >= start:
        return True
    prev_weekday = (local.weekday() - 1) % 7
    return prev_weekday in days and t < end


async def get_sla_minutes() -> int:
    """Working minutes a customer may wait before their card starts warning."""
    raw = await settings_service.get_value(KEY_SLA_MINUTES, str(DEFAULT_SLA_MINUTES))
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_SLA_MINUTES
    return value if value > 0 else DEFAULT_SLA_MINUTES


def sla_state(waiting_seconds: int | None, sla_minutes: int) -> str | None:
    """Warn at 80% of the response target; breach at the deadline."""
    if waiting_seconds is None:
        return None
    target = sla_minutes * 60
    if waiting_seconds >= target:
        return "breach"
    if waiting_seconds >= target * .8:
        return "warn"
    return None


def working_seconds_between(config: dict, start_utc: datetime, end_utc: datetime) -> int:
    """Working seconds elapsed between two moments.

    This is what an SLA clock has to measure. Counting wall-clock instead
    would mark a ticket that arrived at 18:05 as fifteen hours late by nine
    the next morning, and a board where everything is red on Monday tells the
    operator nothing.

    With the feature off the team is claiming round-the-clock cover, so plain
    elapsed time is the honest answer.
    """
    if end_utc <= start_utc:
        return 0
    if not config["enabled"]:
        return int((end_utc - start_utc).total_seconds())

    try:
        tz = ZoneInfo(config["tz"])
    except Exception:
        tz = ZoneInfo(DEFAULT_TZ)

    for dt in (start_utc, end_utc):
        if dt.tzinfo is None:
            raise ValueError("working_seconds_between needs aware datetimes")

    begin = start_utc.astimezone(tz)
    finish = end_utc.astimezone(tz)
    start_t = _parse_hhmm(config["start"], DEFAULT_START)
    end_t = _parse_hhmm(config["end"], DEFAULT_END)
    days = set(config["days"])
    if not days:
        return 0

    total = 0.0
    # Walk local days and sum each working window's overlap with the interval.
    # One day back so an overnight window that opened yesterday is counted.
    day = (begin - timedelta(days=1)).date()
    last = finish.date()
    guard = 0
    while day <= last and guard < 800:
        guard += 1
        if day.weekday() in days:
            opens = datetime.combine(day, start_t, tzinfo=tz)
            # start > end means the window crosses midnight into the next day
            closes = datetime.combine(
                day if start_t <= end_t else day + timedelta(days=1), end_t, tzinfo=tz)
            overlap = min(finish, closes) - max(begin, opens)
            if overlap.total_seconds() > 0:
                total += overlap.total_seconds()
        day += timedelta(days=1)
    return int(total)


async def off_hours_note() -> str | None:
    """The auto-reply text if we're outside working hours, else None."""
    config = await get_config()
    if not config["enabled"]:
        return None
    if is_working_now(config):
        return None
    return config["message"]
