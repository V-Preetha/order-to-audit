"""Constraint tests for migration 0004: production runs, run lines, overheads.

A run is created together with its snapshot lines in one transaction; "the lines equal the BOM scaled by the
batches" is checked at COMMIT by a deferred trigger, so most tests commit for real. Actual consumption does not
exist in these tables: it is derived from the stock ledger (a later migration), and a test pins that.
"""
from decimal import Decimal

import psycopg.errors as pge
import pytest
from sqlalchemy import text

from app.domain.variance import compute_variance
from tests.db.helpers import run, scalar, violates
from tests.db.test_bom_constraints import _active_pair, _bom, _draft_pair, _pline, _recipe, _rline, _set, m  # noqa: F401

D = Decimal
BENCH = (("flour", "40"), ("oil", "10"), ("spice", "1.2"), ("pouch", "200"), ("carton", "10"))
_n = iter(range(1, 100_000))


@pytest.fixture
def p(db_session, m):  # noqa: F811
    """Active benchmark BOM (200 packets per batch) and an operator, committed."""
    run(db_session, "INSERT INTO users (username, full_name, password_hash, role) VALUES ('op', 'Operator', 'x', 'admin'), ('op2', 'Other', 'x', 'admin')")
    run(db_session, "INSERT INTO settings (id, factory_name, factory_gstin, factory_state_code, variance_tolerance_pct) "
                    "VALUES (1, 'Factory', '33AABCM1111A1Z1', '33', 3.00)")
    recipe, bom = _active_pair(db_session, m)
    return dict(recipe=recipe, bom=bom, user=scalar(db_session, "SELECT id FROM users WHERE username='op'"),
                user2=scalar(db_session, "SELECT id FROM users WHERE username='op2'"))


def _run(s, m, p, bom=None, sku=None, batches="1", expected="200", tolerance="3.00", run_no=None, **extra):
    cols = dict(run_no=run_no or f"RUN-{next(_n):04d}", sku_item_id=sku or m["sku200"], bom_id=bom or p["bom"], planned_batches=batches,
                expected_output_packets=expected, tolerance_pct=tolerance, started_by=p["user"])
    cols.update(extra)
    run(s, f"INSERT INTO production_runs ({', '.join(cols)}) VALUES ({', '.join(':' + k for k in cols)})", **cols)
    return scalar(s, "SELECT id FROM production_runs WHERE run_no = :n", n=cols["run_no"])


def _rl(s, run_id, item_id, qty):
    run(s, "INSERT INTO production_run_lines (run_id, item_id, item_kind, unit, expected_qty_base) "
           "SELECT :r, id, kind, base_unit, :q FROM items WHERE id = :i", r=run_id, i=item_id, q=qty)


def _lines(s, m, run_id, scale="1", skip=(), override=None):
    for name, qty in BENCH:
        if name in skip:
            continue
        value = (D(qty) * D(scale)).quantize(D("0.001"), rounding="ROUND_HALF_UP")
        _rl(s, run_id, m[name], format(value, "f") if override is None or name not in override else override[name])


def _salt(s):
    return run(s, "INSERT INTO items (code, name, kind, base_unit) VALUES ('SALT', 'Salt', 'raw', 'kg') RETURNING id").scalar()


def _started(s, m, p, **kw):
    """A complete, valid run (header plus the five snapshot lines), uncommitted."""
    run_id = _run(s, m, p, **kw)
    _lines(s, m, run_id)
    return run_id


def _complete(s, p, run_id, output="188", **extra):
    sets = {"status": "'completed'", "good_output_packets": ":g", "completed_at": "now()", "completed_by": ":u"}
    sets.update(extra)
    run(s, f"UPDATE production_runs SET {', '.join(f'{k} = {v}' for k, v in sets.items())} WHERE id = :r", r=run_id, g=output, u=p["user"])


# ================================================================ run header: SKU, BOM, snapshots
def test_the_benchmark_run_commits(db_session, m, p):
    run_id = _started(db_session, m, p)
    db_session.commit()
    row = run(db_session, "SELECT status, planned_batches, expected_output_packets, tolerance_pct, good_output_packets FROM production_runs WHERE id = :r", r=run_id).one()
    assert tuple(row) == ("in_progress", D("1.000"), D("200.000"), D("3.00"), None)
    assert scalar(db_session, "SELECT count(*) FROM production_run_lines WHERE run_id = :r", r=run_id) == 5


