"""GST split, Indian financial year, invoice numbers. Pure domain logic: no database, no floats."""
import ast
from dataclasses import FrozenInstanceError
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from app.domain.gst import (
    GstError,
    InvalidAmount,
    InvalidDate,
    InvalidInvoiceNumber,
    InvalidStateCode,
    InvalidTaxRate,
    TaxSplit,
    financial_year,
    format_invoice_no,
    round_grand_total,
    split_tax,
    supply_type,
    to_ist_date,
)

D = Decimal
UTC = timezone.utc


# --- benchmark invoices ---------------------------------------------------------------
def test_intra_state_split():
    assert split_tax(D("7500"), D("5"), "intra") == TaxSplit(D("187.50"), D("187.50"), D("0"))


def test_inter_state_igst():
    assert split_tax(D("6000"), D("5"), "inter").igst == D("300.00")


def test_benchmark_retailer_a_invoice_is_7875():
    taxable = D("150") * D("50")  # 150 packets at Rs 50
    split = split_tax(taxable, D("5"), supply_type("33", "33"))
    assert (taxable, split.cgst, split.sgst, split.igst) == (D("7500"), D("187.50"), D("187.50"), D("0"))
    assert round_grand_total(taxable + split.total) == (D("7875.00"), D("0.00"))


def test_benchmark_distributor_b_would_be_igst_6300():
    taxable = D("120") * D("50")
    split = split_tax(taxable, D("5"), supply_type("33", "29"))
    assert (split.cgst, split.sgst, split.igst) == (D("0"), D("0"), D("300.00"))
    assert round_grand_total(taxable + split.total) == (D("6300.00"), D("0.00"))


def test_split_amounts_always_carry_two_decimal_places():
    split = split_tax(D("7500"), D("5"), "intra")
    assert [x.as_tuple().exponent for x in (split.cgst, split.sgst, split.igst)] == [-2, -2, -2]


def test_tax_split_total_and_immutability():
    split = split_tax(D("7500"), D("5"), "intra")
    assert split.total == D("375.00")
    with pytest.raises(FrozenInstanceError):
        split.cgst = D("1")  # type: ignore[misc]


# --- rounding edge cases --------------------------------------------------------------
def test_per_component_rounding_is_half_up_on_each_component():
    # 0.10 at 10% intra: each half is 0.005 exactly, which rounds up to 0.01 per component.
    assert split_tax(D("0.10"), D("10"), "intra") == TaxSplit(D("0.01"), D("0.01"), D("0"))
    # Just below the tie: 0.09 at 5% -> 0.00225 per component -> 0.00.
    assert split_tax(D("0.09"), D("5"), "intra") == TaxSplit(D("0.00"), D("0.00"), D("0"))


def test_inter_state_tie_rounds_up():
    assert split_tax(D("0.10"), D("5"), "inter").igst == D("0.01")  # 0.005 -> 0.01
    assert split_tax(D("0.09"), D("5"), "inter").igst == D("0.00")  # 0.0045 -> 0.00


def test_odd_paisa_case_rounds_each_component_separately():
    # 333.33 at 5%: intra halves are 8.33325 -> 8.33 each; inter is 16.6665 -> 16.67.
    intra = split_tax(D("333.33"), D("5"), "intra")
    inter = split_tax(D("333.33"), D("5"), "inter")
    assert (intra.cgst, intra.sgst) == (D("8.33"), D("8.33"))
    assert inter.igst == D("16.67")
    assert intra.total == D("16.66")  # one paisa below inter: the documented per-component effect


def test_fractional_rates_are_exact():
    assert split_tax(D("100.00"), D("2.5"), "intra") == TaxSplit(D("1.25"), D("1.25"), D("0"))
    assert split_tax(D("100.00"), D("0.25"), "inter").igst == D("0.25")


def test_largest_storable_amount_is_exact():
    # 999999999999.99 x 28% = 279999999999.9972 exactly, which rounds up to a whole 280000000000.00.
    assert split_tax(D("999999999999.99"), D("28"), "inter").igst == D("280000000000.00")


