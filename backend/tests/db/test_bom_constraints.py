"""Constraint tests for migration 0002: recipes, recipe lines, BOMs, BOM packaging lines.

Fixtures commit their rows, because `violates()` rolls the session back after the expected error.
Each test breaks exactly one rule and names the constraint (or the trigger message) that must fire.
"""
from decimal import Decimal

import psycopg.errors as pge
import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from app.domain.units import split_cartons
from tests.db.helpers import run, scalar, violates

D = Decimal


# ---------------------------------------------------------------- builders
def _item(s, code, kind, unit, product_id=None, pack_size=None, per_carton=None, price=None):
    run(s, "INSERT INTO items (code, name, kind, base_unit, product_id, pack_size_g, packets_per_carton, list_price) "
           "VALUES (:c, :c, :k, CAST(:u AS unit_code), :p, :g, :pc, :lp)",
        c=code, k=kind, u=unit, p=product_id, g=pack_size, pc=per_carton, lp=price)
    return scalar(s, "SELECT id FROM items WHERE code = :c", c=code)


def _recipe(s, product_id, code="MUR-R1", version=1, status=None):
    if status is None:
        run(s, "INSERT INTO recipes (product_id, code, name, version) VALUES (:p, :c, 'Murukku dough', :v)",
            p=product_id, c=code, v=version)
    else:
        run(s, "INSERT INTO recipes (product_id, code, name, version, status) VALUES (:p, :c, 'Murukku dough', :v, :st)",
            p=product_id, c=code, v=version, st=status)
    return scalar(s, "SELECT id FROM recipes WHERE code = :c", c=code)


def _rline(s, recipe_id, item_id, qty):
    run(s, "INSERT INTO recipe_lines (recipe_id, item_id, unit, qty_per_batch) "
           "SELECT :r, id, base_unit, :q FROM items WHERE id = :i", r=recipe_id, i=item_id, q=qty)


def _bom(s, sku, product_id, recipe_id, yield_="200", version=1, status=None):
    cols, vals = "sku_item_id, product_id, recipe_id, yield_packets_per_batch, version", ":s, :p, :r, :y, :v"
    params = dict(s=sku, p=product_id, r=recipe_id, y=yield_, v=version)
    if status is not None:
        cols, vals, params["st"] = cols + ", status", vals + ", :st", status
    run(s, f"INSERT INTO boms ({cols}) VALUES ({vals})", **params)
    return scalar(s, "SELECT id FROM boms WHERE sku_item_id = :s AND version = :v", s=sku, v=version)


def _pline(s, bom_id, item_id, qty):
    run(s, "INSERT INTO bom_packaging_lines (bom_id, item_id, unit, qty_per_batch) "
           "SELECT :b, id, base_unit, :q FROM items WHERE id = :i", b=bom_id, i=item_id, q=qty)


def _set(s, table, row_id, status):
    run(s, f"UPDATE {table} SET status = :st WHERE id = :i", st=status, i=row_id)


@pytest.fixture
def m(db_session):
    """Benchmark masters: Murukku (5%) with a 200 g SKU, plus a second product, committed."""
    s = db_session
    run(s, "INSERT INTO products (name, hsn_code, gst_rate) VALUES ('Murukku', '1905', 5)")
    run(s, "INSERT INTO products (name, hsn_code, gst_rate) VALUES ('Mixture', '1905', 5)")
    murukku = scalar(s, "SELECT id FROM products WHERE name = 'Murukku'")
    mixture = scalar(s, "SELECT id FROM products WHERE name = 'Mixture'")
    ids = dict(
        murukku=murukku, mixture=mixture,
        flour=_item(s, "FLOUR", "raw", "kg"), oil=_item(s, "OIL", "raw", "l"), spice=_item(s, "SPICE", "raw", "kg"),
        pouch=_item(s, "POUCH-200", "packaging", "pc"), carton=_item(s, "CARTON", "packaging", "pc"),
        sku200=_item(s, "MUR-200", "finished", "pkt", murukku, 200, 20, "50.00"),
        mixture_sku=_item(s, "MIX-200", "finished", "pkt", mixture, 200, 20, "60.00"),
    )
    s.commit()
    return ids


