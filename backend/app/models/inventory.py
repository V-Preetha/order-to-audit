"""Stock adjustments, the append-only stock ledger and reservations (design 4.5, migration 0005).

`stock_movements` is the only place stock exists: on-hand is a SUM over it. Rows are inserted, never updated or deleted;
a mistake is corrected by a count adjustment, and only dispatches and purchase receipts have a reversal row.
"""
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (BigInteger, CheckConstraint, Date, DateTime, ForeignKey, ForeignKeyConstraint, Identity, Index, Numeric, Text,
                        UniqueConstraint, text)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class StockAdjustment(Base):
    __tablename__ = "stock_adjustments"
    __table_args__ = (
        UniqueConstraint("adjustment_no", name="stock_adjustments_adjustment_no_key"),
        UniqueConstraint("id", "item_id", name="stock_adjustments_id_item_id_key"),
        ForeignKeyConstraint(["item_id", "item_kind"], ["items.id", "items.kind"], name="fk_stock_adjustments_item_kind"),
        CheckConstraint("btrim(adjustment_no) <> ''", name="ck_stock_adjustments_no_not_blank"),
        CheckConstraint("kind IN ('opening','count')", name="ck_stock_adjustments_kind"),
        CheckConstraint("qty_delta_base <> 0 AND qty_delta_base <> 'NaN'", name="ck_stock_adjustments_delta_nonzero"),
        CheckConstraint("kind <> 'opening' OR qty_delta_base > 0", name="ck_stock_adjustments_opening_positive"),
        CheckConstraint("unit_cost IS NULL OR (unit_cost >= 0 AND unit_cost <> 'NaN')", name="ck_stock_adjustments_unit_cost"),
        CheckConstraint("item_kind <> 'finished' OR unit_cost IS NULL", name="ck_stock_adjustments_finished_no_cost"),
        CheckConstraint("kind <> 'opening' OR item_kind = 'finished' OR unit_cost IS NOT NULL", name="ck_stock_adjustments_opening_cost"),
        CheckConstraint("btrim(reason) <> ''", name="ck_stock_adjustments_reason_not_blank"),
        Index("ix_stock_adjustments_item_date", "item_id", "adjustment_date"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    adjustment_no: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text)
    item_id: Mapped[int] = mapped_column(BigInteger)
    item_kind: Mapped[str] = mapped_column(Text)
    qty_delta_base: Mapped[Decimal] = mapped_column(Numeric(14, 3))
    unit_cost: Mapped[Decimal | None] = mapped_column(Numeric(14, 4))
    reason: Mapped[str] = mapped_column(Text)
    adjustment_date: Mapped[date] = mapped_column(Date)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))


