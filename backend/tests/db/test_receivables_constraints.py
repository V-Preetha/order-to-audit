"""Migration 0006: payments, payment allocations and the append-only audit log, tested against real PostgreSQL with real commits.

The cross-row money rules (allocation <= payment, allocations <= invoice, invoice still issued, payment still posted) are DEFERRED
constraint triggers, so they fire at COMMIT; tests that expect them commit for real. Concurrency tests use separate connections and
force the two commit-time checks to overlap (see tests.db.test_inventory_constraints._overlapping_commits).

Benchmark: Retailer A's invoice is 7,500.00 + 187.50 CGST + 187.50 SGST = 7,875.00; a 4,000.00 payment leaves 3,875.00 outstanding.
"""
import json
import uuid
from datetime import date
from decimal import Decimal

import psycopg.errors as pge
import pytest
from sqlalchemy import text

from tests.db.helpers import run, scalar, violates
from tests.db.test_bom_constraints import m  # noqa: F401
from tests.db.test_inventory_constraints import _overlapping_commits, x  # noqa: F401
from tests.db.test_production_constraints import p  # noqa: F401
from tests.db.test_sales_constraints import _benchmark_invoice, _cancel_with_credit_note

D = Decimal
D0 = date(2026, 11, 20)
_seq = iter(range(1, 1_000_000))


# ---------------------------------------------------------------- builders
def _invoice(s, x, customer="A", seq=1):
    """A committed, issued benchmark invoice (A: 7,875.00; B: 6,300.00). Returns its id."""
    _, _, invoice = _benchmark_invoice(s, x, customer, seq=seq)
    s.commit()
    return invoice


def _payment(s, x, amount="4000.00", customer="A", method="upi", **over):
    n = next(_seq)
    cols = dict(payment_no=f"PAY/{n:04d}", client_uuid=str(uuid.uuid4()), customer_id=x[customer], amount=amount, payment_date=D0,
                method=method, reference=f"REF{n}", created_by=x["ada"])
    cols.update(over)
    run(s, f"INSERT INTO payments ({', '.join(cols)}) VALUES ({', '.join(':' + k for k in cols)})", **cols)
    return scalar(s, "SELECT id FROM payments WHERE payment_no = :n", n=cols["payment_no"])


def _alloc(s, x, payment, invoice, amount, customer="A"):
    cols = dict(payment_id=payment, invoice_id=invoice, customer_id=x[customer], amount=amount, created_by=x["ada"])
    run(s, f"INSERT INTO payment_allocations ({', '.join(cols)}) VALUES ({', '.join(':' + k for k in cols)})", **cols)
    return scalar(s, "SELECT max(id) FROM payment_allocations WHERE payment_id = :p AND invoice_id = :i", p=payment, i=invoice)


def _reverse(s, x, payment, reason="Cheque bounced"):
    run(s, "UPDATE payments SET status = 'reversed', reversed_by = :u, reverse_reason = :r WHERE id = :p", u=x["ada"], r=reason, p=payment)


def _outstanding(s, invoice):
    """grand_total minus allocations from POSTED payments (the definition used by the later reporting view)."""
    return scalar(s, "SELECT i.grand_total - coalesce((SELECT sum(a.amount) FROM payment_allocations a JOIN payments p ON p.id = a.payment_id "
                     "WHERE a.invoice_id = i.id AND p.status = 'posted'), 0) FROM invoices i WHERE i.id = :i", i=invoice)


# ================================================================ the benchmark
def test_benchmark_payment_of_4000_leaves_3875_outstanding(db_session, x):
    s = db_session
    invoice = _invoice(s, x)
    assert scalar(s, "SELECT grand_total FROM invoices WHERE id = :i", i=invoice) == D("7875.00")
    pay = _payment(s, x, "4000.00")
    _alloc(s, x, pay, invoice, "4000.00")
    s.commit()
    assert _outstanding(s, invoice) == D("3875.00")


def test_paise_are_preserved_exactly(db_session, x):
    s = db_session
    invoice = _invoice(s, x)
    pay = _payment(s, x, "1234.56")
    _alloc(s, x, pay, invoice, "1234.56")
    s.commit()
    assert scalar(s, "SELECT amount FROM payment_allocations") == D("1234.56")
    assert _outstanding(s, invoice) == D("6640.44")


