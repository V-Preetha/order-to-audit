"""StockService core against real PostgreSQL: posting, balances, weighted-average cost, idempotency, lock order."""
import threading
from datetime import date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.domain.costing import InvalidQuantity
from app.services.stock import (CostRequired, InsufficientStock, InvalidPosting, PostingMismatch, Source, UnknownItem, available, average_cost,
                                lock_items, on_hand, post, reserved)
from tests.db.helpers import run, scalar
from tests.db.test_bom_constraints import _item, m  # noqa: F401
from tests.db.test_inventory_constraints import _adj, _approved_line, _invoice_line_of, _pline, _purchase, _reserve, x  # noqa: F401
from tests.db.test_production_constraints import _started, p  # noqa: F401
from tests.db.test_sales_constraints import _benchmark_invoice

D = Decimal
ON = date(2026, 11, 20)


def _receipt(s, x, item, qty, cost, key=None):
    """Purchase header + line (no receipt movement yet), then the receipt through the service."""
    _, (line,) = _purchase(s, x, lines=((item, qty, cost),), receipts=False)
    return post(s, key=key or f"purchase_line:{line}:receipt", item_id=x[item], movement_type="purchase_receipt", qty=D(qty),
                source=Source("purchase_line", line), movement_date=ON, user_id=x["u"], unit_cost=D(cost))


def _issue(s, x, item, qty, run_id, key, kind="production_issue"):
    return post(s, key=key, item_id=x[item], movement_type=kind, qty=D(qty), source=Source("production_run", run_id), movement_date=ON, user_id=x["u"])


def _opening(s, x, item, qty, cost=None):
    adj = _adj(s, x, x[item], qty, cost)
    return post(s, key=f"adjustment:{adj}", item_id=x[item], movement_type="opening", qty=D(qty), source=Source("adjustment", adj),
                movement_date=ON, user_id=x["u"], unit_cost=None if cost is None else D(cost))


def _count(s, x, item, qty, cost=None, key=None):
    run(s, "INSERT INTO stock_adjustments (adjustment_no, kind, item_id, item_kind, qty_delta_base, unit_cost, reason, adjustment_date, created_by) "
           "SELECT 'CNT-' || (SELECT count(*) + 1 FROM stock_adjustments), 'count', id, kind, :q, :c, 'physical count', :d, :u FROM items WHERE id = :i",
        i=x[item], q=qty, c=cost, d=ON, u=x["u"])
    adj = scalar(s, "SELECT max(id) FROM stock_adjustments")
    return post(s, key=key or f"adjustment:{adj}", item_id=x[item], movement_type="count_adjustment", qty=D(qty), source=Source("adjustment", adj),
                movement_date=ON, user_id=x["u"], unit_cost=None if cost is None else D(cost))


def _rows(s):
    return scalar(s, "SELECT count(*) FROM stock_movements")


# ---------------------------------------------------------------- valuation
def test_receipt_then_issue_values_at_wac(db_session, x):
    s = db_session
    _receipt(s, x, "flour", "100", "42")
    _receipt(s, x, "flour", "60", "45")
    run_id = _started(s, x, x)
    issue = _issue(s, x, "flour", "40", run_id, "run:1:flour")
    assert issue.value == D("-1725.00") and issue.qty_base == D("-40.000") and issue.unit_cost == D("43.1250")
    assert average_cost(s, x["flour"]) == D("43.1250")  # an issue at the average leaves the average unchanged
    assert on_hand(s, x["flour"]) == D("120.000")


def test_postings_commit_with_all_deferred_invariants_satisfied(db_session, x):
    s = db_session
    _receipt(s, x, "flour", "100", "42")
    run_id = _started(s, x, x)
    _issue(s, x, "flour", "40", run_id, "run:1:flour")
    s.commit()
    assert scalar(s, "SELECT sum(value) FROM stock_movements") == D("2520.00")


def test_issue_over_on_hand_raises_insufficient_stock_and_posts_nothing(db_session, x):
    s = db_session
    _receipt(s, x, "flour", "100", "42")
    run_id = _started(s, x, x)
    before = _rows(s)
    with pytest.raises(InsufficientStock):
        _issue(s, x, "flour", "100.001", run_id, "run:1:flour")
    assert _rows(s) == before


