"""Private history, file isolation and content-based analysis with fake providers."""
import json
from io import BytesIO
import zipfile

import pytest
from sqlalchemy import select

from tests.test_assistant import environment, operator, ticket, client, post, fake_model
from src.ai.assistant_access import authorize
from src.ai.assistant_schema import ToolRequest
from src.ai.assistant_tools import read_tool
from src.ai.client import LLMUnavailable
from src.database.assistant_models import AssistantConversation, AssistantTurn, AssistantUpload
from src.database.db import db_session
from src.database.models import Message, Ticket
from src.services import settings_service


def upload(c, name='facts.txt', content=b'Fictional support policy: response within 2 hours.'):
    return c.post('/assistant/uploads', params={'filename':name}, content=content,
        headers={'X-CSRF-Token':c.cookies.get('supbot_csrf'), 'Content-Type':'application/octet-stream'})


async def test_analysis_reads_public_ticket_content_and_reports_coverage(environment):
    row = await ticket(text='OVERVIEWUNIQUE: VPN disconnects every 15 minutes', status='closed')
    async with db_session(write=True) as s:
        s.add(Message(ticket_id=row.id, sender_id=1, sender_type='admin', text='INTERNAL PRIVATE', is_internal=True))
        s.add(Message(ticket_id=row.id, sender_id=1, sender_type='admin', text='Fixed VPN profile', delivery_status='delivered'))
        s.add(Message(ticket_id=row.id, sender_id=1, sender_type='admin', text='FAILED PRIVATE', delivery_status='failed'))
    access = await authorize(environment.id, environment.session_version)
    data, refs = await read_tool(access, ToolRequest(tool='analyze_tickets', status='all', query='OVERVIEWUNIQUE'))
    assert data['analyzed_count'] == data['total_matching'] == 1
    assert 'VPN disconnects' in data['tickets'][0]['opening']
    assert data['tickets'][0]['latest_reply'] == 'Fixed VPN profile'
    assert 'PRIVATE' not in json.dumps(data)
    assert refs[0]['url'] == f'/tickets/{row.id}'
    hidden = await ticket(category='assistant-secret', text='OVERVIEWUNIQUE hidden')
    data, _ = await read_tool(access, ToolRequest(tool='analyze_tickets', status='all', ticket_ids=[hidden.id]))
    assert data['total_matching'] == 0


async def test_history_roundtrip_followup_and_idempotent_first_request(environment, monkeypatch):
    c = client(environment)
    fake = fake_model(monkeypatch, {'tools':[]}, 'Previous answer: case Atlas needs a VPN profile.')
    payload = {'message':'Remember the Atlas issue', 'request_id':'a'*32}
    first = post(c, '/assistant/chat', payload=payload)
    assert first.status_code == 200, first.text
    ident = first.json()['conversation_id']
    repeat = post(c, '/assistant/chat', payload=payload)
    assert repeat.status_code == 200 and repeat.json()['conversation_id'] == ident
    assert fake.complete.await_count == 2
    page = c.get('/assistant/conversations/'+ident).json()
    assert page['turns'][0]['question'] == payload['message']
    assert page['turns'][0]['result']['answer'].startswith('Previous answer')
    assert c.get('/assistant/conversations').json()['conversations'][0]['id'] == ident
    async with db_session() as s:
        stored = (await s.execute(select(AssistantTurn).where(AssistantTurn.conversation_id == ident))).scalar_one()
        assert 'Atlas' not in stored.payload and stored.payload.startswith('gAAAAA')
    fake = fake_model(monkeypatch, {'tools':[]})
    follow = post(c, '/assistant/chat', payload={'message':'What did we decide?', 'conversation_id':ident})
    assert follow.status_code == 200
    context = json.loads(fake.complete.call_args_list[-1].kwargs['user'])
    assert 'Atlas' in context['conversation_history'][0]['answer']


async def test_history_and_files_belong_only_to_the_operator(environment, monkeypatch):
    c = client(environment)
    file = upload(c).json()
    fake_model(monkeypatch, {'tools':[]})
    result = post(c, '/assistant/chat', payload={'message':'Read my file', 'attachment_ids':[file['id']]}).json()
    other = client(await operator('owner'))
    assert other.get('/assistant/conversations').json() == {'conversations':[]}
    assert other.get('/assistant/conversations/'+result['conversation_id']).status_code == 404
    assert other.get('/assistant/uploads/'+file['id']).status_code == 404
    assert post(other, '/assistant/chat', payload={'message':'Read', 'attachment_ids':[file['id']]}).status_code == 404
    assert post(other, '/assistant/chat', payload={'message':'Read', 'conversation_id':result['conversation_id']}).status_code == 404


async def test_uploaded_text_is_available_to_model_and_followup_but_not_other_chat(environment, monkeypatch):
    c = client(environment)
    attached = upload(c, 'support.csv', b'issue,count\nVPN,12').json()
    fake = fake_model(monkeypatch, {'tools':[]})
    result = post(c, '/assistant/chat', payload={'message':'Analyze the file', 'attachment_ids':[attached['id']]}).json()
    evidence = json.loads(fake.complete.call_args_list[-1].kwargs['user'])
    assert evidence['uploaded_documents'][0]['text'] == 'issue,count\nVPN,12'
    assert c.get('/assistant/uploads/'+attached['id']).json()['text'] == 'issue,count\nVPN,12'
    async with db_session() as s:
        assert 'VPN' not in (await s.get(AssistantUpload, attached['id'])).content
    fake = fake_model(monkeypatch, {'tools':[]})
    follow = post(c, '/assistant/chat', payload={'message':'Summarize again', 'conversation_id':result['conversation_id']})
    assert follow.status_code == 200
    assert json.loads(fake.complete.call_args_list[-1].kwargs['user'])['uploaded_documents'][0]['text'].endswith('VPN,12')
    assert post(c, '/assistant/chat', payload={'message':'Different chat', 'attachment_ids':[attached['id']]}).status_code == 404


