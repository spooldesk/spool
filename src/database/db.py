"""Database session and initialization helpers."""

from contextlib import asynccontextmanager
from typing import AsyncIterator

from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from src.config import load_settings
from src.database import models


# Create an async SQLAlchemy engine using the configured database URL.
settings = load_settings()
# Under pytest (TESTING=1) use NullPool: tests spin up multiple event loops
# (TestClient + async tests), and pooled aiosqlite connections are loop-bound.
import os as _os  # noqa: E402
if _os.getenv("TESTING") == "1":
    from sqlalchemy.pool import NullPool
    engine: AsyncEngine = create_async_engine(
        settings.database_url, echo=False, poolclass=NullPool
    )
else:
    engine: AsyncEngine = create_async_engine(settings.database_url, echo=False)

# Provide a session factory for request-scoped database work.
SessionFactory = async_sessionmaker(engine, expire_on_commit=False)


# ---- Custom SQLite functions ------------------------------------------------
# SQLite's LIKE/lower() are case-insensitive for ASCII only, which breaks
# Cyrillic search ("принтер" wouldn't match "Принтер"). ci_contains() does a
# proper Unicode casefold in Python and doubles as an injection-safe "contains"
# (no LIKE wildcards to escape). Registered on every new connection.

def _ci_contains(haystack, needle) -> int:
    if haystack is None or needle is None:
        return 0
    return 1 if str(needle).casefold() in str(haystack).casefold() else 0


import logging  # noqa: E402

from sqlalchemy import event  # noqa: E402


@event.listens_for(engine.sync_engine, "connect")
def _register_sqlite_functions(dbapi_connection, connection_record) -> None:
    # driver_connection is the aiosqlite.Connection; its create_function is a
    # coroutine that proxies into aiosqlite's worker thread (the raw sqlite3
    # object is thread-bound, so it must NOT be called directly). The connect
    # event runs inside SQLAlchemy's greenlet context, so await_only() works.
    from sqlalchemy.util import await_only
    raw = getattr(dbapi_connection, "driver_connection", dbapi_connection)
    try:
        await_only(raw.create_function("ci_contains", 2, _ci_contains, deterministic=True))
    except Exception:  # pragma: no cover — never block a connection over this
        logging.getLogger(__name__).warning("Could not register ci_contains()", exc_info=True)


@asynccontextmanager
async def db_session() -> AsyncIterator[AsyncSession]:
    """Yield a managed async DB session."""

    session = SessionFactory()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


async def init_db() -> None:
    """Create database tables if they do not already exist and run migrations.

    Also enables WAL journal mode so the dashboard (separate process, read-only)
    can read without blocking the bot's writes. Safe to call repeatedly.
    """

    async with engine.begin() as conn:
        # WAL allows concurrent reads from the dashboard process without locking.
        # Persistent across opens — set once, sticks until manually changed.
        await conn.execute(text("PRAGMA journal_mode=WAL"))
        await conn.run_sync(models.Base.metadata.create_all)
        await conn.run_sync(_migrate_to_new_schema)
        await conn.run_sync(_init_teams)
        await conn.run_sync(_init_categories)
        await conn.run_sync(_init_close_reasons)
        await conn.run_sync(_init_admin_user)


