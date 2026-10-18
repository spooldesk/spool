"""Customer-agent behaviour with a fake model and fake transports only."""
import asyncio
from dataclasses import asdict
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import delete, select
from src.ai import autonomy_settings, customer_agent, deflect
from src.ai.client import LLMResponse, LLMUnavailable
from src.database.ai_models import AIAutoTurn
from src.database.db import db_session
from src.database.delivery_models import OutboundJob
from src.database.models import AppSetting, Message, ReplyTemplate, Team, Ticket, User
from src.services import intake_service, settings_service, outbound
from tests.conftest import RecordingChannel, next_tg_id


@pytest.fixture
async def agent_setup(initialized_db, monkeypatch):
    import src.channels as channels
    keys = [settings_service.KEY_AI_ENABLED, settings_service.KEY_AI_DEFLECTION,
            settings_service.KEY_AI_SHADOW, settings_service.KEY_AI_BLACKLIST, autonomy_settings.KEY]
    async with db_session() as session:
        old = {key: (await session.get(AppSetting, key)).value if await session.get(AppSetting, key) else None for key in keys}
    await settings_service.set_ai_enabled(True, updated_by=0)
    await settings_service.set_ai_deflection_enabled(True, updated_by=0)
    await settings_service.set_ai_shadow_mode(False, updated_by=0)
    await settings_service.set_ai_blacklist([], updated_by=0)
    await autonomy_settings.save({"mode": "conversation"}, 0)
    model = AsyncMock(return_value=LLMResponse(
        text="ACTION: answer\nSOURCE: none\nRestart the application and check the network connection.",
        model="offline-test", tokens_in=100, tokens_out=20, latency_ms=250))
    monkeypatch.setattr(deflect, "get_client", AsyncMock(return_value=SimpleNamespace(complete=model)))
    pipes = {name: RecordingChannel(name=name) for name in ("telegram", "email", "slack", "widget")}
    for pipe in pipes.values():
        channels.register(pipe)
    ticket_ids, user_ids, team_ids = [], [], []

    async def start(channel="widget"):
        ident = next_tg_id()
        address = str(ident) if channel == "telegram" else f"offline-{ident}"
        async with db_session(write=True) as session:
            team = Team(name=f"agent-{ident}")
            session.add(team)
            await session.flush()
            user = User(tg_id=ident, channel=channel, channel_user_id=address, team_id=team.id,
                        is_active=True, is_verified=True, language="en")
            session.add_all([user, ReplyTemplate(team_id=team.id, category="Network", title="Network help",
                body="Restart the application and check the network connection.", created_by=0)])
            await session.flush()
            user_ids.append(user.id)
            team_ids.append(team.id)
        result = await incoming(channel, address, "The network cannot connect, please help")
        ticket_ids.append(result.ticket_id)
        return channel, address, result.ticket_id, await turn_for(result.message_id)
    yield start, model, pipes
    async with db_session() as session:
        from src.database.models import AICall
        await session.execute(delete(AICall).where(AICall.ticket_id.in_(ticket_ids)))
        await session.execute(delete(Ticket).where(Ticket.id.in_(ticket_ids)))
        await session.execute(delete(User).where(User.id.in_(user_ids)))
        await session.execute(delete(ReplyTemplate).where(ReplyTemplate.team_id.in_(team_ids)))
        await session.execute(delete(Team).where(Team.id.in_(team_ids)))
        for key, value in old.items():
            row = await session.get(AppSetting, key)
            if value is None and row:
                await session.delete(row)
            elif row:
                row.value = value
    channels.reset()


async def incoming(channel, address, text):
    async with db_session(write=True) as session:
        return await intake_service.receive(session, intake_service.Inbound(
            channel=channel, address=address, text=text, category="Network"))


async def turn_for(message_id):
    async with db_session() as session:
        return (await session.execute(select(AIAutoTurn).where(AIAutoTurn.message_id == message_id))).scalar_one()


async def reload_turn(ident):
    async with db_session() as session:
        return await session.get(AIAutoTurn, ident)


