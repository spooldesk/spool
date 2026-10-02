"""Ordered, transactional schema revisions shared by SQLite and PostgreSQL.

Revisions are forward-only. Restoring a verified pre-upgrade backup is the
rollback procedure; dropping columns is not a safe rollback for support data.
"""

from datetime import datetime

from sqlalchemy import inspect, text

CURRENT_REVISION = "20260924_03"


def _add(conn, table: str, column: str, definition: str) -> bool:
    if column in {c["name"] for c in inspect(conn).get_columns(table)}:
        return False
    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {definition}"))
    return True


def _release_01(conn) -> None:
    _add(conn, "admin_users", "session_version", "INTEGER NOT NULL DEFAULT 0")
    added = _add(conn, "users", "identity_verified_fields", "JSON")
    if added:
        conn.execute(text("UPDATE users SET identity_verified = false, identity_verified_fields = '[]'"))
    _add(conn, "tickets", "conversation_key", "VARCHAR(255) NOT NULL DEFAULT ''")
    _add(conn, "messages", "operator_id", "INTEGER REFERENCES admin_users(id) ON DELETE SET NULL")
    _add(conn, "messages", "admin_message_id", "BIGINT")
    _add(conn, "messages", "delivery_status", "VARCHAR(20) NOT NULL DEFAULT 'delivered'")
    _add(conn, "messages", "delivery_error", "TEXT")
    _add(conn, "messages", "delivered_at", "TIMESTAMP")
    _add(conn, "outbound_jobs", "purpose", "VARCHAR(40)")
    if conn.dialect.name == "postgresql":
        conn.execute(text("ALTER TABLE messages ALTER COLUMN channel_ref TYPE VARCHAR(998)"))
        conn.execute(text("ALTER TABLE messages ALTER COLUMN file_id TYPE TEXT"))
        conn.execute(text("ALTER TABLE users ALTER COLUMN channel_user_id TYPE VARCHAR(320)"))

    # Do not delete or merge historical conversations. Preserve each extra
    # legacy active ticket under a separate conversation key.
    rows = conn.execute(text(
        "SELECT id, user_id, conversation_key FROM tickets WHERE status='active' ORDER BY id DESC"
    )).fetchall()
    seen = set()
    for ident, user_id, key in rows:
        pair = (user_id, key)
        if pair in seen:
            conn.execute(text("UPDATE tickets SET conversation_key=:key WHERE id=:id"),
                         {"key": f"legacy:{ident}", "id": ident})
        seen.add(pair)

    # Setting a nullable dangling relation to NULL preserves the record.
    # Required relations need an explicit operator repair, never data loss.
    from src.database.models import Base
    for table in Base.metadata.sorted_tables:
        if table.name not in inspect(conn).get_table_names():
            continue
        columns = {c["name"] for c in inspect(conn).get_columns(table.name)}
        for column in table.columns:
            if column.name not in columns:
                continue
            for fk in column.foreign_keys:
                parent = fk.column.table.name
                target = fk.column.name
                missing = (f'"{column.name}" IS NOT NULL AND NOT EXISTS '
                           f'(SELECT 1 FROM "{parent}" p WHERE p."{target}" = '
                           f'"{table.name}"."{column.name}")')
                count = conn.execute(text(f'SELECT COUNT(*) FROM "{table.name}" WHERE {missing}')).scalar()
                if not count:
                    continue
                if not column.nullable:
                    raise RuntimeError(f"Migration refused: {count} orphan records in {table.name}.{column.name}. Restore or repair a copy first.")
                conn.execute(text(f'UPDATE "{table.name}" SET "{column.name}"=NULL WHERE {missing}'))

    for name, statement in (
        ("uq_active_conversation", "ON tickets(user_id, conversation_key) WHERE status='active'"),
        ("uq_customer_channel_address", "ON users(channel, channel_user_id) WHERE channel_user_id IS NOT NULL AND channel_user_id <> ''"),
        ("uq_operator_telegram", "ON admin_users(tg_id) WHERE tg_id IS NOT NULL"),
    ):
        conn.execute(text(f"CREATE UNIQUE INDEX IF NOT EXISTS {name} {statement}"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_messages_operator_id ON messages(operator_id)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_messages_admin_message_id ON messages(admin_message_id)"))
    if conn.dialect.name == "sqlite":
        if conn.execute(text("PRAGMA foreign_key_check")).first():
            raise RuntimeError("Migration refused: foreign-key integrity check failed")


def _conversation_02(conn) -> None:
    _add(conn, "messages", "reply_to_id", "INTEGER REFERENCES messages(id) ON DELETE SET NULL")
    _add(conn, "messages", "edited_at", "TIMESTAMP")
    _add(conn, "messages", "revision", "INTEGER NOT NULL DEFAULT 0")
    _add(conn, "messages", "mutation_status", "VARCHAR(20)")
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_messages_reply_to_id ON messages(reply_to_id)"))


def _workspace_03(conn) -> None:
    _add(conn, "users", "customer_profile_id", "INTEGER REFERENCES customer_profiles(id) ON DELETE SET NULL")
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_users_customer_profile_id ON users(customer_profile_id)"))
    # Preserve established associations as neutral customer profiles. Pending
    # username matches are not identity proof and are deliberately discarded.
    rows = conn.execute(text('SELECT id, linked_to_user_id FROM users')).all()
    parent = {ident: ident for ident, _ in rows}
    def find(ident):
        while parent[ident] != ident:
            ident = parent[ident]
        return ident
    for ident, linked in rows:
        if linked in parent:
            parent[find(ident)] = find(linked)
    groups = {}
    for ident in parent:
        groups.setdefault(find(ident), []).append(ident)
    for ids in groups.values():
        if len(ids) < 2:
            continue
        profile = conn.execute(text("INSERT INTO customer_profiles(name,created_at) VALUES('',:now) RETURNING id"), {'now': datetime.utcnow()}).scalar()
        for ident in ids:
            conn.execute(text('UPDATE users SET customer_profile_id=:profile WHERE id=:ident AND customer_profile_id IS NULL'), {'profile': profile, 'ident': ident})
    conn.execute(text('UPDATE users SET linked_to_user_id=NULL, pending_work_username=NULL'))


REVISIONS = (("20260917_01", _release_01), ("20260922_02", _conversation_02),
             (CURRENT_REVISION, _workspace_03))


def migrate(conn) -> None:
    conn.execute(text("CREATE TABLE IF NOT EXISTS schema_migrations (revision VARCHAR(64) PRIMARY KEY, applied_at TIMESTAMP NOT NULL)"))
    applied = set(conn.execute(text("SELECT revision FROM schema_migrations")).scalars())
    known = {revision for revision, _ in REVISIONS}
    if applied - known:
        raise RuntimeError("Database schema is newer than this application; restore the matching release")
    for revision, upgrade in REVISIONS:
        if revision in applied:
            continue
        upgrade(conn)
        conn.execute(text("INSERT INTO schema_migrations(revision, applied_at) VALUES(:revision,:now)"),
                     {"revision": revision, "now": datetime.utcnow()})
