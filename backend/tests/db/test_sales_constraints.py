"""Constraint tests for migration 0003: orders, order lines, invoices, invoice lines, credit notes.

Most rules are checked at the statement. Three are checked at COMMIT by deferred constraint triggers
(header totals equal the sum of the lines; cancelled invoice and credit note exist together), so those
tests commit for real. Fixtures commit master data, because `violates()` rolls the session back.
"""
from datetime import date
from decimal import Decimal

import psycopg.errors as pge
import pytest
from sqlalchemy import text

from app.domain.gst import financial_year, format_invoice_no, round_grand_total, split_tax, supply_type
from tests.db.helpers import run, scalar, violates

D = Decimal
GSTIN_TN, GSTIN_KA = "33AABCR1234F1Z5", "29AABCD5678G1Z2"
FACTORY_GSTIN = "33AABCM1111A1Z1"


def fy_no(fy, seq):
    """Independent spelling of the invoice-number pattern (the domain function is compared in a parity test)."""
    return f"INV/{fy[2:4]}-{fy[5:7]}/{seq:04d}"


# ---------------------------------------------------------------- builders
@pytest.fixture
def no_order_link(db_session):
    """Status-machine tests walk orders through invoiced/dispatched without invoices; switch the commit-time link rule off for them."""
    run(db_session, "ALTER TABLE orders DISABLE TRIGGER trg_order_invoice_link")
    db_session.commit()
    yield
    db_session.rollback()
    run(db_session, "ALTER TABLE orders ENABLE TRIGGER trg_order_invoice_link")
    db_session.commit()

@pytest.fixture
def m(db_session):
    s = db_session
    run(s, "INSERT INTO users (username, full_name, password_hash, role) VALUES ('sam', 'Sam', 'x', 'salesman'), ('ada', 'Ada', 'x', 'admin')")
    run(s, "INSERT INTO products (name, hsn_code, gst_rate) VALUES ('Murukku', '1905', 5)")
    product = scalar(s, "SELECT id FROM products")
    run(s, "INSERT INTO customers (name, customer_type, gstin, state_code, address) VALUES "
           "('Retailer A', 'retailer', :a, '33', 'Chennai'), ('Distributor B', 'distributor', :b, '29', 'Bengaluru')", a=GSTIN_TN, b=GSTIN_KA)

    def item(code, kind, unit, **extra):
        run(s, "INSERT INTO items (code, name, kind, base_unit, product_id, pack_size_g, packets_per_carton, list_price) "
               "VALUES (:c, :c, :k, CAST(:u AS unit_code), :p, :g, :pc, :lp)",
            c=code, k=kind, u=unit, p=extra.get("product"), g=extra.get("pack"), pc=extra.get("per"), lp=extra.get("price"))
        return scalar(s, "SELECT id FROM items WHERE code = :c", c=code)

    ids = dict(
        sam=scalar(s, "SELECT id FROM users WHERE username='sam'"), ada=scalar(s, "SELECT id FROM users WHERE username='ada'"),
        A=scalar(s, "SELECT id FROM customers WHERE name='Retailer A'"), B=scalar(s, "SELECT id FROM customers WHERE name='Distributor B'"),
        flour=item("FLOUR", "raw", "kg"),
        sku=item("MUR-200", "finished", "pkt", product=product, pack=200, per=20, price="50.00"),
        sku2=item("MUR-500", "finished", "pkt", product=product, pack=500, per=10, price="120.00"),
    )
    s.commit()
    return ids


_seq = iter(range(1, 10_000))


def _order(s, m, customer="A", status=None, **extra):
    n = next(_seq)
    cols = dict(order_no=f"ORD/{n:04d}", client_uuid=f"00000000-0000-0000-0000-{n:012d}", customer_id=m[customer],
                salesman_id=m["sam"], order_date=date(2026, 11, 20))
    cols.update(extra)
    if status is not None:
        cols["status"] = status
    names = ", ".join(cols)
    marks = ", ".join(f"CAST(:{k} AS uuid)" if k == "client_uuid" else f":{k}" for k in cols)
    run(s, f"INSERT INTO orders ({names}) VALUES ({marks})", **cols)
    return scalar(s, "SELECT id FROM orders WHERE order_no = :n", n=cols["order_no"])


def _oline(s, order_id, item_id, qty="150", price="50.00"):
    run(s, "INSERT INTO order_lines (order_id, item_id, qty, unit_price) VALUES (:o, :i, :q, :p)", o=order_id, i=item_id, q=qty, p=price)
    return scalar(s, "SELECT id FROM order_lines WHERE order_id = :o AND item_id = :i", o=order_id, i=item_id)


def _approve(s, m, order_id):
    run(s, "UPDATE orders SET status = 'approved', decided_by = :u, decided_at = now() WHERE id = :o", u=m["ada"], o=order_id)


def _header(s, m, order_id, customer="A", link=True, **over):
    if link and run(s, "SELECT status FROM orders WHERE id = :o", o=order_id).scalar() == "submitted":
        _approve(s, m, order_id)
    inter = customer == "B"
    v = dict(
        invoice_no="INV/26-27/0001", financial_year="2026-27", invoice_seq=1, order_id=order_id, customer_id=m[customer],
        invoice_date=date(2026, 11, 20), seller_gstin=FACTORY_GSTIN, seller_state_code="33",
        customer_name="Distributor B" if inter else "Retailer A", customer_gstin=GSTIN_KA if inter else GSTIN_TN,
        customer_address="Bengaluru" if inter else "Chennai", place_of_supply_state="29" if inter else "33",
        supply_type="inter" if inter else "intra",
        taxable_total="6000.00" if inter else "7500.00", cgst_total="0" if inter else "187.50", sgst_total="0" if inter else "187.50",
        igst_total="300.00" if inter else "0", round_off="0", grand_total="6300.00" if inter else "7875.00", created_by=m["ada"],
    )
    v.update(over)
    names = ", ".join(v)
    run(s, f"INSERT INTO invoices ({names}) VALUES ({', '.join(':' + k for k in v)})", **v)
    if link and run(s, "SELECT status FROM orders WHERE id = :o", o=order_id).scalar() == "approved":
        run(s, "UPDATE orders SET status = 'invoiced' WHERE id = :o", o=order_id)
    return scalar(s, "SELECT id FROM invoices WHERE order_id = :o", o=v["order_id"])


def _iline(s, invoice_id, order_id, oline_id, item_id, customer="A", **over):
    inter = customer == "B"
    v = dict(
        invoice_id=invoice_id, order_id=order_id, order_line_id=oline_id, item_id=item_id, description="Murukku 200 g", hsn_code="1905",
        qty="120" if inter else "150", unit_price="50.00", taxable_value="6000.00" if inter else "7500.00", gst_rate="5",
        cgst_amount="0" if inter else "187.50", sgst_amount="0" if inter else "187.50", igst_amount="300.00" if inter else "0",
        supply_type="inter" if inter else "intra",
    )
    v.update(over)
    run(s, f"INSERT INTO invoice_lines ({', '.join(v)}) VALUES ({', '.join(':' + k for k in v)})", **v)


def _benchmark_invoice(s, m, customer="A", seq=1):
    """Order (approved) + header + one line, all uncommitted. Returns (order, order_line, invoice)."""
    qty, price = ("120", "50.00") if customer == "B" else ("150", "50.00")
    order = _order(s, m, customer)
    oline = _oline(s, order, m["sku"], qty, price)
    _approve(s, m, order)
    invoice = _header(s, m, order, customer, invoice_no=fy_no("2026-27", seq), invoice_seq=seq)
    _iline(s, invoice, order, oline, m["sku"], customer)
    return order, oline, invoice


def _cancel_with_credit_note(s, m, invoice, **over):
    h = run(s, "SELECT taxable_total, cgst_total, sgst_total, igst_total, round_off, grand_total FROM invoices WHERE id = :i", i=invoice).one()
    v = dict(credit_note_no="CN/26-27/0001", invoice_id=invoice, issue_date=date(2026, 11, 21), reason="Order cancelled by customer",
             taxable_total=h.taxable_total, cgst_total=h.cgst_total, sgst_total=h.sgst_total, igst_total=h.igst_total,
             round_off=h.round_off, grand_total=h.grand_total, restock=False, created_by=m["ada"])
    v.update(over)
    run(s, f"INSERT INTO credit_notes ({', '.join(v)}) VALUES ({', '.join(':' + k for k in v)})", **v)
    run(s, "UPDATE invoices SET status = 'cancelled' WHERE id = :i", i=invoice)


