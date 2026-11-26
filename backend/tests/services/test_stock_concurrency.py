"""StockService reservations, reversals and races, with real commits on separate connections."""
import threading
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.domain.costing import InvalidQuantity
from app.services.stock import (InsufficientStock, lock_items, InvalidPosting, PostingMismatch, ReservationClosed, ReversalNotAllowed, Source, available, fulfil_reservation,
                                on_hand, post, release_reservation, reserve, reserved, reverse)
from tests.db.helpers import run, scalar
from tests.db.test_bom_constraints import m  # noqa: F401
from tests.db.test_inventory_constraints import _approved_line, _invoice_line_of, _purchase, x  # noqa: F401
from tests.db.test_production_constraints import _started, p  # noqa: F401
from tests.db.test_sales_constraints import _benchmark_invoice, _cancel_with_credit_note
from tests.services.test_stock_core import ON, _issue, _opening, _receipt

D = Decimal
THREADS = 8


def _dispatch(s, x, il, qty, key=None):
    return post(s, key=key or f"invoice_line:{il}:dispatch", item_id=x["sku"], movement_type="dispatch", qty=D(qty), source=Source("invoice_line", il),
                movement_date=ON, user_id=x["u"])


def _reserve(s, x, qty, customer="A"):
    _, line = _approved_line(s, x, qty, customer)
    return reserve(s, order_line_id=line, item_id=x["sku"], qty=D(qty), user_id=x["ada"])


# ---------------------------------------------------------------- reservations
def test_reserve_takes_availability_not_stock(db_session, x):
    s = db_session
    _opening(s, x, "sku", "88")
    r = _reserve(s, x, "60")
    assert (r.status, r.qty_base) == ("active", D("60.000"))
    assert (on_hand(s, x["sku"]), reserved(s, x["sku"]), available(s, x["sku"])) == (D("88.000"), D("60.000"), D("28.000"))
    s.commit()


def test_reserving_more_than_available_raises_and_leaves_nothing(db_session, x):
    s = db_session
    _opening(s, x, "sku", "88")
    _reserve(s, x, "60")
    with pytest.raises(InsufficientStock):
        _reserve(s, x, "29", customer="B")
    assert reserved(s, x["sku"]) == D("60.000")


def test_reserving_exactly_what_is_available_succeeds(db_session, x):
    s = db_session
    _opening(s, x, "sku", "88")
    _reserve(s, x, "88")
    assert available(s, x["sku"]) == D("0")
    s.commit()


def test_a_line_cannot_be_reserved_twice(db_session, x):
    s = db_session
    _opening(s, x, "sku", "88")
    _, line = _approved_line(s, x, "20")
    reserve(s, order_line_id=line, item_id=x["sku"], qty=D("20"), user_id=x["ada"])
    with pytest.raises(InvalidPosting):
        reserve(s, order_line_id=line, item_id=x["sku"], qty=D("20"), user_id=x["ada"])


@pytest.mark.parametrize("qty", ["0", "-5", "NaN", "Infinity", "2.5"])
def test_hostile_reservation_quantities_are_refused(db_session, x, qty):
    s = db_session
    _opening(s, x, "sku", "88")
    _, line = _approved_line(s, x, "20")
    with pytest.raises(InvalidQuantity):
        reserve(s, order_line_id=line, item_id=x["sku"], qty=D(qty), user_id=x["ada"])


def test_release_returns_the_quantity_and_cannot_be_repeated(db_session, x):
    s = db_session
    _opening(s, x, "sku", "88")
    r = _reserve(s, x, "60")
    release_reservation(s, r.id)
    assert available(s, x["sku"]) == D("88.000")
    assert scalar(s, "SELECT status FROM stock_reservations WHERE id = :r", r=r.id) == "released"
    with pytest.raises(ReservationClosed):
        release_reservation(s, r.id)
    with pytest.raises(ReservationClosed):
        fulfil_reservation(s, r.id)
    s.commit()


