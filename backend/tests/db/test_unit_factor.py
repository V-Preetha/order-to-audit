"""PostgreSQL unit_factor() must agree with domain.units, pair for pair."""
from decimal import Decimal
from itertools import product

import pytest
from sqlalchemy import text

from app.domain.units import UNIT_FACTORS, IncompatibleUnits, convert

UNITS = sorted({unit for unit, other in UNIT_FACTORS if unit == other})
ALL_PAIRS = list(product(UNITS, UNITS))


def _db_factor(session, f, t):
    return session.execute(text("SELECT unit_factor(CAST(:f AS unit_code), CAST(:t AS unit_code))"), {"f": f, "t": t}).scalar_one()


def test_enum_holds_exactly_the_python_units(db_session):
    labels = db_session.execute(text("SELECT unnest(enum_range(NULL::unit_code))::text")).scalars().all()
    assert sorted(labels) == UNITS


@pytest.mark.parametrize("f, t", ALL_PAIRS)
def test_sql_and_python_unit_factors_agree(db_session, f, t):
    expected = UNIT_FACTORS.get((f, t))
    actual = _db_factor(db_session, f, t)
    if expected is None:
        assert actual is None, f"{f}->{t}: Python says incompatible, SQL returned {actual}"
        with pytest.raises(IncompatibleUnits):
            convert(Decimal("1"), f, t)
    else:
        assert actual == expected, f"{f}->{t}: SQL {actual} != Python {expected}"


def test_every_python_factor_is_covered_by_the_pair_grid():
    assert set(UNIT_FACTORS) <= set(ALL_PAIRS)


def test_kilogram_to_litre_is_null(db_session):
    assert _db_factor(db_session, "kg", "l") is None


def test_benchmark_conversion_through_the_sql_factor(db_session):
    """200 g of spice is 0.2 kg whichever side does the arithmetic."""
    assert convert(Decimal("200"), "g", "kg") == Decimal("200") * _db_factor(db_session, "g", "kg")


def test_unit_factor_is_immutable(db_session):
    volatility = db_session.execute(text("SELECT provolatile FROM pg_proc WHERE proname = 'unit_factor'")).scalar_one()
    assert volatility == "i"