# ================================================================ orders
def test_order_line_rejects_raw_item(db_session, m):
    order = _order(db_session, m)
    db_session.commit()
    with violates(db_session, pge.ForeignKeyViolation, "fk_order_lines_item_kind"):
        _oline(db_session, order, m["flour"])


def test_order_line_item_kind_witness_cannot_be_changed(db_session, m):
    order = _order(db_session, m)
    db_session.commit()
    with violates(db_session, pge.CheckViolation, "ck_order_lines_item_kind"):
        run(db_session, "INSERT INTO order_lines (order_id, item_id, item_kind, qty, unit_price) VALUES (:o, :i, 'raw', 1, 1)", o=order, i=m["flour"])


@pytest.mark.parametrize("qty, constraint", [("0", "ck_order_lines_qty_positive"), ("-5", "ck_order_lines_qty_positive"),
                                              ("1.5", "ck_order_lines_whole_packets")])
def test_order_quantity_is_a_positive_whole_number_of_packets(db_session, m, qty, constraint):
    order = _order(db_session, m)
    db_session.commit()
    with violates(db_session, pge.CheckViolation, constraint):
        _oline(db_session, order, m["sku"], qty)


def test_order_unit_price_cannot_be_negative(db_session, m):
    order = _order(db_session, m)
    db_session.commit()
    with violates(db_session, pge.CheckViolation, "ck_order_lines_unit_price_nonnegative"):
        _oline(db_session, order, m["sku"], "1", "-0.01")


def test_a_free_sample_line_is_allowed(db_session, m):
    _oline(db_session, _order(db_session, m), m["sku"], "10", "0.00")


def test_an_item_appears_once_per_order(db_session, m):
    order = _order(db_session, m)
    _oline(db_session, order, m["sku"])
    db_session.commit()
    with violates(db_session, pge.UniqueViolation, "order_lines_order_id_item_id_key"):
        _oline(db_session, order, m["sku"], "5")


def test_duplicate_client_uuid_rejected(db_session, m):
    _order(db_session, m, client_uuid="11111111-1111-1111-1111-111111111111")
    db_session.commit()
    with violates(db_session, pge.UniqueViolation, "orders_client_uuid_key"):
        _order(db_session, m, client_uuid="11111111-1111-1111-1111-111111111111")


def test_duplicate_order_number_rejected(db_session, m):
    _order(db_session, m, order_no="ORD/DUP")
    db_session.commit()
    with violates(db_session, pge.UniqueViolation, "orders_order_no_key"):
        _order(db_session, m, order_no="ORD/DUP")


def test_order_customer_must_exist(db_session, m):
    with violates(db_session, pge.ForeignKeyViolation):
        _order(db_session, m, customer_id=999)


def test_order_status_values_are_the_documented_ones(db_session, m):
    with violates(db_session, pge.CheckViolation, message="(new) -> shipped"):
        _order(db_session, m, status="shipped")


def test_a_new_order_must_start_submitted(db_session, m):
    with violates(db_session, pge.CheckViolation, message="(new) -> approved"):
        _order(db_session, m, status="approved")


def test_order_illegal_transition_rejected(db_session, m):
    order = _order(db_session, m)
    db_session.commit()
    with violates(db_session, pge.CheckViolation, message="orders: submitted -> dispatched"):
        run(db_session, "UPDATE orders SET status = 'dispatched' WHERE id = :o", o=order)


@pytest.mark.parametrize("path, bad", [
    (["approved"], "dispatched"), (["approved", "invoiced"], "approved"), (["rejected"], "approved"),
    (["approved", "cancelled"], "invoiced"), (["on_hold", "approved", "invoiced", "dispatched"], "invoiced"),
])
def test_other_illegal_order_transitions_rejected(db_session, m, path, bad, no_order_link):
    order = _order(db_session, m)
    for status in path:
        extra = ", hold_reason = 'check stock'" if status == "on_hold" else ", decided_by = coalesce(decided_by, :u), decided_at = coalesce(decided_at, now())"
        run(db_session, f"UPDATE orders SET status = :s{extra} WHERE id = :o", s=status, o=order, u=m["ada"])
    db_session.commit()
    with violates(db_session, pge.CheckViolation, message=f"{path[-1]} -> {bad}"):
        run(db_session, "UPDATE orders SET status = :s WHERE id = :o", s=bad, o=order)


def test_the_whole_order_lifecycle_is_walkable(db_session, m, no_order_link):
    order = _order(db_session, m)
    run(db_session, "UPDATE orders SET status = 'on_hold', hold_reason = 'shortage' WHERE id = :o", o=order)
    for status in ("approved", "invoiced", "dispatched", "cancelled"):
        run(db_session, "UPDATE orders SET status = :s, decided_by = coalesce(decided_by, :u), decided_at = coalesce(decided_at, now()) WHERE id = :o", s=status, u=m["ada"], o=order)
    assert scalar(db_session, "SELECT status FROM orders") == "cancelled"


def test_on_hold_needs_a_reason(db_session, m):
    order = _order(db_session, m)
    db_session.commit()
    with violates(db_session, pge.CheckViolation, "ck_orders_hold_reason"):
        run(db_session, "UPDATE orders SET status = 'on_hold' WHERE id = :o", o=order)


def test_decision_fields_come_as_a_pair(db_session, m):
    with violates(db_session, pge.CheckViolation, "ck_orders_decided_pair"):
        _order(db_session, m, decided_by=m["ada"])


def test_an_order_cannot_pass_approval_without_a_decider(db_session, m):
    order = _order(db_session, m)
    db_session.commit()
    with violates(db_session, pge.CheckViolation, "ck_orders_decided_after_hold"):
        run(db_session, "UPDATE orders SET status = 'approved' WHERE id = :o", o=order)


def test_an_order_cannot_be_deleted(db_session, m):
    order = _order(db_session, m)
    db_session.commit()
    with violates(db_session, pge.RestrictViolation, message="cannot be deleted"):
        run(db_session, "DELETE FROM orders WHERE id = :o", o=order)


@pytest.mark.parametrize("status", ["submitted", "on_hold"])
def test_order_lines_are_editable_before_approval(db_session, m, status):
    order = _order(db_session, m)
    line = _oline(db_session, order, m["sku"])
    if status == "on_hold":
        run(db_session, "UPDATE orders SET status = 'on_hold', hold_reason = 'x' WHERE id = :o", o=order)
    run(db_session, "UPDATE order_lines SET qty = 160 WHERE id = :l", l=line)
    _oline(db_session, order, m["sku2"], "10", "120.00")
    run(db_session, "DELETE FROM order_lines WHERE item_id = :i", i=m["sku2"])


@pytest.mark.parametrize("change", ["insert", "update", "delete"])
def test_order_lines_are_frozen_once_approved(db_session, m, change):
    order = _order(db_session, m)
    _oline(db_session, order, m["sku"])
    _approve(db_session, m, order)
    db_session.commit()
    with violates(db_session, pge.RestrictViolation, message="order_lines"):
        if change == "insert":
            _oline(db_session, order, m["sku2"], "1", "120.00")
        elif change == "update":
            run(db_session, "UPDATE order_lines SET qty = 1 WHERE order_id = :o", o=order)
        else:
            run(db_session, "DELETE FROM order_lines WHERE order_id = :o", o=order)


def test_a_line_cannot_be_moved_out_of_an_approved_order(db_session, m):
    approved, other = _order(db_session, m), _order(db_session, m)
    _oline(db_session, approved, m["sku"])
    _approve(db_session, m, approved)
    db_session.commit()
    with violates(db_session, pge.RestrictViolation, message="order_lines"):
        run(db_session, "UPDATE order_lines SET order_id = :t WHERE order_id = :a", t=other, a=approved)


# ================================================================ invoice header: numbering and financial year
def test_benchmark_invoice_is_accepted(db_session, m):
    _benchmark_invoice(db_session, m)
    db_session.commit()
    assert scalar(db_session, "SELECT grand_total FROM invoices") == D("7875.00")


def test_invoice_number_unique_per_fy(db_session, m):
    first = _order(db_session, m)
    second = _order(db_session, m)
    _header(db_session, m, first)
    with violates(db_session, pge.UniqueViolation):
        _header(db_session, m, second)  # same number, same financial year


def test_the_same_sequence_number_is_fine_in_another_financial_year(db_session, m):
    first, second = _order(db_session, m), _order(db_session, m)
    _header(db_session, m, first)
    _header(db_session, m, second, invoice_no=fy_no("2027-28", 1), financial_year="2027-28", invoice_date=date(2027, 4, 1))
    assert scalar(db_session, "SELECT count(*) FROM invoices") == 2


