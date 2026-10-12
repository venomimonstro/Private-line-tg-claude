"""Add VPN data-plane health proof timestamps.

Revision ID: 020
Revises: 019
Create Date: 2026-10-12
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "020"
down_revision = "019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "vpn_servers",
        sa.Column("last_e2e_success_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "vpn_servers",
        sa.Column("last_hot_add_success_at", sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("vpn_servers", "last_hot_add_success_at")
    op.drop_column("vpn_servers", "last_e2e_success_at")
