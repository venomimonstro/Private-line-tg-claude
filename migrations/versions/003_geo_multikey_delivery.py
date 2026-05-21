"""Geo switching, multi-key, and key delivery logging

Revision ID: 003
Revises: 002
Create Date: 2024-01-03 00:00:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "003"
down_revision: Union[str, None] = "002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- vpn_servers: add geo + selectable fields ---
    op.add_column("vpn_servers", sa.Column("country_code", sa.String(length=5), nullable=True))
    op.add_column("vpn_servers", sa.Column("flag_emoji", sa.String(length=10), nullable=True))
    op.add_column(
        "vpn_servers",
        sa.Column("is_selectable", sa.Boolean(), nullable=False, server_default=sa.true()),
    )

    # --- vpn_keys: add label field ---
    op.add_column("vpn_keys", sa.Column("label", sa.String(length=64), nullable=True))

    # --- key_delivery_logs table ---
    op.create_table(
        "key_delivery_logs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("vpn_key_id", sa.Integer(), nullable=True),
        sa.Column("subscription_id", sa.Integer(), nullable=False),
        sa.Column("delivered_at", sa.DateTime(), nullable=True),
        sa.Column("attempt_number", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("error_msg", sa.String(length=512), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["vpn_key_id"], ["vpn_keys.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["subscription_id"], ["subscriptions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_key_delivery_logs_user_id", "key_delivery_logs", ["user_id"])
    op.create_index("ix_key_delivery_logs_subscription_id", "key_delivery_logs", ["subscription_id"])

    # --- pending_operations table ---
    # Using String for operation_type for SQLite compatibility
    op.create_table(
        "pending_operations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("operation_type", sa.String(length=32), nullable=False),
        sa.Column("subscription_id", sa.Integer(), nullable=True),
        sa.Column("vpn_key_id", sa.Integer(), nullable=True),
        sa.Column("server_id", sa.Integer(), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("last_error", sa.String(length=512), nullable=True),
        sa.Column("next_retry_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["subscription_id"], ["subscriptions.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["vpn_key_id"], ["vpn_keys.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["server_id"], ["vpn_servers.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_pending_operations_completed_at", "pending_operations", ["completed_at"])
    op.create_index("ix_pending_operations_next_retry_at", "pending_operations", ["next_retry_at"])


def downgrade() -> None:
    op.drop_index("ix_pending_operations_next_retry_at", table_name="pending_operations")
    op.drop_index("ix_pending_operations_completed_at", table_name="pending_operations")
    op.drop_table("pending_operations")

    op.drop_index("ix_key_delivery_logs_subscription_id", table_name="key_delivery_logs")
    op.drop_index("ix_key_delivery_logs_user_id", table_name="key_delivery_logs")
    op.drop_table("key_delivery_logs")

    op.drop_column("vpn_keys", "label")

    op.drop_column("vpn_servers", "is_selectable")
    op.drop_column("vpn_servers", "flag_emoji")
    op.drop_column("vpn_servers", "country_code")
