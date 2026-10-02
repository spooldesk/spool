"""Practice data is additive, offline and repeat-safe."""
from collections import Counter
from datetime import datetime

import pytest
from sqlalchemy import delete, func, select

from scripts.seed_ai_preview import MARKER, seed
from src.database.db import db_session
from src.database.models import AppSetting, Article, Message, Ticket, User
from src.database.delivery_models import OutboundJob
from src.database.workspace_models import CustomerProfile


@pytest.fixture(autouse=True)
async def clean_practice_data(initialized_db):
    # The session database is shared: this large historical dataset must not
    # change queue ordering, board caps or language assertions in later tests.
    models = (Message, Ticket, Article, User, CustomerProfile)
    async with db_session() as s:
        before = {model: set((await s.execute(select(model.id))).scalars()) for model in models}
    yield
    async with db_session(write=True) as s:
        for model in models:
            await s.execute(delete(model).where(model.id.not_in(before[model])))
        await s.execute(delete(AppSetting).where(AppSetting.key.in_([MARKER, 'ai_preview_test_sentinel'])))


async def test_practice_seed_preserves_settings_and_is_idempotent(initialized_db):
    now = datetime(2026, 10, 1, 10, 0)
    async with db_session(write=True) as s:
        s.add(AppSetting(key="ai_preview_test_sentinel", value="must survive"))
        before_settings = dict((await s.execute(select(AppSetting.key, AppSetting.value))).all())
        before_tickets = set((await s.execute(select(Ticket.id))).scalars())
        before_jobs = (await s.execute(select(func.count()).select_from(OutboundJob))).scalar_one()
        first = await seed(s, now=now)
    async with db_session(write=True) as s:
        second = await seed(s, now=now)
        assert second.pop("already_present") is True and second == first
        after_settings = dict((await s.execute(select(AppSetting.key, AppSetting.value))).all())
        assert {k:v for k,v in after_settings.items() if k != MARKER} == before_settings
        assert (await s.execute(select(func.count()).select_from(OutboundJob))).scalar_one() == before_jobs
        assert set((await s.execute(select(Ticket.id))).scalars()) == before_tickets | set(first["tickets"])
        tickets = list((await s.execute(select(Ticket).where(Ticket.id.in_(first["tickets"])))).scalars())
        users = list((await s.execute(select(User).where(User.id.in_(first["users"])))).scalars())
        messages = list((await s.execute(select(Message).where(Message.id.in_(first["messages"])))).scalars())
        assert len(tickets) == 72 and len(users) == 24 and len(messages) == 304
        assert Counter(t.status for t in tickets) == {"active":24, "closed":48}
        assert Counter(t.stage for t in tickets if t.status == "active") == {"new":8, "in_progress":8, "waiting":8}
        assert sum(not u.is_verified for u in users) == 6
        assert all(u.channel == "widget" and u.email.endswith("@example.test") and len(u.channel_user_id) == 64 for u in users)
        assert all(t.created_at < now and (not t.closed_at or t.created_at < t.closed_at < now) for t in tickets)
        assert all(m.created_at < now and m.delivery_status == "delivered" for m in messages)
        assert not any((await s.execute(select(Article.is_published).where(Article.id.in_(first["articles"])))).scalars())
        for ticket in tickets:
            transcript = [m for m in messages if m.ticket_id == ticket.id and not m.is_internal]
            last = max(transcript, key=lambda m:m.created_at)
            if ticket.status == "active":
                assert last.sender_type == ("admin" if ticket.stage == "waiting" else "user")
