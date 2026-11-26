"""Constraint tests for migration 0005: purchasing, stock adjustments, the append-only stock ledger, reservations.

What the database enforces on its own is tested here; what only a locking service can guarantee is NOT claimed.
Rules that depend on the SUM of rows (no negative stock, reservations <= on-hand, no valuation residue) run in a
deferred constraint trigger at COMMIT, so those tests commit for real. Source-document rules that depend on other
rows (a dispatch matches its invoice line, a purchase line has its receipt) also run at COMMIT.

Fixtures commit master data because `violates()` rolls the session back after an expected error.
"""
import threading
import time
from datetime import date, timedelta
from decimal import Decimal

import psycopg.errors as pge
import pytest
from sqlalchemy import text

from app.domain.costing import wac_issue_value
from tests.db.helpers import run, scalar, violates
from tests.db.test_bom_constraints import m  # noqa: F401
from tests.db.test_production_constraints import _complete, _started, p  # noqa: F401
from tests.db.test_sales_constraints import GSTIN_KA, GSTIN_TN, _approve, _benchmark_invoice, _oline, _order, fy_no

D = Decimal
D0 = date(2026, 11, 20)
_n = iter(range(1, 1_000_000))


# ---------------------------------------------------------------- builders
@pytest.fixture
def x(db_session, m, p):  # noqa: F811
    """Benchmark masters + run operator (from the BOM/production fixtures) + sales and supplier masters, committed."""
    s = db_session
    run(s, "INSERT INTO users (username, full_name, password_hash, role) VALUES ('sam', 'Sam', 'x', 'salesman'), ('ada', 'Ada', 'x', 'admin')")
    run(s, "INSERT INTO suppliers (name, state_code) VALUES ('Supplier S', '33'), ('Supplier T', '29')")
    run(s, "INSERT INTO customers (name, customer_type, gstin, state_code, address) VALUES "
           "('Retailer A', 'retailer', :a, '33', 'Chennai'), ('Distributor B', 'distributor', :b, '29', 'Bengaluru')", a=GSTIN_TN, b=GSTIN_KA)
    d = dict(m)
    d.update(p)
    d.update(sku=m["sku200"], u=p["user"], sam=scalar(s, "SELECT id FROM users WHERE username='sam'"), ada=scalar(s, "SELECT id FROM users WHERE username='ada'"),
             A=scalar(s, "SELECT id FROM customers WHERE name='Retailer A'"), B=scalar(s, "SELECT id FROM customers WHERE name='Distributor B'"),
             sup=scalar(s, "SELECT id FROM suppliers WHERE name='Supplier S'"), sup2=scalar(s, "SELECT id FROM suppliers WHERE name='Supplier T'"))
    s.commit()
    return d


def _mv(s, x, key, item, mtype, qty, value=None, cost="auto", on=D0, **source):
    """Insert one ledger row; the item kind is copied from the item. `source` holds the ONE source column (or none, or two).

    `unit_cost` is the effective cost per base unit, value / qty rounded to 4 places; pass cost=None or an explicit number to override.
    """
    if cost == "auto":
        cost = None if value is None else (D(str(value)) / D(str(qty))).quantize(D("0.0001"), rounding="ROUND_HALF_UP")
    cols = dict(posting_key=key, movement_date=on, movement_type=mtype, qty_base=qty, unit_cost=cost, value=value, created_by=x["u"], **source)
    names = ", ".join(cols)
    run(s, f"INSERT INTO stock_movements (item_id, item_kind, {names}) SELECT id, kind, {', '.join(':' + k for k in cols)} FROM items WHERE id = :item",
        item=item, **cols)
    return scalar(s, "SELECT id FROM stock_movements WHERE posting_key = :k", k=key)


def _adj(s, x, item, qty, cost=None, kind="opening", reason="opening stock", on=D0):
    run(s, "INSERT INTO stock_adjustments (adjustment_no, kind, item_id, item_kind, qty_delta_base, unit_cost, reason, adjustment_date, created_by) "
           "SELECT :no, :k, id, kind, :q, :c, :r, :d, :u FROM items WHERE id = :i",
        no=f"ADJ-{next(_n):05d}", k=kind, i=item, q=qty, c=cost, r=reason, d=on, u=x["u"])
    return scalar(s, "SELECT max(id) FROM stock_adjustments")


def _value(qty, cost):
    return None if cost is None else (D(qty) * D(cost)).quantize(D("0.01"), rounding="ROUND_HALF_UP")


def _open(s, x, item, qty, cost=None, key=None):
    """Opening stock: an adjustment and its movement. Raw/packaging carry a cost; finished goods do not."""
    adj = _adj(s, x, item, qty, cost)
    return _mv(s, x, key or f"adjustment:{adj}", item, "opening", qty, value=_value(qty, cost), cost=cost, adjustment_id=adj)


def _balance(s, item):
    return tuple(run(s, "SELECT coalesce(sum(qty_base), 0), coalesce(sum(value), 0) FROM stock_movements WHERE item_id = :i", i=item).one())


def _issue_value(s, item, qty):
    """The WAC value an issue of `qty` would take at this instant (single implementation: domain.costing)."""
    on_hand_qty, on_hand_value = _balance(s, item)
    return wac_issue_value(D(qty), on_hand_qty, on_hand_value)


def _purchase(s, x, no=None, invoice_no=None, supplier=None, lines=(("flour", "100", "42"),), receipts=True, **over):
    """A purchase header, its lines (item key, qty in the item's base unit, rate) and the receipt movements. Returns (id, [line ids])."""
    n = next(_n)
    h = dict(purchase_no=no or f"PUR/{n:04d}", supplier_id=supplier or x["sup"], supplier_invoice_no=invoice_no or f"SI-{n}", purchase_date=D0, created_by=x["u"])
    h.update(over)
    run(s, f"INSERT INTO purchases ({', '.join(h)}) VALUES ({', '.join(':' + k for k in h)})", **h)
    pid = scalar(s, "SELECT id FROM purchases WHERE purchase_no = :n", n=h["purchase_no"])
    ids = []
    for item, qty, rate in lines:
        line = _pline(s, pid, x[item], qty, rate)
        ids.append(line)
        if receipts:
            _receive(s, x, line)
    return pid, ids


def _pline(s, pid, item, qty, rate, **over):
    v = dict(qty=qty, rate=rate)
    v.update(over)
    run(s, "INSERT INTO purchase_lines (purchase_id, item_id, item_kind, base_unit, qty, unit, qty_base, rate, amount, gst_rate, tax_amount) "
           "SELECT :p, i.id, i.kind, i.base_unit, :qty, coalesce(CAST(:unit AS unit_code), i.base_unit), "
           "coalesce(CAST(:qb AS numeric), round(CAST(:qty AS numeric) * unit_factor(coalesce(CAST(:unit AS unit_code), i.base_unit), i.base_unit), 3)), "
           "CAST(:rate AS numeric), coalesce(CAST(:amt AS numeric), round(CAST(:qty AS numeric) * CAST(:rate AS numeric), 2)), "
           "coalesce(CAST(:gst AS numeric), 0), coalesce(CAST(:tax AS numeric), 0) FROM items i WHERE i.id = :i",
        p=pid, i=item, unit=v.get("unit"), qb=v.get("qty_base"), amt=v.get("amount"), gst=v.get("gst_rate"), tax=v.get("tax_amount"), qty=qty, rate=rate)
    return scalar(s, "SELECT max(id) FROM purchase_lines WHERE purchase_id = :p", p=pid)


def _receive(s, x, line_id, key=None, **over):
    row = run(s, "SELECT item_id, qty_base, rate, amount FROM purchase_lines WHERE id = :l", l=line_id).one()
    v = dict(qty=row.qty_base, value=row.amount)
    v.update(over)
    return _mv(s, x, key or f"purchase_line:{line_id}:receipt", row.item_id, "purchase_receipt", v["qty"], value=v["value"], purchase_line_id=line_id)


def _reserve(s, x, oline, qty="150", item=None, status=None):
    cols = dict(order_line_id=oline, item_id=item or x["sku"], qty_base=qty, created_by=x["ada"])
    if status:
        cols["status"] = status
    run(s, f"INSERT INTO stock_reservations ({', '.join(cols)}) VALUES ({', '.join(':' + k for k in cols)})", **cols)
    return scalar(s, "SELECT id FROM stock_reservations WHERE order_line_id = :o", o=oline)


def _approved_line(s, x, qty="150", customer="A"):
    order = _order(s, x, customer)
    line = _oline(s, order, x["sku"], qty, "50.00")
    _approve(s, x, order)
    return order, line


def _dispatch(s, x, invoice_line, qty="-150", key=None, **over):
    return _mv(s, x, key or f"invoice_line:{invoice_line}:dispatch", x["sku"], "dispatch", qty, invoice_line_id=invoice_line, **over)


def _invoice_line_of(s, invoice):
    return scalar(s, "SELECT id FROM invoice_lines WHERE invoice_id = :i", i=invoice)


# ================================================================ purchases and purchase lines
def test_a_purchase_with_lines_and_receipts_commits(db_session, x):
    pid, lines = _purchase(db_session, x, lines=(("flour", "60", "45"), ("oil", "10", "140")))
    db_session.commit()
    assert scalar(db_session, "SELECT status FROM purchases WHERE id = :p", p=pid) == "posted"
    assert _balance(db_session, x["flour"]) == (D("60.000"), D("2700.00"))
    assert _balance(db_session, x["oil"]) == (D("10.000"), D("1400.00"))


def test_purchase_line_wrong_qty_base_rejected(db_session, x):
    """250 g stored as 250 for a kg item is exactly the mistake this rule exists for."""
    pid, _ = _purchase(db_session, x, lines=(), receipts=False)
    with violates(db_session, pge.CheckViolation, "ck_purchase_lines_qty_base"):
        _pline(db_session, pid, x["flour"], "250", "42", unit="g", qty_base="250")


def test_purchase_line_converts_grams_to_the_base_unit(db_session, x):
    pid, _ = _purchase(db_session, x, lines=(), receipts=False)
    line = _pline(db_session, pid, x["flour"], "250", "0.042", unit="g")
    assert scalar(db_session, "SELECT qty_base FROM purchase_lines WHERE id = :l", l=line) == D("0.250")