# --- zero-rated and zero-value supplies ------------------------------------------------
@pytest.mark.parametrize("supply", ["intra", "inter"])
def test_zero_rated_supply_has_no_tax(supply):
    assert split_tax(D("7500"), D("0"), supply) == TaxSplit(D("0.00"), D("0.00"), D("0.00"))


@pytest.mark.parametrize("supply", ["intra", "inter"])
def test_zero_value_supply_has_no_tax(supply):
    assert split_tax(D("0"), D("5"), supply).total == D("0.00")


# --- invalid tax rates -----------------------------------------------------------------
@pytest.mark.parametrize("bad", [D("-1"), D("-0.01"), D("28.01"), D("100")])
def test_rate_outside_zero_to_28_rejected(bad):
    with pytest.raises(InvalidTaxRate, match="between 0 and 28"):
        split_tax(D("100"), bad, "intra")


def test_rate_with_more_than_two_decimals_rejected():
    with pytest.raises(InvalidTaxRate, match="2 decimal"):
        split_tax(D("100"), D("5.005"), "intra")


@pytest.mark.parametrize("bad", [D("NaN"), D("Infinity"), D("-Infinity")])
def test_non_finite_rate_rejected(bad):
    with pytest.raises(InvalidTaxRate, match="finite"):
        split_tax(D("100"), bad, "intra")


@pytest.mark.parametrize("bad", [5.0, "5", None, True])
def test_non_decimal_rate_rejected(bad):
    with pytest.raises(InvalidTaxRate, match="Decimal"):
        split_tax(D("100"), bad, "intra")


def test_float_rate_message_explains_why():
    with pytest.raises(InvalidTaxRate, match="float"):
        split_tax(D("100"), 5.0, "intra")


def test_int_rate_is_accepted():
    assert split_tax(D("100"), 5, "inter").igst == D("5.00")


# --- invalid amounts and supply -------------------------------------------------------
def test_negative_taxable_rejected():
    with pytest.raises(InvalidAmount, match="negative"):
        split_tax(D("-1"), D("5"), "intra")


def test_taxable_must_be_whole_paise():
    with pytest.raises(InvalidAmount, match="paise"):
        split_tax(D("10.001"), D("5"), "intra")


def test_taxable_larger_than_the_database_column_rejected():
    with pytest.raises(InvalidAmount, match="999999999999.99"):
        split_tax(D("1000000000000"), D("5"), "intra")


@pytest.mark.parametrize("bad", [100.0, "100", None, D("NaN"), D("Infinity")])
def test_invalid_taxable_rejected(bad):
    with pytest.raises(InvalidAmount):
        split_tax(bad, D("5"), "intra")


@pytest.mark.parametrize("bad", ["", "INTRA", "interstate", None])
def test_unknown_supply_type_rejected(bad):
    with pytest.raises(GstError, match="'intra' or 'inter'"):
        split_tax(D("100"), D("5"), bad)


def test_all_gst_errors_are_value_errors():
    for exc in (InvalidAmount, InvalidDate, InvalidInvoiceNumber, InvalidStateCode, InvalidTaxRate):
        assert issubclass(exc, GstError) and issubclass(GstError, ValueError)


# --- supply type from state codes -----------------------------------------------------
def test_supply_type_tn_vs_ka():
    assert supply_type("33", "33") == "intra"
    assert supply_type("33", "29") == "inter"


@pytest.mark.parametrize("code", ["01", "09", "33", "37", "38", "97"])
def test_valid_state_codes_accepted(code):
    assert supply_type(code, code) == "intra"


@pytest.mark.parametrize(
    "bad", ["", "3", "333", "AB", "3A", "00", "39", "98", " 33", "33 ", "٣٣", None, 33, 3.3]
)
def test_incorrect_state_codes_rejected(bad):
    with pytest.raises(InvalidStateCode, match="state code"):
        supply_type("33", bad)
    with pytest.raises(InvalidStateCode, match="state code"):
        supply_type(bad, "33")


def test_state_code_message_shows_what_was_given_and_an_example():
    with pytest.raises(InvalidStateCode) as caught:
        supply_type("33", "3")
    assert "'3'" in str(caught.value) and "'33'" in str(caught.value)


