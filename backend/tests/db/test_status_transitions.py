"""status_transitions seed (design section 8) and the enforce_status_transition() trigger function.

The workflow tables do not exist yet (later migrations), so each test builds a TEMP table with the
real table's name, attaches the real trigger function, and rolls back. Temp tables vanish with the
session and cannot collide with the permanent tables that later migrations create.
"""
import psycopg.errors as pge
import pytest
from sqlalchemy import text

from tests.db.helpers import run, scalar, violates

# Written out from design section 8, independently of the migration.
ALLOWED = {
    "orders": [
        ("(new)", "submitted"),
        ("submitted", "approved"), ("submitted", "on_hold"), ("submitted", "rejected"),
        ("on_hold", "approved"), ("on_hold", "rejected"),
        ("approved", "invoiced"), ("approved", "cancelled"),
        ("invoiced", "dispatched"), ("invoiced", "cancelled"),
        ("dispatched", "cancelled"),
    ],
    "stock_reservations": [("(new)", "active"), ("active", "released"), ("active", "fulfilled")],
    "invoices": [("(new)", "issued"), ("issued", "cancelled")],
    "production_runs": [("(new)", "in_progress"), ("in_progress", "completed")],
    "purchases": [("(new)", "posted"), ("posted", "voided")],
    "payments": [("(new)", "posted"), ("posted", "reversed")],
}
EDGES = [(entity, f, t) for entity, pairs in ALLOWED.items() for f, t in pairs]
STATUSES = {e: sorted({s for pair in pairs for s in pair} - {"(new)"}) for e, pairs in ALLOWED.items()}
ILLEGAL = [(e, f, t) for e in ALLOWED for f in STATUSES[e] for t in STATUSES[e]
           if f != t and (f, t) not in ALLOWED[e]]


def _scratch(session, table):
    run(session, f"CREATE TEMP TABLE {table} (id serial PRIMARY KEY, status text NOT NULL, note text)")


def _attach(session, table):
    run(session, f"CREATE TRIGGER trg_status BEFORE INSERT OR UPDATE ON {table} "
                 f"FOR EACH ROW EXECUTE FUNCTION enforce_status_transition()")


# ---------------------------------------------------------------- the seed
def test_seed_matches_design_section_8_exactly(db_session):
    rows = run(db_session, "SELECT entity, from_status, to_status FROM status_transitions").all()
    assert {tuple(r) for r in rows} == set(EDGES)
    assert len(rows) == len(EDGES) == 22


def test_status_transitions_seeded(db_session):
    assert scalar(db_session, "SELECT count(*) FROM status_transitions WHERE entity='orders' "
                              "AND from_status='on_hold' AND to_status='approved'") == 1
    assert scalar(db_session, "SELECT count(*) FROM status_transitions WHERE entity='production_runs' "
                              "AND from_status='in_progress' AND to_status='completed'") == 1


def test_a_completed_run_is_terminal(db_session):
    assert scalar(db_session, "SELECT count(*) FROM status_transitions "
                              "WHERE entity='production_runs' AND from_status='completed'") == 0


def test_seed_survives_the_truncate_between_tests(db_session):
    assert scalar(db_session, "SELECT count(*) FROM status_transitions") == 22


def test_a_status_cannot_transition_to_itself_in_the_table(db_session):
    with violates(db_session, pge.CheckViolation, "ck_status_transitions_distinct"):
        run(db_session, "INSERT INTO status_transitions VALUES ('orders', 'approved', 'approved')")


def test_an_edge_cannot_be_listed_twice(db_session):
    with violates(db_session, pge.UniqueViolation, "status_transitions_pkey"):
        run(db_session, "INSERT INTO status_transitions VALUES ('orders', 'submitted', 'approved')")


# ---------------------------------------------------------------- the trigger function
@pytest.mark.parametrize("entity, from_status, to_status", EDGES)
def test_every_allowed_transition_succeeds(db_session, entity, from_status, to_status):
    _scratch(db_session, entity)
    if from_status == "(new)":
        _attach(db_session, entity)
        run(db_session, f"INSERT INTO {entity} (status) VALUES (:s)", s=to_status)
        assert scalar(db_session, f"SELECT status FROM {entity}") == to_status
    else:
        run(db_session, f"INSERT INTO {entity} (status) VALUES (:s)", s=from_status)  # before the trigger exists
        _attach(db_session, entity)
        run(db_session, f"UPDATE {entity} SET status = :s", s=to_status)
        assert scalar(db_session, f"SELECT status FROM {entity}") == to_status


