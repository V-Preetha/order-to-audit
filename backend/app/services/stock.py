"""StockService: the only way stock moves (design 6, 9).

Rules this module enforces, in the order a posting meets them:

1. **Lock first, read second.** ``post`` locks the item row, and only then reads balances in a *later*
   statement, so a concurrent transaction's commit is visible (READ COMMITTED, design 6). Callers that
   touch several items call ``lock_items`` once, in ascending id order, before posting.
2. **Idempotent by posting key.** A key that was already posted returns the stored movement, even if
   the stock has since run out; the same key with different content is a ``PostingMismatch``.
3. **Costs come from the ledger.** Issues, scrap and negative counts take the weighted average at that
   instant (``domain.costing.wac_issue_value``); receipts, openings and costed counts take the entered
   cost. Finished goods carry no value. Nothing here is a constant.
4. **Malformed input fails with a domain error.** Quantities, costs, movement dates, the movement type
   against the item kind, and the one-shot rule (a source is posted once) are checked here and leave no
   trace. What the service does *not* re-check is the source document itself (an adjustment's quantity, an
   invoice's status, a run's state): the services that create those rows do so in the same transaction with
   matching values, and the database refuses any mismatch at COMMIT as the second net.

Sign convention of ``qty`` in ``post``: a positive magnitude for every movement type (the service
applies the sign: issue, scrap and dispatch reduce stock); ``count_adjustment`` takes a signed quantity.
"""
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Context, Decimal
from typing import Literal

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.domain.costing import MAX_AMOUNT, MAX_QUANTITY, InsufficientStock, InvalidQuantity, wac_issue_value
from app.models.inventory import StockMovement, StockReservation

__all__ = ["CostRequired", "InsufficientStock", "InvalidPosting", "PostingMismatch", "ReservationClosed", "ReversalNotAllowed", "Source", "UnknownItem",
           "available", "average_cost", "fulfil_reservation", "lock_items", "on_hand", "post", "release_reservation", "reserve", "reserved", "reverse"]

_PAISE = Decimal("0.01")
_COST = Decimal("0.0001")
_QTY = Decimal("0.001")
_MAX_COST = Decimal("9999999999.9999")
_EXACT = Context(prec=60)  # far above NUMERIC(14, 4) work; the division is done last and rounded once

_SOURCE_COLUMN = {"purchase_line": "purchase_line_id", "production_run": "production_run_id", "invoice_line": "invoice_line_id", "adjustment": "adjustment_id"}
_TYPE_SOURCE = {"opening": "adjustment", "count_adjustment": "adjustment", "purchase_receipt": "purchase_line", "production_issue": "production_run",
                "production_scrap": "production_run", "production_output": "production_run", "dispatch": "invoice_line"}
_REDUCES_STOCK = {"production_issue", "production_scrap", "dispatch"}


class PostingMismatch(Exception):
    """The posting key exists but was posted with different content."""


class InvalidPosting(ValueError):
    """The request itself is malformed: bad key, type, source or cost."""


class CostRequired(InvalidPosting):
    """A valued movement that adds stock needs a cost and the ledger has none to default to."""


class ReservationClosed(Exception):
    """The reservation is not active (already released or fulfilled)."""


class ReversalNotAllowed(Exception):
    """The movement cannot be reversed (design 9, item 6)."""


class UnknownItem(LookupError):
    """No item with that id."""


@dataclass(frozen=True)
class Source:
    kind: Literal["purchase_line", "production_run", "invoice_line", "adjustment"]
    id: int


# ------------------------------------------------------------------ locking and balances
def lock_items(s: Session, item_ids: Iterable[int]) -> None:
    """Lock the item rows, ascending id, in one statement (the per-item mutex of design 6).

    FOR NO KEY UPDATE still excludes every other stock writer (they take the same lock) but, unlike
    FOR UPDATE, does not block unrelated transactions that only reference the item (foreign-key checks).
    """
    ids = sorted({int(i) for i in item_ids})
    if ids:
        isolation = s.execute(text("SELECT current_setting('transaction_isolation')")).scalar_one()
        if isolation != "read committed":  # a later read would use a snapshot taken before the lock wait (design 6, rule 2)
            raise InvalidPosting(f"stock can only be posted in a READ COMMITTED transaction (this one is {isolation}).")
        s.execute(text("SELECT id FROM items WHERE id = ANY(:ids) ORDER BY id FOR NO KEY UPDATE"), {"ids": ids}).all()


