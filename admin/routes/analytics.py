from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from admin.dependencies import get_current_admin
from app.database import get_async_session
from app.enums import PaymentStatus, ReferralStatus, SubscriptionStatus, UserStatus
from app.models.payment import Payment
from app.models.referral import Referral
from app.models.subscription import Subscription
from app.models.tariff import Tariff
from app.models.user import User
from app.models.vpn_key import VpnKey

router = APIRouter(prefix="/analytics")
templates = Jinja2Templates(directory="admin/templates")


@router.get("", response_class=HTMLResponse)
async def analytics_page(
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    admin: User = Depends(get_current_admin),
) -> HTMLResponse:
    now = datetime.utcnow()
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_ago = today - timedelta(days=7)
    month_ago = today - timedelta(days=30)

    total_users = (await session.execute(select(func.count(User.id)))).scalar_one() or 0
    new_today = (await session.execute(select(func.count(User.id)).where(User.created_at >= today))).scalar_one() or 0
    new_7d = (await session.execute(select(func.count(User.id)).where(User.created_at >= week_ago))).scalar_one() or 0
    new_30d = (await session.execute(select(func.count(User.id)).where(User.created_at >= month_ago))).scalar_one() or 0
    blocked_users = (await session.execute(select(func.count(User.id)).where(User.status == UserStatus.blocked))).scalar_one() or 0

    active_subs = (await session.execute(select(func.count(Subscription.id)).where(
        Subscription.status.in_([SubscriptionStatus.active, SubscriptionStatus.expires_soon])))).scalar_one() or 0
    expired_subs = (await session.execute(select(func.count(Subscription.id)).where(
        Subscription.status == SubscriptionStatus.expired))).scalar_one() or 0

    def _rev_query(since: datetime):
        return select(func.coalesce(func.sum(Payment.amount), 0)).where(
            Payment.status == PaymentStatus.paid, Payment.confirmed_at >= since)

    rev_today = float((await session.execute(_rev_query(today))).scalar_one() or 0)
    rev_7d = float((await session.execute(_rev_query(week_ago))).scalar_one() or 0)
    rev_30d = float((await session.execute(_rev_query(month_ago))).scalar_one() or 0)

    paid_today = (await session.execute(select(func.count(Payment.id)).where(
        Payment.status == PaymentStatus.paid, Payment.confirmed_at >= today))).scalar_one() or 0
    paid_7d = (await session.execute(select(func.count(Payment.id)).where(
        Payment.status == PaymentStatus.paid, Payment.confirmed_at >= week_ago))).scalar_one() or 0
    paid_30d = (await session.execute(select(func.count(Payment.id)).where(
        Payment.status == PaymentStatus.paid, Payment.confirmed_at >= month_ago))).scalar_one() or 0

    avg_ticket = rev_30d / paid_30d if paid_30d > 0 else 0
    arpu = rev_30d / total_users if total_users > 0 else 0
    mrr_forecast = rev_30d

    tariffs_result = await session.execute(select(Tariff).order_by(Tariff.sort_order))
    tariffs = tariffs_result.scalars().all()

    tariff_stats = []
    for tariff in tariffs:
        count_r = await session.execute(select(func.count(Subscription.id)).where(
            Subscription.tariff_id == tariff.id, Subscription.status.in_(
                [SubscriptionStatus.active, SubscriptionStatus.expires_soon])))
        count = count_r.scalar_one() or 0
        rev_r = await session.execute(select(func.coalesce(func.sum(Payment.amount), 0)).where(
            Payment.tariff_id == tariff.id, Payment.status == PaymentStatus.paid))
        rev = float(rev_r.scalar_one() or 0)
        tariff_stats.append({"tariff": tariff, "count": count, "revenue": rev})

    referral_total = (await session.execute(select(func.count(Referral.id)))).scalar_one() or 0
    referral_activated = (await session.execute(select(func.count(Referral.id)).where(
        Referral.status == ReferralStatus.activated))).scalar_one() or 0
    referral_bonus_days = (await session.execute(select(func.coalesce(func.sum(Referral.bonus_days_awarded), 0)).where(
        Referral.status == ReferralStatus.activated))).scalar_one() or 0

    expiring_soon = (await session.execute(select(func.count(Subscription.id)).where(
        Subscription.status == SubscriptionStatus.expires_soon))).scalar_one() or 0

    context = {
        "request": request, "admin": admin, "page_title": "Бизнес-аналитика",
        "total_users": total_users, "new_today": new_today, "new_7d": new_7d, "new_30d": new_30d,
        "blocked_users": blocked_users, "active_subs": active_subs, "expired_subs": expired_subs,
        "rev_today": rev_today, "rev_7d": rev_7d, "rev_30d": rev_30d,
        "paid_today": paid_today, "paid_7d": paid_7d, "paid_30d": paid_30d,
        "avg_ticket": round(avg_ticket, 2), "arpu": round(arpu, 2), "mrr_forecast": round(mrr_forecast, 2),
        "tariff_stats": tariff_stats,
        "referral_total": referral_total, "referral_activated": referral_activated,
        "referral_bonus_days": referral_bonus_days, "expiring_soon": expiring_soon,
    }
    return templates.TemplateResponse("analytics.html", context)
