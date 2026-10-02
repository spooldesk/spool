"""AI policy is enforced at use-case boundaries, not only in one notifier."""

import asyncio
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import delete, select

from src.ai import deflect, suggest_from_history, suggest_template
from src.ai.client import LLMResponse
from src.database.db import db_session
from src.database.models import AICall, Article, Message, ReplyTemplate, Team, Ticket, User
from src.services import choice_service, settings_service
from tests.conftest import next_tg_id


@pytest.fixture()
async def ai_context(initialized_db, monkeypatch):
    async with db_session() as session:
        original_articles = set((await session.execute(select(Article.id))).scalars())
    monkeypatch.setattr(settings_service, "get_ai_enabled", AsyncMock(return_value=True))
    monkeypatch.setattr(settings_service, "get_ai_deflection_enabled", AsyncMock(return_value=True))
    monkeypatch.setattr(settings_service, "get_ai_shadow_mode", AsyncMock(return_value=False))

    async def blocked(category):
        return "sensitive" in (category or "").lower()

    monkeypatch.setattr(settings_service, "is_category_ai_blacklisted", blocked)
    complete = AsyncMock(return_value=LLMResponse(
        text="Restart the local application and reconnect to the network.", model="offline-test",
    ))
    factory = AsyncMock(return_value=SimpleNamespace(complete=complete))
    for module in (deflect, suggest_from_history, suggest_template):
        monkeypatch.setattr(module, "get_client", factory)
    async with db_session() as session:
        team, other = Team(name=f"ai-own-{next_tg_id()}"), Team(name=f"ai-other-{next_tg_id()}")
        session.add_all([team, other])
        await session.flush()
        user = User(tg_id=next_tg_id(), team_id=team.id, is_active=True)
        session.add(user)
        await session.flush()
        ticket = Ticket(user_id=user.id, team_id=team.id, category="Network", status="active")
        old = Ticket(user_id=user.id, team_id=team.id, category="Network", status="closed",
                     closed_at=datetime.utcnow())
        session.add_all([ticket, old])
        await session.flush()
        session.add_all([
            ReplyTemplate(team_id=team.id, category="Network", title="local-public-marker",
                          body="Restart the local application.", created_by=0),
            ReplyTemplate(team_id=team.id, category="Sensitive", title="blocked-template-marker",
                          body="blocked-body-marker", created_by=0),
            ReplyTemplate(team_id=other.id, category="Network", title="other-team-marker",
                          body="other-body-marker", created_by=0),
            Message(ticket_id=ticket.id, sender_type="user", sender_id=user.tg_id,
                    text="The application cannot connect to the network"),
            Message(ticket_id=ticket.id, sender_type="admin", sender_id=1,
                    text="deleted-live-marker", deleted_at=datetime.utcnow()),
            Message(ticket_id=old.id, sender_type="user", sender_id=user.tg_id,
                    text="The application could not connect"),
            Message(ticket_id=old.id, sender_type="admin", sender_id=1,
                    text="Restart the local application and reconnect."),
            Message(ticket_id=old.id, sender_type="admin", sender_id=1,
                    text="deleted-history-marker", deleted_at=datetime.utcnow()),
            Message(ticket_id=old.id, sender_type="admin", sender_id=1,
                    text="internal-history-marker", is_internal=True),
            Message(ticket_id=old.id, sender_type="admin", sender_id=1,
                    text="undelivered-history-marker", delivery_status="pending"),
        ])
        tid, uid = ticket.id, user.tg_id
    yield tid, uid, factory, complete
    # Published knowledge affects the public help centre in later tests.
    # Delete only records created in this fixture's test, even on assertion failure.
    async with db_session() as session:
        await session.execute(delete(Article).where(Article.id.not_in(original_articles)))


@pytest.mark.parametrize("gate", ["disabled", "category", "no_team", "closed"])
async def test_every_direct_use_case_refuses_blocked_tickets_before_client_creation(
    ai_context, monkeypatch, gate,
):
    tid, _, factory, complete = ai_context
    if gate == "disabled":
        monkeypatch.setattr(settings_service, "get_ai_enabled", AsyncMock(return_value=False))
    else:
        async with db_session() as session:
            ticket = await session.get(Ticket, tid)
            if gate == "category":
                ticket.category = "Sensitive credentials"
            elif gate == "no_team":
                ticket.team_id = None
            else:
                ticket.status = "closed"
    await suggest_template.suggest_template_for_ticket(tid, "A detailed support question")
    await suggest_from_history.suggest_from_history_for_ticket(tid, "A detailed support question")
    await deflect.try_deflect_for_ticket(tid, "A detailed support question")
    factory.assert_not_awaited()
    complete.assert_not_awaited()