def _draft_pair(s, m, recipe_code="MUR-R1"):
    """A draft recipe with the benchmark lines and a draft 200 g BOM with its packaging lines."""
    recipe = _recipe(s, m["murukku"], recipe_code)
    for item, qty in (("flour", "40"), ("oil", "10"), ("spice", "1.2")):
        _rline(s, recipe, m[item], qty)
    bom = _bom(s, m["sku200"], m["murukku"], recipe)
    _pline(s, bom, m["pouch"], "200")
    _pline(s, bom, m["carton"], "10")
    return recipe, bom


def _active_pair(s, m, recipe_code="MUR-R1"):
    recipe, bom = _draft_pair(s, m, recipe_code)
    _set(s, "recipes", recipe, "active")
    _set(s, "boms", bom, "active")
    s.commit()
    return recipe, bom


# ---------------------------------------------------------------- item types
def test_recipe_line_rejects_packaging_item(db_session, m):
    recipe = _recipe(db_session, m["murukku"])
    db_session.commit()
    with violates(db_session, pge.ForeignKeyViolation):
        _rline(db_session, recipe, m["pouch"], "5")


def test_recipe_line_rejects_finished_item(db_session, m):
    recipe = _recipe(db_session, m["murukku"])
    db_session.commit()
    with violates(db_session, pge.ForeignKeyViolation):
        _rline(db_session, recipe, m["sku200"], "5")


def test_recipe_line_item_kind_witness_cannot_be_changed(db_session, m):
    recipe = _recipe(db_session, m["murukku"])
    db_session.commit()
    with violates(db_session, pge.CheckViolation, "ck_recipe_lines_item_kind"):
        run(db_session, "INSERT INTO recipe_lines (recipe_id, item_id, item_kind, unit, qty_per_batch) "
                        "VALUES (:r, :i, 'packaging', 'pc', 5)", r=recipe, i=m["pouch"])


def test_bom_packaging_rejects_raw_item(db_session, m):
    recipe, bom = _draft_pair(db_session, m)
    db_session.commit()
    with violates(db_session, pge.ForeignKeyViolation):
        _pline(db_session, bom, m["flour"], "5")


def test_bom_sku_must_be_finished(db_session, m):
    recipe = _recipe(db_session, m["murukku"])
    db_session.commit()
    with violates(db_session, pge.ForeignKeyViolation):
        _bom(db_session, m["flour"], m["murukku"], recipe)  # a raw material cannot be a SKU


def test_bom_sku_kind_witness_cannot_be_changed(db_session, m):
    recipe = _recipe(db_session, m["murukku"])
    db_session.commit()
    with violates(db_session, pge.CheckViolation, "ck_boms_sku_kind"):
        run(db_session, "INSERT INTO boms (sku_item_id, sku_kind, product_id, recipe_id, yield_packets_per_batch) "
                        "VALUES (:s, 'raw', :p, :r, 200)", s=m["flour"], p=m["murukku"], r=recipe)


# ---------------------------------------------------------------- product consistency
def test_bom_rejects_recipe_of_other_product(db_session, m):
    other_recipe = _recipe(db_session, m["mixture"], "MIX-R1")
    db_session.commit()
    with violates(db_session, pge.ForeignKeyViolation, "fk_boms_recipe_product"):
        _bom(db_session, m["sku200"], m["murukku"], other_recipe)  # Murukku SKU, Mixture recipe


def test_bom_product_must_be_the_skus_product(db_session, m):
    recipe = _recipe(db_session, m["mixture"], "MIX-R1")
    db_session.commit()
    with violates(db_session, pge.ForeignKeyViolation, "fk_boms_sku_product"):
        _bom(db_session, m["sku200"], m["mixture"], recipe)  # recipe and BOM agree, SKU does not


def test_recipe_product_must_exist(db_session, m):
    with violates(db_session, pge.ForeignKeyViolation):
        _recipe(db_session, 999)