def test_a_different_sequence_number_in_the_same_year_is_fine(db_session, m):
    first, second = _order(db_session, m), _order(db_session, m)
    _header(db_session, m, first)
    _header(db_session, m, second, invoice_no=fy_no("2026-27", 2), invoice_seq=2)


def test_duplicate_sequence_with_a_different_number_text_is_still_rejected(db_session, m):
    first, second = _order(db_session, m), _order(db_session, m)
    _header(db_session, m, first)
    with violates(db_session, pge.CheckViolation, "ck_invoices_invoice_no_format"):
        _header(db_session, m, second, invoice_no="INV/26-27/9999")  # number text must be derived from (year, seq)


def test_invoice_no_longer_than_16_chars(db_session, m):
    order = _order(db_session, m)
    seq = 1_000_000  # 'INV/26-27/1000000' is 17 characters
    with violates(db_session, pge.CheckViolation, "ck_invoices_invoice_no_length"):
        _header(db_session, m, order, invoice_no=fy_no("2026-27", seq), invoice_seq=seq)


def test_the_longest_allowed_number_is_exactly_16_characters(db_session, m):
    order = _order(db_session, m)
    _header(db_session, m, order, invoice_no=fy_no("2026-27", 999_999), invoice_seq=999_999)
    assert len(scalar(db_session, "SELECT invoice_no FROM invoices")) == 16


def test_invoice_sequence_starts_at_one(db_session, m):
    order = _order(db_session, m)
    with violates(db_session, pge.CheckViolation, "ck_invoices_invoice_seq_positive"):
        _header(db_session, m, order, invoice_no=fy_no("2026-27", 0), invoice_seq=0)


@pytest.mark.parametrize("seq", [1, 9, 10, 99, 100, 999, 1000, 9999, 10000, 12345, 999999])
def test_invoice_number_text_matches_the_python_formatter(db_session, m, seq):
    order = _order(db_session, m)
    _header(db_session, m, order, invoice_no=format_invoice_no("2026-27", seq), invoice_seq=seq)


@pytest.mark.parametrize("fy, constraint", [
    ("2026/27", "ck_invoices_financial_year_format"), ("26-27", "ck_invoices_financial_year_format"),
    ("abcd-ef", "ck_invoices_financial_year_format"), ("2026-28", "ck_invoices_financial_year_consecutive"),
    ("2026-26", "ck_invoices_financial_year_consecutive"),
])
def test_financial_year_must_be_well_formed_and_consecutive(db_session, m, fy, constraint):
    order = _order(db_session, m)
    with violates(db_session, pge.CheckViolation, constraint):
        _header(db_session, m, order, financial_year=fy, invoice_no="INV/26-27/0001")


@pytest.mark.parametrize("d", [date(2026, 4, 1), date(2026, 11, 20), date(2027, 3, 31)])
def test_dates_inside_the_financial_year_are_accepted(db_session, m, d):
    _header(db_session, m, _order(db_session, m), invoice_date=d)


@pytest.mark.parametrize("d", [date(2026, 3, 31), date(2027, 4, 1)])
def test_dates_outside_the_financial_year_are_rejected(db_session, m, d):
    with violates(db_session, pge.CheckViolation, "ck_invoices_date_in_financial_year"):
        _header(db_session, m, _order(db_session, m), invoice_date=d)


@pytest.mark.parametrize("d", [date(2025, 3, 31), date(2025, 4, 1), date(2026, 3, 31), date(2026, 4, 1), date(2026, 12, 31),
                                date(2027, 1, 1), date(2027, 3, 31), date(2027, 4, 1), date(2099, 3, 31), date(2099, 4, 1)])
def test_database_financial_year_agrees_with_python(db_session, m, d):
    fy = financial_year(d)
    seq = 1
    _header(db_session, m, _order(db_session, m), financial_year=fy, invoice_no=fy_no(fy, seq), invoice_date=d)
    db_session.rollback()


# ================================================================ invoice header: supply type and tax split
def test_invoice_supply_type_must_match_state_codes(db_session, m):
    order = _order(db_session, m)
    with violates(db_session, pge.CheckViolation, "ck_invoices_supply_type_matches_states"):
        _header(db_session, m, order, place_of_supply_state="29", customer_gstin=GSTIN_KA)  # intra, but 33 -> 29
    order = _order(db_session, m)
    with violates(db_session, pge.CheckViolation, "ck_invoices_supply_type_matches_states"):
        _header(db_session, m, order, supply_type="inter", cgst_total="0", sgst_total="0", igst_total="375.00")  # inter, but 33 -> 33


@pytest.mark.parametrize("code", ["01", "07", "27", "29", "33", "37", "38", "97"])
def test_database_supply_type_agrees_with_python(db_session, m, code):
    kind = supply_type("33", code)
    split = split_tax(D("6000.00"), D("5"), kind)
    gstin = f"{code}AABCP9999Q1Z5"
    run(db_session, "INSERT INTO customers (name, customer_type, gstin, state_code) VALUES ('Probe', 'retailer', :g, :c)", g=gstin, c=code)
    probe = scalar(db_session, "SELECT id FROM customers WHERE name = 'Probe'")
    order = _order(db_session, m, customer_id=probe)
    _header(db_session, m, order, customer_id=probe, customer_name="Probe", place_of_supply_state=code, supply_type=kind, customer_gstin=gstin,
            taxable_total="6000.00", cgst_total=str(split.cgst), sgst_total=str(split.sgst), igst_total=str(split.igst),
            grand_total=str(D("6000.00") + split.total))


def test_invoice_intra_requires_cgst_equals_sgst_and_no_igst(db_session, m):
    for bad in (dict(cgst_total="187.51", sgst_total="187.49"), dict(igst_total="1.00", grand_total="7876.00")):
        order = _order(db_session, m)
        with violates(db_session, pge.CheckViolation, "ck_invoices_tax_split"):
            _header(db_session, m, order, **bad)


def test_invoice_inter_requires_igst_only(db_session, m):
    for bad in (dict(cgst_total="150.00", sgst_total="150.00", igst_total="0", grand_total="6300.00"),
                dict(cgst_total="1.00", igst_total="300.00", grand_total="6301.00")):
        order = _order(db_session, m, "B")
        with violates(db_session, pge.CheckViolation, "ck_invoices_tax_split"):
            _header(db_session, m, order, "B", **bad)


def test_distributor_b_invoice_shape_is_accepted(db_session, m):
    _header(db_session, m, _order(db_session, m, "B"), "B")
    assert scalar(db_session, "SELECT grand_total FROM invoices") == D("6300.00")


def test_invoice_total_must_equal_components(db_session, m):
    with violates(db_session, pge.CheckViolation, "ck_invoices_grand_total"):
        _header(db_session, m, _order(db_session, m), grand_total="7875.01")


def test_invoice_amounts_cannot_be_negative(db_session, m):
    with violates(db_session, pge.CheckViolation, "ck_invoices_amounts_nonnegative"):
        _header(db_session, m, _order(db_session, m), taxable_total="-7500.00", cgst_total="0", sgst_total="0", grand_total="-7500.00")


@pytest.mark.parametrize("taxable, rate", [("1234.56", "5"), ("0.10", "5"), ("999.99", "12"), ("7500.00", "5"), ("0.00", "40")])
@pytest.mark.parametrize("round_to_rupee", [False, True])
def test_database_totals_agree_with_python_rounding(db_session, m, taxable, rate, round_to_rupee):
    split = split_tax(D(taxable), D(rate), "intra")
    total, round_off = round_grand_total(D(taxable) + split.total, round_to_rupee=round_to_rupee)
    _header(db_session, m, _order(db_session, m), taxable_total=taxable, cgst_total=str(split.cgst), sgst_total=str(split.sgst),
            igst_total="0", round_off=str(round_off), grand_total=str(total))


def test_a_round_off_needs_a_whole_rupee_total(db_session, m):
    with violates(db_session, pge.CheckViolation, "ck_invoices_round_off"):
        _header(db_session, m, _order(db_session, m), round_off="0.50", grand_total="7875.50")  # 7875.50 is not whole


def test_a_round_off_is_at_most_fifty_paise(db_session, m):
    with violates(db_session, pge.CheckViolation, "ck_invoices_round_off"):
        _header(db_session, m, _order(db_session, m), taxable_total="7500.00", round_off="1.00", grand_total="7876.00")


