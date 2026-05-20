"""Initial schema: all 15 tables

Revision ID: 001
Revises:
Create Date: 2024-01-01 00:00:00.000000

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("telegram_id", sa.BigInteger(), nullable=False),
        sa.Column("username", sa.String(length=64), nullable=True),
        sa.Column("first_name", sa.String(length=128), nullable=True),
        sa.Column("last_name", sa.String(length=128), nullable=True),
        sa.Column("language_code", sa.String(length=8), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="new"),
        sa.Column("is_blocked", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("block_reason", sa.Text(), nullable=True),
        sa.Column("referral_code", sa.String(length=32), nullable=True),
        sa.Column("referred_by_id", sa.Integer(), nullable=True),
        sa.Column("balance", sa.Numeric(precision=10, scale=2), nullable=False, server_default="0"),
        sa.Column("rules_accepted_at", sa.DateTime(), nullable=True),
        sa.Column("admin_role", sa.String(length=32), nullable=True),
        sa.Column("admin_password_hash", sa.String(length=256), nullable=True),
        sa.Column("admin_login_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("admin_locked_until", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("last_activity_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["referred_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("telegram_id"),
        sa.UniqueConstraint("referral_code"),
    )
    op.create_index("ix_users_telegram_id", "users", ["telegram_id"])
    op.create_index("ix_users_referral_code", "users", ["referral_code"])

    op.create_table(
        "tariffs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("slug", sa.String(length=32), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("price_monthly", sa.Numeric(precision=10, scale=2), nullable=False),
        sa.Column("price_first_month", sa.Numeric(precision=10, scale=2), nullable=True),
        sa.Column("duration_days", sa.Integer(), nullable=False),
        sa.Column("traffic_limit_gb", sa.Integer(), nullable=True),
        sa.Column("max_keys", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="1"),
        sa.Column("is_visible", sa.Boolean(), nullable=False, server_default="1"),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("slug"),
    )

    op.create_table(
        "vpn_servers",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("country", sa.String(length=64), nullable=False),
        sa.Column("city", sa.String(length=64), nullable=True),
        sa.Column("api_url", sa.String(length=512), nullable=False),
        sa.Column("api_secret_encrypted", sa.String(length=1024), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="active"),
        sa.Column("max_users", sa.Integer(), nullable=False, server_default="100"),
        sa.Column("current_users", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "subscriptions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("tariff_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="pending_payment"),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("auto_renew", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tariff_id"], ["tariffs.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_subscriptions_user_id", "subscriptions", ["user_id"])
    op.create_index("ix_subscriptions_status", "subscriptions", ["status"])

    op.create_table(
        "payments",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("subscription_id", sa.Integer(), nullable=True),
        sa.Column("tariff_id", sa.Integer(), nullable=False),
        sa.Column("amount", sa.Numeric(precision=10, scale=2), nullable=False),
        sa.Column("currency", sa.String(length=8), nullable=False, server_default="RUB"),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="created"),
        sa.Column("provider", sa.String(length=32), nullable=False, server_default="manual"),
        sa.Column("provider_payment_id", sa.String(length=256), nullable=True),
        sa.Column("confirmed_by_id", sa.Integer(), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(), nullable=True),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["subscription_id"], ["subscriptions.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["tariff_id"], ["tariffs.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["confirmed_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_payments_user_id", "payments", ["user_id"])
    op.create_index("ix_payments_status", "payments", ["status"])

    op.create_table(
        "vpn_keys",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("subscription_id", sa.Integer(), nullable=False),
        sa.Column("server_id", sa.Integer(), nullable=False),
        sa.Column("outline_key_id", sa.String(length=64), nullable=False),
        sa.Column("access_url_encrypted", sa.String(length=2048), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="active"),
        sa.Column("traffic_used_gb", sa.Numeric(precision=12, scale=4), nullable=False, server_default="0"),
        sa.Column("traffic_limit_gb", sa.Integer(), nullable=True),
        sa.Column("last_reissued_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["subscription_id"], ["subscriptions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["server_id"], ["vpn_servers.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_vpn_keys_user_id", "vpn_keys", ["user_id"])
    op.create_index("ix_vpn_keys_status", "vpn_keys", ["status"])

    op.create_table(
        "referrals",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("referrer_id", sa.Integer(), nullable=False),
        sa.Column("invited_user_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="pending"),
        sa.Column("bonus_days_awarded", sa.Integer(), nullable=True),
        sa.Column("activated_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["referrer_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["invited_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("invited_user_id"),
    )

    op.create_table(
        "admin_logs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("admin_user_id", sa.Integer(), nullable=False),
        sa.Column("action", sa.String(length=128), nullable=False),
        sa.Column("target_user_id", sa.Integer(), nullable=True),
        sa.Column("target_entity", sa.String(length=64), nullable=True),
        sa.Column("target_entity_id", sa.Integer(), nullable=True),
        sa.Column("ip_address", sa.String(length=45), nullable=True),
        sa.Column("details", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["admin_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["target_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_admin_logs_admin_user_id", "admin_logs", ["admin_user_id"])
    op.create_index("ix_admin_logs_created_at", "admin_logs", ["created_at"])

    op.create_table(
        "system_updates",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("version_from", sa.String(length=32), nullable=False),
        sa.Column("version_to", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="uploaded"),
        sa.Column("zip_path", sa.String(length=512), nullable=True),
        sa.Column("backup_id", sa.Integer(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("log_path", sa.String(length=512), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "backups",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("type", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="creating"),
        sa.Column("file_path", sa.String(length=512), nullable=True),
        sa.Column("size_mb", sa.Numeric(precision=10, scale=2), nullable=True),
        sa.Column("checksum", sa.String(length=64), nullable=True),
        sa.Column("created_by", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "health_checks",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("database_status", sa.String(length=32), nullable=False),
        sa.Column("bot_status", sa.String(length=32), nullable=False),
        sa.Column("backend_status", sa.String(length=32), nullable=False),
        sa.Column("vpn_api_status", sa.String(length=32), nullable=False),
        sa.Column("dns_filter_status", sa.String(length=32), nullable=False),
        sa.Column("firewall_status", sa.String(length=32), nullable=False),
        sa.Column("backup_status", sa.String(length=32), nullable=False),
        sa.Column("free_disk_space", sa.Numeric(precision=10, scale=2), nullable=True),
        sa.Column("cpu_percent", sa.Numeric(precision=5, scale=2), nullable=True),
        sa.Column("ram_percent", sa.Numeric(precision=5, scale=2), nullable=True),
        sa.Column("details", sa.JSON(), nullable=True),
        sa.Column("checked_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "blocked_domains",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("domain", sa.String(length=253), nullable=False),
        sa.Column("category", sa.String(length=32), nullable=False, server_default="manual"),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="active"),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("added_by_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["added_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("domain"),
    )

    op.create_table(
        "blocked_ips",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("ip_or_cidr", sa.String(length=43), nullable=False),
        sa.Column("category", sa.String(length=32), nullable=False, server_default="manual"),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="active"),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("added_by_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["added_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("ip_or_cidr"),
    )

    op.create_table(
        "abuse_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("severity", sa.String(length=32), nullable=False, server_default="low"),
        sa.Column("action_taken", sa.String(length=32), nullable=False, server_default="LOG_ONLY"),
        sa.Column("details", sa.Text(), nullable=True),
        sa.Column("admin_comment", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_abuse_events_user_id", "abuse_events", ["user_id"])

    op.create_table(
        "analytics_daily",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("new_users", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("active_subscriptions", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("expired_subscriptions", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("revenue", sa.Numeric(precision=12, scale=2), nullable=False, server_default="0"),
        sa.Column("payments_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("active_vpn_keys", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_traffic_gb", sa.Numeric(precision=14, scale=4), nullable=False, server_default="0"),
        sa.Column("new_referrals", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("date"),
    )
    op.create_index("ix_analytics_daily_date", "analytics_daily", ["date"])


def downgrade() -> None:
    op.drop_table("analytics_daily")
    op.drop_table("abuse_events")
    op.drop_table("blocked_ips")
    op.drop_table("blocked_domains")
    op.drop_table("health_checks")
    op.drop_table("backups")
    op.drop_table("system_updates")
    op.drop_table("admin_logs")
    op.drop_table("referrals")
    op.drop_table("vpn_keys")
    op.drop_table("payments")
    op.drop_table("subscriptions")
    op.drop_table("vpn_servers")
    op.drop_table("tariffs")
    op.drop_table("users")
