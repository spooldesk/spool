"""Real shared presence and HTTP permissions, without customer network calls."""
from datetime import datetime, timedelta
from unittest.mock import AsyncMock

from tests.test_assistant import environment, operator, ticket, client, post
from src.database.db import db_session
from src.database.models import ConversationPresence
from src.services import presence_service, permissions


async def test_tabs_are_independent_and_operator_is_not_shown_to_themself(environment):
    row = await ticket()
    colleague = await operator('admin', 'Colleague')
    await presence_service.heartbeat(row.id, 'tab-a', 'Colleague', operator_id=colleague.id, typing=True)
    await presence_service.heartbeat(row.id, 'tab-b', 'Colleague', operator_id=colleague.id)
    seen = await presence_service.heartbeat(row.id, 'viewer', 'Owner', operator_id=environment.id)
    assert len(seen) == 1 and seen[0]['typing'] and seen[0]['replying']
    assert await presence_service.heartbeat(row.id, 'tab-c', 'Colleague', operator_id=colleague.id) == [
        {'id':environment.id, 'name':'Owner', 'customer':False, 'typing':False, 'replying':False}]
    await presence_service.heartbeat(row.id, 'tab-b', 'Colleague', operator_id=colleague.id, leaving=True)
    assert (await presence_service.heartbeat(row.id, 'viewer', 'Owner', operator_id=environment.id))[0]['replying']


async def test_note_is_distinct_and_not_exposed_to_customer_and_stops_on_expiry(environment):
    row = await ticket()
    await presence_service.heartbeat(row.id, 'writer', 'Writer', operator_id=environment.id, typing=True, public=False)
    seen = await presence_service.heartbeat(row.id, 'visitor', 'Customer')
    assert seen[0]['typing'] and not seen[0]['replying']
    assert await presence_service.public_typists(row.id) == []
    async with db_session(write=True) as session:
        (await session.get(ConversationPresence, (row.id, 'writer'))).typing_until = datetime.utcnow()-timedelta(seconds=1)
    assert not (await presence_service.heartbeat(row.id, 'visitor', 'Customer'))[0]['typing']
    async with db_session(write=True) as session:
        (await session.get(ConversationPresence, (row.id, 'writer'))).seen_at = datetime.utcnow()-timedelta(seconds=21)
    assert await presence_service.heartbeat(row.id, 'visitor', 'Customer') == []


async def test_http_two_operators_stop_and_leave_without_affecting_another_tab(environment, monkeypatch):
    monkeypatch.setattr('src.dashboard.routes.tickets._show_typing_to_customer', AsyncMock())
    other = await operator('admin', 'Second')
    row = await ticket()
    a, b = client(environment), client(other)
    url = f'/api/tickets/{row.id}/presence'
    assert post(b, url, form={'client_id':'b'*32,'typing':'1'}).status_code == 200
    seen = post(a, url, form={'client_id':'a'*32}).json()['others']
    assert seen[0]['name'] == 'Second' and seen[0]['replying']
    post(b, url, form={'client_id':'c'*32,'leaving':'1'})
    assert post(a,url).json()['others'][0]['typing']
    post(b,url,form={'client_id':'b'*32,'typing':'0'})
    assert not post(a,url).json()['others'][0]['typing']
    assert b.post(url,data={'typing':'1'}).status_code == 403
    assert post(b,url,form={'client_id':'../invalid'}).status_code == 400
    page = a.get(f'/tickets/{row.id}')
    assert page.status_code == 200 and 'id="composer-presence"' in page.text
    assert 'ticket-presence.js' in page.text


async def test_viewer_cannot_advertise_typing(environment, monkeypatch):
    nudge = AsyncMock()
    monkeypatch.setattr('src.dashboard.routes.tickets._show_typing_to_customer', nudge)
    role = await permissions.save(environment.id, 'Typing viewer', {'tickets':'view'})
    reader = await operator(role)
    row = await ticket()
    response = post(client(reader), f'/api/tickets/{row.id}/presence', form={'typing':'1'})
    assert response.status_code == 200
    seen = await presence_service.heartbeat(row.id, 'owner', 'Owner', operator_id=environment.id)
    assert not seen[0]['typing'] and not seen[0]['replying']
    nudge.assert_not_awaited()