def test_paise_are_kept_by_default(db_session, m):
    split = split_tax(D("1234.56"), D("5"), "intra")
    _header(db_session, m, _order(db_session, m), taxable_total="1234.56", cgst_total=str(split.cgst), sgst_total=str(split.sgst),
            grand_total=str(D("1234.56") + split.total))
    assert scalar(db_session, "SELECT round_off FROM invoices") == D("0.00")
    assert scalar(db_session, "SELECT grand_total FROM invoices") == D("1296.28")


# ================================================================ invoice header: customer, snapshots, one per order
def test_invoice_customer_must_match_order_customer(db_session, m):
    order = _order(db_session, m, "A")
    db_session.commit()
    with violates(db_session, pge.ForeignKeyViolation, "fk_invoices_order_customer"):
        _header(db_session, m, order, customer="A", customer_id=m["B"])


def test_one_invoice_per_order(db_session, m):
    order = _order(db_session, m)
    _header(db_session, m, order)
    with violates(db_session, pge.UniqueViolation, "invoices_order_id_key"):
        _header(db_session, m, order, invoice_no=fy_no("2026-27", 2), invoice_seq=2)


def test_invoice_order_must_exist(db_session, m):
    with violates(db_session, pge.ForeignKeyViolation):
        _header(db_session, m, 999)


def test_invoice_snapshots_seller_and_customer_details(db_session, m):
    _header(db_session, m, _order(db_session, m))
    row = run(db_session, "SELECT seller_gstin, seller_state_code, customer_name, customer_gstin, customer_address, place_of_supply_state FROM invoices").one()
    assert tuple(row) == (FACTORY_GSTIN, "33", "Retailer A", GSTIN_TN, "Chennai", "33")


def test_an_unregistered_customer_can_be_invoiced(db_session, m):
    run(db_session, "INSERT INTO customers (name, customer_type, state_code) VALUES ('Walk-in', 'retailer', '33')")
    walk = scalar(db_session, "SELECT id FROM customers WHERE name = 'Walk-in'")
    order = _order(db_session, m, customer_id=walk)
    _header(db_session, m, order, customer_id=walk, customer_name="Walk-in", customer_gstin=None, customer_address=None)


@pytest.mark.parametrize("over, constraint", [
    (dict(seller_gstin="33AABCM1111A1X1"), "ck_invoices_seller_gstin_format"),
    (dict(seller_gstin="29AABCM1111A1Z1"), "ck_invoices_seller_gstin_state"),
    (dict(customer_gstin="33aabcr1234f1z5"), "ck_invoices_customer_gstin_format"),
    (dict(customer_gstin=GSTIN_KA), "ck_invoices_customer_gstin_state"),
    (dict(place_of_supply_state="99", supply_type="inter", cgst_total="0", sgst_total="0", igst_total="375.00", customer_gstin=None,
          grand_total="7875.00"), "ck_invoices_place_of_supply_state_code"),
    (dict(seller_state_code="99", seller_gstin="99AABCM1111A1Z1", supply_type="inter", cgst_total="0", sgst_total="0", igst_total="375.00",
          grand_total="7875.00"), "ck_invoices_seller_state_code"),
    (dict(customer_name="  "), "ck_invoices_customer_name_not_blank"),
])
def test_snapshot_fields_are_validated(db_session, m, over, constraint):
    with violates(db_session, pge.CheckViolation, constraint):
        _header(db_session, m, _order(db_session, m), **over)


def test_dispatch_fields_come_as_a_pair(db_session, m):
    with violates(db_session, pge.CheckViolation, "ck_invoices_dispatch_pair"):
        _header(db_session, m, _order(db_session, m), dispatched_by=m["ada"])


def test_invoice_status_is_issued_or_cancelled(db_session, m):
    with violates(db_session, pge.CheckViolation, message="(new) -> void"):
        _header(db_session, m, _order(db_session, m), status="void")


def test_an_invoice_must_be_born_issued(db_session, m):
    with violates(db_session, pge.CheckViolation, message="(new) -> cancelled"):
        _header(db_session, m, _order(db_session, m), status="cancelled")


# ================================================================ issued invoices are immutable
@pytest.mark.parametrize("assignment", [
    "grand_total = 1", "taxable_total = 1", "cgst_total = 0", "invoice_no = 'INV/26-27/0002'", "customer_name = 'Someone Else'",
    "customer_gstin = NULL", "seller_gstin = '29AABCM1111A1Z1'", "invoice_date = '2026-11-21'", "customer_address = 'elsewhere'",
    "place_of_supply_state = '29'", "round_off = 0.50", "order_id = 999", "financial_year = '2027-28'",
])
def test_posted_invoice_amounts_immutable(db_session, m, assignment):
    _benchmark_invoice(db_session, m)
    db_session.commit()
    with violates(db_session, pge.RestrictViolation, message="invoices"):
        run(db_session, f"UPDATE invoices SET {assignment}")


def test_an_invoice_may_record_dispatch(db_session, m):
    _benchmark_invoice(db_session, m)
    db_session.commit()
    run(db_session, "UPDATE invoices SET dispatched_at = now(), dispatched_by = :u", u=m["ada"])
    db_session.commit()
    assert scalar(db_session, "SELECT dispatched_by FROM invoices") == m["ada"]


def test_invoice_cannot_be_deleted(db_session, m):
    _benchmark_invoice(db_session, m)
    db_session.commit()
    with violates(db_session, pge.RestrictViolation, message="cannot be deleted"):
        run(db_session, "DELETE FROM invoices")


def test_issued_to_cancelled_allowed_and_back_rejected(db_session, m):
    _, _, invoice = _benchmark_invoice(db_session, m)
    db_session.commit()
    _cancel_with_credit_note(db_session, m, invoice)
    db_session.commit()
    assert scalar(db_session, "SELECT status FROM invoices") == "cancelled"
    with violates(db_session, pge.CheckViolation, message="cancelled -> issued"):
        run(db_session, "UPDATE invoices SET status = 'issued'")


# ================================================================ invoice lines
def test_invoice_line_tax_math_exact(db_session, m):
    order = _order(db_session, m)
    oline = _oline(db_session, order, m["sku"])
    _approve(db_session, m, order)
    invoice = _header(db_session, m, order)
    with violates(db_session, pge.CheckViolation, "ck_invoice_lines_tax_math"):
        _iline(db_session, invoice, order, oline, m["sku"], cgst_amount="187.00", sgst_amount="187.00")


def test_invoice_line_accepts_the_benchmark_amounts(db_session, m):
    _benchmark_invoice(db_session, m)
    assert run(db_session, "SELECT taxable_value, cgst_amount, sgst_amount, igst_amount FROM invoice_lines").one() == (
        D("7500.00"), D("187.50"), D("187.50"), D("0.00"))


def test_inter_state_line_has_igst_only(db_session, m):
    _benchmark_invoice(db_session, m, "B")
    assert scalar(db_session, "SELECT igst_amount FROM invoice_lines") == D("300.00")


@pytest.mark.parametrize("over", [
    dict(cgst_amount="187.50", sgst_amount="187.50", igst_amount="375.00"),   # intra with an IGST amount too
    dict(cgst_amount="187.51", sgst_amount="187.49"),                          # halves must be equal
    dict(cgst_amount="0", sgst_amount="0", igst_amount="375.00"),              # intra line carrying the inter split
    dict(taxable_value="7500.01"),                                             # taxable is qty x unit price
])
def test_intra_line_arithmetic_is_enforced(db_session, m, over):
    order = _order(db_session, m)
    oline = _oline(db_session, order, m["sku"])
    _approve(db_session, m, order)
    invoice = _header(db_session, m, order)
    with violates(db_session, pge.CheckViolation):
        _iline(db_session, invoice, order, oline, m["sku"], **over)


def test_a_line_cannot_use_a_different_supply_type_from_its_invoice(db_session, m):
    order = _order(db_session, m)
    oline = _oline(db_session, order, m["sku"])
    _approve(db_session, m, order)
    invoice = _header(db_session, m, order)  # intra
    with violates(db_session, pge.ForeignKeyViolation, "fk_invoice_lines_invoice"):
        _iline(db_session, invoice, order, oline, m["sku"], supply_type="inter", cgst_amount="0", sgst_amount="0", igst_amount="375.00")


