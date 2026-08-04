"""Team CRUD — replaces the old hard-coded team defaults with
runtime-managed teams.

Deletion semantics (defined by the models):
    - users.team_id is ON DELETE SET NULL → deleting a team un-assigns its
      users rather than deleting them. They keep their tickets and history.
    - reply_templates cascade-delete with the team (templates are team-scoped).
So deleting a team is recoverable for users (re-assign them) but destroys that
team's templates. We surface a warning with the user/template counts before
deletion so the admin makes an informed choice.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.database.models import ReplyTemplate, Team, User

logger = logging.getLogger(__name__)


class TeamError(Exception):
    """Validation / conflict on a team operation."""
def _t(key: str, **kwargs) -> str:
    from src.dashboard.i18n import t as dash_t
    return dash_t(key, **kwargs)


@dataclass(slots=True)
class TeamRow:
    id: int
    name: str
    user_count: int
    template_count: int


async def list_teams(session: AsyncSession) -> list[TeamRow]:
    """All teams with their user + (non-archived) template counts."""
    teams = (await session.execute(select(Team).order_by(Team.name.asc()))).scalars().all()

    # User counts in one grouped query
    user_counts = dict(
        (await session.execute(
            select(User.team_id, func.count(User.id))
            .where(User.team_id.isnot(None))
            .group_by(User.team_id)
        )).all()
    )
    template_counts = dict(
        (await session.execute(
            select(ReplyTemplate.team_id, func.count(ReplyTemplate.id))
            .where(ReplyTemplate.is_archived.is_(False))
            .group_by(ReplyTemplate.team_id)
        )).all()
    )

    return [
        TeamRow(
            id=t.id,
            name=t.name,
            user_count=int(user_counts.get(t.id, 0)),
            template_count=int(template_counts.get(t.id, 0)),
        )
        for t in teams
    ]


async def get_team(session: AsyncSession, team_id: int) -> Team | None:
    return (await session.execute(
        select(Team).where(Team.id == team_id)
    )).scalar_one_or_none()


async def create_team(session: AsyncSession, name: str) -> Team:
    """Create a team. Raises TeamError on empty/duplicate name."""
    name = (name or "").strip()
    if not name:
        raise TeamError(_t("v_name_empty"))
    if len(name) > 100:
        raise TeamError(_t("v_name_too_long_100"))

    existing = (await session.execute(
        select(Team).where(func.lower(Team.name) == name.lower())
    )).scalar_one_or_none()
    if existing is not None:
        raise TeamError(_t("v_duplicate", name=name))

    team = Team(name=name)
    session.add(team)
    await session.flush()
    logger.info(f"Team #{team.id} '{name}' created")
    return team


async def delete_team(session: AsyncSession, team_id: int) -> None:
    """Delete a team. Users are un-assigned (SET NULL), templates cascade-delete."""
    team = await get_team(session, team_id)
    if team is None:
        raise TeamError(_t("v_not_found", id=team_id))
    # The DB handles the cascades; we just issue the delete.
    await session.execute(delete(Team).where(Team.id == team_id))
    logger.info(f"Team #{team_id} '{team.name}' deleted")