async def test_changed_permissions_hide_history_and_changed_category_redacts_answer(environment, monkeypatch):
    row = await ticket(text='Previously visible issue')
    fake_model(monkeypatch, {'tools':[{'tool':'ticket','ticket_id':row.id}]}, 'Ticket evidence')
    c = client(environment)
    ident = post(c, '/assistant/chat', payload={'message':'Analyze'}).json()['conversation_id']
    fake_model(monkeypatch, {'tools':[]}, 'Derived from Ticket evidence')
    assert post(c, '/assistant/chat', payload={'message':'Explain again', 'conversation_id':ident}).status_code == 200
    async with db_session(write=True) as s:
        (await s.get(Ticket,row.id)).category = 'assistant-secret'
    page = c.get('/assistant/conversations/'+ident)
    assert page.status_code == 200 and page.json()['turns'][0]['status'] == 'redacted'
    assert page.json()['turns'][1]['status'] == 'redacted'
    assert 'Ticket evidence' not in page.text
    await settings_service.set_ai_blacklist(['assistant-secret','new-private'], updated_by=environment.id)
    assert c.get('/assistant/conversations/'+ident).status_code == 403
    assert c.get('/assistant/conversations').json()['conversations'] == []


async def test_request_id_collision_does_not_leak_another_operators_chat(environment, monkeypatch):
    fake_model(monkeypatch, {'tools':[]})
    payload = {'message':'Private question', 'request_id':'b'*32}
    assert post(client(environment), '/assistant/chat', payload=payload).status_code == 200
    other = client(await operator('owner'))
    response = post(other, '/assistant/chat', payload=payload)
    assert response.status_code == 409 and response.json()['code'] == 'assistant_busy'
    assert other.get('/assistant/conversations').json()['conversations'] == []


async def test_action_only_history_is_redacted_when_ticket_becomes_private(environment, monkeypatch):
    row = await ticket()
    fake_model(monkeypatch, {'tools':[], 'action':{'kind':'stage','ticket_id':row.id,'stage':'waiting'}})
    c = client(environment)
    result = post(c, '/assistant/chat', payload={'message':'Move to waiting'}).json()
    async with db_session(write=True) as s:
        (await s.get(Ticket,row.id)).category = 'assistant-secret'
    page = c.get('/assistant/conversations/'+result['conversation_id'])
    assert page.json()['turns'][0]['status'] == 'redacted'
    assert result['action']['id'] not in page.text


@pytest.mark.parametrize('name,content,code', [
    ('x.exe',b'hello',400), ('x.txt',b'\x00broken',400), ('x.txt',b'\xff',400),
    ('x.txt',b'a'*24001,400), ('x.txt',b'a'*(2*1024*1024+1),413), ('x.docx',b'bad zip',400)],
    ids=['unsupported-extension', 'binary-nul', 'invalid-utf8', 'text-too-long', 'body-too-large', 'invalid-docx'])
async def test_bad_uploads_are_rejected(environment, name, content, code):
    response = upload(client(environment), name, content)
    assert response.status_code == code


async def test_docx_is_read_as_text_without_external_entities(environment):
    c = client(environment)
    def document(xml):
        buf = BytesIO()
        with zipfile.ZipFile(buf,'w') as z: z.writestr('word/document.xml',xml)
        return buf.getvalue()
    good = b'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Company policy</w:t></w:r></w:p></w:body></w:document>'
    response = upload(c,'policy.docx',document(good))
    assert response.status_code == 200
    assert c.get('/assistant/uploads/'+response.json()['id']).json()['text'] == 'Company policy'
    assert upload(c,'bad.docx',document(b'<!DOCTYPE a SYSTEM "file:///private">'+good)).status_code == 400
    assert c.post('/assistant/uploads?filename=x.txt',content=b'hello').status_code == 403


async def test_delete_chat_removes_history_uploads_and_pending_proposal(environment, monkeypatch):
    c = client(environment)
    row = await ticket()
    file = upload(c).json()
    fake_model(monkeypatch, {'tools':[], 'action':{'kind':'stage','ticket_id':row.id,'stage':'waiting'}})
    result = post(c, '/assistant/chat', payload={'message':'Move to waiting', 'attachment_ids':[file['id']]}).json()
    response = c.delete('/assistant/conversations/'+result['conversation_id'],headers={'X-CSRF-Token':c.cookies.get('supbot_csrf')})
    assert response.status_code == 200
    assert c.get('/assistant/conversations/'+result['conversation_id']).status_code == 404
    assert c.get('/assistant/uploads/'+file['id']).status_code == 404
    assert post(c,'/assistant/actions/'+result['action']['id'],form={'decision':'confirm'}).status_code == 409


async def test_failed_request_remains_in_history_without_raw_provider_error(environment, monkeypatch):
    fake = fake_model(monkeypatch, {'tools':[]})
    fake.complete.side_effect = LLMUnavailable('secret-provider-body', code='auth')
    c = client(environment)
    response = post(c, '/assistant/chat', payload={'message':'My question'})
    assert response.status_code == 503
    page = c.get('/assistant/conversations/'+response.json()['conversation_id'])
    assert page.status_code == 200 and 'secret-provider-body' not in page.text
    assert page.json()['turns'][0]['question'] == 'My question'
    assert page.json()['turns'][0]['error'] == 'assistant_provider_auth'
