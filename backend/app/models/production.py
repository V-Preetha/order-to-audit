"""Production runs and overheads (design 4.8, migration 0004).

Runs hold the plan (snapshots taken at creation) and the good output. Actual consumption is NOT stored here:
it is derived from stock-ledger movements (migration 0005).
"""
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (BigInteger, CheckConstraint, Date, DateTime, ForeignKey, ForeignKeyConstraint, Identity, Index, Numeric, Text,
                        UniqueConstraint, text)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.masters import UnitCode


class ProductionRun(Base):
    __tablename__ = "production_runs"
    __table_args__ = (
        UniqueConstraint("run_no", name="production_runs_run_no_key"),
        ForeignKeyConstraint(["bom_id", "sku_item_id"], ["boms.id", "boms.sku_item_id"], name="fk_production_runs_bom_sku"),
        CheckConstraint("btrim(run_no) <> ''", name="ck_production_runs_run_no_not_blank"),
        CheckConstraint("planned_batches > 0 AND planned_batches <> 'NaN'", name="ck_production_runs_planned_batches_positive"),
        CheckConstraint("expected_output_packets > 0 AND expected_output_packets <> 'NaN'", name="ck_production_runs_expected_output_positive"),
        CheckConstraint("tolerance_pct BETWEEN 0 AND 100", name="ck_production_runs_tolerance"),
        CheckConstraint("good_output_packets IS NULL OR (good_output_packets >= 0 AND good_output_packets <> 'NaN')",
                        name="ck_production_runs_good_output_nonnegative"),
        CheckConstraint("status IN ('in_progress','completed')", name="ck_production_runs_status"),
        CheckConstraint("(status = 'completed') = (good_output_packets IS NOT NULL AND completed_at IS NOT NULL)", name="ck_production_runs_completion"),
        CheckConstraint("(completed_at IS NULL) = (completed_by IS NULL)", name="ck_production_runs_completed_by_pair"),
        CheckConstraint("completed_at IS NULL OR completed_at >= started_at", name="ck_production_runs_completed_after_started"),
        CheckConstraint("status = 'completed' OR (good_output_packets IS NULL AND completed_at IS NULL AND completed_by IS NULL AND completion_note IS NULL)",
                        name="ck_production_runs_in_progress_blank"),
        CheckConstraint("status <> 'completed' OR good_output_packets > 0 OR btrim(coalesce(completion_note, '')) <> ''",
                        name="ck_production_runs_zero_output_note"),
        Index("ix_production_runs_status_started", "status", "started_at"),
        Index("ix_production_runs_bom", "bom_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    run_no: Mapped[str] = mapped_column(Text)
    sku_item_id: Mapped[int] = mapped_column(BigInteger)
    bom_id: Mapped[int] = mapped_column(BigInteger)
    planned_batches: Mapped[Decimal] = mapped_column(Numeric(8, 3))
    expected_output_packets: Mapped[Decimal] = mapped_column(Numeric(14, 3))
    tolerance_pct: Mapped[Decimal] = mapped_column(Numeric(5, 2))
    good_output_packets: Mapped[Decimal | None] = mapped_column(Numeric(14, 3))
    completion_note: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, server_default=text("'in_progress'"))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))
    started_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    notes: Mapped[str | None] = mapped_column(Text)


class ProductionRunLine(Base):
    """Append-only snapshot of what the BOM called for (expected 0 = unplanned material)."""
    __tablename__ = "production_run_lines"
    __table_args__ = (
        UniqueConstraint("run_id", "item_id", name="production_run_lines_run_id_item_id_key"),
        CheckConstraint("item_kind IN ('raw','packaging')", name="ck_production_run_lines_item_kind"),
        CheckConstraint("expected_qty_base >= 0 AND expected_qty_base <> 'NaN'", name="ck_production_run_lines_expected_nonnegative"),
        ForeignKeyConstraint(["item_id", "item_kind"], ["items.id", "items.kind"], name="fk_production_run_lines_item_kind"),
        ForeignKeyConstraint(["item_id", "unit"], ["items.id", "items.base_unit"], name="fk_production_run_lines_item_unit"),
        Index("ix_production_run_lines_item", "item_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("production_runs.id"))
    item_id: Mapped[int] = mapped_column(BigInteger)
    item_kind: Mapped[str] = mapped_column(Text)
    unit: Mapped[str] = mapped_column(UnitCode)
    expected_qty_base: Mapped[Decimal] = mapped_column(Numeric(14, 3))


class ProductionOverhead(Base):
    """Direct (run_id) or shared (period_month); values frozen, only voidable."""
    __tablename__ = "production_overheads"
    __table_args__ = (
        CheckConstraint("kind IN ('labour','gas_power','other')", name="ck_production_overheads_kind"),
        CheckConstraint("amount > 0 AND amount <> 'NaN'", name="ck_production_overheads_amount_positive"),
        CheckConstraint("(run_id IS NOT NULL) <> (period_month IS NOT NULL)", name="ck_production_overheads_target"),
        CheckConstraint("period_month IS NULL OR extract(day FROM period_month) = 1", name="ck_production_overheads_period_first_of_month"),
        CheckConstraint("description IS NULL OR btrim(description) <> ''", name="ck_production_overheads_description_not_blank"),
        CheckConstraint("(voided_at IS NULL) = (voided_by IS NULL)", name="ck_production_overheads_void_pair"),
        CheckConstraint("voided_at IS NULL OR voided_at >= created_at", name="ck_production_overheads_voided_after_created"),
        Index("ix_production_overheads_run", "run_id"),
        Index("ix_production_overheads_period", "period_month"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("production_runs.id"))
    period_month: Mapped[date | None] = mapped_column(Date)
    kind: Mapped[str] = mapped_column(Text)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    description: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    voided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    voided_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
