"""referral revshare: balance, transactions, withdrawal_requests

Revision ID: 008
Revises: 007
Create Date: 2026-05-24
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "008"
down_revision = "007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("referral_balance", sa.Numeric(12, 2), nullable=False, server_default="0"),
    )

    op.create_table(
        "referral_transactions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("referrer_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("payer_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("payment_id", sa.Integer(), sa.ForeignKey("payments.id"), nullable=False, unique=True),
        sa.Column("amount_rub", sa.Numeric(12, 2), nullable=False),
        sa.Column("payment_amount_rub", sa.Numeric(12, 2), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_referral_transactions_referrer_id", "referral_transactions", ["referrer_id"])
    op.create_index("ix_referral_transactions_payer_user_id", "referral_transactions", ["payer_user_id"])
    op.create_index("ix_referral_transactions_payment_id", "referral_transactions", ["payment_id"])

    op.create_table(
        "withdrawal_requests",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("amount_rub", sa.Numeric(12, 2), nullable=False),
        sa.Column("stars_amount", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("processed_at", sa.DateTime(), nullable=True),
        sa.Column("processed_by_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("admin_note", sa.Text(), nullable=True),
    )
    op.create_index("ix_withdrawal_requests_user_id", "withdrawal_requests", ["user_id"])


def downgrade() -> None:
    op.drop_table("withdrawal_requests")
    op.drop_table("referral_transactions")
    op.drop_column("users", "referral_balance")
