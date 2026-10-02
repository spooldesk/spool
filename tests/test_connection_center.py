"""Connection diagnostics run only on isolated DBs and mocked providers."""
import asyncio
import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import delete, select

from src.database.db import db_session
from src.database.delivery_models import OutboundJob, WorkerLease
from src.database.inbound_models import InboundEvent
from src.database.models import AppSetting, Message, Ticket
from src.services import admin_user_service, connection_center as center, integrations, integration_checks as checks
from src.services.connection_urls import public_origin
from tests.conftest import next_tg_id
from tests.test_workspace_admin import workspace, client, post, customer


@pytest.fixture(autouse=True)
async def clean_diagnostics(initialized_db):
    keys = ['integration_check_' + c for c in integrations.FIELDS] + ['widget_public_url']
    async with db_session(write=True) as session:
        original_tickets = list((await session.execute(select(Ticket.id))).scalars())
        original_events = list((await session.execute(select(InboundEvent.id))).scalars())
        old = dict((await session.execute(select(AppSetting.key, AppSetting.value).where(AppSetting.key.in_(keys)))).all())
        await session.execute(delete(AppSetting).where(AppSetting.key.in_(keys)))
        await session.execute(delete(WorkerLease).where(WorkerLease.name.like('connection-check-%')))
    yield
    async with db_session(write=True) as session:
        await session.execute(delete(InboundEvent).where(InboundEvent.id.not_in(original_events)))
        await session.execute(delete(Ticket).where(Ticket.id.not_in(original_tickets)))
        await session.execute(delete(AppSetting).where(AppSetting.key.in_(keys)))
        session.add_all(AppSetting(key=k, value=v) for k, v in old.items())
        await session.execute(delete(WorkerLease).where(WorkerLease.name.like('connection-check-%')))


async def save_slack(owner, **extra):
    await integrations.save('slack', {'bot_token': 'xoxb-test-only',
        'signing_secret': 'synthetic-signing-secret', **extra}, enabled=True, actor_id=owner.id)


@pytest.mark.parametrize('url', ['http://localhost:8011', 'https://127.0.0.1', 'https://[::1]',
    'https://10.1.2.3', 'https://169.254.169.254', 'https://localhost', 'https://service.local',
    'https://user:password@example.com', 'https://example.com/slack/events',
    'https://example.com?secret=oops', 'https://example.com#oops', 'https://example.com:0',
    'https://example.com:99999', 'https://example.com\\@localhost', 'https://foo..com',
    'https://127.1', 'https://0177.0.0.1', 'https://0x7f.0.0.1'])
def test_public_origin_rejects_misleading_or_sensitive_urls(url):
    with pytest.raises(ValueError, match='connection_public_invalid'):
        public_origin(url)


def test_public_origin_normalizes_only_an_origin():
    assert public_origin(' https://Example.COM:443/ ') == 'https://example.com'
    assert public_origin('https://example.com:8443') == 'https://example.com:8443'
    assert public_origin('') == ''


async def test_owner_page_never_infers_slack_or_widget_url_from_host(workspace, monkeypatch):
    probe = AsyncMock()
    monkeypatch.setattr(checks, 'probe', probe)
    await save_slack(workspace)
    with client(workspace) as c:
        page = c.get('/manage/integrations', headers={'host': 'attacker.example.com'})
        assert page.status_code == 200
        assert 'attacker.example.com/slack/events' not in page.text
        assert 'http://testserver/slack/events' not in page.text
        assert 'synthetic-signing-secret' not in page.text
        assert 'xoxb-test-only' not in page.text
        assert 'Socket Mode' in page.text and '405' in page.text
        assert 'Access has not been checked.' in page.text
        assert 'data-connection-check' in page.text
    probe.assert_not_called()


