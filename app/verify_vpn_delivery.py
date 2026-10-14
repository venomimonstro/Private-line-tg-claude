"""Verify the actual subscription endpoint and Telegram-client link without secrets.

Run on the MAIN server:
  docker compose exec -T admin python -m app.verify_vpn_delivery --subscription 3

Checks DB active key, canonical link, and subscription endpoint through Nginx
using the user's generated HMAC token. Outputs status only, never a VPN URL.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
from datetime import datetime
import time
from urllib.parse import urlsplit

import httpx
from sqlalchemy import select

from app.config import settings
from app.database import async_session_maker
from app.enums import SubscriptionStatus, VpnKeyStatus, VpnServerStatus
from app.models.subscription import Subscription
from app.models.user import User
from app.models.vpn_key import VpnKey
from app.models.vpn_server import VpnServer
from app.services.sub_token import make_sub_token
from app.services.vpn_key import get_access_url
from app.services.vpn_link import canonical_client_link
from app.services.vpn_eligibility import is_vless_node


async def verify(subscription_id: int) -> int:
    async with async_session_maker() as db:
        sub = await db.get(Subscription, subscription_id)
        if (
            sub is None
            or sub.status not in (SubscriptionStatus.active, SubscriptionStatus.expires_soon)
            or not sub.expires_at
            or sub.expires_at <= datetime.utcnow()
        ):
            print("FAIL: subscription is inactive or expired")
            return 2
        user = await db.get(User, sub.user_id)
        if user is None or not user.telegram_id:
            print("FAIL: Telegram recipient is missing")
            return 2
        # Mirror the public /sub endpoint exactly for this Telegram user.
        active = (await db.execute(
            select(VpnKey, VpnServer)
            .join(Subscription, Subscription.id == VpnKey.subscription_id)
            .join(VpnServer, VpnServer.id == VpnKey.server_id)
            .where(
                VpnKey.user_id == sub.user_id,
                VpnKey.status == VpnKeyStatus.active,
                Subscription.status.in_([
                    SubscriptionStatus.active,
                    SubscriptionStatus.expires_soon,
                ]),
                Subscription.expires_at > datetime.utcnow(),
                VpnServer.status != VpnServerStatus.disabled,
                VpnServer.provider != "private-line-primary",
            )
            .order_by(VpnServer.id.asc(), VpnKey.id.asc())
        )).all()
        active = [(key, server) for key, server in active if is_vless_node(server)]
        if not active:
            print("FAIL: no active VPN keys")
            return 2
        expected_links = set()
        for key, server in active:
            try:
                label = key.label or f"{server.flag_emoji or '🌍'} {server.country}"
                if server.status == VpnServerStatus.unreachable:
                    label = f"{label} • резерв"
                expected = canonical_client_link(get_access_url(key), label)
                parsed = urlsplit(expected)
                if (
                    expected.count("#") != 1
                    or parsed.scheme != "vless"
                    or not parsed.username or not parsed.hostname
                    or not parsed.port
                    or "security=reality" not in parsed.query
                    or "fp=chrome" not in parsed.query
                    or "flow=xtls-rprx-vision" not in parsed.query
                    or not parsed.fragment
                ):
                    raise ValueError("Invalid VLESS profile")
                expected_links.add(expected)
            except Exception as exc:
                print(f"FAIL: client-ready link invalid ({type(exc).__name__})")
                return 2
        token = make_sub_token(user.telegram_id, settings.SECRET_KEY)
        # Release the read transaction before making an HTTP request that also
        # needs a DB connection. This avoids coupling availability to pool
        # pressure during a low-memory deployment.
        await db.rollback()

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(25.0, connect=3.0), trust_env=False
    ) as client:
        for stage, url in (
            ("FastAPI", f"http://127.0.0.1:8000/sub/{token}"),
            ("Nginx readiness", "http://nginx:29148/admin/login"),
            ("Nginx subscription", f"http://nginx:29148/sub/{token}"),
        ):
            started = time.monotonic()
            try:
                response = await client.get(url, follow_redirects=False)
            except httpx.RequestError as exc:
                print(f"FAIL: {stage} request {type(exc).__name__} after {time.monotonic()-started:.1f}s")
                return 2
            print(f"CHECK: {stage} HTTP {response.status_code} in {time.monotonic()-started:.1f}s")
            if stage == "Nginx readiness":
                if response.status_code != 200:
                    print("FAIL: Nginx admin route is not ready")
                    return 2
                continue
            if response.status_code != 200:
                print(f"FAIL: {stage} subscription route HTTP {response.status_code}")
                return 2
            if stage == "FastAPI":
                try:
                    direct = base64.b64decode(response.text, validate=True).decode("utf-8").splitlines()
                except Exception:
                    print("FAIL: FastAPI subscription is not base64 VLESS")
                    return 2
                if set(direct) != expected_links:
                    print("FAIL: FastAPI subscription differs from active client-ready keys")
                    return 2
    try:
        lines = base64.b64decode(response.text, validate=True).decode("utf-8").splitlines()
    except Exception:
        print("FAIL: Nginx subscription endpoint is not base64 VLESS")
        return 2
    if set(lines) != expected_links:
        print("FAIL: Nginx subscription differs from active client-ready keys")
        return 2
    print(f"PASS: user for subscription {subscription_id} has {len(active)} verified client-ready key(s)")
    print("PASS: Nginx -> FastAPI subscription endpoint exactly matches verified active keys")
    print("NOTE: this does NOT prove connectivity from a remote user's ISP or client app")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--subscription", type=int, required=True)
    a = parser.parse_args()
    if a.subscription <= 0:
        parser.error("Subscription must be positive")
    raise SystemExit(asyncio.run(verify(a.subscription)))