# ---------------------------------------------------------------- precision and ranges
@pytest.mark.parametrize("qty", ["0", "-1", "0.0004"])  # 0.0004 rounds to 0.000 in numeric(14,3)
def test_recipe_line_quantity_must_be_positive(db_session, m, qty):
    recipe = _recipe(db_session, m["murukku"])
    db_session.commit()
    with violates(db_session, pge.CheckViolation, "ck_recipe_lines_qty_positive"):
        _rline(db_session, recipe, m["flour"], qty)


@pytest.mark.parametrize("qty", ["0", "-1"])
def test_packaging_line_quantity_must_be_positive(db_session, m, qty):
    recipe, bom = _draft_pair(db_session, m)
    db_session.commit()
    with violates(db_session, pge.CheckViolation, "ck_bom_packaging_lines_qty_positive"):
        _pline(db_session, bom, _item(db_session, "TAPE", "packaging", "pc"), qty)


@pytest.mark.parametrize("yield_", ["0", "-5"])
def test_bom_yield_must_be_positive(db_session, m, yield_):
    recipe = _recipe(db_session, m["murukku"])
    db_session.commit()
    with violates(db_session, pge.CheckViolation, "ck_boms_yield_positive"):
        _bom(db_session, m["sku200"], m["murukku"], recipe, yield_=yield_)


def test_quantities_keep_three_decimals_exactly(db_session, m):
    recipe, bom = _draft_pair(db_session, m)
    db_session.commit()
    assert scalar(db_session, "SELECT qty_per_batch FROM recipe_lines WHERE item_id = :i", i=m["spice"]) == D("1.200")
    _rline(db_session, recipe, _item(db_session, "SALT", "raw", "kg"), "0.001")
    assert scalar(db_session, "SELECT min(qty_per_batch) FROM recipe_lines") == D("0.001")


def test_an_item_appears_once_per_recipe(db_session, m):
    recipe, bom = _draft_pair(db_session, m)
    db_session.commit()
    with violates(db_session, pge.UniqueViolation, "recipe_lines_recipe_id_item_id_key"):
        _rline(db_session, recipe, m["flour"], "1")


def test_an_item_appears_once_per_bom(db_session, m):
    recipe, bom = _draft_pair(db_session, m)
    db_session.commit()
    with violates(db_session, pge.UniqueViolation, "bom_packaging_lines_bom_id_item_id_key"):
        _pline(db_session, bom, m["pouch"], "1")


def test_recipe_code_is_unique_and_not_blank(db_session, m):
    _recipe(db_session, m["murukku"], "MUR-R1")
    db_session.commit()
    with violates(db_session, pge.UniqueViolation, "recipes_code_key"):
        _recipe(db_session, m["murukku"], "MUR-R1", version=2)
    with violates(db_session, pge.CheckViolation, "ck_recipes_code_not_blank"):
        _recipe(db_session, m["murukku"], " ")


@pytest.mark.parametrize("table", ["recipes", "boms"])
def test_version_must_be_at_least_one(db_session, m, table):
    recipe = _recipe(db_session, m["murukku"])
    db_session.commit()
    with violates(db_session, pge.CheckViolation, f"ck_{table}_version_positive"):
        if table == "recipes":
            _recipe(db_session, m["murukku"], "MUR-R0", version=0)
        else:
            _bom(db_session, m["sku200"], m["murukku"], recipe, version=0)


@pytest.mark.parametrize("table", ["recipes", "boms"])
def test_unknown_status_rejected(db_session, m, table):
    recipe = _recipe(db_session, m["murukku"])
    db_session.commit()
    # The lifecycle trigger fires before the column CHECK, so the error carries the transition text.
    with violates(db_session, pge.CheckViolation, message="(new) -> archived"):
        if table == "recipes":
            _recipe(db_session, m["murukku"], "MUR-R2", version=2, status="archived")
        else:
            _bom(db_session, m["sku200"], m["murukku"], recipe, status="archived")