@pytest.mark.parametrize("entity, from_status, to_status", ILLEGAL)
def test_illegal_transitions_rejected(db_session, entity, from_status, to_status):
    _scratch(db_session, entity)
    run(db_session, f"INSERT INTO {entity} (status) VALUES (:s)", s=from_status)
    _attach(db_session, entity)
    with violates(db_session, pge.CheckViolation, message=f"{from_status} -> {to_status}"):
        run(db_session, f"UPDATE {entity} SET status = :s", s=to_status)


@pytest.mark.parametrize("entity", sorted(ALLOWED))
def test_a_new_row_may_only_start_at_the_initial_status(db_session, entity):
    _scratch(db_session, entity)
    _attach(db_session, entity)
    initial = {t for f, t in ALLOWED[entity] if f == "(new)"}
    for status in STATUSES[entity]:
        if status in initial:
            continue
        with violates(db_session, pge.CheckViolation, message=f"(new) -> {status}"):
            run(db_session, f"INSERT INTO {entity} (status) VALUES (:s)", s=status)
        _scratch(db_session, entity)  # the rollback discarded the scratch table
        _attach(db_session, entity)


def test_a_completed_run_cannot_be_reopened(db_session):
    _scratch(db_session, "production_runs")
    _attach(db_session, "production_runs")
    run(db_session, "INSERT INTO production_runs (status) VALUES ('in_progress')")
    run(db_session, "UPDATE production_runs SET status = 'completed'")
    with violates(db_session, pge.CheckViolation, message="completed -> in_progress"):
        run(db_session, "UPDATE production_runs SET status = 'in_progress'")


def test_an_edit_that_leaves_the_status_alone_is_not_a_transition(db_session):
    _scratch(db_session, "orders")
    _attach(db_session, "orders")
    run(db_session, "INSERT INTO orders (status) VALUES ('submitted')")
    run(db_session, "UPDATE orders SET note = 'call back' WHERE status = 'submitted'")
    assert scalar(db_session, "SELECT note FROM orders") == "call back"


def test_an_unknown_status_is_rejected(db_session):
    _scratch(db_session, "orders")
    _attach(db_session, "orders")
    with violates(db_session, pge.CheckViolation, message="(new) -> shipped"):
        run(db_session, "INSERT INTO orders (status) VALUES ('shipped')")


def test_a_table_with_no_lifecycle_fails_closed(db_session):
    _scratch(db_session, "widgets")
    _attach(db_session, "widgets")
    with violates(db_session, pge.CheckViolation, message="widgets"):
        run(db_session, "INSERT INTO widgets (status) VALUES ('submitted')")


def test_the_error_names_table_and_both_statuses(db_session):
    _scratch(db_session, "orders")
    run(db_session, "INSERT INTO orders (status) VALUES ('on_hold')")
    _attach(db_session, "orders")
    with violates(db_session, pge.CheckViolation, message="orders: on_hold -> invoiced"):
        run(db_session, "UPDATE orders SET status = 'invoiced'")


def test_a_session_cannot_shadow_the_lifecycle_table_with_a_temp_table(db_session):
    """The function must read the real table, not a same-named temp table an attacker creates first."""
    run(db_session, "CREATE TEMP TABLE status_transitions (entity text, from_status text, to_status text)")
    run(db_session, "INSERT INTO status_transitions VALUES ('orders', '(new)', 'dispatched')")
    _scratch(db_session, "orders")
    _attach(db_session, "orders")
    with violates(db_session, pge.CheckViolation, message="(new) -> dispatched"):
        run(db_session, "INSERT INTO orders (status) VALUES ('dispatched')")


def test_a_self_edge_to_new_is_rejected_in_the_table(db_session):
    with violates(db_session, pge.CheckViolation, "ck_status_transitions_new_is_initial_only"):
        run(db_session, "INSERT INTO status_transitions VALUES ('orders', 'submitted', '(new)')")
