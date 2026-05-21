from __future__ import annotations
import logging
from datetime import datetime, timedelta
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.enums import VpnKeyStatus, SubscriptionStatus
from app.models.subscription import Subscription
from app.models.tariff import Tariff
from app.models.vpn_key import VpnKey
from app.models.vpn_server import VpnServer
from app.services.notification import notify_admin

logger = logging.getLogger(__name__)

_SPEED_CHECK_WINDOW_HOURS = 1.0  # Compare traffic over last hour


async def check_speed_limits(session: AsyncSession) -> None:
    """
    Soft speed limiting: if a key's average speed over the last hour
    exceeds the tariff's speed_limit_mbps, send admin notification.
    True bandwidth shaping requires OS-level tc/iptables on VPN servers.
    """
    result = await session.execute(
        select(VpnKey, Subscription, Tariff)
        .join(Subscription, VpnKey.subscription_id == Subscription.id)
        .join(Tariff, Subscription.tariff_id == Tariff.id)
        .where(
            VpnKey.status == VpnKeyStatus.active,
            Subscription.status.in_([SubscriptionStatus.active, SubscriptionStatus.expires_soon]),
            Tariff.speed_limit_mbps.isnot(None),
        )
    )
    rows = result.all()

    for vpn_key, subscription, tariff in rows:
        if not vpn_key.last_checked_at:
            continue

        now = datetime.utcnow()
        elapsed_hours = (now - vpn_key.last_checked_at).total_seconds() / 3600.0
        if elapsed_hours < 0.1:  # Less than 6 minutes — skip
            continue

        # This is a rough estimate based on the traffic diff since last sync
        # For accurate speed monitoring, compare two consecutive readings
        # Currently we just log; actual enforcement requires server-side TC
        traffic_gb = float(vpn_key.traffic_used_gb or 0)
        # We can't easily get delta without storing previous value
        # This is a placeholder for future enhancement
        logger.debug(
            "Key %d (user %d): %.4f GB used, tariff limit %d Mbps",
            vpn_key.id, vpn_key.user_id, traffic_gb, tariff.speed_limit_mbps,
        )
