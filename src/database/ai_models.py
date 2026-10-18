"""Durable automatic-reply attempts, one per inbound message."""
from datetime import datetime
from sqlalchemy import DateTime, ForeignKey, Integer, JSON, String
from sqlalchemy.orm import Mapped, mapped_column
from src.database.models import Base


class AIAutoTurn(Base):
    __tablename__ = "ai_auto_turns"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("tickets.id", ondelete="CASCADE"), index=True)
    message_id: Mapped[int] = mapped_column(ForeignKey("messages.id", ondelete="CASCADE"), unique=True)
    status: Mapped[str] = mapped_column(String(24), default="pending", index=True)
    reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ai_call_id: Mapped[int | None] = mapped_column(ForeignKey("ai_calls.id", ondelete="SET NULL"), nullable=True)
    outbound_job_id: Mapped[int | None] = mapped_column(ForeignKey("outbound_jobs.id", ondelete="SET NULL"), nullable=True)
    snapshot: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
