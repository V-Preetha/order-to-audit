"""Weighted-average issue value, per-run cost, shared overhead allocation.

Pure domain logic: no database, no floats. Benchmark numbers must emerge from the
functions, never be typed in as expected inputs.
"""
import ast
import random
from decimal import Decimal
from pathlib import Path

import pytest

from app.domain.costing import (
    CostingError,
    InsufficientStock,
    InvalidCost,
    InvalidQuantity,
    ZeroOutput,
    allocate_shared,
    run_cost,
    wac_issue_value,
    wac_rate,
)

D = Decimal


# --- benchmark ------------------------------------------------------------------------
def test_wac_rate_flour_is_43_125():
    # Opening 100 kg @ 42 plus a purchase of 60 kg @ 45 = 160 kg worth 6,900.
    rate = wac_rate(D("160"), D("6900.00"))
    assert rate == D("43.1250") and rate.as_tuple().exponent == -4


def test_wac_flour_issue():
    issue = wac_issue_value(D("40"), D("160"), D("6900.00"))
    assert issue == D("1725.00") and issue.as_tuple().exponent == -2


def test_benchmark_materials_are_4055_and_remaining_stock_is_9745():
    # item: (kg/L/pc issued, on hand before, value on hand before). Issued amounts follow the
    # benchmark run: flour 40, oil 10.5 (+5%), spice 1.2, pouches 194, cartons 9.
    stock = {
        "flour": (D("40"), D("160"), D("6900.00")),
        "oil": (D("10.5"), D("30"), D("4200.00")),
        "spice": (D("1.2"), D("5"), D("1000.00")),
        "pouches": (D("194"), D("500"), D("1250.00")),
        "cartons": (D("9"), D("30"), D("450.00")),
    }
    issues = {name: wac_issue_value(*args) for name, args in stock.items()}
    assert issues == {
        "flour": D("1725.00"), "oil": D("1470.00"), "spice": D("240.00"),
        "pouches": D("485.00"), "cartons": D("135.00"),
    }
    materials = sum(issues.values(), D("0.00"))
    remaining = sum((value - issues[name] for name, (_, _, value) in stock.items()), D("0.00"))
    assert materials == D("4055.00")
    assert remaining == D("9745.00")


def test_benchmark_run_cost():
    cost = run_cost(D("4055.00"), D("1800.00"), D("188"))
    assert cost.materials == D("4055.00") and cost.overheads == D("1800.00")
    assert cost.total == D("5855.00")
    assert cost.per_packet == D("31.1436")
    assert cost.per_packet_display == D("31.14")


# --- issue value: rounding and residue -----------------------------------------------
def test_issue_that_empties_stock_takes_remaining_value():
    assert wac_issue_value(D("3"), D("3"), D("100.00")) == D("100.00")
    assert wac_issue_value(D("7"), D("7"), D("33.33")) == D("33.33")


@pytest.mark.parametrize(
    "qty, value, step",
    [("3", "100.00", "1"), ("7", "100.01", "1"), ("9", "0.10", "2"), ("10", "999.99", "3"), ("0.3", "0.07", "0.1")],
)
def test_wac_rounding_no_residue(qty, value, step):
    """Issue the pool in awkward steps until empty: the issues must add back to the pool exactly."""
    on_qty, on_val, issued = D(qty), D(value), []
    while on_qty > 0:
        take = min(D(step), on_qty)
        v = wac_issue_value(take, on_qty, on_val)
        assert D("0") <= v <= on_val
        issued.append(v)
        on_qty, on_val = on_qty - take, on_val - v
    assert on_val == D("0.00")
    assert sum(issued, D("0.00")) == D(value)


def test_half_up_rounding_on_a_tie():
    assert wac_issue_value(D("1.5"), D("3"), D("0.03")) == D("0.02")  # 0.015 -> 0.02


def test_tie_is_computed_from_the_exact_ratio_not_from_a_rounded_average():
    # The average is 0.0033333...; rounding it first would lose the exact tie 0.005.
    assert wac_issue_value(D("1.5"), D("3"), D("0.01")) == D("0.01")


def test_stock_with_no_value_issues_at_zero_cost():
    assert wac_issue_value(D("1"), D("10"), D("0.00")) == D("0.00")


# --- issue value: invalid input --------------------------------------------------------
@pytest.mark.parametrize("bad", [D("0"), D("-1"), D("1.0005"), 1.5, "1", None, True, D("NaN"), D("Infinity")])
def test_invalid_issue_quantity_rejected(bad):
    with pytest.raises(InvalidQuantity):
        wac_issue_value(bad, D("10"), D("100.00"))


