"""Add email and source columns to users table

Revision ID: 010
Revises: 009
Create Date: 2026-05-26
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "010"
down_revision = "009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("email", sa.String(255), nullable=True))
    op.add_column(
        "users",
        sa.Column(
            "source",
            sa.String(20),
            nullable=False,
            server_default="telegram",
        ),
    )
    op.add_column("payments", sa.Column("payment_url", sa.Text, nullable=True))


def downgrade() -> None:
    op.drop_column("payments", "payment_url")
    op.drop_column("users", "source")
    op.drop_column("users", "email")
