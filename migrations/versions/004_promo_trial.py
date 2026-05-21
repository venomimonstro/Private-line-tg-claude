"""Promo codes, trial subscriptions, traffic notification flags

Revision ID: 004_promo_trial
Revises: 003
Create Date: 2024-01-04 00:00:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "004_promo_trial"
down_revision: Union[str, None] = "003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Create promo_codes table
    op.create_table(
        "promo_codes",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("description", sa.String(length=255), nullable=True),
        sa.Column("discount_percent", sa.Integer(), nullable=True),
        sa.Column("discount_fixed", sa.Numeric(precision=10, scale=2), nullable=True),
        sa.Column("tariff_slug", sa.String(length=64), nullable=True),
        sa.Column("uses_max", sa.Integer(), nullable=True),
        sa.Column("uses_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("is_trial", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("trial_days", sa.Integer(), nullable=True),
        sa.Column("trial_traffic_gb", sa.Integer(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="1"),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("created_by_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["created_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_promo_codes_code", "promo_codes", ["code"], unique=True)

    # Add traffic notification flags to vpn_keys
    op.add_column(
        "vpn_keys",
        sa.Column("notified_80pct", sa.Boolean(), nullable=False, server_default="0"),
    )
    op.add_column(
        "vpn_keys",
        sa.Column("notified_95pct", sa.Boolean(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("vpn_keys", "notified_95pct")
    op.drop_column("vpn_keys", "notified_80pct")
    op.drop_index("ix_promo_codes_code", table_name="promo_codes")
    op.drop_table("promo_codes")
