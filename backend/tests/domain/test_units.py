"""Unit conversion and carton splitting. Pure domain logic: no database, no floats."""
from decimal import Decimal

import pytest

from app.domain.units import (
    UNIT_FACTORS,
    IncompatibleUnits,
    InvalidQuantity,
    UnknownUnit,
    convert,
    split_cartons,
)

D = Decimal


# --- convert: the two supported dimensions -------------------------------------------
def test_g_to_kg_is_exact():
    result = convert(D("250"), "g", "kg")
    assert result == D("0.250")
    assert result.as_tuple().exponent == -3  # always three decimal places


def test_kg_to_g():
    assert convert(D("1.2"), "kg", "g") == D("1200.000")


def test_litre_to_ml():
    assert convert(D("0.5"), "l", "ml") == D("500.000")


def test_ml_to_litre():
    assert convert(D("1500"), "ml", "l") == D("1.500")


def test_conversion_is_exact_where_floats_are_not():
    # 0.1 + 0.2 style traps: Decimal keeps these exact.
    assert convert(D("0.1"), "kg", "g") == D("100.000")
    assert convert(D("123456789.123"), "kg", "g") == D("123456789123.000")


# --- identity ------------------------------------------------------------------------
@pytest.mark.parametrize("unit", ["kg", "g", "l", "ml", "pc", "pkt"])
def test_same_unit_is_identity(unit):
    assert convert(D("5"), unit, unit) == D("5.000")


def test_identity_rounds_to_three_places_like_every_other_result():
    assert convert(D("1.2345"), "pc", "pc") == D("1.235")


# --- rounding ------------------------------------------------------------------------
def test_rounds_half_up_to_three_places():
    assert convert(D("0.5"), "ml", "l") == D("0.001")  # 0.0005 -> 0.001
    assert convert(D("0.4"), "ml", "l") == D("0.000")  # 0.0004 -> 0.000


def test_rounding_is_symmetric_for_negative_quantities():
    assert convert(D("-0.5"), "ml", "l") == D("-0.001")


# --- negative and zero ---------------------------------------------------------------
def test_negative_quantity_keeps_its_sign():
    # Ledger issues are signed; conversion is linear so the sign must survive.
    assert convert(D("-250"), "g", "kg") == D("-0.250")


def test_zero_converts_to_zero():
    assert convert(D("0"), "kg", "g") == D("0.000")


# --- incompatible and unknown units --------------------------------------------------
def test_litre_for_kg_rejected():
    with pytest.raises(IncompatibleUnits, match="litre"):
        convert(D("1"), "l", "kg")


def test_incompatible_message_names_both_units():
    with pytest.raises(IncompatibleUnits) as caught:
        convert(D("1"), "kg", "ml")
    assert "kilogram" in str(caught.value) and "millilitre" in str(caught.value)


@pytest.mark.parametrize("src, dst", [("pc", "kg"), ("pkt", "pc"), ("pc", "pkt"), ("ml", "g")])
def test_other_cross_dimension_pairs_rejected(src, dst):
    with pytest.raises(IncompatibleUnits):
        convert(D("1"), src, dst)


def test_incompatible_units_is_a_value_error():
    assert issubclass(IncompatibleUnits, ValueError)


@pytest.mark.parametrize("bad", ["oz", "KG", "", "kilogram"])
def test_unknown_unit_rejected_with_supported_list(bad):
    with pytest.raises(UnknownUnit, match="kg") as caught:
        convert(D("1"), bad, "kg")
    assert repr(bad) in str(caught.value)


def test_unknown_target_unit_rejected():
    with pytest.raises(UnknownUnit, match="oz"):
        convert(D("1"), "kg", "oz")


# --- invalid quantities --------------------------------------------------------------
def test_float_quantity_rejected():
    with pytest.raises(InvalidQuantity, match="float"):
        convert(0.1, "kg", "g")


@pytest.mark.parametrize("bad", ["5", None, True, [1]])
def test_non_numeric_quantity_rejected(bad):
    with pytest.raises(InvalidQuantity, match="Decimal"):
        convert(bad, "kg", "g")


@pytest.mark.parametrize("bad", [D("NaN"), D("Infinity"), D("-Infinity"), D("sNaN")])
def test_non_finite_quantity_rejected(bad):
    with pytest.raises(InvalidQuantity, match="finite"):
        convert(bad, "kg", "g")


def test_int_quantity_is_accepted_because_it_is_exact():
    assert convert(3, "kg", "g") == D("3000.000")


def test_absurdly_long_quantity_is_rejected_not_silently_rounded():
    with pytest.raises(InvalidQuantity, match="digits"):
        convert(D("1" * 80), "kg", "g")


def test_invalid_quantity_is_a_value_error():
    assert issubclass(InvalidQuantity, ValueError) and issubclass(UnknownUnit, ValueError)


# --- the factor table (parity with SQL unit_factor() is tested in Task 6) -----------
def test_factor_table_matches_the_design_document():
    expected = {
        ("kg", "kg"): D("1"), ("g", "g"): D("1"), ("l", "l"): D("1"),
        ("ml", "ml"): D("1"), ("pc", "pc"): D("1"), ("pkt", "pkt"): D("1"),
        ("kg", "g"): D("1000"), ("g", "kg"): D("0.001"),
        ("l", "ml"): D("1000"), ("ml", "l"): D("0.001"),
    }
    assert UNIT_FACTORS == expected
    assert all(isinstance(v, Decimal) for v in UNIT_FACTORS.values())


# --- split_cartons -------------------------------------------------------------------
def test_split_cartons():
    assert split_cartons(D("188"), 20) == (9, 8)


def test_split_cartons_exact():
    assert split_cartons(D("200"), 20) == (10, 0)


def test_split_cartons_fewer_than_one_carton():
    assert split_cartons(D("19"), 20) == (0, 19)


def test_split_cartons_zero_packets():
    assert split_cartons(D("0"), 20) == (0, 0)


def test_split_cartons_accepts_whole_valued_decimal_and_int():
    assert split_cartons(D("188.000"), 20) == (9, 8)
    assert split_cartons(188, 20) == (9, 8)


def test_split_cartons_returns_plain_ints():
    cartons, loose = split_cartons(D("188"), 20)
    assert type(cartons) is int and type(loose) is int


def test_split_cartons_rejects_fractional_packets():
    with pytest.raises(InvalidQuantity, match="whole number"):
        split_cartons(D("188.5"), 20)


def test_split_cartons_rejects_negative_packets():
    with pytest.raises(InvalidQuantity, match="negative"):
        split_cartons(D("-1"), 20)


@pytest.mark.parametrize("bad", [D("NaN"), D("Infinity"), 18.0, "188", None])
def test_split_cartons_rejects_invalid_packet_counts(bad):
    with pytest.raises(InvalidQuantity):
        split_cartons(bad, 20)


@pytest.mark.parametrize("bad", [0, -5, 20.0, D("20"), "20", True, None])
def test_split_cartons_rejects_invalid_carton_size(bad):
    with pytest.raises(InvalidQuantity, match="packets_per_carton"):
        split_cartons(D("188"), bad)
