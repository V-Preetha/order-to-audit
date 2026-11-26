"""The ORM models must describe the migrated schema exactly, and round-trip real rows.

Schema is owned by the migration (hand-written SQL). The models mirror it, and these tests fail
the moment the two drift apart: a missing column, a changed type, a different nullability, a
renamed constraint, or a table with no model.
"""
from decimal import Decimal

import pytest
from sqlalchemy import inspect, select, text
from sqlalchemy.dialects import postgresql

from app.models import (Base, Customer, DocumentSequence, Item, Product, RequestLog, Settings,
                        StatusTransition, Supplier, User)

TEST_ONLY_TABLES = {"alembic_version", "_disposable_test_db", "harness_probe"}


def _type(column_type):
    return column_type.compile(dialect=postgresql.dialect())


@pytest.fixture
def inspector(engine):
    return inspect(engine)


def test_every_migrated_table_has_a_model_and_vice_versa(inspector):
    migrated = set(inspector.get_table_names()) - TEST_ONLY_TABLES
    assert migrated == set(Base.metadata.tables)


@pytest.mark.parametrize("table", sorted(Base.metadata.tables))
def test_columns_types_and_nullability_match(inspector, table):
    model = {c.name: (_type(c.type), c.nullable) for c in Base.metadata.tables[table].columns}
    actual = {c["name"]: (_type(c["type"]), c["nullable"]) for c in inspector.get_columns(table)}
    assert model == actual


@pytest.mark.parametrize("table", sorted(Base.metadata.tables))
def test_primary_and_foreign_keys_match(inspector, table):
    t = Base.metadata.tables[table]
    assert [c.name for c in t.primary_key.columns] == inspector.get_pk_constraint(table)["constrained_columns"]
    model_fks = {(tuple(fk.parent.name for fk in c.elements), c.elements[0].column.table.name) for c in t.foreign_key_constraints}
    actual_fks = {(tuple(f["constrained_columns"]), f["referred_table"]) for f in inspector.get_foreign_keys(table)}
    assert model_fks == actual_fks


@pytest.mark.parametrize("table", sorted(Base.metadata.tables))
def test_check_constraint_names_match(inspector, table):
    from sqlalchemy import CheckConstraint
    model = {c.name for c in Base.metadata.tables[table].constraints if isinstance(c, CheckConstraint)}
    actual = {c["name"] for c in inspector.get_check_constraints(table)}
    assert model == actual


@pytest.mark.parametrize("table", sorted(Base.metadata.tables))
def test_unique_constraints_and_indexes_match(inspector, table):
    from sqlalchemy import Column, UniqueConstraint
    t = Base.metadata.tables[table]
    model = {(c.name, tuple(col.name for col in c.columns)) for c in t.constraints if isinstance(c, UniqueConstraint)}
    # an expression in an index (for example lower(...)) is reflected as None
    model |= {(i.name, tuple(e.name if isinstance(e, Column) else None for e in i.expressions)) for i in t.indexes if i.unique}
    actual = {(u["name"], tuple(u["column_names"])) for u in inspector.get_unique_constraints(table)}
    actual |= {(i["name"], tuple(i["column_names"])) for i in inspector.get_indexes(table) if i["unique"]}
    assert model == actual


@pytest.mark.parametrize("table", sorted(Base.metadata.tables))
def test_server_defaults_and_identity_match(inspector, table):
    def norm(default):
        # PostgreSQL reports a text default as 'draft'::text; compare the literal, not the cast
        return None if default is None else str(default).split("::")[0].strip("()").replace("'", "")
    model = {c.name: (norm(c.server_default.arg if c.server_default is not None and hasattr(c.server_default, "arg") else None),
                      c.identity is not None and c.identity.always) for c in Base.metadata.tables[table].columns}
    actual = {c["name"]: (norm(c["default"]) if not c.get("identity") else None,
                          bool(c.get("identity") and c["identity"].get("always"))) for c in inspector.get_columns(table)}
    assert model == actual