# --- financial year (April to March) ---------------------------------------------------
def test_financial_year_boundaries():
    assert financial_year(date(2027, 3, 31)) == "2026-27"
    assert financial_year(date(2027, 4, 1)) == "2027-28"


@pytest.mark.parametrize(
    "d, expected",
    [
        (date(2026, 4, 1), "2026-27"),
        (date(2026, 12, 31), "2026-27"),
        (date(2027, 1, 1), "2026-27"),
        (date(2099, 4, 1), "2099-00"),   # two-digit end wraps at the century
        (date(2100, 3, 31), "2099-00"),
    ],
)
def test_financial_year_examples(d, expected):
    assert financial_year(d) == expected


def test_financial_year_rejects_a_datetime():
    with pytest.raises(InvalidDate, match="to_ist_date"):
        financial_year(datetime(2027, 3, 31, 12, 0))


@pytest.mark.parametrize("bad", ["2027-03-31", None, 20270331])
def test_financial_year_rejects_non_dates(bad):
    with pytest.raises(InvalidDate, match="date"):
        financial_year(bad)


# --- Asia/Kolkata handling ------------------------------------------------------------
def test_ist_conversion_crosses_midnight():
    assert to_ist_date(datetime(2027, 3, 31, 19, 0, tzinfo=UTC)) == date(2027, 4, 1)


def test_ist_midnight_is_exactly_1830_utc():
    assert to_ist_date(datetime(2027, 3, 31, 18, 29, 59, tzinfo=UTC)) == date(2027, 3, 31)
    assert to_ist_date(datetime(2027, 3, 31, 18, 30, 0, tzinfo=UTC)) == date(2027, 4, 1)