def _balance(s: Session, item_id: int) -> tuple[Decimal, Decimal]:
    row = s.execute(text("SELECT coalesce(sum(qty_base), 0), coalesce(sum(value), 0) FROM stock_movements WHERE item_id = :i"), {"i": item_id}).one()
    return Decimal(row[0]), Decimal(row[1])


def on_hand(s: Session, item_id: int) -> Decimal:
    return _balance(s, item_id)[0]


def reserved(s: Session, item_id: int) -> Decimal:
    return Decimal(s.execute(text("SELECT coalesce(sum(qty_base), 0) FROM stock_reservations WHERE item_id = :i AND status = 'active'"), {"i": item_id}).scalar_one())


def available(s: Session, item_id: int) -> Decimal:
    return on_hand(s, item_id) - reserved(s, item_id)


def average_cost(s: Session, item_id: int) -> Decimal | None:
    """Weighted average cost per base unit, or None when nothing is on hand (finished goods are never valued)."""
    if s.execute(text("SELECT kind FROM items WHERE id = :i"), {"i": item_id}).scalar() in (None, "finished"):
        return None
    qty, value = _balance(s, item_id)
    if qty <= 0:
        return None
    return _EXACT.divide(value, qty).quantize(_COST, rounding=ROUND_HALF_UP)


# ------------------------------------------------------------------ validation
def _finite(value: Decimal, what: str, error: type[Exception]) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise error(f"{what} must be a finite Decimal, got {value!r}.")
    return value


def _quantity(qty: Decimal, movement_type: str, whole: bool) -> Decimal:
    qty = _finite(qty, "quantity", InvalidQuantity)
    if qty == 0:
        raise InvalidQuantity("quantity cannot be zero.")
    if movement_type != "count_adjustment" and qty < 0:
        raise InvalidQuantity(f"quantity is a positive magnitude for {movement_type}, got {qty}.")
    if abs(qty) > MAX_QUANTITY:
        raise InvalidQuantity(f"quantity cannot exceed {MAX_QUANTITY}, got {qty}.")
    if qty != qty.quantize(_QTY):
        raise InvalidQuantity(f"quantity has more than 3 decimal places: {qty}.")
    if whole and qty != qty.to_integral_value():
        raise InvalidQuantity(f"finished goods move in whole packets, got {qty}.")
    return qty


def _cost(unit_cost: Decimal | None) -> Decimal | None:
    if unit_cost is None:
        return None
    unit_cost = _finite(unit_cost, "unit cost", InvalidPosting)
    if unit_cost < 0 or unit_cost > _MAX_COST:
        raise InvalidPosting(f"unit cost must be between 0 and {_MAX_COST}, got {unit_cost}.")
    if unit_cost != unit_cost.quantize(_COST):
        raise InvalidPosting(f"unit cost has more than 4 decimal places: {unit_cost}.")
    return unit_cost


def _round(value: Decimal) -> Decimal:
    return value.quantize(_PAISE, rounding=ROUND_HALF_UP)


# ------------------------------------------------------------------ posting
def _same(row: StockMovement, want: dict) -> bool:
    return all(getattr(row, k) == v for k, v in want.items())


