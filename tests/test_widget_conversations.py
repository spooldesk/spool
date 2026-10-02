"""Visitor mutations are isolated by session, ticket, revision and lifecycle."""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from src.dashboard.app import app
from src.dashboard.routes import widget
from src.database.db import db_session
from src.database.models import Message,MessageRevision,Ticket
from src.services import settings_service


@pytest.fixture
async def client(initialized_db):
    widget._hits.clear()
    await settings_service.set_widget_enabled(True,updated_by=0)
    await settings_service.set_widget_origins('',updated_by=0)
    with TestClient(app,base_url='http://testserver') as c: yield c
    widget._hits.clear()
    await settings_service.set_widget_enabled(False,updated_by=0)


def start(client,text='Original'):
    response=client.post('/widget/api/message',json={'text':text})
    assert response.status_code==200,response.text
    body=response.json()
    return body,{'x-spooldesk-session':body['session']}


async def test_reply_edit_delete_and_revision_history(client):
    body,headers=start(client); mid=body['messages'][0]['id']
    result=client.post('/widget/api/message',headers=headers,json={'text':'A follow up','reply_to_id':mid})
    assert result.status_code==200 and result.json()['messages'][-1]['reply']['id']==mid
    assert client.post(f'/widget/api/message/{mid}/edit',headers=headers,json={'text':'Corrected','revision':0}).status_code==200
    assert client.post(f'/widget/api/message/{mid}/edit',headers=headers,json={'text':'Stale','revision':0}).status_code==409
    deleted=client.post(f'/widget/api/message/{mid}/delete',headers=headers,json={'revision':1})
    assert deleted.status_code==200
    assert mid not in [m['id'] for m in deleted.json()['messages']]
    async with db_session() as session:
        row=await session.get(Message,mid)
        assert row.text=='Corrected' and row.revision==2 and row.deleted_at
        changes=list((await session.execute(select(MessageRevision).where(MessageRevision.message_id==mid))).scalars())
        assert {c.kind for c in changes}=={'customer_edit','customer_delete'}


async def test_foreign_session_cannot_reply_or_change_message(client):
    body,first=start(client); mid=body['messages'][0]['id']
    _,second=start(client,'Other visitor')
    assert client.post(f'/widget/api/message/{mid}/edit',headers=second,json={'text':'Stolen','revision':0}).status_code==404
    assert client.post('/widget/api/message',headers=second,json={'text':'Cross-ticket quote','reply_to_id':mid}).status_code==400


async def test_closed_ticket_and_invalid_json_cannot_mutate(client):
    body,headers=start(client);mid=body['messages'][0]['id']
    assert client.post(f'/widget/api/message/{mid}/edit',headers=headers,json=[]).status_code==400
    assert client.post(f'/widget/api/message/{mid}/edit',headers=headers,content='bad json').status_code==400
    async with db_session(write=True) as session:
        (await session.get(Ticket,body['ticket'])).status='closed'
    assert client.post(f'/widget/api/message/{mid}/edit',headers=headers,json={'text':'Too late','revision':0}).status_code==404
