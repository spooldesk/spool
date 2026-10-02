"""Reply template service: CRUD, rendering, and application to tickets.

This is the single point of truth for sending a templated reply to a user.
Handlers (button callback, /t command, future AI-suggest) all call `apply()`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Sequence

# The Bot here is the *operators'* Telegram — the admin group every reply is
# mirrored into. Reaching the customer goes through their channel adapter, and
# this file no longer knows what that adapter raises when it fails.
from aiogram import Bot
from sqlalchemy import case, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.database.models import (
    ReplyTemplate,
    Team,
    Ticket,
    User,
)

logger = logging.getLogger(__name__)


def _t(key: str, **kwargs) -> str:
    """Operator-facing text. These errors surface in the dashboard, so they
    follow the instance's UI language, not the ticket author's."""
    from src.dashboard.i18n import t as dash_t
    return dash_t(key, **kwargs)


def _delivery_error(delivery) -> str:
    """A failed delivery, in the operator's language.

    The key is localised; the detail is not, because a Bot API sentence has no
    translation and hiding it would leave nothing to debug with.
    """
    key = delivery.error_key or "e_send_failed"
    detail = delivery.error_detail or ""
    if delivery.blocked:
        return _t(key, tg=detail)
    return f"{_t(key)}{f': {detail}' if detail else ''}"



# ==================== Public types ====================


@dataclass(slots=True)
class ApplyResult:
    """Outcome of TemplateService.apply()."""

    success: bool
    error: Optional[str] = None
    rendered_text: Optional[str] = None


class TemplateError(Exception):
    """Domain error for template operations (validation, not-found, conflict)."""


# ==================== Internal helpers ====================


class _SafeDict(dict):
    """str.format_map helper: missing placeholders → empty string instead of KeyError."""

    def __missing__(self, key: str) -> str:  # type: ignore[override]
        logger.debug(f"Template placeholder '{{{key}}}' missing from context — substituted empty string")
        return ""


# ==================== Rendering ====================


def build_context(
    ticket: Ticket,
    user: User,
    team: Optional[Team],
    admin_first_name: Optional[str] = None,
) -> dict:
    """Build the context dict used to render template placeholders.

    Available placeholders for templates:
        {user_name}   — @username or fallback id
        {ticket_id}   — ticket numeric id
        {category}    — ticket category (user-selected, e.g. "VPN")
        {team}        — team name
        {admin_name}  — admin's first name (who applies the template)
    """
    user_name = f"@{user.username}" if user.username else f"id{user.tg_id}"
    return {
        "user_name": user_name,
        "ticket_id": ticket.id,
        "category": ticket.category or "",
        "team": team.name if team else "",
        "admin_name": admin_first_name or "",
    }


def render(body: str, context: dict) -> str:
    """Render template body. Missing placeholders become empty strings, never raises."""
    try:
        return body.format_map(_SafeDict(context))
    except (ValueError, IndexError) as e:
        # Bad format spec like {0} or {:invalid} — return raw body
        logger.warning(f"Template render error: {e}. Returning body as-is.")
        return body


# ==================== Read queries ====================


async def list_for_ticket(
    session: AsyncSession,
    ticket: Ticket,
    limit: int = 5,
) -> Sequence[ReplyTemplate]:
    """Top-N templates for inline picker, ordered by category match then usage."""
    if not ticket.team_id:
        return []
    stmt = (
        select(ReplyTemplate)
        .where(
            ReplyTemplate.team_id == ticket.team_id,
            ReplyTemplate.is_archived.is_(False),
        )
        .order_by(
            case((ReplyTemplate.category == ticket.category, 0), else_=1),
            ReplyTemplate.usage_count.desc(),
            ReplyTemplate.created_at.desc(),
        )
        .limit(limit)
    )
    result = await session.execute(stmt)
    return result.scalars().all()


async def get_by_id(session: AsyncSession, template_id: int) -> Optional[ReplyTemplate]:
    result = await session.execute(
        select(ReplyTemplate).where(ReplyTemplate.id == template_id)
    )
    return result.scalar_one_or_none()


# ==================== CRUD ====================


async def create(
    session: AsyncSession,
    *,
    team_id: int,
    title: str,
    body: str,
    category: Optional[str],
    created_by: int,
) -> ReplyTemplate:
    """Create a new template. Raises TemplateError on validation."""
    title = (title or "").strip()
    body = (body or "").strip()

    if not title:
        raise TemplateError(_t("e_title_empty"))
    if len(title) > 100:
        raise TemplateError(_t("e_title_too_long"))
    if not body:
        raise TemplateError(_t("e_body_empty"))
    if len(body) > 4096:
        raise TemplateError(_t("e_body_too_long"))

    template = ReplyTemplate(
        team_id=team_id,
        title=title,
        body=body,
        category=category,
        shortcut=None,
        usage_count=0,
        is_archived=False,
        created_by=created_by,
    )
    session.add(template)
    await session.flush()
    logger.info(f"Created template #{template.id} for team {team_id} (title={title!r})")
    return template


async def update(
    session: AsyncSession,
    template_id: int,
    *,
    title: Optional[str] = None,
    body: Optional[str] = None,
    category: Optional[str] = None,
) -> ReplyTemplate:
    """Update fields that are not None. Raises TemplateError on validation."""
    template = await get_by_id(session, template_id)
    if template is None:
        raise TemplateError(_t("e_template_not_found", id=template_id))

    if title is not None:
        title = title.strip()
        if not title or len(title) > 100:
            raise TemplateError(_t("e_title_length"))
        template.title = title
    if body is not None:
        body = body.strip()
        if not body or len(body) > 4096:
            raise TemplateError(_t("e_body_length"))
        template.body = body
    if category is not None:
        template.category = category or None

    template.updated_at = datetime.utcnow()
    await session.flush()
    logger.info(f"Updated template #{template.id}")
    return template


async def archive(session: AsyncSession, template_id: int) -> None:
    """Soft-delete: mark as archived. Keeps history in messages.template_id intact."""
    template = await get_by_id(session, template_id)
    if template is None:
        raise TemplateError(_t("e_template_not_found", id=template_id))
    template.is_archived = True
    template.updated_at = datetime.utcnow()
    await session.flush()
    logger.info(f"Archived template #{template.id}")


# ==================== Apply (the main path) ====================


async def _persist_and_deliver(session, bot, ticket, text, *, admin_tg_id,
                               admin_chat_id, operator_id=None, template_id=None, reply_to_id=None):
    from src.channels.identity import address_of, channel_of
    from src.services import outbound

    try:
        job = await outbound.enqueue(
            session, ticket_id=ticket.id, channel=channel_of(ticket.user),
            address=address_of(ticket.user), text=text, sender_id=admin_tg_id,
            operator_id=operator_id, template_id=template_id,
            reply_to_id=reply_to_id,
            require_active=True,
        )
    except (LookupError, ValueError) as exc:
        return ApplyResult(success=False, error=str(exc), rendered_text=text)
    job_id, message_id = job.id, job.message_id
    # The provider is never called until the reply and its intent are durable.
    await session.commit()
    delivery = await outbound.deliver(job_id)
    if not delivery.ok:
        return ApplyResult(success=False, error=_delivery_error(delivery), rendered_text=text)
    # Keep the caller's identity map accurate after the separate delivery txn.
    await session.refresh(ticket)
    if bot is not None and admin_chat_id:
        try:
            from src.services.announce_service import announce_message
            await announce_message(bot, ticket.id, message_id)
        except Exception:
            logger.warning("Could not mirror outgoing message %s", message_id)
    return ApplyResult(success=True, rendered_text=text)


async def apply_text(
    session: AsyncSession, bot: Bot, *, ticket_id: int, text: str,
    admin_tg_id: int, admin_chat_id: int, source_label: str | None = None,
    operator_id: int | None = None,
    reply_to_id: int | None = None,
) -> ApplyResult:
    """Commit a free-form reply before sending it through the delivery queue."""
    text = (text or "").strip()
    if not text:
        return ApplyResult(success=False, error=_t("e_empty_text"))
    from src.database.db import acquire_write_lock
    await acquire_write_lock(session)
    ticket = (await session.execute(select(Ticket).where(Ticket.id == ticket_id)
              .options(selectinload(Ticket.user)))).scalar_one_or_none()
    if ticket is None:
        return ApplyResult(success=False, error=_t("e_ticket_not_found_id", id=ticket_id))
    if ticket.status != "active":
        return ApplyResult(success=False, error=_t("e_ticket_already_closed", id=ticket_id))
    return await _persist_and_deliver(session, bot, ticket, text,
        admin_tg_id=admin_tg_id, admin_chat_id=admin_chat_id, operator_id=operator_id, reply_to_id=reply_to_id)


async def apply(
    session: AsyncSession, bot: Bot, *, template_id: int, ticket_id: int,
    admin_tg_id: int, admin_chat_id: int, admin_first_name: Optional[str] = None,
    operator_id: int | None = None,
) -> ApplyResult:
    """Render and persist a template reply; usage increments only on delivery."""
    from src.database.db import acquire_write_lock
    await acquire_write_lock(session)
    template = await get_by_id(session, template_id)
    if template is None or template.is_archived:
        return ApplyResult(success=False, error=_t("e_template_archived"))
    ticket = (await session.execute(select(Ticket).where(Ticket.id == ticket_id)
              .options(selectinload(Ticket.user)))).scalar_one_or_none()
    if ticket is None:
        return ApplyResult(success=False, error=_t("e_ticket_not_found_id", id=ticket_id))
    if ticket.status != "active":
        return ApplyResult(success=False, error=_t("e_ticket_already_closed", id=ticket_id))
    if ticket.team_id and template.team_id != ticket.team_id:
        return ApplyResult(success=False, error=_t("e_template_other_team"))
    team = await session.get(Team, ticket.team_id) if ticket.team_id else None
    rendered = render(template.body, build_context(ticket, ticket.user, team, admin_first_name))
    result = await _persist_and_deliver(session, bot, ticket, rendered,
        admin_tg_id=admin_tg_id, admin_chat_id=admin_chat_id, operator_id=operator_id,
        template_id=template.id)
    await session.refresh(template)
    return result
