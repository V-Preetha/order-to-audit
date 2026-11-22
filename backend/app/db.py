from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings


def make_engine(url: str) -> Engine:
    return create_engine(url, pool_pre_ping=True)


_sessionmaker: sessionmaker[Session] | None = None


def get_session() -> Iterator[Session]:
    """FastAPI dependency: one session per request, closed afterwards."""
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = sessionmaker(make_engine(Settings().database_url))
    with _sessionmaker() as session:
        yield session


@contextmanager
def transaction(session: Session) -> Iterator[Session]:
    """Commit on success, roll back on any error. Every posting runs inside one."""
    try:
        yield session
        session.commit()
    except BaseException:
        session.rollback()
        raise