def test_purchase_line_incompatible_unit_rejected(db_session, x):
    pid, _ = _purchase(db_session, x, lines=(), receipts=False)
    with violates(db_session, pge.CheckViolation, "ck_purchase_lines_qty_base"):
        _pline(db_session, pid, x["flour"], "5", "42", unit="l", qty_base="5")


def test_purchase_line_rejects_finished_goods(db_session, x):
    pid, _ = _purchase(db_session, x, lines=(), receipts=False)
    with violates(db_session, pge.CheckViolation, "ck_purchase_lines_item_kind"):
        run(db_session, "INSERT INTO purchase_lines (purchase_id, item_id, item_kind, base_unit, qty, unit, qty_base, rate, amount) "
                        "SELECT :p, id, kind, base_unit, 5, base_unit, 5, 1, 5 FROM items WHERE id = :i", p=pid, i=x["sku"])


def test_purchase_line_item_kind_must_be_the_items_real_kind(db_session, x):
    pid, _ = _purchase(db_session, x, lines=(), receipts=False)
    with violates(db_session, pge.ForeignKeyViolation, "fk_purchase_lines_item_kind"):
        run(db_session, "INSERT INTO purchase_lines (purchase_id, item_id, item_kind, base_unit, qty, unit, qty_base, rate, amount) "
                        "VALUES (:p, :i, 'packaging', 'kg', 5, 'kg', 5, 1, 5)", p=pid, i=x["flour"])


def test_purchase_line_base_unit_must_be_the_items_real_base_unit(db_session, x):
    pid, _ = _purchase(db_session, x, lines=(), receipts=False)
    with violates(db_session, pge.ForeignKeyViolation, "fk_purchase_lines_item_unit"):
        run(db_session, "INSERT INTO purchase_lines (purchase_id, item_id, item_kind, base_unit, qty, unit, qty_base, rate, amount) "
                        "VALUES (:p, :i, 'raw', 'g', 5, 'g', 5, 1, 5)", p=pid, i=x["flour"])


@pytest.mark.parametrize("over, name", [
    (dict(amount="9999.00"), "ck_purchase_lines_amount"),
    (dict(gst_rate="5", tax_amount="1.00"), "ck_purchase_lines_tax"),
    (dict(gst_rate="150"), "ck_purchase_lines_gst_rate"),
])
def test_purchase_line_arithmetic_must_hold(db_session, x, over, name):
    pid, _ = _purchase(db_session, x, lines=(), receipts=False)
    with violates(db_session, pge.CheckViolation, name):
        _pline(db_session, pid, x["flour"], "100", "42", **over)


def test_purchase_line_tax_follows_the_rate(db_session, x):
    pid, _ = _purchase(db_session, x, lines=(), receipts=False)
    _pline(db_session, pid, x["flour"], "100", "42", gst_rate="5", tax_amount="210.00")


@pytest.mark.parametrize("qty, rate", [("0", "42"), ("-1", "42"), ("NaN", "42"), ("100", "-1"), ("100", "NaN")])
def test_purchase_line_quantities_and_rates_must_be_real_numbers(db_session, x, qty, rate):
    pid, _ = _purchase(db_session, x, lines=(), receipts=False)
    with violates(db_session, pge.CheckViolation):
        run(db_session, "INSERT INTO purchase_lines (purchase_id, item_id, item_kind, base_unit, qty, unit, qty_base, rate, amount) "
                        "VALUES (:p, :i, 'raw', 'kg', :q, 'kg', :q, :r, 0)", p=pid, i=x["flour"], q=qty, r=rate)


def test_duplicate_supplier_invoice_rejected(db_session, x):
    _purchase(db_session, x, invoice_no="SI-100")
    db_session.commit()
    with violates(db_session, pge.UniqueViolation, "uq_purchases_supplier_invoice"):
        _purchase(db_session, x, invoice_no="SI-100")


def test_supplier_invoice_numbers_differing_only_in_case_or_spaces_are_duplicates(db_session, x):
    _purchase(db_session, x, invoice_no="SI-100")
    db_session.commit()
    with violates(db_session, pge.UniqueViolation, "uq_purchases_supplier_invoice"):
        _purchase(db_session, x, invoice_no="  si-100 ")


def test_the_same_invoice_number_from_another_supplier_is_fine(db_session, x):
    _purchase(db_session, x, invoice_no="SI-100")
    _purchase(db_session, x, invoice_no="SI-100", supplier=x["sup2"])
    db_session.commit()


def test_purchase_number_is_unique(db_session, x):
    _purchase(db_session, x, no="PUR/1")
    db_session.commit()
    with violates(db_session, pge.UniqueViolation, "purchases_purchase_no_key"):
        _purchase(db_session, x, no="PUR/1")


def test_an_item_appears_once_per_purchase(db_session, x):
    with violates(db_session, pge.UniqueViolation, "purchase_lines_purchase_id_item_id_key"):
        _purchase(db_session, x, lines=(("flour", "10", "42"), ("flour", "5", "42")))


@pytest.mark.parametrize("over", [dict(purchase_no="  "), dict(supplier_invoice_no="")])
def test_purchase_identifiers_cannot_be_blank(db_session, x, over):
    with violates(db_session, pge.CheckViolation):
        _purchase(db_session, x, lines=(), receipts=False, **over)


def test_purchase_lines_cannot_be_updated_or_deleted(db_session, x):
    _, (line,) = _purchase(db_session, x)
    db_session.commit()
    with violates(db_session, pge.RestrictViolation, message="append-only"):
        run(db_session, "UPDATE purchase_lines SET rate = 1 WHERE id = :l", l=line)
    with violates(db_session, pge.RestrictViolation, message="append-only"):
        run(db_session, "DELETE FROM purchase_lines WHERE id = :l", l=line)


def test_a_line_cannot_be_added_to_a_purchase_posted_earlier(db_session, x):
    pid, _ = _purchase(db_session, x)
    db_session.commit()
    with violates(db_session, pge.RestrictViolation, message="posted purchase"):
        _pline(db_session, pid, x["oil"], "5", "140")


def test_a_purchase_line_without_its_receipt_movement_fails_at_commit(db_session, x):
    _purchase(db_session, x, receipts=False)
    with violates(db_session, pge.CheckViolation, message="no receipt movement"):
        db_session.commit()
    assert scalar(db_session, "SELECT count(*) FROM purchases") == 0


@pytest.mark.parametrize("over, why", [(dict(qty="99", value="4158.00"), "quantity"), (dict(value="4199.00"), "value")])
def test_a_receipt_must_mirror_its_purchase_line(db_session, x, over, why):
    pid, (line,) = _purchase(db_session, x, receipts=False)
    _receive(db_session, x, line, **over)
    with violates(db_session, pge.CheckViolation, message="does not match purchase line"):
        db_session.commit()


def test_header_fields_of_a_posted_purchase_cannot_change(db_session, x):
    pid, _ = _purchase(db_session, x)
    db_session.commit()
    for assignment in ("supplier_invoice_no = 'X'", "supplier_id = %d" % x["sup2"], "purchase_date = '2026-01-01'", "purchase_no = 'Y'"):
        with violates(db_session, pge.RestrictViolation, message="cannot be changed"):
            run(db_session, f"UPDATE purchases SET {assignment} WHERE id = :p", p=pid)


def test_a_purchase_cannot_be_deleted(db_session, x):
    pid, _ = _purchase(db_session, x)
    db_session.commit()
    with violates(db_session, pge.RestrictViolation, message="cannot be deleted"):
        run(db_session, "DELETE FROM purchases WHERE id = :p", p=pid)


def _void(s, x, pid, reversals=True, reason="Wrong supplier"):
    run(s, "UPDATE purchases SET status = 'voided', voided_at = now(), voided_by = :u, void_reason = :r WHERE id = :p", u=x["u"], r=reason, p=pid)
    if reversals:
        for mv in run(s, "SELECT m.id, m.item_id, m.qty_base, m.value, m.unit_cost, m.purchase_line_id, m.posting_key FROM stock_movements m "
                         "JOIN purchase_lines l ON l.id = m.purchase_line_id WHERE l.purchase_id = :p AND m.reverses_movement_id IS NULL", p=pid).all():
            _mv(s, x, f"{mv.posting_key}:reversal", mv.item_id, "purchase_receipt", -mv.qty_base, value=-mv.value,
                purchase_line_id=mv.purchase_line_id, reverses_movement_id=mv.id)


def test_voiding_a_purchase_reverses_its_receipts_and_keeps_both_rows(db_session, x):
    pid, _ = _purchase(db_session, x)
    db_session.commit()
    _void(db_session, x, pid)
    db_session.commit()
    assert _balance(db_session, x["flour"]) == (D("0.000"), D("0.00"))
    assert scalar(db_session, "SELECT count(*) FROM stock_movements") == 2


def test_voiding_a_purchase_without_reversing_its_stock_fails_at_commit(db_session, x):
    pid, _ = _purchase(db_session, x)
    db_session.commit()
    _void(db_session, x, pid, reversals=False)
    with violates(db_session, pge.CheckViolation, message="not reversed"):
        db_session.commit()
    assert scalar(db_session, "SELECT status FROM purchases WHERE id = :p", p=pid) == "posted"


def test_a_receipt_reversal_needs_a_voided_purchase(db_session, x):
    pid, (line,) = _purchase(db_session, x)
    db_session.commit()
    original = scalar(db_session, "SELECT id FROM stock_movements")
    _mv(db_session, x, "rev", x["flour"], "purchase_receipt", "-100", value="-4200.00", purchase_line_id=line, reverses_movement_id=original)
    with violates(db_session, pge.CheckViolation, message="purchase is not voided"):
        db_session.commit()


def test_purchase_status_lifecycle(db_session, x):
    pid, _ = _purchase(db_session, x)
    db_session.commit()
    _void(db_session, x, pid)
    db_session.commit()
    with violates(db_session, pge.CheckViolation, message="illegal status transition"):
        run(db_session, "UPDATE purchases SET status = 'posted' WHERE id = :p", p=pid)
    with violates(db_session, pge.CheckViolation, message="illegal status transition"):
        _purchase(db_session, x, lines=(), receipts=False, status="voided")


