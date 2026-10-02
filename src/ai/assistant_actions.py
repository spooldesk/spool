"""A model can propose; only an authenticated, explicit confirmation can apply."""
from datetime import datetime, timedelta
import secrets

from sqlalchemy import func, or_, select

from src.ai.assistant_access import Access, AssistantError, recheck
from src.ai.assistant_schema import ActionRequest
from src.database.assistant_models import AssistantAction
from src.database.db import db_session
from src.database.models import AdminUser, Message, Ticket, User
from src.services import assignment_service, permissions, workflow_service


async def snapshot(session, ticket):
    last_message = (await session.execute(select(func.max(Message.id)).where(Message.ticket_id == ticket.id))).scalar()
    return {"status": ticket.status, "stage": ticket.stage, "assignee_id": ticket.assigned_admin_id,
            "team_id": ticket.team_id, "category": ticket.category, "last_message_id": last_message}


async def allowed(session, access, ticket, kind):
    actor = await session.get(AdminUser, access.actor_id)
    section = "board" if kind == "stage" else "tickets"
    if (not actor or actor.session_version != access.generation
            or not await permissions.can(actor, "assistant", edit=True, session=session)
            or not await permissions.can(actor, "tickets", session=session)
            or not await permissions.can(actor, section, edit=True, session=session)):
        raise AssistantError("assistant_forbidden", 403)
    if not ticket or ticket.status != "active" or not access.permits_category(ticket.category):
        raise AssistantError("assistant_ticket_unavailable", 409)


async def propose(access: Access, action: ActionRequest):
    await recheck(access)
    async with db_session(write=True) as session:
        ticket = await session.get(Ticket, action.ticket_id)
        await allowed(session, access, ticket, action.kind)
        if action.kind == "assign":
            target = action.assignee.strip()
            if not target:
                raise AssistantError("assistant_assignee_ambiguous", 409)
            if target == "none":
                admin, admin_id = None, None
            else:
                if target == "me":
                    candidates = [await session.get(AdminUser, access.actor_id)]
                elif target.isdigit() and 0 < int(target) <= 2**31-1:
                    candidates = [await session.get(AdminUser, int(target))]
                else:
                    candidates = list((await session.execute(select(AdminUser).where(
                        or_(AdminUser.name == target, func.lower(AdminUser.name) == target.lower()),
                        AdminUser.is_active.is_(True)).limit(2))).scalars())
                if len(candidates) != 1 or not candidates[0] or not await assignment_service.eligible_for_ticket(session, ticket, candidates[0]):
                    raise AssistantError("assistant_assignee_ambiguous", 409)
                admin = candidates[0]
                admin_id = admin.id
            proposed = {"assignee_id": admin_id, "assignee_name": (admin.name or f"#{admin.id}") if admin else ""}
            if ticket.assigned_admin_id == admin_id:
                raise AssistantError("assistant_no_change", 409)
        elif action.kind == "stage":
            if not workflow_service.is_valid_stage(action.stage):
                raise AssistantError("assistant_invalid_plan", 400)
            if ticket.stage == action.stage:
                raise AssistantError("assistant_no_change", 409)
            proposed = {"stage": action.stage}
        else:
            proposed = {"status": "closed", "customer_notification": True}
        row = AssistantAction(id=secrets.token_hex(16), operator_id=access.actor_id,
            session_version=access.generation, ticket_id=ticket.id, kind=action.kind,
            expected=await snapshot(session, ticket), proposed=proposed,
            expires_at=datetime.utcnow()+timedelta(minutes=10))
        session.add(row)
    return public(row)


def public(row):
    # The server derives every displayed field. The model cannot supply confirmation text.
    return {"id": row.id, "ticket_id": row.ticket_id, "kind": row.kind,
            "before": {k: row.expected[k] for k in ("status", "stage", "assignee_id")},
            "after": row.proposed, "expires_at": row.expires_at.isoformat() + "Z"}


async def confirm(access: Access, action_id: str, *, cancel=False):
    await recheck(access)
    async with db_session(write=True) as session:
        row = (await session.execute(select(AssistantAction).where(AssistantAction.id == action_id)
                                     .with_for_update())).scalar_one_or_none()
        if not row or row.operator_id != access.actor_id or row.session_version != access.generation:
            raise AssistantError("assistant_forbidden", 403)
        if row.status != "pending" or row.expires_at <= datetime.utcnow():
            raise AssistantError("assistant_confirmation_expired", 409)
        if cancel:
            row.status = "cancelled"
            return {"ok": True, "cancelled": True}
        ticket = (await session.execute(select(Ticket).where(Ticket.id == row.ticket_id)
                                       .with_for_update())).scalar_one_or_none()
        await allowed(session, access, ticket, row.kind)
        if await snapshot(session, ticket) != row.expected:
            raise AssistantError("assistant_context_changed", 409)
        if row.kind == "assign":
            ok = await assignment_service.assign_in_session(session, ticket.id, row.proposed["assignee_id"])
        elif row.kind == "stage":
            ok = await workflow_service.set_stage_in_session(session, ticket.id, row.proposed["stage"])
        elif row.kind == "close":
            from src.services.ticket_closure import close_in_session
            user = await session.get(User, ticket.user_id)
            ok = await close_in_session(session, ticket, user, operator_id=access.actor_id)
            # The normal durable outbox delivers the standard closure notice after commit.
        else:
            ok = False
        if not ok:
            raise AssistantError("assistant_context_changed", 409)
        row.status, row.applied_at = "applied", datetime.utcnow()
    return {"ok": True, "ticket_id": row.ticket_id, "kind": row.kind}
