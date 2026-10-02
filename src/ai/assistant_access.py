"""Interactive operator AI policy, distinct from automatic customer deflection.

Closed/unassigned tickets can be researched by an operator who can view tickets.
Teams route work; section permissions (not team membership) control visibility.
The category blacklist still applies to every ticket and knowledge source.
"""
from dataclasses import dataclass

from src.database.db import db_session
from src.database.models import AdminUser
from src.services import permissions, settings_service

ENABLED_KEY = "ai_assistant_enabled"


class AssistantError(Exception):
    def __init__(self, code="assistant_unavailable", status=400):
        self.code, self.status = code, status
        super().__init__(code)


async def enabled() -> bool:
    return (await settings_service.get_ai_enabled()
            and await settings_service.get_value(ENABLED_KEY, "0") == "1")


@dataclass(frozen=True)
class Access:
    actor_id: int
    generation: int
    grants: dict
    blacklist: tuple[str, ...]
    provider: str
    model: str

    def can(self, section: str, edit=False) -> bool:
        mode = self.grants.get(section)
        return mode == "edit" if edit else mode in {"view", "edit"}

    def permits_category(self, category) -> bool:
        return not any(fragment in (category or "").lower() for fragment in self.blacklist)


async def authorize(actor_id: int, generation: int) -> Access:
    if not await enabled():
        raise AssistantError("assistant_disabled", 403)
    async with db_session() as session:
        actor = await session.get(AdminUser, actor_id)
        if not actor or not actor.is_active or actor.session_version != generation:
            raise AssistantError("assistant_forbidden", 403)
        grants = await permissions.for_role(actor.role, session)
    if grants.get("assistant") not in {"view", "edit"}:
        raise AssistantError("assistant_forbidden", 403)
    return Access(actor_id, generation, grants, tuple(await settings_service.get_ai_blacklist()),
                  await settings_service.get_ai_provider(), await settings_service.get_ai_model())


async def recheck(access: Access) -> None:
    if await authorize(access.actor_id, access.generation) != access:
        raise AssistantError("assistant_context_changed", 409)