def _migrate_to_new_schema(sync_connection) -> None:
    """Migrate existing database to the new schema if needed."""

    inspector = inspect(sync_connection)
    tables = inspector.get_table_names()

    if "users" not in tables:
        return

    user_columns = {col["name"] for col in inspector.get_columns("users")}
    
    # Rename telegram_id to tg_id if needed (SQLite 3.25.0+)
    if "telegram_id" in user_columns and "tg_id" not in user_columns:
        try:
            sync_connection.execute(
                text("ALTER TABLE users RENAME COLUMN telegram_id TO tg_id")
            )
            user_columns.remove("telegram_id")
            user_columns.add("tg_id")
        except Exception:
            # If RENAME COLUMN fails (old SQLite version), create new column and copy data
            sync_connection.execute(
                text("ALTER TABLE users ADD COLUMN tg_id BIGINT")
            )
            sync_connection.execute(
                text("UPDATE users SET tg_id = telegram_id WHERE tg_id IS NULL")
            )
            user_columns.add("tg_id")
    
    # If tg_id doesn't exist and telegram_id doesn't exist either, add tg_id
    if "tg_id" not in user_columns:
        sync_connection.execute(
            text("ALTER TABLE users ADD COLUMN tg_id BIGINT")
        )
    
    # Add is_verified if missing
    if "is_verified" not in user_columns:
        sync_connection.execute(
            text("ALTER TABLE users ADD COLUMN is_verified BOOLEAN DEFAULT 0 NOT NULL")
        )
    
    # Add is_active if missing (for blocking functionality)
    if "is_active" not in user_columns:
        sync_connection.execute(
            text("ALTER TABLE users ADD COLUMN is_active BOOLEAN DEFAULT 1 NOT NULL")
        )
    
    # Add team_id if missing (for teams system)
    if "team_id" not in user_columns:
        sync_connection.execute(
            text("ALTER TABLE users ADD COLUMN team_id INTEGER")
        )
    
    # Add local_user_id if missing (for teams system)
    if "local_user_id" not in user_columns:
        sync_connection.execute(
            text("ALTER TABLE users ADD COLUMN local_user_id INTEGER")
        )
    
    # Add language if missing (for localization)
    if "language" not in user_columns:
        sync_connection.execute(
            text("ALTER TABLE users ADD COLUMN language VARCHAR(10)")
        )

    # Add work_username (bridges a personal Telegram account ↔ the work name)
    if "work_username" not in user_columns:
        try:
            sync_connection.execute(
                text("ALTER TABLE users ADD COLUMN work_username VARCHAR(64)")
            )
        except Exception:
            pass

    # Add linked_to_user_id (personal account → work account mapping)
    if "linked_to_user_id" not in user_columns:
        try:
            sync_connection.execute(
                text("ALTER TABLE users ADD COLUMN linked_to_user_id INTEGER")
            )
        except Exception:
            pass

    # Add pending_work_username (deferred link target until the work acct appears)
    if "pending_work_username" not in user_columns:
        try:
            sync_connection.execute(
                text("ALTER TABLE users ADD COLUMN pending_work_username VARCHAR(64)")
            )
        except Exception:
            pass
    
    # Which channel a customer talks to us through, and their address on it.
    # Backfilled rather than left null: every customer predating this column
    # arrived through Telegram, where the routing key and the address were the
    # same number. See src/channels/identity.py.
    if "channel" not in user_columns:
        try:
            sync_connection.execute(
                text("ALTER TABLE users ADD COLUMN channel VARCHAR(20) DEFAULT 'telegram' NOT NULL")
            )
        except Exception:
            pass
    if "channel_user_id" not in user_columns:
        try:
            sync_connection.execute(
                text("ALTER TABLE users ADD COLUMN channel_user_id VARCHAR(255)")
            )
            sync_connection.execute(
                text("UPDATE users SET channel_user_id = CAST(tg_id AS TEXT) "
                     "WHERE channel_user_id IS NULL")
            )
        except Exception:
            pass

    # Who a widget visitor says they are, and whether they proved it.
    for column, ddl in (
        ("email", "ALTER TABLE users ADD COLUMN email VARCHAR(320)"),
        ("external_id", "ALTER TABLE users ADD COLUMN external_id VARCHAR(120)"),
        ("identity_verified",
         "ALTER TABLE users ADD COLUMN identity_verified BOOLEAN DEFAULT 0 NOT NULL"),
    ):
        if column not in user_columns:
            try:
                sync_connection.execute(text(ddl))
            except Exception:
                pass

    # Remove old columns if they exist (but keep is_active now)
    for old_col in ["is_deleted", "full_name"]:
        if old_col in user_columns:
            try:
                sync_connection.execute(text(f"ALTER TABLE users DROP COLUMN {old_col}"))
            except Exception:
                pass  # Column might be referenced, skip

    if "tickets" not in tables:
        return

    ticket_columns = {col["name"] for col in inspector.get_columns("tickets")}
    
    # Ensure user_id exists (required for new schema)
    if "user_id" not in ticket_columns:
        # Check if tickets.id is a foreign key to users (old schema)
        # If so, we can use it to populate user_id
        try:
            sync_connection.execute(
                text("ALTER TABLE tickets ADD COLUMN user_id INTEGER")
            )
            # Try to populate from existing relationships
            # If tickets.id was the user_id in old schema, copy it
            try:
                sync_connection.execute(
                    text("UPDATE tickets SET user_id = id WHERE user_id IS NULL")
                )
            except Exception:
                pass
            # Refresh ticket_columns after adding user_id
            ticket_columns.add("user_id")
        except Exception:
            pass
    
    # Add new columns if missing
    if "admin_categories" not in ticket_columns:
        sync_connection.execute(
            text("ALTER TABLE tickets ADD COLUMN admin_categories TEXT")
        )
    
    if "custom_reason" not in ticket_columns:
        sync_connection.execute(
            text("ALTER TABLE tickets ADD COLUMN custom_reason TEXT")
        )
    
    if "processing_time_seconds" not in ticket_columns:
        sync_connection.execute(
            text("ALTER TABLE tickets ADD COLUMN processing_time_seconds INTEGER")
        )
    
    if "category" not in ticket_columns:
        sync_connection.execute(
            text("ALTER TABLE tickets ADD COLUMN category VARCHAR(50) DEFAULT 'Інше питання' NOT NULL")
        )
    
    if "status" not in ticket_columns:
        sync_connection.execute(
            text("ALTER TABLE tickets ADD COLUMN status VARCHAR(20) DEFAULT 'active' NOT NULL")
        )
    else:
        # Migrate old 'open' status to 'active'
        try:
            sync_connection.execute(
                text("UPDATE tickets SET status = 'active' WHERE status = 'open'")
            )
        except Exception:
            pass
    
    # Add closed_at if missing
    if "closed_at" not in ticket_columns:
        sync_connection.execute(
            text("ALTER TABLE tickets ADD COLUMN closed_at DATETIME")
        )
    
    if "thread_id" not in ticket_columns:
        sync_connection.execute(
            text("ALTER TABLE tickets ADD COLUMN thread_id INTEGER")
        )
    
    # Add header_message_id if missing
    if "header_message_id" not in ticket_columns:
        sync_connection.execute(
            text("ALTER TABLE tickets ADD COLUMN header_message_id INTEGER")
        )
    
    # Add team_id if missing (denormalized for quick access)
    if "team_id" not in ticket_columns:
        sync_connection.execute(
            text("ALTER TABLE tickets ADD COLUMN team_id INTEGER")
        )
        # Populate team_id from user's team_id
        try:
            sync_connection.execute(
                text("""
                    UPDATE tickets 
                    SET team_id = (
                        SELECT users.team_id 
                        FROM users 
                        WHERE users.id = tickets.user_id
                    )
                """)
            )
        except Exception:
            pass
    
    # Add notification_sent if missing
    if "notification_sent" not in ticket_columns:
        sync_connection.execute(
            text("ALTER TABLE tickets ADD COLUMN notification_sent BOOLEAN DEFAULT 0 NOT NULL")
        )

    # CSAT columns (user rates support 1–5 after ticket close)
    if "csat_rating" not in ticket_columns:
        sync_connection.execute(
            text("ALTER TABLE tickets ADD COLUMN csat_rating INTEGER")
        )
    if "csat_rated_at" not in ticket_columns:
        sync_connection.execute(
            text("ALTER TABLE tickets ADD COLUMN csat_rated_at DATETIME")
        )

    # The channel's id for a message we sent. Backfilled from the Telegram-only
    # column it replaces, so unsending keeps working for everything already in
    # the transcript.
    _msg_cols = {col["name"] for col in inspector.get_columns("messages")} \
        if "messages" in tables else set()
    if _msg_cols and "channel_ref" not in _msg_cols:
        try:
            sync_connection.execute(
                text("ALTER TABLE messages ADD COLUMN channel_ref VARCHAR(64)")
            )
            if "tg_message_id" in _msg_cols:
                sync_connection.execute(text(
                    "UPDATE messages SET channel_ref = CAST(tg_message_id AS TEXT) "
                    "WHERE tg_message_id IS NOT NULL AND channel_ref IS NULL"
                ))
        except Exception:
            pass

    # Per-operator interface preferences.
    if "admin_users" in tables:
        _admin_cols = {col["name"] for col in inspector.get_columns("admin_users")}
        if "preferences" not in _admin_cols:
            try:
                sync_connection.execute(
                    text("ALTER TABLE admin_users ADD COLUMN preferences TEXT")
                )
            except Exception:
                pass

    # Who spoke, in conversations that have more than one person in them.
    message_columns = {col["name"] for col in inspector.get_columns("messages")}         if "messages" in tables else set()
    if message_columns and "sender_label" not in message_columns:
        try:
            sync_connection.execute(
                text("ALTER TABLE messages ADD COLUMN sender_label VARCHAR(80)")
            )
        except Exception:
            pass

    # SLA policy bookkeeping — see src/services/sla_service.py
    if "sla_breached_at" not in ticket_columns:
        sync_connection.execute(
            text("ALTER TABLE tickets ADD COLUMN sla_breached_at DATETIME")
        )

    # Workflow stage (board column within an active ticket)
    if "stage" not in ticket_columns:
        sync_connection.execute(
            text("ALTER TABLE tickets ADD COLUMN stage VARCHAR(20) DEFAULT 'new' NOT NULL")
        )
        # Anything that already has an admin reply is past "new".
        try:
            sync_connection.execute(text("""
                UPDATE tickets SET stage = 'in_progress'
                WHERE status = 'active' AND EXISTS (
                    SELECT 1 FROM messages m
                    WHERE m.ticket_id = tickets.id AND m.sender_type = 'admin'
                )
            """))
        except Exception:
            pass

    # Operator ownership (assignment)
    if "assigned_admin_id" not in ticket_columns:
        sync_connection.execute(
            text("ALTER TABLE tickets ADD COLUMN assigned_admin_id INTEGER "
                 "REFERENCES admin_users(id) ON DELETE SET NULL")
        )
    
    # ticket_code is a LEGACY column the ORM model still declares (see models.py).
    # It must NOT be dropped: create_all recreates it on every fresh DB and the
    # Ticket model selects it on every query. The old code tried to DROP it here,
    # which silently failed on pre-3.35 SQLite (so the column survived and things
    # worked) but SUCCEEDS on modern SQLite (3.35+) — removing a column the model
    # needs and breaking every ticket query with "no such column: ticket_code".
    # We only ensure existing rows carry a value to satisfy the NOT NULL constraint.
    if "ticket_code" in ticket_columns:
        try:
            sync_connection.execute(
                text("UPDATE tickets SET ticket_code = '#' || CAST(id AS TEXT) WHERE ticket_code IS NULL OR ticket_code = ''")
            )
        except Exception:
            pass
    
    # Handle telegram_id - keep it if it exists (legacy compatibility)
    if "telegram_id" in ticket_columns:
        # Ensure all rows have telegram_id populated from user_id if NULL
        try:
            sync_connection.execute(
                text("""
                    UPDATE tickets 
                    SET telegram_id = (
                        SELECT users.tg_id 
                        FROM users 
                        WHERE users.id = tickets.user_id
                    )
                    WHERE telegram_id IS NULL
                """)
            )
        except Exception:
            pass
    
    # Try to remove other old columns
    for old_col in ["username", "full_name", "verified", "active"]:
        if old_col in ticket_columns:
            try:
                sync_connection.execute(text(f"ALTER TABLE tickets DROP COLUMN {old_col}"))
            except Exception:
                pass

    if "messages" not in tables:
        return

    message_columns = {col["name"] for col in inspector.get_columns("messages")}
    
    # Rename sender to sender_type if needed
    if "sender" in message_columns and "sender_type" not in message_columns:
        sync_connection.execute(
            text("ALTER TABLE messages RENAME COLUMN sender TO sender_type")
        )
    
    # Add sender_id if missing
    if "sender_id" not in message_columns:
        sync_connection.execute(
            text("ALTER TABLE messages ADD COLUMN sender_id BIGINT")
        )
        # Try to populate from ticket user_id (only if user_id column exists in tickets)
        if "user_id" in ticket_columns:
            try:
                sync_connection.execute(
                    text("""
                        UPDATE messages 
                        SET sender_id = (
                            SELECT CASE 
                                WHEN messages.sender_type = 'user' THEN tickets.user_id
                                ELSE 0
                            END
                            FROM tickets 
                            WHERE tickets.id = messages.ticket_id
                        )
                    """)
                )
            except Exception:
                # If update fails, just leave sender_id as NULL
                pass
    
    # Rename file columns if needed
    if "telegram_file_id" in message_columns and "file_id" not in message_columns:
        sync_connection.execute(
            text("ALTER TABLE messages RENAME COLUMN telegram_file_id TO file_id")
        )
    
    # Remove old columns
    for old_col in ["has_attachment", "file_unique_id"]:
        if old_col in message_columns:
            try:
                sync_connection.execute(text(f"ALTER TABLE messages DROP COLUMN {old_col}"))
            except Exception:
                pass

    # Add template_id column for reply templates feature
    # Internal operator notes (never relayed to the user)
    if "is_internal" not in message_columns:
        try:
            sync_connection.execute(
                text("ALTER TABLE messages ADD COLUMN is_internal BOOLEAN DEFAULT 0 NOT NULL")
            )
        except Exception:
            pass

    # Telegram's id for the copy sitting in the user's chat, so an operator can
    # unsend it. Old rows stay NULL and simply cannot be unsent — there is no
    # way to recover an id that was never recorded.
    if "tg_message_id" not in message_columns:
        try:
            sync_connection.execute(
                text("ALTER TABLE messages ADD COLUMN tg_message_id BIGINT")
            )
        except Exception:
            pass

    if "deleted_at" not in message_columns:
        try:
            sync_connection.execute(
                text("ALTER TABLE messages ADD COLUMN deleted_at DATETIME")
            )
        except Exception:
            pass

    if "template_id" not in message_columns:
        try:
            sync_connection.execute(
                text(
                    "ALTER TABLE messages ADD COLUMN template_id INTEGER "
                    "REFERENCES reply_templates(id) ON DELETE SET NULL"
                )
            )
        except Exception:
            # Fallback if FK syntax fails on older SQLite — add plain column
            try:
                sync_connection.execute(
                    text("ALTER TABLE messages ADD COLUMN template_id INTEGER")
                )
            except Exception:
                pass

    # Close-reason sentinels used to be literal human-language strings
    # ("Свой вариант", "Решено ИИ") snapshotted into tickets.admin_categories.
    # That baked a language into every instance, so they became "__custom__" and
    # "__ai__". Rewrite old rows in place. The ORM writes this JSON with \uXXXX
    # escapes, so each value is matched in both the escaped and the plain form.
    _reason_renames = [
        # (escaped form, plain form, new sentinel)
        ("\\u0421\\u0432\\u043e\\u0439 \\u0432\\u0430\\u0440\\u0438\\u0430\\u043d\\u0442",
         "Свой вариант", "__custom__"),
        ("\\u0420\\u0435\\u0448\\u0435\\u043d\\u043e \\u0418\\u0418",
         "Решено ИИ", "__ai__"),
        ("Solved by AI", "Solved by AI", "__ai__"),
    ]
    for escaped, plain, sentinel in _reason_renames:
        try:
            sync_connection.execute(
                text("""
                    UPDATE tickets
                    SET admin_categories = replace(
                        replace(admin_categories, :esc_q, :new_q), :plain_q, :new_q)
                    WHERE admin_categories IS NOT NULL
                      AND (admin_categories LIKE :esc_like
                           OR admin_categories LIKE :plain_like)
                """),
                {
                    "esc_q": f'"{escaped}"', "plain_q": f'"{plain}"',
                    "new_q": f'"{sentinel}"',
                    "esc_like": f"%{escaped}%", "plain_like": f"%{plain}%",
                },
            )
        except Exception:
            pass

    # Add columns to ai_calls (for free-form AI suggestions + tracking who accepted)
    if "ai_calls" in tables:
        ai_call_columns = {col["name"] for col in inspector.get_columns("ai_calls")}
        if "suggested_text" not in ai_call_columns:
            try:
                sync_connection.execute(
                    text("ALTER TABLE ai_calls ADD COLUMN suggested_text TEXT")
                )
            except Exception:
                pass
        if "accepted_by" not in ai_call_columns:
            try:
                sync_connection.execute(
                    text("ALTER TABLE ai_calls ADD COLUMN accepted_by BIGINT")
                )
            except Exception:
                pass
        if "shadow" not in ai_call_columns:
            try:
                sync_connection.execute(
                    text("ALTER TABLE ai_calls ADD COLUMN shadow BOOLEAN DEFAULT 0 NOT NULL")
                )
            except Exception:
                pass

    # The onboarding tables are deliberately NOT dropped here. They only exist
    # in databases that ran the feature, dropping them would destroy that data,
    # and an unused table costs nothing. New installs never create them.


