"""Add payment_type to tariffs

Revision ID: 009
Revises: 008
Create Date: 2026-05-25
"""
from alembic import op
import sqlalchemy as sa

revision = "009"
down_revision = "008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tariffs",
        sa.Column("payment_type", sa.String(20), nullable=False, server_default="recurring"),
    )


def downgrade() -> None:
    op.drop_column("tariffs", "payment_type")