@pytest.mark.parametrize("assignment", ["status = 'voided'", "status = 'voided', voided_at = now()", "status = 'voided', voided_at = now(), voided_by = :u",
                                        "voided_at = now()", "void_reason = 'x'"])
def test_void_columns_must_agree_with_the_status(db_session, x, assignment):
    pid, _ = _purchase(db_session, x)
    db_session.commit()
    with violates(db_session, pge.CheckViolation):
        run(db_session, f"UPDATE purchases SET {assignment} WHERE id = :p", p=pid, u=x["u"])


def test_a_void_cannot_predate_the_purchase(db_session, x):
    pid, _ = _purchase(db_session, x)
    db_session.commit()
    with violates(db_session, pge.CheckViolation, "ck_purchases_voided_after_created"):
        run(db_session, "UPDATE purchases SET status = 'voided', voided_at = created_at - interval '1 day', voided_by = :u, void_reason = 'x' WHERE id = :p",
            u=x["u"], p=pid)
    assert scalar(db_session, "SELECT count(*) FROM stock_movements") == 1


# ================================================================ stock adjustments
def test_an_opening_adjustment_and_its_movement_commit(db_session, x):
    _open(db_session, x, x["flour"], "100", "42")
    _open(db_session, x, x["sku"], "50")
    db_session.commit()
    assert _balance(db_session, x["flour"]) == (D("100.000"), D("4200.00"))
    assert _balance(db_session, x["sku"]) == (D("50.000"), D("0"))


@pytest.mark.parametrize("kwargs, name", [
    (dict(kind="opening", qty="-5", cost="42"), "ck_stock_adjustments_opening_positive"),
    (dict(kind="count", qty="0", cost="42"), "ck_stock_adjustments_delta_nonzero"),
    (dict(kind="count", qty="NaN", cost="42"), "ck_stock_adjustments_delta_nonzero"),
    (dict(kind="opening", qty="5", cost="-1"), "ck_stock_adjustments_unit_cost"),
    (dict(kind="opening", qty="5", cost="NaN"), "ck_stock_adjustments_unit_cost"),
    (dict(kind="opening", qty="5", cost=None), "ck_stock_adjustments_opening_cost"),
    (dict(kind="other", qty="5", cost="42"), "ck_stock_adjustments_kind"),
    (dict(kind="count", qty="5", cost="42", reason="  "), "ck_stock_adjustments_reason_not_blank"),
])
def test_adjustment_rules(db_session, x, kwargs, name):
    with violates(db_session, pge.CheckViolation, name):
        _adj(db_session, x, x["flour"], **kwargs)


def test_finished_goods_adjustments_carry_no_cost(db_session, x):
    with violates(db_session, pge.CheckViolation, "ck_stock_adjustments_finished_no_cost"):
        _adj(db_session, x, x["sku"], "5", "10")


def test_adjustment_number_is_unique(db_session, x):
    run(db_session, "INSERT INTO stock_adjustments (adjustment_no, kind, item_id, item_kind, qty_delta_base, unit_cost, reason, adjustment_date, created_by) "
                    "VALUES ('A1', 'opening', :i, 'raw', 5, 1, 'r', :d, :u)", i=x["flour"], d=D0, u=x["u"])
    with violates(db_session, pge.UniqueViolation, "stock_adjustments_adjustment_no_key"):
        run(db_session, "INSERT INTO stock_adjustments (adjustment_no, kind, item_id, item_kind, qty_delta_base, unit_cost, reason, adjustment_date, created_by) "
                        "VALUES ('A1', 'opening', :i, 'raw', 5, 1, 'r', :d, :u)", i=x["flour"], d=D0, u=x["u"])


def test_adjustment_item_kind_must_be_the_items_real_kind(db_session, x):
    with violates(db_session, pge.ForeignKeyViolation, "fk_stock_adjustments_item_kind"):
        run(db_session, "INSERT INTO stock_adjustments (adjustment_no, kind, item_id, item_kind, qty_delta_base, unit_cost, reason, adjustment_date, created_by) "
                        "VALUES ('A1', 'opening', :i, 'packaging', 5, 1, 'r', :d, :u)", i=x["flour"], d=D0, u=x["u"])


def test_adjustments_cannot_be_updated_or_deleted(db_session, x):
    adj = _adj(db_session, x, x["flour"], "5", "42")
    _mv(db_session, x, "k", x["flour"], "opening", "5", value="210.00", cost="42", adjustment_id=adj)
    db_session.commit()
    with violates(db_session, pge.RestrictViolation, message="append-only"):
        run(db_session, "UPDATE stock_adjustments SET reason = 'edited' WHERE id = :a", a=adj)
    with violates(db_session, pge.RestrictViolation, message="append-only"):
        run(db_session, "DELETE FROM stock_adjustments WHERE id = :a", a=adj)


def test_an_adjustment_without_its_movement_fails_at_commit(db_session, x):
    _adj(db_session, x, x["flour"], "5", "42")
    with violates(db_session, pge.CheckViolation, message="no movement"):
        db_session.commit()
    assert scalar(db_session, "SELECT count(*) FROM stock_adjustments") == 0


@pytest.mark.parametrize("mtype, kind", [("opening", "count"), ("count_adjustment", "opening")])
def test_a_movement_must_be_of_the_adjustments_kind(db_session, x, mtype, kind):
    adj = _adj(db_session, x, x["flour"], "5", "42", kind=kind)
    _mv(db_session, x, "k", x["flour"], mtype, "5", value="210.00", cost="42", adjustment_id=adj)
    with violates(db_session, pge.CheckViolation, message="adjustment"):
        db_session.commit()


@pytest.mark.parametrize("over", [dict(qty="6", value="252.00"), dict(cost="43", value="215.00")])
def test_an_adjustment_movement_must_mirror_the_adjustment(db_session, x, over):
    adj = _adj(db_session, x, x["flour"], "5", "42")
    v = dict(qty="5", value="210.00", cost="42")
    v.update(over)
    _mv(db_session, x, "k", x["flour"], "opening", v["qty"], value=v["value"], cost=v["cost"], adjustment_id=adj)
    with violates(db_session, pge.CheckViolation, message="adjustment"):
        db_session.commit()


def test_an_adjustment_movement_must_be_for_the_adjustments_item(db_session, x):
    adj = _adj(db_session, x, x["flour"], "5", "42")
    with violates(db_session, pge.ForeignKeyViolation, "fk_stock_movements_adjustment_item"):
        _mv(db_session, x, "k", x["oil"], "opening", "5", value="210.00", cost="42", adjustment_id=adj)


def test_a_count_adjustment_can_reduce_stock_but_not_below_zero(db_session, x):
    _open(db_session, x, x["flour"], "100", "42")
    db_session.commit()
    adj = _adj(db_session, x, x["flour"], "-10", None, kind="count", reason="spoilage")
    _mv(db_session, x, f"adjustment:{adj}", x["flour"], "count_adjustment", "-10", value=-_issue_value(db_session, x["flour"], "10"), cost="42", adjustment_id=adj)
    db_session.commit()
    assert _balance(db_session, x["flour"]) == (D("90.000"), D("3780.00"))
    adj = _adj(db_session, x, x["flour"], "-91", None, kind="count", reason="theft")
    _mv(db_session, x, f"adjustment:{adj}", x["flour"], "count_adjustment", "-91", value="-3822.00", cost="42", adjustment_id=adj)
    with violates(db_session, pge.CheckViolation, message="negative stock"):
        db_session.commit()


# ================================================================ stock movements: one source, types, signs, values
def test_movement_requires_exactly_one_source(db_session, x):
    adj = _adj(db_session, x, x["flour"], "5", "42")
    pid, (line,) = _purchase(db_session, x, receipts=False)
    with violates(db_session, pge.CheckViolation, "ck_stock_movements_one_source"):
        _mv(db_session, x, "none", x["flour"], "opening", "5", value="210.00", cost="42")
    with violates(db_session, pge.CheckViolation, "ck_stock_movements_one_source"):
        _mv(db_session, x, "two", x["flour"], "opening", "5", value="210.00", cost="42", adjustment_id=adj, purchase_line_id=line)


@pytest.mark.parametrize("mtype, column", [
    ("purchase_receipt", "adjustment_id"), ("opening", "purchase_line_id"), ("count_adjustment", "purchase_line_id"),
    ("production_issue", "adjustment_id"), ("dispatch", "production_run_id"), ("production_output", "invoice_line_id"),
])
def test_each_movement_type_has_its_own_source_column(db_session, x, mtype, column):
    ids = dict(adjustment_id=_adj(db_session, x, x["flour"], "5", "42"))
    pid, (line,) = _purchase(db_session, x, receipts=False)
    ids["purchase_line_id"] = line
    ids["production_run_id"] = _started(db_session, x, x)
    _, oline, invoice = _benchmark_invoice(db_session, x, "A")
    ids["invoice_line_id"] = _invoice_line_of(db_session, invoice)
    item = x["sku"] if mtype in ("dispatch", "production_output") else x["flour"]
    sign = "-5" if mtype in ("production_issue", "dispatch") else "5"
    value = None if item == x["sku"] else ("-210.00" if sign == "-5" else "210.00")
    with violates(db_session, pge.CheckViolation, "ck_stock_movements_type_source"):
        _mv(db_session, x, "k", item, mtype, sign, value=value, cost=None if item == x["sku"] else "42", **{column: ids[column]})


def test_movement_type_must_be_known(db_session, x):
    adj = _adj(db_session, x, x["flour"], "5", "42")
    with violates(db_session, pge.CheckViolation, "ck_stock_movements_movement_type"):
        _mv(db_session, x, "k", x["flour"], "transfer", "5", value="210.00", cost="42", adjustment_id=adj)


def test_finished_movement_must_have_no_value(db_session, x):
    adj = _adj(db_session, x, x["sku"], "50")
    with violates(db_session, pge.CheckViolation, "ck_stock_movements_valued_iff_stock_item"):
        _mv(db_session, x, "k", x["sku"], "opening", "50", value="100.00", cost="2", adjustment_id=adj)


