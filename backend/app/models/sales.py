"""Orders, invoices and credit notes (design 4.6, migration 0003).

Documents only: creating an order or an invoice never changes stock. Issued invoices are snapshots;
the database allows only `status` and the dispatch fields to change, and cancellation is a credit note.
"""
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (CHAR, BigInteger, Boolean, CheckConstraint, Date, DateTime, ForeignKey, ForeignKeyConstraint, Identity,
                        Index, Integer, Numeric, Text, UniqueConstraint, Uuid, text)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.identity import GSTIN, STATE_CODE

FY = "^[0-9]{4}-[0-9]{2}$"
HSN = "^[0-9]{4}([0-9]{2}([0-9]{2})?)?$"


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (
        UniqueConstraint("order_no", name="orders_order_no_key"),
        UniqueConstraint("client_uuid", name="orders_client_uuid_key"),
        UniqueConstraint("id", "customer_id", name="orders_id_customer_id_key"),
        CheckConstraint("btrim(order_no) <> ''", name="ck_orders_order_no_not_blank"),
        CheckConstraint("status IN ('submitted','on_hold','approved','rejected','invoiced','dispatched','cancelled')", name="ck_orders_status"),
        CheckConstraint("status <> 'on_hold' OR btrim(coalesce(hold_reason, '')) <> ''", name="ck_orders_hold_reason"),
        CheckConstraint("(decided_by IS NULL) = (decided_at IS NULL)", name="ck_orders_decided_pair"),
        CheckConstraint("status IN ('submitted','on_hold') OR decided_by IS NOT NULL", name="ck_orders_decided_after_hold"),
        Index("ix_orders_salesman_created", "salesman_id", text("created_at DESC")),
        Index("ix_orders_status_created", "status", "created_at"),
        Index("ix_orders_customer", "customer_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    order_no: Mapped[str] = mapped_column(Text)
    client_uuid: Mapped[UUID] = mapped_column(Uuid)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"))
    salesman_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    order_date: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(Text, server_default=text("'submitted'"))
    hold_reason: Mapped[str | None] = mapped_column(Text)
    decided_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))


