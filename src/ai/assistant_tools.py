"""Bounded, permission-filtered data readers for the dashboard assistant."""
from datetime import datetime, timedelta
import json

from sqlalchemy import and_, case, exists, func, or_, select
from sqlalchemy.orm import aliased

from src.ai.assistant_access import Access, AssistantError
from src.ai.assistant_schema import ToolRequest
from src.ai.scrubber import scrub
from src.database.db import db_session, settings
from src.database.dialect import contains_ci
from src.database.models import (AdminUser, AICall, AppSetting, Article, CloseReason,
                                 Message, ReplyTemplate, Team, Ticket, TicketCategory, User)
from src.database.workspace_models import CustomRole
from src.services import permissions, settings_service

LIMIT = 20


def clean(value, limit=1000):
    return scrub(str(value or ""))[:limit]


def contains(column, query):
    return contains_ci(column, query, url=settings.database_url)


def category_filter(column, access):
    return and_(True, *(~contains(func.coalesce(column, ""), part) for part in access.blacklist))


def need(access, section):
    if not access.can(section):
        raise AssistantError("assistant_forbidden", 403)


def source(title, url):
    return {"title": clean(title, 160), "url": url}


def unanswered_filter():
    incoming, reply = aliased(Message), aliased(Message)
    return exists(select(incoming.id).where(
        incoming.ticket_id == Ticket.id, incoming.sender_type == "user",
        incoming.deleted_at.is_(None), incoming.is_internal.is_(False),
        ~exists(select(reply.id).where(reply.ticket_id == incoming.ticket_id,
            reply.sender_type == "admin", reply.is_internal.is_(False),
            reply.delivery_status == "delivered", reply.deleted_at.is_(None),
            reply.created_at >= incoming.created_at)).correlate(incoming)))


def ticket_metadata(ticket, channel):
    return {"id": ticket.id, "status": ticket.status, "stage": ticket.stage,
            "category": clean(ticket.category, 100), "team_id": ticket.team_id,
            "assignee_id": ticket.assigned_admin_id, "channel": channel,
            "created_at": ticket.created_at.isoformat(),
            "closed_at": ticket.closed_at.isoformat() if ticket.closed_at else None}


def ticket_filters(access, args):
    filters = [category_filter(Ticket.category, access), Ticket.status.in_(["active", "closed"])]
    if args.status != "all": filters.append(Ticket.status == args.status)
    if args.stage: filters.append(Ticket.stage == args.stage)
    if args.channel: filters.append(User.channel == args.channel)
    if args.unassigned: filters.append(Ticket.assigned_admin_id.is_(None))
    if args.unanswered: filters.append(unanswered_filter())
    if args.team_id: filters.append(Ticket.team_id == args.team_id)
    if args.ticket_ids: filters.append(Ticket.id.in_(args.ticket_ids))
    if args.query:
        q = args.query.strip()
        match = [contains(Ticket.category, q), exists(select(Message.id).where(
            Message.ticket_id == Ticket.id, Message.deleted_at.is_(None), Message.is_internal.is_(False),
            contains(Message.text, q)))]
        if q.lstrip('#').isdigit() and 0 < int(q.lstrip('#')) <= 2**31-1:
            match.append(Ticket.id == int(q.lstrip('#')))
        filters.append(or_(*match))
    return filters


async def excerpts(session, ids):
    """One bounded query: first/latest public text per sender and ticket."""
    ranked = select(Message.ticket_id, Message.sender_type, func.substr(Message.text, 1, 500).label("text"),
        func.row_number().over(partition_by=(Message.ticket_id, Message.sender_type), order_by=Message.id).label("first"),
        func.row_number().over(partition_by=(Message.ticket_id, Message.sender_type), order_by=Message.id.desc()).label("last"))\
        .where(Message.ticket_id.in_(ids), Message.deleted_at.is_(None), Message.is_internal.is_(False),
               Message.sender_type.in_(["user", "admin"]),
               or_(Message.sender_type == "user", Message.delivery_status == "delivered")).subquery()
    found = {ident:{} for ident in ids}
    for row in (await session.execute(select(ranked).where(or_(ranked.c.first == 1, ranked.c.last == 1)))).mappings():
        item = found[row["ticket_id"]]
        if row["sender_type"] == "user" and row["first"] == 1:
            item["opening"] = clean(row["text"], 320)
        if row["sender_type"] == "user" and row["last"] == 1:
            item["latest_customer"] = clean(row["text"], 220)
        if row["sender_type"] == "admin" and row["last"] == 1:
            item["latest_reply"] = clean(row["text"], 280)
    return found


async def search_tickets(access: Access, args: ToolRequest):
    need(access, "tickets")
    filters = ticket_filters(access, args)
    async with db_session() as session:
        total = (await session.execute(select(func.count()).select_from(Ticket)
                 .join(User, Ticket.user_id == User.id).where(*filters))).scalar_one()
        rows = (await session.execute(select(Ticket, User.channel).join(User, Ticket.user_id == User.id)
                .where(*filters).order_by(Ticket.id.desc()).limit(LIMIT))).all()
        previews = await excerpts(session, [t.id for t, _ in rows])
    return {"total_matching": total, "limit": LIMIT, "tickets": [{**ticket_metadata(t, c), **previews[t.id]} for t, c in rows]}, [
        source(f"#{t.id} · {t.category}", f"/tickets/{t.id}") for t, _ in rows]