def test_a_valid_partial_allocation_commits_and_a_second_one_completes_it(db_session, x):
    s = db_session
    invoice = _invoice(s, x)
    first, second = _payment(s, x, "4000.00"), _payment(s, x, "3875.00")
    _alloc(s, x, first, invoice, "1500.00")                 # partial use of a payment...
    _alloc(s, x, second, invoice, "3875.00")                # ...and partial settlement of the invoice
    s.commit()
    assert _outstanding(s, invoice) == D("2500.00")
    # a further payment finishes the invoice
    third = _payment(s, x, "2500.00")
    _alloc(s, x, third, invoice, "2500.00")
    s.commit()
    assert _outstanding(s, invoice) == D("0.00")


# ================================================================ payments
def test_a_payment_commits_with_every_method(db_session, x):
    for method in ("cash", "upi", "bank", "cheque"):
        _payment(db_session, x, "10.00", method=method)
    db_session.commit()
    assert scalar(db_session, "SELECT count(*) FROM payments") == 4


@pytest.mark.parametrize("amount", ["0", "-1.00", "NaN", "0.004"])
def test_payment_amount_must_be_positive(db_session, x, amount):
    with violates(db_session, pge.CheckViolation, "ck_payments_amount"):
        _payment(db_session, x, amount)


def test_payment_method_is_one_of_the_documented_ones(db_session, x):
    with violates(db_session, pge.CheckViolation, "ck_payments_method"):
        _payment(db_session, x, method="card")


def test_duplicate_client_uuid_rejected(db_session, x):
    key = str(uuid.uuid4())
    _payment(db_session, x, client_uuid=key)
    db_session.commit()
    with violates(db_session, pge.UniqueViolation, "payments_client_uuid_key"):
        _payment(db_session, x, client_uuid=key)


def test_duplicate_payment_number_rejected(db_session, x):
    _payment(db_session, x, payment_no="PAY/DUP")
    db_session.commit()
    with violates(db_session, pge.UniqueViolation, "payments_payment_no_key"):
        _payment(db_session, x, payment_no="PAY/DUP")


def test_payment_number_and_reference_cannot_be_blank(db_session, x):
    with violates(db_session, pge.CheckViolation, "ck_payments_payment_no_not_blank"):
        _payment(db_session, x, payment_no="  ")
    with violates(db_session, pge.CheckViolation, "ck_payments_reference_not_blank"):
        _payment(db_session, x, reference="  ")


def test_payment_customer_must_exist(db_session, x):
    with violates(db_session, pge.ForeignKeyViolation, "payments_customer_id_fkey"):
        _payment(db_session, x, customer_id=999999)


def test_a_payment_must_start_posted(db_session, x):
    with violates(db_session, pge.CheckViolation, message="illegal status transition"):
        _payment(db_session, x, status="reversed")


def test_payment_creation_time_is_stamped_by_the_database_but_the_business_date_is_kept(db_session, x):
    pay = _payment(db_session, x, created_at="2001-01-01 00:00+00", payment_date=date(2026, 4, 1))
    assert scalar(db_session, "SELECT created_at = now() FROM payments WHERE id = :p", p=pay) is True
    assert scalar(db_session, "SELECT payment_date FROM payments WHERE id = :p", p=pay) == date(2026, 4, 1)


# ---------------------------------------------------------------- reversal: a controlled lifecycle, history kept
def test_reversal_stamps_the_time_and_keeps_the_row(db_session, x):
    pay = _payment(db_session, x)
    db_session.commit()
    run(db_session, "UPDATE payments SET status = 'reversed', reversed_at = timestamp '2001-01-01', reversed_by = :u, reverse_reason = 'Cheque bounced' WHERE id = :p",
        u=x["ada"], p=pay)
    db_session.commit()
    assert scalar(db_session, "SELECT reversed_at <> timestamp '2001-01-01' AND reversed_at >= created_at AND reversed_at <= now() FROM payments WHERE id = :p", p=pay) is True
    assert scalar(db_session, "SELECT count(*) FROM payments") == 1


def test_reversal_needs_no_client_timestamp(db_session, x):
    pay = _payment(db_session, x)
    db_session.commit()
    _reverse(db_session, x, pay)
    assert scalar(db_session, "SELECT reversed_at = now() FROM payments WHERE id = :p", p=pay) is True


