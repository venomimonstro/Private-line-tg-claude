"""Public subscription endpoint — no admin auth required.

VPN apps (Hiddify, V2RayNG, V2Box, Streisand) periodically GET this URL and
auto-apply the latest config.  When repair_marzban_keys() updates the stored
access_url in the DB the next app refresh picks it up automatically — the user
never needs to touch their client manually.

Token format:  base64url(telegram_id) + "." + hmac_sha256(SECRET_KEY, telegram_id)[:12]
- Stable: same user → same token always
- Secure: can't guess another user's token without SECRET_KEY
- No DB migration needed — derived on the fly
"""
from __future__ import annotations

import base64
import logging
from datetime import datetime

from fastapi import APIRouter, Depends, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_async_session
from app.enums import SubscriptionStatus, VpnKeyStatus, VpnServerStatus
from app.models.user import User
from app.models.subscription import Subscription
from app.models.vpn_key import VpnKey
from app.models.vpn_server import VpnServer
from app.services.sub_token import make_sub_token, verify_sub_token as _verify_sub_token
from app.services.vpn_key import get_client_access_url

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
        select(VpnKey)
        .join(Subscription, Subscription.id == VpnKey.subscription_id)
        .join(VpnServer, VpnServer.id == VpnKey.server_id)
        .where(
            VpnKey.user_id == user.id,
            VpnKey.status == VpnKeyStatus.active,
            Subscription.status.in_([SubscriptionStatus.active, SubscriptionStatus.expires_soon]),
            Subscription.expires_at > datetime.utcnow(),
            VpnServer.status == VpnServerStatus.active,
            VpnServer.is_selectable.is_(True),
            VpnServer.provider != "private-line-primary",
        )
        .order_by(VpnServer.id.asc(), VpnKey.id.asc())
    )
    keys = keys_result.scalars().all()

    links: list[str] = []
    for key in keys:
        try:
            url = get_client_access_url(key)
            if url:
                # The canonical share URI already has exactly one #fragment.
                # Never append another one: a duplicate breaks some importers.
                if url not in links:
                    links.append(url)
        except Exception as exc:
            logger.warning("sub: cannot decrypt key %d: %s", key.id, exc)

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
        },
    )
