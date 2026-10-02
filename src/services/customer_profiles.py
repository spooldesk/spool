"""Explicit account grouping. Linking never changes routing or proves identity."""
import re
from sqlalchemy import select
from src.database.db import db_session
from src.database.models import AdminUser, User, Ticket
from src.database.workspace_models import CustomerProfile
from src.channels.identity import address_of, channel_of, display_address, create_customer, find_customer
from src.services.permissions import can


async def accounts(session, user: User) -> list[User]:
    if user.customer_profile_id is None:
        return [user]
    return list((await session.execute(select(User).where(
        User.customer_profile_id == user.customer_profile_id).order_by(User.id))).scalars())


def label(user: User) -> str:
    if user.username:
        return ('@' if channel_of(user) == 'telegram' else '') + user.username
    return display_address(user)


async def details(user_id: int) -> list[dict]:
    async with db_session() as session:
        user = await session.get(User, user_id)
        if user is None:
            return []
        return [{"id": u.id, "channel": channel_of(u), "address": display_address(u),
                 "label": label(u), "is_active": u.is_active,
                 "verified": bool(u.identity_verified_fields)} for u in await accounts(session, user)]


def normalize(channel, address):
    address = address.strip()
    if len(address) > 320 or any(c in address for c in '\r\n\x00'):
        raise ValueError('contact_invalid')
    if channel == 'telegram':
        if not re.fullmatch(r'[1-9]\d{0,17}', address):
            raise ValueError('contact_invalid')
    elif channel == 'slack':
        if not re.fullmatch(r'[UD][A-Z0-9]{5,30}', address):
            raise ValueError('contact_invalid')
    elif channel == 'email':
        if not re.fullmatch(r'[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+', address):
            raise ValueError('e_invalid_email')
        address = address.lower()
    else:
        raise ValueError('contact_invalid')
    return address


async def link(user_id: int, channel: str, address: str, actor_id: int, *, confirmed=False) -> int:
    if not confirmed:
        raise ValueError('contact_link_confirm')
    address = normalize(channel, address)
    # Authorize before any provider request; the API opens a private DM only.
    async with db_session() as session:
        if not await can(await session.get(AdminUser, actor_id), 'users', edit=True, session=session):
            raise ValueError('e_forbidden_section')
    if channel == 'slack' and address.startswith('U'):
        from src.channels import get
        try:
            pipe = get('slack')
            ok, result = await pipe._post('conversations.open', {'users': address})
            if not ok:
                raise ValueError('contact_slack_failed')
            address = str(result.get('channel', {}).get('id', ''))
            if not re.fullmatch(r'D[A-Z0-9]{5,30}', address):
                raise ValueError('contact_slack_failed')
        except (LookupError, AttributeError):
            raise ValueError('contact_slack_failed') from None
    async with db_session(write=True) as session:
        if not await can(await session.get(AdminUser, actor_id), 'users', edit=True, session=session):
            raise ValueError('e_forbidden_section')
        anchor = await session.get(User, user_id)
        if not anchor:
            raise ValueError('e_user_not_found')
        other = await find_customer(session, channel=channel, address=address)
        if other and (other.id == anchor.id or (other.customer_profile_id and other.customer_profile_id == anchor.customer_profile_id)):
            raise ValueError('contact_same')
        if other and other.customer_profile_id and other.customer_profile_id != anchor.customer_profile_id:
            raise ValueError('contact_already_linked')
        if not other:
            other = await create_customer(session, channel=channel, address=address,
                team_id=anchor.team_id, email=address if channel == 'email' else None)
            other.is_active = anchor.is_active
        if anchor.customer_profile_id is None:
            group = CustomerProfile(name=label(anchor)[:255])
            session.add(group)
            await session.flush()
            anchor.customer_profile_id = group.id
        other.customer_profile_id = anchor.customer_profile_id
        # Manual association is not proof of a widget/email identity. Preserve
        # each account's original verification and blocked state unchanged.
        return other.id


async def unlink(user_id: int, account_id: int, actor_id: int) -> None:
    async with db_session(write=True) as session:
        if not await can(await session.get(AdminUser, actor_id), 'users', edit=True, session=session):
            raise ValueError('e_forbidden_section')
        anchor, account = await session.get(User, user_id), await session.get(User, account_id)
        if not anchor or not account or not anchor.customer_profile_id or account.customer_profile_id != anchor.customer_profile_id:
            raise ValueError('contact_invalid')
        account.customer_profile_id = None


async def send(user_id: int, account_id: int, text: str, actor_id: int, *, request_key: str) -> tuple[int, int]:
    """Record an operator-initiated conversation before delivery, on that pipe."""
    from src.channels import get
    from src.services import outbound
    text = text.strip()
    if not text or len(text) > 4096:
        raise ValueError('chat_invalid_text')
    if not re.fullmatch(r'[a-f0-9]{32}', request_key):
        raise ValueError('contact_invalid')
    async with db_session(write=True) as session:
        actor = await session.get(AdminUser, actor_id)
        if not await can(actor, 'users', edit=True, session=session):
            raise ValueError('e_forbidden_section')
        anchor = await session.get(User, user_id)
        if not anchor:
            raise ValueError('e_user_not_found')
        linked = {u.id: u for u in await accounts(session, anchor)}
        account = linked.get(account_id)
        if not account or not account.is_active:
            raise ValueError('contact_invalid')
        channel = channel_of(account)
        try:
            get(channel)
        except LookupError:
            raise ValueError('contact_channel_off') from None
        from src.database.delivery_models import OutboundJob
        key = f'customer:{actor_id}:{request_key}'
        previous_ticket = (await session.execute(select(Ticket).where(Ticket.conversation_key == key))).scalar_one_or_none()
        if previous_ticket:
            from src.database.models import Message
            previous = (await session.execute(select(OutboundJob, Message).join(Message, Message.id == OutboundJob.message_id)
                .where(Message.ticket_id == previous_ticket.id, OutboundJob.purpose == 'customer_direct'))).first()
            if (not previous or previous_ticket.user_id != account.id or previous[0].address != address_of(account)
                    or previous[1].text != text or previous[0].channel != channel):
                raise ValueError('contact_invalid')
            return previous_ticket.id, previous[0].id
        # A direct message is a new tracked conversation; it must not be
        # appended to an unrelated, possibly sensitive, active ticket.
        ticket = Ticket(user_id=account.id, team_id=account.team_id, status='active',
            stage='in_progress', category='', conversation_key=key,
            assigned_admin_id=actor_id if await can(actor, 'tickets', edit=True, session=session) else None)
        session.add(ticket)
        await session.flush()
        job = await outbound.enqueue(session, ticket_id=ticket.id, channel=channel,
            address=address_of(account), text=text, sender_id=actor_id, operator_id=actor_id,
            purpose='customer_direct')
        return ticket.id, job.id


async def update_profile(user_id: int, actor_id: int, *, action: str, team_id: int = 0):
    from src.services import user_service
    async with db_session(write=True) as session:
        if not await can(await session.get(AdminUser, actor_id), 'users', edit=True, session=session):
            raise ValueError('e_forbidden_section')
        user = await session.get(User, user_id)
        if not user:
            raise ValueError('e_user_not_found')
        for member in await accounts(session, user):
            if action == 'remove':
                await user_service.remove(session, member.id)
            elif action == 'team':
                await user_service.change_team(session, member.id, team_id)
            elif action in {'block', 'unblock'}:
                await user_service.set_active(session, member.id, action == 'unblock')
            else:
                raise ValueError('contact_invalid')
