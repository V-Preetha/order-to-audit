"""forbid_mutation() and guard_posted_row(): append-only and posted-record protection (design 5.1).

Tests use TEMP tables carrying the real table names, because the real tables arrive in later
migrations. Everything rolls back.
"""
import psycopg.errors as pge
import pytest

from tests.db.helpers import run, scalar, violates


def _append_only(session, table="audit_log"):
    run(session, f"CREATE TEMP TABLE {table} (id serial PRIMARY KEY, action text NOT NULL)")
    run(session, f"CREATE TRIGGER trg_append_only BEFORE UPDATE OR DELETE ON {table} "
                 f"FOR EACH ROW EXECUTE FUNCTION forbid_mutation()")
    run(session, f"INSERT INTO {table} (action) VALUES ('order approved')")


# ---------------------------------------------------------------- forbid_mutation
@pytest.mark.parametrize("table", ["audit_log", "stock_movements", "credit_notes", "invoice_lines", "purchase_lines"])
def test_append_only_tables_accept_inserts(db_session, table):
    _append_only(db_session, table)
    run(db_session, f"INSERT INTO {table} (action) VALUES ('second entry')")
    assert scalar(db_session, f"SELECT count(*) FROM {table}") == 2


def test_audit_rows_cannot_be_updated(db_session):
    _append_only(db_session)
    with violates(db_session, pge.RestrictViolation, message="audit_log is append-only: UPDATE"):
        run(db_session, "UPDATE audit_log SET action = 'something else'")


def test_audit_rows_cannot_be_deleted(db_session):
    _append_only(db_session)
    with violates(db_session, pge.RestrictViolation, message="audit_log is append-only: DELETE"):
        run(db_session, "DELETE FROM audit_log")


def test_a_refused_update_leaves_the_row_unchanged(db_session):
    _append_only(db_session)
    with pytest.raises(Exception):
        with db_session.begin_nested():  # savepoint: keeps the scratch table alive after the failure
            run(db_session, "UPDATE audit_log SET action = 'tampered'")
    assert scalar(db_session, "SELECT action FROM audit_log") == "order approved"


# ---------------------------------------------------------------- guard_posted_row
def _guarded(session, table, columns):
    run(session, f"CREATE TEMP TABLE {table} (id serial PRIMARY KEY, {columns})")
    run(session, f"CREATE TRIGGER trg_guard BEFORE UPDATE OR DELETE ON {table} "
                 f"FOR EACH ROW EXECUTE FUNCTION guard_posted_row()")


INVOICE_COLS = "status text NOT NULL, grand_total numeric NOT NULL, dispatched_at timestamptz, dispatched_by bigint"
PAYMENT_COLS = ("status text NOT NULL, amount numeric NOT NULL, reversed_at timestamptz, "
                "reversed_by bigint, reversed_reason text")
RUN_COLS = "status text NOT NULL, note text"


@pytest.fixture
def invoice(db_session):
    _guarded(db_session, "invoices", INVOICE_COLS)
    run(db_session, "INSERT INTO invoices (status, grand_total) VALUES ('issued', 7875)")


@pytest.mark.parametrize("assignment", [
    "status = 'cancelled'",
    "dispatched_at = now()",
    "dispatched_by = 4",
    "status = 'cancelled', dispatched_at = now(), dispatched_by = 4",
])
def test_issued_invoice_may_change_only_status_and_dispatch_fields(db_session, invoice, assignment):
    run(db_session, f"UPDATE invoices SET {assignment}")


@pytest.mark.parametrize("assignment", ["grand_total = 1", "grand_total = 7875, status = 'cancelled', id = 99"])
def test_issued_invoice_amounts_are_frozen(db_session, invoice, assignment):
    with violates(db_session, pge.RestrictViolation, message="invoices"):
        run(db_session, f"UPDATE invoices SET {assignment}")


def test_invoice_cannot_be_deleted(db_session, invoice):
    with violates(db_session, pge.RestrictViolation, message="cannot be deleted"):
        run(db_session, "DELETE FROM invoices")


@pytest.fixture
def payment(db_session):
    _guarded(db_session, "payments", PAYMENT_COLS)
    run(db_session, "INSERT INTO payments (status, amount) VALUES ('posted', 4000)")


def test_payment_may_be_reversed(db_session, payment):
    run(db_session, "UPDATE payments SET status='reversed', reversed_at=now(), reversed_by=2, reversed_reason='bounced'")


def test_payment_amount_is_frozen(db_session, payment):
    with violates(db_session, pge.RestrictViolation, message="payments"):
        run(db_session, "UPDATE payments SET amount = 5000")


def test_payment_cannot_be_deleted(db_session, payment):
    with violates(db_session, pge.RestrictViolation, message="cannot be deleted"):
        run(db_session, "DELETE FROM payments")


@pytest.fixture
def production_run(db_session):
    _guarded(db_session, "production_runs", RUN_COLS)
    run(db_session, "INSERT INTO production_runs (status) VALUES ('in_progress')")


def test_run_in_progress_can_be_edited_and_completed(db_session, production_run):
    run(db_session, "UPDATE production_runs SET note = 'second batch started'")
    run(db_session, "UPDATE production_runs SET status = 'completed'")


@pytest.mark.parametrize("assignment", ["note = 'late edit'", "status = 'completed'", "status = 'in_progress'"])
def test_completed_run_rejects_any_update(db_session, production_run, assignment):
    run(db_session, "UPDATE production_runs SET status = 'completed'")
    with violates(db_session, pge.RestrictViolation, message="completed production run"):
        run(db_session, f"UPDATE production_runs SET {assignment}")


def test_run_cannot_be_deleted_even_in_progress(db_session, production_run):
    with violates(db_session, pge.RestrictViolation, message="cannot be deleted"):
        run(db_session, "DELETE FROM production_runs")


def test_a_table_without_a_guard_rule_fails_closed(db_session):
    _guarded(db_session, "widgets", "status text")
    run(db_session, "INSERT INTO widgets (status) VALUES ('x')")
    with violates(db_session, pge.RestrictViolation, message="no guard rule for widgets"):
        run(db_session, "UPDATE widgets SET status = 'y'")
