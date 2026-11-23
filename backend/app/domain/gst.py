"""GST split, Indian financial year and invoice numbering.

Pure domain logic: no database, no web framework, no clock, no network. All money is
Decimal; floats are rejected because a float such as 0.1 is not the number the user typed.

Rounding rule (the same as the SQL CHECK constraints, proved by tests against PostgreSQL):
every tax component is rounded half-up to the paisa on its own. An intra-state supply
therefore has CGST = SGST = round(taxable x rate / 200) and an inter-state supply has
IGST = round(taxable x rate / 100). The two can differ by a paisa for the same taxable
value (333.33 at 5%: 8.33 + 8.33 = 16.66 against 16.67). That is a documented effect of
per-component rounding, not a defect.

Invoice totals keep paise precision. Whole-rupee rounding is an explicit option only.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Context, Decimal
from typing import Literal

SupplyType = Literal["intra", "inter"]


class GstError(ValueError):
    """Base class for every input error in this module."""


class InvalidTaxRate(GstError):
    """The GST rate is not a Decimal between 0 and 40 with at most two decimal places."""


class InvalidStateCode(GstError):
    """The state code is not a valid two-digit GST state code."""


class InvalidAmount(GstError):
    """The amount is not a non-negative Decimal in whole paise that fits the database."""


class InvalidDate(GstError):
    """A date or timestamp is the wrong type or has no timezone."""


class InvalidInvoiceNumber(GstError):
    """The financial year or sequence cannot form a valid invoice number."""


# Asia/Kolkata has had no daylight saving since 1945, so a fixed +05:30 offset is exactly
# equivalent for every date this system will see, and it needs no timezone database (which
# plain Windows Python installs lack). A test compares it with the tz database where present.
IST = timezone(timedelta(hours=5, minutes=30), "Asia/Kolkata")

MAX_RATE = Decimal("40")                      # matches products.gst_rate CHECK (0..40); the 2025 reforms added a 40% rate
MAX_AMOUNT = Decimal("999999999999.99")       # matches NUMERIC(14,2)
MAX_INVOICE_NO_LENGTH = 16                    # GST invoice rules allow at most 16 characters
# Two-digit GST state/UT codes 01-38 plus 97 (Other Territory). A format and range check,
# not a live lookup.
_STATE_CODES = frozenset(f"{i:02d}" for i in range(1, 39)) | {"97"}

_PAISE = Decimal("0.01")
_RUPEE = Decimal("1")
_ZERO = Decimal("0.00")
_CALC = Context(prec=60, rounding=ROUND_HALF_UP)  # 60 digits: every valid calculation is exact
_FY_PATTERN = re.compile(r"^(\d{4})-(\d{2})$", re.ASCII)


@dataclass(frozen=True)
class TaxSplit:
    cgst: Decimal
    sgst: Decimal
    igst: Decimal

    @property
    def total(self) -> Decimal:
        return self.cgst + self.sgst + self.igst


def _decimal(value: object, name: str, error: type[GstError]) -> Decimal:
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


def _rate(value: object) -> Decimal:
    rate = _decimal(value, "GST rate", InvalidTaxRate)
    if rate < 0 or rate > MAX_RATE:
        raise InvalidTaxRate(f"GST rate must be between 0 and 40 percent, got {rate}.")
    if rate != _CALC.quantize(rate, _PAISE):
        raise InvalidTaxRate(f"GST rate can have at most 2 decimal places, got {rate}.")
    return rate


def _paise(value: object, name: str) -> Decimal:
    amount = _decimal(value, name, InvalidAmount)
    if amount < 0:
        raise InvalidAmount(f"{name} cannot be negative, got {amount}.")
    if amount != _CALC.quantize(amount, _PAISE):
        raise InvalidAmount(f"{name} must be in whole paise (at most 2 decimal places), got {amount}. Round it first.")
    if amount > MAX_AMOUNT:
        raise InvalidAmount(f"{name} cannot exceed {MAX_AMOUNT}, got {amount}.")
    return _CALC.quantize(amount, _PAISE)


def _state_code(value: object) -> str:
    if not isinstance(value, str) or value not in _STATE_CODES:
        raise InvalidStateCode(
            f"Invalid state code {value!r}: expected a two-digit GST state code such as '33' (Tamil Nadu) "
            f"or '29' (Karnataka)."
        )
    return value


def supply_type(seller_state: str, place_of_supply: str) -> SupplyType:
    """'intra' when the seller's state equals the place of supply, otherwise 'inter'."""
    return "intra" if _state_code(seller_state) == _state_code(place_of_supply) else "inter"


