"""Upgrade populated pre-release schemas on both supported database engines."""
import uuid
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import Column, ForeignKey, MetaData, Table, inspect, text
from sqlalchemy.ext.asyncio import create_async_engine

from src.database import delivery_models, inbound_models, workspace_models  # noqa: F401
from src.database.db import engine as shared_engine
from src.database.migrations import CURRENT_REVISION, migrate
from src.database.models import Base


@asynccontextmanager
async def baseline(tmp_path):
    pg = shared_engine.dialect.name == "postgresql"
    local = shared_engine if pg else create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'upgrade.db').as_posix()}")
    async with local.connect() as conn:
        schema = "release_" + uuid.uuid4().hex
        if pg:
            await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
            await conn.execute(text(f'SET search_path TO "{schema}"'))
        else:
            await conn.execute(text("BEGIN IMMEDIATE"))
        try:
            omitted = {
                "admin_users": ["session_version"], "users": ["identity_verified_fields", "customer_profile_id"],
                "tickets": ["conversation_key"],
                "messages": ["operator_id", "admin_message_id", "delivery_status", "delivery_error", "delivered_at", "reply_to_id", "edited_at", "revision", "mutation_status"],
            }
            previous = MetaData()
            for table in Base.metadata.sorted_tables:
                columns = [Column(c.name, c.type,
                    *(ForeignKey(f.target_fullname, ondelete=f.ondelete) for f in c.foreign_keys),
                    primary_key=c.primary_key, nullable=c.nullable,
                    server_default=c.server_default, autoincrement=c.autoincrement)
                    for c in table.columns if c.name not in omitted.get(table.name, [])]
                Table(table.name, previous, *columns)
            await conn.run_sync(previous.create_all)
            await conn.execute(text("INSERT INTO users(id,tg_id,channel,channel_user_id,identity_verified,is_verified,is_active,created_at) VALUES(1,100,'telegram','100',true,true,true,CURRENT_TIMESTAMP)"))
            for ident in (1, 2):
                await conn.execute(text("INSERT INTO tickets(id,user_id,category,status,stage,notification_sent,ticket_code,telegram_id,created_at) VALUES(:id,1,'Other','active','new',false,'',100,CURRENT_TIMESTAMP)"), {"id": ident})
            await conn.commit()
            yield conn
        finally:
            await conn.rollback()
            if pg:
                await conn.execute(text('SET search_path TO public'))
                await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
                await conn.commit()
    if not pg:
        await local.dispose()


async def test_populated_upgrade_preserves_tickets_and_invalidates_legacy_proof(tmp_path):
    async with baseline(tmp_path) as conn:
        await conn.run_sync(migrate)
        await conn.commit()
        rows = (await conn.execute(text("SELECT id,conversation_key FROM tickets ORDER BY id"))).all()
        assert rows == [(1, "legacy:1"), (2, "")]
        assert not (await conn.execute(text("SELECT identity_verified FROM users WHERE id=1"))).scalar()
        assert (await conn.execute(text("SELECT revision FROM schema_migrations ORDER BY revision DESC"))).scalar() == CURRENT_REVISION
        columns = await conn.run_sync(lambda c: {x["name"] for x in inspect(c).get_columns("messages")})
        assert {"delivery_status", "delivered_at", "operator_id", "reply_to_id", "edited_at", "revision", "mutation_status"} <= columns
        await conn.run_sync(migrate)
        assert (await conn.execute(text("SELECT COUNT(*) FROM schema_migrations"))).scalar() == 3


async def test_unknown_newer_revision_refuses_downgrade(tmp_path):
    async with baseline(tmp_path) as conn:
        await conn.run_sync(migrate)
        await conn.execute(text("INSERT INTO schema_migrations VALUES('future',CURRENT_TIMESTAMP)"))
        await conn.commit()
        with pytest.raises(RuntimeError, match="newer"):
            await conn.run_sync(migrate)


async def test_migration_failure_does_not_publish_revision(tmp_path, monkeypatch):
    from src.database import migrations
    async with baseline(tmp_path) as conn:
        if conn.dialect.name == "sqlite":
            await conn.execute(text("BEGIN IMMEDIATE"))
        def broken(sync):
            sync.execute(text("UPDATE users SET username='partial change'"))
            raise RuntimeError("injected migration failure")
        monkeypatch.setattr(migrations, "REVISIONS", ((CURRENT_REVISION, broken),))
        with pytest.raises(RuntimeError, match="injected"):
            await conn.run_sync(migrate)
        await conn.rollback()
        assert (await conn.execute(text("SELECT username FROM users WHERE id=1"))).scalar() is None
        exists = await conn.run_sync(lambda c: inspect(c).has_table("schema_migrations"))
        if exists:
            assert (await conn.execute(text("SELECT COUNT(*) FROM schema_migrations"))).scalar() == 0