@pytest.mark.parametrize("assignment", ["status = 'reversed'", "status = 'reversed', reversed_by = :u", "status = 'reversed', reversed_by = :u, reverse_reason = '  '",
                                        "reversed_by = :u", "reverse_reason = 'x'"])
def test_reversal_columns_must_agree_with_the_status(db_session, x, assignment):
    pay = _payment(db_session, x)
    db_session.commit()
    with violates(db_session, pge.CheckViolation, "ck_payments_reverse_columns"):
        run(db_session, f"UPDATE payments SET {assignment} WHERE id = :p", p=pay, u=x["ada"])


def test_a_reversed_payment_cannot_be_reinstated_or_edited(db_session, x):
    pay = _payment(db_session, x)
    db_session.commit()
    _reverse(db_session, x, pay)
    db_session.commit()
    with violates(db_session, pge.CheckViolation, message="illegal status transition"):
        run(db_session, "UPDATE payments SET status = 'posted', reversed_at = NULL, reversed_by = NULL, reverse_reason = NULL WHERE id = :p", p=pay)
    for assignment in ("reverse_reason = 'rewritten'", "reversed_at = reversed_at + interval '1 day'", "reversed_by = reversed_by + 1"):
        with violates(db_session, pge.RestrictViolation, message="reversed payment"):
            run(db_session, f"UPDATE payments SET {assignment} WHERE id = :p", p=pay)


@pytest.mark.parametrize("assignment", ["amount = 1", "customer_id = customer_id + 1", "payment_no = 'X'", "method = 'cash'", "payment_date = payment_date + 1",
                                        "created_by = created_by + 1", "reference = 'x'"])
def test_a_posted_payment_cannot_be_edited(db_session, x, assignment):
    pay = _payment(db_session, x)
    db_session.commit()
    with violates(db_session, pge.RestrictViolation, message="cannot be changed once posted"):
        run(db_session, f"UPDATE payments SET {assignment} WHERE id = :p", p=pay)


def test_a_payment_cannot_be_deleted(db_session, x):
    pay = _payment(db_session, x)
    db_session.commit()
    with violates(db_session, pge.RestrictViolation, message="cannot be deleted"):
        run(db_session, "DELETE FROM payments WHERE id = :p", p=pay)


def test_reversal_keeps_the_allocations_as_history_but_they_stop_counting(db_session, x):
    s = db_session
    invoice = _invoice(s, x)
    pay = _payment(s, x, "4000.00")
    _alloc(s, x, pay, invoice, "4000.00")
    s.commit()
    assert _outstanding(s, invoice) == D("3875.00")
    _reverse(s, x, pay)
    s.commit()
    assert scalar(s, "SELECT count(*) FROM payment_allocations") == 1      # history preserved
    assert _outstanding(s, invoice) == D("7875.00")                        # no longer counted


def test_a_reversed_payment_frees_invoice_capacity(db_session, x):
    s = db_session
    invoice = _invoice(s, x)
    first = _payment(s, x, "7875.00")
    _alloc(s, x, first, invoice, "7875.00")
    s.commit()
    second = _payment(s, x, "7875.00")
    _alloc(s, x, second, invoice, "1.00")
    with violates(db_session, pge.CheckViolation, message="exceed the invoice total"):
        s.commit()
    second = _payment(s, x, "7875.00")
    _reverse(s, x, first)
    s.commit()
    _alloc(s, x, second, invoice, "7875.00")
    s.commit()
    assert _outstanding(s, invoice) == D("0.00")


# ================================================================ allocations: amounts
def test_allocation_exceeding_payment_rejected(db_session, x):
    s = db_session
    invoice = _invoice(s, x)
    pay = _payment(s, x, "4000.00")
    s.commit()
    _alloc(s, x, pay, invoice, "4000.01")
    with violates(s, pge.CheckViolation, message="exceed the payment amount"):
        s.commit()
    assert scalar(s, "SELECT count(*) FROM payment_allocations") == 0     # nothing partial survives


def test_allocation_up_to_the_payment_amount_is_accepted(db_session, x):
    s = db_session
    invoice = _invoice(s, x)
    pay = _payment(s, x, "4000.00")
    _alloc(s, x, pay, invoice, "4000.00")
    s.commit()
    assert scalar(s, "SELECT sum(amount) FROM payment_allocations") == D("4000.00")


