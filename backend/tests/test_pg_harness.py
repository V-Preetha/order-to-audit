"""Proves the harness talks to a real PostgreSQL with real commits, locks and isolation.

Every critical inventory test relies on these properties, so they are checked once,
up front, without mocks.
"""
import uuid

import psycopg
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError

from tests.safety import UnsafeTestDatabase, assert_disposable, truncate_disposable


def test_server_is_real_postgres(db_session):
    version = db_session.execute(text("SHOW server_version_num")).scalar_one()
    assert int(version) >= 140000


def test_migrations_ran_in_the_disposable_database(db_session):
    from alembic.script import ScriptDirectory
    from tests.db.helpers import alembic_config
    head = ScriptDirectory.from_config(alembic_config("postgresql://unused/unused")).get_current_head()
    rows = db_session.execute(text("SELECT version_num FROM alembic_version")).all()
    assert [r[0] for r in rows] == [head]


def test_committed_row_visible_to_second_connection(db_session, connect):
    db_session.execute(text("INSERT INTO harness_probe (id, note) VALUES (1, 'committed')"))
    db_session.commit()
    with connect() as other:
        note = other.execute(text("SELECT note FROM harness_probe WHERE id = 1")).scalar_one()
    assert note == "committed"


def test_uncommitted_row_invisible_to_second_connection(db_session, connect):
    db_session.execute(text("INSERT INTO harness_probe (id, note) VALUES (2, 'pending')"))
    with connect() as other:
        count = other.execute(text("SELECT count(*) FROM harness_probe WHERE id = 2")).scalar_one()
    assert count == 0
    db_session.rollback()


def test_rollback_discards_the_row(db_session):
    db_session.execute(text("INSERT INTO harness_probe (id, note) VALUES (3, 'gone')"))
    db_session.rollback()
    assert db_session.execute(text("SELECT count(*) FROM harness_probe WHERE id = 3")).scalar_one() == 0


def test_row_lock_blocks_second_connection(db_session, connect):
    db_session.execute(text("INSERT INTO harness_probe (id, note) VALUES (4, 'lock me')"))
    db_session.commit()
    db_session.execute(text("SELECT id FROM harness_probe WHERE id = 4 FOR UPDATE"))  # lock held
    with connect() as other:
        other.execute(text("SET LOCAL lock_timeout = '200ms'"))
        with pytest.raises(OperationalError) as caught:
            other.execute(text("SELECT id FROM harness_probe WHERE id = 4 FOR UPDATE"))
    assert isinstance(caught.value.orig, psycopg.errors.LockNotAvailable)
    db_session.rollback()


def test_assert_disposable_accepts_the_harness_database(engine):
    with engine.connect() as conn:
        assert_disposable(conn)


def test_truncate_disposable_empties_tables_but_keeps_alembic_version(db_session, engine):
    db_session.execute(text("INSERT INTO harness_probe (id, note) VALUES (5, 'wipe me')"))
    db_session.commit()
    truncate_disposable(engine)
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM harness_probe")).scalar_one() == 0
        assert conn.execute(text("SELECT count(*) FROM alembic_version")).scalar_one() == 1


def test_clean_db_runs_between_tests_part_1(db_session):
    db_session.execute(text("INSERT INTO harness_probe (id, note) VALUES (6, 'left behind')"))
    db_session.commit()


def test_clean_db_runs_between_tests_part_2(db_session):
    # Part 1 committed a row; the autouse clean_db fixture must have removed it.
    assert db_session.execute(text("SELECT count(*) FROM harness_probe")).scalar_one() == 0


def _admin_engine(pg_admin_url):
    return create_engine(pg_admin_url, isolation_level="AUTOCOMMIT")


def _server_url_for(pg_admin_url, dbname):
    return pg_admin_url.rsplit("/", 1)[0] + "/" + dbname


@pytest.mark.parametrize(
    "make_marker, name_prefix, expected",
    [(False, "o2a_test_", "marker"), (True, "plain_", "o2a_test_")],
)
def test_clean_db_refuses_unsafe_databases(pg_admin_url, make_marker, name_prefix, expected):
    dbname = f"{name_prefix}{uuid.uuid4().hex[:8]}"
    admin = _admin_engine(pg_admin_url)
    victim = None
    try:
        with admin.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{dbname}"'))
        victim = create_engine(_server_url_for(pg_admin_url, dbname))
        with victim.begin() as conn:
            conn.execute(text("CREATE TABLE precious (id int)"))
            conn.execute(text("INSERT INTO precious VALUES (1)"))
            if make_marker:
                conn.execute(text("CREATE TABLE _disposable_test_db (token text)"))
        with pytest.raises(UnsafeTestDatabase, match=expected):
            truncate_disposable(victim)
        with victim.connect() as conn:  # the data must have survived the refused truncate
            assert conn.execute(text("SELECT count(*) FROM precious")).scalar_one() == 1
    finally:
        if victim is not None:
            victim.dispose()
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{dbname}" WITH (FORCE)'))
        admin.dispose()