def post(s: Session, *, key: str, item_id: int, movement_type: str, qty: Decimal, source: Source, movement_date: date, user_id: int,
         unit_cost: Decimal | None = None, batch_no: str | None = None) -> StockMovement:
    """Append one movement to the ledger (see the module doc for the rules). Never updates or deletes."""
    if not isinstance(key, str) or not key.strip():
        raise InvalidPosting("posting key cannot be blank.")
    if movement_type not in _TYPE_SOURCE:
        raise InvalidPosting(f"unknown movement type {movement_type!r}.")
    if source.kind != _TYPE_SOURCE[movement_type]:
        raise InvalidPosting(f"{movement_type} must come from a {_TYPE_SOURCE[movement_type]}, not a {source.kind}.")
    if not isinstance(item_id, int) or isinstance(item_id, bool) or not isinstance(user_id, int) or not isinstance(source.id, int):
        raise InvalidPosting("item, user and source ids must be integers.")
    if isinstance(movement_date, datetime) or not isinstance(movement_date, date):
        raise InvalidPosting(f"movement date must be a date, got {movement_date!r}.")
    unit_cost = _cost(unit_cost)

    lock_items(s, [item_id])  # lock first ...
    item = s.execute(text("SELECT kind FROM items WHERE id = :i"), {"i": item_id}).first()
    if item is None:
        raise UnknownItem(f"item {item_id} does not exist.")
    kind = item.kind
    if movement_type not in ("opening", "count_adjustment") and (kind == "finished") != (movement_type in ("production_output", "dispatch")):
        raise InvalidPosting(f"{movement_type} cannot be posted for a {kind} item.")
    qty = _quantity(qty, movement_type, whole=kind == "finished")
    signed = -qty if movement_type in _REDUCES_STOCK else qty
    column = _SOURCE_COLUMN[source.kind]
    if unit_cost is not None and (kind == "finished" or (signed < 0 and movement_type != "count_adjustment") or (movement_type == "count_adjustment" and signed < 0)):
        raise InvalidPosting("a cost can only be given for movements that add valued stock; issues and dispatches take the average.")

    expected = {"item_id": item_id, "movement_type": movement_type, "qty_base": signed, "movement_date": movement_date, "batch_no": batch_no, column: source.id}
    existing = s.scalars(select(StockMovement).where(StockMovement.posting_key == key)).first()
    if existing is not None:
        return _replay(existing, expected, unit_cost, key)

    qty_on_hand, stock_value = _balance(s, item_id)  # ... read second, in a later statement
    value: Decimal | None
    if movement_type == "purchase_receipt":  # quantity and value come from the purchase line itself: its amount is in the purchase unit
        line = s.execute(text("SELECT item_id, qty_base, amount FROM purchase_lines WHERE id = :l"), {"l": source.id}).first()
        if line is None or line.item_id != item_id:
            raise InvalidPosting(f"purchase line {source.id} does not exist for item {item_id}.")
        if signed != line.qty_base:
            raise InvalidPosting(f"receipt quantity {signed} differs from purchase line {source.id} ({line.qty_base}).")
        if unit_cost is not None and _round(_EXACT.multiply(signed, unit_cost)) != line.amount:
            raise InvalidPosting(f"unit cost {unit_cost} does not give the purchase line amount {line.amount}.")
        value, stored_cost = Decimal(line.amount), None
    elif kind == "finished":
        if signed < 0:  # stock promised to other orders is not free; a dispatch may use its own order's reservation
            free = qty_on_hand - reserved(s, item_id) + (_own_reservation(s, source.id) if movement_type == "dispatch" else Decimal(0))
            if -signed > free:
                raise InsufficientStock(f"Cannot take {-signed} of item {item_id}: {qty_on_hand} on hand, {qty_on_hand - free} reserved for other orders.")
        value, stored_cost = None, None
    elif signed < 0:
        value = -wac_issue_value(-signed, qty_on_hand, stock_value)  # raises InsufficientStock past on-hand
        stored_cost = None
    elif unit_cost is not None:
        value = _round(_EXACT.multiply(signed, unit_cost))
        stored_cost = None
    elif movement_type == "count_adjustment" and qty_on_hand > 0:
        value = _round(_EXACT.divide(_EXACT.multiply(signed, stock_value), qty_on_hand))  # at the current average
        stored_cost = None
    else:
        raise CostRequired(f"{movement_type} of item {item_id} needs a unit cost: there is no stock to take an average from.")
    if value is not None:
        if abs(value) > MAX_AMOUNT:
            raise InvalidPosting(f"value {value} is out of range.")
        stored_cost = _EXACT.divide(value, signed).quantize(_COST, rounding=ROUND_HALF_UP)  # the ledger rule: unit_cost = round(value / qty, 4)

    try:
        with s.begin_nested():  # a failed one-shot insert must not poison the caller's transaction
            inserted = _insert(s, column, key, item_id, kind, movement_date, movement_type, signed, stored_cost, value, batch_no, source, user_id)
    except IntegrityError as exc:
        if (getattr(exc.orig, "diag", None) is not None and (exc.orig.diag.constraint_name or "").startswith("uq_stock_movements_")):
            raise PostingMismatch(f"{source.kind} {source.id} was already posted as a {movement_type} under another key.") from exc
        raise
    if inserted is None:  # the same key landed from another transaction (on another item) after our check
        existing = s.scalars(select(StockMovement).where(StockMovement.posting_key == key)).one()
        return _replay(existing, expected, unit_cost, key)
    if movement_type == "dispatch":  # the dispatch consumes the reservation it was allowed to count as free
        s.execute(text("UPDATE stock_reservations r SET status = 'fulfilled' FROM invoice_lines il "
                       "WHERE il.id = :i AND r.order_line_id = il.order_line_id AND r.status = 'active'"), {"i": source.id})
    return s.scalars(select(StockMovement).where(StockMovement.id == inserted.id).execution_options(populate_existing=True)).one()


