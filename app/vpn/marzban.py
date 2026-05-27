from __future__ import annotations

import base64
import logging
import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

_MARKDOWN_LINK_RE = re.compile(r'\[([^\]]+)\]\([^)]*\)')

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
        self._inbounds_cache: dict[str, list[str]] | None = None  # {protocol: [tag, ...]}

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
                raw_sni = server_names[0] if server_names else "www.microsoft.com"
                # Strip markdown link formatting that can corrupt xray_config.json
                # e.g. '[www.microsoft.com](https://...)' → 'www.microsoft.com'
                sni = _MARKDOWN_LINK_RE.sub(r"\1", raw_sni)

                self._reality_cache = (public_key, short_id, sni)
                return self._reality_cache
        except Exception as exc:
            logger.warning("Could not fetch Reality params from Marzban: %s", exc)
        return None

    async def get_inbounds(self) -> dict[str, list[str]]:
        """Return {protocol: [inbound_tag, ...]} from the server's /api/inbounds.

        The result is cached per-client instance to avoid extra API calls.
        Falls back to {"vless": ["VLESS-TCP"]} on error — callers should treat
        this as a signal to check logs if connections still fail.
        """
        if self._inbounds_cache is not None:
            return self._inbounds_cache
        try:
            headers = await self._headers()
            resp = await self._client.get(f"{self._base}/api/inbounds", headers=headers)
            if resp.status_code == 200:
                raw: dict = resp.json()
                # Keep only protocols that actually have inbound tags configured
                data = {proto: tags for proto, tags in raw.items() if tags}
                self._inbounds_cache = data
                logger.info("Fetched inbounds from Marzban: %s", data)
                return self._inbounds_cache
            logger.warning(
                "Could not fetch inbounds from Marzban: HTTP %s — using fallback",
                resp.status_code,
            )
        except Exception as exc:
            logger.warning("Could not fetch inbounds from Marzban: %s — using fallback", exc)
        # Safe fallback: do NOT cache so next call retries the server
        return {"vless": ["VLESS-TCP"]}

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

    async def create_key(self, name: str, traffic_limit_gb: float | None = None, ip_limit: int = 0) -> MarzbanKey:
        headers = await self._headers()
        safe_name = name[:32]

        # Resolve actual inbound tags from the server — never hardcode "VLESS-TCP"
        inbounds_map = await self.get_inbounds()
        vless_inbounds = inbounds_map.get("vless") or ["VLESS-TCP"]
        logger.info("Using VLESS inbounds for %s: %s", safe_name, vless_inbounds)

        body: dict = {
            "username": safe_name,
            "proxies": {"vless": {"flow": "xtls-rprx-vision"}},
            "inbounds": {"vless": vless_inbounds},
            "status": "active",
            "data_limit_reset_strategy": "no_reset",
        }
        if ip_limit > 0:
            body["ip_limit"] = ip_limit
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
                    "inbounds": {"vless": vless_inbounds},
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
                access_url = f"vless://{uuid}@{host}:443?type=tcp&security=none#{safe_name}"
            else:
                access_url = ""

        # ALWAYS fetch Reality params from server and inject them.
        # We never trust Marzban's link generation because:
        #   - hosts API may not be configured → pbk/sid/sni missing
        #   - flow=xtls-rprx-vision may be omitted even when server requires it
        #   - security=none may appear instead of security=reality
        # Deriving params from /api/core/config is the single source of truth.
        if access_url:
            reality = await self.get_reality_params()
            if reality:
                public_key, short_id, sni = reality
                access_url = self._inject_reality(access_url, public_key, short_id, sni)
                logger.info(
                    "Applied Reality params for %s: pbk=%s... sid=%s sni=%s flow=xtls-rprx-vision",
                    safe_name, public_key[:8], short_id, sni,
                )
            else:
                # Couldn't reach /api/core/config — at least force flow if security=reality is present
                if "security=reality" in access_url and "flow=" not in access_url:
                    parsed = urlparse(access_url)
                    qs = {k: v[0] for k, v in parse_qs(parsed.query, keep_blank_values=True).items()}
                    qs["flow"] = "xtls-rprx-vision"
                    access_url = urlunparse(parsed._replace(query=urlencode(qs)))
                logger.warning(
                    "Could not fetch Reality params from server for %s — "
                    "verify Marzban is running and /api/core/config is accessible.",
                    safe_name,
                )

        return MarzbanKey(id=data["username"], name=name, access_url=access_url, port=None)

    async def repair_all_users_inbounds(self) -> tuple[int, int]:
        """Fix every Marzban user whose inbounds don't match the server's actual config.

        Returns (repaired_count, error_count).  Intended to be called once on
        startup and periodically thereafter to correct keys created while the
        inbound tag was hardcoded to "VLESS-TCP".
        """
        headers = await self._headers()
        inbounds_map = await self.get_inbounds()
        desired_vless = set(inbounds_map.get("vless") or ["VLESS-TCP"])

        # Fetch all users (up to 10 000 — adjust if needed for large deployments)
        resp = await self._client.get(
            f"{self._base}/api/users",
            headers=headers,
            params={"limit": 10000},
        )
        resp.raise_for_status()
        users: list[dict] = resp.json().get("users", [])

        repaired = 0
        errors = 0
        for user in users:
            username: str = user.get("username", "")
            if not username:
                continue
            current_vless = set(user.get("inbounds", {}).get("vless", []))
            if current_vless == desired_vless:
                continue  # already correct
            try:
                put_resp = await self._client.put(
                    f"{self._base}/api/user/{username}",
                    headers=headers,
                    json={
                        "proxies": {"vless": {"flow": "xtls-rprx-vision"}},
                        "inbounds": {"vless": list(desired_vless)},
                        "status": user.get("status", "active"),
                    },
                )
                if put_resp.is_success:
                    logger.info(
                        "Repaired inbounds for Marzban user '%s': %s → %s",
                        username, sorted(current_vless), sorted(desired_vless),
                    )
                    repaired += 1
                else:
                    logger.warning(
                        "Failed to repair '%s': HTTP %s — %s",
                        username, put_resp.status_code, put_resp.text[:200],
                    )
                    errors += 1
            except Exception as exc:
                logger.warning("Error repairing Marzban user '%s': %s", username, exc)
                errors += 1

        logger.info(
            "Marzban inbound repair complete: %d repaired, %d errors (of %d total users)",
            repaired, errors, len(users),
        )
        return repaired, errors

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