def test_run_sku_must_match_bom(db_session, m, p):
    other_sku = scalar(db_session, "SELECT id FROM items WHERE code = 'MIX-200'")
    with violates(db_session, pge.ForeignKeyViolation, "fk_production_runs_bom_sku"):
        _run(db_session, m, p, sku=other_sku)


def test_run_requires_an_existing_bom(db_session, m, p):
    with violates(db_session, pge.ForeignKeyViolation, "fk_production_runs_bom_sku"):
        _run(db_session, m, p, bom=99999)


def test_run_sku_must_be_the_boms_finished_sku(db_session, m, p):
    with violates(db_session, pge.ForeignKeyViolation, "fk_production_runs_bom_sku"):
        _run(db_session, m, p, sku=m["flour"])


def test_a_run_cannot_start_on_a_draft_bom(db_session, m, p):
    recipe = _recipe(db_session, m["murukku"], "MUR-R2", version=2)
    for item, qty in (("flour", "40"), ("oil", "10"), ("spice", "1.2")):
        _rline(db_session, recipe, m[item], qty)
    draft = _bom(db_session, m["sku200"], m["murukku"], recipe, version=2)
    db_session.commit()
    with violates(db_session, pge.CheckViolation, message="draft and is not active"):
        _run(db_session, m, p, bom=draft)


def test_a_run_cannot_start_on_a_retired_bom(db_session, m, p):
    run(db_session, "UPDATE boms SET status = 'retired' WHERE id = :b", b=p["bom"])
    db_session.commit()
    with violates(db_session, pge.CheckViolation, message="retired and is not active"):
        _run(db_session, m, p)


@pytest.mark.parametrize("batches, expected, ok", [
    ("1", "200", True), ("2", "400", True), ("0.5", "100", True), ("1.5", "300", True), ("0.333", "66.6", True),
    ("1", "188", False), ("1", "201", False), ("2", "200", False), ("0.5", "200", False),
])
def test_expected_output_is_the_boms_yield_times_the_batches(db_session, m, p, batches, expected, ok):
    if ok:
        _run(db_session, m, p, batches=batches, expected=expected)
    else:
        with violates(db_session, pge.CheckViolation, message="expected output"):
            _run(db_session, m, p, batches=batches, expected=expected)


@pytest.mark.parametrize("over, constraint", [
    (dict(batches="0"), "ck_production_runs_planned_batches_positive"),
    (dict(batches="-1"), "ck_production_runs_planned_batches_positive"),
    (dict(batches="NaN"), "ck_production_runs_planned_batches_positive"),
    (dict(tolerance="-0.01"), "ck_production_runs_tolerance"),
    (dict(tolerance="100.01"), "ck_production_runs_tolerance"),
    (dict(tolerance="NaN"), "ck_production_runs_tolerance"),
    (dict(run_no="  "), "ck_production_runs_run_no_not_blank"),
])
def test_run_header_field_rules(db_session, m, p, over, constraint):
    with violates(db_session, pge.CheckViolation, constraint):
        _run(db_session, m, p, **over)


def test_run_number_is_unique(db_session, m, p):
    _started(db_session, m, p, run_no="RUN-DUP")
    db_session.commit()
    with violates(db_session, pge.UniqueViolation, "production_runs_run_no_key"):
        _run(db_session, m, p, run_no="RUN-DUP")


def test_a_run_has_a_starter(db_session, m, p):
    with violates(db_session, pge.NotNullViolation):
        _run(db_session, m, p, started_by=None)


def test_a_new_run_cannot_start_completed(db_session, m, p):
    with violates(db_session, pge.CheckViolation, message="(new) -> completed"):
        _run(db_session, m, p, status="completed")


# ================================================================ run lines: item types and snapshots
def test_run_line_rejects_finished_item(db_session, m, p):
    run_id = _started(db_session, m, p)
    db_session.commit()
    with violates(db_session, pge.CheckViolation, "ck_production_run_lines_item_kind"):
        _rl(db_session, run_id, m["sku200"], "1")