# ---------------------------------------------------------------- one active BOM per SKU, versions
def test_second_active_bom_for_sku_rejected(db_session, m):
    recipe, bom = _active_pair(db_session, m)
    v2 = _bom(db_session, m["sku200"], m["murukku"], recipe, version=2)
    _pline(db_session, v2, m["pouch"], "200")
    _pline(db_session, v2, m["carton"], "10")
    db_session.commit()
    with violates(db_session, pge.UniqueViolation, "uq_boms_one_active_per_sku"):
        _set(db_session, "boms", v2, "active")


def test_retired_bom_allows_new_active(db_session, m):
    recipe, v1 = _active_pair(db_session, m)
    v2 = _bom(db_session, m["sku200"], m["murukku"], recipe, yield_="205", version=2)
    _pline(db_session, v2, m["pouch"], "205")
    _pline(db_session, v2, m["carton"], "11")
    _set(db_session, "boms", v1, "retired")
    _set(db_session, "boms", v2, "active")
    db_session.commit()
    assert scalar(db_session, "SELECT count(*) FROM boms WHERE status = 'active'") == 1
    assert scalar(db_session, "SELECT id FROM boms WHERE status = 'active'") == v2


def test_many_draft_boms_for_one_sku_are_allowed(db_session, m):
    recipe = _recipe(db_session, m["murukku"])
    for version in (1, 2, 3):
        _bom(db_session, m["sku200"], m["murukku"], recipe, version=version)
    assert scalar(db_session, "SELECT count(*) FROM boms") == 3


def test_active_boms_of_different_skus_coexist(db_session, m):
    _active_pair(db_session, m)
    mix_recipe = _recipe(db_session, m["mixture"], "MIX-R1")
    _rline(db_session, mix_recipe, m["flour"], "30")
    mix_bom = _bom(db_session, m["mixture_sku"], m["mixture"], mix_recipe)
    _pline(db_session, mix_bom, m["pouch"], "200")
    _set(db_session, "recipes", mix_recipe, "active")
    _set(db_session, "boms", mix_bom, "active")
    assert scalar(db_session, "SELECT count(*) FROM boms WHERE status = 'active'") == 2


def test_bom_version_is_unique_per_sku(db_session, m):
    recipe, bom = _draft_pair(db_session, m)
    db_session.commit()
    with violates(db_session, pge.UniqueViolation, "boms_sku_item_id_version_key"):
        _bom(db_session, m["sku200"], m["murukku"], recipe, version=1)


def test_a_retired_version_keeps_its_original_assumptions(db_session, m):
    """Historical production assumptions: v1 reads the same after v2 replaces it."""
    recipe, v1 = _active_pair(db_session, m)
    snapshot = run(db_session, "SELECT yield_packets_per_batch, recipe_id, version FROM boms WHERE id = :i", i=v1).one()
    lines = run(db_session, "SELECT item_id, qty_per_batch FROM bom_packaging_lines WHERE bom_id = :i ORDER BY item_id", i=v1).all()
    v2 = _bom(db_session, m["sku200"], m["murukku"], recipe, yield_="190", version=2)
    _pline(db_session, v2, m["pouch"], "190")
    _pline(db_session, v2, m["carton"], "9")
    _set(db_session, "boms", v1, "retired")
    _set(db_session, "boms", v2, "active")
    db_session.commit()
    assert run(db_session, "SELECT yield_packets_per_batch, recipe_id, version FROM boms WHERE id = :i", i=v1).one() == snapshot
    assert run(db_session, "SELECT item_id, qty_per_batch FROM bom_packaging_lines WHERE bom_id = :i ORDER BY item_id", i=v1).all() == lines
    assert scalar(db_session, "SELECT status FROM boms WHERE id = :i", i=v1) == "retired"


# ---------------------------------------------------------------- immutability once active
def test_active_bom_cannot_be_edited(db_session, m):
    recipe, bom = _active_pair(db_session, m)
    with violates(db_session, pge.RestrictViolation, message="boms"):
        run(db_session, "UPDATE boms SET yield_packets_per_batch = 250 WHERE id = :i", i=bom)