async def analyze_tickets(access: Access, args: ToolRequest):
    need(access, "tickets")
    since = datetime.utcnow()-timedelta(days=args.days)
    filters = ticket_filters(access, args)
    if not args.ticket_ids:
        filters.append(Ticket.created_at >= since)
    async with db_session() as session:
        total = (await session.execute(select(func.count()).select_from(Ticket).join(User)
            .where(*filters))).scalar_one()
        rows = (await session.execute(select(Ticket, User.channel).join(User)
            .where(*filters).order_by(Ticket.created_at.desc(), Ticket.id.desc()).offset(args.offset).limit(60))).all()
        previews = await excerpts(session, [t.id for t, _ in rows])
    items = [{**ticket_metadata(t, c), **previews[t.id]} for t, c in rows]
    return {"total_matching":total, "analyzed_count":len(items), "offset":args.offset,
            "next_offset":args.offset+len(items) if args.offset+len(items)<total else None,
            "period_start_utc":None if args.ticket_ids else since.isoformat(), "tickets":items,
            "limitations":"Text excerpts, not full transcripts: first customer message (320 chars), latest customer (220), latest delivered reply (280). Up to 60 tickets ordered newest first. No internal/deleted messages or original ticket attachments. Use ticket for full detail; recurring-issue counts from excerpts are observations within this sample only."}, [
                source(f"#{t.id} · {t.category}", f"/tickets/{t.id}") for t, _ in rows]


async def ticket_context(access: Access, ticket_id: int | None):
    need(access, "tickets")
    async with db_session() as session:
        row = (await session.execute(select(Ticket, User.channel).join(User, Ticket.user_id == User.id)
               .where(Ticket.id == ticket_id, Ticket.status.in_(["active", "closed"]),
                      category_filter(Ticket.category, access)))).first()
        if not row:
            raise AssistantError("assistant_ticket_unavailable", 404)
        ticket, channel = row
        messages = list((await session.execute(select(Message).where(Message.ticket_id == ticket.id,
            Message.deleted_at.is_(None), Message.is_internal.is_(False))
            .order_by(Message.id.desc()).limit(12))).scalars())
    data = ticket_metadata(ticket, channel)
    data.update(messages=[{"role": m.sender_type, "text": clean(m.text, 900),
                           "has_attachment": bool(m.file_id), "at": m.created_at.isoformat()}
                          for m in reversed(messages)],
                limitations="Latest 12 public messages only, up to 900 characters each. Attachments and internal notes excluded.")
    return data, [source(f"#{ticket.id} · {ticket.category}", f"/tickets/{ticket.id}")]


async def workload(access: Access, days: int, section="overview"):
    if section not in {"overview", "reports", "board"}:
        section = "overview"
    need(access, section)
    now = datetime.utcnow()
    start, previous = now - timedelta(days=days), now - timedelta(days=2*days)
    allowed = category_filter(Ticket.category, access)
    async with db_session() as session:
        current = (await session.execute(select(
            func.count(), func.sum(case((Ticket.stage == "waiting", 1), else_=0)),
            func.sum(case((Ticket.assigned_admin_id.is_(None), 1), else_=0)))
            .where(allowed, Ticket.status == "active"))).one()
        unanswered = (await session.execute(select(func.count()).select_from(Ticket).where(
            allowed, Ticket.status == "active", unanswered_filter()))).scalar_one()
        recent = (await session.execute(select(func.count()).select_from(Ticket).where(
            allowed, Ticket.created_at >= start, Ticket.created_at < now))).scalar_one()
        prior = (await session.execute(select(func.count()).select_from(Ticket).where(
            allowed, Ticket.created_at >= previous, Ticket.created_at < start))).scalar_one()
        closed = (await session.execute(select(func.count()).select_from(Ticket).where(
            allowed, Ticket.status == "closed", Ticket.closed_at >= start, Ticket.closed_at < now))).scalar_one()
        previous_closed = (await session.execute(select(func.count()).select_from(Ticket).where(
            allowed, Ticket.status == "closed", Ticket.closed_at >= previous, Ticket.closed_at < start))).scalar_one()
        groups = (await session.execute(select(Team.name, Ticket.assigned_admin_id, func.count())
            .select_from(Ticket).outerjoin(Team, Ticket.team_id == Team.id)
            .where(allowed, Ticket.status == "active").group_by(Team.name, Ticket.assigned_admin_id)
            .order_by(func.count().desc()).limit(50))).all()
    return {"as_of_utc": now.isoformat(), "period_days": days, "period_start_utc": start.isoformat(),
            "current_queue": {"active": current[0], "waiting": int(current[1] or 0),
                              "unassigned": int(current[2] or 0), "unanswered": unanswered},
            "created_in_period": recent, "created_in_previous_equal_period": prior,
            "closed_in_period": closed,
            "closed_in_previous_equal_period": previous_closed,
            "current_workload_top_50": [{"team": clean(n, 100), "assignee_id": a, "active": c} for n, a, c in groups],
            "limitations": "Excluded AI-blacklisted categories. Counts are exact within that scope; grouped breakdown is capped at 50. No SLA trend or cause measurements are available."}, [source(section, permissions.SECTIONS[section][1])]