def test_issue_quantity_messages_are_specific():
    with pytest.raises(InvalidQuantity, match="greater than zero"):
        wac_issue_value(D("0"), D("10"), D("100.00"))
    with pytest.raises(InvalidQuantity, match="negative"):
        wac_issue_value(D("-1"), D("10"), D("100.00"))
    with pytest.raises(InvalidQuantity, match="3 decimal"):
        wac_issue_value(D("1.0005"), D("10"), D("100.00"))
    with pytest.raises(InvalidQuantity, match="float"):
        wac_issue_value(1.5, D("10"), D("100.00"))


def test_negative_stock_quantity_rejected():
    with pytest.raises(InvalidQuantity, match="negative"):
        wac_issue_value(D("1"), D("-10"), D("100.00"))


@pytest.mark.parametrize("bad", [D("-0.01"), D("100.001"), 100.0, "100.00", None, D("NaN")])
def test_invalid_stock_value_rejected(bad):
    with pytest.raises(InvalidCost):
        wac_issue_value(D("1"), D("10"), bad)


def test_negative_cost_message():
    with pytest.raises(InvalidCost, match="negative"):
        wac_issue_value(D("1"), D("10"), D("-5.00"))


def test_cannot_issue_more_than_is_on_hand():
    with pytest.raises(InsufficientStock, match="Cannot issue 11 with only 10 on hand"):
        wac_issue_value(D("11"), D("10"), D("100.00"))


def test_zero_inventory_cannot_be_issued_from():
    with pytest.raises(InsufficientStock, match="only 0 on hand"):
        wac_issue_value(D("1"), D("0"), D("0.00"))


def test_empty_stock_that_still_carries_value_is_reported_as_inconsistent():
    with pytest.raises(CostingError, match="inconsistent"):
        wac_issue_value(D("1"), D("0"), D("5.00"))
    with pytest.raises(CostingError, match="inconsistent"):
        wac_rate(D("0"), D("5.00"))


def test_no_average_rate_without_stock():
    with pytest.raises(InsufficientStock, match="no average cost"):
        wac_rate(D("0"), D("0.00"))


def test_costing_errors_are_value_errors():
    for exc in (InsufficientStock, InvalidCost, InvalidQuantity, ZeroOutput):
        assert issubclass(exc, CostingError) and issubclass(CostingError, ValueError)


# --- run cost -------------------------------------------------------------------------
def test_per_packet_is_rounded_half_up_to_four_places():
    assert run_cost(D("0.01"), D("0.00"), D("8")).per_packet == D("0.0013")  # 0.00125 -> 0.0013


def test_display_value_comes_from_the_exact_cost_not_the_rounded_four_place_one():
    # 4449.96 / 10000 = 0.444996: four places give 0.4450, but the true 2-place value is 0.44.
    cost = run_cost(D("4449.96"), D("0.00"), D("10000"))
    assert cost.per_packet == D("0.4450")
    assert cost.per_packet_display == D("0.44")


def test_run_with_no_cost_has_zero_per_packet_cost():
    assert run_cost(D("0.00"), D("0.00"), D("188")).per_packet == D("0.0000")


def test_zero_output_raises():
    with pytest.raises(ZeroOutput, match="zero good packets"):
        run_cost(D("4055.00"), D("1800.00"), D("0"))


@pytest.mark.parametrize("field, args", [
    ("materials", (D("-0.01"), D("1800.00"), D("188"))),
    ("overheads", (D("4055.00"), D("-0.01"), D("188"))),
])
def test_negative_costs_rejected(field, args):
    with pytest.raises(InvalidCost, match=f"{field}.*negative"):
        run_cost(*args)


@pytest.mark.parametrize("bad", [D("-1"), D("188.5"), 188.0, "188", None, D("NaN"), True])
def test_invalid_good_packet_count_rejected(bad):
    with pytest.raises(InvalidQuantity, match="good packets"):
        run_cost(D("4055.00"), D("1800.00"), bad)


@pytest.mark.parametrize("bad", [D("10.001"), 10.5, "10.00", None])
def test_costs_must_be_exact_paise(bad):
    with pytest.raises(InvalidCost):
        run_cost(bad, D("0.00"), D("1"))
    with pytest.raises(InvalidCost):
        run_cost(D("0.00"), bad, D("1"))


# --- shared overhead allocation (largest remainder) ------------------------------------
def test_allocate_shared_sums_to_pool():
    shares = allocate_shared(D("100.00"), {1: D("1"), 2: D("1"), 3: D("1")})
    assert sum(shares.values()) == D("100.00")
    # The single leftover paisa goes to the lowest id on a tie: deterministic, not random.
    assert shares == {1: D("33.34"), 2: D("33.33"), 3: D("33.33")}


def test_allocation_follows_the_weights():
    shares = allocate_shared(D("1000.00"), {1: D("4055.00"), 2: D("2000.00")})
    assert shares == {1: D("669.69"), 2: D("330.31")}  # exact 669.694..., 330.305...: leftover goes to the larger remainder
    assert sum(shares.values()) == D("1000.00")


