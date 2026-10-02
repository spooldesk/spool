"""One section policy for server routes, navigation and operator mutations.

Teams route work; these permissions grant sections, not tenant isolation.
Unknown sections/roles fail closed. Owner-only administration is not delegable.
"""
from datetime import datetime
import secrets
from sqlalchemy import select, update
from src.database.db import db_session
from src.database.models import AdminUser, OperatorInvitation, Ticket
from src.database.workspace_models import CustomRole

BUILTINS = ("owner", "admin", "agent", "viewer")
SECTIONS = {
    "tickets": ("nav_tickets", "/tickets"),
    "board": ("nav_board", "/board"),
    "users": ("nav_users", "/users"),
    "templates": ("nav_templates", "/manage/templates"),
    "articles": ("nav_articles", "/manage/articles"),
    "categories": ("nav_categories", "/manage/categories"),
    "close_reasons": ("nav_close_reasons", "/manage/close-reasons"),
    "teams": ("nav_teams", "/manage/teams"),
    "settings": ("nav_settings", "/manage/settings"),
    "ai_settings": ("nav_ai_config", "/manage/ai"),
    "overview": ("nav_dashboard", "/overview"),
    "reports": ("nav_reports", "/reports"),
    "ai": ("nav_ai_analytics", "/ai"),
    "assistant": ("nav_ai_assistant", "/assistant"),
    "broadcast": ("nav_broadcast", "/broadcast"),
}
OWNER_SECTIONS = {"integrations", "operators", "roles"}
PREFIXES = [
    ("/manage/settings/widget", "integrations"),
    ("/manage/integrations", "integrations"), ("/manage/roles", "roles"),
    ("/manage/admins", "operators"), ("/manage/close-reasons", "close_reasons"),
    ("/manage/templates", "templates"), ("/manage/articles", "articles"),
    ("/manage/categories", "categories"), ("/manage/teams", "teams"),
    ("/manage/settings", "settings"),
    ("/manage/ai", "ai_settings"), ("/api/tickets", "tickets"),
    ("/api/tickets-version", "tickets"), ("/api/queue-status", "tickets"),
    ("/tickets", "tickets"), ("/board", "board"), ("/queue", "tickets"),
    ("/users", "users"), ("/templates", "templates"), ("/reports", "reports"),
    ("/overview", "overview"), ("/ai", "ai"), ("/assistant", "assistant"), ("/broadcast", "broadcast"),
    ("/me", "personal"), ("/views", "personal"),
]


def section_for(path: str) -> str | None:
    path = path.rstrip("/") or "/"
    if path == "/":
        return "personal"
    if path.startswith('/api/tickets/') and path.endswith('/stage'):
        return 'board'
    return next((section for prefix, section in PREFIXES
                 if path == prefix or path.startswith(prefix + "/")), None)


async def for_role(role: str, session=None) -> dict[str, str]:
    if role == "owner":
        return {s: "edit" for s in (*SECTIONS, *OWNER_SECTIONS, "personal")}
    if role == "admin":
        return {**{s: "edit" for s in SECTIONS}, "personal": "edit"}
    if role in {"agent", "viewer"}:
        mode = "edit" if role == "agent" else "view"
        return {"tickets": mode, "board": mode, "users": mode, "overview": "view", "ai": "view",
                "templates": "view", "assistant": mode, "personal": "edit"}
    if session is None:
        async with db_session() as db:
            return await for_role(role, db)
    row = await session.get(CustomRole, role)
    if row is None:
        return {"personal": "edit"}
    return {**{s: m for s, m in row.permissions.items()
               if s in SECTIONS and m in {"view", "edit"}}, "personal": "edit"}


async def can(actor, section: str, *, edit=False, session=None) -> bool:
    if actor is None or not actor.is_active:
        return False
    mode = (await for_role(actor.role, session)).get(section)
    return mode == "edit" if edit else mode in {"view", "edit"}


async def valid_role(role: str, session=None) -> bool:
    if role in BUILTINS:
        return True
    if session is None:
        async with db_session() as db:
            return await valid_role(role, db)
    return bool(role and len(role) <= 20 and await session.get(CustomRole, role))


async def choices() -> dict[str, str]:
    async with db_session() as session:
        rows = (await session.execute(select(CustomRole).order_by(CustomRole.name))).scalars()
        return {**{r: r for r in BUILTINS}, **{r.key: r.name for r in rows}}


async def save(actor_id: int, name: str, grants: dict, key: str = "") -> str:
    name = name.strip()
    if not name or len(name) > 80 or name.lower() in BUILTINS:
        raise ValueError("role_name_invalid")
    if set(grants) - set(SECTIONS) or any(v not in {"none", "view", "edit"} for v in grants.values()):
        raise ValueError("role_permissions_invalid")
    normalized = {s: v for s, v in grants.items() if v != "none"}
    if not normalized:
        raise ValueError("role_permissions_empty")
    async with db_session(write=True) as session:
        actor = await session.get(AdminUser, actor_id)
        if not actor or not actor.is_active or actor.role != "owner":
            raise ValueError("e_forbidden_section")
        row = await session.get(CustomRole, key) if key else None
        if key and row is None:
            raise ValueError("e_invalid_role")
        duplicate = (await session.execute(select(CustomRole).where(CustomRole.name == name))).scalar_one_or_none()
        if duplicate and duplicate.key != key:
            raise ValueError("role_name_exists")
        if row is None:
            row = CustomRole(key="role_" + secrets.token_hex(6), name=name, permissions=normalized)
            session.add(row)
        else:
            row.name, row.permissions = name, normalized
            row.updated_at = datetime.utcnow()
            await session.execute(update(AdminUser).where(AdminUser.role == row.key)
                                  .values(session_version=AdminUser.session_version + 1))
            if normalized.get("tickets") != "edit":
                ids = select(AdminUser.id).where(AdminUser.role == row.key)
                await session.execute(update(Ticket).where(Ticket.status == "active",
                    Ticket.assigned_admin_id.in_(ids)).values(assigned_admin_id=None))
        return row.key


async def remove(actor_id: int, key: str) -> None:
    async with db_session(write=True) as session:
        actor = await session.get(AdminUser, actor_id)
        if not actor or not actor.is_active or actor.role != "owner":
            raise ValueError("e_forbidden_section")
        row = await session.get(CustomRole, key)
        if row is None:
            raise ValueError("e_invalid_role")
        assigned = (await session.execute(select(AdminUser.id).where(AdminUser.role == key))).first()
        pending = (await session.execute(select(OperatorInvitation.id).where(
            OperatorInvitation.role == key, OperatorInvitation.accepted_at.is_(None),
            OperatorInvitation.revoked_at.is_(None), OperatorInvitation.expires_at > datetime.utcnow()))).first()
        if assigned or pending:
            raise ValueError("role_in_use")
        await session.delete(row)
