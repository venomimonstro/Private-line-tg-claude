"""Add vpn_key bot reissue daily counter

Revision ID: 012
Revises: 011
Create Date: 2026-05-27
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "012"
down_revision = "011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "vpn_keys",
        sa.Column("reissue_count_today", sa.Integer, nullable=False, server_default="0"),
    )
    op.add_column(
        "vpn_keys",
        sa.Column("reissue_date", sa.Date, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("vpn_keys", "reissue_date")
    op.drop_column("vpn_keys", "reissue_count_today")
