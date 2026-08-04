"""Operator accounts: argon2 hashing, authentication, lifecycle."""

import pytest

from src.services import admin_user_service as svc


async def test_create_and_authenticate(initialized_db):
    admin = await svc.create_admin(
        email="alice@test.dev", password="alicepass123", name="Alice", role="admin",
    )
    assert admin.id > 0
    assert admin.password_hash.startswith("$argon2")
    assert "alicepass123" not in admin.password_hash

    ok = await svc.authenticate("alice@test.dev", "alicepass123")
    assert ok is not None and ok.id == admin.id
    assert ok.last_login_at is not None

    assert await svc.authenticate("alice@test.dev", "wrong-password") is None
    assert await svc.authenticate("nobody@test.dev", "alicepass123") is None


async def test_email_normalized_and_unique(initialized_db):
    await svc.create_admin(email="  Bob@Test.DEV ", password="bobpass12345")
    found = await svc.get_by_email("bob@test.dev")
    assert found is not None and found.email == "bob@test.dev"

    with pytest.raises(ValueError):
        await svc.create_admin(email="bob@test.dev", password="anotherpass1")


async def test_short_password_rejected(initialized_db):
    with pytest.raises(ValueError):
        await svc.create_admin(email="carol@test.dev", password="short")


async def test_disable_blocks_login(initialized_db):
    a = await svc.create_admin(email="dave@test.dev", password="davepass1234")
    assert await svc.authenticate("dave@test.dev", "davepass1234") is not None
    await svc.set_active(a.id, False)
    assert await svc.authenticate("dave@test.dev", "davepass1234") is None
    await svc.set_active(a.id, True)
    assert await svc.authenticate("dave@test.dev", "davepass1234") is not None


async def test_set_password(initialized_db):
    a = await svc.create_admin(email="erin@test.dev", password="erinpass1234")
    assert await svc.set_password(a.id, "newpass12345")
    assert await svc.authenticate("erin@test.dev", "erinpass1234") is None
    assert await svc.authenticate("erin@test.dev", "newpass12345") is not None
    with pytest.raises(ValueError):
        await svc.set_password(a.id, "tiny")


async def test_seeded_owner_exists(initialized_db):
    """A fresh DB seeds admin@local from ACCESS_PASSWORD (lockout protection)."""
    owner = await svc.get_by_email("admin@local")
    assert owner is not None
    assert owner.role == "owner"
    assert await svc.authenticate("admin@local", "test-password-123") is not None


def test_display_name():
    from src.database.models import AdminUser
    named = AdminUser(email="x@y.z", password_hash="h", name="Иван")
    unnamed = AdminUser(email="frank@corp.io", password_hash="h", name=None)
    assert svc.display_name(named) == "Иван"
    assert svc.display_name(unnamed) == "frank"
