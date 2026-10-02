"""Channel mutations must preserve evidence and never cross a ticket boundary."""
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from src.channels.base import Delivery
from src.database.db import db_session
from src.database.models import Message, MessageRevision, Ticket, User
from src.services import admin_user_service, conversation_service as service, presence_service
from tests.conftest import next_tg_id


@pytest.fixture
async def context(initialized_db, monkeypatch):
    actor = await admin_user_service.create_admin(email=f'edit-{next_tg_id()}@test.example',
        password='test-password', name='Operator', role='agent')
    async with db_session(write=True) as session:
        user = User(tg_id=next_tg_id(), is_active=True)
        session.add(user); await session.flush()
        ticket = Ticket(user_id=user.id, category='Other', status='active')
        session.add(ticket); await session.flush()
        row = Message(ticket_id=ticket.id, sender_type='admin', sender_id=0,
            operator_id=actor.id, text='Original', channel_ref='991', delivery_status='delivered')
        session.add(row); await session.flush()
        tid, mid = ticket.id, row.id
    adapter = SimpleNamespace(edit_message=AsyncMock(return_value=Delivery.sent('991')))
    monkeypatch.setattr(service, 'get_channel', lambda channel: adapter)
    return SimpleNamespace(tid=tid, mid=mid, actor=actor, adapter=adapter)


async def test_success_commits_intent_before_provider_and_preserves_revision(context):
    async def send(*args, **kwargs):
        async with db_session(write=True) as session:
            row = await session.get(Message, context.mid)
            assert row.text == 'Original' and row.mutation_status == 'pending'
            change = (await session.execute(select(MessageRevision).where(
                MessageRevision.message_id == row.id))).scalar_one()
            assert change.before_text == 'Original' and change.after_text == 'Corrected'
        return Delivery.sent('991')
    context.adapter.edit_message.side_effect = send
    result = await service.edit_message(context.tid, context.mid, context.actor.id, 'Corrected', 0)
    assert result.ok
    async with db_session() as session:
        row = await session.get(Message, context.mid)
        assert (row.text, row.revision, row.mutation_status) == ('Corrected', 1, None)
        assert row.edited_at
        assert (await session.execute(select(MessageRevision.status).where(
            MessageRevision.message_id == row.id))).scalar_one() == 'applied'


async def test_recent_edit_to_old_message_is_not_hidden_by_later_deliveries(context):
    from src.dashboard.queries import messages_after
    now=datetime.utcnow()
    async with db_session(write=True) as session:
        old=await session.get(Message,context.mid)
        old.created_at=now-timedelta(days=1)
        old.edited_at=now;old.revision=1;old.text='Recent correction'
        rows=[Message(ticket_id=context.tid,sender_type='admin',sender_id=0,text='Later original message',
            delivery_status='delivered',created_at=now-timedelta(minutes=20),delivered_at=now-timedelta(minutes=10)) for _ in range(105)]
        session.add_all(rows);await session.flush();cursor=rows[-1].id
    updates=(await messages_after(context.tid,cursor))['updates']
    assert updates[0]['id']==context.mid and updates[0]['text']=='Recent correction'


@pytest.mark.parametrize('condition', ['other_agent', 'stale', 'foreign_ticket', 'inactive', 'viewer'])
async def test_refused_edit_never_calls_channel(context, condition):
    actor_id, tid, revision = context.actor.id, context.tid, 0
    if condition in ('other_agent', 'inactive', 'viewer'):
        async with db_session(write=True) as session:
            from src.database.models import AdminUser
            if condition == 'other_agent':
                (await session.get(Message, context.mid)).operator_id = None
            elif condition == 'inactive':
                (await session.get(AdminUser, actor_id)).is_active = False
            else:
                (await session.get(AdminUser, actor_id)).role = 'viewer'
    if condition == 'stale': revision = 8
    if condition == 'foreign_ticket': tid += 100000
    assert not (await service.edit_message(tid, context.mid, actor_id, 'Changed', revision)).ok
    context.adapter.edit_message.assert_not_called()


