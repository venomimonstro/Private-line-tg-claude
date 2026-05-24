from __future__ import annotations

import base64
import logging
import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import httpx

logger = logging.getLogger(__name__)


@dataclass
class MarzbanKey:
    id: str        # Marzban username (used as key identifier)
    name: str
    access_url: str
    port: int | None


class MarzbanClient:
    """Client for the Marzban panel REST API (XRay/VLESS backend)."""

    def __init__(self, api_url: str, username: str, password: str) -> None:
        self._base = api_url.rstrip("/")
        self._username = username
        self._password = password
        self._token: str | None = None
        self._client = httpx.AsyncClient(verify=False, timeout=15.0)
        self._reality_cache: tuple[str, str, str] | None = None  # (pubkey, short_id, sni)

    async def _auth(self) -> str:
        resp = await self._client.post(
            f"{self._base}/api/admin/token",
            data={"username": self._username, "password": self._password},
        )
        resp.raise_for_status()
        self._token = resp.json()["access_token"]
        return self._token

    async def _headers(self) -> dict[str, str]:
        if self._token is None:
            await self._auth()
        return {"Authorization": f"Bearer {self._token}"}

    async def get_reality_params(self) -> tuple[str, str, str] | None:
        """Return (public_key, short_id, sni) from the server's XRay config.

        Calls /api/core/config, finds the first inbound with security=reality,
        derives the x25519 public key from the stored private key, and returns
        everything needed to build a valid Reality VLESS link.
        """
        if self._reality_cache is not None:
            return self._reality_cache
        try:
            headers = await self._headers()
            resp = await self._client.get(f"{self._base}/api/core/config", headers=headers)
            if resp.status_code != 200:
                return None
            config = resp.json()
            for inbound in config.get("inbounds", []):
                stream = inbound.get("streamSettings", {})
                if stream.get("security") != "reality":
                    continue
                reality = stream.get("realitySettings", {})
                priv_b64 = reality.get("privateKey", "")
                short_ids = reality.get("shortIds", [])
                server_names = reality.get("serverNames", [])
                if not priv_b64 or not short_ids:
                    continue

                from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
                from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

                # XRay encodes keys as base64url without padding
                padded = priv_b64 + "=" * (-len(priv_b64) % 4)
                priv_key = X25519PrivateKey.from_private_bytes(base64.urlsafe_b64decode(padded))
                pub_bytes = priv_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
                public_key = base64.urlsafe_b64encode(pub_bytes).rstrip(b"=").decode()

                short_id = next((s for s in short_ids if s), "")
                sni = server_names[0] if server_names else "www.microsoft.com"

                self._reality_cache = (public_key, short_id, sni)
                return self._reality_cache
        except Exception as exc:
            logger.warning("Could not fetch Reality params from Marzban: %s", exc)
        return None

    def _inject_reality(self, url: str, public_key: str, short_id: str, sni: str) -> str:
        """Return url with all Reality params set, including required flow."""
        if not url or not url.startswith("vless://"):
            return url
        parsed = urlparse(url)
        qs = {k: v[0] for k, v in parse_qs(parsed.query, keep_blank_values=True).items()}
        qs["security"] = "reality"
        qs["pbk"] = public_key
        qs["sni"] = sni
        qs["fp"] = "chrome"
        qs["type"] = "tcp"
        qs["flow"] = "xtls-rprx-vision"
        if short_id:
            qs["sid"] = short_id
        return urlunparse(parsed._replace(query=urlencode(qs)))

    async def create_key(self, name: str, traffic_limit_gb: float | None = None) -> MarzbanKey:
        headers = await self._headers()
        safe_name = name[:32]
        body: dict = {
            "username": safe_name,
            "proxies": {"vless": {"flow": "xtls-rprx-vision"}},
            "inbounds": {"vless": ["VLESS-TCP"]},
            "status": "active",
            "data_limit_reset_strategy": "no_reset",
        }
        if traffic_limit_gb and traffic_limit_gb > 0:
            body["data_limit"] = int(traffic_limit_gb * 1024 ** 3)

        resp = await self._client.post(f"{self._base}/api/user", headers=headers, json=body)
        if resp.status_code == 409:
            # User already exists — update config to ensure flow+inbound are correct, then fetch
            await self._client.put(
                f"{self._base}/api/user/{safe_name}",
                headers=headers,
                json={
                    "proxies": {"vless": {"flow": "xtls-rprx-vision"}},
                    "inbounds": {"vless": ["VLESS-TCP"]},
                    "status": "active",
                },
            )
            get_resp = await self._client.get(f"{self._base}/api/user/{safe_name}", headers=headers)
            get_resp.raise_for_status()
            data = get_resp.json()
        else:
            resp.raise_for_status()
            data = resp.json()

        links: list[str] = data.get("links", [])
        vless_links = [lnk for lnk in links if lnk.startswith("vless://")]

        # Fallback: decode Marzban subscription URL (base64-encoded link list)
        if not vless_links:
            sub_path = data.get("subscription_url", "")
            if sub_path:
                full_sub = sub_path if sub_path.startswith("http") else f"{self._base}{sub_path}"
                try:
                    sub_resp = await self._client.get(full_sub, headers=headers)
                    if sub_resp.status_code == 200:
                        decoded = base64.b64decode(sub_resp.text.strip()).decode("utf-8")
                        vless_links = [ln for ln in decoded.strip().split("\n") if ln.startswith("vless://")]
                except Exception:
                    pass

        if vless_links:
            access_url = vless_links[0]
            # Fix bind address 0.0.0.0 → server's public IP from API URL
            if "@0.0.0.0:" in access_url:
                m = re.match(r'https?://([^:/]+)', self._base)
                if m:
                    access_url = re.sub(r'@0\.0\.0\.0:', f'@{m.group(1)}:', access_url)
        else:
            # Last resort: build minimal URL from UUID
            uuid = data.get("proxies", {}).get("vless", {}).get("id", "")
            if uuid:
                m = re.match(r'https?://([^:/]+)', self._base)
                host = m.group(1) if m else "unknown"
                access_url = f"vless://{uuid}@{host}:10443?type=tcp&security=none#{safe_name}"
            else:
                access_url = ""

        # Guarantee Reality params are present.
        # Marzban omits pbk/sid when the hosts API hasn't been configured yet
        # or when the user was created before Reality was set up.
        if access_url and "pbk=" not in access_url:
            reality = await self.get_reality_params()
            if reality:
                public_key, short_id, sni = reality
                access_url = self._inject_reality(access_url, public_key, short_id, sni)
                logger.info(
                    "Injected Reality params into VLESS link for %s (pbk=%s...)",
                    safe_name, public_key[:8],
                )
            else:
                logger.warning(
                    "VLESS link for %s is missing Reality params and server config is unavailable. "
                    "Run fix_reality.sh on the node.",
                    safe_name,
                )

        return MarzbanKey(id=data["username"], name=name, access_url=access_url, port=None)

    async def delete_key(self, username: str) -> None:
        headers = await self._headers()
        resp = await self._client.delete(f"{self._base}/api/user/{username}", headers=headers)
        if resp.status_code == 404:
            return
        resp.raise_for_status()

    async def get_transfer_stats(self) -> dict[str, int]:
        """Return {username: bytes_used} for all users."""
        headers = await self._headers()
        resp = await self._client.get(
            f"{self._base}/api/users",
            headers=headers,
            params={"limit": 1000},
        )
        resp.raise_for_status()
        data = resp.json()
        return {u["username"]: u.get("used_traffic", 0) for u in data.get("users", [])}

    async def check_connection(self) -> bool:
        try:
            await self._auth()
            return True
        except Exception as exc:
            logger.warning("Marzban connection failed: %s", exc)
            return False

    async def aclose(self) -> None:
        await self._client.aclose()
