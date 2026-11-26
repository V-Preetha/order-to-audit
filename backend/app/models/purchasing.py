"""Purchases and purchase lines (design 4.4, migration 0005).

A purchase line is append-only and carries the quantity in the item's BASE unit (`qty_base`); the receipt movement that
puts it into stock lives in the stock ledger (app.models.inventory) and must agree with it.
"""
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (BigInteger, CheckConstraint, Date, DateTime, ForeignKey, ForeignKeyConstraint, Identity, Index, Numeric, Text,
                        UniqueConstraint, func, text)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.masters import UnitCode


class Purchase(Base):
    __tablename__ = "purchases"
    __table_args__ = (
        UniqueConstraint("purchase_no", name="purchases_purchase_no_key"),
        CheckConstraint("btrim(purchase_no) <> ''", name="ck_purchases_purchase_no_not_blank"),
        CheckConstraint("btrim(supplier_invoice_no) <> ''", name="ck_purchases_invoice_no_not_blank"),
        CheckConstraint("status IN ('posted','voided')", name="ck_purchases_status"),
        CheckConstraint(
            "(status = 'voided') = (voided_at IS NOT NULL AND voided_by IS NOT NULL AND btrim(coalesce(void_reason, '')) <> '') "
            "AND (status = 'voided' OR (voided_at IS NULL AND voided_by IS NULL AND void_reason IS NULL))", name="ck_purchases_void_columns"),
        CheckConstraint("voided_at IS NULL OR voided_at >= created_at", name="ck_purchases_voided_after_created"),
        Index("uq_purchases_supplier_invoice", "supplier_id", func.lower(func.btrim(text("supplier_invoice_no"))), unique=True),
        Index("ix_purchases_supplier", "supplier_id"),
        Index("ix_purchases_date", "purchase_date"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    purchase_no: Mapped[str] = mapped_column(Text)
    supplier_id: Mapped[int] = mapped_column(ForeignKey("suppliers.id"))
    supplier_invoice_no: Mapped[str] = mapped_column(Text)
    purchase_date: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(Text, server_default=text("'posted'"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    voided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    voided_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    void_reason: Mapped[str | None] = mapped_column(Text)


class PurchaseLine(Base):
    __tablename__ = "purchase_lines"
    __table_args__ = (
        UniqueConstraint("purchase_id", "item_id", name="purchase_lines_purchase_id_item_id_key"),
        UniqueConstraint("id", "item_id", name="purchase_lines_id_item_id_key"),
        ForeignKeyConstraint(["item_id", "item_kind"], ["items.id", "items.kind"], name="fk_purchase_lines_item_kind"),
        ForeignKeyConstraint(["item_id", "base_unit"], ["items.id", "items.base_unit"], name="fk_purchase_lines_item_unit"),
        CheckConstraint("item_kind IN ('raw','packaging')", name="ck_purchase_lines_item_kind"),
        CheckConstraint("qty > 0 AND qty <> 'NaN' AND qty_base > 0 AND qty_base <> 'NaN'", name="ck_purchase_lines_qty_positive"),
        CheckConstraint("rate >= 0 AND rate <> 'NaN'", name="ck_purchase_lines_rate"),
        CheckConstraint("unit_factor(unit, base_unit) IS NOT NULL AND qty_base = round(qty * unit_factor(unit, base_unit), 3)", name="ck_purchase_lines_qty_base"),
        CheckConstraint("amount = round(qty * rate, 2)", name="ck_purchase_lines_amount"),
        CheckConstraint("gst_rate BETWEEN 0 AND 100", name="ck_purchase_lines_gst_rate"),
        CheckConstraint("tax_amount = round(amount * gst_rate / 100, 2)", name="ck_purchase_lines_tax"),
        Index("ix_purchase_lines_item", "item_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    purchase_id: Mapped[int] = mapped_column(ForeignKey("purchases.id"))
    item_id: Mapped[int] = mapped_column(BigInteger)
    item_kind: Mapped[str] = mapped_column(Text)
    base_unit: Mapped[str] = mapped_column(UnitCode)
    qty: Mapped[Decimal] = mapped_column(Numeric(14, 3))
    unit: Mapped[str] = mapped_column(UnitCode)
    qty_base: Mapped[Decimal] = mapped_column(Numeric(14, 3))
    rate: Mapped[Decimal] = mapped_column(Numeric(14, 4))
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    gst_rate: Mapped[Decimal] = mapped_column(Numeric(5, 2), server_default=text("0"))
    tax_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), server_default=text("0"))