def _init_teams(sync_connection) -> None:
    """Seed a single neutral team on a fresh DB (table EMPTY only).

    Seeds only when empty — so a deployment that renamed/deleted its teams won't
    get defaults re-added on restart. White-label installs start with one generic
    team the owner can rename and add to.
    """
    inspector = inspect(sync_connection)
    if "teams" not in inspector.get_table_names():
        return
    try:
        cnt = sync_connection.execute(text("SELECT COUNT(*) FROM teams")).scalar()
        if cnt and int(cnt) > 0:
            return  # already has teams (seeded or user-defined) — leave alone
        sync_connection.execute(
            text("INSERT INTO teams (name, created_at) VALUES (:name, datetime('now'))"),
            {"name": "General"},
        )
    except Exception:
        pass  # Teams might already be initialized


def _init_categories(sync_connection) -> None:
    """Seed a default set of ticket categories on a fresh DB.

    Only seeds when the table is EMPTY — so a deployment that has had its
    categories edited/cleared won't get the defaults re-added. White-label
    deployments can simply delete these and add their own.
    """
    inspector = inspect(sync_connection)
    if "ticket_categories" not in inspector.get_table_names():
        return
    try:
        cnt = sync_connection.execute(
            text("SELECT COUNT(*) FROM ticket_categories")
        ).scalar()
        if cnt and int(cnt) > 0:
            return  # already has categories (seeded or user-defined) — leave alone
        # Neutral English defaults — this is a global product; every deployment
        # renames/replaces these from Settings → Categories.
        defaults = [
            ("Access", "🔑", 1),
            ("Software", "💻", 2),
            ("Hardware", "🖥", 3),
            ("Network", "🌐", 4),
            ("Other", "✏️", 5),
        ]
        for name, emoji, order in defaults:
            sync_connection.execute(
                text("INSERT INTO ticket_categories (name, emoji, sort_order, is_active, created_at) "
                     "VALUES (:n, :e, :o, 1, datetime('now'))"),
                {"n": name, "e": emoji, "o": order},
            )
    except Exception:
        pass