def test_raw_movement_must_have_value(db_session, x):
    adj = _adj(db_session, x, x["flour"], "5", "42")
    with violates(db_session, pge.CheckViolation, "ck_stock_movements_valued_iff_stock_item"):
        _mv(db_session, x, "k", x["flour"], "opening", "5", adjustment_id=adj)


def test_packaging_movement_must_have_value(db_session, x):
    adj = _adj(db_session, x, x["pouch"], "5", "2.5")
    with violates(db_session, pge.CheckViolation, "ck_stock_movements_valued_iff_stock_item"):
        _mv(db_session, x, "k", x["pouch"], "opening", "5", adjustment_id=adj)


def test_value_and_unit_cost_come_together(db_session, x):
    adj = _adj(db_session, x, x["flour"], "5", "42")
    with violates(db_session, pge.CheckViolation, "ck_stock_movements_cost_pair"):
        _mv(db_session, x, "k", x["flour"], "opening", "5", value="210.00", cost=None, adjustment_id=adj)


@pytest.mark.parametrize("mtype, qty, value", [
    ("production_issue", "5", "210.00"), ("production_scrap", "5", "210.00"), ("purchase_receipt", "-5", "-210.00"), ("opening", "-5", "-210.00"),
])
def test_issue_must_be_negative_and_receipt_positive(db_session, x, mtype, qty, value):
    source = {}
    if mtype in ("production_issue", "production_scrap"):
        source = dict(production_run_id=_started(db_session, x, x))
    elif mtype == "purchase_receipt":
        _, (line,) = _purchase(db_session, x, receipts=False)
        source = dict(purchase_line_id=line)
    else:
        source = dict(adjustment_id=_adj(db_session, x, x["flour"], "5", "42"))
    with violates(db_session, pge.CheckViolation, "ck_stock_movements_sign"):
        _mv(db_session, x, "k", x["flour"], mtype, qty, value=value, cost="42", **source)


@pytest.mark.parametrize("mtype, qty", [("dispatch", "150"), ("production_output", "-188")])
def test_finished_movement_signs(db_session, x, mtype, qty):
    if mtype == "dispatch":
        _, _, invoice = _benchmark_invoice(db_session, x, "A")
        source = dict(invoice_line_id=_invoice_line_of(db_session, invoice))
    else:
        source = dict(production_run_id=_started(db_session, x, x))
    with violates(db_session, pge.CheckViolation, "ck_stock_movements_sign"):
        _mv(db_session, x, "k", x["sku"], mtype, qty, **source)


def test_value_has_the_sign_of_the_quantity(db_session, x):
    adj = _adj(db_session, x, x["flour"], "5", "42")
    with violates(db_session, pge.CheckViolation, "ck_stock_movements_value_sign"):
        _mv(db_session, x, "k", x["flour"], "opening", "5", value="-210.00", cost="42", adjustment_id=adj)


def test_unit_cost_is_the_value_per_unit(db_session, x):
    adj = _adj(db_session, x, x["flour"], "5", "42")
    with violates(db_session, pge.CheckViolation, "ck_stock_movements_value_unit_cost"):
        _mv(db_session, x, "k", x["flour"], "opening", "5", value="210.00", cost="41", adjustment_id=adj)


@pytest.mark.parametrize("mtype, item", [
    ("production_output", "flour"), ("dispatch", "flour"), ("purchase_receipt", "sku"), ("production_issue", "sku"), ("production_scrap", "sku"),
])
def test_movement_type_must_suit_the_item_kind(db_session, x, mtype, item):
    source = {}
    if mtype == "purchase_receipt":
        _, (line,) = _purchase(db_session, x, receipts=False)
        source = dict(purchase_line_id=line)
    elif mtype == "dispatch":
        _, _, invoice = _benchmark_invoice(db_session, x, "A")
        source = dict(invoice_line_id=_invoice_line_of(db_session, invoice))
    else:
        source = dict(production_run_id=_started(db_session, x, x))
    sign = "-5" if mtype in ("dispatch", "production_issue", "production_scrap") else "5"
    valued = item != "sku"
    with violates(db_session, pge.CheckViolation, "ck_stock_movements_type_kind"):
        _mv(db_session, x, "k", x[item], mtype, sign, value=(("-" if sign == "-5" else "") + "210.00") if valued else None, cost="42" if valued else None, **source)


@pytest.mark.parametrize("qty", ["0", "NaN"])
def test_quantity_must_be_a_real_nonzero_number(db_session, x, qty):
    adj = _adj(db_session, x, x["flour"], "5", "42")
    with violates(db_session, pge.CheckViolation, "ck_stock_movements_qty"):
        _mv(db_session, x, "k", x["flour"], "opening", qty, value="0", cost="42", adjustment_id=adj)


@pytest.mark.parametrize("value, cost", [("NaN", "42"), ("210.00", "NaN"), ("210.00", "-1")])
def test_value_and_cost_must_be_real_numbers(db_session, x, value, cost):
    adj = _adj(db_session, x, x["flour"], "5", "42")
    with violates(db_session, pge.CheckViolation):
        _mv(db_session, x, "k", x["flour"], "opening", "5", value=value, cost=cost, adjustment_id=adj)


def test_finished_goods_move_in_whole_packets(db_session, x):
    adj = _adj(db_session, x, x["sku"], "5.5")
    with violates(db_session, pge.CheckViolation, "ck_stock_movements_whole_packets"):
        _mv(db_session, x, "k", x["sku"], "opening", "5.5", adjustment_id=adj)


def test_raw_materials_may_move_in_fractions(db_session, x):
    _open(db_session, x, x["oil"], "10.5", "140")
    db_session.commit()
    assert _balance(db_session, x["oil"]) == (D("10.500"), D("1470.00"))


def test_movement_item_must_match_source_line_item(db_session, x):
    _, (line,) = _purchase(db_session, x, receipts=False)
    with violates(db_session, pge.ForeignKeyViolation, "fk_stock_movements_purchase_line_item"):
        _mv(db_session, x, "k", x["oil"], "purchase_receipt", "100", value="4200.00", cost="42", purchase_line_id=line)


def test_dispatch_item_must_match_the_invoice_line_item(db_session, x):
    _, _, invoice = _benchmark_invoice(db_session, x, "A")
    line = _invoice_line_of(db_session, invoice)
    other_sku = scalar(db_session, "SELECT id FROM items WHERE code = 'MIX-200'")
    with violates(db_session, pge.ForeignKeyViolation, "fk_stock_movements_invoice_line_item"):
        _mv(db_session, x, "k", other_sku, "dispatch", "-150", invoice_line_id=line)


def test_movement_item_kind_must_be_the_items_real_kind(db_session, x):
    adj = _adj(db_session, x, x["flour"], "5", "42")
    with violates(db_session, pge.ForeignKeyViolation, "fk_stock_movements_item_kind"):
        run(db_session, "INSERT INTO stock_movements (posting_key, item_id, item_kind, movement_date, movement_type, qty_base, unit_cost, value, created_by, adjustment_id) "
                        "VALUES ('k', :i, 'packaging', :d, 'opening', 5, 42, 210, :u, :a)", i=x["flour"], d=D0, u=x["u"], a=adj)


def test_a_movement_needs_a_source_that_exists(db_session, x):
    with violates(db_session, pge.ForeignKeyViolation, "fk_stock_movements_purchase_line_item"):
        _mv(db_session, x, "k", x["flour"], "purchase_receipt", "5", value="210.00", cost="42", purchase_line_id=99999)


def test_posting_key_is_required_and_not_blank(db_session, x):
    adj = _adj(db_session, x, x["flour"], "5", "42")
    with violates(db_session, pge.CheckViolation, "ck_stock_movements_posting_key_not_blank"):
        _mv(db_session, x, "  ", x["flour"], "opening", "5", value="210.00", cost="42", adjustment_id=adj)


def test_duplicate_posting_key_rejected(db_session, x):
    _open(db_session, x, x["flour"], "5", "42", key="same")
    db_session.commit()
    adj = _adj(db_session, x, x["oil"], "5", "140")
    with violates(db_session, pge.UniqueViolation, "stock_movements_posting_key_key"):
        _mv(db_session, x, "same", x["oil"], "opening", "5", value="700.00", cost="140", adjustment_id=adj)


def test_the_same_posting_can_be_replayed_with_on_conflict_do_nothing(db_session, x):
    """The service's idempotent insert: a replay with the same key adds no row."""
    _open(db_session, x, x["flour"], "5", "42", key="same")
    db_session.commit()
    adj = scalar(db_session, "SELECT max(id) FROM stock_adjustments")
    run(db_session, "INSERT INTO stock_movements (posting_key, item_id, item_kind, movement_date, movement_type, qty_base, unit_cost, value, created_by, adjustment_id) "
                    "VALUES ('same', :i, 'raw', :d, 'opening', 5, 42, 210, :u, :a) ON CONFLICT (posting_key) DO NOTHING", i=x["flour"], d=D0, u=x["u"], a=adj)
    db_session.commit()
    assert scalar(db_session, "SELECT count(*) FROM stock_movements") == 1


# ---------------------------------------------------------------- one-shot events cannot be posted twice, whatever the key
def test_second_receipt_for_a_purchase_line_rejected(db_session, x):
    _, (line,) = _purchase(db_session, x)
    db_session.commit()
    with violates(db_session, pge.UniqueViolation, "uq_stock_movements_receipt_once"):
        _receive(db_session, x, line, key="a different key")


def test_second_dispatch_for_line_rejected(db_session, x):
    _open(db_session, x, x["sku"], "238")
    _, _, invoice = _benchmark_invoice(db_session, x, "A")
    line = _invoice_line_of(db_session, invoice)
    _dispatch(db_session, x, line)
    db_session.commit()
    with violates(db_session, pge.UniqueViolation, "uq_stock_movements_dispatch_once"):
        _dispatch(db_session, x, line, key="another key")