def test_allocations_to_several_invoices_may_not_add_up_to_more_than_the_payment(db_session, x):
    s = db_session
    first, second = _invoice(s, x, seq=1), _invoice(s, x, seq=2)
    pay = _payment(s, x, "10000.00")
    _alloc(s, x, pay, first, "7875.00")
    _alloc(s, x, pay, second, "2125.01")
    with violates(s, pge.CheckViolation, message="exceed the payment amount"):
        s.commit()


def test_allocation_exceeding_invoice_rejected(db_session, x):
    s = db_session
    invoice = _invoice(s, x)
    pay = _payment(s, x, "9000.00")
    _alloc(s, x, pay, invoice, "7875.01")
    with violates(s, pge.CheckViolation, message="exceed the invoice total"):
        s.commit()
    assert scalar(s, "SELECT count(*) FROM payment_allocations") == 0


def test_allocation_up_to_the_invoice_total_is_accepted(db_session, x):
    s = db_session
    invoice = _invoice(s, x)
    pay = _payment(s, x, "9000.00")
    _alloc(s, x, pay, invoice, "7875.00")
    s.commit()
    assert _outstanding(s, invoice) == D("0.00")


def test_several_payments_may_not_over_settle_one_invoice(db_session, x):
    s = db_session
    invoice = _invoice(s, x)
    first, second = _payment(s, x, "5000.00"), _payment(s, x, "5000.00")
    _alloc(s, x, first, invoice, "5000.00")
    s.commit()
    _alloc(s, x, second, invoice, "2875.01")
    with violates(s, pge.CheckViolation, message="exceed the invoice total"):
        s.commit()
    _alloc(s, x, second, invoice, "2875.00")
    s.commit()
    assert _outstanding(s, invoice) == D("0.00")


@pytest.mark.parametrize("amount", ["0", "-1.00", "NaN", "0.004"])
def test_allocation_amount_must_be_positive(db_session, x, amount):
    s = db_session
    invoice = _invoice(s, x)
    pay = _payment(s, x)
    with violates(s, pge.CheckViolation, "ck_payment_allocations_amount"):
        _alloc(s, x, pay, invoice, amount)


def test_a_payment_is_allocated_to_an_invoice_once(db_session, x):
    s = db_session
    invoice = _invoice(s, x)
    pay = _payment(s, x, "4000.00")
    _alloc(s, x, pay, invoice, "1000.00")
    s.commit()
    with violates(s, pge.UniqueViolation, "payment_allocations_payment_id_invoice_id_key"):
        _alloc(s, x, pay, invoice, "1000.00")


# ================================================================ allocations: customer, invoice state, payment state
def test_allocation_other_customer_rejected_by_the_invoice_key(db_session, x):
    """Payment and allocation belong to B, the invoice to A: only the invoice composite foreign key can refuse it."""
    s = db_session
    invoice = _invoice(s, x, "A")
    pay = _payment(s, x, "4000.00", customer="B")
    with violates(s, pge.ForeignKeyViolation, "fk_payment_allocations_invoice_customer"):
        _alloc(s, x, pay, invoice, "100.00", customer="B")


def test_allocation_other_customer_rejected_by_the_payment_key(db_session, x):
    """Invoice and allocation belong to A, the payment to B: only the payment composite foreign key can refuse it."""
    s = db_session
    invoice = _invoice(s, x, "A")
    pay = _payment(s, x, "4000.00", customer="B")
    with violates(s, pge.ForeignKeyViolation, "fk_payment_allocations_payment_customer"):
        _alloc(s, x, pay, invoice, "100.00", customer="A")


def test_allocation_to_a_cancelled_invoice_rejected(db_session, x):
    s = db_session
    invoice = _invoice(s, x)
    _cancel_with_credit_note(s, x, invoice)
    s.commit()
    pay = _payment(s, x, "4000.00")
    _alloc(s, x, pay, invoice, "100.00")
    with violates(s, pge.CheckViolation, message="is not issued"):
        s.commit()
    assert scalar(s, "SELECT count(*) FROM payment_allocations") == 0


def test_allocation_to_a_reversed_payment_rejected(db_session, x):
    s = db_session
    invoice = _invoice(s, x)
    pay = _payment(s, x, "4000.00")
    s.commit()
    _reverse(s, x, pay)
    s.commit()
    _alloc(s, x, pay, invoice, "100.00")
    with violates(s, pge.CheckViolation, message="is not posted"):
        s.commit()