def test_retired_bom_cannot_be_edited_or_reactivated(db_session, m):
    recipe, bom = _active_pair(db_session, m)
    _set(db_session, "boms", bom, "retired")
    db_session.commit()
    with violates(db_session, pge.RestrictViolation, message="boms"):
        run(db_session, "UPDATE boms SET yield_packets_per_batch = 250 WHERE id = :i", i=bom)
    with violates(db_session, pge.CheckViolation, message="retired -> active"):
        _set(db_session, "boms", bom, "active")


def test_active_recipe_cannot_be_edited(db_session, m):
    recipe, bom = _active_pair(db_session, m)
    with violates(db_session, pge.RestrictViolation, message="recipes"):
        run(db_session, "UPDATE recipes SET name = 'changed' WHERE id = :i", i=recipe)


def test_draft_bom_and_recipe_can_be_edited_and_deleted(db_session, m):
    recipe, bom = _draft_pair(db_session, m)
    run(db_session, "UPDATE boms SET yield_packets_per_batch = 198 WHERE id = :i", i=bom)
    run(db_session, "UPDATE recipes SET name = 'Murukku dough v1' WHERE id = :i", i=recipe)
    run(db_session, "DELETE FROM bom_packaging_lines WHERE bom_id = :i", i=bom)
    run(db_session, "DELETE FROM boms WHERE id = :i", i=bom)
    run(db_session, "DELETE FROM recipe_lines WHERE recipe_id = :i", i=recipe)
    run(db_session, "DELETE FROM recipes WHERE id = :i", i=recipe)
    assert scalar(db_session, "SELECT count(*) FROM boms") == 0


@pytest.mark.parametrize("table", ["boms", "recipes"])
@pytest.mark.parametrize("status", ["active", "retired"])
def test_non_draft_rows_cannot_be_deleted(db_session, m, table, status):
    recipe, bom = _active_pair(db_session, m)
    row = bom if table == "boms" else recipe
    if status == "retired":
        _set(db_session, "boms", bom, "retired")
        if table == "recipes":
            _set(db_session, "recipes", recipe, "retired")
        db_session.commit()
    with violates(db_session, pge.RestrictViolation, message=table):
        run(db_session, f"DELETE FROM {table} WHERE id = :i", i=row)


@pytest.mark.parametrize("change", ["insert", "update", "delete"])
def test_lines_of_an_active_bom_are_frozen(db_session, m, change):
    recipe, bom = _active_pair(db_session, m)
    extra = _item(db_session, "TAPE", "packaging", "pc")
    db_session.commit()
    with violates(db_session, pge.RestrictViolation, message="bom_packaging_lines"):
        if change == "insert":
            _pline(db_session, bom, extra, "1")
        elif change == "update":
            run(db_session, "UPDATE bom_packaging_lines SET qty_per_batch = 11 WHERE bom_id = :b", b=bom)
        else:
            run(db_session, "DELETE FROM bom_packaging_lines WHERE bom_id = :b", b=bom)


@pytest.mark.parametrize("change", ["insert", "update", "delete"])
def test_lines_of_an_active_recipe_are_frozen(db_session, m, change):
    recipe, bom = _active_pair(db_session, m)
    extra = _item(db_session, "SALT", "raw", "kg")
    db_session.commit()
    with violates(db_session, pge.RestrictViolation, message="recipe_lines"):
        if change == "insert":
            _rline(db_session, recipe, extra, "1")
        elif change == "update":
            run(db_session, "UPDATE recipe_lines SET qty_per_batch = 41 WHERE recipe_id = :r", r=recipe)
        else:
            run(db_session, "DELETE FROM recipe_lines WHERE recipe_id = :r", r=recipe)


