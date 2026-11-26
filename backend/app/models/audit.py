"""The append-only audit log (design 4.9, migration 0006): who did what to which entity, when, with before/after snapshots."""
from datetime import datetime
from uuid import UUID

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Identity, Index, Text, Uuid, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class AuditLog(Base):
    __tablename__ = "audit_log"
    __table_args__ = (
        CheckConstraint("btrim(action) <> ''", name="ck_audit_log_action_not_blank"),
        CheckConstraint("btrim(entity_type) <> ''", name="ck_audit_log_entity_type_not_blank"),
        CheckConstraint("before IS NULL OR jsonb_typeof(before) = 'object'", name="ck_audit_log_before_object"),
        CheckConstraint("after IS NULL OR jsonb_typeof(after) = 'object'", name="ck_audit_log_after_object"),
        Index("ix_audit_log_entity", "entity_type", "entity_id", "occurred_at"),
        Index("ix_audit_log_user", "user_id", "occurred_at"),
        Index("ix_audit_log_request", "request_id", postgresql_where=text("request_id IS NOT NULL")),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    action: Mapped[str] = mapped_column(Text)
    entity_type: Mapped[str] = mapped_column(Text)
    entity_id: Mapped[int] = mapped_column(BigInteger)
    before: Mapped[dict | None] = mapped_column(JSONB)
    after: Mapped[dict | None] = mapped_column(JSONB)
    request_id: Mapped[UUID | None] = mapped_column(Uuid)