def test_second_production_output_for_a_run_rejected(db_session, x):
    run_id = _started(db_session, x, x)
    _complete(db_session, x, run_id, "188")
    _mv(db_session, x, "run:out", x["sku"], "production_output", "188", production_run_id=run_id)
    db_session.commit()
    with violates(db_session, pge.UniqueViolation, "uq_stock_movements_output_once"):
        _mv(db_session, x, "run:out:again", x["sku"], "production_output", "188", production_run_id=run_id)


def test_second_movement_for_an_adjustment_rejected(db_session, x):
    adj = _adj(db_session, x, x["flour"], "5", "42")
    _mv(db_session, x, "k1", x["flour"], "opening", "5", value="210.00", cost="42", adjustment_id=adj)
    db_session.commit()
    with violates(db_session, pge.UniqueViolation, "uq_stock_movements_adjustment_once"):
        _mv(db_session, x, "k2", x["flour"], "opening", "5", value="210.00", cost="42", adjustment_id=adj)


def test_issues_for_one_run_may_repeat_with_distinct_keys(db_session, x):
    """Issuing more material to a run is legitimate; only the one-shot events are structurally unique."""
    _open(db_session, x, x["flour"], "100", "42")
    run_id = _started(db_session, x, x)
    _mv(db_session, x, "i1", x["flour"], "production_issue", "-30", value="-1260.00", cost="42", production_run_id=run_id)
    _mv(db_session, x, "i2", x["flour"], "production_issue", "-10", value="-420.00", cost="42", production_run_id=run_id)
    db_session.commit()
    assert _balance(db_session, x["flour"]) == (D("60.000"), D("2520.00"))


# ---------------------------------------------------------------- append-only
def test_movement_update_and_delete_rejected(db_session, x):
    mv = _open(db_session, x, x["flour"], "5", "42")
    db_session.commit()
    for statement in ("UPDATE stock_movements SET batch_no = 'x' WHERE id = :m", "UPDATE stock_movements SET qty_base = 6 WHERE id = :m",
                      "DELETE FROM stock_movements WHERE id = :m"):
        with violates(db_session, pge.RestrictViolation, message="append-only"):
            run(db_session, statement, m=mv)


@pytest.mark.parametrize("table", ["stock_movements", "stock_adjustments", "purchase_lines"])
def test_truncate_is_rejected_on_the_ledger_tables(db_session, x, table):
    _purchase(db_session, x)
    db_session.commit()
    with violates(db_session, pge.RestrictViolation, message="append-only"):
        run(db_session, f"TRUNCATE {table} CASCADE")


# ---------------------------------------------------------------- reversals keep the audit trail
def test_a_dispatch_can_be_reversed_once_when_its_invoice_is_cancelled(db_session, x):
    from tests.db.test_sales_constraints import _cancel_with_credit_note
    _open(db_session, x, x["sku"], "238")
    _, _, invoice = _benchmark_invoice(db_session, x, "A")
    line = _invoice_line_of(db_session, invoice)
    original = _dispatch(db_session, x, line)
    db_session.commit()
    _cancel_with_credit_note(db_session, x, invoice, restock=True)
    _mv(db_session, x, f"invoice_line:{line}:dispatch:reversal", x["sku"], "dispatch", "150", invoice_line_id=line, reverses_movement_id=original)
    db_session.commit()
    assert _balance(db_session, x["sku"])[0] == D("238.000")
    assert scalar(db_session, "SELECT count(*) FROM stock_movements WHERE item_id = :i", i=x["sku"]) == 3  # opening, dispatch, reversal: nothing erased


def _reversible_dispatch(s, x):
    _open(s, x, x["sku"], "238")
    _, _, invoice = _benchmark_invoice(s, x, "A")
    line = _invoice_line_of(s, invoice)
    original = _dispatch(s, x, line)
    s.commit()
    return invoice, line, original


def test_reversal_is_unique(db_session, x):
    from tests.db.test_sales_constraints import _cancel_with_credit_note
    invoice, line, original = _reversible_dispatch(db_session, x)
    _cancel_with_credit_note(db_session, x, invoice, restock=True)
    _mv(db_session, x, "rev1", x["sku"], "dispatch", "150", invoice_line_id=line, reverses_movement_id=original)
    db_session.commit()
    with violates(db_session, pge.UniqueViolation, "stock_movements_reverses_movement_id_key"):
        _mv(db_session, x, "rev2", x["sku"], "dispatch", "150", invoice_line_id=line, reverses_movement_id=original)


def test_reversal_must_be_same_item(db_session, x):
    """The mirror trigger is the first layer (it also compares the source), so it is disabled INSIDE this test's transaction (rolled
    back afterwards) to show that the reversal foreign key on its own refuses a movement of a different item. The reversal row is
    valid at statement level: its own purchase line, type, and mirrored quantity and value."""
    pid, (flour_line, oil_line) = _purchase(db_session, x, lines=(("flour", "100", "42"), ("oil", "100", "140")))
    db_session.commit()      # no pending deferred checks, so the trigger can be disabled for this transaction only
    original = scalar(db_session, "SELECT id FROM stock_movements WHERE purchase_line_id = :l", l=flour_line)
    run(db_session, "ALTER TABLE stock_movements DISABLE TRIGGER trg_mirror")
    with violates(db_session, pge.ForeignKeyViolation, "fk_stock_movements_reverses"):
        _mv(db_session, x, "rev-wrong-item", x["oil"], "purchase_receipt", "-100", value="-14000.00", purchase_line_id=oil_line, reverses_movement_id=original)


def test_with_the_mirror_trigger_in_place_a_wrong_item_reversal_is_refused_first_by_the_mirror(db_session, x):
    pid, (flour_line, oil_line) = _purchase(db_session, x, lines=(("flour", "100", "42"), ("oil", "100", "140")))
    run(db_session, "UPDATE purchases SET status = 'voided', voided_at = now(), voided_by = :u, void_reason = 'x' WHERE id = :p", u=x["u"], p=pid)
    original = scalar(db_session, "SELECT id FROM stock_movements WHERE purchase_line_id = :l", l=flour_line)
    with violates(db_session, pge.CheckViolation, message="exact mirror"):
        _mv(db_session, x, "rev-wrong-item", x["oil"], "purchase_receipt", "-100", value="-14000.00", purchase_line_id=oil_line, reverses_movement_id=original)


def test_control_a_same_item_reversal_passes_the_statement_level_checks(db_session, x):
    """Control for the test above: the same reversal naming the right item's movement is accepted at statement level."""
    pid, (flour_line, oil_line) = _purchase(db_session, x, lines=(("flour", "100", "42"), ("oil", "100", "140")))
    run(db_session, "UPDATE purchases SET status = 'voided', voided_at = now(), voided_by = :u, void_reason = 'x' WHERE id = :p", u=x["u"], p=pid)
    original = scalar(db_session, "SELECT id FROM stock_movements WHERE purchase_line_id = :l", l=oil_line)
    _mv(db_session, x, "rev-right-item", x["oil"], "purchase_receipt", "-100", value="-14000.00", purchase_line_id=oil_line, reverses_movement_id=original)


def test_a_reversal_must_be_a_mirror_of_the_original(db_session, x):
    invoice, line, original = _reversible_dispatch(db_session, x)
    with violates(db_session, pge.CheckViolation, message="exact mirror"):
        _mv(db_session, x, "rev", x["sku"], "dispatch", "100", invoice_line_id=line, reverses_movement_id=original)


def test_a_reversal_must_carry_the_same_source_as_the_original(db_session, x):
    invoice, line, original = _reversible_dispatch(db_session, x)
    _, _, second = _benchmark_invoice(db_session, x, "B", seq=2)
    with violates(db_session, pge.CheckViolation, message="exact mirror"):
        _mv(db_session, x, "rev", x["sku"], "dispatch", "150", invoice_line_id=_invoice_line_of(db_session, second), reverses_movement_id=original)


def test_a_reversal_cannot_be_reversed(db_session, x):
    from tests.db.test_sales_constraints import _cancel_with_credit_note
    invoice, line, original = _reversible_dispatch(db_session, x)
    _cancel_with_credit_note(db_session, x, invoice, restock=True)
    rev = _mv(db_session, x, "rev", x["sku"], "dispatch", "150", invoice_line_id=line, reverses_movement_id=original)
    db_session.commit()
    with violates(db_session, pge.CheckViolation, message="cannot be reversed"):
        _mv(db_session, x, "rev-of-rev", x["sku"], "dispatch", "-150", invoice_line_id=line, reverses_movement_id=rev)


@pytest.mark.parametrize("mtype", ["production_issue", "opening"])
def test_only_dispatches_and_receipts_can_be_reversed(db_session, x, mtype):
    _open(db_session, x, x["flour"], "100", "42")
    run_id = _started(db_session, x, x)
    issue = _mv(db_session, x, "i", x["flour"], "production_issue", "-10", value="-420.00", cost="42", production_run_id=run_id)
    db_session.commit()
    original = issue if mtype == "production_issue" else scalar(db_session, "SELECT id FROM stock_movements WHERE movement_type = 'opening'")
    source = dict(production_run_id=run_id) if mtype == "production_issue" else dict(adjustment_id=scalar(db_session, "SELECT max(id) FROM stock_adjustments"))
    with violates(db_session, pge.CheckViolation, "ck_stock_movements_reversal_types"):
        _mv(db_session, x, "rev", x["flour"], mtype, "10" if mtype == "production_issue" else "-100",
            value="420.00" if mtype == "production_issue" else "-4200.00", cost="42", reverses_movement_id=original, **source)


def test_a_reversal_cannot_predate_the_original(db_session, x):
    from tests.db.test_sales_constraints import _cancel_with_credit_note
    invoice, line, original = _reversible_dispatch(db_session, x)
    _cancel_with_credit_note(db_session, x, invoice, restock=True)
    with violates(db_session, pge.CheckViolation, message="before the movement it reverses"):
        _mv(db_session, x, "rev", x["sku"], "dispatch", "150", invoice_line_id=line, reverses_movement_id=original, on=D0 - timedelta(days=1))


def test_a_dispatch_reversal_needs_a_cancelled_invoice(db_session, x):
    invoice, line, original = _reversible_dispatch(db_session, x)
    _mv(db_session, x, "rev", x["sku"], "dispatch", "150", invoice_line_id=line, reverses_movement_id=original)
    with violates(db_session, pge.CheckViolation, message="invoice is not cancelled"):
        db_session.commit()