def test_an_invoice_with_live_allocations_cannot_be_cancelled(db_session, x):
    s = db_session
    invoice = _invoice(s, x)
    pay = _payment(s, x, "4000.00")
    _alloc(s, x, pay, invoice, "4000.00")
    s.commit()
    _cancel_with_credit_note(s, x, invoice)
    with violates(s, pge.CheckViolation, message="still has allocations"):
        s.commit()
    assert scalar(s, "SELECT status FROM invoices WHERE id = :i", i=invoice) == "issued"


def test_an_invoice_can_be_cancelled_once_its_payment_is_reversed(db_session, x):
    s = db_session
    invoice = _invoice(s, x)
    pay = _payment(s, x, "4000.00")
    _alloc(s, x, pay, invoice, "4000.00")
    s.commit()
    _reverse(s, x, pay)
    _cancel_with_credit_note(s, x, invoice)          # same transaction: the checks look at the final state at COMMIT
    s.commit()
    assert scalar(s, "SELECT status FROM invoices WHERE id = :i", i=invoice) == "cancelled"
    assert scalar(s, "SELECT count(*) FROM payment_allocations") == 1


# ---------------------------------------------------------------- deferred behaviour at COMMIT
def test_the_limits_are_checked_at_commit_not_per_statement(db_session, x):
    """Allocate to a full invoice and reverse the payment that filled it in the SAME transaction: only the final state matters."""
    s = db_session
    invoice = _invoice(s, x)
    first = _payment(s, x, "7875.00")
    _alloc(s, x, first, invoice, "7875.00")
    s.commit()
    second = _payment(s, x, "7875.00")
    _alloc(s, x, second, invoice, "7875.00")          # momentarily over-settled
    _reverse(s, x, first)                             # ...but the first payment is reversed before COMMIT
    s.commit()
    assert _outstanding(s, invoice) == D("0.00")


def test_set_constraints_immediate_runs_the_check_at_once(db_session, x):
    s = db_session
    invoice = _invoice(s, x)
    pay = _payment(s, x, "100.00")
    s.commit()
    _alloc(s, x, pay, invoice, "100.01")
    with violates(s, pge.CheckViolation, message="exceed the payment amount"):
        run(s, "SET CONSTRAINTS ALL IMMEDIATE")


def test_the_check_refuses_to_run_above_read_committed(engine, db_session, x):
    s = db_session
    invoice = _invoice(s, x)
    pay = _payment(s, x, "100.00")
    s.commit()
    with engine.connect() as conn:
        conn.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))
        conn.execute(text("INSERT INTO payment_allocations (payment_id, invoice_id, customer_id, amount, created_by) VALUES (:p, :i, :c, 50, :u)"),
                     dict(p=pay, i=invoice, c=x["A"], u=x["ada"]))
        with pytest.raises(Exception) as caught:
            conn.commit()
        assert "READ COMMITTED" in str(caught.value)
        conn.rollback()
    assert scalar(s, "SELECT count(*) FROM payment_allocations") == 0


# ---------------------------------------------------------------- allocations are history
def test_allocations_are_append_only(db_session, x):
    s = db_session
    invoice = _invoice(s, x)
    pay = _payment(s, x, "4000.00")
    alloc = _alloc(s, x, pay, invoice, "1000.00")
    s.commit()
    with violates(s, pge.RestrictViolation, message="append-only"):
        run(s, "UPDATE payment_allocations SET amount = 1 WHERE id = :a", a=alloc)
    with violates(s, pge.RestrictViolation, message="append-only"):
        run(s, "DELETE FROM payment_allocations WHERE id = :a", a=alloc)


@pytest.mark.parametrize("table", ["payment_allocations", "payments", "audit_log"])
def test_truncate_is_rejected_on_the_financial_tables(db_session, x, table):
    with violates(db_session, pge.RestrictViolation, message=f"{table} is append-only: TRUNCATE"):
        run(db_session, f"TRUNCATE {table} CASCADE")


def test_allocation_creation_time_is_stamped_by_the_database(db_session, x):
    s = db_session
    invoice = _invoice(s, x)
    pay = _payment(s, x, "4000.00")
    run(s, "INSERT INTO payment_allocations (payment_id, invoice_id, customer_id, amount, created_by, created_at) "
           "VALUES (:p, :i, :c, 10, :u, timestamp '2001-01-01')", p=pay, i=invoice, c=x["A"], u=x["ada"])
    assert scalar(s, "SELECT created_at = now() FROM payment_allocations") is True