@pytest.mark.parametrize("rate, ok", [("0", True), ("5", True), ("12.5", True), ("40", True), ("40.01", False), ("41", False), ("-1", False)])
def test_line_gst_rate_range_is_zero_to_forty(db_session, m, rate, ok):
    order = _order(db_session, m)
    oline = _oline(db_session, order, m["sku"])
    _approve(db_session, m, order)
    invoice = _header(db_session, m, order, taxable_total="7500.00")
    half = (D("7500.00") * D(rate) / 200).quantize(D("0.01"), rounding="ROUND_HALF_UP")
    over = dict(gst_rate=rate, cgst_amount=str(half), sgst_amount=str(half))
    if ok:
        _iline(db_session, invoice, order, oline, m["sku"], **over)
    else:
        with violates(db_session, pge.CheckViolation, "ck_invoice_lines_gst_rate"):
            _iline(db_session, invoice, order, oline, m["sku"], **over)


@pytest.mark.parametrize("qty, price, rate, kind", [
    ("150", "50.00", "5", "intra"), ("120", "50.00", "5", "inter"), ("3", "0.10", "5", "intra"), ("7", "33.33", "12", "intra"),
    ("1", "0.30", "5", "intra"), ("1000", "49.99", "40", "inter"), ("13", "9.99", "0.25", "inter"), ("11", "1.50", "18", "intra"),
])
def test_line_tax_check_accepts_exactly_what_python_computes(db_session, m, qty, price, rate, kind):
    taxable = D(qty) * D(price)
    split = split_tax(taxable, D(rate), kind)
    customer = "B" if kind == "inter" else "A"
    order = _order(db_session, m, customer)
    oline = _oline(db_session, order, m["sku"], qty, price)
    _approve(db_session, m, order)
    invoice = _header(db_session, m, order, customer)
    base = dict(qty=qty, unit_price=price, taxable_value=str(taxable), gst_rate=rate, cgst_amount=str(split.cgst),
                sgst_amount=str(split.sgst), igst_amount=str(split.igst))
    _iline(db_session, invoice, order, oline, m["sku"], customer, **base)
    db_session.rollback()
    # one paisa off in the first non-zero component is rejected
    order = _order(db_session, m, customer)
    oline = _oline(db_session, order, m["sku"], qty, price)
    _approve(db_session, m, order)
    invoice = _header(db_session, m, order, customer)
    wrong = dict(base)
    key = "igst_amount" if kind == "inter" else "cgst_amount"
    wrong[key] = str(D(wrong[key]) + D("0.01"))
    with violates(db_session, pge.CheckViolation, "ck_invoice_lines_tax_math"):
        _iline(db_session, invoice, order, oline, m["sku"], customer, **wrong)


@pytest.mark.parametrize("over, constraint", [
    (dict(qty="0", taxable_value="0.00", cgst_amount="0", sgst_amount="0"), "ck_invoice_lines_qty_positive"),
    (dict(qty="1.5", taxable_value="75.00", cgst_amount="1.88", sgst_amount="1.88"), "ck_invoice_lines_whole_packets"),
    (dict(unit_price="-1", taxable_value="-150.00", cgst_amount="-3.75", sgst_amount="-3.75"), "ck_invoice_lines_unit_price_nonnegative"),
    (dict(hsn_code="19"), "ck_invoice_lines_hsn_code"),
    (dict(description=" "), "ck_invoice_lines_description_not_blank"),
])
def test_invoice_line_field_rules(db_session, m, over, constraint):
    order = _order(db_session, m)
    oline = _oline(db_session, order, m["sku"])
    _approve(db_session, m, order)
    invoice = _header(db_session, m, order)
    with violates(db_session, pge.CheckViolation, constraint):
        _iline(db_session, invoice, order, oline, m["sku"], **over)


def test_an_invoice_line_must_belong_to_the_invoices_order(db_session, m):
    order, other = _order(db_session, m), _order(db_session, m)
    _oline(db_session, order, m["sku"])
    other_line = _oline(db_session, other, m["sku"])
    _approve(db_session, m, order)
    invoice = _header(db_session, m, order)
    with violates(db_session, pge.ForeignKeyViolation, "fk_invoice_lines_order_line"):
        _iline(db_session, invoice, order, other_line, m["sku"])


def test_an_invoice_line_item_must_be_the_order_lines_item(db_session, m):
    order = _order(db_session, m)
    oline = _oline(db_session, order, m["sku"])
    _approve(db_session, m, order)
    invoice = _header(db_session, m, order)
    with violates(db_session, pge.ForeignKeyViolation, "fk_invoice_lines_order_line"):
        _iline(db_session, invoice, order, oline, m["sku2"])


def test_an_invoice_line_cannot_name_another_invoices_order(db_session, m):
    order, other = _order(db_session, m), _order(db_session, m)
    other_line = _oline(db_session, other, m["sku"])
    invoice = _header(db_session, m, order)
    with violates(db_session, pge.ForeignKeyViolation, "fk_invoice_lines_invoice"):
        _iline(db_session, invoice, other, other_line, m["sku"])


def test_an_order_line_is_invoiced_once(db_session, m):
    order, oline, invoice = _benchmark_invoice(db_session, m)
    with violates(db_session, pge.UniqueViolation, "invoice_lines_order_line_id_key"):
        _iline(db_session, invoice, order, oline, m["sku"])


@pytest.mark.parametrize("statement", ["UPDATE invoice_lines SET qty = 1", "DELETE FROM invoice_lines"])
def test_invoice_lines_are_append_only(db_session, m, statement):
    _benchmark_invoice(db_session, m)
    db_session.commit()
    with violates(db_session, pge.RestrictViolation, message="invoice_lines is append-only"):
        run(db_session, statement)


def test_invoice_line_is_a_snapshot_of_the_sale(db_session, m):
    """Later changes to the product or the order line's item master do not reach the invoice line."""
    _benchmark_invoice(db_session, m)
    db_session.commit()
    run(db_session, "UPDATE products SET gst_rate = 18, hsn_code = '2106'")
    run(db_session, "UPDATE items SET name = 'Renamed', list_price = 99 WHERE code = 'MUR-200'")
    db_session.commit()
    assert run(db_session, "SELECT hsn_code, gst_rate, description, unit_price FROM invoice_lines").one() == ("1905", D("5.00"), "Murukku 200 g", D("50.00"))


# ================================================================ header totals equal the sum of lines (checked at commit)
def test_a_complete_invoice_commits(db_session, m):
    _benchmark_invoice(db_session, m)
    db_session.commit()


def test_a_multi_line_invoice_commits_when_totals_are_the_sum_of_lines(db_session, m):
    order = _order(db_session, m)
    first = _oline(db_session, order, m["sku"], "150", "50.00")
    second = _oline(db_session, order, m["sku2"], "3", "120.10")  # taxable 360.30 -> 9.0075 each half, rounds to 9.01
    _approve(db_session, m, order)
    cgst2 = split_tax(D("360.30"), D("5"), "intra")
    invoice = _header(db_session, m, order, taxable_total="7860.30", cgst_total=str(D("187.50") + cgst2.cgst),
                      sgst_total=str(D("187.50") + cgst2.sgst), grand_total=str(D("7860.30") + D("375.00") + cgst2.total))
    _iline(db_session, invoice, order, first, m["sku"])
    _iline(db_session, invoice, order, second, m["sku2"], qty="3", unit_price="120.10", taxable_value="360.30",
           cgst_amount=str(cgst2.cgst), sgst_amount=str(cgst2.sgst))
    db_session.commit()
    assert scalar(db_session, "SELECT count(*) FROM invoice_lines") == 2


@pytest.mark.parametrize("over", [dict(taxable_total="7500.01", grand_total="7875.01"),
                                  dict(cgst_total="187.51", sgst_total="187.51", grand_total="7875.02")])
def test_header_totals_must_equal_the_sum_of_the_lines(db_session, m, over):
    order = _order(db_session, m)
    oline = _oline(db_session, order, m["sku"])
    _approve(db_session, m, order)
    invoice = _header(db_session, m, order, **over)
    _iline(db_session, invoice, order, oline, m["sku"])
    with violates(db_session, pge.CheckViolation, message="do not equal the sum of its lines"):
        db_session.commit()


def test_an_invoice_without_lines_cannot_commit(db_session, m):
    _header(db_session, m, _order(db_session, m))
    with violates(db_session, pge.CheckViolation, message="has no lines"):
        db_session.commit()


def test_creating_an_invoice_does_not_touch_any_other_table(db_session, m):
    """Order and invoice creation must not post stock (or anything else). Row counts everywhere else stay put."""
    owned = {"orders", "order_lines", "invoices", "invoice_lines"}
    tables = run(db_session, "SELECT tablename FROM pg_tables WHERE schemaname = 'public'").scalars().all()
    before = {t: scalar(db_session, f'SELECT count(*) FROM "{t}"') for t in tables if t not in owned}
    _benchmark_invoice(db_session, m)
    db_session.commit()
    after = {t: scalar(db_session, f'SELECT count(*) FROM "{t}"') for t in before}
    assert after == before