async def test_ambiguous_edit_keeps_original_and_refuses_blind_retry(context):
    context.adapter.edit_message.side_effect = TimeoutError()
    assert not (await service.edit_message(context.tid, context.mid, context.actor.id, 'New', 0)).ok
    assert not (await service.edit_message(context.tid, context.mid, context.actor.id, 'New', 0)).ok
    assert context.adapter.edit_message.call_count == 1
    async with db_session() as session:
        row = await session.get(Message, context.mid)
        assert (row.text, row.revision, row.mutation_status) == ('Original', 0, 'uncertain')


@pytest.mark.parametrize('applied',[True,False])
async def test_only_lead_can_reconcile_uncertain_edit_without_resending(context,applied):
    context.adapter.edit_message.side_effect=TimeoutError()
    await service.edit_message(context.tid,context.mid,context.actor.id,'Updated',0)
    assert not (await service.reconcile_mutation(context.tid,context.mid,context.actor.id,0,applied)).ok
    lead=await admin_user_service.create_admin(email=f'reconcile-{next_tg_id()}@test.example',password='test-password',role='admin')
    assert (await service.reconcile_mutation(context.tid,context.mid,lead.id,0,applied)).ok
    assert not (await service.reconcile_mutation(context.tid,context.mid,lead.id,0,applied)).ok
    assert context.adapter.edit_message.call_count==1
    async with db_session() as session:
        row=await session.get(Message,context.mid)
        assert row.mutation_status is None and row.revision==1
        assert row.text==('Updated' if applied else 'Original')


@pytest.mark.parametrize('condition', ['internal', 'deleted', 'other_ticket', 'pending'])
async def test_private_or_unavailable_reply_refused(context, condition):
    async with db_session(write=True) as session:
        row = await session.get(Message, context.mid)
        if condition == 'internal': row.is_internal = True
        if condition == 'deleted': row.deleted_at = datetime.utcnow()
        if condition == 'pending': row.delivery_status = 'pending'
        await session.flush()
        with pytest.raises(ValueError):
            await service.reply_context(session, context.tid + (1 if condition == 'other_ticket' else 0), context.mid)


async def test_presence_shared_and_private_typing_hidden(context):
    await presence_service.heartbeat(context.tid, 'operator:one', 'Operator',
        operator_id=context.actor.id, typing=True, public=False)
    assert await presence_service.public_typists(context.tid) == []
    others = await presence_service.heartbeat(context.tid, 'visitor:one', 'Visitor', typing=True)
    assert others[0]['typing'] and others[0]['name'] == 'Operator'
    await presence_service.heartbeat(context.tid, 'operator:one', 'Operator',
        operator_id=context.actor.id, typing=True, public=True)
    assert await presence_service.public_typists(context.tid) == ['Operator']
    await presence_service.heartbeat(context.tid, 'operator:one', '', leaving=True)
    assert await presence_service.public_typists(context.tid) == []


async def test_killed_request_becomes_uncertain(context):
    async with db_session(write=True) as session:
        row = await session.get(Message, context.mid); row.mutation_status = 'pending'
        session.add(MessageRevision(message_id=row.id, kind='edit', before_text='Original',
            after_text='New', status='pending', created_at=datetime.utcnow()-timedelta(minutes=3)))
    await service.recover_stale_edits()
    async with db_session() as session:
        assert (await session.get(Message, context.mid)).mutation_status == 'uncertain'


async def test_incoming_native_edit_matches_address_and_channel(context):
    from src.services.channel_events import update_customer_message
    async with db_session(write=True) as session:
        row = await session.get(Message, context.mid); row.sender_type = 'user'
        ticket = await session.get(Ticket, context.tid)
        user = await session.get(User, ticket.user_id)
        address = str(user.tg_id)
    async with db_session(write=True) as session:
        assert await update_customer_message(session, channel='slack', address=address, ref='991', text='Bad') is None
        assert await update_customer_message(session, channel='telegram', address=str(int(address)+1), ref='991', text='Bad') is None
        assert await update_customer_message(session, channel='telegram', address=address, ref='991', text='Native edit') == context.mid
    async with db_session() as session:
        row = await session.get(Message, context.mid)
        assert row.text == 'Native edit' and row.revision == 1
