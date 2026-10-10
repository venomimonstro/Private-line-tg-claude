"""Restore a paid subscription's missing Marzban user without changing its UUID.

Run on the MAIN server, not the VPN node:
 docker compose exec -T admin python -m app.reconcile_marzban_user --subscription 3
 docker compose exec -T admin python -m app.reconcile_marzban_user --subscription 3 --apply

Fail closed on expired subscriptions, ambiguous keys, unexpected Marzban user,
UUID mismatch or Reality mismatch. Does not rotate credentials or touch Xray.
"""
from __future__ import annotations

import argparse
import asyncio
import uuid
from datetime import datetime
from urllib.parse import parse_qs, urlsplit

from sqlalchemy import select

from app.database import async_session_maker
from app.enums import SubscriptionStatus, VpnKeyStatus
from app.models.subscription import Subscription
from app.models.vpn_key import VpnKey
from app.models.vpn_server import VpnServer
from app.services.vpn_key import (
    _make_client, _server_protocol, _marzban_username, get_access_url,
)

ALLOWED = {SubscriptionStatus.active, SubscriptionStatus.expires_soon}


async def run(sub_id: int, apply: bool) -> int:
    async with async_session_maker() as db:
        subscription = await db.get(Subscription, sub_id)
        if subscription is None:
            print("STOP: subscription not found")
            return 2
        if (
            subscription.status not in ALLOWED
            or subscription.expires_at is None
            or subscription.expires_at <= datetime.utcnow()
        ):
            print("STOP: subscription is not active and unexpired")
            return 2

        keys = (await db.execute(
            select(VpnKey).where(
                VpnKey.subscription_id == sub_id,
                VpnKey.status == VpnKeyStatus.active,
            )
        )).scalars().all()
        if len(keys) != 1:
            print(f"STOP: expected one active key, found {len(keys)}")
            return 2
        key = keys[0]
        server = await db.get(VpnServer, key.server_id)
        if server is None or _server_protocol(server) != "marzban":
            print("STOP: not a Marzban VPN node")
            return 2

        expected_name = _marzban_username(subscription.user_id, sub_id)
        if key.user_id != subscription.user_id or key.outline_key_id != expected_name:
            print("STOP: stored key identity differs from subscription")
            return 2
        link = urlsplit(get_access_url(key))
        args = parse_qs(link.query)
        opts = lambda name: args.get(name, [""])[0]
        try:
            original_uuid = str(uuid.UUID(link.username or ""))
        except ValueError:
            print("STOP: stored VLESS UUID is invalid")
            return 2
        if (
            link.scheme != "vless"
            or opts("security") != "reality"
            or opts("flow") != "xtls-rprx-vision"
            or opts("pbk") == ""
            or opts("sid") == ""
        ):
            print("STOP: stored VLESS link lacks required Reality fields")
            return 2

        client = _make_client(server)
        try:
            if not await client.check_connection():
                print("STOP: Marzban API authentication failed")
                return 2
            config = await client._get_core_config()
            reality = await client.get_reality_params()
            if not config or not reality:
                print("STOP: Reality configuration is unavailable")
                return 2
            pubkey, _, sni = reality
            reality_inbounds = [
                i for i in config.get("inbounds", [])
                if i.get("protocol") == "vless"
                and i.get("streamSettings", {}).get("security") == "reality"
            ]
            if len(reality_inbounds) != 1:
                print("STOP: expected one Reality inbound")
                return 2
            inbound = reality_inbounds[0]
            tag = inbound.get("tag")
            port = inbound.get("port")
            sids = inbound.get("streamSettings", {}).get("realitySettings", {}).get("shortIds", [])
            if (
                not tag
                or not isinstance(port, int)
                or link.port != port
                or link.hostname != urlsplit(server.api_url).hostname
                or opts("pbk") != pubkey
                or opts("sid") not in sids
                or opts("sni") != sni
            ):
                print("STOP: saved link differs from the live Reality inbound")
                return 2

            headers = await client._headers()
            url = f"{client._base}/api/user/{expected_name}"
            resp = await client._client.get(url, headers=headers)
            if resp.status_code == 200:
                current = resp.json()
                current_uuid = current.get("proxies", {}).get("vless", {}).get("id")
                current_tags = current.get("inbounds", {}).get("vless", [])
                current_flow = current.get("proxies", {}).get("vless", {}).get("flow")
                current_status = current.get("status")
                if (
                    current_uuid == original_uuid
                    and tag in current_tags
                    and current_flow == "xtls-rprx-vision"
                    and current_status == "active"
                ):
                    print("PASS: Marzban user exists and matches the saved key; no modifications")
                    return 0
                print("STOP: Marzban username already exists with mismatched config; do not overwrite")
                return 2
            if resp.status_code != 404:
                print(f"STOP: Marzban user lookup returned HTTP {resp.status_code}")
                return 2

            print(
                f"MISSING: Marzban user for subscription {sub_id}; "
                f"same UUID will be preserved; inbound={tag}"
            )
            if not apply:
                print("DRY RUN: re-run with --apply to restore only the missing user")
                return 1

            body = {
                "username": expected_name,
                "proxies": {"vless": {"id": original_uuid, "flow": "xtls-rprx-vision"}},
                "inbounds": {"vless": [tag]},
                "status": "active",
                "data_limit_reset_strategy": "no_reset",
                "data_limit": max(0, int((key.traffic_limit_gb or 0) * 1024**3)),
            }
            created = await client._client.post(
                f"{client._base}/api/user", headers=headers, json=body
            )
            if created.status_code not in (200, 201):
                print(f"FAIL: create Marzban user returned HTTP {created.status_code}")
                return 2

            checked = await client._client.get(url, headers=headers)
            if checked.status_code != 200:
                print(f"FAIL: new Marzban user not readable (HTTP {checked.status_code})")
                return 2
            obj = checked.json()
            if (
                obj.get("proxies", {}).get("vless", {}).get("id") != original_uuid
                or tag not in obj.get("inbounds", {}).get("vless", [])
                or obj.get("proxies", {}).get("vless", {}).get("flow") != "xtls-rprx-vision"
                or obj.get("status") != "active"
            ):
                print("FAIL: created Marzban user does not match the stored VPN key")
                return 2
            print("RESTORED: missing Marzban user recreated with original UUID")
            print("NEXT: perform actual VLESS end-to-end test, not only API checks")
            return 0
        except Exception as exc:
            print(f"FAIL: {type(exc).__name__}: {str(exc)[:160]}")
            return 2
        finally:
            await client.aclose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--subscription", type=int, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if args.subscription <= 0:
        parser.error("subscription must be positive")
    raise SystemExit(asyncio.run(run(args.subscription, args.apply)))
