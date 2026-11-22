"""Real-PostgreSQL fixtures. A throwaway database is created per test session, migrated
with Alembic, and dropped at the end. Nothing here ever connects to DATABASE_URL.

Tests use real commits and separate connections: deferred constraint triggers fire only
at COMMIT, so rollback-wrapped tests cannot exercise them. Isolation comes from
truncating between tests instead.
"""
import os
import uuid
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from tests.safety import DB_PREFIX, MARKER_TABLE, assert_disposable, resolve_admin_url, truncate_disposable

BACKEND_DIR = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def pg_admin_url():
    return resolve_admin_url(os.environ)


@pytest.fixture(scope="session")
def engine(pg_admin_url):
    dbname = f"{DB_PREFIX}{uuid.uuid4().hex[:10]}"
    admin = create_engine(pg_admin_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{dbname}"'))
    url = pg_admin_url.rsplit("/", 1)[0] + "/" + dbname
    eng = create_engine(url, pool_pre_ping=True)
    try:
        with eng.begin() as conn:
            conn.execute(text(f"CREATE TABLE {MARKER_TABLE} (token text)"))
        cfg = Config(str(BACKEND_DIR / "alembic.ini"))
        cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
        cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
        command.upgrade(cfg, "head")
        with eng.begin() as conn:
            assert_disposable(conn)
            conn.execute(text("CREATE TABLE harness_probe (id integer PRIMARY KEY, note text)"))
        yield eng
    finally:
        eng.dispose()
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{dbname}" WITH (FORCE)'))
        admin.dispose()


@pytest.fixture
def db_session(engine):
    session = Session(engine)
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def connect(engine):
    """Open an independent connection: `with connect() as other:`."""
    return engine.connect


@pytest.fixture(autouse=True)
def clean_db(request):
    yield
    if "engine" in request.fixturenames:  # pure tests need no database
        truncate_disposable(request.getfixturevalue("engine"))