def test_invoice_just_before_and_after_the_new_year_gets_the_right_fy():
    last_moment = datetime(2027, 3, 31, 23, 59, 59, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    first_moment = datetime(2027, 4, 1, 0, 0, 0, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    assert financial_year(to_ist_date(last_moment)) == "2026-27"
    assert financial_year(to_ist_date(first_moment)) == "2027-28"


def test_any_timezone_is_converted_to_the_ist_calendar_day():
    new_york = timezone(timedelta(hours=-5))
    # 15:00 at UTC-5 is 20:00 UTC, which is 01:30 IST on 1 April; 09:00 is still 31 March in India.
    assert to_ist_date(datetime(2027, 3, 31, 15, 0, tzinfo=new_york)) == date(2027, 4, 1)
    assert to_ist_date(datetime(2027, 3, 31, 9, 0, tzinfo=new_york)) == date(2027, 3, 31)


def test_naive_timestamp_rejected_because_its_day_is_ambiguous():
    with pytest.raises(InvalidDate, match="timezone"):
        to_ist_date(datetime(2027, 3, 31, 19, 0))


@pytest.mark.parametrize("bad", [date(2027, 3, 31), "2027-03-31", None])
def test_to_ist_date_rejects_non_datetimes(bad):
    with pytest.raises(InvalidDate, match="datetime"):
        to_ist_date(bad)


def test_ist_offset_agrees_with_the_tz_database():
    zoneinfo = pytest.importorskip("zoneinfo")
    try:
        kolkata = zoneinfo.ZoneInfo("Asia/Kolkata")
    except zoneinfo.ZoneInfoNotFoundError:
        pytest.skip("tz database not installed on this machine")
    start = datetime(2025, 1, 1, tzinfo=UTC)
    for hours in range(0, 24 * 365 * 6, 7):  # every 7 hours for six years
        ts = start + timedelta(hours=hours)
        assert to_ist_date(ts) == ts.astimezone(kolkata).date()
    for year in range(2025, 2031):  # and the exact FY boundary instants
        for stamp in (datetime(year, 3, 31, 18, 29, 59, tzinfo=UTC), datetime(year, 3, 31, 18, 30, tzinfo=UTC)):
            assert to_ist_date(stamp) == stamp.astimezone(kolkata).date()


# --- invoice numbers -------------------------------------------------------------------
def test_invoice_number_format():
    number = format_invoice_no("2026-27", 1)
    assert number == "INV/26-27/0001"
    assert len(number) <= 16


@pytest.mark.parametrize("seq, expected", [(12, "INV/26-27/0012"), (9999, "INV/26-27/9999"), (10000, "INV/26-27/10000")])
def test_sequence_is_zero_padded_to_four_digits(seq, expected):
    assert format_invoice_no("2026-27", seq) == expected


def test_largest_sequence_that_fits_is_exactly_sixteen_characters():
    number = format_invoice_no("2026-27", 999_999)
    assert number == "INV/26-27/999999" and len(number) == 16


def test_sequence_that_would_exceed_sixteen_characters_rejected():
    with pytest.raises(InvalidInvoiceNumber, match="16 characters"):
        format_invoice_no("2026-27", 1_000_000)


@pytest.mark.parametrize("bad", [0, -1, 1.0, "1", None, True])
def test_invalid_sequence_rejected(bad):
    with pytest.raises(InvalidInvoiceNumber, match="sequence"):
        format_invoice_no("2026-27", bad)


@pytest.mark.parametrize("bad", ["2026", "26-27", "2026-2027", "2026-28", "2026/27", "", None, 2026])
def test_invalid_financial_year_rejected(bad):
    with pytest.raises(InvalidInvoiceNumber, match="financial year"):
        format_invoice_no(bad, 1)


def test_century_wrap_financial_year_formats():
    assert format_invoice_no("2099-00", 1) == "INV/99-00/0001"


def test_invoice_number_uses_only_characters_the_gst_rules_allow():
    assert all(c.isalnum() or c in "/-" for c in format_invoice_no("2026-27", 123))


# --- grand total: paise by default -----------------------------------------------------
def test_default_keeps_paise():
    assert round_grand_total(D("7875.00")) == (D("7875.00"), D("0.00"))
    assert round_grand_total(D("100.49")) == (D("100.49"), D("0.00"))


def test_default_never_rounds_a_half_rupee():
    assert round_grand_total(D("100.50")) == (D("100.50"), D("0.00"))


def test_explicit_rupee_rounding():
    assert round_grand_total(D("100.49"), round_to_rupee=True) == (D("100.00"), D("-0.49"))
    assert round_grand_total(D("100.50"), round_to_rupee=True) == (D("101.00"), D("0.50"))
    assert round_grand_total(D("7875.00"), round_to_rupee=True) == (D("7875.00"), D("0.00"))


def test_rupee_rounding_result_invariant():
    for amount in (D("0.50"), D("0.49"), D("1234.56"), D("99.99")):
        total, off = round_grand_total(amount, round_to_rupee=True)
        assert total == amount + off and total == total.to_integral_value()


def test_round_off_has_two_decimal_places():
    total, off = round_grand_total(D("100.49"), round_to_rupee=True)
    assert total.as_tuple().exponent == -2 and off.as_tuple().exponent == -2


def test_amount_is_normalised_to_two_places():
    assert round_grand_total(D("100.5"))[0].as_tuple().exponent == -2


def test_rounding_flag_must_be_a_real_boolean():
    with pytest.raises(TypeError, match="round_to_rupee"):
        round_grand_total(D("100"), round_to_rupee="yes")  # type: ignore[arg-type]


def test_rounding_flag_is_keyword_only():
    with pytest.raises(TypeError):
        round_grand_total(D("100"), True)  # type: ignore[misc]


def test_grand_total_must_be_whole_paise_and_non_negative():
    with pytest.raises(InvalidAmount, match="paise"):
        round_grand_total(D("100.123"))
    with pytest.raises(InvalidAmount, match="negative"):
        round_grand_total(D("-1"))


# --- independence ---------------------------------------------------------------------
def test_gst_module_does_not_depend_on_database_or_api_code():
    source = Path(__file__).resolve().parents[2] / "app" / "domain" / "gst.py"
    imported = set()
    for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    forbidden = {"sqlalchemy", "psycopg", "alembic", "fastapi", "starlette", "pydantic", "app"}
    assert not imported & forbidden, f"gst.py must stay pure, found: {imported & forbidden}"
