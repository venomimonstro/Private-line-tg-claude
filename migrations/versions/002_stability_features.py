"""Stability features: refund_requests, speed limits, server health tracking

Revision ID: 002
Revises: 001
Create Date: 2024-01-02 00:00:00.000000
"""
from __future__ import annotations
from typing import Sequence, Union
import sqlalchemy as sa
from alembic import op

revision: str = "002"
down_revision: Union[str, None] = "001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add speed_limit_mbps to tariffs
    op.add_column("tariffs", sa.Column("speed_limit_mbps", sa.Integer(), nullable=True))

    # Add server health tracking to vpn_servers
    op.add_column("vpn_servers", sa.Column("failure_count", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("vpn_servers", sa.Column("last_error", sa.String(length=512), nullable=True))
    op.add_column("vpn_servers", sa.Column("last_error_at", sa.DateTime(), nullable=True))
    op.add_column("vpn_servers", sa.Column("city", sa.String(length=64), nullable=True))

    # Add activation tracking to subscriptions
    op.add_column("subscriptions", sa.Column("activation_attempts", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("subscriptions", sa.Column("last_activation_error", sa.String(length=512), nullable=True))

    # Create refund_requests table
    op.create_table(
        "refund_requests",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("payment_id", sa.Integer(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="pending"),
        sa.Column("traffic_used_mb_at_request", sa.Numeric(precision=12, scale=2), nullable=False, server_default="0"),
        sa.Column("admin_comment", sa.Text(), nullable=True),
        sa.Column("processed_by_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("processed_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["payment_id"], ["payments.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["processed_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_refund_requests_user_id", "refund_requests", ["user_id"])
    op.create_index("ix_refund_requests_payment_id", "refund_requests", ["payment_id"])
    op.create_index("ix_refund_requests_status", "refund_requests", ["status"])


def downgrade() -> None:
    op.drop_table("refund_requests")
    op.drop_column("subscriptions", "last_activation_error")
    op.drop_column("subscriptions", "activation_attempts")
    op.drop_column("vpn_servers", "city")
    op.drop_column("vpn_servers", "last_error_at")
    op.drop_column("vpn_servers", "last_error")
    op.drop_column("vpn_servers", "failure_count")
    op.drop_column("tariffs", "speed_limit_mbps")