def test_a_line_cannot_be_moved_into_an_active_parent(db_session, m):
    active_recipe, bom = _active_pair(db_session, m)
    draft_recipe = _recipe(db_session, m["murukku"], "MUR-R2", version=2)
    _rline(db_session, draft_recipe, m["flour"], "41")
    db_session.commit()
    with violates(db_session, pge.RestrictViolation, message="recipe_lines"):
        run(db_session, "UPDATE recipe_lines SET recipe_id = :a WHERE recipe_id = :d", a=active_recipe, d=draft_recipe)


# ---------------------------------------------------------------- lifecycle and activation
@pytest.mark.parametrize("table", ["recipes", "boms"])
def test_new_rows_cannot_start_active(db_session, m, table):
    recipe = _recipe(db_session, m["murukku"])
    db_session.commit()
    with violates(db_session, pge.CheckViolation, message="(new) -> active"):
        if table == "recipes":
            _recipe(db_session, m["murukku"], "MUR-R2", version=2, status="active")
        else:
            _bom(db_session, m["sku200"], m["murukku"], recipe, status="active")


@pytest.mark.parametrize("table", ["recipes", "boms"])
def test_active_rows_cannot_return_to_draft(db_session, m, table):
    recipe, bom = _active_pair(db_session, m)
    with violates(db_session, pge.CheckViolation, message="active -> draft"):
        _set(db_session, table, bom if table == "boms" else recipe, "draft")


def test_a_draft_can_be_retired_without_ever_being_active(db_session, m):
    recipe, bom = _draft_pair(db_session, m)
    _set(db_session, "boms", bom, "retired")
    assert scalar(db_session, "SELECT status FROM boms") == "retired"


def test_bom_activation_requires_lines(db_session, m):
    recipe = _recipe(db_session, m["murukku"])
    _rline(db_session, recipe, m["flour"], "40")
    _set(db_session, "recipes", recipe, "active")
    bom = _bom(db_session, m["sku200"], m["murukku"], recipe)  # no packaging lines
    db_session.commit()
    with violates(db_session, pge.CheckViolation, message="at least one packaging line"):
        _set(db_session, "boms", bom, "active")


def test_recipe_activation_requires_lines(db_session, m):
    recipe = _recipe(db_session, m["murukku"])
    db_session.commit()
    with violates(db_session, pge.CheckViolation, message="at least one ingredient"):
        _set(db_session, "recipes", recipe, "active")


def test_bom_activation_requires_an_active_recipe(db_session, m):
    recipe, bom = _draft_pair(db_session, m)  # recipe still draft
    db_session.commit()
    with violates(db_session, pge.CheckViolation, message="recipe must be active"):
        _set(db_session, "boms", bom, "active")


def test_a_retired_recipe_cannot_back_a_new_active_bom(db_session, m):
    recipe, bom = _draft_pair(db_session, m)
    _set(db_session, "recipes", recipe, "active")
    _set(db_session, "recipes", recipe, "retired")
    db_session.commit()
    with violates(db_session, pge.CheckViolation, message="recipe must be active"):
        _set(db_session, "boms", bom, "active")


# ---------------------------------------------------------------- the benchmark and a second pack size
def test_benchmark_200g_bom_is_represented_exactly(db_session, m):
    recipe, bom = _active_pair(db_session, m)
    lines = dict(run(db_session, "SELECT i.code, l.qty_per_batch FROM recipe_lines l JOIN items i ON i.id = l.item_id").all())
    assert lines == {"FLOUR": D("40"), "OIL": D("10"), "SPICE": D("1.2")}
    packaging = dict(run(db_session, "SELECT i.code, l.qty_per_batch FROM bom_packaging_lines l JOIN items i ON i.id = l.item_id").all())
    assert packaging == {"POUCH-200": D("200"), "CARTON": D("10")}
    assert scalar(db_session, "SELECT yield_packets_per_batch FROM boms") == D("200")
    # the carton standard agrees with the SKU's packing: 200 packets at 20 per carton is 10 cartons
    assert split_cartons(200, scalar(db_session, "SELECT packets_per_carton FROM items WHERE code = 'MUR-200'"))[0] == 10