# ---------------------------------------------------------------- movements agree with their source documents (checked at COMMIT)
def test_dispatch_must_equal_the_invoice_line_quantity(db_session, x):
    _open(db_session, x, x["sku"], "238")
    _, _, invoice = _benchmark_invoice(db_session, x, "A")
    _dispatch(db_session, x, _invoice_line_of(db_session, invoice), qty="-100")
    with violates(db_session, pge.CheckViolation, message="does not match invoice line"):
        db_session.commit()


def test_dispatch_needs_an_issued_invoice(db_session, x):
    from tests.db.test_sales_constraints import _cancel_with_credit_note
    _open(db_session, x, x["sku"], "238")
    _, _, invoice = _benchmark_invoice(db_session, x, "A")
    _cancel_with_credit_note(db_session, x, invoice)
    _dispatch(db_session, x, _invoice_line_of(db_session, invoice))
    with violates(db_session, pge.CheckViolation, message="invoice is cancelled"):
        db_session.commit()


def test_production_output_must_equal_the_runs_good_output(db_session, x):
    run_id = _started(db_session, x, x)
    _complete(db_session, x, run_id, "188")
    _mv(db_session, x, "o", x["sku"], "production_output", "200", production_run_id=run_id)
    with violates(db_session, pge.CheckViolation, message="good output"):
        db_session.commit()


def test_production_output_needs_a_completed_run(db_session, x):
    run_id = _started(db_session, x, x)
    _mv(db_session, x, "o", x["sku"], "production_output", "188", production_run_id=run_id)
    with violates(db_session, pge.CheckViolation, message="not completed"):
        db_session.commit()


def test_production_output_must_be_the_runs_sku(db_session, x):
    other_sku = scalar(db_session, "SELECT id FROM items WHERE code = 'MIX-200'")
    run_id = _started(db_session, x, x)
    _complete(db_session, x, run_id, "188")
    _mv(db_session, x, "o", other_sku, "production_output", "188", production_run_id=run_id)
    with violates(db_session, pge.CheckViolation, message="not the SKU of the run"):
        db_session.commit()


def test_run_issue_must_be_for_an_item_on_the_run(db_session, x):
    _open(db_session, x, x["oil"], "30", "140")
    salt = run(db_session, "INSERT INTO items (code, name, kind, base_unit) VALUES ('SALT', 'Salt', 'raw', 'kg') RETURNING id").scalar()
    _open(db_session, x, salt, "5", "20")
    db_session.commit()
    run_id = _started(db_session, x, x)
    db_session.commit()
    _mv(db_session, x, "i", salt, "production_issue", "-1", value="-20.00", cost="20", production_run_id=run_id)
    with violates(db_session, pge.CheckViolation, message="not on the run"):
        db_session.commit()


def test_nothing_can_be_issued_to_a_run_completed_in_an_earlier_transaction(db_session, x):
    _open(db_session, x, x["flour"], "100", "42")
    run_id = _started(db_session, x, x)
    db_session.commit()
    _complete(db_session, x, run_id, "188")
    db_session.commit()
    _mv(db_session, x, "i", x["flour"], "production_issue", "-1", value="-42.00", cost="42", production_run_id=run_id)
    with violates(db_session, pge.CheckViolation, message="already completed"):
        db_session.commit()


# ================================================================ database-level stock invariants (checked at COMMIT)
def test_trigger_catches_unchecked_insert(db_session, x):
    """A -10 movement on an empty item: no service in the way, the database still says no."""
    adj = _adj(db_session, x, x["flour"], "-10", None, kind="count", reason="count")
    _mv(db_session, x, "k", x["flour"], "count_adjustment", "-10", value="-420.00", cost="42", adjustment_id=adj)
    with violates(db_session, pge.CheckViolation, message="negative stock"):
        db_session.commit()
    assert scalar(db_session, "SELECT count(*) FROM stock_movements") == 0


def test_issue_over_on_hand_rejected_and_nothing_is_left_behind(db_session, x):
    _open(db_session, x, x["flour"], "100", "42")
    db_session.commit()
    run_id = _started(db_session, x, x)
    _mv(db_session, x, "i", x["flour"], "production_issue", "-101", value="-4242.00", cost="42", production_run_id=run_id)
    with violates(db_session, pge.CheckViolation, message="negative stock"):
        db_session.commit()
    assert _balance(db_session, x["flour"]) == (D("100.000"), D("4200.00"))
    assert scalar(db_session, "SELECT count(*) FROM production_runs") == 0, "the whole transaction rolled back, run included"


def test_issuing_exactly_everything_is_allowed(db_session, x):
    _open(db_session, x, x["flour"], "100", "42")
    run_id = _started(db_session, x, x)
    _mv(db_session, x, "i", x["flour"], "production_issue", "-100", value="-4200.00", cost="42", production_run_id=run_id)
    db_session.commit()
    assert _balance(db_session, x["flour"]) == (D("0.000"), D("0.00"))


def test_a_receipt_and_an_issue_in_one_transaction_are_judged_by_their_net_effect(db_session, x):
    run_id = _started(db_session, x, x)
    _purchase(db_session, x, lines=(("flour", "50", "42"),))
    _mv(db_session, x, "i", x["flour"], "production_issue", "-40", value="-1680.00", cost="42", production_run_id=run_id)
    db_session.commit()
    assert _balance(db_session, x["flour"])[0] == D("10.000")


def test_one_bad_movement_rolls_back_every_movement_of_the_transaction(db_session, x):
    _open(db_session, x, x["flour"], "100", "42")
    _open(db_session, x, x["oil"], "30", "140")
    db_session.commit()
    before = scalar(db_session, "SELECT count(*) FROM stock_movements")
    run_id = _started(db_session, x, x)
    _mv(db_session, x, "ok1", x["flour"], "production_issue", "-40", value="-1680.00", cost="42", production_run_id=run_id)
    _mv(db_session, x, "ok2", x["oil"], "production_issue", "-10", value="-1400.00", cost="140", production_run_id=run_id)
    _mv(db_session, x, "bad", x["oil"], "production_issue", "-25", value="-3500.00", cost="140", production_run_id=run_id)
    with violates(db_session, pge.CheckViolation, message="negative stock"):
        db_session.commit()
    assert scalar(db_session, "SELECT count(*) FROM stock_movements") == before
    assert _balance(db_session, x["oil"]) == (D("30.000"), D("4200.00"))


@pytest.mark.parametrize("issue_value, message", [
    ("-4300.00", "negative valuation"),   # takes out more value than the item holds
    ("-4000.00", "valuation residue"),    # empties the item but leaves value behind
])
def test_valuation_must_stay_consistent_with_quantity(db_session, x, issue_value, message):
    _open(db_session, x, x["flour"], "100", "42")
    run_id = _started(db_session, x, x)
    _mv(db_session, x, "i", x["flour"], "production_issue", "-100", value=issue_value, production_run_id=run_id)
    with violates(db_session, pge.CheckViolation, message=message):
        db_session.commit()


def test_an_issue_that_empties_the_item_must_take_the_residual_value(db_session, x):
    """WAC rounding can leave pennies; the emptying issue takes them, which the invariant demands."""
    _open(db_session, x, x["flour"], "3", "10.0033")  # value 30.01
    run_id = _started(db_session, x, x)
    first = _issue_value(db_session, x["flour"], "1")
    _mv(db_session, x, "i1", x["flour"], "production_issue", "-1", value=-first, production_run_id=run_id)
    db_session.commit()
    qty, value = _balance(db_session, x["flour"])
    _mv(db_session, x, "i2", x["flour"], "production_issue", f"-{qty}", value=-value, production_run_id=run_id)
    db_session.commit()
    assert _balance(db_session, x["flour"]) == (D("0.000"), D("0.00"))


# ---------------------------------------------------------------- reservations
def test_reservation_trigger_rejects_overcommit(db_session, x):
    _open(db_session, x, x["sku"], "88")
    db_session.commit()
    _, oline = _approved_line(db_session, x, "100")
    db_session.commit()
    _reserve(db_session, x, oline, "100")
    with violates(db_session, pge.CheckViolation, message="reservations exceed"):
        db_session.commit()
    assert scalar(db_session, "SELECT count(*) FROM stock_reservations") == 0


def test_a_reservation_up_to_available_stock_commits(db_session, x):
    _open(db_session, x, x["sku"], "88")
    _, oline = _approved_line(db_session, x, "88")
    _reserve(db_session, x, oline, "88")
    db_session.commit()
    assert scalar(db_session, "SELECT count(*) FROM stock_reservations WHERE status = 'active'") == 1


def test_two_reservations_together_cannot_exceed_stock(db_session, x):
    _open(db_session, x, x["sku"], "88")
    _, a = _approved_line(db_session, x, "50")
    _, b = _approved_line(db_session, x, "50", customer="B")
    _reserve(db_session, x, a, "50")
    db_session.commit()
    _reserve(db_session, x, b, "50")
    with violates(db_session, pge.CheckViolation, message="reservations exceed"):
        db_session.commit()


def test_releasing_a_reservation_frees_the_stock(db_session, x):
    _open(db_session, x, x["sku"], "88")
    _, a = _approved_line(db_session, x, "60")
    _, b = _approved_line(db_session, x, "60", customer="B")
    first = _reserve(db_session, x, a, "60")
    db_session.commit()
    run(db_session, "UPDATE stock_reservations SET status = 'released', closed_at = now() WHERE id = :r", r=first)
    _reserve(db_session, x, b, "60")
    db_session.commit()
    assert scalar(db_session, "SELECT coalesce(sum(qty_base), 0) FROM stock_reservations WHERE status = 'active'") == D("60.000")


def test_stock_cannot_drop_below_what_is_reserved(db_session, x):
    """test_adjustment_cannot_drop_below_reserved: the count adjustment would leave 88 on hand against 88 reserved minus nothing."""
    _open(db_session, x, x["sku"], "88")
    _, oline = _approved_line(db_session, x, "88")
    _reserve(db_session, x, oline, "88")
    db_session.commit()
    adj = _adj(db_session, x, x["sku"], "-1", None, kind="count", reason="damaged")
    _mv(db_session, x, f"adjustment:{adj}", x["sku"], "count_adjustment", "-1", adjustment_id=adj)
    with violates(db_session, pge.CheckViolation, message="reservations exceed"):
        db_session.commit()