@pytest.mark.parametrize("table", sorted(Base.metadata.tables))
def test_foreign_keys_point_at_the_same_columns(inspector, table):
    model = {(tuple(e.parent.name for e in c.elements), tuple(e.column.name for e in c.elements))
             for c in Base.metadata.tables[table].foreign_key_constraints}
    actual = {(tuple(f["constrained_columns"]), tuple(f["referred_columns"])) for f in inspector.get_foreign_keys(table)}
    assert model == actual


@pytest.mark.parametrize("table", ["customers", "suppliers"])
def test_gstin_uniqueness_is_partial_so_unregistered_parties_can_share_null(inspector, table):
    (index,) = [i for i in inspector.get_indexes(table) if i["name"] == f"uq_{table}_gstin"]
    assert index["unique"] and index["dialect_options"]["postgresql_where"].strip("()") == "gstin IS NOT NULL"
    (model_index,) = [i for i in Base.metadata.tables[table].indexes if i.name == f"uq_{table}_gstin"]
    assert str(model_index.dialect_options["postgresql"]["where"]) == "gstin IS NOT NULL"


def test_enum_labels_match_the_database(inspector):
    from app.models.masters import UnitCode
    (db_enum,) = [e for e in inspector.get_enums() if e["name"] == "unit_code"]
    assert list(UnitCode.enums) == db_enum["labels"]


def test_round_trip_of_the_benchmark_masters(engine):
    from sqlalchemy.orm import Session
    with Session(engine) as s:
        s.add(User(username="asha", full_name="Asha K", password_hash="x", role="admin"))
        s.add(Settings(id=1, factory_name="Madurai Murukku Works", factory_gstin="33AABCR1234F1Z5", factory_state_code="33"))
        s.add(DocumentSequence(scope="INV/2026-27"))
        s.add(RequestLog(request_id="11111111-1111-1111-1111-111111111111", action="x", fingerprint="a" * 64))
        product = Product(name="Murukku", hsn_code="1905", gst_rate=Decimal("5.00"))
        s.add(product)
        s.flush()
        s.add_all([
            Item(code="FLOUR", name="Flour", kind="raw", base_unit="kg"),
            Item(code="MUR-200", name="Murukku 200 g", kind="finished", base_unit="pkt", product_id=product.id,
                 pack_size_g=200, packets_per_carton=20, list_price=Decimal("50.00")),
            Customer(name="Retailer A", customer_type="retailer", gstin="33AABCR1234F1Z5", state_code="33"),
            Supplier(name="Mill Co", state_code="33"),
        ])
        s.commit()
    with Session(engine) as s:
        assert s.scalar(select(Product.gst_rate)) == Decimal("5.00")
        assert s.scalar(select(Item.list_price).where(Item.code == "MUR-200")) == Decimal("50.00")
        assert s.scalar(select(Settings.variance_tolerance_pct)) == Decimal("3.00")
        assert s.scalar(select(Settings.invoice_round_to_rupee)) is False
        assert s.scalar(select(User.is_active)) is True
        assert s.scalar(select(DocumentSequence.last_value)) == 0
        assert s.scalar(select(RequestLog.created_at)) is not None
        assert s.scalar(select(Customer.state_code)) == "33"
        assert s.scalar(select(StatusTransition.entity).where(StatusTransition.to_status == "completed")) == "production_runs"


def test_round_trip_of_the_benchmark_recipe_and_bom(engine):
    from sqlalchemy.orm import Session
    from app.models import Bom, BomPackagingLine, Recipe, RecipeLine
    with Session(engine) as s:
        product = Product(name="Murukku", hsn_code="1905", gst_rate=Decimal("5.00"))
        s.add(product)
        s.flush()
        flour = Item(code="FLOUR", name="Flour", kind="raw", base_unit="kg")
        pouch = Item(code="POUCH", name="Pouch", kind="packaging", base_unit="pc")
        sku = Item(code="MUR-200", name="Murukku 200 g", kind="finished", base_unit="pkt", product_id=product.id,
                   pack_size_g=200, packets_per_carton=20, list_price=Decimal("50.00"))
        recipe = Recipe(product_id=product.id, code="MUR-R1", name="Murukku dough")
        s.add_all([flour, pouch, sku, recipe])
        s.flush()
        s.add(RecipeLine(recipe_id=recipe.id, item_id=flour.id, unit="kg", qty_per_batch=Decimal("40")))
        bom = Bom(sku_item_id=sku.id, product_id=product.id, recipe_id=recipe.id, yield_packets_per_batch=Decimal("200"))
        s.add(bom)
        s.flush()
        s.add(BomPackagingLine(bom_id=bom.id, item_id=pouch.id, unit="pc", qty_per_batch=Decimal("200")))
        s.commit()
    with Session(engine) as s:
        assert s.scalar(select(Recipe.status)) == "draft" and s.scalar(select(Recipe.version)) == 1
        assert s.scalar(select(RecipeLine.item_kind)) == "raw"
        assert s.scalar(select(Bom.sku_kind)) == "finished" and s.scalar(select(Bom.status)) == "draft"
        assert s.scalar(select(BomPackagingLine.item_kind)) == "packaging"
        assert s.scalar(select(Bom.yield_packets_per_batch)) == Decimal("200")