def test_a_500g_sku_reuses_the_recipe_with_its_own_yield_and_packaging(db_session, m):
    """Design 4.3 'Interview change': data entry only, no schema change."""
    recipe, bom200 = _active_pair(db_session, m)
    sku500 = _item(db_session, "MUR-500", "finished", "pkt", m["murukku"], 500, 10, "120.00")
    pouch500 = _item(db_session, "POUCH-500", "packaging", "pc")
    carton500 = _item(db_session, "CARTON-500", "packaging", "pc")
    bom500 = _bom(db_session, sku500, m["murukku"], recipe, yield_="80")  # same 40 kg batch, fewer, bigger packs
    _pline(db_session, bom500, pouch500, "80")
    _pline(db_session, bom500, carton500, "8")
    _set(db_session, "boms", bom500, "active")
    db_session.commit()
    rows = run(db_session, "SELECT sku_item_id, recipe_id, yield_packets_per_batch FROM boms ORDER BY sku_item_id").all()
    assert {r.recipe_id for r in rows} == {recipe}, "both pack sizes share one recipe"
    assert {r.sku_item_id: r.yield_packets_per_batch for r in rows} == {m["sku200"]: D("200"), sku500: D("80")}
    assert scalar(db_session, "SELECT count(*) FROM recipes") == 1
    assert scalar(db_session, "SELECT count(*) FROM recipe_lines") == 3, "the recipe lines were not copied"
    # 200 g and 500 g packs from one batch carry the same mass: 200 x 200 g = 80 x 500 g = 40 kg
    assert D(200) * 200 == D(80) * 500


# ---------------------------------------------------------------- review follow-ups
def test_line_unit_must_be_the_items_base_unit(db_session, m):
    recipe = _recipe(db_session, m["murukku"])
    db_session.commit()
    with violates(db_session, pge.ForeignKeyViolation, "fk_recipe_lines_item_unit"):
        run(db_session, "INSERT INTO recipe_lines (recipe_id, item_id, unit, qty_per_batch) VALUES (:r, :i, 'g', 40)",
            r=recipe, i=m["flour"])  # flour is stocked in kg


def test_packaging_line_unit_must_be_the_items_base_unit(db_session, m):
    recipe, bom = _draft_pair(db_session, m)
    tape = _item(db_session, "TAPE", "packaging", "pc")
    db_session.commit()
    with violates(db_session, pge.ForeignKeyViolation, "fk_bom_packaging_lines_item_unit"):
        run(db_session, "INSERT INTO bom_packaging_lines (bom_id, item_id, unit, qty_per_batch) VALUES (:b, :i, 'kg', 1)",
            b=bom, i=tape)


@pytest.mark.parametrize("code, new_unit", [("FLOUR", "g"), ("POUCH-200", "pkt")])
def test_an_items_base_unit_cannot_change_while_a_line_uses_it(db_session, m, code, new_unit):
    """Otherwise a frozen '40' of flour would silently become 40 g."""
    _active_pair(db_session, m)
    with violates(db_session, pge.ForeignKeyViolation):
        run(db_session, "UPDATE items SET base_unit = CAST(:u AS unit_code) WHERE code = :c", u=new_unit, c=code)


@pytest.mark.parametrize("parent", ["recipes", "boms"])
@pytest.mark.parametrize("change", ["insert", "update", "delete"])
def test_lines_of_a_retired_parent_are_frozen(db_session, m, parent, change):
    recipe, bom = _active_pair(db_session, m)
    _set(db_session, "boms", bom, "retired")
    if parent == "recipes":
        _set(db_session, "recipes", recipe, "retired")
    extra_raw = _item(db_session, "SALT", "raw", "kg")
    extra_pack = _item(db_session, "TAPE", "packaging", "pc")
    db_session.commit()
    table = "recipe_lines" if parent == "recipes" else "bom_packaging_lines"
    owner = recipe if parent == "recipes" else bom
    key = "recipe_id" if parent == "recipes" else "bom_id"
    with violates(db_session, pge.RestrictViolation, message=table):
        if change == "insert":
            (_rline if parent == "recipes" else _pline)(db_session, owner, extra_raw if parent == "recipes" else extra_pack, "1")
        elif change == "update":
            run(db_session, f"UPDATE {table} SET qty_per_batch = 99 WHERE {key} = :o", o=owner)
        else:
            run(db_session, f"DELETE FROM {table} WHERE {key} = :o", o=owner)


