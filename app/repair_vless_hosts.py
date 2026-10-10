"""Repair a Marzban VLESS Reality host entry without rotating any keys.

Usage (main server):
  docker compose exec -T admin python -m app.repair_vless_hosts --server 1
  docker compose exec -T admin python -m app.repair_vless_hosts --server 1 --apply

Only changes Marzban /api/hosts for the specified, verified inbound.  It does
not change Xray private keys, Marzban users, VLESS UUIDs or subscriptions.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import pathlib
from datetime import datetime, timezone
from urllib.parse import urlsplit

from app.database import async_session_maker
from app.models.vpn_server import VpnServer
from app.services.vpn_key import _make_client, _server_protocol


def _effective_port(value: object, inbound_port: int) -> int | None:
    if value in (None, "", 0, "0"):
        return inbound_port
    try:
        num = int(value)
        return num if 1 <= num <= 65535 else None
    except (TypeError, ValueError):
        return None


def _is_disabled(value: object) -> bool:
    return value is True or (isinstance(value, str) and value.lower() == "true")


async def repair(server_id: int, apply: bool) -> int:
    async with async_session_maker() as session:
        server = await session.get(VpnServer, server_id)
        if not server or _server_protocol(server) != "marzban":
            print("FAIL: Marzban server not found")
            return 2
        base_host = urlsplit(server.api_url).hostname
        if not base_host:
            print("FAIL: server API address is invalid")
            return 2
        client = _make_client(server)

        try:
            if not await client.check_connection():
                print("FAIL: Marzban API authentication failed")
                return 2

            config = await client._get_core_config()
            reality = await client.get_reality_params()
            if not isinstance(config, dict) or reality is None:
                print("FAIL: missing live Xray Reality config")
                return 2

            inbounds = [item for item in config.get("inbounds", [])
                        if item.get("protocol") == "vless"
                        and item.get("streamSettings", {}).get("security") == "reality"]
            if len(inbounds) != 1:
                print(f"FAIL: expected one Reality VLESS inbound, found {len(inbounds)}")
                return 2
            inbound = inbounds[0]
            tag, port = inbound.get("tag"), inbound.get("port")
            if not tag or not isinstance(port, int) or not (1 <= port <= 65535):
                print("FAIL: invalid Reality inbound port/tag")
                return 2
            _, _, sni = reality

            response = await client._client.get(
                f"{client._base}/api/hosts", headers=await client._headers(),
            )
            if response.status_code != 200:
                print(f"FAIL: GET /api/hosts HTTP {response.status_code}")
                return 2
            host_map = response.json()
            if not isinstance(host_map, dict):
                print("FAIL: unexpected Marzban hosts response")
                return 2
            entries = host_map.get(tag)
            if not isinstance(entries, list) or len(entries) != 1 or not isinstance(entries[0], dict):
                print(f"FAIL: expected one host entry for {tag}; found {len(entries) if isinstance(entries, list) else '?'}")
                return 2

            entry = entries[0]
            effective_port = _effective_port(entry.get("port"), port)
            print(
                f"Node: {server.name}; inbound={tag}; "
                f"host_address={entry.get('address', '(missing)')}; "
                f"host_port={entry.get('port')!r} (effective={effective_port}); "
                f"security={entry.get('security')!r}; disabled={entry.get('is_disabled')!r}"
            )
            expected = {
                "address": base_host,
                "port": port,
                "sni": sni,
                # Marzban hosts supports 'inbound_default', 'none' and 'tls'
                # in its API; REALITY is configured in the Xray inbound.
                "security": "inbound_default",
                "fingerprint": "chrome",
                "is_disabled": False,
            }
            change_keys = [k for k, val in expected.items() if entry.get(k) != val]
            if not change_keys:
                print("PASS: Marzban host settings are already correct")
                return 0
            print("Host corrections needed: " + ", ".join(change_keys))
            if not apply:
                print("DRY RUN: no changes applied; rerun with --apply")
                return 1

            backup_dir = pathlib.Path("/app/backups")
            backup_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            filename = "marzban-hosts-server-%d-%s.json" % (
                server_id, datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
            )
            backup = backup_dir / filename
            fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(host_map, stream, ensure_ascii=False, indent=2)
            print(f"Backup saved: {backup}")

            entry.update(expected)
            response = await client._client.put(
                f"{client._base}/api/hosts",
                headers={**await client._headers(), "Content-Type": "application/json"},
                json=host_map,
            )
            if response.status_code != 200:
                print(f"FAIL: PUT /api/hosts HTTP {response.status_code}; previous hosts backed up")
                return 2

            response = await client._client.get(
                f"{client._base}/api/hosts", headers=await client._headers()
            )
            if response.status_code != 200:
                print("FAIL: cannot verify /api/hosts after update")
                return 2
            new_entries = response.json().get(tag)
            if not isinstance(new_entries, list) or len(new_entries) != 1:
                print("FAIL: unexpected host data after update")
                return 2
            new_entry = new_entries[0]
            healthy = (
                new_entry.get("address") == base_host
                and _effective_port(new_entry.get("port"), port) == port
                and not _is_disabled(new_entry.get("is_disabled"))
                and new_entry.get("sni") in (sni, None, "")
                and new_entry.get("security") in ("inbound_default", None, "")
            )
            if not healthy:
                print("FAIL: hosts update did not persist as expected")
                return 2
            print("PASS: Marzban Reality host configuration verified; no keys rotated")
            return 0
        except Exception as exc:
            print(f"FAIL: {type(exc).__name__}: {str(exc)[:150]}")
            return 2
        finally:
            await client.aclose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--server", type=int, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if args.server <= 0:
        parser.error("--server must be positive")
    raise SystemExit(asyncio.run(repair(args.server, args.apply)))