# ================================================================ real concurrency: separate connections, overlapping commits
def test_two_concurrent_allocations_of_one_payment_cannot_both_commit(engine, db_session, x):
    s = db_session
    first_invoice, second_invoice = _invoice(s, x, seq=1), _invoice(s, x, seq=2)
    pay = _payment(s, x, "4000.00")
    s.commit()

    def allocate(invoice):
        def work(conn):
            conn.execute(text("INSERT INTO payment_allocations (payment_id, invoice_id, customer_id, amount, created_by) VALUES (:p, :i, :c, 3000, :u)"),
                         dict(p=pay, i=invoice, c=x["A"], u=x["ada"]))
        return work

    outcome, blocked = _overlapping_commits(engine, allocate(first_invoice), allocate(second_invoice), ["exceed the payment amount"])
    assert blocked, f"the second commit never waited on the lock, so nothing proves the lock serialises the checks: {outcome}"
    assert outcome == {"first": "ok", "second": "rejected"}, outcome
    assert scalar(s, "SELECT count(*) FROM payment_allocations") == 1
    assert scalar(s, "SELECT invoice_id FROM payment_allocations") == first_invoice
    assert scalar(s, "SELECT sum(amount) FROM payment_allocations WHERE payment_id = :p", p=pay) == D("3000.00")


def test_two_concurrent_allocations_to_one_invoice_cannot_both_commit(engine, db_session, x):
    s = db_session
    invoice = _invoice(s, x)
    first_pay, second_pay = _payment(s, x, "5000.00"), _payment(s, x, "5000.00")
    s.commit()

    def allocate(pay):
        def work(conn):
            conn.execute(text("INSERT INTO payment_allocations (payment_id, invoice_id, customer_id, amount, created_by) VALUES (:p, :i, :c, 5000, :u)"),
                         dict(p=pay, i=invoice, c=x["A"], u=x["ada"]))
        return work

    outcome, blocked = _overlapping_commits(engine, allocate(first_pay), allocate(second_pay), ["exceed the invoice total"])
    assert blocked, f"the second commit never waited on the lock, so nothing proves the lock serialises the checks: {outcome}"
    assert outcome == {"first": "ok", "second": "rejected"}, outcome
    assert scalar(s, "SELECT count(*) FROM payment_allocations") == 1
    assert scalar(s, "SELECT payment_id FROM payment_allocations") == first_pay
    assert _outstanding(s, invoice) == D("2875.00")


def test_an_allocation_racing_a_reversal_of_its_payment_ends_consistent(engine, db_session, x):
    """The allocation's check locks the payment row, so a concurrent reversal waits; whichever order they land in, no live allocation
    to a reversed payment is ever counted against the invoice."""
    s = db_session
    invoice = _invoice(s, x)
    pay = _payment(s, x, "4000.00")
    s.commit()

    def allocate(conn):
        conn.execute(text("INSERT INTO payment_allocations (payment_id, invoice_id, customer_id, amount, created_by) VALUES (:p, :i, :c, 4000, :u)"),
                     dict(p=pay, i=invoice, c=x["A"], u=x["ada"]))

    def reverse(conn):
        conn.execute(text("UPDATE payments SET status = 'reversed', reversed_by = :u, reverse_reason = 'raced' WHERE id = :p"), dict(u=x["ada"], p=pay))

    outcome, blocked = _overlapping_commits(engine, allocate, reverse, ["is not posted"])
    assert blocked, f"the reversal never waited on the allocation's lock: {outcome}"
    assert outcome == {"first": "ok", "second": "ok"}, outcome
    # allocation committed first, then the reversal: the allocation is history and no longer counts
    assert scalar(s, "SELECT status FROM payments WHERE id = :p", p=pay) == "reversed"
    assert _outstanding(s, invoice) == D("7875.00")


def test_infinity_cannot_be_stored_as_money(db_session, x):
    invoice = _invoice(db_session, x)
    with violates(db_session, pge.NumericValueOutOfRange):
        _payment(db_session, x, "Infinity")
    pay = _payment(db_session, x, "100.00")
    with violates(db_session, pge.NumericValueOutOfRange):
        _alloc(db_session, x, pay, invoice, "Infinity")


