"""Variance between expected and actual quantities: numbers first, wording second.

Pure domain logic: no database, no web framework, no clock. Decimal only; floats are
rejected because a float is already not the number the user typed.

Two layers are kept apart on purpose (design section 10):

* ``compute_variance`` states facts: how far actual is from expected, in quantity and
  percent, whether it is over or under, and whether it is *strictly* beyond the tolerance.
  It never says whether the result is good or bad.
* ``describe_variance`` turns those facts into a short neutral sentence for factory staff.
  It never labels a result favourable or unfavourable, because using less of something is
  not good news if output also fell or packing was incomplete.

Yield (packets produced) and material consumption are separate calculations: call
``compute_variance`` once per line, with ``kind="yield"`` for the output line.

The tolerance test is exact: ``|actual - expected| x 100 > tolerance x expected``. It is not
made on the rounded percentage, so 3.004% is flagged against a 3% tolerance even though it
displays as 3.00. When nothing was expected the percentage is undefined (``None``) and any
use at all exceeds every tolerance.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Context, Decimal
from typing import Literal

Deviation = Literal["over", "under", "on_standard"]
Kind = Literal["yield", "raw", "packaging"]

_CALC = Context(prec=60, rounding=ROUND_HALF_UP)
_QTY_STEP = Decimal("0.001")
_PCT_STEP = Decimal("0.01")
MAX_QUANTITY = Decimal("99999999999.999")  # NUMERIC(14,3)
MAX_TOLERANCE = Decimal("100")             # matches settings.variance_tolerance_pct CHECK (0..100)
_KINDS = ("yield", "raw", "packaging")


class VarianceError(ValueError):
    """Base class for every input error in this module."""


class InvalidQuantity(VarianceError):
    """A quantity is not an exact, finite, non-negative Decimal with at most 3 decimal places."""


class InvalidTolerance(VarianceError):
    """The tolerance is not a Decimal between 0 and 100 with at most 2 decimal places."""


@dataclass(frozen=True)
class Variance:
    variance_qty: Decimal          # actual - expected, signed, 3 decimal places
    variance_pct: Decimal | None   # variance_qty / expected x 100, 2 places; None if expected is 0 and actual is not
    deviation: Deviation           # purely numeric: over / under / on_standard
    exceeds_tolerance: bool        # strictly greater than the tolerance
    tolerance_pct: Decimal         # the tolerance this was judged against


def _decimal(value: object, name: str, error: type[VarianceError]) -> Decimal:
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
    if qty > MAX_QUANTITY:  # checked before rounding, which fails on absurdly large values
        raise InvalidQuantity(f"{name} cannot exceed {MAX_QUANTITY}, got {qty}.")
    if qty != _CALC.quantize(qty, _QTY_STEP):
        raise InvalidQuantity(f"{name} can have at most 3 decimal places, got {qty}.")
    return qty.copy_abs() if qty.is_zero() else qty  # never carry a negative zero


def _tolerance(value: object) -> Decimal:
    tol = _decimal(value, "tolerance", InvalidTolerance)
    if tol < 0 or tol > MAX_TOLERANCE:
        raise InvalidTolerance(f"tolerance must be between 0 and 100 percent, got {tol}.")
    if tol != _CALC.quantize(tol, _PCT_STEP):
        raise InvalidTolerance(f"tolerance can have at most 2 decimal places, got {tol}.")
    return tol.copy_abs() if tol.is_zero() else tol


def _no_negative_zero(value: Decimal) -> Decimal:
    return value.copy_abs() if value.is_zero() else value


def compute_variance(expected: Decimal, actual: Decimal, tolerance_pct: Decimal) -> Variance:
    """The numeric facts about how ``actual`` differs from ``expected``."""
    exp = _quantity(expected, "expected quantity")
    act = _quantity(actual, "actual quantity")
    tol = _tolerance(tolerance_pct)

    diff = _CALC.subtract(act, exp)
    exceeds = _CALC.multiply(diff.copy_abs(), 100) > _CALC.multiply(tol, exp)  # exact, no division
    if exp == 0:
        pct = None if act != 0 else Decimal("0.00")  # a percentage of nothing is undefined
    else:
        pct = _no_negative_zero(_CALC.quantize(_CALC.divide(_CALC.multiply(diff, 100), exp), _PCT_STEP))
    deviation: Deviation = "over" if diff > 0 else "under" if diff < 0 else "on_standard"
    return Variance(
        variance_qty=_no_negative_zero(_CALC.quantize(diff, _QTY_STEP)),
        variance_pct=pct,
        deviation=deviation,
        exceeds_tolerance=exceeds,
        tolerance_pct=tol,
    )


def _plain(value: Decimal) -> str:
    """A Decimal without trailing zeros or exponent: 0.250 -> '0.25', 3.00 -> '3'."""
    return format(value.normalize(), "f")


def _headline(kind: Kind, v: Variance) -> str:
    tol = _plain(v.tolerance_pct)
    shown = None if v.variance_pct is None else _CALC.quantize(v.variance_pct.copy_abs(), _PCT_STEP)
    if not v.exceeds_tolerance:
        flag = f"within {tol}% tolerance"
    elif shown is not None and shown <= v.tolerance_pct:
        # The flag is exact but the percentage shown is rounded: "3.00% exceeds 3%" would read as a mistake.
        flag = f"marginally above the {tol}% tolerance"
    else:
        flag = f"exceeds {tol}% tolerance"
    is_yield = kind == "yield"
    if v.variance_pct is None:
        subject = "Output recorded" if is_yield else "Used"
        return f"{subject} although none was expected{'' if is_yield else ' for this run'} ({flag})."
    if v.deviation == "on_standard":
        return "Output matched the expected quantity." if is_yield else "On standard: the expected quantity was used."
    pct = format(shown, "f")
    if is_yield:
        direction = "below" if v.deviation == "under" else "above"
        return f"Output {direction} expected by {pct}% ({flag})."
    direction = "Under" if v.deviation == "under" else "Over"
    return f"{direction} standard by {pct}% ({flag})."


def describe_variance(
    kind: Kind, v: Variance, *, scrap_qty: Decimal = Decimal(0), uncartoned_packets: int = 0
) -> str:
    """A short, neutral sentence for factory staff. It states facts and never judges them.

    ``scrap_qty`` is the quantity of this item recorded as scrapped (raw or packaging lines).
    ``uncartoned_packets`` is the number of finished packets not packed into a carton
    (packaging line for cartons only).
    """
    if kind not in _KINDS:
        raise VarianceError(f"kind must be 'yield', 'raw' or 'packaging', got {kind!r}.")
    scrap = _quantity(scrap_qty, "scrap quantity")
    if type(uncartoned_packets) is not int or uncartoned_packets < 0:
        raise InvalidQuantity(f"uncartoned packets must be a whole number of 0 or more, got {uncartoned_packets!r}.")
    if scrap > 0 and kind == "yield":
        raise VarianceError("scrap quantity does not apply to yield.")
    if uncartoned_packets and kind != "packaging":
        raise VarianceError("uncartoned packets only applies to packaging.")

    parts = [_headline(kind, v)]
    if scrap > 0:
        parts.append(f"{_plain(scrap)} scrapped.")
    if uncartoned_packets:
        noun = "packet" if uncartoned_packets == 1 else "packets"
        parts.append(f"{uncartoned_packets} {noun} uncartoned: verify packing.")
    return " ".join(parts)