def test_run_line_kind_must_match_the_item(db_session, m, p):
    run_id = _started(db_session, m, p)
    salt = _salt(db_session)
    db_session.commit()
    with violates(db_session, pge.ForeignKeyViolation, "fk_production_run_lines_item_kind"):
        run(db_session, "INSERT INTO production_run_lines (run_id, item_id, item_kind, unit, expected_qty_base) "
                        "SELECT :r, id, 'packaging', base_unit, 0 FROM items WHERE id = :i", r=run_id, i=salt)


def test_run_line_unit_must_be_the_items_base_unit(db_session, m, p):
    run_id = _started(db_session, m, p)
    salt = _salt(db_session)
    db_session.commit()
    with violates(db_session, pge.ForeignKeyViolation, "fk_production_run_lines_item_unit"):
        run(db_session, "INSERT INTO production_run_lines (run_id, item_id, item_kind, unit, expected_qty_base) "
                        "VALUES (:r, :i, 'raw', 'g', 0)", r=run_id, i=salt)


@pytest.mark.parametrize("qty, constraint", [("-1", "ck_production_run_lines_expected_nonnegative"), ("NaN", "ck_production_run_lines_expected_nonnegative")])
def test_run_line_expected_quantity_rules(db_session, m, p, qty, constraint):
    run_id = _started(db_session, m, p)
    salt = _salt(db_session)
    db_session.commit()
    with violates(db_session, pge.CheckViolation, constraint):
        _rl(db_session, run_id, salt, qty)


def test_an_item_appears_once_per_run(db_session, m, p):
    run_id = _started(db_session, m, p)
    db_session.commit()
    with violates(db_session, pge.UniqueViolation, "production_run_lines_run_id_item_id_key"):
        _rl(db_session, run_id, m["flour"], "40")


def test_run_line_needs_an_existing_run(db_session, m, p):
    with violates(db_session, pge.ForeignKeyViolation):
        _rl(db_session, 99999, m["flour"], "1")


@pytest.mark.parametrize("batches, expected", [("1", "200"), ("2", "400"), ("0.5", "100"), ("1.5", "300"), ("0.333", "66.6"), ("0.001", "0.2")])
def test_run_lines_are_the_bom_scaled_by_the_batches(db_session, m, p, batches, expected):
    run_id = _run(db_session, m, p, batches=batches, expected=expected)
    _lines(db_session, m, run_id, scale=batches)
    db_session.commit()
    got = dict(run(db_session, "SELECT i.code, l.expected_qty_base FROM production_run_lines l JOIN items i ON i.id = l.item_id WHERE l.run_id = :r", r=run_id).all())
    assert got["FLOUR"] == (D(40) * D(batches)).quantize(D("0.001"), rounding="ROUND_HALF_UP")
    assert got["SPICE"] == (D("1.2") * D(batches)).quantize(D("0.001"), rounding="ROUND_HALF_UP")


@pytest.mark.parametrize("skip, override, why", [
    ((), {"flour": "41"}, "wrong quantity"),
    ((), {"pouch": "199.999"}, "off by one thousandth"),
    (("spice",), None, "missing raw line"),
    (("carton",), None, "missing packaging line"),
    ((("flour", "oil", "spice", "pouch", "carton")), None, "no lines at all"),
])
def test_run_lines_that_differ_from_the_bom_fail_at_commit(db_session, m, p, skip, override, why):
    run_id = _run(db_session, m, p)
    _lines(db_session, m, run_id, skip=skip, override=override)
    with violates(db_session, pge.CheckViolation, message="do not match the BOM"):
        db_session.commit()


def test_a_planned_line_for_an_item_outside_the_bom_fails_at_commit(db_session, m, p):
    oil2 = _salt(db_session)
    run_id = _run(db_session, m, p)
    _lines(db_session, m, run_id)
    _rl(db_session, run_id, oil2, "1")  # expected > 0 but not in the BOM
    with violates(db_session, pge.CheckViolation, message="do not match the BOM"):
        db_session.commit()


