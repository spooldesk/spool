"""Schema creation + migration regressions.

The big one: `ticket_code` — the ORM model declares it, and an old migration
used to DROP it on modern SQLite, breaking every ticket query. This guards
against that class of bug (model ↔ schema drift) permanently.
"""

from sqlalchemy import text

from src.database.db import engine, init_db


async def _table_columns(table: str) -> set[str]:
    async with engine.connect() as conn:
        rows = await conn.execute(text(f"PRAGMA table_info({table})"))
        return {r[1] for r in rows.fetchall()}


async def test_init_db_is_idempotent(initialized_db):
    # Second (and third) run must not raise or mangle the schema.
    await init_db()
    await init_db()


async def test_tickets_schema_matches_model(initialized_db):
    cols = await _table_columns("tickets")
    # Every column the Ticket model selects must exist — incl. legacy ones.
    expected = {
        "id", "user_id", "team_id", "category", "status", "thread_id",
        "header_message_id", "notification_sent", "ticket_code", "telegram_id",
        "created_at", "closed_at", "admin_categories", "custom_reason",
        "processing_time_seconds", "csat_rating", "csat_rated_at",
    }
    missing = expected - cols
    assert not missing, f"tickets table is missing columns: {missing}"


async def test_core_tables_exist(initialized_db):
    async with engine.connect() as conn:
        rows = await conn.execute(text(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ))
        tables = {r[0] for r in rows.fetchall()}
    for required in ("users", "tickets", "messages", "teams", "admin_users",
                     "reply_templates", "ai_calls", "app_settings",
                     "ticket_categories", "close_reasons"):
        assert required in tables, f"missing table: {required}"


async def test_default_seeds_present(initialized_db):
    async with engine.connect() as conn:
        teams = (await conn.execute(text("SELECT COUNT(*) FROM teams"))).scalar_one()
        cats = (await conn.execute(text("SELECT COUNT(*) FROM ticket_categories"))).scalar_one()
        reasons = (await conn.execute(text("SELECT COUNT(*) FROM close_reasons"))).scalar_one()
    assert teams >= 1
    assert cats >= 1
    assert reasons >= 1
