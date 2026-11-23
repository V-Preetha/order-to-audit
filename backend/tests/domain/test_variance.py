"""Variance: the numeric layer (facts) and the wording layer (neutral description).

Pure domain logic: no database, no floats. The numeric layer never says whether a variance
is good or bad, and the wording never does either.
"""
import ast
import dataclasses
from decimal import Decimal
from pathlib import Path

import pytest

from app.domain.variance import (
    InvalidQuantity,
    InvalidTolerance,
    Variance,
    VarianceError,
    compute_variance,
    describe_variance,
)

D = Decimal


# --- numeric layer: the benchmark run -------------------------------------------------
def test_yield_variance():
    v = compute_variance(D("200"), D("188"), D("3"))
    assert (v.variance_qty, v.variance_pct, v.deviation, v.exceeds_tolerance) == (D("-12"), D("-6.00"), "under", True)


def test_oil_is_five_percent_over_and_flagged():
    v = compute_variance(D("10"), D("10.5"), D("3"))
    assert (v.variance_qty, v.variance_pct, v.deviation, v.exceeds_tolerance) == (D("0.5"), D("5.00"), "over", True)


def test_pouches_at_exactly_the_tolerance_are_not_flagged():
    v = compute_variance(D("200"), D("194"), D("3"))
    assert (v.variance_pct, v.deviation, v.exceeds_tolerance) == (D("-3.00"), "under", False)


def test_cartons_ten_percent_under_and_flagged():
    v = compute_variance(D("10"), D("9"), D("3"))
    assert (v.variance_pct, v.deviation, v.exceeds_tolerance) == (D("-10.00"), "under", True)


@pytest.mark.parametrize("expected, actual", [("40", "40"), ("1.2", "1.2")])
def test_flour_and_spice_are_on_standard(expected, actual):
    v = compute_variance(D(expected), D(actual), D("3"))
    assert (v.variance_qty, v.variance_pct, v.deviation, v.exceeds_tolerance) == (D("0"), D("0.00"), "on_standard", False)


def test_benchmark_flags_exactly_yield_oil_and_cartons():
    lines = {  # name: (expected, actual)
        "yield": ("200", "188"), "flour": ("40", "40"), "oil": ("10", "10.5"),
        "spice": ("1.2", "1.2"), "pouches": ("200", "194"), "cartons": ("10", "9"),
    }
    flagged = {n for n, (e, a) in lines.items() if compute_variance(D(e), D(a), D("3")).exceeds_tolerance}
    assert flagged == {"yield", "oil", "cartons"}


def test_results_carry_fixed_decimal_places():
    v = compute_variance(D("200"), D("188"), D("3"))
    assert v.variance_pct.as_tuple().exponent == -2 and v.variance_qty.as_tuple().exponent == -3


# --- numeric layer: strictness and rounding -------------------------------------------
@pytest.mark.parametrize("actual, flagged", [
    ("1030.00", False), ("1030.01", True), ("970.00", False), ("969.99", True),
])
def test_only_strictly_more_than_the_tolerance_is_flagged(actual, flagged):
    assert compute_variance(D("1000"), D(actual), D("3")).exceeds_tolerance is flagged


def test_flag_uses_the_exact_ratio_not_the_rounded_percentage():
    # 3.004% displays as 3.00 but is strictly above 3, so it is flagged. 3.000% is not.
    above = compute_variance(D("1000"), D("1030.04"), D("3"))
    assert above.variance_pct == D("3.00") and above.exceeds_tolerance is True
    assert compute_variance(D("1000"), D("1030.00"), D("3")).exceeds_tolerance is False


def test_percentage_rounds_half_up_symmetrically():
    assert compute_variance(D("8"), D("8.001"), D("3")).variance_pct == D("0.01")    # 0.0125 -> 0.01
    assert compute_variance(D("8"), D("7.999"), D("3")).variance_pct == D("-0.01")


def test_tiny_negative_variance_never_shows_negative_zero():
    v = compute_variance(D("100000"), D("99999.999"), D("3"))
    assert v.deviation == "under" and v.exceeds_tolerance is False
    assert str(v.variance_pct) == "0.00"


def test_zero_tolerance_flags_any_deviation():
    assert compute_variance(D("10"), D("10.001"), D("0")).exceeds_tolerance is True
    assert compute_variance(D("10"), D("10"), D("0")).exceeds_tolerance is False