async def section_snapshot(access: Access, section: str, query="", days=30):
    need(access, section)
    if section in {"overview", "reports", "board"}:
        return await workload(access, days, section)
    if section == "tickets":
        return await search_tickets(access, ToolRequest(tool="search_tickets", query=query))
    url = (permissions.SECTIONS.get(section) or (None, {
        "integrations": "/manage/integrations", "operators": "/manage/admins", "roles": "/manage/roles"
    }.get(section, "/assistant")))[1]
    async with db_session() as session:
        if section in {"articles", "templates"}:
            model = Article if section == "articles" else ReplyTemplate
            where = [category_filter(model.category, access)]
            if section == "templates": where.append(model.is_archived.is_(False))
            if query: where.append(or_(contains(model.title, query), contains(model.body, query)))
            rows = list((await session.execute(select(model).where(*where).order_by(model.id.desc()).limit(10))).scalars())
            data = [{"id": r.id, "title": clean(r.title, 200), "body": clean(r.body, 1200),
                     "category": clean(r.category, 100)} for r in rows]
            refs = [source(r.title, f"{url}/{r.id}/edit") for r in rows]
        elif section == "users":
            # Profile labels only, no delivery addresses, contact tokens or account identifiers.
            rows = list((await session.execute(select(User).where(
                contains(func.coalesce(User.username, ""), query) if query else True)
                .order_by(User.id.desc()).limit(LIMIT))).scalars())
            data = [{"id": r.id, "name": clean(r.username, 100), "channel": r.channel,
                     "team_id": r.team_id} for r in rows]
            refs = [source(f"Customer #{r.id}", f"/users/{r.id}") for r in rows]
        elif section in {"teams", "categories", "close_reasons"}:
            model = {"teams": Team, "categories": TicketCategory, "close_reasons": CloseReason}[section]
            rows = list((await session.execute(select(model).order_by(model.id).limit(50))).scalars())
            data = [{"id": r.id, "name": clean(r.name, 100), "active": getattr(r, "is_active", True)} for r in rows]
            refs = [source(section, url)]
        elif section == "roles":
            rows = list((await session.execute(select(CustomRole).order_by(CustomRole.name).limit(30))).scalars())
            data = [{"name": clean(r.name, 100), "permissions": r.permissions} for r in rows]
            refs = [source(section, url)]
        elif section == "operators":
            rows = list((await session.execute(select(AdminUser).order_by(AdminUser.id).limit(50))).scalars())
            data = [{"id": r.id, "name": clean(r.name, 100), "role": r.role, "active": r.is_active} for r in rows]
            refs = [source(section, url)]
        elif section == "integrations":
            data = []
            for channel in ("email", "slack", "telegram"):
                # Read only the non-secret enabled bit. Never decrypt connection settings.
                row = await session.get(AppSetting, "integration_" + channel)
                try: state = json.loads(row.value).get("enabled", False) if row else None
                except (ValueError, TypeError, AttributeError): state = None
                data.append({"channel": channel, "saved_enabled": state})
            row = await session.get(AppSetting, "widget_enabled")
            data.append({"channel": "widget", "saved_enabled": row.value == "1" if row else None})
            refs = [source(section, url)]
        elif section == "ai":
            # Only aggregate usage; never include rejected/suggested message text.
            row = (await session.execute(select(func.count(), func.sum(AICall.tokens_in), func.sum(AICall.tokens_out))
                .where(AICall.created_at >= datetime.utcnow()-timedelta(days=days)))).one()
            data = {"days": days, "calls": row[0], "tokens_in": int(row[1] or 0), "tokens_out": int(row[2] or 0)}
            refs = [source(section, url)]
        elif section in {"settings", "ai_settings"}:
            # Explicit allowlist; never enumerate the settings table or return hosts/keys.
            data = {"ai_enabled": await settings_service.get_ai_enabled(),
                    "assistant_enabled": True, "provider": access.provider, "model": access.model}
            refs = [source(section, url)]
        else:
            raise AssistantError("assistant_unsupported", 400)
    return {"section": section, "records": data, "limited_sample": True}, refs


async def read_tool(access: Access, args: ToolRequest):
    if args.tool == "search_tickets": return await search_tickets(access, args)
    if args.tool == "analyze_tickets": return await analyze_tickets(access, args)
    if args.tool == "ticket": return await ticket_context(access, args.ticket_id)
    if args.tool == "workload": return await workload(access, args.days, args.section or "overview")
    return await section_snapshot(access, args.section, args.query, args.days)
