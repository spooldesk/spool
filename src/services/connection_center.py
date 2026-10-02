"""Owner diagnostics: separate credentials, authentication and observed traffic.

Page reads never contact providers or retry delivery. No payload, address or
raw provider error is selected for the problem list. Network checks use a
snapshot and their results become stale when that configuration changes.
"""
import hashlib
import json
import secrets
from datetime import datetime, timedelta

from sqlalchemy import and_, func, or_, select

from src.database.db import db_session
from src.database.delivery_models import OutboundJob
from src.database.inbound_models import InboundEvent
from src.database.models import AdminUser, AppSetting, Message, Ticket, User
from src.services import integrations, integration_checks, worker_lease
from src.services.connection_urls import usable_origin
from src.services.outbound import STALE_AFTER

CHANNELS = ('telegram', 'slack', 'email', 'widget')
CHECK_TTL = timedelta(hours=24)
COOLDOWN = timedelta(seconds=30)
STALL_AFTER = timedelta(minutes=15)
CHECK_CODES = frozenset(('rate_limited', 'provider_unavailable', 'authentication_failed',
    'authentication_ok', 'slack_scopes_unknown', 'slack_scopes_missing',
    'slack_optional_scopes', 'telegram_webhook_active', 'imap_inbox_unavailable',
    'tls_failed', 'mail_tls_or_auth_unavailable', 'connection_failed',
    'connection_timeout', 'configuration_incomplete'))


def _json(raw):
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else {}
    except (ValueError, TypeError):
        return {}


async def _config(session, channel):
    row = await session.get(AppSetting, 'integration_' + channel)
    raw = row.value if row else None
    try:
        values, enabled, unreadable = integrations._decode(channel, raw)
    except (ValueError, TypeError, AttributeError):
        values, enabled, unreadable = {}, False, True
    # DB secrets are already ciphertext. Environment fallbacks are hashed,
    # never persisted in plaintext; the digest is not exposed to templates.
    fingerprint = hashlib.sha256((raw if row else json.dumps(values, sort_keys=True)).encode()).hexdigest()
    required = {'telegram': ('bot_token',), 'slack': ('bot_token', 'signing_secret'),
        'email': ('address', 'password', 'imap_host', 'smtp_host')}[channel]
    return values, bool(enabled and not unreadable and all(values.get(k) for k in required)), fingerprint, row.updated_at if row else None


def _safe_parts(parts):
    safe = []
    if not isinstance(parts, list):
        return safe
    for part in parts[:3]:
        if not isinstance(part, dict):
            continue
        name = part.get('part')
        if name not in ('slack', 'telegram', 'imap', 'smtp', 'email'):
            continue
        code = part.get('code')
        missing = part.get('missing') if isinstance(part.get('missing'), list) else []
        safe.append({'part': name,
            'status': part.get('status') if code in CHECK_CODES and part.get('status') in ('ok', 'warning', 'error') else 'error',
            'code': code if code in CHECK_CODES else 'connection_failed',
            'missing': [s for s in (*integration_checks.SLACK_REQUIRED, *integration_checks.SLACK_OPTIONAL)
                        if s in missing]})
    return safe


def _check_view(row, fingerprint, now):
    data = _json(row.value if row else '')
    try:
        checked = datetime.fromisoformat(data['checked_at'])
        if checked.tzinfo is not None or checked > now + timedelta(seconds=10):
            return None
    except (KeyError, ValueError, TypeError):
        return None
    parts = _safe_parts(data.get('parts', []))
    if not parts:
        return None
    stale = data.get('fingerprint') != fingerprint or now - checked > CHECK_TTL
    status = 'error' if any(p['status'] == 'error' for p in parts) else (
        'warning' if any(p['status'] == 'warning' for p in parts) else 'ok')
    return {'checked_at': checked, 'stale': stale, 'status': status, 'parts': parts}


