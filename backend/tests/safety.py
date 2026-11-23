"""Guards that keep the test suite away from any database that is not disposable.

Tests never read DATABASE_URL to connect; they only read it to prove the test server is
a *different* server. Destructive helpers refuse to run unless the target database is
one this harness created (name prefix + marker table).
"""
from __future__ import annotations

from collections.abc import Mapping

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine, make_url

DB_PREFIX = "o2a_test_"
MARKER_TABLE = "_disposable_test_db"
ADMIN_ENV = "TEST_DATABASE_ADMIN_URL"
_LOOPBACK = {"localhost", "127.0.0.1", "::1"}
_KEEP = {"alembic_version", MARKER_TABLE, "status_transitions"}  # status_transitions is migration-seeded reference data


class UnsafeTestDatabase(RuntimeError):
    """Raised when a test run would touch a database that is not provably disposable."""


def _server_key(url) -> tuple[str, int]:
    host = "localhost" if (url.host or "") in _LOOPBACK else (url.host or "")
    return host, url.port or 5432


def assert_safe_admin_url(admin_url: str | None, *, dev_url: str | None) -> None:
    if not admin_url:
        raise UnsafeTestDatabase(f"{ADMIN_ENV} is not set; refusing to run database tests.")
    try:
        url = make_url(admin_url)
    except Exception as exc:  # malformed URL
        raise UnsafeTestDatabase(f"{ADMIN_ENV} is not a valid URL.") from exc
    if url.get_backend_name() != "postgresql":
        raise UnsafeTestDatabase("Only PostgreSQL is supported for database tests.")
    if (url.host or "") not in _LOOPBACK:
        raise UnsafeTestDatabase(f"Refusing non-local host {url.host!r}; tests only run on a local server.")
    if url.database != "postgres":
        raise UnsafeTestDatabase("The admin URL must point at the maintenance database 'postgres'.")
    if dev_url and _server_key(url) == _server_key(make_url(dev_url)):
        raise UnsafeTestDatabase("The test server is the development server; use a separate server/port.")


def resolve_admin_url(env: Mapping[str, str]) -> str:
    admin_url = env.get(ADMIN_ENV)
    assert_safe_admin_url(admin_url, dev_url=env.get("DATABASE_URL"))
    return admin_url  # type: ignore[return-value]


def assert_disposable(conn: Connection) -> None:
    name = conn.execute(text("SELECT current_database()")).scalar_one()
    if not name.startswith(DB_PREFIX):
        raise UnsafeTestDatabase(f"Database {name!r} does not start with {DB_PREFIX!r}; refusing.")
    has_marker = conn.execute(text("SELECT to_regclass(:t) IS NOT NULL"), {"t": f"public.{MARKER_TABLE}"}).scalar_one()
    if not has_marker:
        raise UnsafeTestDatabase(f"Database {name!r} has no {MARKER_TABLE} marker table; refusing.")


def truncate_disposable(engine: Engine) -> None:
    """Empty every table except alembic_version and the marker, after re-proving safety."""
    with engine.begin() as conn:
        assert_disposable(conn)
        tables = conn.execute(text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")).scalars().all()
        targets = [t for t in tables if t not in _KEEP]
        if targets:
            names = ", ".join(f'"{t}"' for t in targets)
            conn.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))
