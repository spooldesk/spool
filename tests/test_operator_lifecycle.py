"""Account and team transitions against both supported database engines."""
import asyncio
from datetime import datetime, timedelta
import pytest
from sqlalchemy import select
from src.database.db import db_session
from src.database.models import AdminUser, OperatorInvitation, OperatorTeam, Team, Ticket, User
from src.services import admin_user_service as accounts, invitation_service as invites, assignment_service
from tests.conftest import next_tg_id


async def operator(role='owner'):
    return await accounts.create_admin(email=f'lifecycle-{next_tg_id()}@example.test',
        password='test-password', role=role)


async def test_invitation_accepts_once_and_stores_only_hash(initialized_db):
    owner = await operator()
    async with db_session(write=True) as session:
        team = Team(name=f'Lifecycle {next_tg_id()}'); session.add(team); await session.flush()
    email = f'invite-{next_tg_id()}@example.test'
    token = await invites.issue(actor_id=owner.id, email=email.upper(), name='Pat', role='agent', team_id=team.id)
    async with db_session() as session:
        row = (await session.execute(select(OperatorInvitation).where(OperatorInvitation.email == email))).scalar_one()
        assert token not in row.token_hash and len(row.token_hash) == 64
    accepted = await invites.accept(token, 'new-test-password')
    assert accepted.email == email and accepted.role == 'agent'
    assert await accounts.authenticate(email, 'new-test-password')
    async with db_session() as session:
        assert await session.get(OperatorTeam, (accepted.id, team.id))
    with pytest.raises(ValueError, match='invite_invalid'):
        await invites.accept(token, 'another-password')


@pytest.mark.parametrize('reason', ['expired','revoked','disabled','demoted','replaced'])
async def test_invalidated_invitation_cannot_create_account(initialized_db, reason):
    owner = await operator()
    email=f'invalid-{next_tg_id()}@example.test'
    token=await invites.issue(actor_id=owner.id,email=email,name='',role='viewer')
    if reason in {'expired','revoked'}:
        async with db_session(write=True) as session:
            row=(await session.execute(select(OperatorInvitation).where(OperatorInvitation.email==email))).scalar_one()
            if reason=='expired': row.expires_at=datetime.utcnow()-timedelta(seconds=1)
            else: row.revoked_at=datetime.utcnow()
    elif reason=='disabled': await accounts.set_active(owner.id,False)
    elif reason=='demoted':
        await accounts.update_profile(owner.id,role='agent')
        await accounts.update_profile(owner.id,role='owner')  # old links stay revoked
    else: await invites.issue(actor_id=owner.id,email=email,name='',role='agent')
    with pytest.raises(ValueError,match='invite_invalid'):
        await invites.accept(token,'new-test-password')
    assert await accounts.get_by_email(email) is None


async def test_concurrent_accept_creates_one_account(initialized_db):
    owner=await operator(); email=f'race-{next_tg_id()}@example.test'
    token=await invites.issue(actor_id=owner.id,email=email,name='',role='agent')
    results=await asyncio.gather(invites.accept(token,'test-password'),invites.accept(token,'test-password'),return_exceptions=True)
    assert sum(isinstance(r,AdminUser) for r in results)==1
    assert sum(isinstance(r,ValueError) for r in results)==1


async def test_agent_cannot_invite_or_change_membership(initialized_db):
    agent=await operator('agent')
    with pytest.raises(ValueError,match='e_forbidden_section'):
        await invites.issue(actor_id=agent.id,email='new@example.test',name='',role='owner')
    with pytest.raises(ValueError,match='e_forbidden_section'):
        await invites.set_teams(agent.id,agent.id,[])


async def test_public_acceptance_route_requires_csrf_and_supports_first_login(initialized_db):
    from fastapi.testclient import TestClient
    from src.dashboard.app import app
    owner=await operator();email=f'join-{next_tg_id()}@example.test'
    token=await invites.issue(actor_id=owner.id,email=email,name='New teammate',role='agent')
    with TestClient(app) as client:
        page=client.get('/join')
        assert page.status_code==200 and 'invitation-token' in page.text
        assert client.post('/join',data={'token':token,'password':'new-test-password'}).status_code==403
        accepted=client.post('/join',data={'token':token,'password':'new-test-password',
            'csrf_token':client.cookies.get('supbot_csrf')},follow_redirects=False)
        assert accepted.status_code==303 and accepted.headers['location']=='/login'
    assert await accounts.authenticate(email,'new-test-password')


async def test_viewer_cannot_mutate_tickets_but_can_save_own_preferences(initialized_db):
    from fastapi.testclient import TestClient
    from src.dashboard.app import app
    from src.dashboard.auth import make_session_cookie,SESSION_COOKIE
    viewer=await operator('viewer')
    with TestClient(app) as client:
        client.get('/login')
        client.cookies.set(SESSION_COOKIE,make_session_cookie(viewer.id,viewer.session_version))
        csrf=client.cookies.get('supbot_csrf')
        assert client.get('/tickets').status_code==200
        assert client.post('/tickets/999999/reply',data={'text':'forbidden','csrf_token':csrf}).status_code==403
        assert client.post('/me',data={'theme':'light','csrf_token':csrf},follow_redirects=False).status_code==303


async def test_team_routing_and_offboarding_preserve_history(initialized_db):
    owner, first, second = await operator(), await operator('agent'), await operator('agent')
    async with db_session(write=True) as session:
        team=Team(name=f'Routing {next_tg_id()}'); user=User(tg_id=next_tg_id(),is_active=True)
        session.add_all([team,user]); await session.flush()
        ticket=Ticket(user_id=user.id,team_id=team.id,status='active',category='Other'); session.add(ticket); await session.flush()
    await invites.set_teams(owner.id,first.id,[team.id])
    assert not await assignment_service.assign_ticket(ticket.id,second.id)
    assert await assignment_service.assign_ticket(ticket.id,first.id)
    eligible=await assignment_service.list_active_operators(ticket.id)
    assert first.id in [a.id for a in eligible] and second.id not in [a.id for a in eligible]
    old_version=first.session_version
    await accounts.set_active(first.id,False)
    async with db_session() as session:
        assert (await session.get(Ticket,ticket.id)).assigned_admin_id is None
        assert (await session.get(AdminUser,first.id)).session_version > old_version
    assert not await accounts.authenticate(first.email,'test-password')
    assert await accounts.delete_admin(first.id)
    async with db_session() as session:
        assert await session.get(Ticket,ticket.id)