# ================================================================ credit notes
def test_credit_note_cancels_an_invoice_in_one_transaction(db_session, m):
    _, _, invoice = _benchmark_invoice(db_session, m)
    db_session.commit()
    _cancel_with_credit_note(db_session, m, invoice)
    db_session.commit()
    assert scalar(db_session, "SELECT grand_total FROM credit_notes") == D("7875.00")
    assert scalar(db_session, "SELECT restock FROM credit_notes") is False


def test_one_credit_note_per_invoice(db_session, m):
    _, _, invoice = _benchmark_invoice(db_session, m)
    db_session.commit()
    _cancel_with_credit_note(db_session, m, invoice)
    db_session.commit()
    with violates(db_session, pge.UniqueViolation, "credit_notes_invoice_id_key"):
        _cancel_with_credit_note(db_session, m, invoice, credit_note_no="CN/26-27/0002")


def test_credit_note_number_is_unique(db_session, m):
    _, _, first = _benchmark_invoice(db_session, m)
    _, _, second = _benchmark_invoice(db_session, m, "B", seq=2)
    _cancel_with_credit_note(db_session, m, first)
    db_session.commit()
    with violates(db_session, pge.UniqueViolation, "credit_notes_credit_note_no_key"):
        _cancel_with_credit_note(db_session, m, second)


@pytest.mark.parametrize("number, constraint", [("CN/26-27/00000001", "ck_credit_notes_number_length"),
                                                  (" ", "ck_credit_notes_number_not_blank")])
def test_credit_note_number_rules(db_session, m, number, constraint):
    _, _, invoice = _benchmark_invoice(db_session, m)
    db_session.commit()
    with violates(db_session, pge.CheckViolation, constraint):
        _cancel_with_credit_note(db_session, m, invoice, credit_note_no=number)


def test_credit_note_reason_is_required(db_session, m):
    _, _, invoice = _benchmark_invoice(db_session, m)
    db_session.commit()
    with violates(db_session, pge.CheckViolation, "ck_credit_notes_reason_not_blank"):
        _cancel_with_credit_note(db_session, m, invoice, reason="  ")
    with violates(db_session, pge.NotNullViolation):
        _cancel_with_credit_note(db_session, m, invoice, reason=None)


def test_credit_note_restock_must_be_chosen(db_session, m):
    _, _, invoice = _benchmark_invoice(db_session, m)
    db_session.commit()
    with violates(db_session, pge.NotNullViolation):
        _cancel_with_credit_note(db_session, m, invoice, restock=None)


@pytest.mark.parametrize("over", [dict(grand_total="7000.00"), dict(taxable_total="7000.00"), dict(cgst_total="100.00"),
                                  dict(round_off="0.10")])
def test_credit_note_must_reverse_the_whole_invoice(db_session, m, over):
    """Full reversal only (design assumption 12): every amount equals the invoice's."""
    _, _, invoice = _benchmark_invoice(db_session, m)
    db_session.commit()
    with violates(db_session, pge.ForeignKeyViolation, "fk_credit_notes_invoice_amounts"):
        _cancel_with_credit_note(db_session, m, invoice, **over)


@pytest.mark.parametrize("statement", ["UPDATE credit_notes SET reason = 'edited'", "DELETE FROM credit_notes"])
def test_credit_notes_are_append_only(db_session, m, statement):
    _, _, invoice = _benchmark_invoice(db_session, m)
    db_session.commit()
    _cancel_with_credit_note(db_session, m, invoice)
    db_session.commit()
    with violates(db_session, pge.RestrictViolation, message="credit_notes is append-only"):
        run(db_session, statement)


def test_an_invoice_cannot_be_cancelled_without_a_credit_note(db_session, m):
    _, _, invoice = _benchmark_invoice(db_session, m)
    db_session.commit()
    run(db_session, "UPDATE invoices SET status = 'cancelled' WHERE id = :i", i=invoice)
    with violates(db_session, pge.CheckViolation, message="cancelled without a credit note"):
        db_session.commit()


def test_a_credit_note_needs_a_cancelled_invoice(db_session, m):
    _, _, invoice = _benchmark_invoice(db_session, m)
    db_session.commit()
    h = run(db_session, "SELECT taxable_total, cgst_total, sgst_total, igst_total, round_off, grand_total FROM invoices").one()
    run(db_session, "INSERT INTO credit_notes (credit_note_no, invoice_id, issue_date, reason, taxable_total, cgst_total, sgst_total, "
                    "igst_total, round_off, grand_total, restock, created_by) VALUES ('CN/26-27/0009', :i, '2026-11-21', 'x', :a, :b, :c, :d, :e, :f, false, :u)",
        i=invoice, a=h[0], b=h[1], c=h[2], d=h[3], e=h[4], f=h[5], u=m["ada"])
    with violates(db_session, pge.CheckViolation, message="not cancelled"):
        db_session.commit()


# ================================================================ review round: immutability and lifecycle links
def _commit_benchmark(db_session, m):
    order, oline, invoice = _benchmark_invoice(db_session, m)
    db_session.commit()
    return order, oline, invoice


def test_lines_cannot_be_added_to_an_invoice_after_it_was_issued(db_session, m):
    """A zero-value line (a free sample) must not slip into an issued invoice later."""
    order, oline, invoice = _commit_benchmark(db_session, m)
    run(db_session, "ALTER TABLE order_lines DISABLE TRIGGER trg_freeze")  # the order freeze is a different guard; bypass it to reach this one
    try:
        extra = _oline(db_session, order, m["sku2"], "3", "0.00")
    finally:
        run(db_session, "ALTER TABLE order_lines ENABLE TRIGGER trg_freeze")
    db_session.commit()
    with violates(db_session, pge.RestrictViolation, message="issued invoice"):
        _iline(db_session, invoice, order, extra, m["sku2"], qty="3", unit_price="0.00", taxable_value="0.00", cgst_amount="0", sgst_amount="0")


def test_dispatch_cannot_be_rewritten(db_session, m):
    _benchmark_invoice(db_session, m)
    run(db_session, "UPDATE invoices SET dispatched_at = now(), dispatched_by = :u", u=m["ada"])
    db_session.commit()
    for statement in ("UPDATE invoices SET dispatched_at = now() - interval '5 days'",
                      "UPDATE invoices SET dispatched_at = NULL, dispatched_by = NULL",
                      "UPDATE invoices SET dispatched_by = :other"):
        with violates(db_session, pge.RestrictViolation, message="dispatch"):
            run(db_session, statement, other=m["sam"])


def test_a_cancelled_invoice_cannot_be_dispatched(db_session, m):
    _, _, invoice = _benchmark_invoice(db_session, m)
    db_session.commit()
    _cancel_with_credit_note(db_session, m, invoice)
    db_session.commit()
    with violates(db_session, pge.RestrictViolation, message="dispatch"):
        run(db_session, "UPDATE invoices SET dispatched_at = now(), dispatched_by = :u", u=m["ada"])


@pytest.mark.parametrize("assignment", ["order_no = 'X'", "salesman_id = :sam", "client_uuid = gen_random_uuid()",
                                         "created_at = now() - interval '1 day'"])
def test_order_identity_is_never_editable(db_session, m, assignment):
    order = _order(db_session, m)
    db_session.commit()
    with violates(db_session, pge.RestrictViolation, message="orders"):
        run(db_session, f"UPDATE orders SET {assignment} WHERE id = :o", o=order, sam=m["ada"])


@pytest.mark.parametrize("assignment", ["customer_id = :b", "decided_at = now() - interval '9 days'", "decided_by = :sam"])
def test_an_approved_order_is_frozen(db_session, m, assignment):
    order = _order(db_session, m)
    _approve(db_session, m, order)
    db_session.commit()
    with violates(db_session, pge.RestrictViolation, message="orders"):
        run(db_session, f"UPDATE orders SET {assignment} WHERE id = :o", o=order, b=m["B"], sam=m["sam"])


def test_the_customer_cannot_change_together_with_the_approval(db_session, m):
    order = _order(db_session, m)
    db_session.commit()
    with violates(db_session, pge.RestrictViolation, message="orders"):
        run(db_session, "UPDATE orders SET status = 'approved', decided_by = :u, decided_at = now(), customer_id = :b WHERE id = :o",
            u=m["ada"], b=m["B"], o=order)


