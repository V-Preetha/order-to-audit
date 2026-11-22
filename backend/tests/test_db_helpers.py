import pytest
from sqlalchemy import text

from app.db import make_engine, transaction


def test_transaction_commits_on_success(db_session, connect):
    with transaction(db_session):
        db_session.execute(text("INSERT INTO harness_probe (id, note) VALUES (10, 'kept')"))
    with connect() as other:
        assert other.execute(text("SELECT count(*) FROM harness_probe WHERE id = 10")).scalar_one() == 1


def test_transaction_rolls_back_on_error(db_session, connect):
    with pytest.raises(ValueError):
        with transaction(db_session):
            db_session.execute(text("INSERT INTO harness_probe (id, note) VALUES (11, 'lost')"))
            raise ValueError("boom")
    with connect() as other:
        assert other.execute(text("SELECT count(*) FROM harness_probe WHERE id = 11")).scalar_one() == 0


def test_make_engine_returns_a_postgres_engine(engine):
    eng = make_engine(engine.url.render_as_string(hide_password=False))
    with eng.connect() as conn:
        assert conn.execute(text("SELECT 1")).scalar_one() == 1
    eng.dispose()
