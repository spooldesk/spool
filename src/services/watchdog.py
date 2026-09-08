"""Telling somebody when the instance stops working.

Errors go to `logs/dashboard.log`, on a server, in a file nobody opens. That
is how a mailbox loop can be failing for three days and be discovered by a
customer who did not get an answer — which, for a helpdesk, is the worst
possible discoverer.

The channel is the one that is already there: the Telegram admin group where
tickets land. No new service, no subscription, no second thing to configure,
and it reaches the operator where they already are. An instance without
Telegram gets nothing from this, and is told so once at startup rather than
left wondering.

**Only on a change.** A message every minute while something is broken is a
group everybody mutes, and a muted group is worse than no alerting, because
the next real message goes into it too. So: one message when a check starts
failing, one when it recovers, nothing in between.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from src.services import health

logger = logging.getLogger(__name__)

#: Twice the container's healthcheck interval. This is not a monitor racing
#: to be first — the container already checks every thirty seconds. This is
#: the thing that puts an answer in front of a person.
INTERVAL_SECONDS = 60

#: Consecutive failing rounds before saying anything. A database that blinks
#: during a backup, an IMAP server refusing one connection: real, transient,
#: and not worth waking somebody for. Two rounds is two minutes.
_ROUNDS_BEFORE_ALERTING = 2


async def _announce(text: str) -> None:
    """Into the admin group, or nowhere at all.

    Every failure here is swallowed. The watchdog exists to report a problem;
    it must never become one, and an instance whose Telegram is misconfigured
    should still serve pages.
    """
    try:
        from src.config import load_settings
        from src.dashboard.bot_client import get_bot

        bot = get_bot()
        if bot is None:
            return
        await bot.send_message(
            chat_id=load_settings().admin_chat_id, text=text,
            parse_mode="HTML", disable_notification=False,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("Could not send the health alert: %s", e)


def _broken_message(failing) -> str:
    lines = ["⚠️ <b>Spooldesk has a problem</b>", ""]
    lines += [f"• <b>{c.name}</b> — {c.detail}" for c in failing]
    lines += ["", "<i>Tickets may not be arriving. The log has the detail: "
                  "logs/dashboard.log</i>"]
    return "\n".join(lines)


@dataclass
class _State:
    """What has been said, so only changes get said again.

    Starts reported-healthy: an instance that boots broken sends one message
    after the first rounds, which is exactly what somebody wants to know
    about a deploy that has just gone wrong.
    """

    reported_ok: bool = True
    consecutive_failures: int = 0


def decide(result: health.Health, state: _State) -> str | None:
    """The whole of the watchdog's judgement, with no clock in it.

    Separated from the loop because this is the part that can be wrong in a
    way nobody notices: alerting twice, alerting on a blip, or going quiet
    after a recovery it never announced. A loop with a sleep in it cannot be
    tested for any of that.

    Returns the message to send, or None to stay silent, and updates `state`.
    """
    if result.ok:
        state.consecutive_failures = 0
        if state.reported_ok:
            return None
        state.reported_ok = True
        return "✅ <b>Spooldesk is answering again</b>"

    state.consecutive_failures += 1
    if not state.reported_ok:
        return None  # already said so; saying it every minute is a muted group
    if state.consecutive_failures < _ROUNDS_BEFORE_ALERTING:
        return None  # a blip, not an outage
    state.reported_ok = False
    return _broken_message(result.failing)


async def loop() -> None:
    """Watch until cancelled.

    Never raises: a watchdog that dies on an unexpected error leaves an
    instance that looks supervised and is not, which is worse than one that
    was never watched — nobody goes looking for alerts they know they do not
    have.
    """
    logger.info("Health watchdog running every %ss", INTERVAL_SECONDS)
    state = _State()

    while True:
        try:
            await asyncio.sleep(INTERVAL_SECONDS)
            result = await health.check()
            if not result.ok:
                logger.warning("Health check failing (%s in a row): %s",
                               state.consecutive_failures + 1, result.summary())
            if (message := decide(result, state)) is not None:
                await _announce(message)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.warning("Watchdog round failed, will retry: %s", e)


def should_run() -> bool:
    """Whether there is anywhere to send an alert.

    Without Telegram the loop would run forever, find problems and have
    nobody to tell. Saying so at startup is more useful than a silent task.
    """
    from src.config import load_settings
    return bool(load_settings().has_telegram)