def test_the_customer_and_date_can_change_while_submitted(db_session, m):
    order = _order(db_session, m)
    run(db_session, "UPDATE orders SET customer_id = :b, order_date = '2026-11-21' WHERE id = :o", b=m["B"], o=order)


@pytest.mark.parametrize("status", ["submitted", "on_hold", "rejected", "cancelled"])
def test_an_invoice_needs_an_approved_order(db_session, m, status):
    order = _order(db_session, m)
    if status == "on_hold":
        run(db_session, "UPDATE orders SET status = 'on_hold', hold_reason = 'x' WHERE id = :o", o=order)
    elif status != "submitted":
        run(db_session, "UPDATE orders SET status = 'approved', decided_by = :u, decided_at = now() WHERE id = :o", u=m["ada"], o=order)
        if status == "cancelled":
            run(db_session, "UPDATE orders SET status = 'cancelled' WHERE id = :o", o=order)
        else:
            db_session.rollback()
            order = _order(db_session, m)
            run(db_session, "UPDATE orders SET status = 'rejected', decided_by = :u, decided_at = now() WHERE id = :o", u=m["ada"], o=order)
    with violates(db_session, pge.CheckViolation, message="is " + status):
        _header(db_session, m, order, link=False)


def test_an_order_cannot_be_invoiced_without_an_invoice(db_session, m):
    order = _order(db_session, m)
    _approve(db_session, m, order)
    run(db_session, "UPDATE orders SET status = 'invoiced' WHERE id = :o", o=order)
    with violates(db_session, pge.CheckViolation, message="without an invoice"):
        db_session.commit()


def test_an_issued_invoice_needs_its_order_marked_invoiced(db_session, m):
    order = _order(db_session, m)
    oline = _oline(db_session, order, m["sku"])
    _approve(db_session, m, order)
    invoice = _header(db_session, m, order, link=False)
    _iline(db_session, invoice, order, oline, m["sku"])
    with violates(db_session, pge.CheckViolation, message="has an issued invoice"):
        db_session.commit()


def test_an_order_with_an_issued_invoice_cannot_be_cancelled(db_session, m):
    order, _, _ = _commit_benchmark(db_session, m)
    run(db_session, "UPDATE orders SET status = 'cancelled' WHERE id = :o", o=order)
    with violates(db_session, pge.CheckViolation, message="has an issued invoice"):
        db_session.commit()


def test_an_invoice_must_follow_the_customer_master(db_session, m):
    """Customer A is registered in state 33; an invoice cannot claim an inter-state sale to Karnataka for A's order."""
    with violates(db_session, pge.CheckViolation, message="customer master"):
        _header(db_session, m, _order(db_session, m), place_of_supply_state="29", customer_gstin=GSTIN_KA, supply_type="inter",
                cgst_total="0", sgst_total="0", igst_total="375.00", grand_total="7875.00")


def test_an_invoice_must_carry_the_customers_registered_name(db_session, m):
    with violates(db_session, pge.CheckViolation, message="customer master"):
        _header(db_session, m, _order(db_session, m), customer_name="Somebody Else")


def test_an_invoice_line_must_repeat_the_order_lines_quantity_and_price(db_session, m):
    order = _order(db_session, m)
    oline = _oline(db_session, order, m["sku"])  # 150 at 50.00
    _approve(db_session, m, order)
    invoice = _header(db_session, m, order)
    with violates(db_session, pge.ForeignKeyViolation, "fk_invoice_lines_order_line"):
        _iline(db_session, invoice, order, oline, m["sku"], qty="1", unit_price="0.30", taxable_value="0.30", cgst_amount="0.01", sgst_amount="0.01")


def test_an_invoice_must_cover_every_order_line(db_session, m):
    order = _order(db_session, m)
    first = _oline(db_session, order, m["sku"])
    _oline(db_session, order, m["sku2"], "3", "120.10")
    _approve(db_session, m, order)
    invoice = _header(db_session, m, order)
    _iline(db_session, invoice, order, first, m["sku"])
    with violates(db_session, pge.CheckViolation, message="every order line"):
        db_session.commit()


# ================================================================ issued invoices are historical snapshots
SNAPSHOT_COLUMNS = ("customer_name, customer_gstin, customer_address, place_of_supply_state, seller_gstin, seller_state_code, "
                    "supply_type, taxable_total, cgst_total, sgst_total, igst_total, grand_total, invoice_no, status")


def _change_customer_master(db_session, m):
    """The customer renames, moves and re-registers: every field the invoice snapshot copied."""
    run(db_session, "UPDATE customers SET name = 'Retailer A (Renamed)', address = 'Madurai', gstin = '33AABCN9999N1Z5' WHERE id = :c", c=m["A"])
    db_session.commit()


def test_later_customer_changes_do_not_alter_an_issued_invoice(db_session, m):
    _, _, invoice = _benchmark_invoice(db_session, m)
    db_session.commit()
    before = tuple(run(db_session, f"SELECT {SNAPSHOT_COLUMNS} FROM invoices WHERE id = :i", i=invoice).one())
    _change_customer_master(db_session, m)
    after = tuple(run(db_session, f"SELECT {SNAPSHOT_COLUMNS} FROM invoices WHERE id = :i", i=invoice).one())
    assert after == before
    assert before[:3] == ("Retailer A", GSTIN_TN, "Chennai")


def test_a_customer_can_move_to_another_state_without_touching_old_invoices(db_session, m):
    _, _, invoice = _benchmark_invoice(db_session, m)
    db_session.commit()
    run(db_session, "UPDATE customers SET state_code = '29', gstin = :g WHERE id = :c", g=GSTIN_KA.replace("D5678G", "Q4321H"), c=m["A"])
    db_session.commit()
    row = run(db_session, "SELECT place_of_supply_state, supply_type, cgst_total, igst_total FROM invoices WHERE id = :i", i=invoice).one()
    assert tuple(row) == ("33", "intra", D("187.50"), D("0.00"))


def test_issued_invoice_stays_readable_with_its_lines_after_the_customer_changes(db_session, m):
    _, _, invoice = _benchmark_invoice(db_session, m)
    db_session.commit()
    _change_customer_master(db_session, m)
    row = run(db_session, "SELECT i.invoice_no, i.customer_name, l.description, l.taxable_value, c.name FROM invoices i "
                          "JOIN invoice_lines l ON l.invoice_id = i.id JOIN customers c ON c.id = i.customer_id WHERE i.id = :i", i=invoice).one()
    assert tuple(row) == ("INV/26-27/0001", "Retailer A", "Murukku 200 g", D("7500.00"), "Retailer A (Renamed)")


def test_dispatch_and_cancellation_still_work_after_the_customer_changes(db_session, m):
    _, _, invoice = _benchmark_invoice(db_session, m)
    db_session.commit()
    _change_customer_master(db_session, m)
    run(db_session, "UPDATE invoices SET dispatched_at = now(), dispatched_by = :u WHERE id = :i", u=m["ada"], i=invoice)
    db_session.commit()
    _cancel_with_credit_note(db_session, m, invoice)
    db_session.commit()
    assert scalar(db_session, "SELECT status FROM invoices WHERE id = :i", i=invoice) == "cancelled"


def test_the_master_check_applies_at_issue_only_a_new_invoice_follows_the_new_master(db_session, m):
    _benchmark_invoice(db_session, m)
    db_session.commit()
    _change_customer_master(db_session, m)
    order = _order(db_session, m)
    oline = _oline(db_session, order, m["sku"])
    _approve(db_session, m, order)
    with violates(db_session, pge.CheckViolation, message="customer master"):
        _header(db_session, m, order, invoice_no=fy_no("2026-27", 2), invoice_seq=2)  # still carries the old name
    order = _order(db_session, m)
    oline = _oline(db_session, order, m["sku"])
    _approve(db_session, m, order)
    invoice = _header(db_session, m, order, invoice_no=fy_no("2026-27", 2), invoice_seq=2,
                      customer_name="Retailer A (Renamed)", customer_gstin="33AABCN9999N1Z5", customer_address="Madurai")
    _iline(db_session, invoice, order, oline, m["sku"])
    db_session.commit()
    assert scalar(db_session, "SELECT count(*) FROM invoices") == 2


def test_immutability_still_blocks_aligning_an_old_invoice_with_the_new_master(db_session, m):
    _benchmark_invoice(db_session, m)
    db_session.commit()
    _change_customer_master(db_session, m)
    with violates(db_session, pge.RestrictViolation, message="invoices"):
        run(db_session, "UPDATE invoices SET customer_name = 'Retailer A (Renamed)', customer_address = 'Madurai'")