async def check_connection(channel: str, *, actor_id: int) -> str:
    if channel not in integrations.FIELDS:
        raise ValueError('integration_unknown')
    async with db_session() as session:
        actor = await session.get(AdminUser, actor_id)
        if not actor or not actor.is_active or actor.role != 'owner':
            raise ValueError('e_forbidden_section')
        values, configured, fingerprint, _ = await _config(session, channel)
        row = await session.get(AppSetting, 'integration_check_' + channel)
        previous = _check_view(row, fingerprint, datetime.utcnow())
        if previous and not previous['stale'] and datetime.utcnow() - previous['checked_at'] < COOLDOWN:
            raise ValueError('connection_check_recent')
    if not configured:
        raise ValueError('integration_incomplete')
    lease, owner = 'connection-check-' + channel, secrets.token_hex(16)
    if not await worker_lease.acquire(lease, owner, 90):
        raise ValueError('connection_check_busy')
    try:
        # Recheck under the lease: another check may have completed between
        # the initial read and acquiring it. Never probe an obsolete snapshot.
        async with db_session() as session:
            _, still_configured, latest_fingerprint, _ = await _config(session, channel)
            if not still_configured or latest_fingerprint != fingerprint:
                return 'connection_check_changed'
            latest = _check_view(await session.get(AppSetting, 'integration_check_' + channel), fingerprint, datetime.utcnow())
            if latest and not latest['stale'] and datetime.utcnow() - latest['checked_at'] < COOLDOWN:
                raise ValueError('connection_check_recent')
        parts = _safe_parts(await integration_checks.probe(channel, values))
        async with db_session(write=True) as session:
            actor = await session.get(AdminUser, actor_id)
            if not actor or not actor.is_active or actor.role != 'owner':
                raise ValueError('e_forbidden_section')
            _, current_configured, current_fingerprint, _ = await _config(session, channel)
            if not current_configured or fingerprint != current_fingerprint:
                return 'connection_check_changed'
            key = 'integration_check_' + channel
            row = await session.get(AppSetting, key)
            raw = json.dumps({'fingerprint': fingerprint, 'checked_at': datetime.utcnow().isoformat(), 'parts': parts})
            if row is None:
                session.add(AppSetting(key=key, value=raw, updated_by=actor_id))
            else:
                row.value, row.updated_by = raw, actor_id
        return 'connection_checked'
    finally:
        await worker_lease.release(lease, owner)


def _outbound_problem(now):
    # A scheduled backoff is expected, regardless of the original creation age.
    cutoff = now - STALL_AFTER
    return or_(OutboundJob.status.in_(('failed', 'uncertain')),
        and_(OutboundJob.status == 'sending',
             func.coalesce(OutboundJob.started_at, OutboundJob.created_at) < now - STALE_AFTER),
        and_(OutboundJob.status.in_(('pending', 'retry')),
             func.coalesce(OutboundJob.next_attempt_at, OutboundJob.created_at) < cutoff))