class OrderLine(Base):
    """Finished SKUs only, in whole packets. Editable while the order is submitted or on hold."""
    __tablename__ = "order_lines"
    __table_args__ = (
        UniqueConstraint("order_id", "item_id", name="order_lines_order_id_item_id_key"),
        UniqueConstraint("id", "item_id", name="order_lines_id_item_id_key"),
        UniqueConstraint("id", "order_id", "item_id", "qty", "unit_price", name="order_lines_id_order_id_item_id_key"),
        CheckConstraint("item_kind = 'finished'", name="ck_order_lines_item_kind"),
        CheckConstraint("qty > 0 AND qty <> 'NaN'", name="ck_order_lines_qty_positive"),
        CheckConstraint("qty = trunc(qty)", name="ck_order_lines_whole_packets"),
        CheckConstraint("unit_price >= 0 AND unit_price <> 'NaN'", name="ck_order_lines_unit_price_nonnegative"),
        ForeignKeyConstraint(["item_id", "item_kind"], ["items.id", "items.kind"], name="fk_order_lines_item_kind"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"))
    item_id: Mapped[int] = mapped_column(BigInteger)
    item_kind: Mapped[str] = mapped_column(Text, server_default=text("'finished'"))
    qty: Mapped[Decimal] = mapped_column(Numeric(14, 3))
    unit_price: Mapped[Decimal] = mapped_column(Numeric(12, 2))


class Invoice(Base):
    __tablename__ = "invoices"
    __table_args__ = (
        UniqueConstraint("invoice_no", name="invoices_invoice_no_key"),
        UniqueConstraint("financial_year", "invoice_seq", name="invoices_financial_year_invoice_seq_key"),
        UniqueConstraint("order_id", name="invoices_order_id_key"),
        UniqueConstraint("id", "customer_id", name="invoices_id_customer_id_key"),
        UniqueConstraint("id", "order_id", "supply_type", name="invoices_id_order_id_supply_type_key"),
        UniqueConstraint("id", "taxable_total", "cgst_total", "sgst_total", "igst_total", "round_off", "grand_total",
                         name="invoices_id_amounts_key"),
        ForeignKeyConstraint(["order_id", "customer_id"], ["orders.id", "orders.customer_id"], name="fk_invoices_order_customer"),
        CheckConstraint(f"financial_year ~ '{FY}'", name="ck_invoices_financial_year_format"),
        CheckConstraint(f"CASE WHEN financial_year ~ '{FY}' THEN mod(substr(financial_year, 3, 2)::int + 1, 100) = substr(financial_year, 6, 2)::int ELSE true END",
                        name="ck_invoices_financial_year_consecutive"),
        CheckConstraint("invoice_seq >= 1", name="ck_invoices_invoice_seq_positive"),
        CheckConstraint("invoice_no = 'INV/' || substr(financial_year, 3, 2) || '-' || substr(financial_year, 6, 2) || '/' "
                        "|| repeat('0', greatest(0, 4 - length(invoice_seq::text))) || invoice_seq::text", name="ck_invoices_invoice_no_format"),
        CheckConstraint("length(invoice_no) <= 16", name="ck_invoices_invoice_no_length"),
        CheckConstraint(f"CASE WHEN financial_year ~ '{FY}' THEN CASE WHEN substr(financial_year, 1, 4)::int BETWEEN 1 AND 9998 "
                        "THEN invoice_date >= make_date(substr(financial_year, 1, 4)::int, 4, 1) "
                        "AND invoice_date < make_date(substr(financial_year, 1, 4)::int + 1, 4, 1) ELSE false END ELSE true END",
                        name="ck_invoices_date_in_financial_year"),
        CheckConstraint("btrim(customer_name) <> ''", name="ck_invoices_customer_name_not_blank"),
        CheckConstraint(f"seller_state_code ~ '{STATE_CODE}'", name="ck_invoices_seller_state_code"),
        CheckConstraint(f"place_of_supply_state ~ '{STATE_CODE}'", name="ck_invoices_place_of_supply_state_code"),
        CheckConstraint(f"seller_gstin ~ '{GSTIN}'", name="ck_invoices_seller_gstin_format"),
        CheckConstraint("substr(seller_gstin, 1, 2) = seller_state_code", name="ck_invoices_seller_gstin_state"),
        CheckConstraint(f"customer_gstin IS NULL OR customer_gstin ~ '{GSTIN}'", name="ck_invoices_customer_gstin_format"),
        CheckConstraint("customer_gstin IS NULL OR substr(customer_gstin, 1, 2) = place_of_supply_state", name="ck_invoices_customer_gstin_state"),
        CheckConstraint("supply_type IN ('intra','inter')", name="ck_invoices_supply_type"),
        CheckConstraint("(supply_type = 'intra') = (place_of_supply_state = seller_state_code)", name="ck_invoices_supply_type_matches_states"),
        CheckConstraint("(supply_type = 'intra' AND igst_total = 0 AND cgst_total = sgst_total) "
                        "OR (supply_type = 'inter' AND cgst_total = 0 AND sgst_total = 0)", name="ck_invoices_tax_split"),
        CheckConstraint("taxable_total >= 0 AND cgst_total >= 0 AND sgst_total >= 0 AND igst_total >= 0 AND grand_total >= 0 "
                        "AND taxable_total <> 'NaN' AND cgst_total <> 'NaN' AND sgst_total <> 'NaN' AND igst_total <> 'NaN' AND grand_total <> 'NaN'",
                        name="ck_invoices_amounts_nonnegative"),
        CheckConstraint("grand_total = taxable_total + cgst_total + sgst_total + igst_total + round_off", name="ck_invoices_grand_total"),
        CheckConstraint("abs(round_off) <= 0.50 AND (round_off = 0 OR grand_total = round(grand_total, 0))", name="ck_invoices_round_off"),
        CheckConstraint("status IN ('issued','cancelled')", name="ck_invoices_status"),
        CheckConstraint("(dispatched_at IS NULL) = (dispatched_by IS NULL)", name="ck_invoices_dispatch_pair"),
        Index("ix_invoices_customer_date", "customer_id", "invoice_date"),
        Index("ix_invoices_date", "invoice_date"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    invoice_no: Mapped[str] = mapped_column(Text)
    financial_year: Mapped[str] = mapped_column(Text)
    invoice_seq: Mapped[int] = mapped_column(Integer)
    order_id: Mapped[int] = mapped_column(BigInteger)
    customer_id: Mapped[int] = mapped_column(BigInteger)
    invoice_date: Mapped[date] = mapped_column(Date)
    seller_gstin: Mapped[str] = mapped_column(CHAR(15))
    seller_state_code: Mapped[str] = mapped_column(CHAR(2))
    customer_name: Mapped[str] = mapped_column(Text)
    customer_gstin: Mapped[str | None] = mapped_column(CHAR(15))
    customer_address: Mapped[str | None] = mapped_column(Text)
    place_of_supply_state: Mapped[str] = mapped_column(CHAR(2))
    supply_type: Mapped[str] = mapped_column(Text)
    taxable_total: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    cgst_total: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    sgst_total: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    igst_total: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    round_off: Mapped[Decimal] = mapped_column(Numeric(6, 2), server_default=text("0"))
    grand_total: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    status: Mapped[str] = mapped_column(Text, server_default=text("'issued'"))
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    dispatched_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))


class InvoiceLine(Base):
    """Append-only snapshot of one sale line: description, HSN, rate and amounts as issued."""
    __tablename__ = "invoice_lines"
    __table_args__ = (
        UniqueConstraint("order_line_id", name="invoice_lines_order_line_id_key"),
        UniqueConstraint("id", "item_id", name="invoice_lines_id_item_id_key"),
        ForeignKeyConstraint(["invoice_id", "order_id", "supply_type"], ["invoices.id", "invoices.order_id", "invoices.supply_type"],
                             name="fk_invoice_lines_invoice"),
        ForeignKeyConstraint(["order_line_id", "order_id", "item_id", "qty", "unit_price"],
                             ["order_lines.id", "order_lines.order_id", "order_lines.item_id", "order_lines.qty", "order_lines.unit_price"],
                             name="fk_invoice_lines_order_line"),
        CheckConstraint("supply_type IN ('intra','inter')", name="ck_invoice_lines_supply_type"),
        CheckConstraint("btrim(description) <> ''", name="ck_invoice_lines_description_not_blank"),
        CheckConstraint(f"hsn_code ~ '{HSN}'", name="ck_invoice_lines_hsn_code"),
        CheckConstraint("qty > 0 AND qty <> 'NaN'", name="ck_invoice_lines_qty_positive"),
        CheckConstraint("qty = trunc(qty)", name="ck_invoice_lines_whole_packets"),
        CheckConstraint("unit_price >= 0 AND unit_price <> 'NaN'", name="ck_invoice_lines_unit_price_nonnegative"),
        CheckConstraint("gst_rate BETWEEN 0 AND 40", name="ck_invoice_lines_gst_rate"),
        CheckConstraint("taxable_value = round(qty * unit_price, 2)", name="ck_invoice_lines_taxable_value"),
        CheckConstraint("(supply_type = 'intra' AND igst_amount = 0 AND cgst_amount = sgst_amount AND cgst_amount = round(taxable_value * gst_rate / 200, 2)) "
                        "OR (supply_type = 'inter' AND cgst_amount = 0 AND sgst_amount = 0 AND igst_amount = round(taxable_value * gst_rate / 100, 2))",
                        name="ck_invoice_lines_tax_math"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    invoice_id: Mapped[int] = mapped_column(BigInteger)
    order_id: Mapped[int] = mapped_column(BigInteger)
    order_line_id: Mapped[int] = mapped_column(BigInteger)
    item_id: Mapped[int] = mapped_column(BigInteger)
    supply_type: Mapped[str] = mapped_column(Text)
    description: Mapped[str] = mapped_column(Text)
    hsn_code: Mapped[str] = mapped_column(Text)
    qty: Mapped[Decimal] = mapped_column(Numeric(14, 3))
    unit_price: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    taxable_value: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    gst_rate: Mapped[Decimal] = mapped_column(Numeric(5, 2))
    cgst_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    sgst_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    igst_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2))


class CreditNote(Base):
    """Cancels a whole invoice. Append-only; exists exactly when its invoice is cancelled."""
    __tablename__ = "credit_notes"
    __table_args__ = (
        UniqueConstraint("credit_note_no", name="credit_notes_credit_note_no_key"),
        UniqueConstraint("invoice_id", name="credit_notes_invoice_id_key"),
        CheckConstraint("btrim(credit_note_no) <> ''", name="ck_credit_notes_number_not_blank"),
        CheckConstraint("length(credit_note_no) <= 16", name="ck_credit_notes_number_length"),
        CheckConstraint("btrim(reason) <> ''", name="ck_credit_notes_reason_not_blank"),
        ForeignKeyConstraint(["invoice_id", "taxable_total", "cgst_total", "sgst_total", "igst_total", "round_off", "grand_total"],
                             ["invoices.id", "invoices.taxable_total", "invoices.cgst_total", "invoices.sgst_total",
                              "invoices.igst_total", "invoices.round_off", "invoices.grand_total"], name="fk_credit_notes_invoice_amounts"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    credit_note_no: Mapped[str] = mapped_column(Text)
    invoice_id: Mapped[int] = mapped_column(BigInteger)
    issue_date: Mapped[date] = mapped_column(Date)
    reason: Mapped[str] = mapped_column(Text)
    taxable_total: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    cgst_total: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    sgst_total: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    igst_total: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    round_off: Mapped[Decimal] = mapped_column(Numeric(6, 2), server_default=text("0"))
    grand_total: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    restock: Mapped[bool] = mapped_column(Boolean)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
