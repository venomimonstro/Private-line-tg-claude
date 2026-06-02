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

import logging

from fastapi import APIRouter, Depends, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_async_session
from app.enums import VpnKeyStatus
from app.models.user import User
from app.models.vpn_key import VpnKey
from app.services.sub_token import make_sub_token, verify_sub_token as _verify_sub_token
from app.services.vpn_key import get_access_url

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
        select(VpnKey).where(
            VpnKey.user_id == user.id,
            VpnKey.status == VpnKeyStatus.active,
        )
    )
    keys = keys_result.scalars().all()

    links: list[str] = []
    for key in keys:
        try:
            url = get_access_url(key)
            if url:
                label = key.label or "Private Line VPN"
                links.append(f"{url}#{label}")
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