# ================================================================ complete lifecycles, every integrity trigger enabled
INTEGRITY_TRIGGERS = {
    "orders": {"trg_1_status", "trg_2_no_delete", "trg_3_guard", "trg_order_invoice_link"},
    "order_lines": {"trg_freeze"},
    "invoices": {"trg_1_status", "trg_2_posted", "trg_3_dispatch", "trg_snapshot", "trg_invoice_totals", "trg_cancellation_pair", "trg_order_invoice_link"},
    "invoice_lines": {"trg_append_only", "trg_late_line", "trg_invoice_totals"},
    "credit_notes": {"trg_append_only", "trg_cancellation_pair", "trg_credit_note_date"},
}


def test_every_integrity_trigger_is_installed_and_enabled(db_session, m):
    rows = run(db_session, "SELECT c.relname, t.tgname, t.tgenabled FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
                           "WHERE NOT t.tgisinternal AND c.relname = ANY(:names)", names=list(INTEGRITY_TRIGGERS)).all()
    found = {}
    for table, name, enabled in rows:
        found.setdefault(table, {})[name] = enabled
    for table, names in INTEGRITY_TRIGGERS.items():
        assert names <= set(found.get(table, {})), f"{table}: missing {names - set(found.get(table, {}))}"
        assert all(found[table][n] == "O" for n in names), f"{table}: a trigger is disabled"


def test_lifecycle_approved_order_to_invoiced_commits_in_one_transaction(db_session, m):
    order = _order(db_session, m)
    oline = _oline(db_session, order, m["sku"])
    _approve(db_session, m, order)
    invoice = _header(db_session, m, order, link=False)
    _iline(db_session, invoice, order, oline, m["sku"])
    run(db_session, "UPDATE orders SET status = 'invoiced' WHERE id = :o", o=order)
    db_session.commit()
    assert scalar(db_session, "SELECT status FROM orders WHERE id = :o", o=order) == "invoiced"
    assert scalar(db_session, "SELECT status FROM invoices WHERE id = :i", i=invoice) == "issued"
    assert scalar(db_session, "SELECT grand_total FROM invoices WHERE id = :i", i=invoice) == D("7875.00")


def test_lifecycle_issued_invoice_to_full_credit_note_commits(db_session, m):
    order, _, invoice = _commit_benchmark(db_session, m)
    _cancel_with_credit_note(db_session, m, invoice)
    db_session.commit()
    assert scalar(db_session, "SELECT status FROM invoices WHERE id = :i", i=invoice) == "cancelled"
    assert scalar(db_session, "SELECT grand_total FROM credit_notes WHERE invoice_id = :i", i=invoice) == D("7875.00")
    assert scalar(db_session, "SELECT status FROM orders WHERE id = :o", o=order) == "invoiced"  # cancellation leaves the order as it was (v1)


def test_whole_order_to_audit_sales_path_commits_in_two_transactions(db_session, m):
    """submitted -> approved -> invoiced (+ invoice and lines), then cancelled by credit note. No trigger is disabled."""
    order = _order(db_session, m)
    oline = _oline(db_session, order, m["sku"])
    db_session.commit()
    _approve(db_session, m, order)
    db_session.commit()
    invoice = _header(db_session, m, order)
    _iline(db_session, invoice, order, oline, m["sku"])
    db_session.commit()
    _cancel_with_credit_note(db_session, m, invoice)
    db_session.commit()
    assert tuple(run(db_session, "SELECT o.status, i.status FROM orders o JOIN invoices i ON i.order_id = o.id").one()) == ("invoiced", "cancelled")


def test_partial_transition_order_invoiced_but_invoice_missing_fails_at_commit(db_session, m):
    order = _order(db_session, m)
    _approve(db_session, m, order)
    run(db_session, "UPDATE orders SET status = 'invoiced' WHERE id = :o", o=order)
    with violates(db_session, pge.CheckViolation, message="without an invoice"):
        db_session.commit()


def test_partial_transition_invoice_issued_but_order_not_invoiced_fails_at_commit(db_session, m):
    order = _order(db_session, m)
    oline = _oline(db_session, order, m["sku"])
    _approve(db_session, m, order)
    invoice = _header(db_session, m, order, link=False)
    _iline(db_session, invoice, order, oline, m["sku"])
    with violates(db_session, pge.CheckViolation, message="has an issued invoice"):
        db_session.commit()


def test_partial_transition_invoice_cancelled_without_credit_note_fails_at_commit(db_session, m):
    _, _, invoice = _commit_benchmark(db_session, m)
    run(db_session, "UPDATE invoices SET status = 'cancelled' WHERE id = :i", i=invoice)
    with violates(db_session, pge.CheckViolation, message="without a credit note"):
        db_session.commit()


def test_partial_transition_credit_note_without_cancelling_fails_at_commit(db_session, m):
    _, _, invoice = _commit_benchmark(db_session, m)
    h = run(db_session, "SELECT taxable_total, cgst_total, sgst_total, igst_total, round_off, grand_total FROM invoices").one()
    run(db_session, "INSERT INTO credit_notes (credit_note_no, invoice_id, issue_date, reason, taxable_total, cgst_total, sgst_total, igst_total, "
                    "round_off, grand_total, restock, created_by) VALUES ('CN/26-27/0001', :i, '2026-11-21', 'x', :a, :b, :c, :d, :e, :f, false, :u)",
        i=invoice, a=h[0], b=h[1], c=h[2], d=h[3], e=h[4], f=h[5], u=m["ada"])
    with violates(db_session, pge.CheckViolation, message="not cancelled"):
        db_session.commit()


def test_partial_transition_invoice_without_any_lines_fails_at_commit(db_session, m):
    order = _order(db_session, m)
    _oline(db_session, order, m["sku"])
    _header(db_session, m, order)
    with violates(db_session, pge.CheckViolation, message="has no lines"):
        db_session.commit()


# ================================================================ credit-note date
@pytest.mark.parametrize("issue_date", [date(2026, 11, 20), date(2026, 11, 21), date(2027, 2, 1)])
def test_credit_note_on_or_after_the_invoice_date_is_accepted(db_session, m, issue_date):
    _, _, invoice = _commit_benchmark(db_session, m)
    _cancel_with_credit_note(db_session, m, invoice, issue_date=issue_date)
    db_session.commit()


@pytest.mark.parametrize("issue_date", [date(2026, 11, 19), date(2026, 4, 1), date(2020, 1, 1)])
def test_credit_note_cannot_precede_the_invoice_date(db_session, m, issue_date):
    _, _, invoice = _commit_benchmark(db_session, m)
    with violates(db_session, pge.CheckViolation, message="precedes the invoice date"):
        _cancel_with_credit_note(db_session, m, invoice, issue_date=issue_date)


# ================================================================ NaN is not a quantity, a price or an amount
@pytest.mark.parametrize("column, constraint", [("qty", "ck_order_lines_qty_positive"), ("unit_price", "ck_order_lines_unit_price_nonnegative")])
def test_order_line_rejects_nan(db_session, m, column, constraint):
    order = _order(db_session, m)
    db_session.commit()
    values = {"qty": "1", "unit_price": "1"}
    values[column] = "NaN"
    with violates(db_session, pge.CheckViolation, constraint):
        _oline(db_session, order, m["sku"], values["qty"], values["unit_price"])


@pytest.mark.parametrize("over", [dict(taxable_total="NaN", cgst_total="NaN", sgst_total="NaN", igst_total="0", grand_total="NaN"),
                                  dict(cgst_total="NaN", sgst_total="NaN", grand_total="NaN")])
def test_invoice_header_rejects_nan_amounts(db_session, m, over):
    with violates(db_session, pge.CheckViolation, "ck_invoices_amounts_nonnegative"):
        _header(db_session, m, _order(db_session, m), **over)


@pytest.mark.parametrize("over, constraint", [(dict(qty="NaN"), "ck_invoice_lines_qty_positive"),
                                               (dict(unit_price="NaN", taxable_value="NaN", cgst_amount="NaN", sgst_amount="NaN"), "ck_invoice_lines_unit_price_nonnegative")])
def test_invoice_line_rejects_nan(db_session, m, over, constraint):
    order = _order(db_session, m)
    oline = _oline(db_session, order, m["sku"])
    _approve(db_session, m, order)
    invoice = _header(db_session, m, order)
    with violates(db_session, pge.CheckViolation, constraint):
        _iline(db_session, invoice, order, oline, m["sku"], **over)
