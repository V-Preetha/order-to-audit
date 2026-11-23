"""Python and PostgreSQL must round tax identically.

The expressions below are copied from the CHECK constraints in the design document
(docs/design/01-database-design.md, invoice_lines and invoices). If Python and the
database ever disagree, an invoice the domain produced would be rejected on insert.
Uses real PostgreSQL; no mocks.
"""
import random
from decimal import Decimal

import pytest
from sqlalchemy import text

from app.domain.gst import round_grand_total, split_tax

D = Decimal

SQL_INTRA_HALF = "SELECT ROUND(CAST(:t AS numeric) * CAST(:r AS numeric) / 200, 2)"   # cgst = sgst
SQL_INTER = "SELECT ROUND(CAST(:t AS numeric) * CAST(:r AS numeric) / 100, 2)"        # igst
SQL_RUPEE = "SELECT ROUND(CAST(:a AS numeric), 0)"                                     # whole-rupee option

# Hand-picked ties (exactly half a paisa) and near-ties, then a seeded random grid of integers.
TIES = [
    ("0.10", "10"), ("0.10", "5"), ("0.30", "5"), ("0.50", "1"), ("1.50", "1"),
    ("333.33", "5"), ("0.09", "5"), ("0.02", "25"), ("7500.00", "5"), ("6000.00", "5"),
    ("999999999999.99", "28"), ("0.00", "28"), ("123.45", "0"), ("100.00", "2.5"), ("100.00", "0.25"),
]


def _grid():
    rng = random.Random(20261123)  # fixed seed: reproducible, no floats involved
    random_cases = [
        (str(D(rng.randint(0, 5_000_000)).scaleb(-2).quantize(D("0.01"))),
         str(D(rng.randint(0, 2800)).scaleb(-2).quantize(D("0.01"))))
        for _ in range(400)
    ]
    return TIES + random_cases


@pytest.mark.parametrize("taxable, rate", _grid())
def test_intra_and_inter_match_postgres(db_session, taxable, rate):
    intra = split_tax(D(taxable), D(rate), "intra")
    inter = split_tax(D(taxable), D(rate), "inter")
    db_half = db_session.execute(text(SQL_INTRA_HALF), {"t": taxable, "r": rate}).scalar_one()
    db_inter = db_session.execute(text(SQL_INTER), {"t": taxable, "r": rate}).scalar_one()
    assert intra.cgst == intra.sgst == db_half
    assert inter.igst == db_inter


@pytest.mark.parametrize("amount", ["0.00", "0.49", "0.50", "0.51", "100.49", "100.50", "7875.00", "1234.56", "99999.50"])
def test_whole_rupee_rounding_matches_postgres(db_session, amount):
    total, round_off = round_grand_total(D(amount), round_to_rupee=True)
    db_total = db_session.execute(text(SQL_RUPEE), {"a": amount}).scalar_one()
    assert total == db_total
    assert round_off == db_total - D(amount)


def test_invoice_check_constraint_holds_for_the_benchmark(db_session):
    """grand_total = taxable + cgst + sgst + igst + round_off, evaluated by PostgreSQL itself."""
    split = split_tax(D("7500.00"), D("5"), "intra")
    total, round_off = round_grand_total(D("7500.00") + split.total)
    ok = db_session.execute(
        text("SELECT CAST(:g AS numeric) = CAST(:t AS numeric) + CAST(:c AS numeric) "
             "+ CAST(:s AS numeric) + CAST(:i AS numeric) + CAST(:o AS numeric)"),
        {"g": total, "t": D("7500.00"), "c": split.cgst, "s": split.sgst, "i": split.igst, "o": round_off},
    ).scalar_one()
    assert ok is True and total == D("7875.00")
