"""Database models for tickets and messages."""

from datetime import datetime
from typing import List

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, LargeBinary, String, Text, JSON, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Base declarative class for SQLAlchemy models."""


class Team(Base):
    """Represents a team (project) in the system."""

    __tablename__ = "teams"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )

    users: Mapped[List["User"]] = relationship(
        back_populates="team", cascade="all, delete-orphan"
    )
    templates: Mapped[List["ReplyTemplate"]] = relationship(
        back_populates="team", cascade="all, delete-orphan"
    )


class User(Base):
    """A customer, reachable on exactly one channel.

    See src/channels/identity.py for why `tg_id` survived the arrival of other
    channels: it is the internal routing key, unique and non-null, and it is a
    real Telegram id only when `channel` says so. Off-Telegram customers get a
    negative one, since Telegram's are always positive and the two ranges
    therefore cannot collide.
    """

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tg_id: Mapped[int] = mapped_column(
        BigInteger, unique=True, nullable=False, index=True
    )
    # Which pipe this customer talks to us through: "telegram", "widget",
    # "email"… Stored per customer rather than per ticket, because a person
    # writing from two channels is two conversations that happen to be the
    # same human — merging them is a separate feature with its own UI.
    channel: Mapped[str] = mapped_column(
        String(20), default="telegram", nullable=False, index=True
    )
    # The address on that channel, as the channel spells it. A chat id, an
    # email address, a widget session token — hence a string.
    channel_user_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True, index=True
    )
    username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Who they say they are. On Telegram this comes from the account; on the
    # widget it is typed into a form or handed over by the host page, and the
    # difference matters — see `identity_verified`.
    email: Mapped[str | None] = mapped_column(String(320), nullable=True, index=True)
    # The customer's id in the host's own system, when their page passes one.
    # Support's most useful question is "which of your users is this", and the
    # site already knows the answer.
    external_id: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    # Whether that identity was proved rather than claimed. A widget runs on a
    # page the visitor controls: anyone can open the console and call
    # identify({email: "ceo@..."}). Verified means the host's *server* signed
    # it with a shared secret. An operator resetting access on the strength of
    # an unverified email is the incident this flag exists to prevent.
    identity_verified: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    # Work nickname. Bridges someone's personal Telegram account and the name
    # their organisation knows them by; shown on the ticket header.
    work_username: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    team_id: Mapped[int | None] = mapped_column(
        ForeignKey("teams.id", ondelete="SET NULL"), nullable=True, index=True
    )
    local_user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)  # Unique within team
    # Links a PERSONAL account to its owner's WORK account. When set, THIS user
    # is the personal/secondary one, and linked_to_user_id points at the work
    # account. Lets admins identify who's writing when someone messages from
    # their personal TG because their work account is unavailable.
    linked_to_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # Pending link target: the work account's @username (or tg_id as string) that
    # this personal account should link to once it appears in the bot. Resolved
    # automatically on the work account's first /start. Cleared once linked.
    pending_work_username: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True
    )
    language: Mapped[str | None] = mapped_column(String(10), nullable=True)  # 'ru', 'uk', 'en'
    is_verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)  # False = blocked
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )

    team: Mapped["Team | None"] = relationship(back_populates="users")
    tickets: Mapped[List["Ticket"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class Ticket(Base):
    """Represents a support ticket with category and thread-based communication."""

    __tablename__ = "tickets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    team_id: Mapped[int | None] = mapped_column(
        ForeignKey("teams.id", ondelete="SET NULL"), nullable=True, index=True
    )  # Denormalized for quick access
    category: Mapped[str] = mapped_column(String(50), nullable=False)  # user-selected category
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False)  # active, closed, blocked
    # Workflow stage WITHIN an active ticket. Deliberately separate from
    # `status`: status is the lifecycle (open vs closed) that the bot and every
    # existing query rely on, while stage adds board granularity on top without
    # touching that logic. Closed tickets ignore stage — the board derives their
    # column from status instead.
    stage: Mapped[str] = mapped_column(String(20), default="new", nullable=False)  # new, in_progress, waiting
    thread_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    header_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)  # Message ID of ticket header in admin chat
    notification_sent: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)  # Whether initial notification was sent to user
    ticket_code: Mapped[str] = mapped_column(String(32), nullable=False, default="")  # Legacy field, kept for compatibility
    telegram_id: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)  # Legacy field, kept for compatibility
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)  # When ticket was closed
    # Admin categories (multiselect) - stored as JSON array of category strings
    admin_categories: Mapped[List[str] | None] = mapped_column(JSON, nullable=True, default=None)
    # Custom reason text (if "Свой вариант" was selected)
    custom_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Processing time in seconds (calculated when ticket is closed)
    processing_time_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # CSAT: 1–5 rating the user gives after the ticket is closed (None = not rated)
    csat_rating: Mapped[int | None] = mapped_column(Integer, nullable=True)
    csat_rated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # When the SLA policy last acted on this ticket. Not a metric: it is
    # the record that stops an alert firing again on every pass.
    sla_breached_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Operator ownership: which dashboard operator is responsible for this ticket.
    # Auto-assigned to the first responder, reassignable from the ticket card.
    assigned_admin_id: Mapped[int | None] = mapped_column(
        ForeignKey("admin_users.id", ondelete="SET NULL"), nullable=True, index=True
    )

    user: Mapped["User"] = relationship(back_populates="tickets")
    messages: Mapped[List["Message"]] = relationship(
        back_populates="ticket", cascade="all, delete-orphan", passive_deletes=True
    )


class Message(Base):
    """Stores a single message exchanged between a user and admins in a ticket thread."""

    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticket_id: Mapped[int] = mapped_column(
        ForeignKey("tickets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    sender_type: Mapped[str] = mapped_column(String(32), nullable=False)  # "user" or "admin"
    # Internal note: written by an operator, visible only in the dashboard and
    # the admin thread. NEVER relayed to the user, and never used as an example
    # for AI answers that go straight to a user (see src/ai/deflect.py).
    is_internal: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    sender_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    # Who actually said it, when that differs from whose ticket it is.
    #
    # A conversation is normally one person, and this stays empty. But a Slack
    # channel shared with a client's team is one conversation with five people
    # in it, and without a per-message author the transcript reads as one
    # person contradicting themselves. Not Slack-specific: any group
    # conversation has the same problem.
    sender_label: Mapped[str | None] = mapped_column(String(80), nullable=True)
    text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    file_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    file_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    file_name: Mapped[str | None] = mapped_column(String(512), nullable=True)
    file_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    mime_type: Mapped[str | None] = mapped_column(String(255), nullable=True)
    template_id: Mapped[int | None] = mapped_column(
        ForeignKey("reply_templates.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # The channel's own id for a message we sent, and the only thing that
    # makes an unsend possible later: most channels address a message by
    # (conversation, id) and neither part survives the call.
    #
    # A string, because Telegram's is a number, Slack's is a timestamp and
    # email's would be a Message-ID header. `tg_message_id` below is what this
    # replaced; it is backfilled from and no longer read.
    channel_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Superseded by `channel_ref`. Kept so an instance can be rolled back to
    # the previous release without losing the ability to unsend; safe to drop
    # once nobody is doing that.
    tg_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # Set when an operator unsends. The row stays: a support transcript that
    # quietly loses messages is worse than one that says something was removed,
    # and the reports counting admin replies must stay reproducible.
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )

    ticket: Mapped["Ticket"] = relationship(back_populates="messages")
    template: Mapped["ReplyTemplate | None"] = relationship()


class TicketCategory(Base):
    """User-selectable ticket category. Editable from the dashboard.

    Single-name (no per-language variants) by design. `ticket.category` stores
    the name as a snapshot string, so renaming/deleting a category never breaks
    existing tickets.
    """

    __tablename__ = "ticket_categories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    emoji: Mapped[str | None] = mapped_column(String(16), nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )


class CloseReason(Base):
    """Admin-selectable ticket *closure* reason. Editable from the dashboard.

    Mirrors TicketCategory: single-name, ordered, toggleable. The chosen reason
    name(s) are stored as a snapshot list on Ticket.admin_categories, so editing
    or deleting a reason never rewrites history. The special "Свой вариант"
    (free-text) option is NOT a row here — it's handled in code as a sentinel.
    """

    __tablename__ = "close_reasons"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    emoji: Mapped[str | None] = mapped_column(String(16), nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )


class AdminUser(Base):
    """A dashboard operator account — per-person login for the web panel.

    Replaces the single shared ACCESS_PASSWORD with individual accounts so we
    know *who* did what and can revoke one person without rotating everyone's
    access. `role` is stored now but not yet enforced (RBAC lands in Phase 4).
    `tg_id` optionally links this web account to the operator's Telegram admin
    identity (used by audit fields elsewhere that store an admin tg_id).
    """

    __tablename__ = "admin_users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    # argon2 hash string (algorithm + params + salt + digest, self-describing).
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    # Display name shown in the UI; falls back to the email's local part if empty.
    name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    # 'owner' | 'admin' | 'agent' — reserved for Phase 4 RBAC, not enforced yet.
    role: Mapped[str] = mapped_column(String(20), default="admin", nullable=False)
    # Optional link to the operator's Telegram admin id.
    tg_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)  # False = access revoked
    # How this operator likes the dashboard: language, theme, density,
    # keyboard shortcuts. One JSON blob rather than a column each, because the
    # set will keep growing and none of it is ever queried on — it is read
    # whole, with the account, on every page.
    #
    # Personal, not instance-wide: two people sharing an installation can
    # disagree about the interface language without either being wrong.
    preferences: Mapped[dict | None] = mapped_column(JSON, nullable=True, default=None)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )


class AppSetting(Base):
    """Generic key-value settings store, editable from the dashboard at runtime.

    Used for things that would otherwise be hard-coded: brand name, per-category
    admin tags, AI toggles, working hours. Value is always TEXT — callers that
    need structure store JSON and parse it. Keeps the schema stable as we add
    new settings without migrations.
    """

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False, default="")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )
    updated_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)  # admin tg_id


class AICall(Base):
    """Audit log of LLM calls — for cost/quality monitoring and analytics."""

    __tablename__ = "ai_calls"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    use_case: Mapped[str] = mapped_column(String(50), nullable=False, index=True)  # e.g. "suggest_template"
    ticket_id: Mapped[int | None] = mapped_column(
        ForeignKey("tickets.id", ondelete="SET NULL"), nullable=True, index=True
    )
    suggested_template_id: Mapped[int | None] = mapped_column(
        ForeignKey("reply_templates.id", ondelete="SET NULL"), nullable=True
    )
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    tokens_in: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    success: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    accepted: Mapped[bool | None] = mapped_column(Boolean, nullable=True)  # set when admin clicks Apply/Dismiss
    accepted_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)  # admin tg_id
    # For free-form suggestions (suggest_from_history): store the generated text
    # so admin can accept/edit by ai_call_id without smuggling text via callback_data.
    suggested_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Produced while the AI was being evaluated, so it never reached
    # anyone. Kept apart from live calls: mixing them would make the
    # accept rate meaningless, since a shadow call cannot be accepted.
    shadow: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )


class ReplyTemplate(Base):
    """Reusable canned reply for the admin team. Scoped to a team (no global templates)."""

    __tablename__ = "reply_templates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    team_id: Mapped[int] = mapped_column(
        ForeignKey("teams.id", ondelete="CASCADE"), nullable=False, index=True
    )
    category: Mapped[str | None] = mapped_column(String(50), nullable=True, index=True)
    title: Mapped[str] = mapped_column(String(100), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    shortcut: Mapped[str | None] = mapped_column(
        String(32), unique=True, nullable=True, index=True
    )
    usage_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    is_archived: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_by: Mapped[int] = mapped_column(BigInteger, nullable=False)  # admin tg_id
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    team: Mapped["Team"] = relationship(back_populates="templates")


class SavedView(Base):
    """A filter set an operator named and kept.

    Per operator, not per instance: "my overdue" means something different to
    each person, and a shared list would fill up with everyone else's idea of
    what matters. Sharing can come later if anyone asks; starting shared and
    retreating to private is the harder direction.
    """

    __tablename__ = "saved_views"
    __table_args__ = (
        UniqueConstraint("admin_id", "name", name="uq_saved_view_owner_name"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    admin_id: Mapped[int] = mapped_column(
        ForeignKey("admin_users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(60), nullable=False)
    # Which page it belongs to — the same filters mean different things on the
    # list and on the board, so a view is offered only where it applies.
    path: Mapped[str] = mapped_column(String(32), nullable=False)
    # The query string as submitted, without the leading "?". Stored verbatim
    # rather than parsed: the filters change over releases, and a view that
    # survives by being opaque outlives one that has to be migrated.
    query: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )
