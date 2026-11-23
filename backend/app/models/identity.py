"""Identity, settings and shared infrastructure tables (design 4.1, migration 0001)."""
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (CHAR, BigInteger, Boolean, CheckConstraint, DateTime, Identity, Numeric, PrimaryKeyConstraint,
                        SmallInteger, Text, Uuid, UniqueConstraint, text)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

# Same patterns as the migration; kept identical so a typo shows up as a test failure, not a data bug.
STATE_CODE = "^(0[1-9]|[12][0-9]|3[0-8]|97)$"
GSTIN = "^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$"


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("username", name="users_username_key"),
        CheckConstraint("username = lower(username)", name="ck_users_username_lowercase"),
        CheckConstraint("btrim(username) <> ''", name="ck_users_username_not_blank"),
        CheckConstraint("role IN ('salesman','admin','production','auditor','owner')", name="ck_users_role"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    username: Mapped[str] = mapped_column(Text)
    full_name: Mapped[str] = mapped_column(Text)
    password_hash: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))


class Settings(Base):
    """The single configuration row (id is always 1)."""
    __tablename__ = "settings"
    __table_args__ = (
        CheckConstraint("id = 1", name="ck_settings_single_row"),
        CheckConstraint("btrim(factory_name) <> ''", name="ck_settings_factory_name_not_blank"),
        CheckConstraint(f"factory_state_code ~ '{STATE_CODE}'", name="ck_settings_state_code"),
        CheckConstraint(f"factory_gstin ~ '{GSTIN}'", name="ck_settings_gstin_format"),
        CheckConstraint("substr(factory_gstin, 1, 2) = factory_state_code", name="ck_settings_gstin_state"),
        CheckConstraint("variance_tolerance_pct BETWEEN 0 AND 100", name="ck_settings_tolerance"),
    )

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True, autoincrement=False)
    factory_name: Mapped[str] = mapped_column(Text)
    factory_gstin: Mapped[str] = mapped_column(CHAR(15))
    factory_state_code: Mapped[str] = mapped_column(CHAR(2))
    variance_tolerance_pct: Mapped[Decimal] = mapped_column(Numeric(5, 2), server_default=text("3.00"))
    invoice_round_to_rupee: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))


class DocumentSequence(Base):
    """Gap-free counters per scope, e.g. 'INV:2026-27'. Incremented by services under a row lock."""
    __tablename__ = "document_sequences"
    __table_args__ = (
        CheckConstraint("btrim(scope) <> ''", name="ck_document_sequences_scope_not_blank"),
        CheckConstraint("last_value >= 0", name="ck_document_sequences_last_value"),
    )

    scope: Mapped[str] = mapped_column(Text, primary_key=True)
    last_value: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))


class RequestLog(Base):
    """Idempotency record: one row per client request id, written in the action's own transaction."""
    __tablename__ = "request_log"
    __table_args__ = (
        CheckConstraint("fingerprint ~ '^[0-9a-f]{64}$'", name="ck_request_log_fingerprint"),
        CheckConstraint("(result_entity_type IS NULL) = (result_entity_id IS NULL)", name="ck_request_log_result_pair"),
    )

    request_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    action: Mapped[str] = mapped_column(Text)
    fingerprint: Mapped[str] = mapped_column(Text)
    result_entity_type: Mapped[str | None] = mapped_column(Text)
    result_entity_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))


class StatusTransition(Base):
    """One legal edge of a lifecycle. '(new)' as from_status is the legal initial status (design 8)."""
    __tablename__ = "status_transitions"
    __table_args__ = (
        PrimaryKeyConstraint("entity", "from_status", "to_status", name="status_transitions_pkey"),
        CheckConstraint("from_status <> to_status", name="ck_status_transitions_distinct"),
        CheckConstraint("to_status <> '(new)'", name="ck_status_transitions_new_is_initial_only"),
    )

    entity: Mapped[str] = mapped_column(Text)
    from_status: Mapped[str] = mapped_column(Text)
    to_status: Mapped[str] = mapped_column(Text)
