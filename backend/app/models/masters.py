"""Master data: products, items, customers, suppliers (design 4.2, migration 0001)."""
from decimal import Decimal

from sqlalchemy import (CHAR, BigInteger, Boolean, CheckConstraint, ForeignKey, Identity, Index, Integer, Numeric,
                        Text, UniqueConstraint, text)
from sqlalchemy.dialects.postgresql import ENUM
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.identity import GSTIN, STATE_CODE

# The type is created by the migration, never by the models.
UnitCode = ENUM("kg", "g", "l", "ml", "pc", "pkt", name="unit_code", create_type=False)


class Product(Base):
    __tablename__ = "products"
    __table_args__ = (
        UniqueConstraint("name", name="products_name_key"),
        CheckConstraint("btrim(name) <> ''", name="ck_products_name_not_blank"),
        CheckConstraint("hsn_code ~ '^[0-9]{4}([0-9]{2}([0-9]{2})?)?$'", name="ck_products_hsn_code"),
        CheckConstraint("gst_rate BETWEEN 0 AND 40", name="ck_products_gst_rate"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    hsn_code: Mapped[str] = mapped_column(Text)
    gst_rate: Mapped[Decimal] = mapped_column(Numeric(5, 2))


class Item(Base):
    """Raw material, packaging or finished SKU. Quantities elsewhere are always in `base_unit`.

    The three composite UNIQUE constraints are the targets of typed foreign keys in later migrations.
    """
    __tablename__ = "items"
    __table_args__ = (
        UniqueConstraint("code", name="items_code_key"),
        UniqueConstraint("id", "kind", name="items_id_kind_key"),
        UniqueConstraint("id", "base_unit", name="items_id_base_unit_key"),
        UniqueConstraint("id", "product_id", name="items_id_product_id_key"),
        CheckConstraint("btrim(code) <> ''", name="ck_items_code_not_blank"),
        CheckConstraint("btrim(name) <> ''", name="ck_items_name_not_blank"),
        CheckConstraint("kind IN ('raw','packaging','finished')", name="ck_items_kind"),
        CheckConstraint("(kind = 'finished') = (product_id IS NOT NULL)", name="ck_items_finished_has_product"),
        CheckConstraint("kind = 'finished' OR (pack_size_g IS NULL AND packets_per_carton IS NULL AND list_price IS NULL)",
                        name="ck_items_nonfinished_no_sales_fields"),
        CheckConstraint("kind <> 'finished' OR (base_unit = 'pkt' AND list_price IS NOT NULL)",
                        name="ck_items_finished_pkt_and_price"),
        CheckConstraint("pack_size_g IS NULL OR pack_size_g > 0", name="ck_items_pack_size_positive"),
        CheckConstraint("packets_per_carton IS NULL OR packets_per_carton > 0", name="ck_items_packets_per_carton_positive"),
        CheckConstraint("list_price IS NULL OR (list_price >= 0 AND list_price <> 'NaN')", name="ck_items_list_price_nonnegative"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    code: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text)
    base_unit: Mapped[str] = mapped_column(UnitCode)
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id"))
    pack_size_g: Mapped[int | None] = mapped_column(Integer)
    packets_per_carton: Mapped[int | None] = mapped_column(Integer)
    list_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))


class Customer(Base):
    __tablename__ = "customers"
    __table_args__ = (
        CheckConstraint("btrim(name) <> ''", name="ck_customers_name_not_blank"),
        CheckConstraint("customer_type IN ('retailer','distributor')", name="ck_customers_type"),
        CheckConstraint(f"state_code ~ '{STATE_CODE}'", name="ck_customers_state_code"),
        CheckConstraint(f"gstin IS NULL OR gstin ~ '{GSTIN}'", name="ck_customers_gstin_format"),
        CheckConstraint("gstin IS NULL OR substr(gstin, 1, 2) = state_code", name="ck_customers_gstin_state"),
        Index("uq_customers_gstin", "gstin", unique=True, postgresql_where=text("gstin IS NOT NULL")),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    customer_type: Mapped[str] = mapped_column(Text)
    gstin: Mapped[str | None] = mapped_column(CHAR(15))
    state_code: Mapped[str] = mapped_column(CHAR(2))
    address: Mapped[str | None] = mapped_column(Text)
    phone: Mapped[str | None] = mapped_column(Text)


class Supplier(Base):
    __tablename__ = "suppliers"
    __table_args__ = (
        CheckConstraint("btrim(name) <> ''", name="ck_suppliers_name_not_blank"),
        CheckConstraint(f"state_code ~ '{STATE_CODE}'", name="ck_suppliers_state_code"),
        CheckConstraint(f"gstin IS NULL OR gstin ~ '{GSTIN}'", name="ck_suppliers_gstin_format"),
        CheckConstraint("gstin IS NULL OR substr(gstin, 1, 2) = state_code", name="ck_suppliers_gstin_state"),
        Index("uq_suppliers_gstin", "gstin", unique=True, postgresql_where=text("gstin IS NOT NULL")),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    gstin: Mapped[str | None] = mapped_column(CHAR(15))
    state_code: Mapped[str] = mapped_column(CHAR(2))
    address: Mapped[str | None] = mapped_column(Text)
    phone: Mapped[str | None] = mapped_column(Text)
