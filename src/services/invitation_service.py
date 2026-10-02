"""Owner-issued, expiring invitations. Only a token hash is stored."""
import asyncio
import hashlib
import secrets
import re
from datetime import datetime, timedelta
from sqlalchemy import select, update, delete
from src.database.db import db_session
from src.database.models import AdminUser, OperatorInvitation, OperatorTeam, Team
from src.services.admin_user_service import hash_password


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def issue(*, actor_id: int, email: str, name: str, role: str, team_id=None) -> str:
    email = email.strip().lower()
    if len(email) > 255 or not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', email):
        raise ValueError('e_invalid_email')
    from src.services.permissions import valid_role
    if not await valid_role(role):
        raise ValueError('e_invalid_role')
    now, token = datetime.utcnow(), secrets.token_urlsafe(32)
    async with db_session(write=True) as session:
        if not await valid_role(role, session):
            raise ValueError('e_invalid_role')
        actor = await session.get(AdminUser, actor_id)
        if actor is None or not actor.is_active or actor.role != 'owner':
            raise ValueError('e_forbidden_section')
        if (await session.execute(select(AdminUser.id).where(AdminUser.email == email))).first():
            raise ValueError('e_email_exists')
        if team_id and await session.get(Team, team_id) is None:
            raise ValueError('e_invalid_team')
        await session.execute(update(OperatorInvitation).where(OperatorInvitation.email == email,
            OperatorInvitation.accepted_at.is_(None), OperatorInvitation.revoked_at.is_(None)
        ).values(revoked_at=now))
        session.add(OperatorInvitation(email=email, name=name.strip()[:120], role=role,
            team_id=team_id, created_by=actor.id, token_hash=_hash(token), expires_at=now+timedelta(hours=48)))
    return token


async def accept(token: str, password: str) -> AdminUser:
    if not 20 <= len(token) <= 100:
        raise ValueError('invite_invalid')
    if not 8 <= len(password) <= 1024:
        raise ValueError('e_password_short')
    # Check token before expensive password hashing, then recheck under the write lock.
    async with db_session() as session:
        row = (await session.execute(select(OperatorInvitation).where(
            OperatorInvitation.token_hash == _hash(token), OperatorInvitation.revoked_at.is_(None),
            OperatorInvitation.accepted_at.is_(None), OperatorInvitation.expires_at > datetime.utcnow(),
        ))).scalar_one_or_none()
        if row is None:
            raise ValueError('invite_invalid')
        ident = row.id
    password_hash = await asyncio.to_thread(hash_password, password)
    async with db_session(write=True) as session:
        row = await session.get(OperatorInvitation, ident)
        if row is None or row.revoked_at or row.accepted_at or row.expires_at <= datetime.utcnow():
            raise ValueError('invite_invalid')
        creator = await session.get(AdminUser, row.created_by)
        if creator is None or not creator.is_active or creator.role != 'owner':
            raise ValueError('invite_invalid')
        from src.services.permissions import valid_role
        if not await valid_role(row.role, session):
            raise ValueError('invite_invalid')
        if (await session.execute(select(AdminUser.id).where(AdminUser.email == row.email))).first():
            raise ValueError('invite_invalid')
        admin = AdminUser(email=row.email, name=row.name or None, role=row.role,
                          password_hash=password_hash, is_active=True)
        session.add(admin); await session.flush()
        if row.team_id:
            session.add(OperatorTeam(operator_id=admin.id, team_id=row.team_id))
        row.accepted_at = datetime.utcnow()
        return admin


async def revoke(actor_id: int, invitation_id: int) -> None:
    async with db_session(write=True) as session:
        actor = await session.get(AdminUser, actor_id)
        if not actor or not actor.is_active or actor.role != 'owner':
            raise ValueError('e_forbidden_section')
        row = await session.get(OperatorInvitation, invitation_id)
        if row and row.accepted_at is None:
            row.revoked_at = datetime.utcnow()


async def pending() -> list:
    async with db_session() as session:
        return list((await session.execute(select(OperatorInvitation).where(
            OperatorInvitation.accepted_at.is_(None), OperatorInvitation.revoked_at.is_(None),
            OperatorInvitation.expires_at > datetime.utcnow(),
        ).order_by(OperatorInvitation.expires_at.desc()))).scalars().all())


async def set_teams(actor_id: int, operator_id: int, team_ids: list[int]) -> None:
    ids = set(team_ids)
    if any(type(i) is not int or not 0 < i <= 2147483647 for i in ids):
        raise ValueError('e_invalid_team')
    async with db_session(write=True) as session:
        actor, operator = await session.get(AdminUser, actor_id), await session.get(AdminUser, operator_id)
        if not actor or not actor.is_active or actor.role != 'owner' or not operator:
            raise ValueError('e_forbidden_section')
        valid = set((await session.execute(select(Team.id).where(Team.id.in_(ids)))).scalars())
        if valid != ids:
            raise ValueError('e_invalid_team')
        await session.execute(delete(OperatorTeam).where(OperatorTeam.operator_id == operator_id))
        session.add_all(OperatorTeam(operator_id=operator_id, team_id=i) for i in ids)
