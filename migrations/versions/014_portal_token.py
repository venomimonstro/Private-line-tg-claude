"""add portal_token to users

Revision ID: 014
Revises: 013
Create Date: 2026-05-28
"""
from __future__ import annotations
from alembic import op
import sqlalchemy as sa

revision = "014"
down_revision = "013"
branch_labels = None
depends_on = None

def upgrade() -> None:
    op.add_column("users", sa.Column("portal_token", sa.String(64), nullable=True))
    op.create_unique_constraint("uq_users_portal_token", "users", ["portal_token"])
    op.create_index("ix_users_portal_token", "users", ["portal_token"])

def downgrade() -> None:
    op.drop_index("ix_users_portal_token", table_name="users")
    op.drop_constraint("uq_users_portal_token", "users", type_="unique")
    op.drop_column("users", "portal_token")
