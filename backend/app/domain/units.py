"""Exact unit conversion and carton splitting.

Everything here uses Decimal only. Floats are rejected rather than converted, because a
float such as 0.1 is already not the number the user typed.

Supported units mirror the SQL enum ``unit_code`` and the SQL function ``unit_factor()``:
kg and g (mass), l and ml (volume), pc and pkt (counts). Converting between dimensions is
an error: there is no density in this system, so litres can never become kilograms.
"""
from decimal import (
    ROUND_HALF_UP,
    Context,
    Decimal,
    Inexact,
    InvalidOperation,
    Overflow,
)


class IncompatibleUnits(ValueError):
    """The two units measure different things (for example litres and kilograms)."""


class UnknownUnit(ValueError):
    """The unit code is not one of the supported codes."""


class InvalidQuantity(ValueError):
    """The quantity is not an exact, finite, valid number for this operation."""


# code -> (display name, dimension)
_UNITS: dict[str, tuple[str, str]] = {
    "kg": ("kilogram", "mass"),
    "g": ("gram", "mass"),
    "l": ("litre", "volume"),
    "ml": ("millilitre", "volume"),
    "pc": ("piece", "pieces"),
    "pkt": ("packet", "packets"),
}

# Multiplier to turn `from` into `to`. Identity pairs are listed so this table matches the
# SQL unit_factor() exactly (checked by a parity test once the schema exists).
UNIT_FACTORS: dict[tuple[str, str], Decimal] = {
    **{(code, code): Decimal("1") for code in _UNITS},
    ("kg", "g"): Decimal("1000"),
    ("g", "kg"): Decimal("0.001"),
    ("l", "ml"): Decimal("1000"),
    ("ml", "l"): Decimal("0.001"),
}

_PLACES = Decimal("0.001")  # quantities are stored with three decimal places
# Multiplying must be exact: if the result needs more than 40 digits we refuse, not round.
_EXACT = Context(prec=40, traps=[Inexact, InvalidOperation, Overflow])
_ROUND = Context(prec=40, rounding=ROUND_HALF_UP, traps=[InvalidOperation, Overflow])


def _as_decimal(value: object, name: str) -> Decimal:
    if isinstance(value, bool):
        raise InvalidQuantity(f"{name} must be a Decimal or int, not bool (got {value!r}).")
    if isinstance(value, float):
        raise InvalidQuantity(
            f"{name} must be a Decimal, not a float, because floats are inexact (got {value!r}). "
            f"Pass Decimal('{value}') instead."
        )
    if isinstance(value, int):
        return Decimal(value)
    if not isinstance(value, Decimal):
        raise InvalidQuantity(f"{name} must be a Decimal or int, got {type(value).__name__} ({value!r}).")
    if not value.is_finite():
        raise InvalidQuantity(f"{name} must be a finite number, got {value}.")
    return value


def _check_unit(unit: object) -> None:
    if not isinstance(unit, str) or unit not in _UNITS:
        supported = ", ".join(_UNITS)
        raise UnknownUnit(f"Unknown unit {unit!r}. Supported units: {supported}.")


def convert(qty: Decimal, from_unit: str, to_unit: str) -> Decimal:
    """Convert ``qty`` between units, rounding half-up to three decimal places.

    The sign is preserved (ledger issues are negative) and zero is allowed. Whether a
    quantity must be positive is the caller's rule, for example a purchase line.
    """
    value = _as_decimal(qty, "quantity")
    _check_unit(from_unit)
    _check_unit(to_unit)
    factor = UNIT_FACTORS.get((from_unit, to_unit))
    if factor is None:
        from_name, from_dim = _UNITS[from_unit]
        to_name, to_dim = _UNITS[to_unit]
        raise IncompatibleUnits(
            f"Cannot convert {from_name} ({from_unit}) to {to_name} ({to_unit}): "
            f"{from_dim} cannot be converted to {to_dim}."
        )
    try:
        result = _ROUND.quantize(_EXACT.multiply(value, factor), _PLACES)
    except ArithmeticError:
        raise InvalidQuantity(f"quantity {value} has too many digits to convert exactly.") from None
    return result.copy_abs() if result.is_zero() else result  # never return -0.000


def split_cartons(packets: Decimal, packets_per_carton: int) -> tuple[int, int]:
    """Split a packet count into (full cartons, loose packets).

    188 packets at 20 per carton is (9, 8): nine full cartons and eight packets left over.
    """
    count = _as_decimal(packets, "packets")
    if type(packets_per_carton) is not int or packets_per_carton <= 0:
        raise InvalidQuantity(
            f"packets_per_carton must be a positive whole-number int, got {packets_per_carton!r}."
        )
    if count < 0:
        raise InvalidQuantity(f"packets cannot be negative, got {count}.")
    if count != count.to_integral_value():
        raise InvalidQuantity(f"packets must be a whole number, got {count}.")
    return divmod(int(count), packets_per_carton)
