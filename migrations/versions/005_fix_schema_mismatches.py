"""Fix schema mismatches between models and DB

Revision ID: 005
Revises: 004_promo_trial
Create Date: 2026-05-22 00:00:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "005"
down_revision: Union[str, None] = "004_promo_trial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # backups: add remote_path, restore_test_status
    op.add_column("backups", sa.Column("remote_path", sa.String(512), nullable=True))
    op.add_column("backups", sa.Column("restore_test_status", sa.String(64), nullable=True))

    # system_updates: add initiated_by_admin_id
    op.add_column("system_updates", sa.Column(
        "initiated_by_admin_id", sa.Integer(),
        sa.ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    ))

    # blocked_domains: add source, updated_at
    op.add_column("blocked_domains", sa.Column("source", sa.String(128), nullable=True))
    op.add_column("blocked_domains", sa.Column("updated_at", sa.DateTime(), nullable=True))

    # blocked_ips: add source, updated_at
    op.add_column("blocked_ips", sa.Column("source", sa.String(128), nullable=True))
    op.add_column("blocked_ips", sa.Column("updated_at", sa.DateTime(), nullable=True))

    # abuse_events: rename details→description, add missing columns
    op.alter_column("abuse_events", "details", new_column_name="description")
    op.add_column("abuse_events", sa.Column(
        "vpn_key_id", sa.Integer(),
        sa.ForeignKey("vpn_keys.id", ondelete="SET NULL"),
        nullable=True,
    ))
    op.add_column("abuse_events", sa.Column("source", sa.String(128), nullable=True))
    op.add_column("abuse_events", sa.Column("target_domain", sa.String(255), nullable=True))
    op.add_column("abuse_events", sa.Column("target_ip", sa.String(64), nullable=True))

    # analytics_daily: rename columns to match model, add missing
    op.alter_column("analytics_daily", "payments_count", new_column_name="paid_orders")
    op.alter_column("analytics_daily", "total_traffic_gb", new_column_name="traffic_total_gb")
    op.add_column("analytics_daily", sa.Column("renewals", sa.Integer(), nullable=True))
    op.add_column("analytics_daily", sa.Column("abuse_events_count", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.alter_column("analytics_daily", "abuse_events_count", new_column_name=None)
    op.alter_column("analytics_daily", "renewals", new_column_name=None)
    op.alter_column("analytics_daily", "traffic_total_gb", new_column_name="total_traffic_gb")
    op.alter_column("analytics_daily", "paid_orders", new_column_name="payments_count")

    op.drop_column("abuse_events", "target_ip")
    op.drop_column("abuse_events", "target_domain")
    op.drop_column("abuse_events", "source")
    op.drop_column("abuse_events", "vpn_key_id")
    op.alter_column("abuse_events", "description", new_column_name="details")

    op.drop_column("blocked_ips", "updated_at")
    op.drop_column("blocked_ips", "source")
    op.drop_column("blocked_domains", "updated_at")
    op.drop_column("blocked_domains", "source")

    op.drop_column("system_updates", "initiated_by_admin_id")
    op.drop_column("backups", "restore_test_status")
    op.drop_column("backups", "remote_path")
