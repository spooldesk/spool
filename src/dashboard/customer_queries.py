"""Customer-level views over channel identities without merging conversations."""
from collections import Counter
from sqlalchemy import select, func, case
from src.database.db import db_session
from src.database.models import User, Team, Ticket
from src.channels.identity import channel_of, display_address
from src.services import customer_profiles


async def listing(limit=None, *, team_id=None, unassigned_only=False, sort='last', direction='desc'):
    async with db_session() as session:
        users = list((await session.execute(select(User).order_by(User.id))).scalars())
        teams = dict((await session.execute(select(Team.id, Team.name))).all())
        tickets = (await session.execute(select(Ticket.user_id, func.count(Ticket.id),
            func.sum(case((Ticket.status == 'active', 1), else_=0)),
            func.sum(case((Ticket.status == 'closed', 1), else_=0)), func.max(Ticket.created_at)
        ).group_by(Ticket.user_id))).all()
        categories = (await session.execute(select(Ticket.user_id, Ticket.category, func.count(Ticket.id))
            .group_by(Ticket.user_id, Ticket.category))).all()
    grouped = {}
    by_user = {}
    for user in users:
        key = ('profile', user.customer_profile_id) if user.customer_profile_id else ('user', user.id)
        grouped.setdefault(key, []).append(user)
        by_user[user.id] = key
    totals = {k: {'total': 0, 'active': 0, 'closed': 0, 'last': None, 'cats': Counter()} for k in grouped}
    for uid, total, active, closed, created in tickets:
        if uid not in by_user:
            continue
        stats = totals[by_user[uid]]
        stats['total'] += total
        stats['active'] += active
        stats['closed'] += closed
        if created and (stats['last'] is None or created > stats['last']):
            stats['last'] = created
    for uid, category, count in categories:
        if uid in by_user:
            totals[by_user[uid]]['cats'][category or '—'] += count
    result = []
    for key, members in grouped.items():
        if unassigned_only and not any(u.team_id is None for u in members):
            continue
        if not unassigned_only and team_id and not any(u.team_id == team_id for u in members):
            continue
        user, stats = members[0], totals[key]
        result.append({'user_db_id': user.id, 'tg_id': user.tg_id, 'username': user.username or '',
            'label': customer_profiles.label(user), 'team': ', '.join(dict.fromkeys(teams.get(u.team_id, '—') for u in members)),
            'local_user_id': user.local_user_id, 'total': stats['total'], 'active': stats['active'],
            'closed': stats['closed'], 'last_ticket_at': stats['last'], 'top_categories': stats['cats'].most_common(3),
            'channels': list(dict.fromkeys(channel_of(u) for u in members)),
            'search': ' '.join(customer_profiles.label(u) + ' ' + display_address(u) for u in members)})
    field = {'user': 'label', 'team': 'team', 'total': 'total', 'active': 'active', 'closed': 'closed', 'last': 'last_ticket_at'}.get(sort, 'last_ticket_at')
    result.sort(key=lambda r: r['user_db_id'], reverse=True)
    valued = [r for r in result if r[field] is not None]
    def sort_value(row):
        value = (row['username'] or row['label']) if sort == 'user' else row[field]
        return value.casefold() if isinstance(value, str) else value
    valued.sort(key=sort_value, reverse=direction != 'asc')
    ordered = valued + [r for r in result if r[field] is None]
    return ordered if limit is None else ordered[:limit]


async def detail(user_db_id):
    from src.dashboard.queries import _effective_close_seconds_map, is_row_id, is_sensitive_category
    if not is_row_id(user_db_id):
        return None
    async with db_session() as session:
        user = await session.get(User, user_db_id)
        if not user:
            return None
        members = await customer_profiles.accounts(session, user)
        ids = [u.id for u in members]
        channels = {u.id: channel_of(u) for u in members}
        tickets = list((await session.execute(select(Ticket).where(Ticket.user_id.in_(ids)).order_by(Ticket.id.desc()))).scalars())
        close_secs = await _effective_close_seconds_map(session, [t.id for t in tickets if t.status == 'closed'])
        teams = dict((await session.execute(select(Team.id, Team.name).order_by(Team.name))).all())
    rows = [{'id': t.id, 'status': t.status, 'category': t.category, 'channel': channels[t.user_id],
             'created_at': t.created_at, 'closed_at': t.closed_at,
             'processing_time_seconds': int(close_secs[t.id]) if close_secs.get(t.id) is not None else None,
             'is_sensitive': is_sensitive_category(t.category)} for t in tickets]
    return {'user_db_id': user.id, 'tg_id': user.tg_id, 'username': user.username or '',
        'label': customer_profiles.label(user), 'channel': channel_of(user), 'address': display_address(user),
        'team': teams.get(user.team_id, '—'), 'team_id': user.team_id, 'local_user_id': user.local_user_id,
        'is_verified': bool(user.identity_verified_fields), 'is_active': any(u.is_active for u in members),
        'language': user.language, 'created_at': user.created_at, 'tickets': rows,
        'categories': Counter(t.category or '—' for t in tickets).most_common(), 'total': len(tickets),
        'active_count': sum(t.status == 'active' for t in tickets), 'closed_count': sum(t.status == 'closed' for t in tickets),
        'all_teams': list(teams.items())}
