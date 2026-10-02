"""The free-text close reason is a language-neutral sentinel.

It used to be the literal string "Свой вариант", which was snapshotted into
tickets.admin_categories — so an English instance showed a Russian reason on
every custom closure, in the UI and in the Excel export.
"""

import pytest
from sqlalchemy import select, text

from src.dashboard import queries
from src.dashboard.i18n import set_active_language
from src.database.db import db_session, engine, init_db
from src.database.dialect import is_sqlite
from src.database.models import Team, Ticket, User
from src.services import close_reason_service as crs


@pytest.fixture(autouse=True)
def _restore_language():
    yield
    set_active_language("en")


async def _closed_ticket(admin_categories: list[str], tg_id: int) -> int:
    async with db_session() as session:
        team = (await session.execute(
            select(Team).where(Team.name == "sentinel-team")
        )).scalar_one_or_none()
        if team is None:
            team = Team(name="sentinel-team")
            session.add(team)
            await session.flush()
        u = User(tg_id=tg_id, username=f"sent_{tg_id}", team_id=team.id,
                 is_verified=True, is_active=True)
        session.add(u)
        await session.flush()
        t = Ticket(user_id=u.id, team_id=team.id, category="Other",
                   status="closed", admin_categories=admin_categories,
                   custom_reason="Handled over the phone")
        session.add(t)
        await session.flush()
        tid = t.id
        await session.commit()
    return tid


async def test_sentinel_is_not_a_human_language(initialized_db):
    """The stored value must survive a language switch unchanged."""
    assert crs.CUSTOM_VALUE == "__custom__"
    assert not any(0x400 <= ord(ch) <= 0x4FF for ch in crs.CUSTOM_VALUE)


async def test_custom_display_follows_the_ui_language(initialized_db):
    set_active_language("en")
    assert crs.custom_display() == "✏️ Custom option"
    set_active_language("ru")
    assert crs.custom_display() == "✏️ Свой вариант"


async def test_stored_value_renders_localised(initialized_db):
    tid = await _closed_ticket([crs.CUSTOM_VALUE, "Access granted"], tg_id=980001)

    set_active_language("en")
    detail = await queries.ticket_detail(tid)
    assert detail["admin_categories"] == ["✏️ Custom option", "Access granted"]
    assert not any("Свой" in c for c in detail["admin_categories"])

    set_active_language("ru")
    detail_ru = await queries.ticket_detail(tid)
    assert detail_ru["admin_categories"] == ["✏️ Свой вариант", "Access granted"]


async def test_export_renders_the_sentinel_too(initialized_db):
    tid = await _closed_ticket([crs.CUSTOM_VALUE], tg_id=980002)
    set_active_language("en")
    rows = await queries.list_tickets_for_export()
    row = next(r for r in rows if r["id"] == tid)
    assert "__custom__" not in row["closure_categories"]
    assert "Custom option" in row["closure_categories"]


@pytest.mark.skipif(
    not is_sqlite(str(engine.url)),
    reason="the rewrite is part of the SQLite in-place migration, which a "
           "PostgreSQL database does not run — it has no legacy to rewrite",
)
async def test_legacy_value_is_migrated(initialized_db):
    """A DB written before versioned migrations is rewritten by the bootstrap.

    Asserts on the *stored* form, which is where the engines part company:
    SQLAlchemy escapes non-ASCII into `\u0421` and SQLite keeps that text
    verbatim, which is exactly what the migration matches on. PostgreSQL
    parses JSON into a value and hands back a list, so there is no escaped
    text to find — and no legacy row to fix either, because create_all builds
    a current schema on a database with no history.
    """
    tid = await _closed_ticket(["Свой вариант", "VPN"], tg_id=980003)

    async with engine.begin() as conn:
        stored = (await conn.execute(text(
            "SELECT admin_categories FROM tickets WHERE id = :i"), {"i": tid}
        )).scalar_one()
    # SQLAlchemy escapes non-ASCII, which is exactly what the migration matches
    assert "\\u0421" in stored

    from src.database.db import _migrate_to_new_schema
    async with engine.begin() as conn:
        await conn.run_sync(_migrate_to_new_schema)

    async with engine.begin() as conn:
        migrated = (await conn.execute(text(
            "SELECT admin_categories FROM tickets WHERE id = :i"), {"i": tid}
        )).scalar_one()
    assert "__custom__" in migrated
    assert "\\u0421" not in migrated
    assert "VPN" in migrated, "other reasons must be left alone"


async def test_legacy_value_still_renders_if_it_slips_through(initialized_db):
    """Read-side safety net for a DB restored from an older backup."""
    assert crs.is_custom("Свой вариант")
    set_active_language("en")
    assert crs.display_stored("Свой вариант") == "✏️ Custom option"
    assert crs.display_stored("Access granted") == "Access granted"


async def test_cannot_create_a_reason_shadowing_the_custom_option(initialized_db):
    """Blocked in every supported language, not just the active one."""
    async with db_session() as session:
        set_active_language("en")
        for name in ("Custom option", "custom option", "Свой вариант", "__custom__"):
            with pytest.raises(crs.CloseReasonError):
                await crs.create(session, name=name, emoji=None)


async def test_validation_errors_are_localised(initialized_db):
    async with db_session() as session:
        set_active_language("en")
        with pytest.raises(crs.CloseReasonError) as e:
            await crs.create(session, name="", emoji=None)
        assert "cannot be empty" in str(e.value)

        set_active_language("ru")
        with pytest.raises(crs.CloseReasonError) as e:
            await crs.create(session, name="", emoji=None)
        assert "пустым" in str(e.value)