def test_reservation_unique_per_order_line(db_session, x):
    _open(db_session, x, x["sku"], "238")
    _, oline = _approved_line(db_session, x, "50")
    _reserve(db_session, x, oline, "50")
    db_session.commit()
    with violates(db_session, pge.UniqueViolation, "stock_reservations_order_line_id_key"):
        _reserve(db_session, x, oline, "50")


def test_reservation_item_must_be_the_order_lines_item(db_session, x):
    other_sku = scalar(db_session, "SELECT id FROM items WHERE code = 'MIX-200'")
    _, oline = _approved_line(db_session, x, "50")
    with violates(db_session, pge.ForeignKeyViolation, "fk_stock_reservations_order_line_item"):
        _reserve(db_session, x, oline, "50", item=other_sku)


def test_reservation_quantity_must_equal_the_order_line_quantity(db_session, x):
    _open(db_session, x, x["sku"], "238")
    _, oline = _approved_line(db_session, x, "150")
    _reserve(db_session, x, oline, "100")
    with violates(db_session, pge.CheckViolation, message="order line quantity"):
        db_session.commit()


def test_only_approved_or_invoiced_orders_hold_reservations(db_session, x):
    _open(db_session, x, x["sku"], "238")
    order = _order(db_session, x, "A")
    oline = _oline(db_session, order, x["sku"], "50", "50.00")  # still 'submitted'
    _reserve(db_session, x, oline, "50")
    with violates(db_session, pge.CheckViolation, message="order is submitted"):
        db_session.commit()


@pytest.mark.parametrize("qty", ["0", "-5", "NaN"])
def test_reservation_quantity_must_be_positive(db_session, x, qty):
    _, oline = _approved_line(db_session, x, "50")
    with violates(db_session, pge.CheckViolation, "ck_stock_reservations_qty_positive"):
        _reserve(db_session, x, oline, qty)


def test_reservation_status_and_closed_at_agree(db_session, x):
    _open(db_session, x, x["sku"], "238")
    _, oline = _approved_line(db_session, x, "50")
    rid = _reserve(db_session, x, oline, "50")
    db_session.commit()
    with violates(db_session, pge.CheckViolation, "ck_stock_reservations_closed"):
        run(db_session, "UPDATE stock_reservations SET status = 'released' WHERE id = :r", r=rid)
    with violates(db_session, pge.CheckViolation, "ck_stock_reservations_closed"):
        run(db_session, "UPDATE stock_reservations SET closed_at = now() WHERE id = :r", r=rid)


def test_reservation_lifecycle_and_frozen_fields(db_session, x):
    _open(db_session, x, x["sku"], "238")
    _, oline = _approved_line(db_session, x, "50")
    rid = _reserve(db_session, x, oline, "50")
    db_session.commit()
    other_sku = scalar(db_session, "SELECT id FROM items WHERE code = 'MIX-200'")
    for assignment in ("qty_base = 10", "item_id = %d" % other_sku, "created_by = %d" % x["u"]):
        with violates(db_session, pge.RestrictViolation, message="cannot be changed"):
            run(db_session, f"UPDATE stock_reservations SET {assignment} WHERE id = :r", r=rid)
    run(db_session, "UPDATE stock_reservations SET status = 'fulfilled', closed_at = now() WHERE id = :r", r=rid)
    db_session.commit()
    with violates(db_session, pge.CheckViolation, message="illegal status transition"):
        run(db_session, "UPDATE stock_reservations SET status = 'active', closed_at = NULL WHERE id = :r", r=rid)
    with violates(db_session, pge.RestrictViolation, message="cannot be deleted"):
        run(db_session, "DELETE FROM stock_reservations WHERE id = :r", r=rid)


def test_a_reservation_cannot_be_born_closed(db_session, x):
    _, oline = _approved_line(db_session, x, "50")
    with violates(db_session, pge.CheckViolation, message="illegal status transition"):
        _reserve(db_session, x, oline, "50", status="released")


# ---------------------------------------------------------------- isolation and concurrency (honest limits)
def test_the_stock_trigger_refuses_to_run_above_read_committed(engine, db_session, x):
    """Under REPEATABLE READ the check would read a stale snapshot, so the trigger fails closed instead."""
    conn = engine.connect().execution_options(isolation_level="REPEATABLE READ")
    try:
        conn.execute(text("SELECT 1"))
        adj = conn.execute(text("INSERT INTO stock_adjustments (adjustment_no, kind, item_id, item_kind, qty_delta_base, unit_cost, reason, adjustment_date, created_by) "
                                "VALUES ('RR1', 'opening', :i, 'raw', 5, 42, 'r', :d, :u) RETURNING id"), dict(i=x["flour"], d=D0, u=x["u"])).scalar_one()
        conn.execute(text("INSERT INTO stock_movements (posting_key, item_id, item_kind, movement_date, movement_type, qty_base, unit_cost, value, created_by, adjustment_id) "
                          "VALUES ('rr', :i, 'raw', :d, 'opening', 5, 42, 210, :u, :a)"), dict(i=x["flour"], d=D0, u=x["u"], a=adj))
        with pytest.raises(Exception) as caught:
            conn.commit()
        assert "READ COMMITTED" in str(caught.value)
    finally:
        conn.close()


def _classify(exc, expected_messages):
    """'rejected' only for the inventory rule the test is about (SQLSTATE 23514 with its message); anything else stays an exception."""
    original = getattr(exc, "orig", exc)
    if isinstance(original, pge.CheckViolation) and any(m in str(original) for m in expected_messages):
        return "rejected"
    return exc


def _overlapping_commits(engine, first, second, expected_messages):
    """Two real connections whose commit-time checks MUST overlap.

    `first` runs and its commit-time check is forced early (SET CONSTRAINTS ALL IMMEDIATE), so it holds the item row lock while its
    transaction is still open. `second` then does its work and commits; it can only get past its own check by waiting for that lock.
    The test fails unless `second` is seen waiting on a lock (so the lock is what serialises, not luck), and then `first` commits.

    Returns (outcomes, second_was_blocked); an outcome is 'ok', 'rejected' (the expected inventory rule) or the unexpected exception.
    """
    outcome = {}
    state = {"pid": None, "first_pid": None, "blocked": False}
    pid_ready = threading.Event()
    lock_held = threading.Event()

    def first_worker():
        conn = engine.connect()
        try:
            conn.execute(text("SET lock_timeout = '20s'"))
            state["first_pid"] = conn.execute(text("SELECT pg_backend_pid()")).scalar()
            first(conn)
            conn.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))   # runs the invariant check now and takes the item lock
            lock_held.set()
            if not pid_ready.wait(timeout=20):
                raise TimeoutError("second connection never started")
            with engine.connect() as probe:
                for _ in range(150):                                  # up to 15 s
                    # the second backend must be blocked BY the first one (not by some other lock)
                    if probe.execute(text("SELECT :f = ANY(pg_blocking_pids(:s))"), dict(f=state["first_pid"], s=state["pid"])).scalar():
                        state["blocked"] = True
                        break
                    time.sleep(0.1)
            conn.commit()
            outcome["first"] = "ok"
        except Exception as exc:  # noqa: BLE001 - classified, never swallowed
            conn.rollback()
            outcome["first"] = _classify(exc, expected_messages)
        finally:
            lock_held.set()
            conn.close()

    def second_worker():
        lock_held.wait(timeout=20)          # the first transaction demonstrably holds the lock before the second starts
        conn = engine.connect()
        try:
            conn.execute(text("SET lock_timeout = '20s'"))
            state["pid"] = conn.execute(text("SELECT pg_backend_pid()")).scalar()
            pid_ready.set()
            second(conn)
            conn.commit()
            outcome["second"] = "ok"
        except Exception as exc:  # noqa: BLE001
            conn.rollback()
            outcome["second"] = _classify(exc, expected_messages)
        finally:
            pid_ready.set()
            conn.close()

    threads = [threading.Thread(target=first_worker), threading.Thread(target=second_worker)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=90)
    assert not any(t.is_alive() for t in threads), "a race worker is still running (a lock was never released)"
    return outcome, state["blocked"]


def test_two_uncoordinated_reservations_cannot_both_commit(engine, db_session, x):
    """Raw SQL, no service, no lock taken by the writers: the commit-time trigger serialises the check on the item row.

    This is defence in depth. It does not replace the StockService locking protocol (see the design doc, section 6).
    """
    _open(db_session, x, x["sku"], "88")
    _, a = _approved_line(db_session, x, "60")
    _, b = _approved_line(db_session, x, "60", customer="B")
    db_session.commit()

    def reserve(line):
        def work(conn):
            conn.execute(text("INSERT INTO stock_reservations (order_line_id, item_id, qty_base, created_by) VALUES (:o, :i, 60, :u)"),
                         dict(o=line, i=x["sku"], u=x["ada"]))
        return work

    outcome, blocked = _overlapping_commits(engine, reserve(a), reserve(b), ["active reservations exceed on-hand stock"])
    assert blocked, f"the second commit never waited on the item lock, so nothing proves the lock serialises the checks: {outcome}"
    assert outcome == {"first": "ok", "second": "rejected"}, outcome
    # committed state: one reservation, within on-hand
    assert scalar(db_session, "SELECT count(*) FROM stock_reservations") == 1
    assert scalar(db_session, "SELECT order_line_id FROM stock_reservations") == a     # the transaction that held the lock first won
    assert scalar(db_session, "SELECT coalesce(sum(qty_base), 0) FROM stock_reservations WHERE status = 'active'") == D("60.000")
    assert scalar(db_session, "SELECT coalesce(sum(qty_base), 0) FROM stock_movements WHERE item_id = :i", i=x["sku"]) == D("88.000")


