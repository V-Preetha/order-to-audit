"""Migrations can be applied, reversed and re-applied, on a database nothing else uses."""
import uuid

import pytest
from alembic import command
from sqlalchemy import create_engine, text

from tests.db.helpers import alembic_config
from tests.safety import DB_PREFIX, MARKER_TABLE


@pytest.fixture
def fresh_url(pg_admin_url):
    """A second disposable database, so the shared one is never downgraded."""
    name = f"{DB_PREFIX}{uuid.uuid4().hex[:10]}"
    admin = create_engine(pg_admin_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    url = pg_admin_url.rsplit("/", 1)[0] + "/" + name
    eng = create_engine(url)
    with eng.begin() as conn:
        conn.execute(text(f"CREATE TABLE {MARKER_TABLE} (token text)"))
    eng.dispose()
    try:
        yield url
    finally:
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


def _inventory(url):
    eng = create_engine(url)
    try:
        with eng.connect() as conn:
            tables = set(conn.execute(text("SELECT tablename FROM pg_tables WHERE schemaname='public'")).scalars())
            functions = set(conn.execute(text(
                "SELECT proname FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname='public'")).scalars())
            types = set(conn.execute(text(
                "SELECT typname FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace "
                "WHERE n.nspname='public' AND t.typtype='e'")).scalars())
            seeded = conn.execute(text("SELECT count(*) FROM status_transitions")).scalar_one() \
                if "status_transitions" in tables else None  # all edges, whichever migration seeded them
            return tables, functions, types, seeded
    finally:
        eng.dispose()


EXPECTED_TABLES = {"users", "settings", "document_sequences", "request_log", "status_transitions",
                   "products", "items", "customers", "suppliers"}
EXPECTED_FUNCTIONS = {"unit_factor", "forbid_mutation", "enforce_status_transition", "guard_posted_row"}
EXPECTED_TABLES_0002 = {"recipes", "recipe_lines", "boms", "bom_packaging_lines"}
EXPECTED_FUNCTIONS_0002 = {"guard_versioned_row", "guard_version_lines", "check_activation"}
EDGES_0001, EDGES_0002 = 22, 8
ALL_EDGES = EDGES_0001 + EDGES_0002


def test_upgrade_downgrade_upgrade_round_trip(fresh_url):
    cfg = alembic_config(fresh_url)
    command.upgrade(cfg, "head")
    tables, functions, types, seeded = _inventory(fresh_url)
    assert EXPECTED_TABLES <= tables and EXPECTED_FUNCTIONS <= functions and "unit_code" in types
    assert seeded == ALL_EDGES

    command.downgrade(cfg, "0000")
    tables, functions, types, seeded = _inventory(fresh_url)
    assert tables == {"alembic_version", MARKER_TABLE}, "downgrade must leave no tables behind"
    assert functions == set() and types == set() and seeded is None

    command.upgrade(cfg, "head")
    tables, functions, types, seeded = _inventory(fresh_url)
    assert EXPECTED_TABLES <= tables and seeded == ALL_EDGES


def test_upgrading_twice_changes_nothing(fresh_url):
    cfg = alembic_config(fresh_url)
    command.upgrade(cfg, "head")
    before = _inventory(fresh_url)
    assert EXPECTED_TABLES <= before[0] and before[3] == ALL_EDGES, "the comparison must be of a real schema"
    command.upgrade(cfg, "head")
    assert _inventory(fresh_url) == before


def test_revision_0001_is_in_the_chain(fresh_url):
    from alembic.script import ScriptDirectory
    chain = [r.revision for r in ScriptDirectory.from_config(alembic_config(fresh_url)).walk_revisions()]
    assert "0001" in chain and chain.index("0001") < chain.index("0000")


def test_0002_can_be_reversed_alone_leaving_0001_intact(fresh_url):
    cfg = alembic_config(fresh_url)
    command.upgrade(cfg, "0001")
    at_0001 = _inventory(fresh_url)
    assert not (EXPECTED_TABLES_0002 & at_0001[0]) and at_0001[3] == EDGES_0001

    command.upgrade(cfg, "0002")
    tables, functions, types, seeded = _inventory(fresh_url)
    assert EXPECTED_TABLES_0002 <= tables and EXPECTED_FUNCTIONS_0002 <= functions and seeded == ALL_EDGES

    command.downgrade(cfg, "0001")
    assert _inventory(fresh_url) == at_0001, "downgrading 0002 must restore exactly the 0001 schema and seed"

    command.upgrade(cfg, "head")
    assert _inventory(fresh_url)[3] == ALL_EDGES
