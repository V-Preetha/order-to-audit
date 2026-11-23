"""Small helpers shared by the database tests (real PostgreSQL, no mocks)."""
from contextlib import contextmanager
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

BACKEND_DIR = Path(__file__).resolve().parents[2]


def alembic_config(url: str) -> Config:
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return cfg


@contextmanager
def violates(session, error_class, constraint=None, message=None):
    """Assert that the statements inside raise `error_class` (a psycopg.errors class).

    Naming the constraint proves the *intended* rule fired, not some other error.
    The session is rolled back afterwards so the test can carry on.
    """
    with pytest.raises(DBAPIError) as caught:
        yield
    session.rollback()
    original = caught.value.orig
    assert isinstance(original, error_class), f"{type(original).__name__}: {original}"
    if constraint is not None:
        assert original.diag.constraint_name == constraint, str(original)
    if message is not None:
        assert message in str(original), str(original)


def run(session, sql, **params):
    return session.execute(text(sql), params)


def scalar(session, sql, **params):
    return session.execute(text(sql), params).scalar_one()
