from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import PaymentStatus, SubscriptionStatus
from app.models.analytics_daily import AnalyticsDaily
from app.models.payment import Payment
from app.models.subscription import Subscription
from app.models.user import User
from app.models.vpn_key import VpnKey

logger = logging.getLogger(__name__)


async def collect_daily_analytics(session: AsyncSession) -> None:
    today = date.today()

    existing = await session.execute(
        select(AnalyticsDaily).where(AnalyticsDaily.date == today)
    )
    record = existing.scalar_one_or_none()
    if record is None:
        record = AnalyticsDaily(date=today)
        session.add(record)

    today_start = datetime.combine(today, datetime.min.time())

    new_users_result = await session.execute(
        select(func.count(User.id)).where(User.created_at >= today_start)
    )
    record.new_users = new_users_result.scalar_one() or 0

    active_subs_result = await session.execute(
        select(func.count(Subscription.id)).where(
            Subscription.status.in_([SubscriptionStatus.active, SubscriptionStatus.expires_soon])
        )
    )
    record.active_subscriptions = active_subs_result.scalar_one() or 0

    paid_result = await session.execute(
        select(func.count(Payment.id), func.coalesce(func.sum(Payment.amount), 0)).where(
            Payment.status == PaymentStatus.paid,
            Payment.confirmed_at >= today_start,
        )
    )
    paid_row = paid_result.one()
    record.paid_orders = paid_row[0] or 0
    record.revenue = float(paid_row[1] or 0)

    expired_result = await session.execute(
        select(func.count(Subscription.id)).where(
            Subscription.status == SubscriptionStatus.expired,
            Subscription.updated_at >= today_start,
        )
    )
    record.expired_subscriptions = expired_result.scalar_one() or 0

    traffic_result = await session.execute(
        select(func.coalesce(func.sum(VpnKey.traffic_used_gb), 0))
    )
    record.traffic_total_gb = float(traffic_result.scalar_one() or 0)

    await session.commit()
    logger.info("Daily analytics collected for %s", today)