async def snapshot() -> dict:
    now = datetime.utcnow()
    states = {ch: await integrations.state(ch) for ch in integrations.FIELDS}
    async with db_session() as session:
        widget_keys = ('widget_enabled', 'widget_public_url', 'widget_origins')
        widget = {r.key: r for r in (await session.execute(select(AppSetting).where(AppSetting.key.in_(widget_keys)))).scalars()}
        widget_value = lambda key: widget[key].value if key in widget else ''
        states['widget'] = {'enabled': widget_value('widget_enabled') == '1',
            'configured': widget_value('widget_enabled') == '1'}
        evidence = {}
        for channel in CHANNELS:
            changed = None
            check = None
            if channel != 'widget':
                _, _, fingerprint, changed = await _config(session, channel)
                check = _check_view(await session.get(AppSetting, 'integration_check_' + channel), fingerprint, now)
                if check and not states[channel]['configured']:
                    check['stale'] = True
            elif widget:
                changed = max(r.updated_at for r in widget.values())
            incoming = (await session.execute(select(func.max(Message.created_at)).join(Ticket,
                Ticket.id == Message.ticket_id).join(User, User.id == Ticket.user_id).where(
                    User.channel == channel, Message.sender_type == 'user', Message.is_internal.is_(False)))).scalar_one()
            outgoing = (await session.execute(select(func.max(OutboundJob.delivered_at)).where(
                OutboundJob.channel == channel, OutboundJob.status == 'delivered'))).scalar_one()
            queues = dict((await session.execute(select(OutboundJob.status, func.count()).where(
                OutboundJob.channel == channel, OutboundJob.status.not_in(('delivered', 'cancelled'))
            ).group_by(OutboundJob.status))).all())
            inbound_pending = (await session.execute(select(func.count()).select_from(InboundEvent).where(
                InboundEvent.channel == channel, InboundEvent.status == 'pending'))).scalar_one()
            public = usable_origin(states[channel].get('values', {}).get('public_url', '')) if channel == 'slack' else (
                usable_origin(widget_value('widget_public_url')) if channel == 'widget' else '')
            # Without a saved configuration epoch, require traffic after the
            # current explicit check; historical traffic alone is not readiness.
            since = changed or (check['checked_at'] if check and not check['stale'] else now)
            steps = [{'key': 'saved', 'done': states[channel]['configured']}]
            if channel in ('slack', 'widget'):
                steps.append({'key': 'public', 'done': bool(public)})
            if channel != 'widget':
                steps.append({'key': 'authentication', 'done': bool(check and not check['stale'] and all(
                    p['code'] in ('authentication_ok', 'slack_optional_scopes') for p in check['parts']))})
            steps.extend([{'key': 'incoming', 'done': bool(incoming and incoming >= since)},
                          {'key': 'outgoing', 'done': bool(outgoing and outgoing >= since)}])
            evidence[channel] = {'check': check, 'incoming_at': incoming, 'outgoing_at': outgoing,
                'outgoing_queues': queues, 'inbound_pending': inbound_pending,
                'public_url': public, 'steps': steps, 'completed': sum(s['done'] for s in steps),
                'changed_at': changed, 'enabled': states[channel]['enabled']}

        outbound_filter = _outbound_problem(now)
        inbound_filter = and_(InboundEvent.status == 'pending', or_(InboundEvent.last_error.is_not(None),
            InboundEvent.created_at < now - STALL_AFTER))
        outgoing_counts = dict((await session.execute(select(OutboundJob.channel, func.count()).where(
            outbound_filter).group_by(OutboundJob.channel))).all())
        incoming_counts = dict((await session.execute(select(InboundEvent.channel, func.count()).where(
            inbound_filter).group_by(InboundEvent.channel))).all())
        total_out, total_in = sum(outgoing_counts.values()), sum(incoming_counts.values())
        for channel in CHANNELS:
            evidence[channel]['problem_count'] = outgoing_counts.get(channel, 0) + incoming_counts.get(channel, 0)
        # Only operational metadata; no addresses, payloads or exception strings.
        outgoing_problems = [dict(r) for r in (await session.execute(select(
            OutboundJob.id, OutboundJob.channel, OutboundJob.status, OutboundJob.attempts,
            OutboundJob.created_at, OutboundJob.next_attempt_at, Message.ticket_id,
            OutboundJob.message_id).join(Message, Message.id == OutboundJob.message_id)
            .where(outbound_filter).order_by(OutboundJob.created_at, OutboundJob.id).limit(25))).mappings()]
        incoming_problems = [dict(r) for r in (await session.execute(select(
            InboundEvent.id, InboundEvent.channel, InboundEvent.attempts,
            InboundEvent.created_at, InboundEvent.next_attempt_at, InboundEvent.ticket_id)
            .where(inbound_filter).order_by(InboundEvent.created_at, InboundEvent.id).limit(25))).mappings()]
    return {'channels': states, 'evidence': evidence, 'outgoing_problems': outgoing_problems,
        'incoming_problems': incoming_problems, 'problem_count': total_out + total_in,
        'outgoing_problem_count': total_out, 'incoming_problem_count': total_in,
        'widget_public_url': widget_value('widget_public_url'), 'generated_at': now}
