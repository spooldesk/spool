"""How one operator likes the dashboard.

Distinct from `settings_service`, which configures the instance. The line
between them is who is entitled to the answer: how long before an SLA breach
is a decision the team makes once, and whether the interface is in Russian is
a decision each person makes for themselves. Two people sharing an
installation can disagree about the second without either being wrong.

Everything here has a default that is the current behaviour, so an operator
who never opens the page notices nothing.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select

from src.database.db import db_session
from src.database.models import AdminUser

logger = logging.getLogger(__name__)

#: Empty means "follow the instance", which is not the same as English —
#: an instance set to Russian should stay Russian for someone who never
#: expressed a preference.
FOLLOW_INSTANCE = ""

THEMES = ("system", "dark", "light")
DENSITIES = ("comfortable", "compact")
#: Where the logo takes you. Restricted to pages every role can reach, so a
#: preference cannot land an agent on a 403 every morning.
LANDINGS = ("/tickets", "/board", "/analytics")

MAX_SIGNATURE = 300

DEFAULTS: dict[str, Any] = {
    "language": FOLLOW_INSTANCE,
    "theme": "system",
    "density": "comfortable",
    "landing": "/tickets",
    # After a reply, hand over the next waiting ticket instead of staying put.
    # Off by default: it moves the page out from under you, which is welcome
    # when you asked for it and alarming when you did not.
    "queue_advance": False,
    "notify_new": False,
    "hotkeys": True,
    # Appended to outgoing replies. Empty by default, because a signature
    # nobody chose is a change to what customers receive.
    "signature": "",
}


def _clean(raw: dict | None) -> dict[str, Any]:
    """Defaults, overlaid with whatever of the stored values is still valid.

    Written to survive its own history: a key that was removed, or a value
    that stopped being allowed, falls back to the default rather than
    reaching a template that does not expect it.
    """
    from src.dashboard.i18n import SUPPORTED_DASH_LANGUAGES

    values = dict(DEFAULTS)
    for key, value in (raw or {}).items():
        if key not in DEFAULTS:
            continue
        if key == "language":
            if value == FOLLOW_INSTANCE or value in SUPPORTED_DASH_LANGUAGES:
                values[key] = value
        elif key == "theme":
            if value in THEMES:
                values[key] = value
        elif key == "density":
            if value in DENSITIES:
                values[key] = value
        elif key == "landing":
            if value in LANDINGS:
                values[key] = value
        elif key == "signature":
            values[key] = str(value or "")[:MAX_SIGNATURE]
        else:
            values[key] = bool(value)
    return values


def defaults() -> dict[str, Any]:
    """What an operator who has never saved anything gets."""
    return _clean(None)


async def for_operator(admin_id: int | None) -> dict[str, Any]:
    """This operator's preferences, or the defaults.

    `None` is a real case: the instance can still be on the bootstrap shared
    password, where nobody is a row in `admin_users` yet.
    """
    if not admin_id:
        return defaults()
    async with db_session() as session:
        raw = (await session.execute(
            select(AdminUser.preferences).where(AdminUser.id == admin_id)
        )).scalar_one_or_none()
    return _clean(raw)


async def save(admin_id: int, values: dict[str, Any]) -> dict[str, Any]:
    """Store what is valid and return what will now be in effect.

    The whole set is written each time rather than merged, because the page
    submits the whole form: a checkbox that was cleared sends nothing at all,
    and merging would make unchecking impossible.
    """
    cleaned = _clean(values)
    async with db_session() as session:
        admin = await session.get(AdminUser, admin_id)
        if admin is None:
            return cleaned
        admin.preferences = cleaned
        await session.commit()
    logger.info(f"Preferences saved for operator {admin_id}")
    return cleaned


def apply_signature(text: str, signature: str) -> str:
    """A reply with the operator's sign-off, if they set one.

    Not appended when it is already there: an operator who pastes their own
    signature into a template should not get it twice, and templates that
    end in one are common.
    """
    text = (text or "").rstrip()
    signature = (signature or "").strip()
    if not signature or not text:
        return text
    if text.endswith(signature):
        return text
    return f"{text}\n\n{signature}"