def test_round_trip_of_the_benchmark_order_and_invoice(engine):
    from datetime import date
    from sqlalchemy.orm import Session
    from app.models import CreditNote, Customer, Invoice, InvoiceLine, Order, OrderLine
    with Session(engine) as s:
        user = User(username="ada", full_name="Ada", password_hash="x", role="admin")
        product = Product(name="Murukku", hsn_code="1905", gst_rate=Decimal("5.00"))
        customer = Customer(name="Retailer A", customer_type="retailer", gstin="33AABCR1234F1Z5", state_code="33")
        s.add_all([user, product, customer])
        s.flush()
        sku = Item(code="MUR-200", name="Murukku 200 g", kind="finished", base_unit="pkt", product_id=product.id,
                   pack_size_g=200, packets_per_carton=20, list_price=Decimal("50.00"))
        s.add(sku)
        s.flush()
        order = Order(order_no="ORD/0001", client_uuid="00000000-0000-0000-0000-000000000001", customer_id=customer.id,
                      salesman_id=user.id, order_date=date(2026, 11, 20))
        s.add(order)
        s.flush()
        line = OrderLine(order_id=order.id, item_id=sku.id, qty=Decimal("150"), unit_price=Decimal("50.00"))
        s.add(line)
        s.flush()
        s.execute(text("UPDATE orders SET status='approved', decided_by=:u, decided_at=now() WHERE id=:o"), {"u": user.id, "o": order.id})
        invoice = Invoice(invoice_no="INV/26-27/0001", financial_year="2026-27", invoice_seq=1, order_id=order.id, customer_id=customer.id,
                          invoice_date=date(2026, 11, 20), seller_gstin="33AABCM1111A1Z1", seller_state_code="33",
                          customer_name="Retailer A", customer_gstin="33AABCR1234F1Z5", customer_address="Chennai",
                          place_of_supply_state="33", supply_type="intra", taxable_total=Decimal("7500.00"), cgst_total=Decimal("187.50"),
                          sgst_total=Decimal("187.50"), igst_total=Decimal("0"), grand_total=Decimal("7875.00"), created_by=user.id)
        s.add(invoice)
        s.flush()
        s.add(InvoiceLine(invoice_id=invoice.id, order_id=order.id, order_line_id=line.id, item_id=sku.id, description="Murukku 200 g",
                          hsn_code="1905", qty=Decimal("150"), unit_price=Decimal("50.00"), taxable_value=Decimal("7500.00"),
                          gst_rate=Decimal("5.00"), cgst_amount=Decimal("187.50"), sgst_amount=Decimal("187.50"),
                          igst_amount=Decimal("0"), supply_type="intra"))
        s.flush()
        s.execute(text("UPDATE orders SET status='invoiced' WHERE id=:o"), {"o": order.id})
        s.commit()
    with Session(engine) as s:
        assert s.scalar(select(Order.status)) == "invoiced"
        assert s.scalar(select(Invoice.status)) == "issued" and s.scalar(select(Invoice.round_off)) == Decimal("0.00")
        assert s.scalar(select(InvoiceLine.cgst_amount)) == Decimal("187.50")
        assert s.scalar(select(CreditNote.id)) is None