def test_hundred_percent_tolerance_boundary():
    assert compute_variance(D("10"), D("20"), D("100")).exceeds_tolerance is False
    assert compute_variance(D("10"), D("20.001"), D("100")).exceeds_tolerance is True


# --- numeric layer: zero expected -----------------------------------------------------
def test_expected_zero_is_unplanned():
    v = compute_variance(D("0"), D("5"), D("3"))
    assert v.variance_pct is None  # documented sentinel: a percentage of nothing is undefined
    assert (v.variance_qty, v.deviation, v.exceeds_tolerance) == (D("5"), "over", True)


def test_unplanned_use_exceeds_even_a_hundred_percent_tolerance():
    assert compute_variance(D("0"), D("0.001"), D("100")).exceeds_tolerance is True


def test_expected_zero_and_actual_zero_is_on_standard():
    v = compute_variance(D("0"), D("0"), D("3"))
    assert (v.variance_pct, v.deviation, v.exceeds_tolerance) == (D("0.00"), "on_standard", False)


# --- numeric layer: invalid input -----------------------------------------------------
@pytest.mark.parametrize("bad", [D("-1"), D("1.0005"), 1.5, "1", None, True, D("NaN"), D("Infinity"), D("1E+100")])
@pytest.mark.parametrize("position", ["expected", "actual"])
def test_invalid_quantities_rejected(bad, position):
    args = {"expected": D("10"), "actual": D("10"), "tolerance_pct": D("3")}
    args[position] = bad
    with pytest.raises(InvalidQuantity, match=f"{position} quantity"):
        compute_variance(**args)


@pytest.mark.parametrize("bad", [D("-0.01"), D("100.01"), D("3.005"), 3.0, "3", None, True, D("NaN"), D("Infinity")])
def test_invalid_tolerance_rejected(bad):
    with pytest.raises(InvalidTolerance, match="tolerance"):
        compute_variance(D("10"), D("10"), bad)


def test_error_messages_are_specific():
    with pytest.raises(InvalidQuantity, match="negative"):
        compute_variance(D("10"), D("-1"), D("3"))
    with pytest.raises(InvalidQuantity, match="3 decimal"):
        compute_variance(D("10.0005"), D("10"), D("3"))
    with pytest.raises(InvalidTolerance, match="between 0 and 100"):
        compute_variance(D("10"), D("10"), D("101"))
    with pytest.raises(InvalidTolerance, match="float"):
        compute_variance(D("10"), D("10"), 3.0)


def test_negative_zero_inputs_are_accepted_and_normalised():
    v = compute_variance(D("-0"), D("-0"), D("3"))
    assert str(v.variance_qty) == "0.000" and v.deviation == "on_standard"


def test_variance_errors_are_value_errors():
    assert issubclass(InvalidQuantity, VarianceError) and issubclass(InvalidTolerance, VarianceError)
    assert issubclass(VarianceError, ValueError)


# --- the two layers stay apart --------------------------------------------------------
def test_numeric_layer_has_no_judgement_field():
    names = [f.name for f in dataclasses.fields(Variance)]
    assert names == ["variance_qty", "variance_pct", "deviation", "exceeds_tolerance", "tolerance_pct"]


# --- wording: the benchmark lines, exactly --------------------------------------------
def test_carton_wording_is_neutral_and_mentions_uncartoned_packets():
    v = compute_variance(D("10"), D("9"), D("3"))
    text = describe_variance("packaging", v, uncartoned_packets=8)
    assert text == "Under standard by 10.00% (exceeds 3% tolerance). 8 packets uncartoned: verify packing."
    assert "favourable" not in text and "unfavourable" not in text


def test_yield_wording():
    v = compute_variance(D("200"), D("188"), D("3"))
    text = describe_variance("yield", v)
    assert text == "Output below expected by 6.00% (exceeds 3% tolerance)."
    assert "below expected" in text


def test_oil_wording():
    v = compute_variance(D("10"), D("10.5"), D("3"))
    assert describe_variance("raw", v) == "Over standard by 5.00% (exceeds 3% tolerance)."


def test_pouch_wording_includes_scrap_and_says_within_tolerance():
    v = compute_variance(D("200"), D("194"), D("3"))
    assert describe_variance("packaging", v, scrap_qty=D("6")) == "Under standard by 3.00% (within 3% tolerance). 6 scrapped."


def test_on_standard_wording():
    v = compute_variance(D("40"), D("40"), D("3"))
    assert describe_variance("raw", v) == "On standard: the expected quantity was used."
    assert describe_variance("yield", v) == "Output matched the expected quantity."