def _init_admin_user(sync_connection) -> None:
    """Seed one operator account from ACCESS_PASSWORD on a fresh DB (table empty).

    Phase 2 introduces per-user dashboard login. To avoid locking the owner out
    during the transition, the existing shared ACCESS_PASSWORD becomes a real
    account: email 'admin@local', role 'owner'. Only seeds when the table is
    EMPTY, so a deployment that created/renamed/removed accounts won't get this
    re-added on restart. The login flow also falls back to the shared password
    while no accounts exist, so there's never a moment without access.
    """
    inspector = inspect(sync_connection)
    if "admin_users" not in inspector.get_table_names():
        return
    try:
        cnt = sync_connection.execute(text("SELECT COUNT(*) FROM admin_users")).scalar()
        if cnt and int(cnt) > 0:
            return
        pw = (settings.access_password or "").strip()
        if not pw:
            return  # nothing to seed from; login falls back to shared password (none) → no-op
        from argon2 import PasswordHasher
        pw_hash = PasswordHasher().hash(pw)
        sync_connection.execute(
            text(
                "INSERT INTO admin_users (email, password_hash, name, role, is_active, created_at) "
                "VALUES (:e, :h, :n, 'owner', 1, datetime('now'))"
            ),
            {"e": "admin@local", "h": pw_hash, "n": "Owner"},
        )
    except Exception:
        pass


def _init_close_reasons(sync_connection) -> None:
    """Seed default ticket-closure reasons on a fresh DB (table empty only).

    Mirrors the previously hard-coded admin closure categories. White-label
    deployments can delete these and add their own from the dashboard. The
    free-text "Свой вариант" option is a code sentinel, not a row here.
    """
    inspector = inspect(sync_connection)
    if "close_reasons" not in inspector.get_table_names():
        return
    try:
        cnt = sync_connection.execute(
            text("SELECT COUNT(*) FROM close_reasons")
        ).scalar()
        if cnt and int(cnt) > 0:
            return
        defaults = [
            ("Solved", "✅", 1),
            ("Access granted", "🔑", 2),
            ("Configuration fixed", "🛠", 3),
            ("Hardware replaced", "🖥", 4),
            ("Not an issue", "💬", 5),
        ]
        for name, emoji, order in defaults:
            sync_connection.execute(
                text("INSERT INTO close_reasons (name, emoji, sort_order, is_active, created_at) "
                     "VALUES (:n, :e, :o, 1, datetime('now'))"),
                {"n": name, "e": emoji, "o": order},
            )
    except Exception:
        pass