def test_an_unplanned_material_line_with_zero_expected_is_allowed(db_session, m, p):
    salt = _salt(db_session)
    run_id = _started(db_session, m, p)
    _rl(db_session, run_id, salt, "0")
    db_session.commit()
    assert scalar(db_session, "SELECT count(*) FROM production_run_lines WHERE run_id = :r", r=run_id) == 6


def test_an_unplanned_line_can_be_added_while_the_run_is_in_progress(db_session, m, p):
    salt = _salt(db_session)
    run_id = _started(db_session, m, p)
    db_session.commit()
    _rl(db_session, run_id, salt, "0")
    db_session.commit()


def test_no_line_can_be_added_once_the_run_is_completed(db_session, m, p):
    salt = _salt(db_session)
    run_id = _started(db_session, m, p)
    db_session.commit()
    _complete(db_session, p, run_id)
    db_session.commit()
    with violates(db_session, pge.RestrictViolation, message="production_run_lines"):
        _rl(db_session, run_id, salt, "0")


@pytest.mark.parametrize("statement", ["UPDATE production_run_lines SET expected_qty_base = 1", "DELETE FROM production_run_lines"])
def test_run_lines_are_append_only_snapshots(db_session, m, p, statement):
    _started(db_session, m, p)
    db_session.commit()
    with violates(db_session, pge.RestrictViolation, message="production_run_lines is append-only"):
        run(db_session, statement)


def test_snapshots_are_immune_to_later_bom_and_settings_changes(db_session, m, p):
    run_id = _started(db_session, m, p)
    db_session.commit()

    def snapshot():
        return (tuple(run(db_session, "SELECT expected_output_packets, tolerance_pct FROM production_runs WHERE id = :r", r=run_id).one()),
                [tuple(r) for r in run(db_session, "SELECT item_id, expected_qty_base FROM production_run_lines WHERE run_id = :r ORDER BY item_id", r=run_id).all()])
    before = snapshot()
    run(db_session, "UPDATE boms SET status = 'retired' WHERE id = :b", b=p["bom"])  # the BOM moves on
    run(db_session, "UPDATE settings SET variance_tolerance_pct = 7.50")
    db_session.commit()
    assert snapshot() == before
    _complete(db_session, p, run_id)  # a run on a since-retired BOM can still be completed
    db_session.commit()


def test_run_tolerance_must_equal_the_settings_value(db_session, m, p):
    _run(db_session, m, p, tolerance="3.00")
    with violates(db_session, pge.CheckViolation, message="settings tolerance"):
        _run(db_session, m, p, tolerance="5.00")


def test_a_run_cannot_start_before_the_settings_row_exists(db_session, m, p):
    run(db_session, "DELETE FROM settings")
    db_session.commit()
    with violates(db_session, pge.CheckViolation, message="no settings row"):
        _run(db_session, m, p, tolerance="99.99")


def test_a_planned_line_added_in_a_later_transaction_still_fails_at_commit(db_session, m, p):
    salt = _salt(db_session)
    run_id = _started(db_session, m, p)
    db_session.commit()
    with violates(db_session, pge.CheckViolation, message="do not match the BOM"):
        _rl(db_session, run_id, salt, "1")  # expected > 0 for an item the BOM does not list
        db_session.commit()


def test_completion_cannot_precede_the_start(db_session, m, p):
    run_id = _started(db_session, m, p)
    db_session.commit()
    with violates(db_session, pge.CheckViolation, "ck_production_runs_completed_after_started"):
        _complete(db_session, p, run_id, completed_at="started_at - interval '10 days'")


# ================================================================ lifecycle: in_progress -> completed
def test_run_starts_in_progress(db_session, m, p):
    _started(db_session, m, p)
    assert scalar(db_session, "SELECT status FROM production_runs") == "in_progress"


def test_benchmark_run_completes_with_188_packets(db_session, m, p):
    run_id = _started(db_session, m, p)
    db_session.commit()
    _complete(db_session, p, run_id, output="188")
    db_session.commit()
    row = run(db_session, "SELECT status, good_output_packets, expected_output_packets FROM production_runs WHERE id = :r", r=run_id).one()
    assert tuple(row) == ("completed", D("188.000"), D("200.000"))


