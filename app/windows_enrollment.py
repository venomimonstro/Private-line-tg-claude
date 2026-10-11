"""Secure one-time Windows pairing and scoped, revocable device sessions.

A Telegram-authenticated user obtains a personalised installer containing only
a single-use enrollment code (not a VLESS credential). Pairing validates current
subscription access and produces a per-installation bearer token. Only SHA256
token digests are stored in the control-plane database.
"""
from __future__ import annotations

import hashlib
import json
import secrets
from datetime import datetime, timedelta
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.enums import SubscriptionStatus, VpnKeyStatus, VpnServerStatus
from app.models.app_setting import AppSetting
from app.models.subscription import Subscription
from app.models.vpn_key import VpnKey
from app.models.vpn_server import VpnServer
from app.services.vpn_key import get_client_access_url, _server_protocol


PAIR_TTL = timedelta(hours=24)
DEVICE_TTL = timedelta(days=90)
PAIR_PREFIX = "winpair_"
DEVICE_PREFIX = "windevice_"


def public_base_url() -> str:
    base = (settings.CONTROL_PLANE_PUBLIC_URL or settings.WEBHOOK_BASE_URL or "").strip().rstrip("/")
    url = urlsplit(base)
    if url.scheme != "https" or not url.hostname or url.username or url.password or url.query or url.fragment or url.path not in ("", "/"):
        raise ValueError("CONTROL_PLANE_PUBLIC_URL must be an HTTPS origin for Windows auto-enrollment")
    return base


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("ascii")).hexdigest()


async def active_user_link(session: AsyncSession, user_id: int) -> str | None:
    """Never give a device credentials for an expired or deactivated subscription."""
    rows = (await session.execute(
        select(VpnKey, VpnServer)
        .join(Subscription, Subscription.id == VpnKey.subscription_id)
        .join(VpnServer, VpnServer.id == VpnKey.server_id)
        .where(
            VpnKey.user_id == user_id,
            VpnKey.status == VpnKeyStatus.active,
            Subscription.user_id == user_id,
            Subscription.status.in_([SubscriptionStatus.active, SubscriptionStatus.expires_soon]),
            Subscription.expires_at > datetime.utcnow(),
            VpnServer.status != VpnServerStatus.disabled,
            VpnServer.provider != "private-line-primary",
        )
        .order_by(VpnKey.id)
    )).all()
    for key, server in rows:
        if _server_protocol(server) == "marzban":
            return get_client_access_url(key)
    return None


async def create_enrollment(session: AsyncSession, user_id: int, *, base_url: str | None = None) -> tuple[str, str, str]:
    base = (base_url or "").strip().rstrip("/") or public_base_url()
    url = urlsplit(base)
    if url.scheme != "https" or not url.hostname or url.username or url.password or url.query or url.fragment or url.path not in ("", "/"):
        raise ValueError("Windows installer requires an HTTPS control-plane origin")
    pin = (settings.CONTROL_PLANE_TLS_PIN or "").strip()
    if not pin.startswith("sha256//") or len(pin) < 50:
        raise ValueError("CONTROL_PLANE_TLS_PIN is not configured for Windows enrollment")
    if not await active_user_link(session, user_id):
        raise ValueError("No active Windows-compatible VPN subscription")
    code = secrets.token_urlsafe(32)
    record = {"user_id": user_id, "expires": (datetime.utcnow() + PAIR_TTL).isoformat()}
    session.add(AppSetting(key=PAIR_PREFIX + digest(code), value=json.dumps(record),
                           description="Single-use Windows enrollment code"))
    await session.commit()
    return base, code, pin


async def redeem_enrollment(session: AsyncSession, code: str) -> str | None:
    if not isinstance(code, str) or len(code) < 35 or len(code) > 128 or not code.isascii():
        return None
    key = PAIR_PREFIX + digest(code)
    # SELECT FOR UPDATE makes redemption atomic across multiple API workers.
    row = (await session.execute(
        select(AppSetting).where(AppSetting.key == key).with_for_update()
    )).scalar_one_or_none()
    if row is None:
        return None
    try:
        data = json.loads(row.value or "{}")
        uid = int(data["user_id"])
        deadline = datetime.fromisoformat(data["expires"])
    except (ValueError, TypeError, KeyError):
        return None
    if datetime.utcnow() >= deadline or not await active_user_link(session, uid):
        await session.delete(row)
        await session.commit()
        return None
    device_token = secrets.token_urlsafe(40)
    expires = datetime.utcnow() + DEVICE_TTL
    await session.delete(row)
    session.add(AppSetting(
        key=DEVICE_PREFIX + digest(device_token),
        value=json.dumps({"user_id": uid, "expires": expires.isoformat()}),
        description="Windows VPN device bearer SHA256; remove record to revoke",
    ))
    await session.commit()
    return device_token


async def authenticated_device(session: AsyncSession, authorization: str) -> int | None:
    if not authorization.startswith("Bearer "):
        return None
    raw = authorization[7:]
    if len(raw) < 45 or len(raw) > 128 or not raw.isascii():
        return None
    row = await session.get(AppSetting, DEVICE_PREFIX + digest(raw))
    if not row:
        return None
    try:
        data = json.loads(row.value or "{}")
        if datetime.utcnow() >= datetime.fromisoformat(data["expires"]):
            return None
        return int(data["user_id"])
    except (ValueError, KeyError, TypeError):
        return None
