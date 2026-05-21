from __future__ import annotations
import logging
from datetime import datetime, timedelta
from decimal import Decimal
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.config import settings
from app.enums import PaymentStatus, SubscriptionStatus, VpnKeyStatus
from app.models.payment import Payment
from app.models.refund_request import RefundRequest
from app.models.subscription import Subscription
from app.models.vpn_key import VpnKey
from app.services.notification import notify_admin

logger = logging.getLogger(__name__)


async def check_refund_eligibility(
    payment_id: int,
    user_id: int,
    session: AsyncSession,
) -> tuple[bool, str]:
    """Return (eligible, reason_if_not)."""
    payment = await session.get(Payment, payment_id)
    if payment is None or payment.user_id != user_id:
        return False, "Платёж не найден."
    if payment.status != PaymentStatus.paid:
        return False, "Платёж не был оплачен."

    age = datetime.utcnow() - payment.created_at
    if age > timedelta(days=settings.REFUND_WINDOW_DAYS):
        return False, f"Срок возврата истёк (только в течение {settings.REFUND_WINDOW_DAYS} дней после оплаты)."

    # Check if refund request already exists
    existing = await session.execute(
        select(RefundRequest).where(
            RefundRequest.payment_id == payment_id,
            RefundRequest.status.in_(["pending", "approved", "auto_approved"]),
        )
    )
    if existing.scalar_one_or_none() is not None:
        return False, "Заявка на возврат уже существует."

    # Check traffic used
    traffic_mb = await _get_traffic_used_mb(payment, session)
    if traffic_mb > settings.REFUND_MAX_TRAFFIC_MB:
        return False, (
            f"Использовано {traffic_mb:.0f} МБ трафика. "
            f"Возврат доступен при использовании менее {settings.REFUND_MAX_TRAFFIC_MB} МБ."
        )

    return True, ""


async def _get_traffic_used_mb(payment: Payment, session: AsyncSession) -> float:
    if payment.subscription_id is None:
        return 0.0
    keys_result = await session.execute(
        select(VpnKey).where(VpnKey.subscription_id == payment.subscription_id)
    )
    keys = keys_result.scalars().all()
    total_gb = sum(float(k.traffic_used_gb or 0) for k in keys)
    return total_gb * 1024


async def create_refund_request(
    payment_id: int,
    user_id: int,
    reason: str,
    session: AsyncSession,
) -> RefundRequest | None:
    eligible, msg = await check_refund_eligibility(payment_id, user_id, session)
    if not eligible:
        return None

    payment = await session.get(Payment, payment_id)
    if payment is None:
        return None

    traffic_mb = await _get_traffic_used_mb(payment, session)

    req = RefundRequest(
        user_id=user_id,
        payment_id=payment_id,
        reason=reason,
        status="pending",
        traffic_used_mb_at_request=Decimal(str(round(traffic_mb, 2))),
        created_at=datetime.utcnow(),
    )
    session.add(req)
    await session.flush()

    await notify_admin(
        f"💸 Заявка на возврат #{req.id}\n"
        f"Пользователь: {user_id}\n"
        f"Платёж: #{payment_id} ({payment.amount} {payment.currency})\n"
        f"Трафик: {traffic_mb:.1f} МБ\n"
        f"Причина: {reason}"
    )
    await session.commit()
    return req


async def process_refund(
    refund_id: int,
    admin_id: int,
    approve: bool,
    admin_comment: str,
    session: AsyncSession,
) -> None:
    """Approve or deny a refund request. Caller commits."""
    req = await session.get(RefundRequest, refund_id)
    if req is None:
        raise ValueError(f"Refund request {refund_id} not found")

    req.status = "approved" if approve else "denied"
    req.admin_comment = admin_comment
    req.processed_by_id = admin_id
    req.processed_at = datetime.utcnow()

    if approve:
        # Load payment explicitly to avoid lazy loading issues
        payment = await session.get(Payment, req.payment_id)
        # Disable the subscription and its VPN keys
        if payment is not None and payment.subscription_id is not None:
            sub = await session.get(Subscription, payment.subscription_id)
            if sub is not None:
                sub.status = SubscriptionStatus.disabled
                # Disable VPN keys
                keys_result = await session.execute(
                    select(VpnKey).where(
                        VpnKey.subscription_id == sub.id,
                        VpnKey.status == VpnKeyStatus.active,
                    )
                )
                from app.services import vpn_key as vpn_key_service
                from app.models.vpn_server import VpnServer
                for key in keys_result.scalars().all():
                    server = await session.get(VpnServer, key.server_id)
                    if server:
                        await vpn_key_service.disable_vpn_key(key, server, session)

        # Notify user
        bot = _get_bot()
        if bot is not None:
            try:
                await bot.send_message(
                    chat_id=req.user_id,
                    text=(
                        f"✅ Ваша заявка на возврат #{req.id} одобрена.\n"
                        f"Средства будут возвращены в течение 3–5 рабочих дней.\n"
                        f"Комментарий: {admin_comment or 'нет'}"
                    ),
                )
            except Exception as exc:
                logger.error("Failed to notify user %s about refund: %s", req.user_id, exc)
    else:
        bot = _get_bot()
        if bot is not None:
            try:
                await bot.send_message(
                    chat_id=req.user_id,
                    text=(
                        f"❌ Заявка на возврат #{req.id} отклонена.\n"
                        f"Комментарий: {admin_comment or 'нет'}"
                    ),
                )
            except Exception as exc:
                logger.error("Failed to notify user %s about refund deny: %s", req.user_id, exc)


def _get_bot():
    try:
        from app.bot_instance import get_bot
        return get_bot()
    except Exception:
        return None
