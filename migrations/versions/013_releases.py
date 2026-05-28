"""Add releases table for deployment tracking

Revision ID: 013
Revises: 012
Create Date: 2026-05-28
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "013"
down_revision = "012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "releases",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("version", sa.String(80), nullable=False),
        sa.Column("commit_hash", sa.String(40), nullable=True),
        sa.Column("previous_commit", sa.String(40), nullable=True),
        sa.Column("status", sa.String(30), nullable=False, server_default="pending"),
        sa.Column("changelog", sa.Text, nullable=True),
        sa.Column("validation_log", sa.Text, nullable=True),
        sa.Column("deploy_log", sa.Text, nullable=True),
        sa.Column("error_message", sa.Text, nullable=True),
        sa.Column("backup_id", sa.Integer, sa.ForeignKey("backups.id"), nullable=True),
        sa.Column("triggered_by", sa.String(100), nullable=True),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
        sa.Column("deployed_at", sa.DateTime, nullable=True),
        sa.Column("rolled_back_at", sa.DateTime, nullable=True),
        sa.Column("is_current", sa.Boolean, nullable=False, server_default="false"),
    )


def downgrade() -> None:
    op.drop_table("releases")
