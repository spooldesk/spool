"""Owner boundaries, custom roles, account grouping and private AI context."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select
from src.dashboard import auth
from src.dashboard.app import app, TEMPLATES
from src.dashboard.i18n import strings
from src.database.db import db_session
from src.database.models import AdminUser, AppSetting, Ticket, Message, User
from src.database.workspace_models import CustomRole
from src.services import admin_user_service, permissions, integrations, customer_profiles
from src.ai import company_context, knowledge
from src.config import channel_values, load_settings
from tests.conftest import next_tg_id, RecordingChannel


@pytest.fixture
async def workspace(initialized_db, monkeypatch):
    monkeypatch.setitem(TEMPLATES.env.globals, 'T', strings('en'))
    keys = ['integration_telegram', 'integration_slack', 'integration_email', company_context.KEY,
            'widget_identity_secret', 'widget_enabled', 'widget_origins', 'widget_greeting', 'widget_ask_identity']
    async with db_session(write=True) as session:
        old = dict((await session.execute(select(AppSetting.key, AppSetting.value).where(AppSetting.key.in_(keys)))).all())
        await session.execute(delete(AppSetting).where(AppSetting.key.in_(keys)))
    channel_values.reset()
    from src.services.settings_service import get_or_create_secret_key
    auth.set_secret_key(await get_or_create_secret_key())
    owner = await admin_user_service.create_admin(email=f'workspace-{next_tg_id()}@example.test', password='workspace-password', role='owner')
    yield owner
    async with db_session(write=True) as session:
        await session.execute(delete(AppSetting).where(AppSetting.key.in_(keys)))
        session.add_all(AppSetting(key=k, value=v) for k, v in old.items())
    channel_values.reset()


def client(actor):
    c = TestClient(app)
    c.cookies.set(auth.SESSION_COOKIE, auth.make_session_cookie(actor.id, actor.session_version))
    c.get('/login')
    return c


def post(c, path, data=None, **kwargs):
    return c.post(path, data={**(data or {}), 'csrf_token': c.cookies.get('supbot_csrf')}, follow_redirects=False, **kwargs)


async def customer(channel='email'):
    from src.channels.identity import create_customer
    ident = next_tg_id()
    address = str(ident) if channel == 'telegram' else f'person-{ident}@example.test'
    async with db_session(write=True) as session:
        return await create_customer(session, channel=channel, address=address)


async def test_contact_addresses_and_widget_names_are_channel_neutral(workspace):
    from src.channels.identity import create_customer, display_address
    from src.dashboard import queries
    address = f'customer-with-a-long-address-{next_tg_id()}@example.test'
    async with db_session(write=True) as session:
        email = await create_customer(session, channel='email', address=address)
        widget = await create_customer(session, channel='widget', address=f'widget-{next_tg_id()}', username='Marta Example')
        session.add(Ticket(user_id=widget.id, status='active', category='Other', conversation_key=''))
    assert display_address(email) == address
    assert customer_profiles.label(widget) == 'Marta Example'
    detail = await queries.user_detail(widget.id)
    assert not detail['is_verified']
    ticket = detail['tickets'][0]['id']
    assert (await queries.ticket_detail(ticket))['user_label'] == 'Marta Example'


@pytest.mark.parametrize('role', ['admin', 'agent', 'viewer'])
async def test_integration_and_role_routes_are_owner_only(workspace, role):
    actor = await admin_user_service.create_admin(email=f'role-{next_tg_id()}@example.test', password='workspace-password', role=role)
    with client(actor) as c:
        for path in ['/manage/integrations', '/manage/roles']:
            assert c.get(path).status_code == 403
        assert post(c, '/manage/integrations/email').status_code == 403
        assert post(c, '/manage/roles/save', {'name':'forbidden','tickets':'edit'}).status_code == 403
        assert post(c, '/manage/settings/widget').status_code == 403


async def test_custom_sections_enforce_read_edit_hidden_and_navigation(workspace):
    key = await permissions.save(workspace.id, f'Triage {next_tg_id()}', {'users':'view', 'tickets':'edit'})
    actor = await admin_user_service.create_admin(email=f'custom-{next_tg_id()}@example.test', password='workspace-password', role=key)
    u = await customer()
    with client(actor) as c:
        page = c.get('/users')
        assert page.status_code == 200 and 'data-readonly="true"' in page.text
        assert 'href="/manage/integrations"' not in page.text
        assert 'href="/manage/roles"' not in page.text
        assert c.get('/tickets').status_code == 200
        assert c.get('/manage/settings').status_code == 403
        assert post(c, f'/users/{u.id}/block').status_code == 403
        assert post(c, '/manage/roles/save', {'name':'bypass','integrations':'edit'}).status_code == 403
        assert post(c, '/me', {}).status_code != 403


async def test_role_edit_revokes_sessions_and_delete_refuses_assignees(workspace):
    key = await permissions.save(workspace.id, f'Research {next_tg_id()}', {'users':'view'})
    actor = await admin_user_service.create_admin(email=f'assigned-{next_tg_id()}@example.test', password='workspace-password', role=key)
    cookie = auth.make_session_cookie(actor.id, actor.session_version)
    with pytest.raises(ValueError, match='role_in_use'):
        await permissions.remove(workspace.id, key)
    await permissions.save(workspace.id, f'Research revised {next_tg_id()}', {'users':'edit'}, key)
    assert not await auth.is_current_session(cookie)
    with pytest.raises(ValueError, match='role_permissions_invalid'):
        await permissions.save(workspace.id, 'Superuser', {'integrations':'edit'})


async def test_owner_dependency_alone_authenticates_before_checking_permissions(workspace):
    from starlette.requests import Request
    from fastapi import HTTPException
    def request(actor=None):
        headers = [] if actor is None else [(b'cookie',
            f'{auth.SESSION_COOKIE}={auth.make_session_cookie(actor.id, actor.session_version)}'.encode())]
        return Request({'type':'http', 'method':'GET', 'path':'/manage/integrations',
                        'query_string':b'', 'headers':headers})
    with pytest.raises(HTTPException) as missing:
        await auth.require_role('owner')(request())
    assert missing.value.status_code in {303, 307, 401}
    key = await permissions.save(workspace.id, f'Reader {next_tg_id()}', {'tickets':'view'})
    reader = await admin_user_service.create_admin(email=f'alone-{next_tg_id()}@example.test',
        password='workspace-password', role=key)
    with pytest.raises(HTTPException) as denied:
        await auth.require_role('owner')(request(reader))
    assert denied.value.status_code == 403
    await auth.require_role('owner')(request(workspace))


async def test_roles_apply_to_invites_and_login_landing(workspace):
    from src.services import invitation_service
    key = await permissions.save(workspace.id, f'Customer view {next_tg_id()}', {'users':'view'})
    email = f'invited-{next_tg_id()}@example.test'
    token = await invitation_service.issue(actor_id=workspace.id, email=email, name='Reader', role=key)
    with pytest.raises(ValueError, match='role_in_use'):
        await permissions.remove(workspace.id, key)
    actor = await invitation_service.accept(token, 'workspace-password')
    assert actor.role == key
    with client(actor) as c:
        response = post(c, '/login', {'email':email,'password':'workspace-password','next':'/tickets'})
        assert response.headers['location'] == '/'
        assert c.get('/').url.path == '/users'


async def test_connection_secrets_are_encrypted_kept_on_blank_and_disabled_overrides_env(workspace, monkeypatch):
    secret = '1234567:' + 'A'*30
    await integrations.save('telegram', {'bot_token': secret}, enabled=True, actor_id=workspace.id)
    state = await integrations.state('telegram')
    assert state['configured'] and 'bot_token' not in state['values']
    async with db_session() as session:
        assert secret not in (await session.get(AppSetting, 'integration_telegram')).value
    await integrations.save('telegram', {'bot_token':'', 'admin_chat_id':'0'}, enabled=True, actor_id=workspace.id)
    await integrations.refresh()
    assert load_settings().bot_token == secret and load_settings().has_telegram
    with client(workspace) as c:
        page = c.get('/manage/integrations')
        assert page.status_code == 200 and secret not in page.text
        assert 'value="0"' in page.text and 'value="993"' in page.text
    monkeypatch.setenv('BOT_TOKEN', secret)
    await integrations.save('telegram', {}, enabled=False, actor_id=workspace.id)
    await integrations.refresh()
    assert not load_settings().has_telegram


async def test_invalid_connection_save_is_atomic_and_widget_secret_is_private(workspace):
    with pytest.raises(ValueError):
        await integrations.save('email', {'address':'bad','imap_port':'0'}, enabled=True, actor_id=workspace.id)
    async with db_session() as session:
        assert await session.get(AppSetting, 'integration_email') is None
    secret = 'widget-' + 'S'*40
    await integrations.save_widget({'widget_enabled':'1','widget_origins':'https://example.test','widget_identity_secret':secret}, actor_id=workspace.id)
    from src.services.settings_service import get_widget_identity_secret, get_widget_origins
    assert await get_widget_identity_secret() == secret
    with pytest.raises(ValueError):
        await integrations.save_widget({'widget_origins':'https://example.test/path','widget_enabled':'0'}, actor_id=workspace.id)
    assert await get_widget_origins() == ['https://example.test']
    with client(workspace) as c:
        assert secret not in c.get('/manage/integrations').text
        assert secret not in c.get('/manage/settings').text
        assert post(c, '/manage/integrations/widget', {'widget_enabled':'1','widget_origins':'https://example.test'}).status_code == 303
    assert await get_widget_identity_secret() == secret


async def test_grouping_unifies_history_without_rerouting_and_unlinks(workspace):
    from src.dashboard import queries
    a, b = await customer(), await customer('telegram')
    async with db_session(write=True) as session:
        session.add_all([Ticket(user_id=a.id, category='Other', status='active'), Ticket(user_id=b.id, category='Other', status='closed')])
    with pytest.raises(ValueError, match='contact_link_confirm'):
        await customer_profiles.link(a.id, 'telegram', str(b.tg_id), workspace.id)
    await customer_profiles.link(a.id, 'telegram', str(b.tg_id), workspace.id, confirmed=True)
    detail = await queries.user_detail(a.id)
    assert detail['total'] == 2 and {t['channel'] for t in detail['tickets']} == {'email','telegram'}
    listing = await queries.users_with_ticket_stats()
    grouped = next(r for r in listing if r['user_db_id'] == a.id)
    assert grouped['total'] == 2 and not any(r['user_db_id'] == b.id for r in listing)
    async with db_session() as session:
        assert not (await session.get(User, b.id)).identity_verified_fields
        assert set((await session.execute(select(Ticket.user_id).where(Ticket.user_id.in_([a.id,b.id])))).scalars()) == {a.id,b.id}
    with client(workspace) as c:
        page = c.get(f'/users/{a.id}')
        assert page.status_code == 200 and 'name="account_id"' in page.text
        assert 'pending_work' not in page.text and 'tg_id:' not in page.text
    await customer_profiles.unlink(a.id, b.id, workspace.id)
    assert (await queries.user_detail(a.id))['total'] == 1


async def test_message_channel_selection_is_scoped_and_delivery_is_idempotent(workspace):
    import src.channels as channels
    from src.services import outbound
    a, outsider = await customer(), await customer('telegram')
    fake = RecordingChannel(name='email')
    channels.register(fake)
    try:
        with pytest.raises(ValueError):
            await customer_profiles.send(a.id, outsider.id, 'Private reply', workspace.id, request_key='a'*32)
        first = await customer_profiles.send(a.id, a.id, 'Private reply', workspace.id, request_key='b'*32)
        second = await customer_profiles.send(a.id, a.id, 'Private reply', workspace.id, request_key='b'*32)
        assert first == second
        assert (await outbound.deliver(first[1])).ok
        assert (await outbound.deliver(second[1])).ok
        assert fake.texts == [(a.channel_user_id, 'Private reply')]
    finally:
        channels.reset()


async def test_document_upload_private_preview_and_company_only_answer(workspace, monkeypatch):
    with client(workspace) as c:
        result = post(c, '/manage/ai/company', {'enabled':'1'}, files={'document':('rules.txt', b'Refund window is exactly fourteen days.', 'text/plain')})
        assert result.status_code == 303 and 'sent=saved' in result.headers['location']
        assert 'Refund window' in c.get('/manage/ai').text
    company = await company_context.load()
    assert company.enabled
    async with db_session() as session:
        assert 'Refund window' not in (await session.get(AppSetting, company_context.KEY)).value
    u = await customer()
    async with db_session(write=True) as session:
        ticket = Ticket(user_id=u.id, category='Other', status='active')
        session.add(ticket)
        await session.flush()
        tid = ticket.id
    fake = SimpleNamespace(complete=AsyncMock(return_value=SimpleNamespace(text=json.dumps({'answer':'Fourteen days.','source_ids':[0]}), model='fake', tokens_in=10, tokens_out=5)))
    monkeypatch.setattr(knowledge, 'get_client', AsyncMock(return_value=fake))
    monkeypatch.setattr(knowledge.policy, 'ticket_allowed', AsyncMock(return_value=True))
    monkeypatch.setattr(knowledge.article_service, 'candidates_for', AsyncMock(return_value=[]))
    result = await knowledge.suggest_from_knowledge(tid, 'What is the refund window?')
    assert result.suggested_text == 'Fourteen days.' and result.sources[0]['id'] == 0
    assert 'Refund window' in fake.complete.call_args.kwargs['user']
    async def replace(**kwargs):
        await company_context.save(workspace.id, enabled=False)
        return SimpleNamespace(text=json.dumps({'answer':'Fourteen days.','source_ids':[0]}), model='fake', tokens_in=10, tokens_out=5)
    fake.complete.side_effect = replace
    assert (await knowledge.suggest_from_knowledge(tid, 'What is the refund window?')).suggested_text is None
