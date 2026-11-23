"""The database accepts exactly the GST state codes the Python domain accepts (01-38 and 97)."""
import psycopg.errors as pge
import pytest
from sqlalchemy import text

from app.domain.gst import InvalidStateCode, supply_type
from tests.db.helpers import violates


def _python_accepts(code):
    try:
        supply_type("33", code)
        return True
    except InvalidStateCode:
        return False


@pytest.mark.parametrize("code", [f"{i:02d}" for i in range(100)])
def test_customer_state_code_matches_python(db_session, code):
    accepted_by_python = _python_accepts(code)
    if accepted_by_python:
        db_session.execute(text("INSERT INTO customers (name, customer_type, state_code) VALUES ('X', 'retailer', :s)"), {"s": code})
        db_session.rollback()
    else:
        with violates(db_session, pge.CheckViolation, "ck_customers_state_code"):
            db_session.execute(text("INSERT INTO customers (name, customer_type, state_code) VALUES ('X', 'retailer', :s)"), {"s": code})
