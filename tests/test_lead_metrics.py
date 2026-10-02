from datetime import datetime, timedelta
from decimal import Decimal
import pytest
from src.dashboard import lead_metrics, queries, report_queries
from src.database.db import db_session
from src.database.models import Message, Ticket, User
from tests.conftest import next_tg_id


def test_percentiles_empty_and_decimal():
    assert lead_metrics.percentile([], .9) is None
    assert lead_metrics.percentile([Decimal('1'),Decimal('3')],.5)==2
    assert lead_metrics.percentile([1,2,3,4,5],.9)==pytest.approx(4.6)


async def test_resolution_uses_closure_and_response_uses_confirmed_delivery(initialized_db):
    base=datetime(2042,1,10)
    async with db_session(write=True) as session:
        user=User(tg_id=next_tg_id(),is_active=True); session.add(user); await session.flush()
        ticket=Ticket(user_id=user.id,status='closed',category='Other',created_at=base,closed_at=base+timedelta(hours=8))
        session.add(ticket); await session.flush()
        session.add_all([
            Message(ticket_id=ticket.id,sender_type='admin',sender_id=0,text='Private',is_internal=True,created_at=base+timedelta(minutes=1)),
            Message(ticket_id=ticket.id,sender_type='admin',sender_id=0,text='Failed',delivery_status='failed',created_at=base+timedelta(minutes=2)),
            Message(ticket_id=ticket.id,sender_type='admin',sender_id=0,text='Delivered',delivery_status='delivered',created_at=base+timedelta(minutes=3),delivered_at=base+timedelta(minutes=20)),
        ])
    snapshot=await lead_metrics.snapshot(base,base+timedelta(days=1))
    summary=await report_queries.report_summary(base,base+timedelta(days=1))
    assert snapshot['response_count']==1 and snapshot['response_p50']==pytest.approx(20,abs=.01)
    assert snapshot['resolution_p90']==pytest.approx(8) and summary['avg_close_hours']==pytest.approx(8)
    async with db_session() as session:
        assert (await queries._effective_close_seconds_map(session,[ticket.id]))[ticket.id]==pytest.approx(8*3600)
