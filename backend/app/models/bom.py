"""Recipes and bills of materials (design 4.3, migration 0002).

A recipe is the shared bulk formulation for a product; a BOM binds one finished SKU to a recipe,
a yield and its own packaging. Rows are editable while `draft`; the database freezes them once
`active` and again once `retired`.
"""
from decimal import Decimal

from sqlalchemy import (BigInteger, CheckConstraint, ForeignKey, ForeignKeyConstraint, Identity, Index, Integer, Numeric,
                        Text, UniqueConstraint, text)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.masters import UnitCode


class Recipe(Base):
    __tablename__ = "recipes"
    __table_args__ = (
        UniqueConstraint("code", name="recipes_code_key"),
        UniqueConstraint("id", "product_id", name="recipes_id_product_id_key"),
        CheckConstraint("btrim(code) <> ''", name="ck_recipes_code_not_blank"),
        CheckConstraint("btrim(name) <> ''", name="ck_recipes_name_not_blank"),
        CheckConstraint("version >= 1", name="ck_recipes_version_positive"),
        CheckConstraint("status IN ('draft','active','retired')", name="ck_recipes_status"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"))
    code: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer, server_default=text("1"))
    status: Mapped[str] = mapped_column(Text, server_default=text("'draft'"))


class RecipeLine(Base):
    """One raw material per recipe, in the item's base unit, for one standard batch."""
    __tablename__ = "recipe_lines"
    __table_args__ = (
        UniqueConstraint("recipe_id", "item_id", name="recipe_lines_recipe_id_item_id_key"),
        CheckConstraint("item_kind = 'raw'", name="ck_recipe_lines_item_kind"),
        CheckConstraint("qty_per_batch > 0 AND qty_per_batch <> 'NaN'", name="ck_recipe_lines_qty_positive"),
        ForeignKeyConstraint(["item_id", "item_kind"], ["items.id", "items.kind"], name="fk_recipe_lines_item_kind"),
        ForeignKeyConstraint(["item_id", "unit"], ["items.id", "items.base_unit"], name="fk_recipe_lines_item_unit"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    recipe_id: Mapped[int] = mapped_column(ForeignKey("recipes.id"))
    item_id: Mapped[int] = mapped_column(BigInteger)
    item_kind: Mapped[str] = mapped_column(Text, server_default=text("'raw'"))
    unit: Mapped[str] = mapped_column(UnitCode)  # witness: always the item's base unit
    qty_per_batch: Mapped[Decimal] = mapped_column(Numeric(14, 3))


class Bom(Base):
    __tablename__ = "boms"
    __table_args__ = (
        UniqueConstraint("id", "sku_item_id", name="boms_id_sku_item_id_key"),
        UniqueConstraint("sku_item_id", "version", name="boms_sku_item_id_version_key"),
        CheckConstraint("sku_kind = 'finished'", name="ck_boms_sku_kind"),
        CheckConstraint("yield_packets_per_batch > 0 AND yield_packets_per_batch <> 'NaN'", name="ck_boms_yield_positive"),
        CheckConstraint("version >= 1", name="ck_boms_version_positive"),
        CheckConstraint("status IN ('draft','active','retired')", name="ck_boms_status"),
        ForeignKeyConstraint(["sku_item_id", "sku_kind"], ["items.id", "items.kind"], name="fk_boms_sku_kind"),
        ForeignKeyConstraint(["sku_item_id", "product_id"], ["items.id", "items.product_id"], name="fk_boms_sku_product"),
        ForeignKeyConstraint(["recipe_id", "product_id"], ["recipes.id", "recipes.product_id"], name="fk_boms_recipe_product"),
        Index("uq_boms_one_active_per_sku", "sku_item_id", unique=True, postgresql_where=text("status = 'active'")),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    sku_item_id: Mapped[int] = mapped_column(BigInteger)
    sku_kind: Mapped[str] = mapped_column(Text, server_default=text("'finished'"))
    product_id: Mapped[int] = mapped_column(BigInteger)
    recipe_id: Mapped[int] = mapped_column(BigInteger)
    yield_packets_per_batch: Mapped[Decimal] = mapped_column(Numeric(14, 3))
    version: Mapped[int] = mapped_column(Integer, server_default=text("1"))
    status: Mapped[str] = mapped_column(Text, server_default=text("'draft'"))


class BomPackagingLine(Base):
    """One packaging item per BOM, in the item's base unit, for one standard batch."""
    __tablename__ = "bom_packaging_lines"
    __table_args__ = (
        UniqueConstraint("bom_id", "item_id", name="bom_packaging_lines_bom_id_item_id_key"),
        CheckConstraint("item_kind = 'packaging'", name="ck_bom_packaging_lines_item_kind"),
        CheckConstraint("qty_per_batch > 0 AND qty_per_batch <> 'NaN'", name="ck_bom_packaging_lines_qty_positive"),
        ForeignKeyConstraint(["item_id", "item_kind"], ["items.id", "items.kind"], name="fk_bom_packaging_lines_item_kind"),
        ForeignKeyConstraint(["item_id", "unit"], ["items.id", "items.base_unit"], name="fk_bom_packaging_lines_item_unit"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    bom_id: Mapped[int] = mapped_column(ForeignKey("boms.id"))
    item_id: Mapped[int] = mapped_column(BigInteger)
    item_kind: Mapped[str] = mapped_column(Text, server_default=text("'packaging'"))
    unit: Mapped[str] = mapped_column(UnitCode)  # witness: always the item's base unit
    qty_per_batch: Mapped[Decimal] = mapped_column(Numeric(14, 3))