def test_the_invoice_cancellation_check_refuses_to_run_above_read_committed(engine, db_session, x):
    invoice = _invoice(db_session, x)
    with engine.connect() as conn:
        conn.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))
        conn.execute(text("UPDATE invoices SET status = status WHERE id = :i"), dict(i=invoice))
        with pytest.raises(Exception) as caught:
            conn.commit()
        assert "READ COMMITTED" in str(caught.value)
        conn.rollback()


def _cancel_work(invoice, user):
    """The cancellation a writer would run: its credit note, then the status change (raw SQL for a separate connection)."""
    def work(conn):
        conn.execute(text(
            "INSERT INTO credit_notes (credit_note_no, invoice_id, issue_date, reason, taxable_total, cgst_total, sgst_total, igst_total, round_off, grand_total, restock, created_by) "
            "SELECT 'CN/RACE/1', id, :d, 'race', taxable_total, cgst_total, sgst_total, igst_total, round_off, grand_total, false, :u FROM invoices WHERE id = :i"),
            dict(d=D0, u=user, i=invoice))
        conn.execute(text("UPDATE invoices SET status = 'cancelled' WHERE id = :i"), dict(i=invoice))
    return work


def _allocate_work(pay, invoice, customer, user, amount=4000):
    def work(conn):
        conn.execute(text("INSERT INTO payment_allocations (payment_id, invoice_id, customer_id, amount, created_by) VALUES (:p, :i, :c, :a, :u)"),
                     dict(p=pay, i=invoice, c=customer, a=amount, u=user))
    return work


def test_an_allocation_that_commits_first_blocks_a_concurrent_cancellation(engine, db_session, x):
    s = db_session
    invoice = _invoice(s, x)
    pay = _payment(s, x, "4000.00")
    s.commit()
    outcome, blocked = _overlapping_commits(engine, _allocate_work(pay, invoice, x["A"], x["ada"]), _cancel_work(invoice, x["ada"]), ["still has allocations"])
    assert blocked, f"the cancellation never waited for the allocation's lock: {outcome}"
    assert outcome == {"first": "ok", "second": "rejected"}, outcome
    assert scalar(s, "SELECT status FROM invoices WHERE id = :i", i=invoice) == "issued"
    assert scalar(s, "SELECT count(*) FROM credit_notes") == 0 and scalar(s, "SELECT count(*) FROM payment_allocations") == 1


def test_a_cancellation_that_commits_first_blocks_a_concurrent_allocation(engine, db_session, x):
    s = db_session
    invoice = _invoice(s, x)
    pay = _payment(s, x, "4000.00")
    s.commit()
    outcome, blocked = _overlapping_commits(engine, _cancel_work(invoice, x["ada"]), _allocate_work(pay, invoice, x["A"], x["ada"]), ["is not issued"])
    assert blocked, f"the allocation never waited for the cancellation's lock: {outcome}"
    assert outcome == {"first": "ok", "second": "rejected"}, outcome
    assert scalar(s, "SELECT status FROM invoices WHERE id = :i", i=invoice) == "cancelled"
    assert scalar(s, "SELECT count(*) FROM payment_allocations") == 0


def test_a_reversal_that_commits_first_blocks_a_concurrent_allocation(engine, db_session, x):
    s = db_session
    invoice = _invoice(s, x)
    pay = _payment(s, x, "4000.00")
    s.commit()

    def reverse(conn):
        conn.execute(text("UPDATE payments SET status = 'reversed', reversed_by = :u, reverse_reason = 'raced' WHERE id = :p"), dict(u=x["ada"], p=pay))

    outcome, blocked = _overlapping_commits(engine, reverse, _allocate_work(pay, invoice, x["A"], x["ada"]), ["is not posted"])
    assert blocked, f"the allocation never waited for the reversal's lock: {outcome}"
    assert outcome == {"first": "ok", "second": "rejected"}, outcome
    assert scalar(s, "SELECT count(*) FROM payment_allocations") == 0
    assert _outstanding(s, invoice) == D("7875.00")