def _insert(s, column, key, item_id, kind, movement_date, movement_type, signed, stored_cost, value, batch_no, source, user_id):
    return s.execute(
        text(f"INSERT INTO stock_movements (posting_key, item_id, item_kind, movement_date, movement_type, qty_base, unit_cost, value, batch_no, {column}, created_by) "
             "VALUES (:key, :item, :kind, :on, :type, :qty, :cost, :value, :batch, :src, :user) ON CONFLICT (posting_key) DO NOTHING RETURNING id"),
        {"key": key, "item": item_id, "kind": kind, "on": movement_date, "type": movement_type, "qty": signed, "cost": stored_cost, "value": value,
         "batch": batch_no, "src": source.id, "user": user_id}).first()


def _replay(existing: StockMovement, expected: dict, unit_cost: Decimal | None, key: str) -> StockMovement:
    same_cost = unit_cost is None or existing.value is None or existing.value == _round(existing.qty_base * unit_cost)
    if not _same(existing, expected) or not same_cost:
        raise PostingMismatch(f"posting key {key!r} was already used for a different movement (id {existing.id}).")
    return existing


def _own_reservation(s: Session, invoice_line_id: int) -> Decimal:
    """Active reservation held for the order line this invoice line bills (its dispatch may consume it)."""
    return Decimal(s.execute(
        text("SELECT coalesce(sum(r.qty_base), 0) FROM stock_reservations r JOIN invoice_lines il ON il.order_line_id = r.order_line_id "
             "WHERE il.id = :i AND r.status = 'active'"), {"i": invoice_line_id}).scalar_one())


# ------------------------------------------------------------------ reservations
def reserve(s: Session, *, order_line_id: int, item_id: int, qty: Decimal, user_id: int) -> StockReservation:
    """Promise ``qty`` packets of finished goods to an order line, if that much is available (on hand minus active reservations)."""
    lock_items(s, [item_id])
    kind = s.execute(text("SELECT kind FROM items WHERE id = :i"), {"i": item_id}).scalar()
    if kind is None:
        raise UnknownItem(f"item {item_id} does not exist.")
    if kind != "finished":
        raise InvalidPosting("only finished goods are reserved.")
    qty = _quantity(qty, "reservation", whole=True)
    if s.execute(text("SELECT 1 FROM stock_reservations WHERE order_line_id = :o"), {"o": order_line_id}).first():
        raise InvalidPosting(f"order line {order_line_id} already has a reservation.")
    free = on_hand(s, item_id) - reserved(s, item_id)  # read after the lock, in its own statement
    if qty > free:
        raise InsufficientStock(f"Cannot reserve {qty} of item {item_id}: only {free} available.")
    rid = s.execute(text("INSERT INTO stock_reservations (order_line_id, item_id, qty_base, created_by) VALUES (:o, :i, :q, :u) RETURNING id"),
                    {"o": order_line_id, "i": item_id, "q": qty, "u": user_id}).scalar_one()
    return s.scalars(select(StockReservation).where(StockReservation.id == rid).execution_options(populate_existing=True)).one()


