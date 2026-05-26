"""Add tariff.trial_days and payment gift columns

Revision ID: 011
Revises: 010
Create Date: 2026-05-26
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "011"
down_revision = "010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tariffs", sa.Column("trial_days", sa.Integer, nullable=True))
    op.add_column("payments", sa.Column("gift_recipient_telegram_id", sa.BigInteger, nullable=True))
    op.add_column(
        "payments",
        sa.Column("gift_recipient_id", sa.Integer, sa.ForeignKey("users.id"), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("payments", "gift_recipient_id")
    op.drop_column("payments", "gift_recipient_telegram_id")
    op.drop_column("tariffs", "trial_days")
