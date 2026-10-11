"""Add encrypted per-node SSH rollout credentials.

Revision ID: 019
Revises: 018
Create Date: 2026-10-11
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "019"
down_revision = "018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("vpn_servers", sa.Column("ssh_host", sa.String(length=255), nullable=True))
    op.add_column("vpn_servers", sa.Column("ssh_port", sa.Integer(), nullable=True))
    op.add_column("vpn_servers", sa.Column("ssh_username", sa.String(length=64), nullable=True))
    op.add_column("vpn_servers", sa.Column("ssh_auth_type", sa.String(length=16), nullable=True))
    op.add_column("vpn_servers", sa.Column("ssh_secret_encrypted", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("vpn_servers", "ssh_secret_encrypted")
    op.drop_column("vpn_servers", "ssh_auth_type")
    op.drop_column("vpn_servers", "ssh_username")
    op.drop_column("vpn_servers", "ssh_port")
    op.drop_column("vpn_servers", "ssh_host")