async def test_public_urls_persist_separately_and_reject_bad_save_atomically(workspace):
    await save_slack(workspace, public_url='https://slack.example.com/')
    await integrations.save_widget({'widget_enabled': '1', 'widget_public_url': 'https://chat.example.com'}, actor_id=workspace.id)
    with client(workspace) as c:
        page = c.get('/manage/integrations')
        assert 'https://slack.example.com/slack/events' in page.text
        assert 'https://chat.example.com/widget/embed.js' in page.text
        assert 'https://slack.example.com/widget/embed.js' not in page.text
        response = post(c, '/manage/integrations/slack', {'enabled': '1', 'public_url': 'http://127.0.0.1:8011'})
        assert 'connection_public_invalid' in response.headers['location']
    assert (await integrations.state('slack'))['values']['public_url'] == 'https://slack.example.com'


@pytest.mark.parametrize('role', ['admin', 'agent', 'viewer'])
async def test_checks_are_owner_only_and_csrf_protected(workspace, monkeypatch, role):
    probe = AsyncMock()
    monkeypatch.setattr(checks, 'probe', probe)
    actor = await admin_user_service.create_admin(email=f'check-{next_tg_id()}@example.test', password='synthetic-password', role=role)
    with client(actor) as c:
        assert post(c, '/manage/integrations/slack/check').status_code == 403
    with pytest.raises(ValueError, match='e_forbidden_section'):
        await center.check_connection('slack', actor_id=actor.id)
    with client(workspace) as c:
        assert c.post('/manage/integrations/slack/check', follow_redirects=False).status_code == 403
    probe.assert_not_called()


async def test_result_persists_without_secrets_and_becomes_stale_on_config_change(workspace, monkeypatch):
    await save_slack(workspace)
    before_incoming = (await center.snapshot())['evidence']['slack']['incoming_at']
    fake = AsyncMock(return_value=[checks.result('slack', 'ok', 'authentication_ok')])
    monkeypatch.setattr(checks, 'probe', fake)
    with client(workspace) as c:
        response = post(c, '/manage/integrations/slack/check')
        assert 'connection_checked' in response.headers['location']
    snap = await center.snapshot()
    assert snap['evidence']['slack']['check']['status'] == 'ok'
    assert not snap['evidence']['slack']['check']['stale']
    # An authentication probe cannot manufacture incoming customer traffic.
    assert snap['evidence']['slack']['incoming_at'] == before_incoming
    assert not next(s['done'] for s in snap['evidence']['slack']['steps'] if s['key'] == 'incoming')
    async with db_session() as session:
        row = await session.get(AppSetting, 'integration_check_slack')
        assert 'xoxb-' not in row.value and 'synthetic-signing-secret' not in row.value
        assert row.updated_by == workspace.id
    with pytest.raises(ValueError, match='connection_check_recent'):
        await center.check_connection('slack', actor_id=workspace.id)
    assert fake.await_count == 1
    await integrations.save('slack', {'public_url': 'https://new.example.com'}, enabled=True, actor_id=workspace.id)
    assert (await center.snapshot())['evidence']['slack']['check']['stale']


async def test_check_drops_result_if_config_changes_during_io(workspace, monkeypatch):
    await save_slack(workspace)
    async def changed(*args):
        await integrations.save('slack', {}, enabled=False, actor_id=workspace.id)
        return [checks.result('slack', 'ok', 'authentication_ok')]
    monkeypatch.setattr(checks, 'probe', changed)
    assert await center.check_connection('slack', actor_id=workspace.id) == 'connection_check_changed'
    async with db_session() as session:
        assert await session.get(AppSetting, 'integration_check_slack') is None


async def test_concurrent_check_uses_one_provider_call(workspace, monkeypatch):
    await save_slack(workspace)
    entered, release = asyncio.Event(), asyncio.Event()
    async def wait(*args):
        entered.set()
        await release.wait()
        return [checks.result('slack', 'ok', 'authentication_ok')]
    monkeypatch.setattr(checks, 'probe', wait)
    first = asyncio.create_task(center.check_connection('slack', actor_id=workspace.id))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        with pytest.raises(ValueError, match='connection_check_busy'):
            await center.check_connection('slack', actor_id=workspace.id)
    finally:
        release.set()
        await first


