"""Pure tests for the guards that keep the test harness away from real databases.

No database is needed here: these functions only inspect configuration.
"""
import pytest

from tests.safety import UnsafeTestDatabase, assert_safe_admin_url, resolve_admin_url

DEV_URL = "postgresql+psycopg://app:app@localhost:5432/order_to_audit"
OK_URL = "postgresql+psycopg://postgres:postgres@localhost:5433/postgres"


def test_missing_admin_url_aborts():
    with pytest.raises(UnsafeTestDatabase, match="TEST_DATABASE_ADMIN_URL"):
        assert_safe_admin_url(None, dev_url=DEV_URL)
    with pytest.raises(UnsafeTestDatabase, match="TEST_DATABASE_ADMIN_URL"):
        assert_safe_admin_url("", dev_url=DEV_URL)


def test_remote_host_rejected():
    url = "postgresql+psycopg://u:p@db.example.com:5432/postgres"
    with pytest.raises(UnsafeTestDatabase, match="host"):
        assert_safe_admin_url(url, dev_url=None)


def test_admin_server_equal_to_dev_server_rejected():
    same_server = "postgresql+psycopg://postgres:postgres@localhost:5432/postgres"
    with pytest.raises(UnsafeTestDatabase, match="development"):
        assert_safe_admin_url(same_server, dev_url=DEV_URL)


def test_loopback_aliases_count_as_the_same_dev_server():
    alias = "postgresql+psycopg://postgres:postgres@127.0.0.1:5432/postgres"
    with pytest.raises(UnsafeTestDatabase, match="development"):
        assert_safe_admin_url(alias, dev_url=DEV_URL)


def test_non_maintenance_database_rejected():
    url = "postgresql+psycopg://u:p@localhost:5433/order_to_audit"
    with pytest.raises(UnsafeTestDatabase, match="postgres"):
        assert_safe_admin_url(url, dev_url=None)


def test_non_postgres_scheme_rejected():
    with pytest.raises(UnsafeTestDatabase, match="PostgreSQL"):
        assert_safe_admin_url("sqlite:///test.db", dev_url=None)


def test_localhost_postgres_admin_url_accepted():
    assert_safe_admin_url(OK_URL, dev_url=DEV_URL)
    assert_safe_admin_url("postgresql+psycopg://u:p@127.0.0.1:5433/postgres", dev_url=None)


def test_resolve_admin_url_requires_the_variable():
    with pytest.raises(UnsafeTestDatabase, match="TEST_DATABASE_ADMIN_URL"):
        resolve_admin_url({})


def test_resolve_admin_url_compares_against_database_url():
    env = {"TEST_DATABASE_ADMIN_URL": OK_URL, "DATABASE_URL": OK_URL.replace("/postgres", "/order_to_audit")}
    with pytest.raises(UnsafeTestDatabase, match="development"):
        resolve_admin_url(env)
    env["DATABASE_URL"] = DEV_URL
    assert resolve_admin_url(env) == OK_URL
