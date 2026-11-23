"""Moving weighted-average costing, per-run cost and shared-overhead allocation.

Pure domain logic: no database, no web framework, no clock. Decimal only; floats are
rejected because a float is already not the number the user typed.

Precision follows the database: quantities have at most 3 decimal places, money is in whole
paise, and a unit cost or cost per packet is shown to 4 decimal places.

Weighted-average rule (design section 9):
  * the cost of an issue is its quantity x (value on hand / quantity on hand), rounded half-up
    to the paisa. It is computed as quantity x value / on-hand quantity, with the division last,
    so the average is never rounded before it is used and exact ties stay exact;
  * an issue that empties the stock takes the whole remaining value, so rounding can never
    leave a residue behind or drive the value negative.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Context, Decimal

_CALC = Context(prec=60, rounding=ROUND_HALF_UP)  # 60 digits: every valid calculation is exact enough
_PAISE = Decimal("0.01")
_QTY_STEP = Decimal("0.001")
_RATE_STEP = Decimal("0.0001")
MAX_AMOUNT = Decimal("999999999999.99")    # NUMERIC(14,2)
MAX_QUANTITY = Decimal("99999999999.999")  # NUMERIC(14,3)


class CostingError(ValueError):
    """Base class for every input or consistency error in this module."""


class InvalidQuantity(CostingError):
    """A quantity is not an exact, finite, non-negative Decimal with at most 3 decimal places."""


class InvalidCost(CostingError):
    """A cost or weight is not an exact, finite, non-negative Decimal in whole paise."""


class InsufficientStock(CostingError):
    """More was asked for than is on hand, or there is no stock to average over."""


class ZeroOutput(CostingError):
    """A run produced no good packets, so its cost has no per-packet value."""


@dataclass(frozen=True)
class RunCost:
    materials: Decimal
    overheads: Decimal
    total: Decimal
    per_packet: Decimal    # total / good packets, 4 decimal places (the stored precision)
    good_packets: Decimal

    @property
    def per_packet_display(self) -> Decimal:
        """Cost per packet to 2 places, rounded once from the exact cost.

        Rounding the 4-place value again could be wrong: 0.444996 is 0.4450 at four places
        but 0.44 at two, not 0.45.
        """
        return _CALC.quantize(_CALC.divide(self.total, self.good_packets), _PAISE)


def _decimal(value: object, name: str, error: type[CostingError]) -> Decimal:
    if isinstance(value, bool):
        raise error(f"{name} must be a Decimal or int, not bool (got {value!r}).")
    if isinstance(value, float):
        raise error(
            f"{name} must be a Decimal, not a float, because floats are inexact (got {value!r}). "
            f"Pass Decimal('{value}') instead."
        )
    if isinstance(value, int):
        return Decimal(value)
    if not isinstance(value, Decimal):
        raise error(f"{name} must be a Decimal or int, got {type(value).__name__} ({value!r}).")
    if not value.is_finite():
        raise error(f"{name} must be a finite number, got {value}.")
    return value


def _quantity(value: object, name: str) -> Decimal:
    qty = _decimal(value, name, InvalidQuantity)
    if qty < 0:
        raise InvalidQuantity(f"{name} cannot be negative, got {qty}.")
    if qty > MAX_QUANTITY:  # checked before rounding, which would fail on absurdly large values
        raise InvalidQuantity(f"{name} cannot exceed {MAX_QUANTITY}, got {qty}.")
    if qty != _CALC.quantize(qty, _QTY_STEP):
        raise InvalidQuantity(f"{name} can have at most 3 decimal places, got {qty}.")
    return qty.copy_abs() if qty.is_zero() else qty  # never carry a negative zero


def _money(value: object, name: str) -> Decimal:
    amount = _decimal(value, name, InvalidCost)
    if amount < 0:
        raise InvalidCost(f"{name} cannot be negative, got {amount}.")
    if amount > MAX_AMOUNT:  # checked before rounding, which would fail on absurdly large values
        raise InvalidCost(f"{name} cannot exceed {MAX_AMOUNT}, got {amount}.")
    if amount != _CALC.quantize(amount, _PAISE):
        raise InvalidCost(f"{name} must be in whole paise (at most 2 decimal places), got {amount}.")
    return _CALC.quantize(amount.copy_abs() if amount.is_zero() else amount, _PAISE)  # no negative zero


def _stock(on_hand_qty: object, on_hand_value: object) -> tuple[Decimal, Decimal]:
    qty = _quantity(on_hand_qty, "quantity on hand")
    value = _money(on_hand_value, "value on hand")
    if qty == 0 and value != 0:
        raise CostingError(f"Stock records are inconsistent: 0 on hand but a value of {value} is recorded.")
    return qty, value


def wac_rate(on_hand_qty: Decimal, on_hand_value: Decimal) -> Decimal:
    """Weighted-average cost per unit (value on hand / quantity on hand), to 4 decimal places."""
    qty, value = _stock(on_hand_qty, on_hand_value)
    if qty == 0:
        raise InsufficientStock("There is no stock on hand, so there is no average cost.")
    return _CALC.quantize(_CALC.divide(value, qty), _RATE_STEP)


def wac_issue_value(qty: Decimal, on_hand_qty: Decimal, on_hand_value: Decimal) -> Decimal:
    """Value of issuing ``qty`` from stock holding ``on_hand_qty`` worth ``on_hand_value``."""
    issue = _quantity(qty, "issue quantity")
    stock_qty, stock_value = _stock(on_hand_qty, on_hand_value)
    if issue == 0:
        raise InvalidQuantity("issue quantity must be greater than zero.")
    if issue > stock_qty:
        raise InsufficientStock(f"Cannot issue {issue} with only {stock_qty} on hand.")
    if issue == stock_qty:
        return stock_value  # emptying the stock takes the exact remaining value: no residue
    return _CALC.quantize(_CALC.divide(_CALC.multiply(issue, stock_value), stock_qty), _PAISE)


def run_cost(materials: Decimal, overheads: Decimal, good_packets: Decimal) -> RunCost:
    """Total cost of a run and its cost per good packet.

    ``materials`` is the sum of the frozen issue values, ``overheads`` the direct and allocated
    overhead, ``good_packets`` the actual output (not the planned yield).
    """
    material_cost = _money(materials, "materials")
    overhead_cost = _money(overheads, "overheads")
    packets = _decimal(good_packets, "good packets", InvalidQuantity)
    if packets < 0:
        raise InvalidQuantity(f"good packets cannot be negative, got {packets}.")
    if packets > MAX_QUANTITY:
        raise InvalidQuantity(f"good packets cannot exceed {MAX_QUANTITY}, got {packets}.")
    if packets != packets.to_integral_value():
        raise InvalidQuantity(f"good packets must be a whole number, got {packets}.")
    if packets == 0:
        raise ZeroOutput("Cannot compute cost per packet: the run produced zero good packets.")
    total = _CALC.add(material_cost, overhead_cost)
    if total > MAX_AMOUNT:
        raise InvalidCost(f"total cost {total} cannot exceed {MAX_AMOUNT}.")
    per_packet = _CALC.quantize(_CALC.divide(total, packets), _RATE_STEP)
    return RunCost(material_cost, overhead_cost, total, per_packet, packets)


def allocate_shared(pool: Decimal, weights: Mapping[int, Decimal]) -> dict[int, Decimal]:
    """Split a shared overhead pool in proportion to ``weights``, to the paisa, summing exactly.

    Largest-remainder method: every key first gets the whole paise of its exact share, then the
    paise still unassigned go one each to the keys with the largest fractional remainders. Equal
    remainders are resolved by the lower key, so the result never depends on input order. The
    arithmetic runs on whole integers (paise and weights in paise), so there is no rounding at all
    until the final paisa are handed out. Weights must be non-negative amounts in whole paise.
    """
    amount = _money(pool, "overhead pool")
    if not weights:
        raise CostingError("Cannot allocate overhead: no runs were given to share it between.")
    # Weights are material costs, so they are validated as money: whole paise and bounded.
    # That keeps every scaled weight a small exact integer.
    scaled = {key: int(_CALC.scaleb(_money(w, f"weight for run {key}"), 2)) for key, w in weights.items()}
    total_weight = sum(scaled.values())
    if total_weight == 0:
        raise CostingError("Cannot allocate overhead: the total weight is zero, so there is nothing to divide it by.")
    pool_paise = int(_CALC.scaleb(amount, 2))
    paise, remainder = {}, {}
    for key, w in scaled.items():
        paise[key], remainder[key] = divmod(pool_paise * w, total_weight)
    unassigned = pool_paise - sum(paise.values())
    for key in sorted(scaled, key=lambda k: (-remainder[k], k))[:unassigned]:
        paise[key] += 1
    return {key: _CALC.scaleb(Decimal(paise[key]), -2) for key in weights}
