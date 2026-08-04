"""Dashboard operator accounts — argon2 password hashing + CRUD.

This is the business layer behind per-user dashboard login (Phase 2). The web
routes and auth helpers call into here; they never touch password hashing or the
ORM directly.

Why argon2: memory-hard, OWASP's current top recommendation, ships as a wheel on
every platform we target (no Rust build step, unlike some alternatives).
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from sqlalchemy import func, select

from src.database.db import db_session
from src.database.models import AdminUser

logger = logging.getLogger(__name__)


def _t(key: str, **kwargs) -> str:
    from src.dashboard.i18n import t as dash_t
    return dash_t(key, **kwargs)

# Default argon2id parameters (argon2-cffi defaults are sane for a web login).
# Centralised so we can tune cost later and let check_needs_rehash upgrade
# existing hashes transparently on next successful login.
_hasher = PasswordHasher()


# ==================== Hashing ====================


def hash_password(plain: str) -> str:
    """Return an argon2id hash string (self-describing: params + salt + digest)."""
    return _hasher.hash(plain)


def _verify(plain: str, hashed: str) -> bool:
    """Constant-time verify. False on mismatch or malformed stored hash."""
    try:
        return _hasher.verify(hashed, plain)
    except (VerifyMismatchError, InvalidHashError, Exception):  # noqa: BLE001
        return False


# ==================== Queries ====================


async def get_by_email(email: str) -> Optional[AdminUser]:
    norm = _norm_email(email)
    async with db_session() as session:
        return (await session.execute(
            select(AdminUser).where(AdminUser.email == norm)
        )).scalar_one_or_none()


async def get_by_id(admin_id: int) -> Optional[AdminUser]:
    async with db_session() as session:
        return await session.get(AdminUser, admin_id)


async def list_admins() -> list[AdminUser]:
    async with db_session() as session:
        return list((await session.execute(
            select(AdminUser).order_by(AdminUser.created_at.asc())
        )).scalars().all())


async def count_admins() -> int:
    async with db_session() as session:
        return int((await session.execute(
            select(func.count()).select_from(AdminUser)
        )).scalar_one())


# ==================== Mutations ====================


async def create_admin(
    *,
    email: str,
    password: str,
    name: Optional[str] = None,
    role: str = "admin",
    tg_id: Optional[int] = None,
    is_active: bool = True,
) -> AdminUser:
    """Create an operator. Raises ValueError if the email already exists."""
    norm = _norm_email(email)
    if not norm or "@" not in norm:
        raise ValueError(_t("e_invalid_email"))
    if not password or len(password) < 8:
        raise ValueError(_t("e_password_short"))
    async with db_session() as session:
        exists = (await session.execute(
            select(AdminUser.id).where(AdminUser.email == norm)
        )).scalar_one_or_none()
        if exists is not None:
            raise ValueError(_t("e_email_exists"))
        admin = AdminUser(
            email=norm,
            password_hash=hash_password(password),
            name=(name or "").strip() or None,
            role=role if role in ("owner", "admin", "agent") else "admin",
            tg_id=tg_id,
            is_active=is_active,
        )
        session.add(admin)
        await session.commit()
        await session.refresh(admin)
    logger.info("AdminUser created: %s (role=%s)", norm, admin.role)
    return admin


async def authenticate(email: str, password: str) -> Optional[AdminUser]:
    """Return the AdminUser on valid credentials + active account, else None.

    Side effects on success: bumps last_login_at and transparently upgrades the
    stored hash if argon2 parameters have since changed.
    """
    norm = _norm_email(email)
    async with db_session() as session:
        admin = (await session.execute(
            select(AdminUser).where(AdminUser.email == norm)
        )).scalar_one_or_none()
        if admin is None or not admin.is_active:
            # Still spend time hashing to blunt user-enumeration timing attacks.
            _verify(password, "$argon2id$v=19$m=65536,t=3,p=4$"
                              "c2FsdHNhbHRzYWx0$c2FsdHNhbHRzYWx0c2FsdHNhbHQ")
            return None
        if not _verify(password, admin.password_hash):
            return None
        admin.last_login_at = datetime.utcnow()
        try:
            if _hasher.check_needs_rehash(admin.password_hash):
                admin.password_hash = hash_password(password)
        except Exception:  # noqa: BLE001
            pass
        await session.commit()
        await session.refresh(admin)
        return admin


async def set_password(admin_id: int, new_password: str) -> bool:
    if not new_password or len(new_password) < 8:
        raise ValueError(_t("e_password_short"))
    async with db_session() as session:
        admin = await session.get(AdminUser, admin_id)
        if admin is None:
            return False
        admin.password_hash = hash_password(new_password)
        await session.commit()
    logger.info("AdminUser %s password changed", admin_id)
    return True


async def set_active(admin_id: int, active: bool) -> bool:
    async with db_session() as session:
        admin = await session.get(AdminUser, admin_id)
        if admin is None:
            return False
        admin.is_active = active
        await session.commit()
    logger.info("AdminUser %s active=%s", admin_id, active)
    return True


async def update_profile(
    admin_id: int, *, name: Optional[str] = None, role: Optional[str] = None,
    tg_id: Optional[int] = None,
) -> bool:
    async with db_session() as session:
        admin = await session.get(AdminUser, admin_id)
        if admin is None:
            return False
        if name is not None:
            admin.name = name.strip() or None
        if role is not None and role in ("owner", "admin", "agent"):
            admin.role = role
        if tg_id is not None:
            admin.tg_id = tg_id or None
        await session.commit()
    return True


async def delete_admin(admin_id: int) -> bool:
    async with db_session() as session:
        admin = await session.get(AdminUser, admin_id)
        if admin is None:
            return False
        await session.delete(admin)
        await session.commit()
    logger.info("AdminUser %s deleted", admin_id)
    return True


# ==================== Helpers ====================


def _norm_email(email: str) -> str:
    return (email or "").strip().lower()


def display_name(admin: AdminUser) -> str:
    """Name for the UI: explicit name, else the email's local part."""
    if admin.name:
        return admin.name
    return admin.email.split("@", 1)[0] if admin.email else "admin"