def test_more_output_than_expected_wording():
    v = compute_variance(D("200"), D("210"), D("3"))
    assert describe_variance("yield", v) == "Output above expected by 5.00% (exceeds 3% tolerance)."


def test_unplanned_wording():
    v = compute_variance(D("0"), D("5"), D("3"))
    assert describe_variance("raw", v) == "Used although none was expected for this run (exceeds 3% tolerance)."
    assert describe_variance("yield", v) == "Output recorded although none was expected (exceeds 3% tolerance)."


def test_one_uncartoned_packet_is_singular():
    v = compute_variance(D("10"), D("9"), D("3"))
    assert "1 packet uncartoned" in describe_variance("packaging", v, uncartoned_packets=1)


def test_decimal_scrap_is_printed_plainly():
    v = compute_variance(D("10"), D("10"), D("3"))
    assert describe_variance("raw", v, scrap_qty=D("0.250")).endswith("0.25 scrapped.")
    assert describe_variance("raw", v, scrap_qty=D("0")) == "On standard: the expected quantity was used."


# --- wording: never labels a result as good or bad -------------------------------------
JUDGEMENT_WORDS = ["favourable", "favorable", "good", "bad", "saving", "waste", "loss", "gain", "better", "worse", "efficien"]


@pytest.mark.parametrize("expected, actual", [("40", "40"), ("10", "9"), ("10", "5"), ("10", "10.5"), ("10", "10.2"), ("0", "3")])
@pytest.mark.parametrize("kind", ["yield", "raw", "packaging"])
def test_wording_never_calls_a_result_good_or_bad(kind, expected, actual):
    v = compute_variance(D(expected), D(actual), D("3"))
    text = describe_variance(kind, v).lower()
    assert not any(word in text for word in JUDGEMENT_WORDS), text


def test_lower_consumption_is_described_as_under_standard_not_as_a_benefit():
    v = compute_variance(D("40"), D("30"), D("3"))
    text = describe_variance("raw", v)
    assert text.startswith("Under standard") and not any(w in text.lower() for w in JUDGEMENT_WORDS)


# --- yield and materials are separate calculations -----------------------------------
def test_yield_and_material_variances_are_independent():
    yield_v = compute_variance(D("200"), D("188"), D("3"))
    pouch_v = compute_variance(D("200"), D("194"), D("3"))
    assert yield_v.exceeds_tolerance and not pouch_v.exceeds_tolerance
    assert describe_variance("yield", yield_v) != describe_variance("packaging", yield_v)
    assert "Output" in describe_variance("yield", yield_v) and "Output" not in describe_variance("packaging", pouch_v)


# --- wording: invalid arguments -------------------------------------------------------
def test_unknown_kind_rejected():
    v = compute_variance(D("10"), D("10"), D("3"))
    with pytest.raises(VarianceError, match="'yield', 'raw' or 'packaging'"):
        describe_variance("oil", v)  # type: ignore[arg-type]


@pytest.mark.parametrize("bad", [D("-1"), D("0.0005"), 1.0, "1", True])
def test_invalid_scrap_rejected(bad):
    v = compute_variance(D("10"), D("9"), D("3"))
    with pytest.raises(InvalidQuantity, match="scrap quantity"):
        describe_variance("raw", v, scrap_qty=bad)


@pytest.mark.parametrize("bad", [-1, 1.0, D("1"), "1", True])
def test_invalid_uncartoned_count_rejected(bad):
    v = compute_variance(D("10"), D("9"), D("3"))
    with pytest.raises(InvalidQuantity, match="uncartoned"):
        describe_variance("packaging", v, uncartoned_packets=bad)


def test_uncartoned_only_applies_to_packaging_and_scrap_not_to_yield():
    v = compute_variance(D("10"), D("9"), D("3"))
    with pytest.raises(VarianceError, match="only applies to packaging"):
        describe_variance("raw", v, uncartoned_packets=2)
    with pytest.raises(VarianceError, match="does not apply to yield"):
        describe_variance("yield", v, scrap_qty=D("1"))


# --- independence ---------------------------------------------------------------------
def test_variance_module_does_not_depend_on_database_or_api_code():
    source = Path(__file__).resolve().parents[2] / "app" / "domain" / "variance.py"
    imported = set()
    for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    forbidden = {"sqlalchemy", "psycopg", "alembic", "fastapi", "starlette", "pydantic", "app", "random", "fractions"}
    assert not imported & forbidden, f"variance.py must stay pure, found: {imported & forbidden}"
