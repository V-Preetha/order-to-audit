"""baseline: empty revision so the migration chain and harness are proven before any schema

Revision ID: 0000
Revises:
"""
revision = "0000"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
