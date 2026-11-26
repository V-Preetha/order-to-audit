"""Payments and payment allocations (design 4.7, migration 0006).

A payment is posted, then (once) reversed; it is never deleted. An allocation ties part of a payment to one invoice of the same
customer; allocations are append-only, and a reversed payment's allocations stay as history but stop counting.
"""
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (BigInteger, CheckConstraint, Date, DateTime, ForeignKey, ForeignKeyConstraint, Identity, Index, Numeric, Text,
                        UniqueConstraint, Uuid, text)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class Payment(Base):
    __tablename__ = "payments"
    __table_args__ = (
        UniqueConstraint("payment_no", name="payments_payment_no_key"),
        UniqueConstraint("client_uuid", name="payments_client_uuid_key"),
        UniqueConstraint("id", "customer_id", name="payments_id_customer_id_key"),
        CheckConstraint("btrim(payment_no) <> ''", name="ck_payments_payment_no_not_blank"),
        CheckConstraint("amount > 0 AND amount <> 'NaN'", name="ck_payments_amount"),
        CheckConstraint("method IN ('cash','upi','bank','cheque')", name="ck_payments_method"),
        CheckConstraint("reference IS NULL OR btrim(reference) <> ''", name="ck_payments_reference_not_blank"),
        CheckConstraint("status IN ('posted','reversed')", name="ck_payments_status"),
        CheckConstraint(
            "(status = 'reversed') = (reversed_at IS NOT NULL AND reversed_by IS NOT NULL AND btrim(coalesce(reverse_reason, '')) <> '') "
            "AND (status = 'reversed' OR (reversed_at IS NULL AND reversed_by IS NULL AND reverse_reason IS NULL))", name="ck_payments_reverse_columns"),
        CheckConstraint("reversed_at IS NULL OR reversed_at >= created_at", name="ck_payments_reversed_after_created"),
        Index("ix_payments_customer", "customer_id"),
        Index("ix_payments_date", "payment_date"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    payment_no: Mapped[str] = mapped_column(Text)
    client_uuid: Mapped[UUID] = mapped_column(Uuid)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"))
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    payment_date: Mapped[date] = mapped_column(Date)
    method: Mapped[str] = mapped_column(Text)
    reference: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, server_default=text("'posted'"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    reversed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reversed_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    reverse_reason: Mapped[str | None] = mapped_column(Text)


class PaymentAllocation(Base):
    __tablename__ = "payment_allocations"
    __table_args__ = (
        UniqueConstraint("payment_id", "invoice_id", name="payment_allocations_payment_id_invoice_id_key"),
        ForeignKeyConstraint(["payment_id", "customer_id"], ["payments.id", "payments.customer_id"], name="fk_payment_allocations_payment_customer"),
        ForeignKeyConstraint(["invoice_id", "customer_id"], ["invoices.id", "invoices.customer_id"], name="fk_payment_allocations_invoice_customer"),
        CheckConstraint("amount > 0 AND amount <> 'NaN'", name="ck_payment_allocations_amount"),
        Index("ix_payment_allocations_invoice", "invoice_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    payment_id: Mapped[int] = mapped_column(BigInteger)
    invoice_id: Mapped[int] = mapped_column(BigInteger)
    customer_id: Mapped[int] = mapped_column(BigInteger)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