def test_dispatching_a_reserved_order_fulfils_it_and_commits(db_session, x):
    s = db_session
    _opening(s, x, "sku", "238")
    order, order_line, invoice = _benchmark_invoice(s, x, "A", seq=1)
    il = _invoice_line_of(s, invoice)
    r = reserve(s, order_line_id=order_line, item_id=x["sku"], qty=D("150"), user_id=x["ada"])
    _dispatch(s, x, il, "150")  # its own 150 are reserved: dispatching them is allowed and consumes the reservation
    assert scalar(s, "SELECT status FROM stock_reservations WHERE id = :r", r=r.id) == "fulfilled"
    s.commit()
    assert (on_hand(s, x["sku"]), reserved(s, x["sku"])) == (D("88.000"), D("0"))


def test_a_dispatch_commits_when_every_remaining_packet_is_promised_to_other_orders(db_session, x):
    s = db_session
    _opening(s, x, "sku", "238")
    _, order_line, invoice = _benchmark_invoice(s, x, "A", seq=1)
    reserve(s, order_line_id=order_line, item_id=x["sku"], qty=D("150"), user_id=x["ada"])
    _reserve(s, x, "88", customer="B")  # 238 on hand, 238 reserved: no free stock at all
    _dispatch(s, x, _invoice_line_of(s, invoice), "150")
    s.commit()  # without the automatic fulfilment the commit-time rule (reserved <= on hand) would refuse this
    assert (on_hand(s, x["sku"]), reserved(s, x["sku"])) == (D("88.000"), D("88.000"))


def test_dispatch_cannot_take_stock_reserved_for_another_order(db_session, x):
    s = db_session
    _opening(s, x, "sku", "238")
    _reserve(s, x, "100", customer="B")  # 138 are free
    _, order_line, invoice = _benchmark_invoice(s, x, "A", seq=1)
    il = _invoice_line_of(s, invoice)
    with pytest.raises(InsufficientStock):
        _dispatch(s, x, il, "150")  # no reservation of its own, and 150 > 138 free


def test_adjustment_cannot_drop_below_reserved(db_session, x):
    s = db_session
    _opening(s, x, "sku", "88")
    _reserve(s, x, "60")
    run(s, "INSERT INTO stock_adjustments (adjustment_no, kind, item_id, item_kind, qty_delta_base, reason, adjustment_date, created_by) "
           "SELECT 'CNT-1', 'count', id, kind, -29, 'breakage', :d, :u FROM items WHERE id = :i", i=x["sku"], d=ON, u=x["u"])
    adj = scalar(s, "SELECT max(id) FROM stock_adjustments")
    with pytest.raises(InsufficientStock):  # 88 - 29 = 59 < 60 reserved
        post(s, key="adj-1", item_id=x["sku"], movement_type="count_adjustment", qty=D("-29"), source=Source("adjustment", adj), movement_date=ON,
             user_id=x["u"])


# ---------------------------------------------------------------- reversal
def test_reverse_dispatch_restocks_without_value(db_session, x):
    s = db_session
    _opening(s, x, "sku", "238")
    _, _, invoice = _benchmark_invoice(s, x, "A", seq=1)
    il = _invoice_line_of(s, invoice)
    original = _dispatch(s, x, il, "150")
    s.commit()
    _cancel_with_credit_note(s, x, invoice, restock=True)
    rev = reverse(s, original.id, key="rev-dispatch", user_id=x["ada"], require_latest=False)
    s.commit()
    assert (rev.qty_base, rev.value, rev.reverses_movement_id) == (D("150.000"), None, original.id)
    assert on_hand(s, x["sku"]) == D("238.000")


def test_reverse_purchase_allowed_when_latest(db_session, x):
    s = db_session
    _receipt(s, x, "flour", "100", "42")
    s.commit()
    first = _receipt(s, x, "flour", "60", "45")
    s.commit()
    pid = scalar(s, "SELECT purchase_id FROM purchase_lines WHERE id = :l", l=first.purchase_line_id)
    run(s, "UPDATE purchases SET status = 'voided', voided_by = :u, void_reason = 'wrong supplier' WHERE id = :p", u=x["u"], p=pid)
    rev = reverse(s, first.id, key="rev-1", user_id=x["u"], require_latest=True)
    s.commit()
    assert rev.qty_base == D("-60.000") and rev.value == D("-2700.00") and rev.unit_cost == first.unit_cost
    assert (on_hand(s, x["flour"]), scalar(s, "SELECT sum(value) FROM stock_movements WHERE item_id = :i", i=x["flour"])) == (D("100.000"), D("4200.00"))