def test_two_uncoordinated_issues_cannot_both_commit(engine, db_session, x):
    _open(db_session, x, x["flour"], "100", "42")
    first, second = _started(db_session, x, x), _started(db_session, x, x)
    db_session.commit()

    def issue(run_id, key):
        def work(conn):
            conn.execute(text("INSERT INTO stock_movements (posting_key, item_id, item_kind, movement_date, movement_type, qty_base, unit_cost, value, created_by, production_run_id) "
                              "VALUES (:k, :i, 'raw', :d, 'production_issue', -60, 42, -2520, :u, :r)"), dict(k=key, i=x["flour"], d=D0, u=x["u"], r=run_id))
        return work

    outcome, blocked = _overlapping_commits(engine, issue(first, "r1"), issue(second, "r2"), ["negative stock for item"])
    assert blocked, f"the second commit never waited on the item lock, so nothing proves the lock serialises the checks: {outcome}"
    assert outcome == {"first": "ok", "second": "rejected"}, outcome
    # committed state: exactly one issue posted, balance and value consistent, no partial row from the loser
    assert _balance(db_session, x["flour"]) == (D("40.000"), D("1680.00"))
    assert scalar(db_session, "SELECT array_agg(posting_key) FROM stock_movements WHERE posting_key IN ('r1', 'r2')") == ["r1"]


def test_an_unexpected_error_is_not_reported_as_a_rejection():
    """The classifier is strict: a deadlock or any other database error must fail the race tests, not satisfy them."""
    assert _classify(RuntimeError("boom"), ["negative stock"]) != "rejected"
    assert _classify(pge.DeadlockDetected("negative stock"), ["negative stock"]) != "rejected"
    assert _classify(pge.CheckViolation("some other check"), ["negative stock"]) != "rejected"
    assert _classify(pge.CheckViolation("negative stock for item 1"), ["negative stock"]) == "rejected"


# ---------------------------------------------------------------- indexes for balances, transactions and audit reports
@pytest.mark.parametrize("index", [
    "ix_stock_movements_item_cover", "ix_stock_movements_item_date", "ix_stock_movements_date", "ix_stock_movements_run",
    "ix_stock_movements_invoice_line", "ix_stock_movements_purchase_line", "ix_stock_reservations_active_item", "ix_purchases_supplier",
    "ix_purchases_date", "ix_purchase_lines_item", "ix_stock_adjustments_item_date",
])
def test_report_indexes_exist(db_session, index):
    assert scalar(db_session, "SELECT count(*) FROM pg_indexes WHERE schemaname = 'public' AND indexname = :i", i=index) == 1


def test_the_covering_index_serves_balance_sums_without_the_table(db_session, x):
    definition = scalar(db_session, "SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_stock_movements_item_cover'")
    assert "(item_id) INCLUDE (qty_base, value)" in definition


# ================================================================ the mandatory benchmark, carried by the ledger
def test_the_benchmark_flows_through_the_ledger(db_session, x):
    """Opening stock, the flour purchase, one run (188 good packets) and the 150-packet dispatch, valued by the single WAC function."""
    s = db_session
    _open(s, x, x["flour"], "100", "42")
    _open(s, x, x["oil"], "30", "140")
    _open(s, x, x["spice"], "5", "200")
    _open(s, x, x["pouch"], "500", "2.50")
    _open(s, x, x["carton"], "30", "15")
    _open(s, x, x["sku"], "50")
    _purchase(s, x, lines=(("flour", "60", "45"),))
    s.commit()
    run_id = _started(s, x, x)
    s.commit()
    issued = {}
    for name, qty in (("flour", "40"), ("oil", "10.5"), ("spice", "1.2"), ("pouch", "194"), ("carton", "9")):
        issued[name] = _issue_value(s, x[name], qty)
        _mv(s, x, f"run:{run_id}:issue:{name}", x[name], "production_issue", f"-{qty}", value=-issued[name], production_run_id=run_id)
    _complete(s, x, run_id, "188")
    _mv(s, x, f"run:{run_id}:output", x["sku"], "production_output", "188", production_run_id=run_id)
    s.commit()

    assert issued["flour"] == D("1725.00"), "WAC 43.125 per kg"
    assert {k: _balance(s, x[k])[0] for k in ("flour", "oil", "spice", "pouch", "carton", "sku")} == {
        "flour": D("120.000"), "oil": D("19.500"), "spice": D("3.800"), "pouch": D("306.000"), "carton": D("21.000"), "sku": D("238.000")}
    assert sum(issued.values()) == D("4055.00"), "materials cost of the run"
    assert sum(_balance(s, x[k])[1] for k in ("flour", "oil", "spice", "pouch", "carton")) == D("9745.00"), "raw and packaging stock value after the run"
    assert scalar(s, "SELECT -sum(value) FROM stock_movements WHERE production_run_id = :r AND movement_type = 'production_issue'", r=run_id) == D("4055.00")

    _, oline, invoice = _benchmark_invoice(s, x, "A")
    _reserve(s, x, oline, "150")
    s.commit()
    _dispatch(s, x, _invoice_line_of(s, invoice))
    run(s, "UPDATE stock_reservations SET status = 'fulfilled', closed_at = now() WHERE order_line_id = :o", o=oline)
    s.commit()
    assert _balance(s, x["sku"])[0] == D("88.000")
    assert scalar(s, "SELECT coalesce(sum(qty_base), 0) FROM stock_reservations WHERE status = 'active'") == 0


def test_distributor_b_shortage_is_visible_from_the_ledger(db_session, x):
    """88 on hand, 120 ordered: the shortage of 32 is computed, not stored (the reservation would not fit)."""
    _open(db_session, x, x["sku"], "88")
    _, oline = _approved_line(db_session, x, "120", customer="B")
    db_session.commit()
    on_hand = _balance(db_session, x["sku"])[0]
    assert D("120") - on_hand == D("32.000")
    _reserve(db_session, x, oline, "120")
    with violates(db_session, pge.CheckViolation, message="reservations exceed"):
        db_session.commit()


# ---- review findings fixed test-first (independent review of migration 0005) ----

def test_a_reversal_cannot_point_forward_to_a_row_in_the_same_statement(db_session, x):
    """Review C1: the mirror check used to be skipped when the original was not yet visible, so a bogus 'reversal' of a later row passed."""
    pid, (line,) = _purchase(db_session, x, receipts=False)
    run(db_session, "UPDATE purchases SET status = 'voided', voided_at = now(), voided_by = :u, void_reason = 'x' WHERE id = :p", u=x["u"], p=pid)
    base = scalar(db_session, "SELECT coalesce(max(id), 0) FROM stock_movements") + 1
    with violates(db_session, pge.CheckViolation):
        run(db_session,
            "INSERT INTO stock_movements (id, posting_key, item_id, item_kind, movement_date, movement_type, qty_base, unit_cost, value, purchase_line_id, "
            "reverses_movement_id, created_by) OVERRIDING SYSTEM VALUE "
            "SELECT :a, 'fwd-rev', i.id, i.kind, :d, 'purchase_receipt', 1000, 1000, 1000000, :l, :b, :u FROM items i WHERE i.id = :i "
            "UNION ALL "
            "SELECT :b, 'fwd-orig', i.id, i.kind, :d, 'purchase_receipt', 100, 42, 4200, :l, NULL, :u FROM items i WHERE i.id = :i",
            a=base, b=base + 1, d=D0, l=line, u=x["u"], i=x["flour"])
        db_session.commit()


def test_a_forged_created_at_cannot_backdate_an_issue_to_a_completed_run(db_session, x):
    """Review I3: created_at is stamped by the database; a client-supplied value is ignored."""
    _open(db_session, x, x["flour"], "100", "42")
    run_id = _started(db_session, x, x)
    db_session.commit()
    _complete(db_session, x, run_id, "188")
    db_session.commit()
    run(db_session,
        "INSERT INTO stock_movements (posting_key, item_id, item_kind, movement_date, movement_type, qty_base, unit_cost, value, production_run_id, created_by, created_at) "
        "SELECT 'late', i.id, i.kind, :d, 'production_issue', -1, 42, -42.00, :r, :u, timestamp '2000-01-01' FROM items i WHERE i.id = :i",
        d=D0, r=run_id, u=x["u"], i=x["flour"])
    with violates(db_session, pge.CheckViolation, message="already completed"):
        db_session.commit()


# ---- lifecycle metadata is history: once voided or closed it cannot be rewritten
@pytest.mark.parametrize("assignment", ["void_reason = 'rewritten'", "voided_at = voided_at + interval '1 day'", "voided_by = created_by + 0"])
def test_a_voided_purchase_keeps_its_void_metadata(db_session, x, assignment):
    pid, _ = _purchase(db_session, x)
    other = scalar(db_session, "SELECT id FROM users WHERE id <> :u LIMIT 1", u=x["u"]) if assignment.startswith("voided_by") else None
    db_session.commit()
    _void(db_session, x, pid)
    db_session.commit()
    if other is not None:
        assignment = f"voided_by = {other}"
    with violates(db_session, pge.RestrictViolation, message="voided purchase"):
        run(db_session, f"UPDATE purchases SET {assignment} WHERE id = :p", p=pid)


def test_voiding_still_works_and_an_unchanged_voided_row_may_be_touched(db_session, x):
    pid, _ = _purchase(db_session, x)
    db_session.commit()
    _void(db_session, x, pid)
    db_session.commit()
    run(db_session, "UPDATE purchases SET void_reason = void_reason WHERE id = :p", p=pid)   # no change, no error
    db_session.commit()
    assert scalar(db_session, "SELECT status FROM purchases WHERE id = :p", p=pid) == "voided"


@pytest.mark.parametrize("final", ["fulfilled", "released"])
def test_a_closed_reservation_keeps_its_closing_time(db_session, x, final):
    _open(db_session, x, x["sku"], "238")
    _, oline = _approved_line(db_session, x, "50")
    rid = _reserve(db_session, x, oline, "50")
    db_session.commit()
    run(db_session, "UPDATE stock_reservations SET status = :s, closed_at = now() WHERE id = :r", s=final, r=rid)
    db_session.commit()
    with violates(db_session, pge.RestrictViolation, message="closed reservation"):
        run(db_session, "UPDATE stock_reservations SET closed_at = closed_at + interval '1 day' WHERE id = :r", r=rid)
