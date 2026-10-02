"""An instance with no Telegram at all.

The landing page promises five minutes from clone to running. Someone who
wants the website widget and nothing else used to be made to create a
Telegram bot, find @BotFather, make a group and dig out its numeric id — for
a feature they were never going to use. That was the whole onboarding, spent
on the wrong thing.

So Telegram is optional now, the way Gemini already was. These tests are the
ones that would go red if it quietly stopped being optional: an import that
reaches for a token, a startup that refuses, a reply path that assumes an
admin group.
"""

import json

import pytest
from fastapi.testclient import TestClient

import src.channels as channels
from src.config import load_settings
from src.dashboard.app import app
from src.database.db import db_session
from src.database.models import Message, Team, Ticket
from src.services import announce_service, intake_service, settings_service
from tests.conftest import next_tg_id


@pytest.fixture()
async def no_telegram(initialized_db, monkeypatch):
    """Strip the token the way a widget-only install would never have set it."""
    import src.dashboard.bot_client as bot_client

    monkeypatch.delenv("BOT_TOKEN", raising=False)
    monkeypatch.delenv("ADMIN_CHAT_ID", raising=False)
    # The client caches its Bot for the life of the process.
    monkeypatch.setattr(bot_client, "_bot", None)
    channels.reset()
    yield
    channels.reset()
    monkeypatch.setattr(bot_client, "_bot", None)


# ---------------------------------------------------------------- config


async def test_the_settings_load_without_a_token(no_telegram):
    """Only the database and the first-login password are required. Anything
    else being mandatory is a step in an onboarding that promises five
    minutes."""
    settings = load_settings()
    assert settings.has_telegram is False
    assert settings.database_url and settings.access_password


async def test_a_token_without_admin_chat_supports_dashboard_operators(
    initialized_db, monkeypatch
):
    """The dashboard receives tickets without requiring a Telegram operator group."""
    monkeypatch.setenv("BOT_TOKEN", "1000000:TEST")
    monkeypatch.delenv("ADMIN_CHAT_ID", raising=False)
    assert load_settings().has_telegram is True


async def test_a_malformed_chat_id_does_not_take_the_dashboard_down(
    initialized_db, monkeypatch
):
    """A typo in one optional variable should cost Telegram, not the whole
    instance — the dashboard is what people are looking at."""
    monkeypatch.setenv("BOT_TOKEN", "1000000:TEST")
    monkeypatch.setenv("ADMIN_CHAT_ID", "not-a-number")
    settings = load_settings()
    assert settings.has_telegram is True
    assert settings.admin_chat_id == 0
    assert settings.database_url


# ---------------------------------------------------------------- the app


async def test_the_dashboard_serves_pages_without_a_bot(no_telegram):
    with TestClient(app, base_url="http://testserver") as c:
        c.get("/login")
        token = c.cookies.get("supbot_csrf")
        r = c.post("/login", data={"email": "admin@local",
                                   "password": "test-password-123",
                                   "next": "/", "csrf_token": token},
                   follow_redirects=False)
        assert r.status_code == 303
        assert c.get("/tickets").status_code == 200
        assert c.get("/board").status_code == 200


async def test_the_widget_takes_a_ticket_with_no_telegram_anywhere(no_telegram):
    """The whole point. A visitor writes on a site, a ticket exists, and no
    part of that needed a bot token."""
    await settings_service.set_widget_enabled(True, updated_by=0)
    try:
        import src.dashboard.routes.widget as widget_routes

        widget_routes._hits.clear()
        with TestClient(app, base_url="http://testserver") as c:
            r = c.post("/widget/api/message",
                       headers={"origin": "https://shop.example"},
                       content=json.dumps({"text": "my order never arrived"}))
        assert r.status_code == 200
        ticket_id = r.json()["ticket"]

        async with db_session() as session:
            ticket = await session.get(Ticket, ticket_id)
        assert ticket is not None and ticket.status == "active"
    finally:
        await settings_service.set_widget_enabled(False, updated_by=0)


async def test_asking_for_the_telegram_channel_says_why(no_telegram):
    """A LookupError like any unregistered channel — and one that names the
    missing variable, because "no adapter for 'telegram'" would send someone
    hunting through the wrong file."""
    with pytest.raises(LookupError) as caught:
        channels.get("telegram")
    assert "BOT_TOKEN" in str(caught.value)


async def test_announcing_is_a_quiet_no_op(no_telegram):
    """There is no admin group to announce into. The ticket is on the board
    and the customer has been answered either way."""
    async with db_session() as session:
        result = await intake_service.receive(session, intake_service.Inbound(
            channel="widget", address=f"quiet-{next_tg_id()}", text="hello",
        ))
        await session.commit()

    assert await announce_service.announce_ticket(None, result.ticket_id) is False


async def test_a_reply_still_reaches_a_widget_customer(no_telegram):
    """The mirror into the admin group is skipped; the reply to the customer
    is not. Those are two different sends and only one of them is Telegram's."""
    from src.channels.identity import create_customer
    from src.services import template_service
    from tests.conftest import RecordingChannel

    # Named something other than "telegram": that channel's address has to be
    # a chat id, and the point here is a customer who has no Telegram at all.
    recorder = RecordingChannel(name="site")
    channels.register(recorder)
    channel = recorder
    async with db_session() as session:
        team = Team(name=f"nt-{next_tg_id()}")
        session.add(team)
        await session.flush()
        user = await create_customer(session, channel="site",
                                     address=f"addr-{next_tg_id()}", team_id=team.id)
        ticket = Ticket(user_id=user.id, team_id=team.id, category="Other",
                        status="active")
        session.add(ticket)
        await session.flush()
        tid = ticket.id
        await session.commit()

    async with db_session() as session:
        result = await template_service.apply_text(
            session, None, ticket_id=tid, text="On its way.",
            admin_tg_id=0, admin_chat_id=0,
        )
        await session.commit()

    assert result.success, result.error
    assert channel.texts and channel.texts[-1][1] == "On its way."

    async with db_session() as session:
        from sqlalchemy import select
        rows = (await session.execute(
            select(Message).where(Message.ticket_id == tid,
                                  Message.sender_type == "admin")
        )).scalars().all()
    assert len(rows) == 1, "the reply was not recorded in the transcript"


# ---------------------------------------------------------------- the bot


async def test_optional_bot_process_stays_off_without_telegram(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from src import main
    from src.services import channel_runtime
    monkeypatch.setattr('src.database.db.init_db', AsyncMock())
    monkeypatch.setattr(channel_runtime, 'refresh', AsyncMock())
    poll = AsyncMock()
    monkeypatch.setattr(channel_runtime, 'telegram_loop', poll)
    monkeypatch.setattr(main, 'load_settings', lambda: SimpleNamespace(has_telegram=False))
    await main.main()
    poll.assert_not_called()