def test_reverse_purchase_refused_when_later_movement_exists(db_session, x):
    s = db_session
    receipt = _receipt(s, x, "flour", "100", "42")
    run_id = _started(s, x, x)
    _issue(s, x, "flour", "40", run_id, "run:1:flour")  # the benchmark: the run issued flour after the purchase
    s.commit()
    before = scalar(s, "SELECT count(*) FROM stock_movements")
    with pytest.raises(ReversalNotAllowed, match="later movement"):
        reverse(s, receipt.id, key="rev-1", user_id=x["u"], require_latest=True)
    assert scalar(s, "SELECT count(*) FROM stock_movements") == before


def test_a_purchase_receipt_is_always_checked_for_latest(db_session, x):
    s = db_session
    receipt = _receipt(s, x, "flour", "100", "42")
    run_id = _started(s, x, x)
    _issue(s, x, "flour", "40", run_id, "run:1:flour")
    s.commit()
    with pytest.raises(ReversalNotAllowed):
        reverse(s, receipt.id, key="rev-1", user_id=x["u"], require_latest=False)  # asking for no check does not switch the rule off


def test_reverse_twice_rejected(db_session, x):
    s = db_session
    _opening(s, x, "sku", "238")
    _, _, invoice = _benchmark_invoice(s, x, "A", seq=1)
    original = _dispatch(s, x, _invoice_line_of(s, invoice), "150")
    s.commit()
    _cancel_with_credit_note(s, x, invoice, restock=True)
    reverse(s, original.id, key="rev-a", user_id=x["ada"], require_latest=False)
    with pytest.raises(ReversalNotAllowed, match="already reversed"):
        reverse(s, original.id, key="rev-b", user_id=x["ada"], require_latest=False)


def test_a_reversal_cannot_itself_be_reversed(db_session, x):
    s = db_session
    _opening(s, x, "sku", "238")
    _, _, invoice = _benchmark_invoice(s, x, "A", seq=1)
    original = _dispatch(s, x, _invoice_line_of(s, invoice), "150")
    s.commit()
    _cancel_with_credit_note(s, x, invoice, restock=True)
    rev = reverse(s, original.id, key="rev-a", user_id=x["ada"], require_latest=False)
    with pytest.raises(ReversalNotAllowed, match="itself a reversal"):
        reverse(s, rev.id, key="rev-of-rev", user_id=x["ada"], require_latest=False)


@pytest.mark.parametrize("kind", ["opening", "issue"])
def test_only_dispatches_and_receipts_can_be_reversed(db_session, x, kind):
    s = db_session
    if kind == "opening":
        target = _opening(s, x, "flour", "100", "42")
    else:
        _receipt(s, x, "flour", "100", "42")
        target = _issue(s, x, "flour", "10", _started(s, x, x), "run:1:flour")
    s.commit()
    with pytest.raises(ReversalNotAllowed, match="cannot be reversed"):
        reverse(s, target.id, key="rev", user_id=x["u"], require_latest=False)


def test_reversing_a_missing_movement_is_an_error(db_session, x):
    with pytest.raises(ReversalNotAllowed, match="does not exist"):
        reverse(db_session, 999999, key="rev", user_id=x["u"], require_latest=False)


def test_reverse_with_a_used_key_returns_the_stored_reversal_or_refuses_a_different_one(db_session, x):
    s = db_session
    _opening(s, x, "sku", "238")
    _, _, invoice = _benchmark_invoice(s, x, "A", seq=1)
    original = _dispatch(s, x, _invoice_line_of(s, invoice), "150")
    s.commit()
    _cancel_with_credit_note(s, x, invoice, restock=True)
    rev = reverse(s, original.id, key="rev-a", user_id=x["ada"], require_latest=False)
    assert reverse(s, original.id, key="rev-a", user_id=x["ada"], require_latest=False).id == rev.id  # a retry of the same request
    with pytest.raises(PostingMismatch):
        reverse(s, rev.id, key="rev-a", user_id=x["ada"], require_latest=False)  # same key, different target


