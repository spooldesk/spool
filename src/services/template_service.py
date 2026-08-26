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
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.database.models import (
    Message as MessageRecord,
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


async def _send_to_customer(user: User, text: str):
    """Deliver to the customer on their own channel.

    Every caller here used to name Telegram's exceptions to decide what an
    operator was told, which made this file a Telegram file. Now the adapter
    answers in a vocabulary that survives the next channel.
    """
    from src.channels import get as get_channel
    from src.channels.identity import address_of, channel_of

    return await get_channel(channel_of(user)).send_text(address_of(user), text)


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


async def list_for_team(
    session: AsyncSession,
    team_id: int,
    *,
    include_archived: bool = False,
    page: int = 0,
    page_size: int = 10,
) -> Sequence[ReplyTemplate]:
    """Paginated list for CRUD UI. Sorted by usage_count DESC, then title."""
    stmt = select(ReplyTemplate).where(ReplyTemplate.team_id == team_id)
    if not include_archived:
        stmt = stmt.where(ReplyTemplate.is_archived.is_(False))
    stmt = (
        stmt.order_by(
            ReplyTemplate.usage_count.desc(),
            ReplyTemplate.title.asc(),
        )
        .offset(page * page_size)
        .limit(page_size)
    )
    result = await session.execute(stmt)
    return result.scalars().all()


async def count_for_team(
    session: AsyncSession,
    team_id: int,
    *,
    include_archived: bool = False,
) -> int:
    """Count templates for pagination."""
    stmt = select(func.count(ReplyTemplate.id)).where(ReplyTemplate.team_id == team_id)
    if not include_archived:
        stmt = stmt.where(ReplyTemplate.is_archived.is_(False))
    result = await session.execute(stmt)
    return int(result.scalar_one())


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


async def apply_text(
    session: AsyncSession,
    bot: Bot,
    *,
    ticket_id: int,
    text: str,
    admin_tg_id: int,
    admin_chat_id: int,
    source_label: str | None = None,
) -> ApplyResult:
    """Send an ad-hoc text reply to the ticket's user + mirror + log.

    Used for AI-generated free-form replies (suggest_from_history) and for any
    other path that needs to send arbitrary text through the same audit funnel
    as templated replies. The mirror prefix changes to indicate the source
    (e.g. "🤖 AI" instead of the template label).

    `source_label` defaults to the localised word for "reply" — resolved here
    rather than in the signature, since a default argument would be evaluated
    at import time and freeze whatever language was active then.
    """
    source_label = source_label or _t("src_reply")
    text = (text or "").strip()
    if not text:
        return ApplyResult(success=False, error=_t("e_empty_text"))

    result = await session.execute(
        select(Ticket)
        .where(Ticket.id == ticket_id)
        .options(selectinload(Ticket.user))
    )
    ticket: Optional[Ticket] = result.scalar_one_or_none()
    if ticket is None:
        return ApplyResult(success=False, error=_t("e_ticket_not_found_id", id=ticket_id))
    if ticket.status != "active":
        return ApplyResult(success=False, error=_t("e_ticket_already_closed", id=ticket_id))

    # 1. Send to the customer, down whichever pipe they arrived on. The bot
    #    below is the operators' Telegram, which is a different thing.
    delivery = await _send_to_customer(ticket.user, text)
    if not delivery.ok:
        return ApplyResult(
            success=False,
            error=_delivery_error(delivery),
            rendered_text=text,
        )

    # 2. Mirror into admin thread
    mirror_text = f"📤 {source_label}:\n{text}"
    try:
        kwargs: dict = {"chat_id": admin_chat_id, "text": mirror_text}
        if ticket.thread_id is not None:
            kwargs["message_thread_id"] = ticket.thread_id
            if ticket.header_message_id is not None:
                kwargs["reply_to_message_id"] = ticket.header_message_id
        elif ticket.header_message_id is not None:
            kwargs["reply_to_message_id"] = ticket.header_message_id
        # Nothing to mirror into on an instance with no Telegram. The
        # customer's reply has already gone out on their own channel.
        if bot is not None and admin_chat_id:
            await bot.send_message(**kwargs)
    except Exception as e:
        logger.warning(f"Failed to mirror ad-hoc reply to admin thread: {e}")

    # 3. Persist as a regular admin message (no template_id — it's ad-hoc)
    session.add(
        MessageRecord(
            ticket_id=ticket.id,
            sender_type="admin",
            sender_id=admin_tg_id,
            text=text,
            # Recorded so an operator can unsend this later.
            channel_ref=delivery.ref,
        )
    )
    if ticket.stage == "new":
        # First operator reply moves the card off the "New" column. Done here so
        # every caller — dashboard, bot, AI suggestions — gets it for free.
        ticket.stage = "in_progress"
    await session.flush()

    logger.info(
        f"✅ Sent ad-hoc text reply to ticket #{ticket.id} "
        f"by admin {admin_tg_id} (source={source_label!r}, {len(text)} chars)"
    )
    return ApplyResult(
        success=True,
        rendered_text=text,
    )


async def apply(
    session: AsyncSession,
    bot: Bot,
    *,
    template_id: int,
    ticket_id: int,
    admin_tg_id: int,
    admin_chat_id: int,
    admin_first_name: Optional[str] = None,
) -> ApplyResult:
    """Send a template to the ticket's user + log to DB + mirror in admin thread.

    This is the single funnel for templated replies. The button handler, /t command,
    and future AI-suggest all converge here.
    """
    # Load template + ticket (with user + team) in one go
    template = await get_by_id(session, template_id)
    if template is None or template.is_archived:
        return ApplyResult(success=False, error=_t("e_template_archived"))

    result = await session.execute(
        select(Ticket)
        .where(Ticket.id == ticket_id)
        .options(selectinload(Ticket.user))
    )
    ticket: Optional[Ticket] = result.scalar_one_or_none()
    if ticket is None:
        return ApplyResult(success=False, error=_t("e_ticket_not_found_id", id=ticket_id))
    if ticket.status != "active":
        return ApplyResult(success=False, error=_t("e_ticket_already_closed", id=ticket_id))
    if ticket.team_id and template.team_id != ticket.team_id:
        return ApplyResult(
            success=False,
            error=_t("e_template_other_team"),
        )

    team: Optional[Team] = None
    if ticket.team_id:
        team_result = await session.execute(select(Team).where(Team.id == ticket.team_id))
        team = team_result.scalar_one_or_none()

    context = build_context(ticket, ticket.user, team, admin_first_name)
    rendered = render(template.body, context)

    # 1. Send to user (private chat)
    delivery = await _send_to_customer(ticket.user, rendered)
    if not delivery.ok:
        return ApplyResult(
            success=False,
            error=_delivery_error(delivery),
            rendered_text=rendered,
        )

    # 2. Mirror into admin thread so other admins see what was sent
    mirror_text = f"📤 {_t('src_template', title=template.title)}:\n{rendered}"
    try:
        kwargs: dict = {"chat_id": admin_chat_id, "text": mirror_text}
        if ticket.thread_id is not None:
            kwargs["message_thread_id"] = ticket.thread_id
            if ticket.header_message_id is not None:
                kwargs["reply_to_message_id"] = ticket.header_message_id
        elif ticket.header_message_id is not None:
            kwargs["reply_to_message_id"] = ticket.header_message_id
        # Nothing to mirror into on an instance with no Telegram. The
        # customer's reply has already gone out on their own channel.
        if bot is not None and admin_chat_id:
            await bot.send_message(**kwargs)
    except Exception as e:
        # Mirror failure must NOT break the user-facing send. Just log.
        logger.warning(
            f"Failed to mirror template #{template.id} into admin thread "
            f"for ticket #{ticket.id}: {e}"
        )

    # 3. Persist as a regular admin message + bump usage_count
    session.add(
        MessageRecord(
            ticket_id=ticket.id,
            sender_type="admin",
            sender_id=admin_tg_id,
            text=rendered,
            template_id=template.id,
            channel_ref=delivery.ref,
        )
    )
    template.usage_count += 1
    if ticket.stage == "new":
        ticket.stage = "in_progress"
    await session.flush()

    logger.info(
        f"✅ Applied template #{template.id} to ticket #{ticket.id} "
        f"by admin {admin_tg_id}. usage_count={template.usage_count}"
    )
    return ApplyResult(
        success=True,
        rendered_text=rendered,
    )