class StockMovement(Base):
    __tablename__ = "stock_movements"
    __table_args__ = (
        UniqueConstraint("posting_key", name="stock_movements_posting_key_key"),
        UniqueConstraint("id", "item_id", name="stock_movements_id_item_id_key"),
        UniqueConstraint("id", "item_id", "movement_type", name="stock_movements_id_item_id_movement_type_key"),
        UniqueConstraint("reverses_movement_id", name="stock_movements_reverses_movement_id_key"),
        ForeignKeyConstraint(["item_id", "item_kind"], ["items.id", "items.kind"], name="fk_stock_movements_item_kind"),
        ForeignKeyConstraint(["purchase_line_id", "item_id"], ["purchase_lines.id", "purchase_lines.item_id"], name="fk_stock_movements_purchase_line_item"),
        ForeignKeyConstraint(["invoice_line_id", "item_id"], ["invoice_lines.id", "invoice_lines.item_id"], name="fk_stock_movements_invoice_line_item"),
        ForeignKeyConstraint(["adjustment_id", "item_id"], ["stock_adjustments.id", "stock_adjustments.item_id"], name="fk_stock_movements_adjustment_item"),
        ForeignKeyConstraint(["production_run_id"], ["production_runs.id"], name="fk_stock_movements_production_run"),
        ForeignKeyConstraint(["reverses_movement_id", "item_id", "movement_type"],
                             ["stock_movements.id", "stock_movements.item_id", "stock_movements.movement_type"], name="fk_stock_movements_reverses"),
        CheckConstraint("btrim(posting_key) <> ''", name="ck_stock_movements_posting_key_not_blank"),
        CheckConstraint("movement_type IN ('opening','count_adjustment','purchase_receipt','production_issue','production_scrap','production_output','dispatch')",
                        name="ck_stock_movements_movement_type"),
        CheckConstraint("qty_base <> 0 AND qty_base <> 'NaN'", name="ck_stock_movements_qty"),
        CheckConstraint("(value IS NULL OR value <> 'NaN') AND (unit_cost IS NULL OR unit_cost <> 'NaN')", name="ck_stock_movements_finite"),
        CheckConstraint("num_nonnulls(purchase_line_id, production_run_id, invoice_line_id, adjustment_id) = 1", name="ck_stock_movements_one_source"),
        CheckConstraint(
            "(movement_type = 'purchase_receipt' AND purchase_line_id IS NOT NULL) "
            "OR (movement_type IN ('production_issue','production_scrap','production_output') AND production_run_id IS NOT NULL) "
            "OR (movement_type = 'dispatch' AND invoice_line_id IS NOT NULL) "
            "OR (movement_type IN ('opening','count_adjustment') AND adjustment_id IS NOT NULL)", name="ck_stock_movements_type_source"),
        CheckConstraint(
            "movement_type IN ('opening','count_adjustment') "
            "OR (movement_type IN ('purchase_receipt','production_issue','production_scrap') AND item_kind <> 'finished') "
            "OR (movement_type IN ('production_output','dispatch') AND item_kind = 'finished')", name="ck_stock_movements_type_kind"),
        CheckConstraint("(item_kind = 'finished') = (value IS NULL)", name="ck_stock_movements_valued_iff_stock_item"),
        CheckConstraint("(value IS NULL) = (unit_cost IS NULL)", name="ck_stock_movements_cost_pair"),
        CheckConstraint("item_kind <> 'finished' OR qty_base = trunc(qty_base)", name="ck_stock_movements_whole_packets"),
        CheckConstraint(
            "reverses_movement_id IS NOT NULL OR movement_type = 'count_adjustment' "
            "OR (movement_type IN ('opening','purchase_receipt','production_output') AND qty_base > 0) "
            "OR (movement_type IN ('production_issue','production_scrap','dispatch') AND qty_base < 0)", name="ck_stock_movements_sign"),
        CheckConstraint("value IS NULL OR value = 0 OR (value > 0) = (qty_base > 0)", name="ck_stock_movements_value_sign"),
        CheckConstraint("value IS NULL OR unit_cost = round(value / qty_base, 4)", name="ck_stock_movements_value_unit_cost"),
        CheckConstraint("reverses_movement_id IS NULL OR movement_type IN ('dispatch','purchase_receipt')", name="ck_stock_movements_reversal_types"),
        Index("uq_stock_movements_receipt_once", "purchase_line_id", unique=True,
              postgresql_where=text("movement_type = 'purchase_receipt' AND reverses_movement_id IS NULL")),
        Index("uq_stock_movements_dispatch_once", "invoice_line_id", unique=True,
              postgresql_where=text("movement_type = 'dispatch' AND reverses_movement_id IS NULL")),
        Index("uq_stock_movements_output_once", "production_run_id", unique=True,
              postgresql_where=text("movement_type = 'production_output' AND reverses_movement_id IS NULL")),
        Index("uq_stock_movements_adjustment_once", "adjustment_id", unique=True, postgresql_where=text("reverses_movement_id IS NULL")),
        Index("ix_stock_movements_item_cover", "item_id", postgresql_include=["qty_base", "value"]),
        Index("ix_stock_movements_item_date", "item_id", "movement_date", "id"),
        Index("ix_stock_movements_date", "movement_date", "id"),
        Index("ix_stock_movements_run", "production_run_id", postgresql_where=text("production_run_id IS NOT NULL")),
        Index("ix_stock_movements_invoice_line", "invoice_line_id", postgresql_where=text("invoice_line_id IS NOT NULL")),
        Index("ix_stock_movements_purchase_line", "purchase_line_id", postgresql_where=text("purchase_line_id IS NOT NULL")),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    posting_key: Mapped[str] = mapped_column(Text)
    item_id: Mapped[int] = mapped_column(BigInteger)
    item_kind: Mapped[str] = mapped_column(Text)
    movement_date: Mapped[date] = mapped_column(Date)
    movement_type: Mapped[str] = mapped_column(Text)
    qty_base: Mapped[Decimal] = mapped_column(Numeric(14, 3))
    unit_cost: Mapped[Decimal | None] = mapped_column(Numeric(14, 4))
    value: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    batch_no: Mapped[str | None] = mapped_column(Text)
    purchase_line_id: Mapped[int | None] = mapped_column(BigInteger)
    production_run_id: Mapped[int | None] = mapped_column(BigInteger)
    invoice_line_id: Mapped[int | None] = mapped_column(BigInteger)
    adjustment_id: Mapped[int | None] = mapped_column(BigInteger)
    reverses_movement_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))


class StockReservation(Base):
    __tablename__ = "stock_reservations"
    __table_args__ = (
        UniqueConstraint("order_line_id", name="stock_reservations_order_line_id_key"),
        ForeignKeyConstraint(["order_line_id", "item_id"], ["order_lines.id", "order_lines.item_id"], name="fk_stock_reservations_order_line_item"),
        CheckConstraint("qty_base > 0 AND qty_base <> 'NaN'", name="ck_stock_reservations_qty_positive"),
        CheckConstraint("status IN ('active','released','fulfilled')", name="ck_stock_reservations_status"),
        CheckConstraint("(status = 'active') = (closed_at IS NULL)", name="ck_stock_reservations_closed"),
        CheckConstraint("closed_at IS NULL OR closed_at >= created_at", name="ck_stock_reservations_closed_after_created"),
        Index("ix_stock_reservations_active_item", "item_id", postgresql_where=text("status = 'active'")),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    order_line_id: Mapped[int] = mapped_column(BigInteger)
    item_id: Mapped[int] = mapped_column(BigInteger)
    qty_base: Mapped[Decimal] = mapped_column(Numeric(14, 3))
    status: Mapped[str] = mapped_column(Text, server_default=text("'active'"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