# ================================================================ audit log
def _audit(s, x, **over):
    cols = dict(action="invoice.cancel", entity_type="invoice", entity_id=1, user_id=x["ada"], request_id=str(uuid.uuid4()),
                before=json.dumps({"status": "issued", "grand_total": "7875.00"}), after=json.dumps({"status": "cancelled"}))
    cols.update(over)
    run(s, f"INSERT INTO audit_log ({', '.join(cols)}) VALUES ({', '.join(':' + k if k not in ('before', 'after') else 'CAST(:' + k + ' AS jsonb)' for k in cols)})", **cols)
    return scalar(s, "SELECT max(id) FROM audit_log")


def test_an_audit_entry_records_who_what_when_and_the_before_and_after_snapshots(db_session, x):
    entry = _audit(db_session, x)
    db_session.commit()
    row = run(db_session, "SELECT user_id, action, entity_type, entity_id, before, after, request_id FROM audit_log WHERE id = :i", i=entry).one()
    assert (row.user_id, row.action, row.entity_type, row.entity_id) == (x["ada"], "invoice.cancel", "invoice", 1)
    assert row.before == {"status": "issued", "grand_total": "7875.00"} and row.after == {"status": "cancelled"}
    assert row.request_id is not None


def test_audit_time_is_stamped_by_the_database(db_session, x):
    entry = _audit(db_session, x, occurred_at="2001-01-01 00:00+00")
    assert scalar(db_session, "SELECT occurred_at >= now() AND occurred_at <= clock_timestamp() FROM audit_log WHERE id = :i", i=entry) is True


def test_audit_time_is_the_wall_clock_not_the_transaction_start(db_session, x):
    """A long transaction cannot stamp its late entries with its early start time, and each entry gets its own time."""
    first = _audit(db_session, x, action="a.first")
    run(db_session, "SELECT pg_sleep(0.05)")
    second = _audit(db_session, x, action="a.second")
    assert scalar(db_session, "SELECT occurred_at > now() FROM audit_log WHERE id = :i", i=second) is True
    assert scalar(db_session, "SELECT (SELECT occurred_at FROM audit_log WHERE id = :b) > (SELECT occurred_at FROM audit_log WHERE id = :a)", a=first, b=second) is True


def test_system_actions_and_event_entries_need_no_user_or_snapshots(db_session, x):
    entry = _audit(db_session, x, user_id=None, before=None, after=None, request_id=None, action="system.start")
    assert scalar(db_session, "SELECT user_id IS NULL AND before IS NULL AND after IS NULL FROM audit_log WHERE id = :i", i=entry) is True


def test_audit_user_must_exist(db_session, x):
    with violates(db_session, pge.ForeignKeyViolation):
        _audit(db_session, x, user_id=999999)


@pytest.mark.parametrize("over, constraint", [
    (dict(action="  "), "ck_audit_log_action_not_blank"),
    (dict(entity_type="  "), "ck_audit_log_entity_type_not_blank"),
    (dict(before="[1, 2]"), "ck_audit_log_before_object"),
    (dict(after="\"text\""), "ck_audit_log_after_object"),
])
def test_audit_entries_are_well_formed(db_session, x, over, constraint):
    with violates(db_session, pge.CheckViolation, constraint):
        _audit(db_session, x, **over)


def test_audit_entry_needs_an_entity_reference(db_session, x):
    with violates(db_session, pge.NotNullViolation):
        _audit(db_session, x, entity_id=None)


def test_the_audit_log_is_append_only(db_session, x):
    entry = _audit(db_session, x)
    db_session.commit()
    for statement in ("UPDATE audit_log SET action = 'edited' WHERE id = :i", "UPDATE audit_log SET occurred_at = now() WHERE id = :i",
                      "UPDATE audit_log SET after = NULL WHERE id = :i", "DELETE FROM audit_log WHERE id = :i"):
        with violates(db_session, pge.RestrictViolation, message="append-only"):
            run(db_session, statement, i=entry)
    assert scalar(db_session, "SELECT action FROM audit_log WHERE id = :i", i=entry) == "invoice.cancel"


# ================================================================ indexes
@pytest.mark.parametrize("index", ["ix_payment_allocations_invoice", "ix_payments_customer", "ix_payments_date", "ix_audit_log_entity", "ix_audit_log_user",
                                   "ix_audit_log_request"])
def test_report_indexes_exist(db_session, index):
    assert scalar(db_session, "SELECT count(*) FROM pg_indexes WHERE schemaname = 'public' AND indexname = :i", i=index) == 1