def _close_reservation(s: Session, reservation_id: int, status: str) -> None:
    item_id = s.execute(text("SELECT item_id FROM stock_reservations WHERE id = :r"), {"r": reservation_id}).scalar()
    if item_id is None:
        raise LookupError(f"reservation {reservation_id} does not exist.")
    lock_items(s, [item_id])
    closed = s.execute(text("UPDATE stock_reservations SET status = :st WHERE id = :r AND status = 'active' RETURNING id"),
                       {"st": status, "r": reservation_id}).first()  # closed_at is stamped by the database
    if closed is None:
        raise ReservationClosed(f"reservation {reservation_id} is not active.")


def release_reservation(s: Session, reservation_id: int) -> None:
    _close_reservation(s, reservation_id, "released")


def fulfil_reservation(s: Session, reservation_id: int) -> None:
    """Close a reservation whose goods have been dispatched (call in the same transaction as the dispatch)."""
    _close_reservation(s, reservation_id, "fulfilled")


# ------------------------------------------------------------------ reversal
_REVERSIBLE = ("dispatch", "purchase_receipt")


def reverse(s: Session, movement_id: int, *, key: str, user_id: int, require_latest: bool) -> StockMovement:
    """Append the exact mirror of a movement (R1 dispatch restock, R2 purchase void). Never edits the original.

    A purchase receipt is always checked for "latest movement of its item", whatever ``require_latest`` says:
    reversing an earlier receipt would silently change the cost of every later issue (design 9).
    """
    if not isinstance(key, str) or not key.strip():
        raise InvalidPosting("posting key cannot be blank.")
    item_id = s.execute(text("SELECT item_id FROM stock_movements WHERE id = :m"), {"m": movement_id}).scalar()
    if item_id is None:
        raise ReversalNotAllowed(f"movement {movement_id} does not exist.")
    lock_items(s, [item_id])
    existing = s.scalars(select(StockMovement).where(StockMovement.posting_key == key)).first()
    if existing is not None:
        if existing.reverses_movement_id != movement_id:
            raise PostingMismatch(f"posting key {key!r} was already used for a different movement (id {existing.id}).")
        return existing
    orig = s.scalars(select(StockMovement).where(StockMovement.id == movement_id).execution_options(populate_existing=True)).one()
    if orig.reverses_movement_id is not None:
        raise ReversalNotAllowed(f"movement {movement_id} is itself a reversal.")
    if orig.movement_type not in _REVERSIBLE:
        raise ReversalNotAllowed(f"a {orig.movement_type} movement cannot be reversed: post a count adjustment instead.")
    if s.execute(text("SELECT 1 FROM stock_movements WHERE reverses_movement_id = :m"), {"m": movement_id}).first():
        raise ReversalNotAllowed(f"movement {movement_id} is already reversed.")
    if (require_latest or orig.movement_type == "purchase_receipt") and s.execute(
            text("SELECT 1 FROM stock_movements WHERE item_id = :i AND id > :m LIMIT 1"), {"i": item_id, "m": movement_id}).first():
        raise ReversalNotAllowed(f"movement {movement_id} cannot be reversed: a later movement of item {item_id} exists. Post a count adjustment instead.")
    rid = s.execute(
        text("INSERT INTO stock_movements (posting_key, item_id, item_kind, movement_date, movement_type, qty_base, unit_cost, value, "
             "purchase_line_id, production_run_id, invoice_line_id, adjustment_id, reverses_movement_id, created_by) "
             "SELECT :key, item_id, item_kind, greatest(movement_date, current_date), movement_type, -qty_base, unit_cost, -value, "
             "purchase_line_id, production_run_id, invoice_line_id, adjustment_id, id, :user FROM stock_movements WHERE id = :m RETURNING id"),
        {"key": key, "user": user_id, "m": movement_id}).scalar_one()
    return s.scalars(select(StockMovement).where(StockMovement.id == rid).execution_options(populate_existing=True)).one()