@pytest.mark.parametrize("channel", ["telegram", "slack", "email", "widget"])
async def test_multiturn_all_channels_and_durable_dedup(agent_setup, channel):
    start, model, _ = agent_setup
    channel, address, tid, first = await start(channel)
    await asyncio.gather(customer_agent.process(first.id), customer_agent.process(first.id))
    first = await reload_turn(first.id)
    assert model.await_count == 1
    async with db_session() as session:
        assert (await session.get(OutboundJob, first.outbound_job_id)).status == "delivered"
        source = await session.get(Message, first.message_id)
        await customer_agent.enqueue(session, source)
    follow = await incoming(channel, address, "No")
    second = await turn_for(follow.message_id)
    await customer_agent.process(second.id)
    assert model.await_count == 2
    prompt = model.call_args.kwargs
    assert "Restart the application" in prompt["user"] and "user: No" in prompt["user"]
    assert prompt["timeout_seconds"] == 45 and prompt["max_output_tokens"] == 1500
    from src.dashboard.ai_diagnostics import overview
    rows = [r for r in (await overview())["rows"] if r["ticket_id"] == tid]
    assert len(rows) == 2 and all(r["status"] == "delivered" for r in rows)


@pytest.mark.parametrize("reply,reason", [
    ("skip", "no_grounded_answer"), ("ACTION: handoff\nSOURCE: none", "needs_operator"),
    ("ACTION: nonsense\nSOURCE: none\nSomething here", "invalid_response"),
])
async def test_skips_have_a_visible_reason(agent_setup, reply, reason):
    start, model, _ = agent_setup
    _, _, tid, turn = await start()
    model.return_value = LLMResponse(text=reply, model="offline-test")
    await customer_agent.process(turn.id)
    row = await reload_turn(turn.id)
    assert row.reason == reason and row.outbound_job_id is None
    from src.dashboard.ai_diagnostics import overview
    assert any(r["ticket_id"] == tid and r["reason"] == reason for r in (await overview())["rows"])


@pytest.mark.parametrize("code", ["timeout", "auth", "billing", "truncated", "empty"])
async def test_provider_failure_is_categorized_without_secret(agent_setup, code):
    start, model, _ = agent_setup
    _, _, _, turn = await start()
    model.side_effect = LLMUnavailable("sensitive-provider-body", code=code)
    await customer_agent.process(turn.id)
    turn = await reload_turn(turn.id)
    assert turn.status == "failed" and turn.reason == f"provider_{code}"
    from src.database.models import AICall
    async with db_session() as session:
        call = await session.get(AICall, turn.ai_call_id)
        assert call.error == f"provider_{code}"


@pytest.mark.parametrize("mutation,expected", [("human", "human_takeover"), ("new_message", "superseded"),
    ("edit", "context_changed"), ("settings", "context_changed"), ("source", "sources_changed"),
    ("shadow", "context_changed"), ("close", "closed_or_blocked")])
async def test_changes_during_generation_withhold_stale_reply(agent_setup, mutation, expected):
    start, model, _ = agent_setup
    channel, address, tid, turn = await start()
    async def changed(**kwargs):
        if mutation == "new_message":
            await incoming(channel, address, "Actually I have a different question now")
        elif mutation == "settings":
            await autonomy_settings.save({"mode": "single"}, 0)
        elif mutation == "shadow":
            await settings_service.set_ai_shadow_mode(True, updated_by=0)
        else:
            async with db_session(write=True) as session:
                ticket = await session.get(Ticket, tid)
                if mutation == "human":
                    session.add(Message(ticket_id=tid, sender_type="admin", sender_id=9988, text="I am helping you."))
                elif mutation == "edit":
                    (await session.get(Message, turn.message_id)).revision += 1
                elif mutation == "source":
                    tpl = (await session.execute(select(ReplyTemplate).where(ReplyTemplate.team_id == ticket.team_id))).scalar_one()
                    tpl.is_archived = True
                else:
                    ticket.status = "closed"
        return LLMResponse(text="ACTION: answer\nSOURCE: none\nRestart the application.", model="offline-test")
    model.side_effect = changed
    await customer_agent.process(turn.id)
    row = await reload_turn(turn.id)
    assert row.reason == expected and row.outbound_job_id is None


async def test_internal_and_deleted_messages_never_enter_context(agent_setup):
    start, model, _ = agent_setup
    _, _, tid, turn = await start()
    async with db_session() as session:
        session.add_all([Message(ticket_id=tid, sender_type="admin", sender_id=33,
            text="PRIVATE-INTERNAL", is_internal=True), Message(ticket_id=tid, sender_type="admin", sender_id=33,
            text="PRIVATE-DELETED", deleted_at=datetime.utcnow())])
    await customer_agent.process(turn.id)
    assert model.await_count == 1
    assert "PRIVATE-" not in model.call_args.kwargs["user"]


