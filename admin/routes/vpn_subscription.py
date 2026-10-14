"""Public subscription endpoint — no admin auth required.

VPN apps (Hiddify, V2RayNG, V2Box, Streisand) periodically GET this URL and
auto-apply the latest config. Standalone Xray and legacy Marzban VLESS keys
are both included while their paid subscription remains active.

Token format: base64url(telegram_id) + "." + 128-bit HMAC-SHA256 signature
- Stable: same user → same token always
- Secure: can't guess another user's token without SECRET_KEY
- No DB migration needed — derived on the fly
"""
from __future__ import annotations

import base64
import logging
from datetime import datetime

from fastapi import APIRouter, Depends, Response
from sqlalchemy import case, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_async_session
from app.enums import SubscriptionStatus, VpnKeyStatus, VpnServerStatus
from app.models.user import User
from app.models.subscription import Subscription
from app.models.vpn_key import VpnKey
from app.models.vpn_server import VpnServer
from app.services.sub_token import make_sub_token, verify_sub_token as _verify_sub_token
from app.services.vpn_key import get_access_url
from app.services.vpn_eligibility import is_vless_node
from app.services.vpn_link import canonical_client_link

logger = logging.getLogger(__name__)

router = APIRouter(tags=["subscription"])


@router.get("/sub/{token}")
async def vpn_subscription(
    token: str,
    session: AsyncSession = Depends(get_async_session),
) -> Response:
    """Return user's active VPN keys in base64 subscription format.

    Compatible with: Hiddify, V2RayNG, V2Box, Streisand, FoXray, v2rayA.
    """
    from app.config import settings

    telegram_id = _verify_sub_token(token, settings.SECRET_KEY)
    if telegram_id is None:
        return Response(status_code=404)

    user_result = await session.execute(
        select(User).where(User.telegram_id == telegram_id)
    )
    user = user_result.scalar_one_or_none()
    if user is None:
        return Response(status_code=404)

    keys_result = await session.execute(
        select(VpnKey, VpnServer)
        .join(Subscription, Subscription.id == VpnKey.subscription_id)
        .join(VpnServer, VpnServer.id == VpnKey.server_id)
        .where(
            VpnKey.user_id == user.id,
            VpnKey.status == VpnKeyStatus.active,
            Subscription.status.in_([SubscriptionStatus.active, SubscriptionStatus.expires_soon]),
            Subscription.expires_at > datetime.utcnow(),
            VpnServer.status != VpnServerStatus.disabled,
            VpnServer.provider != "private-line-primary",
        )
        .order_by(
            case((VpnServer.status == VpnServerStatus.active, 0), else_=1),
            VpnServer.failure_count.asc(),
            VpnServer.id.asc(),
            VpnKey.id.asc(),
        )
    )
    rows = [
        (key, server)
        for key, server in keys_result.all()
        if is_vless_node(server)
    ]

    links: list[str] = []
    for key, server in rows:
        try:
            label = key.label or f"{server.flag_emoji or '🌍'} {server.country}"
            if server.status == VpnServerStatus.unreachable:
                label = f"{label} • резерв"
            url = canonical_client_link(get_access_url(key), label)
            if url and url not in links:
                links.append(url)
        except Exception as exc:
            logger.warning("sub: cannot format key %d: %s", key.id, exc)

    if not links:
        return Response(status_code=204)

    content = base64.b64encode("\n".join(links).encode()).decode()
    return Response(
        content=content,
        media_type="text/plain; charset=utf-8",
        headers={
            "profile-title": "Private Line VPN",
            "profile-update-interval": "6",
            "content-disposition": "attachment; filename=subscription.txt",
            "cache-control": "no-store, private",
            "referrer-policy": "no-referrer",
        },
    )