def test_reverse_is_exact_mirror(db_session, x):
    s = db_session
    receipt = _receipt(s, x, "oil", "30", "140.07")
    s.commit()
    pid = scalar(s, "SELECT purchase_id FROM purchase_lines WHERE id = :l", l=receipt.purchase_line_id)
    run(s, "UPDATE purchases SET status = 'voided', voided_by = :u, void_reason = 'wrong supplier' WHERE id = :p", u=x["u"], p=pid)
    rev = reverse(s, receipt.id, key="rev", user_id=x["u"], require_latest=True)
    assert (rev.item_id, rev.qty_base, rev.value, rev.unit_cost, rev.purchase_line_id) == (receipt.item_id, -receipt.qty_base, -receipt.value, receipt.unit_cost,
                                                                                         receipt.purchase_line_id)
    s.commit()
    assert on_hand(s, x["oil"]) == D("0") and scalar(s, "SELECT coalesce(sum(value), 0) FROM stock_movements WHERE item_id = :i", i=x["oil"]) == D("0")


# ---------------------------------------------------------------- races
def _race(engine, workers, *, ready=None):
    """Run `workers` (callables taking a Session) at once, each in its own session; a worker commits itself. Returns their results or exceptions."""
    barrier = threading.Barrier(len(workers))
    results = [None] * len(workers)

    def go(i, work):
        with Session(engine, expire_on_commit=False) as sess:  # results stay readable after the commit
            sess.execute(text("SET lock_timeout = '20s'"))
            barrier.wait(10)
            try:
                results[i] = work(sess)
                sess.commit()
            except Exception as exc:  # noqa: BLE001 - classified by the callers
                sess.rollback()
                results[i] = exc

    threads = [threading.Thread(target=go, args=(i, w)) for i, w in enumerate(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert not any(t.is_alive() for t in threads)
    return results


@pytest.mark.parametrize("attempt", range(3))
def test_concurrent_reservations_never_oversell(engine, db_session, x, attempt):
    s = db_session
    _opening(s, x, "sku", "88")
    lines = [_approved_line(s, x, "20")[1] for _ in range(THREADS)]
    s.commit()
    results = _race(engine, [lambda sess, ln=ln: reserve(sess, order_line_id=ln, item_id=x["sku"], qty=D("20"), user_id=x["ada"]) for ln in lines])
    won = [r for r in results if not isinstance(r, Exception)]
    lost = [r for r in results if isinstance(r, Exception)]
    assert len(won) == 4 and len(lost) == THREADS - 4, results  # 88 // 20
    assert all(isinstance(e, InsufficientStock) for e in lost), lost
    assert reserved(s, x["sku"]) == D("80.000") and reserved(s, x["sku"]) <= on_hand(s, x["sku"])


@pytest.mark.parametrize("attempt", range(3))
def test_concurrent_issues_never_go_negative(engine, db_session, x, attempt):
    s = db_session
    _receipt(s, x, "flour", "100", "42")
    runs = [_started(s, x, x) for _ in range(THREADS)]
    s.commit()
    results = _race(engine, [lambda sess, r=r: _issue(sess, x, "flour", "20", r, f"run:{r}:flour") for r in runs])
    won = [r for r in results if not isinstance(r, Exception)]
    lost = [r for r in results if isinstance(r, Exception)]
    assert len(won) == 5 and all(isinstance(e, InsufficientStock) for e in lost), results
    assert on_hand(s, x["flour"]) == D("0")
    assert scalar(s, "SELECT coalesce(sum(value), 0) FROM stock_movements WHERE item_id = :i", i=x["flour"]) == D("0")  # no residue
    assert sorted(w.value for w in won) == [D("-840.00")] * 5


@pytest.mark.parametrize("attempt", range(5))
def test_receipt_and_issue_serialise_for_wac(engine, db_session, x, attempt):
    s = db_session
    _opening(s, x, "flour", "100", "40")
    run_id = _started(s, x, x)
    s.commit()

    def receive(sess):
        _, (line,) = _purchase(sess, x, lines=(("flour", "100", "50"),), receipts=False)  # source row and receipt commit together
        return post(sess, key="late-receipt", item_id=x["flour"], movement_type="purchase_receipt", qty=D("100"), source=Source("purchase_line", line),
                    movement_date=ON, user_id=x["u"], unit_cost=D("50"))

    receipt, issue = _race(engine, [receive, lambda sess: _issue(sess, x, "flour", "100", run_id, "run:1:flour")])
    assert not isinstance(receipt, Exception) and not isinstance(issue, Exception), (receipt, issue)
    # Whichever came first in the ledger fixes the issue's value: the issue sees exactly the movements before it.
    expected = D("-4500.00") if receipt.id < issue.id else D("-4000.00")
    assert issue.value == expected
    assert on_hand(s, x["flour"]) == D("100.000")
    assert scalar(s, "SELECT sum(value) FROM stock_movements WHERE item_id = :i", i=x["flour"]) == D("9000.00") + expected


def test_a_second_dispatch_of_the_same_line_under_a_new_key_is_a_posting_mismatch(db_session, x):
    s = db_session
    _opening(s, x, "sku", "400")
    _, _, invoice = _benchmark_invoice(s, x, "A", seq=1)
    il = _invoice_line_of(s, invoice)
    _dispatch(s, x, il, "150")
    with pytest.raises(PostingMismatch, match="already posted"):
        _dispatch(s, x, il, "150", key="another-key")
    assert on_hand(s, x["sku"]) == D("250.000")  # the failed attempt left the transaction usable and wrote nothing
    s.commit()


def test_a_same_key_posting_racing_from_another_transaction_is_detected(engine, db_session, x):
    """Two transactions post one key for different items. The second blocks on the key, then must refuse, not return the other item's row."""
    holder, other = Session(engine), Session(engine)
    try:
        adj0 = _adjust(holder, x, "flour", "10", "42")
        post(holder, key="shared-key", item_id=x["flour"], movement_type="opening", qty=D("10"), source=Source("adjustment", adj0), movement_date=ON,
             user_id=x["u"], unit_cost=D("42"))  # the holder owns the key, uncommitted
        results, returned = [], threading.Event()

        def second():
            try:
                adj = _adjust(other, x, "oil", "5", "140")
                results.append(post(other, key="shared-key", item_id=x["oil"], movement_type="opening", qty=D("5"), source=Source("adjustment", adj),
                                    movement_date=ON, user_id=x["u"], unit_cost=D("140")))
            except Exception as exc:  # noqa: BLE001
                results.append(exc)
            finally:
                returned.set()
                other.rollback()

        t = threading.Thread(target=second)
        t.start()
        for _ in range(100):
            if holder.execute(text("SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock' AND datname = current_database()")).scalar_one():
                break
            returned.wait(0.05)
        assert not returned.is_set()  # blocked on the key
        holder.commit()
        t.join(10)
    finally:
        holder.close()
        other.close()
    assert len(results) == 1 and isinstance(results[0], PostingMismatch), results


def _adjust(s, x, item, qty, cost):
    from tests.db.test_inventory_constraints import _adj  # noqa: PLC0415
    return _adj(s, x, x[item], qty, cost)


def test_the_balance_is_read_after_the_item_lock_is_won(engine, db_session, x):
    """An issue queued behind an uncommitted receipt must be valued with that receipt in the pool (-4500), not without it (-4000)."""
    s = db_session
    _opening(s, x, "flour", "100", "40")
    run_id = _started(s, x, x)
    s.commit()
    holder, issuer, probe = Session(engine), Session(engine, expire_on_commit=False), Session(engine)
    out, returned = [], threading.Event()
    try:
        _receipt(holder, x, "flour", "100", "50")  # takes the item lock, stays uncommitted

        def issue():
            try:
                out.append(_issue(issuer, x, "flour", "100", run_id, "run:1:flour"))
                returned.set()
                issuer.commit()
            except Exception as exc:  # noqa: BLE001
                returned.set()
                out.append(exc)

        t = threading.Thread(target=issue)
        t.start()
        for _ in range(100):
            if probe.execute(text("SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock' AND datname = current_database()")).scalar_one():
                break
            probe.rollback()
            returned.wait(0.05)
        assert not returned.is_set()  # the issue is queued behind the receipt
        holder.commit()
        t.join(10)
    finally:
        for sess in (holder, issuer, probe):
            sess.close()
    assert len(out) == 1 and not isinstance(out[0], Exception), out
    assert out[0].value == D("-4500.00")


def test_stock_cannot_be_posted_outside_read_committed(engine, x):
    with engine.connect().execution_options(isolation_level="REPEATABLE READ") as conn, Session(bind=conn) as sess:
        with pytest.raises(InvalidPosting, match="READ COMMITTED"):
            lock_items(sess, [x["flour"]])