def test_an_issue_that_empties_the_item_takes_the_exact_remaining_value(db_session, x):
    s = db_session
    _opening(s, x, "flour", "3", "33.3333")  # value 100.00
    run_id = _started(s, x, x)
    values = [_issue(s, x, "flour", "1", run_id, f"run:1:flour:{i}").value for i in range(3)]
    assert values == [D("-33.33"), D("-33.34"), D("-33.33")]  # 66.67 / 2 = 33.335 rounds half-up; the last issue takes what is left
    assert sum(values) == D("-100.00")
    s.commit()  # no valuation residue: the deferred invariant passes
    assert average_cost(s, x["flour"]) is None


def test_scrap_is_valued_like_an_issue(db_session, x):
    s = db_session
    _receipt(s, x, "pouch", "500", "2.50")
    run_id = _started(s, x, x)
    scrap = _issue(s, x, "pouch", "6", run_id, "run:1:pouch:scrap", kind="production_scrap")
    assert scrap.value == D("-15.00") and scrap.qty_base == D("-6.000")


def test_finished_movement_has_no_value(db_session, x):
    s = db_session
    _opening(s, x, "sku", "50")
    run_id = _started(s, x, x)
    out = post(s, key="run:1:out", item_id=x["sku"], movement_type="production_output", qty=D("188"), source=Source("production_run", run_id),
               movement_date=ON, user_id=x["u"])
    assert out.value is None and out.unit_cost is None and out.qty_base == D("188.000")
    assert on_hand(s, x["sku"]) == D("238.000")
    assert average_cost(s, x["sku"]) is None


def test_dispatch_over_on_hand_raises_insufficient_stock(db_session, x):
    s = db_session
    _opening(s, x, "sku", "50")
    _, _, invoice = _benchmark_invoice(s, x, "B", seq=1)  # Distributor B: 120 packets against 50 on hand
    il = _invoice_line_of(s, invoice)
    before = _rows(s)
    with pytest.raises(InsufficientStock):
        post(s, key=f"invoice_line:{il}:dispatch", item_id=x["sku"], movement_type="dispatch", qty=D("120"), source=Source("invoice_line", il),
             movement_date=ON, user_id=x["u"])
    assert _rows(s) == before


# ---------------------------------------------------------------- idempotency of posting
def test_posting_same_key_twice_returns_original_and_adds_no_row(db_session, x):
    s = db_session
    first = _receipt(s, x, "flour", "100", "42", key="k-receipt")
    rows = _rows(s)
    line = first.purchase_line_id
    again = post(s, key="k-receipt", item_id=x["flour"], movement_type="purchase_receipt", qty=D("100"), source=Source("purchase_line", line),
                 movement_date=ON, user_id=x["u"], unit_cost=D("42"))
    assert again.id == first.id and _rows(s) == rows


def test_replaying_an_issue_after_stock_ran_out_still_returns_the_original(db_session, x):
    s = db_session
    _receipt(s, x, "flour", "10", "42")
    run_id = _started(s, x, x)
    first = _issue(s, x, "flour", "10", run_id, "run:1:flour")
    assert _issue(s, x, "flour", "10", run_id, "run:1:flour").id == first.id  # on hand is 0 now; a retry must not fail


@pytest.mark.parametrize("change", ["qty", "item", "source", "date", "type", "cost"])
def test_replay_with_different_content_raises_posting_mismatch(db_session, x, change):
    s = db_session
    first = _receipt(s, x, "flour", "100", "42", key="k")
    _, (other_line,) = _purchase(s, x, lines=(("oil", "30", "140"),), receipts=False)
    args = dict(key="k", item_id=x["flour"], movement_type="purchase_receipt", qty=D("100"), source=Source("purchase_line", first.purchase_line_id),
                movement_date=ON, user_id=x["u"], unit_cost=D("42"))
    args.update({"qty": dict(qty=D("99")), "item": dict(item_id=x["oil"], source=Source("purchase_line", other_line)),
                 "source": dict(source=Source("purchase_line", other_line)), "date": dict(movement_date=date(2026, 11, 21)),
                 "type": dict(movement_type="opening", source=Source("adjustment", 1)), "cost": dict(unit_cost=D("43"))}[change])
    with pytest.raises(PostingMismatch):
        post(s, **args)