def test_completed_requires_output_and_timestamp(db_session, m, p):
    run_id = _started(db_session, m, p)
    db_session.commit()
    for sets in ("status = 'completed', completed_at = now(), completed_by = :u",
                 "status = 'completed', good_output_packets = 188, completed_by = NULL, completed_at = NULL"):
        with violates(db_session, pge.CheckViolation, "ck_production_runs_completion"):
            run(db_session, f"UPDATE production_runs SET {sets} WHERE id = :r", r=run_id, u=p["user"])


@pytest.mark.parametrize("assignment", ["good_output_packets = 188", "completed_at = now(), completed_by = :u", "completion_note = 'x'"])
def test_in_progress_with_any_result_is_rejected(db_session, m, p, assignment):
    run_id = _started(db_session, m, p)
    db_session.commit()
    with violates(db_session, pge.CheckViolation, "ck_production_runs_in_progress_blank"):
        run(db_session, f"UPDATE production_runs SET {assignment} WHERE id = :r", r=run_id, u=p["user"])


def test_completion_time_and_person_come_as_a_pair(db_session, m, p):
    run_id = _started(db_session, m, p)
    db_session.commit()
    with violates(db_session, pge.CheckViolation, "ck_production_runs_completed_by_pair"):
        run(db_session, "UPDATE production_runs SET status = 'completed', good_output_packets = 188, completed_at = now() WHERE id = :r", r=run_id)


def test_output_cannot_be_negative_or_nan(db_session, m, p):
    run_id = _started(db_session, m, p)
    db_session.commit()
    for bad in ("-1", "NaN"):
        with violates(db_session, pge.CheckViolation, "ck_production_runs_good_output_nonnegative"):
            _complete(db_session, p, run_id, output=bad)


def test_zero_output_completion_requires_completion_note(db_session, m, p):
    run_id = _started(db_session, m, p)
    db_session.commit()
    sql = ("UPDATE production_runs SET status = 'completed', good_output_packets = 0, completed_at = now(), completed_by = :u, "
           "completion_note = :n WHERE id = :r")
    for note in (None, "", "   "):
        with violates(db_session, pge.CheckViolation, "ck_production_runs_zero_output_note"):
            run(db_session, sql, r=run_id, u=p["user"], n=note)
    run(db_session, sql, r=run_id, u=p["user"], n="Dough spoiled, batch discarded")
    db_session.commit()
    assert scalar(db_session, "SELECT good_output_packets FROM production_runs") == D("0.000")


def test_completed_to_in_progress_rejected(db_session, m, p):
    run_id = _started(db_session, m, p)
    db_session.commit()
    _complete(db_session, p, run_id)
    db_session.commit()
    with violates(db_session, pge.CheckViolation, message="completed -> in_progress"):
        run(db_session, "UPDATE production_runs SET status = 'in_progress', good_output_packets = NULL, completed_at = NULL, completed_by = NULL WHERE id = :r", r=run_id)


@pytest.mark.parametrize("assignment", ["notes = 'late edit'", "good_output_packets = 200", "tolerance_pct = 9", "expected_output_packets = 1",
                                         "run_no = 'X'", "completion_note = 'x'", "status = 'completed'", "completed_at = now()"])
def test_completed_run_is_frozen(db_session, m, p, assignment):
    run_id = _started(db_session, m, p)
    db_session.commit()
    _complete(db_session, p, run_id)
    db_session.commit()
    with violates(db_session, pge.RestrictViolation, message="completed production run cannot be changed"):
        run(db_session, f"UPDATE production_runs SET {assignment} WHERE id = :r", r=run_id)


@pytest.mark.parametrize("finish", [False, True])
def test_a_run_cannot_be_deleted(db_session, m, p, finish):
    run_id = _started(db_session, m, p)
    db_session.commit()
    if finish:
        _complete(db_session, p, run_id)
        db_session.commit()
    with violates(db_session, pge.RestrictViolation, message="cannot be deleted"):
        run(db_session, "DELETE FROM production_runs WHERE id = :r", r=run_id)