def test_result_does_not_depend_on_input_order():
    weights = {7: D("1"), 3: D("1"), 5: D("1"), 1: D("2")}
    forward = allocate_shared(D("10.01"), weights)
    backward = allocate_shared(D("10.01"), dict(reversed(list(weights.items()))))
    assert forward == backward and sum(forward.values()) == D("10.01")


def test_zero_weight_gets_nothing_and_zero_pool_allocates_zero():
    assert allocate_shared(D("10.00"), {1: D("0"), 2: D("5")}) == {1: D("0.00"), 2: D("10.00")}
    assert allocate_shared(D("0.00"), {1: D("1"), 2: D("3")}) == {1: D("0.00"), 2: D("0.00")}


def test_allocation_amounts_have_two_places():
    shares = allocate_shared(D("10"), {1: D("1"), 2: D("2")})
    assert all(v.as_tuple().exponent == -2 for v in shares.values())


def test_allocation_invariants_hold_for_many_pools_and_weights():
    rng = random.Random(20261123)  # fixed seed; integers only
    for _ in range(300):
        pool = D(rng.randint(0, 10_000_000)).scaleb(-2)
        weights = {i: D(rng.randint(0, 50_000)).scaleb(-2) for i in range(rng.randint(1, 8))}
        if sum(weights.values()) == 0:
            continue
        shares = allocate_shared(pool, weights)
        assert sum(shares.values()) == pool
        total_w = sum(weights.values())
        for key, share in shares.items():
            assert share >= 0
            # Within one paisa of the exact proportional share, checked without division.
            assert abs(share * total_w - pool * weights[key]) < total_w * D("0.01")


def test_allocation_rejects_unusable_input():
    with pytest.raises(CostingError, match="no runs"):
        allocate_shared(D("10.00"), {})
    with pytest.raises(CostingError, match="total weight is zero"):
        allocate_shared(D("10.00"), {1: D("0"), 2: D("0")})
    with pytest.raises(InvalidCost, match="negative"):
        allocate_shared(D("10.00"), {1: D("-1"), 2: D("2")})
    with pytest.raises(InvalidCost, match="negative"):
        allocate_shared(D("-10.00"), {1: D("1")})
    with pytest.raises(InvalidCost, match="paise"):
        allocate_shared(D("10.001"), {1: D("1")})
    with pytest.raises(InvalidCost, match="float"):
        allocate_shared(D("10.00"), {1: 1.5})


# --- independence ---------------------------------------------------------------------
def test_costing_module_does_not_depend_on_database_or_api_code():
    source = Path(__file__).resolve().parents[2] / "app" / "domain" / "costing.py"
    imported = set()
    for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    forbidden = {"sqlalchemy", "psycopg", "alembic", "fastapi", "starlette", "pydantic", "app", "random", "fractions"}
    assert not imported & forbidden, f"costing.py must stay pure, found: {imported & forbidden}"


# --- found in independent review: bounds, precision and negative zero -------------------
@pytest.mark.parametrize("call", [
    lambda: wac_issue_value(D("1E+100"), D("1"), D("1.00")),
    lambda: wac_rate(D("1E+100"), D("1.00")),
    lambda: run_cost(D("1E+100"), D("0.00"), D("1")),
    lambda: run_cost(D("1.00"), D("0.00"), D("1E+100")),
    lambda: wac_issue_value(D("1"), D("10"), D("1E+1000000")),
])
def test_absurdly_large_values_raise_costing_errors_not_decimal_errors(call):
    with pytest.raises(CostingError, match="cannot exceed"):
        call()


def test_run_total_cannot_exceed_the_database_column():
    with pytest.raises(InvalidCost, match="total"):
        run_cost(D("999999999999.99"), D("999999999999.99"), D("1"))


def test_weights_are_money_so_precision_is_never_lost():
    # 10**61 and 10**61 + 1 differ by 1 part in 10**61, beyond 60 digits of precision.
    with pytest.raises(InvalidCost, match="cannot exceed"):
        allocate_shared(D("0.01"), {1: D(10**61), 2: D(10**61 + 1)})


def test_exponent_form_weights_work():
    assert allocate_shared(D("10.00"), {1: D("1E+3"), 2: D("1000.00")}) == {1: D("5.00"), 2: D("5.00")}


def test_negative_zero_is_normalised_in_inputs_and_results():
    cost = run_cost(D("-0"), D("-0"), D("1"))
    assert [str(x) for x in (cost.materials, cost.overheads, cost.total, cost.per_packet)] == ["0.00", "0.00", "0.00", "0.0000"]
    assert str(wac_issue_value(D("5"), D("5"), D("-0"))) == "0.00"
    assert str(wac_rate(D("5"), D("-0"))) == "0.0000"
