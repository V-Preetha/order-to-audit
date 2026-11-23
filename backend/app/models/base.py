from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Declarative base. The schema itself is owned by the Alembic migrations (hand-written SQL);
    the models mirror it, and tests/db/test_models.py fails if the two drift apart."""