def test_an_in_progress_run_may_be_annotated_and_its_identity_is_fixed(db_session, m, p):
    run_id = _started(db_session, m, p)
    db_session.commit()
    run(db_session, "UPDATE production_runs SET notes = 'second shift' WHERE id = :r", r=run_id)
    for assignment in ("bom_id = 999", "sku_item_id = 999", "planned_batches = 2", "expected_output_packets = 1", "tolerance_pct = 9", "run_no = 'X'",
                       "started_by = :u2"):
        with violates(db_session, pge.RestrictViolation, message="production_runs"):
            run(db_session, f"UPDATE production_runs SET {assignment} WHERE id = :r", r=run_id, u2=p["user2"])


# ================================================================ actual consumption is not stored
def test_run_tables_hold_no_actual_consumption_columns(db_session, m, p):
    def columns(table):
        return set(run(db_session, "SELECT column_name FROM information_schema.columns WHERE table_schema = 'public' AND table_name = :t", t=table).scalars())
    assert columns("production_run_lines") == {"id", "run_id", "item_id", "item_kind", "unit", "expected_qty_base"}
    assert columns("production_runs") == {"id", "run_no", "sku_item_id", "bom_id", "planned_batches", "expected_output_packets", "tolerance_pct",
                                         "good_output_packets", "completion_note", "status", "started_at", "started_by", "completed_at",
                                         "completed_by", "notes"}


# ================================================================ the mandatory 200 expected / 188 actual scenario
def test_schema_represents_200_expected_188_actual_and_the_minus_six_percent_flag(db_session, m, p):
    run_id = _started(db_session, m, p)
    db_session.commit()
    _complete(db_session, p, run_id, output="188")
    db_session.commit()
    expected, actual, tolerance = (D(str(v)) for v in run(db_session, "SELECT expected_output_packets, good_output_packets, tolerance_pct FROM production_runs WHERE id = :r", r=run_id).one())
    verdict = compute_variance(expected, actual, tolerance)
    assert (expected, actual, tolerance) == (D("200"), D("188"), D("3"))
    assert verdict.variance_qty == D("-12.000") and verdict.variance_pct == D("-6.00") and verdict.exceeds_tolerance
    snapshot = dict(run(db_session, "SELECT i.code, l.expected_qty_base FROM production_run_lines l JOIN items i ON i.id = l.item_id WHERE run_id = :r", r=run_id).all())
    assert snapshot == {"FLOUR": D("40.000"), "OIL": D("10.000"), "SPICE": D("1.200"), "POUCH-200": D("200.000"), "CARTON": D("10.000")}


# ================================================================ overheads
def _overhead(s, p, run_id=None, month=None, kind="labour", amount="1200.00", **extra):
    cols = dict(run_id=run_id, period_month=month, kind=kind, amount=amount, created_by=p["user"])
    cols.update(extra)
    run(s, f"INSERT INTO production_overheads ({', '.join(cols)}) VALUES ({', '.join(':' + k for k in cols)})", **cols)
    return scalar(s, "SELECT max(id) FROM production_overheads")


def test_benchmark_overheads_sum_to_1800(db_session, m, p):
    run_id = _started(db_session, m, p)
    db_session.commit()
    _overhead(db_session, p, run_id, kind="labour", amount="1200.00")
    _overhead(db_session, p, run_id, kind="gas_power", amount="600.00")
    db_session.commit()
    assert scalar(db_session, "SELECT sum(amount) FROM production_overheads WHERE run_id = :r", r=run_id) == D("1800.00")


def test_overheads_can_be_recorded_after_the_run_is_completed(db_session, m, p):
    run_id = _started(db_session, m, p)
    db_session.commit()
    _complete(db_session, p, run_id)
    db_session.commit()
    _overhead(db_session, p, run_id, kind="other", amount="250.50")
    db_session.commit()
    assert scalar(db_session, "SELECT amount FROM production_overheads") == D("250.50")


def test_a_shared_overhead_belongs_to_a_month(db_session, m, p):
    _overhead(db_session, p, None, month="2026-11-01", kind="gas_power", amount="900.00")