def test_a_line_cannot_be_moved_out_of_an_active_parent(db_session, m):
    active_recipe, bom = _active_pair(db_session, m)
    draft_recipe = _recipe(db_session, m["murukku"], "MUR-R2", version=2)
    db_session.commit()
    with violates(db_session, pge.RestrictViolation, message="recipe_lines"):
        run(db_session, "UPDATE recipe_lines SET recipe_id = :d WHERE recipe_id = :a", d=draft_recipe, a=active_recipe)


def test_a_retired_version_cannot_be_altered_afterwards(db_session, m):
    recipe, v1 = _active_pair(db_session, m)
    _set(db_session, "boms", v1, "retired")
    db_session.commit()
    before = run(db_session, "SELECT yield_packets_per_batch, recipe_id, version, status FROM boms WHERE id = :i", i=v1).one()
    with violates(db_session, pge.RestrictViolation, message="boms"):
        run(db_session, "UPDATE boms SET yield_packets_per_batch = 1, version = 9 WHERE id = :i", i=v1)
    assert run(db_session, "SELECT yield_packets_per_batch, recipe_id, version, status FROM boms WHERE id = :i", i=v1).one() == before


def test_a_recipe_cannot_be_retired_under_an_active_bom(db_session, m):
    recipe, bom = _active_pair(db_session, m)
    with violates(db_session, pge.CheckViolation, message="used by an active BOM"):
        _set(db_session, "recipes", recipe, "retired")


def test_a_recipe_can_be_retired_once_its_bom_is_retired(db_session, m):
    recipe, bom = _active_pair(db_session, m)
    _set(db_session, "boms", bom, "retired")
    _set(db_session, "recipes", recipe, "retired")
    assert scalar(db_session, "SELECT status FROM recipes") == "retired"


def test_a_line_added_concurrently_with_activation_cannot_slip_in(db_session, m, connect):
    """Session A adds a line to a draft BOM and has not committed. Activation must wait for it,
    never activate on a stale view and leave a line that appeared after activation."""
    recipe, bom = _draft_pair(db_session, m)
    _set(db_session, "recipes", recipe, "active")
    tape = _item(db_session, "TAPE", "packaging", "pc")
    db_session.commit()
    _pline(db_session, bom, tape, "1")  # A: uncommitted
    with connect() as other:
        other.execute(text("SET LOCAL lock_timeout = '300ms'"))
        with pytest.raises(OperationalError) as caught:
            other.execute(text("UPDATE boms SET status = 'active' WHERE id = :i"), {"i": bom})
    assert isinstance(caught.value.orig, pge.LockNotAvailable)
    db_session.rollback()


# ---------------------------------------------------------------- NaN is not a quantity
def test_recipe_line_quantity_rejects_nan(db_session, m):
    recipe = _recipe(db_session, m["murukku"])
    db_session.commit()
    with violates(db_session, pge.CheckViolation, "ck_recipe_lines_qty_positive"):
        _rline(db_session, recipe, m["flour"], "NaN")


def test_bom_yield_rejects_nan(db_session, m):
    recipe = _recipe(db_session, m["murukku"])
    db_session.commit()
    with violates(db_session, pge.CheckViolation, "ck_boms_yield_positive"):
        _bom(db_session, m["sku200"], m["murukku"], recipe, yield_="NaN")


def test_bom_packaging_quantity_rejects_nan(db_session, m):
    recipe = _recipe(db_session, m["murukku"])
    bom = _bom(db_session, m["sku200"], m["murukku"], recipe)
    db_session.commit()
    with violates(db_session, pge.CheckViolation, "ck_bom_packaging_lines_qty_positive"):
        _pline(db_session, bom, m["pouch"], "NaN")
