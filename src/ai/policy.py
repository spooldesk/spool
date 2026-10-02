"""One fail-closed AI policy, used at every entry point before reading context."""

import logging

from src.services import settings_service

logger = logging.getLogger(__name__)


async def allows(ticket, *, deflection: bool = False) -> bool:
    """Unassigned/closed/blocked-category tickets never reach an LLM.

    A team is the boundary for private templates and historical examples.
    Requiring it avoids accidentally searching every team's knowledge base.
    """
    try:
        return bool(
            ticket is not None
            and ticket.status == "active"
            and ticket.team_id
            and await settings_service.get_ai_enabled()
            and (not deflection or await settings_service.get_ai_deflection_enabled())
            and not await settings_service.is_category_ai_blacklisted(ticket.category)
        )
    except Exception:
        logger.exception("AI policy could not be evaluated; request withheld")
        return False


async def ticket_allowed(ticket_id: int, *, deflection: bool = False) -> bool:
    from src.database.db import db_session
    from src.database.models import Ticket

    try:
        async with db_session() as session:
            ticket = await session.get(Ticket, ticket_id)
        return await allows(ticket, deflection=deflection)
    except Exception:
        logger.exception("AI ticket policy could not be read; request withheld")
        return False


async def permitted_sources(rows) -> list:
    """A permitted ticket does not make a blocked-category source permissible."""
    return [row for row in rows
            if not await settings_service.is_category_ai_blacklisted(row.category)]
