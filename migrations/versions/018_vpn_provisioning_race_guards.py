"""Add database-enforced VPN provisioning race guards.

Revision ID: 018
Revises: 017
Create Date: 2026-10-11
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "018"
down_revision = "017"
branch_labels = None
depends_on = None


def _assert_no_duplicates() -> None:
    bind = op.get_bind()

    duplicate_keys = bind.execute(sa.text("""
        SELECT subscription_id, server_id, COUNT(*)
        FROM vpn_keys
        WHERE status = 'active'
        GROUP BY subscription_id, server_id
        HAVING COUNT(*) > 1
        LIMIT 1
    """)).first()
    if duplicate_keys:
        raise RuntimeError(
            "Cannot install VPN active-key uniqueness guard: duplicate active "
            f"keys exist for subscription={duplicate_keys[0]} server={duplicate_keys[1]}"
        )

    duplicate_ops = bind.execute(sa.text("""
        SELECT subscription_id, server_id, COUNT(*)
        FROM pending_operations
        WHERE completed_at IS NULL
          AND operation_type = 'create_key'
          AND subscription_id IS NOT NULL
          AND server_id IS NOT NULL
        GROUP BY subscription_id, server_id
        HAVING COUNT(*) > 1
        LIMIT 1
    """)).first()
    if duplicate_ops:
        raise RuntimeError(
            "Cannot install pending-operation uniqueness guard: duplicate live "
            f"create_key operations exist for subscription={duplicate_ops[0]} "
            f"server={duplicate_ops[1]}"
        )


def upgrade() -> None:
    _assert_no_duplicates()

    active_predicate = sa.text("status = 'active'")
    create_predicate = sa.text(
        "completed_at IS NULL AND operation_type = 'create_key' "
        "AND subscription_id IS NOT NULL AND server_id IS NOT NULL"
    )

    op.create_index(
        "uq_vpn_keys_active_subscription_server",
        "vpn_keys",
        ["subscription_id", "server_id"],
        unique=True,
        postgresql_where=active_predicate,
        sqlite_where=active_predicate,
    )
    op.create_index(
        "uq_pending_create_subscription_server",
        "pending_operations",
        ["subscription_id", "server_id"],
        unique=True,
        postgresql_where=create_predicate,
        sqlite_where=create_predicate,
    )


def downgrade() -> None:
    op.drop_index(
        "uq_pending_create_subscription_server",
        table_name="pending_operations",
    )
    op.drop_index(
        "uq_vpn_keys_active_subscription_server",
        table_name="vpn_keys",
    )