@pytest.mark.parametrize("mode,limit,expected", [("single", 6, "single_reply_limit"), ("conversation", 1, "turn_limit")])
async def test_turn_limits_stop_followups(agent_setup, mode, limit, expected):
    start, model, _ = agent_setup
    await autonomy_settings.save({"mode": mode, "max_turns": limit}, 0)
    channel, address, _, first = await start()
    await customer_agent.process(first.id)
    second = await turn_for((await incoming(channel, address, "It is still not working, please help")).message_id)
    await customer_agent.process(second.id)
    assert model.await_count == 1 and (await reload_turn(second.id)).reason == expected


async def test_customer_handoff_persists_for_later_messages(agent_setup):
    start, model, _ = agent_setup
    channel, address, _, first = await start()
    human = await turn_for((await incoming(channel, address, "Please connect me to an operator")).message_id)
    await customer_agent.process(human.id)
    later = await turn_for((await incoming(channel, address, "Are you there? I am waiting")).message_id)
    await customer_agent.process(later.id)
    assert model.await_count == 0
    assert (await reload_turn(later.id)).reason == "customer_handoff"


async def test_delivery_failure_not_counted_as_delivered_and_retry_rechecks_takeover(agent_setup):
    start, model, pipes = agent_setup
    pipes["slack"].fail_on = 1
    _, _, tid, turn = await start("slack")
    await customer_agent.process(turn.id)
    turn = await reload_turn(turn.id)
    from src.dashboard.ai_diagnostics import overview
    row = next(r for r in (await overview())["rows"] if r["ticket_id"] == tid)
    assert row["status"] == "failed" and model.await_count == 1
    async with db_session() as session:
        session.add(Message(ticket_id=tid, sender_type="admin", sender_id=333, text="Human taking over"))
    await outbound.retry(turn.outbound_job_id)
    assert (await reload_turn(turn.id)).reason == "human_takeover"
    assert not pipes["slack"].texts


async def test_shadow_never_delivers_even_if_changed_off_mid_generation(agent_setup):
    start, model, _ = agent_setup
    await settings_service.set_ai_shadow_mode(True, updated_by=0)
    _, _, _, turn = await start()
    async def change(**kwargs):
        await settings_service.set_ai_shadow_mode(False, updated_by=0)
        return LLMResponse(text="SOURCE: none\nRestart the application.", model="offline-test")
    model.side_effect = change
    await customer_agent.process(turn.id)
    row = await reload_turn(turn.id)
    assert row.reason == "context_changed" and row.outbound_job_id is None
    from src.database.models import AICall
    async with db_session() as session:
        assert (await session.get(AICall, row.ai_call_id)).shadow is True


@pytest.mark.parametrize("values", [{"timeout_seconds": 0}, {"max_turns": 21}, {"max_output_tokens": 99999},
    {"instructions": "x" * 4001}, {"mode": "unlimited"}, {"allow_general_guidance": "false"},
    {"timeout_seconds": float('inf')}])
def test_invalid_policy_rejected(values):
    with pytest.raises(ValueError):
        autonomy_settings.validate(values)


@pytest.mark.parametrize("raw", ["**ACTION: clarify**\nSOURCE: none\nWhich network are you using?",
    "SOURCE: none\nACTION: clarify\nWhich network are you using?",
    "ACTION: clarify\n\nSOURCE: none\n\nWhich network are you using?"])
def test_control_markers_never_reach_customer(raw):
    assert deflect.split_reply(raw) == ("clarify", None, "Which network are you using?")


async def test_clarification_has_only_handoff_choice(agent_setup):
    start, model, pipes = agent_setup
    _, _, _, turn = await start("telegram")
    model.return_value = LLMResponse(text="ACTION: clarify\nSOURCE: none\nWhich network are you using?", model="offline-test")
    await customer_agent.process(turn.id)
    assert pipes['telegram'].offered[0][0].payload.startswith('defl:no:')
    assert len(pipes['telegram'].offered[0]) == 1


