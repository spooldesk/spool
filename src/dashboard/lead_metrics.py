"""Explicit operational metrics: current queue versus a defined report cohort."""
from datetime import datetime
from sqlalchemy import text
from src.database.db import db_session
from src.database.dialect import seconds_between


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    rows = sorted(float(v) for v in values)
    position = (len(rows)-1)*fraction
    low = int(position)
    high = min(low+1, len(rows)-1)
    return rows[low] + (rows[high]-rows[low])*(position-low)


async def snapshot(date_from: datetime, date_to: datetime) -> dict:
    from src.database.db import settings
    def sec(end, start):
        if end == 'CAST(:now AS TIMESTAMP)' and not settings.database_url.startswith('postgresql'):
            end = ':now'
        return seconds_between(end, start, url=settings.database_url)
    params = {'df':date_from, 'dt':date_to, 'now':datetime.utcnow()}
    async with db_session() as session:
        response = list((await session.execute(text(f'''
            SELECT {sec('fa.first_at', 't.created_at')} / 60.0
            FROM tickets t JOIN (
                SELECT ticket_id, MIN(COALESCE(delivered_at,created_at)) first_at FROM messages
                WHERE sender_type='admin' AND is_internal=false AND delivery_status='delivered'
                GROUP BY ticket_id
            ) fa ON fa.ticket_id=t.id
            WHERE t.created_at>=:df AND t.created_at<:dt AND fa.first_at>=t.created_at
        '''),params)).scalars())
        closed = list((await session.execute(text(f'''
            SELECT {sec('t.closed_at', 't.created_at')} / 3600.0 FROM tickets t
            WHERE t.closed_at>=:df AND t.closed_at<:dt AND t.closed_at>=t.created_at
        '''),params)).scalars())
        queue = (await session.execute(text(f'''
            SELECT COUNT(*), SUM(CASE WHEN stage='waiting' THEN 1 ELSE 0 END),
                   SUM(CASE WHEN sla_breached_at IS NOT NULL THEN 1 ELSE 0 END),
                   MAX({sec('CAST(:now AS TIMESTAMP)', 'created_at')}) / 3600.0
            FROM tickets WHERE status='active'
        '''),params)).one()
        unanswered = (await session.execute(text('''
            SELECT COUNT(*) FROM tickets t WHERE status='active' AND EXISTS (
                SELECT 1 FROM messages incoming WHERE incoming.ticket_id=t.id
                AND incoming.sender_type='user' AND incoming.deleted_at IS NULL
                AND NOT EXISTS (SELECT 1 FROM messages reply WHERE reply.ticket_id=t.id
                    AND reply.sender_type='admin' AND reply.is_internal=false
                    AND reply.delivery_status='delivered'
                    AND COALESCE(reply.delivered_at,reply.created_at)>=incoming.created_at)
            )
        '''))).scalar_one()
        delivery = (await session.execute(text('''
            SELECT COUNT(*) FROM outbound_jobs WHERE status IN ('failed','uncertain')
        '''))).scalar_one()
    return {'response_p50':percentile(response,.5),'response_p90':percentile(response,.9),
            'response_count':len(response), 'resolution_p50':percentile(closed,.5),
            'resolution_p90':percentile(closed,.9), 'resolution_count':len(closed),
            'open':int(queue[0] or 0),'waiting':int(queue[1] or 0),'sla_breached':int(queue[2] or 0),
            'oldest_hours':float(queue[3]) if queue[3] is not None else None,
            'unanswered':int(unanswered), 'delivery_attention':int(delivery), 'as_of':params['now']}