# ---------------------------------------------------------------- balances and reservations
def test_available_is_on_hand_minus_active_reservations(db_session, x):
    s = db_session
    _opening(s, x, "sku", "50")
    _, oline = _approved_line(s, x, "20")
    _reserve(s, x, oline, "20")
    assert (on_hand(s, x["sku"]), reserved(s, x["sku"]), available(s, x["sku"])) == (D("50.000"), D("20.000"), D("30.000"))


def test_released_reservations_do_not_reduce_availability(db_session, x):
    s = db_session
    _opening(s, x, "sku", "50")
    _, oline = _approved_line(s, x, "20")
    rid = _reserve(s, x, oline, "20")
    run(s, "UPDATE stock_reservations SET status = 'released' WHERE id = :r", r=rid)
    assert reserved(s, x["sku"]) == D("0") and available(s, x["sku"]) == D("50.000")


def test_balances_of_an_item_with_no_movements_are_zero(db_session, x):
    assert on_hand(db_session, x["oil"]) == D("0") and available(db_session, x["oil"]) == D("0") and average_cost(db_session, x["oil"]) is None


# ---------------------------------------------------------------- count adjustments
def test_positive_count_adjustment_defaults_to_current_wac(db_session, x):
    s = db_session
    _opening(s, x, "flour", "100", "42")
    adj = _count(s, x, "flour", "10")
    assert adj.value == D("420.00") and adj.unit_cost == D("42.0000") and average_cost(s, x["flour"]) == D("42.0000")
    s.commit()


def test_positive_count_adjustment_may_carry_its_own_cost(db_session, x):
    s = db_session
    _opening(s, x, "flour", "100", "42")
    adj = _count(s, x, "flour", "10", cost="50")
    assert adj.value == D("500.00") and average_cost(s, x["flour"]) == D("42.7273")


def test_positive_adjustment_on_empty_stock_requires_cost(db_session, x):
    with pytest.raises(CostRequired):
        _count(db_session, x, "flour", "10")


def test_negative_count_adjustment_is_valued_at_wac(db_session, x):
    s = db_session
    _opening(s, x, "flour", "100", "42")
    adj = _count(s, x, "flour", "-4")
    assert adj.value == D("-168.00") and adj.qty_base == D("-4.000")
    with pytest.raises(InsufficientStock):
        _count(s, x, "flour", "-97")


# ---------------------------------------------------------------- hostile input
HOSTILE = ["NaN", "Infinity", "-Infinity", "0", "-1", "1.0001", "1E+12"]


def _args(s, x, path, qty):
    """Arguments for one posting of each kind, so every path meets the quantity check (none has a second validator)."""
    if path == "issue":
        _receipt(s, x, "flour", "100", "42")
        return dict(item_id=x["flour"], movement_type="production_issue", source=Source("production_run", _started(s, x, x)))
    if path == "output":
        return dict(item_id=x["sku"], movement_type="production_output", source=Source("production_run", _started(s, x, x)))
    if path == "opening":
        return dict(item_id=x["flour"], movement_type="opening", source=Source("adjustment", _adj(s, x, x["flour"], "1", "1")), unit_cost=D("1"))
    _, (line,) = _purchase(s, x, lines=(("flour", "100", "42"),), receipts=False)
    return dict(item_id=x["flour"], movement_type="purchase_receipt", source=Source("purchase_line", line))


@pytest.mark.parametrize("path", ["issue", "output", "opening", "receipt"])
@pytest.mark.parametrize("qty", HOSTILE)
def test_hostile_quantities_raise_invalid_quantity_before_anything_is_written(db_session, x, path, qty):
    s = db_session
    args = _args(s, x, path, qty)
    rows = _rows(s)
    with pytest.raises(InvalidQuantity):
        post(s, key="k", qty=D(qty), movement_date=ON, user_id=x["u"], **args)
    assert _rows(s) == rows


def test_finished_goods_move_in_whole_packets_only(db_session, x):
    s = db_session
    _opening(s, x, "sku", "50")
    run_id = _started(s, x, x)
    with pytest.raises(InvalidQuantity):
        post(s, key="run:1:out", item_id=x["sku"], movement_type="production_output", qty=D("1.5"), source=Source("production_run", run_id),
             movement_date=ON, user_id=x["u"])


