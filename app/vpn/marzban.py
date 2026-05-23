from __future__ import annotations

import logging
from dataclasses import dataclass

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

    async def create_key(self, name: str, traffic_limit_gb: float | None = None) -> MarzbanKey:
        headers = await self._headers()
        # Marzban usernames: 3-32 chars, alphanumeric + underscore
        safe_name = name[:32]
        body: dict = {
            "username": safe_name,
            "proxies": {"vless": {}},
            "inbounds": {"vless": ["VLESS-TCP"]},
            "status": "active",
            "data_limit_reset_strategy": "no_reset",
        }
        if traffic_limit_gb and traffic_limit_gb > 0:
            body["data_limit"] = int(traffic_limit_gb * 1024 ** 3)

        resp = await self._client.post(f"{self._base}/api/user", headers=headers, json=body)
        if resp.status_code == 409:
            # User already exists in Marzban — fetch their data instead
            get_resp = await self._client.get(f"{self._base}/api/user/{safe_name}", headers=headers)
            get_resp.raise_for_status()
            data = get_resp.json()
        else:
            resp.raise_for_status()
            data = resp.json()

        links: list[str] = data.get("links", [])
        vless_links = [lnk for lnk in links if lnk.startswith("vless://")]

        # If Marzban returned no links, try fetching from subscription URL
        if not vless_links:
            sub_path = data.get("subscription_url", "")
            if sub_path:
                full_sub = sub_path if sub_path.startswith("http") else f"{self._base}{sub_path}"
                try:
                    sub_resp = await self._client.get(full_sub, headers=headers)
                    if sub_resp.status_code == 200:
                        import base64 as _b64
                        decoded = _b64.b64decode(sub_resp.text.strip()).decode("utf-8")
                        vless_links = [ln for ln in decoded.strip().split("\n") if ln.startswith("vless://")]
                except Exception:
                    pass

        if vless_links:
            access_url = vless_links[0]
            # Fix 0.0.0.0 — Marzban uses bind address instead of public IP
            if "@0.0.0.0:" in access_url:
                import re as _re
                m = _re.match(r'https?://([^:/]+)', self._base)
                if m:
                    access_url = _re.sub(r'@0\.0\.0\.0:', f'@{m.group(1)}:', access_url)
        else:
            # Last resort — build minimal VLESS URL from UUID
            uuid = data.get("proxies", {}).get("vless", {}).get("id", "")
            if uuid:
                import re as _re
                m = _re.match(r'https?://([^:/]+)', self._base)
                host = m.group(1) if m else "unknown"
                access_url = f"vless://{uuid}@{host}:10443?type=tcp&security=none#{safe_name}"
            else:
                access_url = ""

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