@pytest.mark.parametrize("target", ["both", "neither"])
def test_overhead_direct_xor_shared(db_session, m, p, target):
    run_id = _started(db_session, m, p)
    db_session.commit()
    with violates(db_session, pge.CheckViolation, "ck_production_overheads_target"):
        _overhead(db_session, p, run_id if target == "both" else None, month="2026-11-01" if target == "both" else None)


@pytest.mark.parametrize("month, ok", [("2026-11-01", True), ("2026-02-01", True), ("2026-11-15", False), ("2026-11-30", False)])
def test_overhead_period_must_be_first_of_month(db_session, m, p, month, ok):
    if ok:
        _overhead(db_session, p, None, month=month)
    else:
        with violates(db_session, pge.CheckViolation, "ck_production_overheads_period_first_of_month"):
            _overhead(db_session, p, None, month=month)


@pytest.mark.parametrize("amount", ["0", "0.00", "-1.00", "-0.01", "NaN"])
def test_overhead_amount_must_be_positive(db_session, m, p, amount):
    with violates(db_session, pge.CheckViolation, "ck_production_overheads_amount_positive"):
        _overhead(db_session, p, None, month="2026-11-01", amount=amount)


def test_overhead_amount_keeps_paise(db_session, m, p):
    _overhead(db_session, p, None, month="2026-11-01", amount="1234.56")
    assert scalar(db_session, "SELECT amount FROM production_overheads") == D("1234.56")


def test_overhead_kind_is_one_of_the_known_kinds(db_session, m, p):
    with violates(db_session, pge.CheckViolation, "ck_production_overheads_kind"):
        _overhead(db_session, p, None, month="2026-11-01", kind="rent")


def test_direct_overhead_needs_an_existing_run(db_session, m, p):
    with violates(db_session, pge.ForeignKeyViolation):
        _overhead(db_session, p, 99999)


def test_overhead_description_cannot_be_blank_when_given(db_session, m, p):
    with violates(db_session, pge.CheckViolation, "ck_production_overheads_description_not_blank"):
        _overhead(db_session, p, None, month="2026-11-01", description="  ")


def test_overhead_can_be_voided_once_with_who_and_when(db_session, m, p):
    oid = _overhead(db_session, p, None, month="2026-11-01")
    db_session.commit()
    run(db_session, "UPDATE production_overheads SET voided_at = now(), voided_by = :u WHERE id = :o", u=p["user"], o=oid)
    db_session.commit()
    with violates(db_session, pge.RestrictViolation, message="production_overheads"):
        run(db_session, "UPDATE production_overheads SET voided_at = now() + interval '1 day', voided_by = :u WHERE id = :o", u=p["user2"], o=oid)


def test_an_overhead_cannot_be_voided_before_it_was_created(db_session, m, p):
    oid = _overhead(db_session, p, None, month="2026-11-01")
    db_session.commit()
    with violates(db_session, pge.CheckViolation, "ck_production_overheads_voided_after_created"):
        run(db_session, "UPDATE production_overheads SET voided_at = created_at - interval '5 days', voided_by = :u WHERE id = :o", u=p["user"], o=oid)


def test_void_time_and_person_come_as_a_pair(db_session, m, p):
    oid = _overhead(db_session, p, None, month="2026-11-01")
    db_session.commit()
    with violates(db_session, pge.CheckViolation, "ck_production_overheads_void_pair"):
        run(db_session, "UPDATE production_overheads SET voided_at = now() WHERE id = :o", o=oid)


@pytest.mark.parametrize("assignment", ["amount = 1.00", "kind = 'other'", "period_month = '2026-12-01'", "description = 'edited'",
                                         "created_by = :u2"])
def test_overhead_values_cannot_be_edited(db_session, m, p, assignment):
    oid = _overhead(db_session, p, None, month="2026-11-01")
    db_session.commit()
    with violates(db_session, pge.RestrictViolation, message="production_overheads"):
        run(db_session, f"UPDATE production_overheads SET {assignment} WHERE id = :o", o=oid, u2=p["user2"])


def test_overheads_cannot_be_deleted(db_session, m, p):
    _overhead(db_session, p, None, month="2026-11-01")
    db_session.commit()
    with violates(db_session, pge.RestrictViolation, message="cannot be deleted"):
        run(db_session, "DELETE FROM production_overheads")