@pytest.mark.parametrize("change", [
    dict(key="  "), dict(movement_type="not_a_type"), dict(movement_type="dispatch"), dict(source=Source("adjustment", 1)),
    dict(unit_cost=D("-1")), dict(unit_cost=D("NaN")), dict(unit_cost=D("42.00001")), dict(unit_cost=D("41")), dict(qty=D("99")),
    dict(movement_date="2026-11-20"), dict(movement_date=datetime(2026, 11, 20)), dict(item_id=None),
])
def test_invalid_postings_are_refused_in_the_service(db_session, x, change):
    s = db_session
    _, (line,) = _purchase(s, x, lines=(("flour", "100", "42"),), receipts=False)
    args = dict(key="k", item_id=x["flour"], movement_type="purchase_receipt", qty=D("100"), source=Source("purchase_line", line), movement_date=ON,
                user_id=x["u"], unit_cost=D("42"))
    args.update(change)
    with pytest.raises(InvalidPosting):
        post(s, **args)


def test_cost_is_forbidden_on_finished_goods_and_on_issues(db_session, x):
    s = db_session
    adj = _adj(s, x, x["sku"], "5")
    with pytest.raises(InvalidPosting):
        post(s, key="k1", item_id=x["sku"], movement_type="opening", qty=D("5"), source=Source("adjustment", adj), movement_date=ON, user_id=x["u"],
             unit_cost=D("1"))
    _receipt(s, x, "flour", "100", "42")
    with pytest.raises(InvalidPosting, match="average"):
        post(s, key="k2", item_id=x["flour"], movement_type="production_issue", qty=D("1"), source=Source("production_run", _started(s, x, x)),
             movement_date=ON, user_id=x["u"], unit_cost=D("1"))


def test_a_receipt_takes_its_quantity_and_value_from_the_purchase_line(db_session, x):
    s = db_session
    _, (line,) = _purchase(s, x, lines=(("flour", "100", "42.37"),), receipts=False)
    receipt = post(s, key="k", item_id=x["flour"], movement_type="purchase_receipt", qty=D("100"), source=Source("purchase_line", line), movement_date=ON,
                   user_id=x["u"])  # no cost given: the line decides
    assert receipt.value == D("4237.00") and receipt.unit_cost == D("42.3700")
    s.commit()


def test_a_receipt_in_a_larger_unit_than_the_base_unit_commits(db_session, x):
    """1 kg bought at 42.37 for an item counted in grams: 0.04237 per gram has no 4-decimal cost, so the value must come from the line."""
    s = db_session
    gram_item = _item(s, "SALT-G", "raw", "g")
    pid, _ = _purchase(s, x, lines=())
    line = _pline(s, pid, gram_item, "1", "42.37", unit="kg")
    receipt = post(s, key="k", item_id=gram_item, movement_type="purchase_receipt", qty=D("1000"), source=Source("purchase_line", line), movement_date=ON,
                   user_id=x["u"])
    assert (receipt.qty_base, receipt.value, receipt.unit_cost) == (D("1000.000"), D("42.37"), D("0.0424"))
    s.commit()


@pytest.mark.parametrize("change", [dict(qty=D("99")), dict(unit_cost=D("41")), dict(item_id="oil")])
def test_a_receipt_that_disagrees_with_its_purchase_line_is_refused(db_session, x, change):
    s = db_session
    _, (line,) = _purchase(s, x, lines=(("flour", "100", "42"),), receipts=False)
    args = dict(key="k", item_id=x["flour"], movement_type="purchase_receipt", qty=D("100"), source=Source("purchase_line", line), movement_date=ON,
                user_id=x["u"], unit_cost=D("42"))
    if change.get("item_id") == "oil":
        change = dict(item_id=x["oil"])
    args.update(change)
    with pytest.raises(InvalidPosting):
        post(s, **args)


@pytest.mark.parametrize("item,mtype,source", [("sku", "purchase_receipt", "purchase_line"), ("flour", "dispatch", "invoice_line"),
                                                ("flour", "production_output", "production_run"), ("sku", "production_issue", "production_run")])
def test_a_movement_type_must_suit_the_item_kind(db_session, x, item, mtype, source):
    with pytest.raises(InvalidPosting, match="cannot be posted"):
        post(db_session, key="k", item_id=x[item], movement_type=mtype, qty=D("1"), source=Source(source, 1), movement_date=ON, user_id=x["u"])