async def test_allowed_ticket_cannot_read_blocked_sources_other_teams_or_deleted_messages(ai_context):
    tid, _, _, complete = ai_context
    await suggest_template.suggest_template_for_ticket(tid, "A detailed support question")
    await suggest_from_history.suggest_from_history_for_ticket(tid, "A detailed support question")
    await deflect.try_deflect_for_ticket(tid, "A detailed support question")
    assert complete.await_count == 3
    prompts = "\n".join(call.kwargs["user"] for call in complete.await_args_list)
    assert "local-public-marker" in prompts
    assert "Restart the local application" in prompts
    for forbidden in ("blocked-template-marker", "blocked-body-marker", "other-team-marker",
                      "other-body-marker", "deleted-live-marker", "deleted-history-marker",
                      "internal-history-marker", "undelivered-history-marker"):
        assert forbidden not in prompts


async def test_deflection_off_does_not_call_a_model(ai_context, monkeypatch):
    tid, _, factory, _ = ai_context
    monkeypatch.setattr(settings_service, "get_ai_deflection_enabled", AsyncMock(return_value=False))
    assert (await deflect.try_deflect_for_ticket(tid, "A detailed support question")).answer is None
    factory.assert_not_awaited()


async def test_policy_change_during_generation_prevents_delivery(ai_context, monkeypatch):
    from src.services import outbound

    tid, uid, _, complete = ai_context

    async def turn_off(**kwargs):
        monkeypatch.setattr(settings_service, "get_ai_enabled", AsyncMock(return_value=False))
        return LLMResponse(text="Restart the local application and reconnect.", model="offline-test")

    complete.side_effect = turn_off
    send = AsyncMock()
    monkeypatch.setattr(outbound, "say", send)
    await deflect.deflect_and_offer(None, tid, "A detailed support question", uid, "en")
    assert complete.await_count == 1
    send.assert_not_awaited()


@pytest.mark.parametrize("prefix", ["defl", "deflect"])
async def test_deflection_payloads_close_the_right_ticket_exactly_once(ai_context, prefix):
    tid, uid, _, _ = ai_context
    async with db_session() as session:
        article = Article(slug=f"credit-{next_tg_id()}", title="Network", body="Restart",
                          is_published=True)
        session.add(article)
        await session.flush()
        call = AICall(ticket_id=tid, use_case="deflect", model="offline-test",
                      success=True, shadow=False, article_id=article.id)
        session.add(call)
        await session.flush()
        cid, aid = call.id, article.id
    results = await asyncio.gather(*[
        choice_service._dispatch(f"{prefix}:ok:{tid}:{cid}", ticket_id=tid, user_tg_id=uid)
        for _ in range(2)
    ])
    assert sum(result.ok for result in results) == 1
    async with db_session() as session:
        assert (await session.get(Ticket, tid)).status == "closed"
        assert (await session.get(AICall, cid)).accepted is True
        assert (await session.get(Article, aid)).deflected_count == 1


@pytest.mark.parametrize("mismatch", ["ticket", "owner", "use_case", "shadow", "failed", "payload"])
async def test_deflection_verdict_rejects_mismatched_evidence(ai_context, mismatch):
    tid, uid, _, _ = ai_context
    async with db_session() as session:
        call = AICall(ticket_id=None if mismatch == "ticket" else tid,
                      use_case="suggest_template" if mismatch == "use_case" else "deflect",
                      model="offline-test", success=mismatch != "failed", shadow=mismatch == "shadow")
        session.add(call)
        await session.flush()
        cid = call.id
    result = await choice_service._dispatch(
        f"defl:ok:{tid + 1 if mismatch == 'payload' else tid}:{cid}",
        ticket_id=tid, user_tg_id=uid + 1 if mismatch == "owner" else uid,
    )
    assert not result.ok
    async with db_session() as session:
        assert (await session.get(Ticket, tid)).status == "active"
        assert (await session.get(AICall, cid)).accepted is None
