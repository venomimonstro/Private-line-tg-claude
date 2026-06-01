"""Add subscription_notifications table for granular expiry reminders

Revision ID: 016
Revises: 015
Create Date: 2026-06-01
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "016"
down_revision = "015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "subscription_notifications",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("subscription_id", sa.Integer(), sa.ForeignKey("subscriptions.id"), nullable=False),
        sa.Column("notification_type", sa.String(20), nullable=False),
        sa.Column("sent_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_sub_notifications_sub_id", "subscription_notifications", ["subscription_id"])
    op.create_unique_constraint(
        "uq_sub_notification_type",
        "subscription_notifications",
        ["subscription_id", "notification_type"],
    )


def downgrade() -> None:
    op.drop_table("subscription_notifications")