def test_a_value_beyond_the_ledger_range_is_refused_in_the_service(db_session, x):
    s = db_session
    adj = _adj(s, x, x["flour"], "1000000", "10000000")
    with pytest.raises(InvalidPosting, match="out of range"):
        post(s, key="k", item_id=x["flour"], movement_type="opening", qty=D("1000000"), source=Source("adjustment", adj), movement_date=ON, user_id=x["u"],
             unit_cost=D("10000000"))


def test_replay_with_a_different_batch_is_a_posting_mismatch(db_session, x):
    s = db_session
    adj = _adj(s, x, x["flour"], "10", "42")
    args = dict(key="k", item_id=x["flour"], movement_type="opening", qty=D("10"), source=Source("adjustment", adj), movement_date=ON, user_id=x["u"],
                unit_cost=D("42"))
    first = post(s, batch_no="B1", **args)
    assert post(s, batch_no="B1", **args).id == first.id
    with pytest.raises(PostingMismatch):
        post(s, batch_no="B2", **args)


def test_an_unknown_item_is_reported_as_such(db_session, x):
    with pytest.raises(UnknownItem):
        post(db_session, key="k", item_id=999999, movement_type="opening", qty=D("1"), source=Source("adjustment", 1), movement_date=ON, user_id=x["u"],
             unit_cost=D("1"))


# ---------------------------------------------------------------- lock order
def test_lock_items_locks_in_ascending_id_order(engine, db_session, x):
    """Session 2 asks for (b, a) while session 1 holds b. In ascending order it takes a first, then waits for b."""
    low, high = sorted((x["flour"], x["oil"]))
    holder, waiter, probe = Session(engine), Session(engine), Session(engine)
    lock_items(holder, [high])
    done = threading.Event()

    def wait_for_both():
        try:
            lock_items(waiter, [high, low, high])  # unsorted, with a duplicate
        finally:
            waiter.rollback()
            done.set()

    t = threading.Thread(target=wait_for_both)
    t.start()
    try:
        for _ in range(200):  # the waiter must appear blocked on the high item
            blocked = probe.execute(text("SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock' AND datname = current_database()")).scalar_one()
            probe.rollback()
            if blocked:
                break
            done.wait(0.05)
        assert blocked == 1
        # ...and by then it must already hold the low item: a NOWAIT attempt on it fails.
        with pytest.raises(DBAPIError) as caught:
            probe.execute(text("SELECT id FROM items WHERE id = :i FOR NO KEY UPDATE NOWAIT"), {"i": low})
        assert "could not obtain lock" in str(caught.value)
    finally:
        probe.rollback()
        holder.rollback()
        t.join(10)
        for sess in (holder, waiter, probe):
            sess.close()
    assert not t.is_alive()


def test_lock_items_with_nothing_to_lock_is_a_no_op(db_session):
    lock_items(db_session, [])


def test_post_waits_for_the_item_lock_before_reading_balances(engine, db_session, x):
    """While another transaction holds the item, post() must block; it proceeds only once the lock is released."""
    holder, poster, probe = Session(engine), Session(engine), Session(engine)
    lock_items(holder, [x["flour"]])
    outcome = []
    returned = threading.Event()  # set when post() itself returns, before the commit

    def receive():
        try:
            _, (line,) = _purchase(poster, x, lines=(("flour", "100", "42"),), receipts=False)  # the source row and its receipt commit together
            post(poster, key="late", item_id=x["flour"], movement_type="purchase_receipt", qty=D("100"), source=Source("purchase_line", line),
                 movement_date=ON, user_id=x["u"], unit_cost=D("42"))
            returned.set()
            poster.commit()
            outcome.append("posted")
        except Exception as exc:  # noqa: BLE001 - reported through the assertion below
            outcome.append(repr(exc))

    t = threading.Thread(target=receive)
    t.start()
    try:
        blocked = 0
        for _ in range(200):
            blocked = probe.execute(text("SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock' AND datname = current_database()")).scalar_one()
            probe.rollback()
            if blocked:
                break
            t.join(0.05)
        assert blocked == 1 and not returned.is_set()  # post() itself is waiting for the lock, not just the commit
    finally:
        holder.rollback()
        t.join(10)
        for sess in (holder, poster, probe):
            sess.close()
    assert outcome == ["posted"] and not t.is_alive()