async def test_expired_check_is_not_current_evidence(workspace, monkeypatch):
    await save_slack(workspace)
    monkeypatch.setattr(checks, 'probe', AsyncMock(return_value=[checks.result('slack', 'ok', 'authentication_ok')]))
    await center.check_connection('slack', actor_id=workspace.id)
    async with db_session(write=True) as session:
        row = await session.get(AppSetting, 'integration_check_slack')
        data = json.loads(row.value)
        data['checked_at'] = (datetime.utcnow() - timedelta(days=2)).isoformat()
        row.value = json.dumps(data)
    evidence = (await center.snapshot())['evidence']['slack']
    assert evidence['check']['stale']
    assert not next(s['done'] for s in evidence['steps'] if s['key'] == 'authentication')


async def test_diagnostic_problem_list_uses_states_and_never_payloads(workspace):
    await save_slack(workspace)
    u = await customer('slack')
    now, before = datetime.utcnow(), datetime.utcnow() - timedelta(hours=1)
    ids = {}
    async with db_session(write=True) as session:
        ticket = Ticket(user_id=u.id, category='Other', status='active')
        session.add(ticket)
        await session.flush()
        for state in ('failed', 'uncertain', 'cancelled', 'delivered', 'sending', 'retry', 'pending'):
            message = Message(ticket_id=ticket.id, sender_type='admin', sender_id=0, text='PRIVATE BODY', delivery_status=state)
            session.add(message)
            await session.flush()
            job = OutboundJob(message_id=message.id, channel='slack', address='PRIVATE ADDRESS',
                status=state, created_at=before, started_at=before,
                next_attempt_at=now + timedelta(hours=1) if state == 'retry' else None,
                delivered_at=now if state == 'delivered' else None, last_error='PRIVATE PROVIDER ERROR')
            session.add(job)
            await session.flush()
            ids[state] = job.id
        incoming = Message(ticket_id=ticket.id, sender_type='user', sender_id=0, text='PRIVATE INCOMING', created_at=now)
        session.add(incoming)
        for status, error in [('pending', 'PRIVATE ERROR'), ('ignored', None), ('processed', None)]:
            session.add(InboundEvent(channel='slack', source_key=f'center-{next_tg_id()}', payload=b'PRIVATE PAYLOAD',
                payload_size=15, status=status, created_at=now, last_error=error, attempts=1,
                next_attempt_at=now + timedelta(minutes=5)))
    snap = await center.snapshot()
    problems = {p['id'] for p in snap['outgoing_problems']}
    assert all(ids[s] in problems for s in ('failed', 'uncertain', 'sending', 'pending'))
    assert all(ids[s] not in problems for s in ('retry', 'delivered', 'cancelled'))
    assert snap['incoming_problem_count'] >= 1
    evidence = snap['evidence']['slack']
    assert evidence['incoming_at'] >= now and evidence['outgoing_at'] >= now
    assert 'PRIVATE' not in json.dumps(snap, default=str)
    with client(workspace) as c:
        page = c.get('/manage/integrations')
        assert page.status_code == 200 and 'Delivery unknown' in page.text
        assert 'PRIVATE' not in page.text
        assert f'/tickets/{ticket.id}' in page.text
        assert '/retry' not in page.text


@pytest.mark.parametrize('scopes,code,status', [
    (','.join(checks.SLACK_REQUIRED + checks.SLACK_OPTIONAL), 'authentication_ok', 'ok'),
    (','.join(checks.SLACK_REQUIRED), 'slack_optional_scopes', 'warning'),
    ('chat:write', 'slack_scopes_missing', 'error'), ('', 'slack_scopes_unknown', 'warning')])
