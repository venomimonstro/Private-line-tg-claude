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
from app.services.vpn_key import get_client_access_url, _server_protocol


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
        active = [key for key, server in active if _server_protocol(server) == "marzban"]
        if not active:
            print("FAIL: no active VPN keys")
            return 2
        expected_links = set()
        for key in active:
            try:
                expected = get_client_access_url(key)
                parsed = urlsplit(expected)
                if (
                    expected.count("#") != 1
                    or parsed.scheme != "vless"
                    or not parsed.username or not parsed.hostname
                    or not parsed.port
                    or "security=reality" not in parsed.query
                    or "fp=chrome" not in parsed.query
                    or "flow=xtls-rprx-vision" not in parsed.query
                    or parsed.fragment != "Private%20Line%20VPN"
                ):
                    raise ValueError("Invalid VLESS profile")
                expected_links.add(expected)
            except Exception as exc:
                print(f"FAIL: client-ready link invalid ({type(exc).__name__})")
                return 2
        token = make_sub_token(user.telegram_id, settings.SECRET_KEY)
        async with httpx.AsyncClient(timeout=10.0, trust_env=False) as client:
            try:
                # Validate the actual published Nginx -> FastAPI -> DB route.
                response = await client.get(
                    f"http://nginx/sub/{token}", follow_redirects=False
                )
            except httpx.RequestError as exc:
                print(f"FAIL: Nginx subscription unreachable ({type(exc).__name__})")
                return 2
        if response.status_code != 200:
            print(f"FAIL: public subscription route HTTP {response.status_code}")
            return 2
        try:
            lines = base64.b64decode(response.text, validate=True).decode("utf-8").splitlines()
        except Exception:
            print("FAIL: subscription endpoint did not return base64 VLESS links")
            return 2
        if set(lines) != expected_links:
            print("FAIL: downloaded subscription does not match all active client-ready keys")
            return 2
        print(f"PASS: user for subscription {subscription_id} has {len(active)} verified client-ready key(s)")
        print("PASS: every profile name is Private Line VPN")
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