async def test_missing_configuration_and_trial_are_visible(agent_setup, monkeypatch):
    start, model, _ = agent_setup
    _, _, _, turn = await start()
    monkeypatch.setattr(deflect, 'get_client', AsyncMock(return_value=None))
    await customer_agent.process(turn.id)
    assert (await reload_turn(turn.id)).reason == 'not_configured'
    monkeypatch.setattr(deflect, 'get_client', AsyncMock(return_value=SimpleNamespace(complete=model)))
    await settings_service.set_ai_shadow_mode(True, updated_by=0)
    _, _, _, shadow = await start()
    await customer_agent.process(shadow.id)
    shadow = await reload_turn(shadow.id)
    assert shadow.status == 'shadow' and shadow.outbound_job_id is None


async def test_customer_rejection_stops_next_turn(agent_setup):
    from src.services.choice_service import apply_deflection_verdict
    start, model, _ = agent_setup
    channel, address, tid, turn = await start()
    await customer_agent.process(turn.id)
    turn = await reload_turn(turn.id)
    async with db_session() as session:
        uid = (await session.get(User, (await session.get(Ticket, tid)).user_id)).tg_id
    assert (await apply_deflection_verdict(ticket_id=tid, ai_call_id=turn.ai_call_id, user_tg_id=uid, accepted=False)).ok
    follow = await turn_for((await incoming(channel, address, 'It is still not working')).message_id)
    await customer_agent.process(follow.id)
    assert model.await_count == 1 and (await reload_turn(follow.id)).reason == 'customer_handoff'


async def test_restart_recovers_interrupted_generation_without_repeating_model_call(agent_setup):
    start, model, _ = agent_setup
    _, _, _, turn = await start()
    async with db_session(write=True) as session:
        row = await session.get(AIAutoTurn, turn.id)
        row.status = 'generating'
        row.started_at = datetime.utcnow() - timedelta(minutes=6)
    await customer_agent.run_once()
    await customer_agent.run_once()
    row = await reload_turn(turn.id)
    assert row.status == 'failed' and row.reason == 'interrupted'
    assert model.await_count == 0 and row.finished_at is not None


async def test_disabled_intake_does_not_replay_old_messages_when_enabled(agent_setup):
    start, model, _ = agent_setup
    channel, address, _, first = await start()
    await customer_agent.process(first.id)
    await settings_service.set_ai_deflection_enabled(False, updated_by=0)
    ignored = await incoming(channel, address, 'Message received while automatic replies are off')
    async with db_session() as session:
        assert (await session.execute(select(AIAutoTurn.id).where(
            AIAutoTurn.message_id == ignored.message_id))).scalar_one_or_none() is None
    await settings_service.set_ai_deflection_enabled(True, updated_by=0)
    await customer_agent.run_once()
    assert model.await_count == 1


async def test_policy_ui_roundtrip_validation_csrf_and_permissions(agent_setup):
    from fastapi.testclient import TestClient
    from src.dashboard.app import app
    from src.services import admin_user_service
    from tests.test_workspace_admin import client, post
    from tests.test_shadow_mode import _login
    with TestClient(app) as c:
        token = _login(c)
        page = c.get('/manage/ai')
        assert page.status_code == 200 and 'name="history_messages"' in page.text
        values = {**asdict(autonomy_settings.CustomerPolicy()), 'mode': 'conversation',
                  'instructions': 'Ask for the product version first.', 'timeout_seconds': 90,
                  'allow_clarification': '1', 'allow_general_guidance': '1'}
        assert c.post('/manage/ai/customer-policy', data=values, follow_redirects=False).status_code == 403
        assert post(c, '/manage/ai/customer-policy', values).status_code == 303
        saved = await autonomy_settings.load()
        assert saved.timeout_seconds == 90 and saved.allow_general_guidance
        assert 'value="90"' in c.get('/manage/ai').text
        response = post(c, '/manage/ai/customer-policy', {**values, 'max_turns': 999})
        assert 'ai_policy_invalid' in response.headers['location']
        assert await autonomy_settings.load() == saved
        assert c.get('/ai').status_code == 200
    actor = await admin_user_service.create_admin(email=f'ai-view-{next_tg_id()}@example.test', password='offline-password', role='viewer')
    with client(actor) as c:
        assert post(c, '/manage/ai/customer-policy', values).status_code == 403
        assert c.get('/manage/ai').status_code == 403
    async with db_session() as session:
        await session.delete(await session.get(type(actor), actor.id))