async def test_slack_probe_scopes_without_customer_messages(monkeypatch, scopes, code, status):
    class FakeClient:
        def __init__(self, **kwargs):
            assert kwargs['follow_redirects'] is False
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, url, **kwargs):
            assert url == 'https://slack.com/api/auth.test'
            assert 'data' not in kwargs and 'json' not in kwargs
            return httpx.Response(200, json={'ok': True, 'private': 'NEVER RETURN'}, headers={'x-oauth-scopes': scopes})
    monkeypatch.setattr(checks.httpx, 'AsyncClient', FakeClient)
    response = await checks.probe('slack', {'bot_token': 'synthetic'})
    assert response[0]['code'] == code and response[0]['status'] == status
    assert 'NEVER RETURN' not in str(response)


async def test_provider_exception_is_not_returned_or_stored(monkeypatch):
    async def fail(*args): raise RuntimeError('SECRET token and private customer text')
    monkeypatch.setattr(checks, '_slack', fail)
    result = await checks.probe('slack', {'bot_token': 'synthetic'})
    assert result == [checks.result('slack', 'error', 'connection_failed')]
    assert 'SECRET' not in str(result)


@pytest.mark.parametrize('webhook,code', [('', 'authentication_ok'),
    ('https://private.example.com/secret', 'telegram_webhook_active')])
async def test_telegram_probe_does_not_consume_updates_or_change_webhook(monkeypatch, webhook, code):
    called = []
    class Response:
        status = 200
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def json(self): return {'ok': True, 'result': {'url': webhook}}
    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        def get(self, url, **kwargs):
            assert kwargs['allow_redirects'] is False
            called.append(url.rsplit('/', 1)[-1])
            return Response()
    monkeypatch.setattr(checks.aiohttp, 'ClientSession', Client)
    response = await checks.probe('telegram', {'bot_token': 'synthetic'})
    assert called == ['getMe', 'getWebhookInfo']
    assert response[0]['code'] == code
    assert 'private.example.com' not in str(response)


def test_mail_probe_keeps_tls_validation_and_never_logs_in_after_tls_failure(monkeypatch):
    class SMTP:
        def __init__(self, *args, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def ehlo(self): pass
        def starttls(self, context):
            assert context.check_hostname
            raise checks.ssl.SSLError('private server certificate detail')
        def login(self, *args): raise AssertionError('must not authenticate without TLS')
    monkeypatch.setattr(checks.smtplib, 'SMTP', SMTP)
    assert checks._mail_part('smtp', {'address': 'test@example.com', 'password': 'synthetic',
        'smtp_host': 'smtp.example.com', 'smtp_port': '587'}) == checks.result('smtp', 'error', 'tls_failed')


def test_mail_checks_do_not_fetch_mark_or_send(monkeypatch):
    called = []
    class IMAP:
        def __init__(self, host, port, **kw): assert kw['ssl_context'] and kw['timeout'] <= 10
        def login(self, login, password): called.append('imap login')
        def select(self, mailbox, readonly):
            assert mailbox == 'INBOX' and readonly is True
            called.append('readonly select')
            return 'OK', []
        def logout(self): called.append('logout')
    class SMTP:
        def __init__(self, host, port, **kw): assert kw['timeout'] <= 10
        def __enter__(self): return self
        def __exit__(self, *args): called.append('quit')
        def ehlo(self): pass
        def starttls(self, context): assert context.check_hostname; called.append('starttls')
        def login(self, login, password): called.append('smtp login')
    monkeypatch.setattr(checks.imaplib, 'IMAP4_SSL', IMAP)
    monkeypatch.setattr(checks.smtplib, 'SMTP', SMTP)
    values = {'address': 'test@example.com', 'password': 'synthetic', 'imap_host': 'imap.example.com',
        'smtp_host': 'smtp.example.com', 'imap_port': '993', 'smtp_port': '587'}
    assert checks._mail_part('imap', values)['status'] == 'ok'
    assert checks._mail_part('smtp', values)['status'] == 'ok'
    assert called == ['imap login', 'readonly select', 'logout', 'starttls', 'smtp login', 'quit']


def test_localization_keys_match():
    from src.dashboard.connection_i18n import EN, RU
    assert EN.keys() == RU.keys()
    assert all(EN.values()) and all(RU.values())
