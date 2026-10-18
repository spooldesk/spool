"""Count decisions separately from model calls, and delivery from generation."""
from sqlalchemy import case, func, select
from src.database.ai_models import AIAutoTurn
from src.database.db import db_session
from src.database.delivery_models import OutboundJob
from src.database.models import AICall, Ticket, User


async def overview():
    state = case((AIAutoTurn.status == "withheld", "withheld"),
                 else_=func.coalesce(OutboundJob.status, AIAutoTurn.status))
    reason = func.coalesce(AIAutoTurn.reason, OutboundJob.last_error)
    async with db_session() as session:
        counts = (await session.execute(select(state, func.count(AIAutoTurn.id))
            .outerjoin(OutboundJob, AIAutoTurn.outbound_job_id == OutboundJob.id)
            .group_by(state))).all()
        rows = (await session.execute(select(AIAutoTurn.id, AIAutoTurn.ticket_id,
            AIAutoTurn.created_at, state, reason, User.channel, AICall.model,
            AICall.tokens_in, AICall.tokens_out, AICall.latency_ms)
            .join(Ticket, Ticket.id == AIAutoTurn.ticket_id)
            .join(User, User.id == Ticket.user_id)
            .outerjoin(OutboundJob, OutboundJob.id == AIAutoTurn.outbound_job_id)
            .outerjoin(AICall, AICall.id == AIAutoTurn.ai_call_id)
            .order_by(AIAutoTurn.id.desc()).limit(50))).all()
        legacy = (await session.execute(select(AICall.id, AICall.ticket_id,
            AICall.created_at, AICall.error).where(AICall.success.is_(False),
            AICall.error.is_not(None)).order_by(AICall.id.desc()).limit(15))).all()
    counts = dict(counts)
    return {"total": sum(counts.values()), "delivered": counts.get("delivered", 0),
            "withheld": counts.get("withheld", 0), "shadow": counts.get("shadow", 0),
            "pending": sum(counts.get(k, 0) for k in ("pending", "generating", "queued", "sending", "retry")),
            "failed": sum(counts.get(k, 0) for k in ("failed", "uncertain", "cancelled")),
            "rows": [dict(zip(("id", "ticket_id", "created_at", "status", "reason", "channel", "model", "tokens_in", "tokens_out", "latency_ms"), row)) for row in rows],
            "errors": [dict(zip(("id", "ticket_id", "created_at", "reason"), row)) for row in legacy]}
