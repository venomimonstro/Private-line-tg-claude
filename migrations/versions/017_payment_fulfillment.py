"""Add durable payment fulfillment marker.

Revision ID: 017
Revises: 016
Create Date: 2026-10-10
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "017"
down_revision = "016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("payments", sa.Column("fulfilled_at", sa.DateTime(), nullable=True))
    op.create_index("ix_payments_fulfilled_at", "payments", ["fulfilled_at"])

    # Legacy rows predate the exactly-once marker. Mark obviously completed
    # paid payments fulfilled when their subscription is not waiting for
    # activation. Leave paid + pending/activation_error rows NULL so the
    # recovery worker can finish stranded VPN activation.
    op.execute(
        sa.text(
            """
            UPDATE payments AS p
            SET fulfilled_at = COALESCE(p.confirmed_at, p.updated_at, p.created_at)
            WHERE p.status = 'paid'
              AND NOT EXISTS (
                  SELECT 1
                  FROM subscriptions AS s
                  WHERE s.id = p.subscription_id
                    AND s.status IN ('pending_payment', 'activation_error')
              )
            """
        )
    )


def downgrade() -> None:
    op.drop_index("ix_payments_fulfilled_at", table_name="payments")
    op.drop_column("payments", "fulfilled_at")