def split_tax(taxable: Decimal, rate_pct: Decimal, supply: SupplyType) -> TaxSplit:
    """Split GST on a taxable value into CGST + SGST (intra) or IGST (inter), in paise."""
    if supply not in ("intra", "inter"):
        raise GstError(f"supply must be 'intra' or 'inter', got {supply!r}.")
    amount = _paise(taxable, "taxable value")
    rate = _rate(rate_pct)
    product = _CALC.multiply(amount, rate)
    if supply == "intra":
        half = _CALC.quantize(_CALC.divide(product, 200), _PAISE)
        return TaxSplit(half, half, _ZERO)
    return TaxSplit(_ZERO, _ZERO, _CALC.quantize(_CALC.divide(product, 100), _PAISE))


def financial_year(d: date) -> str:
    """Indian financial year (April to March) containing ``d``, e.g. '2026-27'."""
    if isinstance(d, datetime):
        raise InvalidDate("financial_year needs a date, not a datetime; convert it with to_ist_date first.")
    if not isinstance(d, date):
        raise InvalidDate(f"financial_year needs a date, got {type(d).__name__} ({d!r}).")
    start = d.year if d.month >= 4 else d.year - 1
    return f"{start:04d}-{(start + 1) % 100:02d}"


def to_ist_date(ts: datetime) -> date:
    """The calendar date in India at the moment ``ts`` (which must carry a timezone)."""
    if not isinstance(ts, datetime):
        raise InvalidDate(f"to_ist_date needs a datetime, got {type(ts).__name__} ({ts!r}).")
    if ts.tzinfo is None or ts.utcoffset() is None:
        raise InvalidDate("to_ist_date needs a datetime with a timezone; a naive time has an ambiguous day.")
    return ts.astimezone(IST).date()


def format_invoice_no(fy: str, seq: int) -> str:
    """'INV/26-27/0001' for ('2026-27', 1). At most 16 characters, as GST rules require."""
    match = _FY_PATTERN.match(fy) if isinstance(fy, str) else None
    if match is None or (int(match.group(1)) + 1) % 100 != int(match.group(2)):
        raise InvalidInvoiceNumber(f"Invalid financial year {fy!r}: expected a form like '2026-27'.")
    if type(seq) is not int or seq < 1:
        raise InvalidInvoiceNumber(f"Invoice sequence must be a whole number of 1 or more, got {seq!r}.")
    number = f"INV/{fy[2:4]}-{fy[5:7]}/{seq:04d}"
    if len(number) > MAX_INVOICE_NO_LENGTH:
        raise InvalidInvoiceNumber(
            f"Invoice number {number!r} would exceed {MAX_INVOICE_NO_LENGTH} characters; "
            f"sequence {seq} is too large for this format."
        )
    return number


def round_grand_total(amount: Decimal, *, round_to_rupee: bool = False) -> tuple[Decimal, Decimal]:
    """Return (grand total, round-off).

    By default the total keeps its paise and the round-off is 0.00. Only when
    ``round_to_rupee`` is True is the total rounded half-up to a whole rupee, and the
    round-off then records the difference (grand total = amount + round-off).
    """
    if not isinstance(round_to_rupee, bool):
        raise TypeError(f"round_to_rupee must be True or False, got {round_to_rupee!r}.")
    exact = _paise(amount, "grand total")
    if not round_to_rupee:
        return exact, _ZERO
    total = _CALC.quantize(_CALC.quantize(exact, _RUPEE), _PAISE)
    return total, total - exact
