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
        # Caches — populated lazily, shared by all methods that need config data
        self._core_config_cache: dict | None = None
        self._inbounds_cache: dict[str, list[str]] | None = None
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

    # ─── Core config (single source of truth) ─────────────────────────────────

    async def _get_core_config(self) -> dict | None:
        """Fetch and cache the full XRay config from /api/core/config.

        This is the SINGLE API call that both get_reality_params() and
        get_inbounds() draw from.  Caching means we pay the HTTP cost once per
        MarzbanClient instance regardless of how many callers need the data.
        """
        if self._core_config_cache is not None:
            return self._core_config_cache
        try:
            headers = await self._headers()
            resp = await self._client.get(f"{self._base}/api/core/config", headers=headers)
            if resp.status_code == 200:
                self._core_config_cache = resp.json()
                logger.debug("Fetched /api/core/config from %s", self._base)
                return self._core_config_cache
            logger.warning(
                "GET /api/core/config returned HTTP %s from %s",
                resp.status_code, self._base,
            )
        except Exception as exc:
            logger.warning("Could not fetch /api/core/config from %s: %s", self._base, exc)
        return None

    # ─── Inbound tags ──────────────────────────────────────────────────────────

    async def get_inbounds(self) -> dict[str, list[str]]:
        """Return {protocol: [inbound_tag, ...]} from the server's XRay config.

        Strategy:
          1. GET /api/inbounds  (newer Marzban ≥ 0.3.x)
          2. Parse from _get_core_config() inbounds array  (all Marzban versions)
          3. Raise MarzbanConfigError so the caller can fail fast and not store a
             broken key — never silently fall back to a hardcoded tag.
        """
        if self._inbounds_cache is not None:
            return self._inbounds_cache

        headers = await self._headers()

        # Attempt 1: /api/inbounds (newer Marzban)
        # IMPORTANT: this endpoint returns a list of inbound *objects*, NOT strings:
        #   {"vless": [{"tag": "VLESS-TCP", "protocol": "vless", "network": "tcp", ...}]}
        # We must extract just the "tag" field — POST /api/user expects ["VLESS-TCP"].
        try:
            resp = await self._client.get(f"{self._base}/api/inbounds", headers=headers)
            if resp.status_code == 200:
                raw: dict = resp.json()
                data: dict[str, list[str]] = {}
                for proto, items in raw.items():
                    tags: list[str] = []
                    for item in items:
                        if isinstance(item, str):
                            tags.append(item)          # older Marzban: plain string
                        elif isinstance(item, dict):
                            tag = item.get("tag", "")  # newer Marzban: object with "tag"
                            if tag:
                                tags.append(tag)
                    if tags:
                        data[proto] = tags
                if data:
                    self._inbounds_cache = data
                    logger.info("Inbounds from /api/inbounds: %s", data)
                    return self._inbounds_cache
        except Exception as exc:
            logger.debug("GET /api/inbounds failed (%s), trying /api/core/config", exc)

        # Attempt 2: parse from /api/core/config (all versions)
        config = await self._get_core_config()
        if config is not None:
            by_proto: dict[str, list[str]] = {}
            for inbound in config.get("inbounds", []):
                proto: str = inbound.get("protocol", "")
                tag: str = inbound.get("tag", "")
                if proto and tag:
                    by_proto.setdefault(proto, []).append(tag)
            if by_proto:
                self._inbounds_cache = by_proto
                logger.info("Inbounds from /api/core/config: %s", by_proto)
                return self._inbounds_cache

        raise MarzbanConfigError(
            f"Could not determine inbound tags from {self._base}. "
            "Check that /api/inbounds or /api/core/config is accessible."
        )

    # ─── Reality params ────────────────────────────────────────────────────────

    def _clean_sni(self, raw: str) -> str:
        """Strip Markdown link formatting from SNI — e.g. [foo](url) → foo."""
        if not raw:
            return raw

        # Always log repr so we can see exact bytes in production logs
        logger.warning("_clean_sni input repr: %r (hex: %s)", raw, raw.encode("utf-8").hex())

        cleaned = raw.strip()

        # Method 1: standard ASCII Markdown regex [text](url)
        result = _MARKDOWN_LINK_RE.sub(r"\1", cleaned)
        if result != cleaned:
            logger.warning("SNI Markdown (regex) cleaned: %r → %r", cleaned, result.strip())
            return result.strip()

        # Method 2: Unicode full-width bracket normalisation
        normalised = (
            cleaned
            .replace("［", "[").replace("］", "]")
            .replace("（", "(").replace("）", ")")
            .replace("【", "[").replace("】", "]")
        )
        result2 = _MARKDOWN_LINK_RE.sub(r"\1", normalised)
        if result2 != normalised:
            logger.warning("SNI Markdown (unicode-bracket) cleaned: %r → %r", cleaned, result2.strip())
            return result2.strip()

        # Method 3: manual extraction [text] → text
        if cleaned.startswith("[") and "]" in cleaned:
            end = cleaned.index("]")
            candidate = cleaned[1:end].strip()
            if candidate and "." in candidate and " " not in candidate and "(" not in candidate:
                logger.warning("SNI bracket-extract cleaned: %r → %r", cleaned, candidate)
                return candidate

        # Method 4: regex for bare [text]
        m = re.match(r"^\[([^\]]+)\]", cleaned)
        if m:
            candidate = m.group(1).strip()
            if candidate and "." in candidate:
                logger.warning("SNI bare-bracket cleaned: %r → %r", cleaned, candidate)
                return candidate

        # Method 5 (nuclear): extract first domain-like token from anywhere in string
        # Handles invisible chars, unusual encodings, or any remaining Markdown variants
        domains = re.findall(r'\b([a-zA-Z0-9](?:[a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?(?:\.[a-zA-Z0-9](?:[a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?)+)\b', cleaned)
        if domains:
            candidate = domains[0]
            logger.error(
                "SNI nuclear-extract: all methods failed for %r — using first domain found: %r. "
                "PLEASE FIX Marzban XRay serverNames config to store plain hostname.",
                cleaned, candidate
            )
            return candidate

        logger.error("_clean_sni: could not clean %r — returning as-is", cleaned)
        return cleaned

    async def get_reality_params(self) -> tuple[str, str, str] | None:
        """Return (public_key, short_id, sni) from the cached XRay config.

        Calls _get_core_config() (no extra HTTP request if already cached),
        finds the first inbound with security=reality, derives the x25519 public
        key, and returns everything needed to build a valid Reality VLESS link.
        """
        if self._reality_cache is not None:
            return self._reality_cache

        config = await self._get_core_config()
        if config is None:
            return None

        try:
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

                padded = priv_b64 + "=" * (-len(priv_b64) % 4)
                priv_key = X25519PrivateKey.from_private_bytes(base64.urlsafe_b64decode(padded))
                pub_bytes = priv_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
                public_key = base64.urlsafe_b64encode(pub_bytes).rstrip(b"=").decode()

                short_id = next((s for s in short_ids if s), "")
                raw_sni = server_names[0] if server_names else "www.microsoft.com"
                sni = self._clean_sni(raw_sni)

                self._reality_cache = (public_key, short_id, sni)
                return self._reality_cache
        except Exception as exc:
            logger.warning("Could not parse Reality params from config: %s", exc)
        return None

    # ─── Port discovery ────────────────────────────────────────────────────────

    async def _get_vless_port(self) -> int:
        """Return the port the VLESS inbound listens on (from XRay config).

        Falls back to 443 only if config is unreachable — callers should treat
        a fallback as a warning that the URL may not work.
        """
        config = await self._get_core_config()
        if config:
            for inbound in config.get("inbounds", []):
                if inbound.get("protocol") == "vless":
                    port = inbound.get("port")
                    if isinstance(port, int) and port > 0:
                        logger.debug("VLESS port from config: %d", port)
                        return port
        logger.warning(
            "Could not determine VLESS port from %s config — defaulting to 443",
            self._base,
        )
        return 443

    # ─── URL helpers ───────────────────────────────────────────────────────────

    def _inject_reality(self, url: str, public_key: str, short_id: str, sni: str) -> str:
        """Return url with all Reality params set, including required flow."""
        if not url or not url.startswith("vless://"):
            return url
        parsed = urlparse(url)
        qs = {k: v[0] for k, v in parse_qs(parsed.query, keep_blank_values=True).items()}
        qs["security"] = "reality"
        qs["pbk"] = public_key
        qs["sni"] = self._clean_sni(sni)  # always sanitize — defence against Markdown in config
        qs["fp"] = "chrome"
        qs["type"] = "tcp"
        qs["flow"] = "xtls-rprx-vision"
        if short_id:
            qs["sid"] = short_id
        return urlunparse(parsed._replace(query=urlencode(qs)))

    def _fix_bind_address(self, url: str) -> str:
        """Replace 0.0.0.0 or 127.0.0.1 in URL with server's public hostname/IP."""
        for placeholder in ("0.0.0.0", "127.0.0.1"):
            if f"@{placeholder}:" in url:
                m = re.match(r'https?://([^:/]+)', self._base)
                if m:
                    url = url.replace(f"@{placeholder}:", f"@{m.group(1)}:", 1)
                    logger.debug("Replaced bind address %s with %s", placeholder, m.group(1))
        return url

    # ─── Key creation ──────────────────────────────────────────────────────────

    async def create_key(
        self,
        name: str,
        traffic_limit_gb: float | None = None,
        ip_limit: int = 0,
    ) -> MarzbanKey:
        headers = await self._headers()
        safe_name = name[:32]

        # ── 1. Resolve actual inbound tags ──────────────────────────────────
        # get_inbounds() raises MarzbanConfigError if it can't determine the tags.
        # This propagates up so the circuit breaker counts a failure and the
        # operation is queued for retry — we never store a key with a broken URL.
        inbounds_map = await self.get_inbounds()
        vless_inbounds = inbounds_map.get("vless") or []
        if not vless_inbounds:
            raise MarzbanConfigError(
                f"No VLESS inbounds configured on {self._base}. "
                "Check XRay configuration."
            )
        logger.info("Creating key %s with VLESS inbounds: %s", safe_name, vless_inbounds)

        # ── 2. Create / update user in Marzban ──────────────────────────────
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
            # User already exists — update config so inbounds are correct, then fetch
            put_resp = await self._client.put(
                f"{self._base}/api/user/{safe_name}",
                headers=headers,
                json={
                    "proxies": {"vless": {"flow": "xtls-rprx-vision"}},
                    "inbounds": {"vless": vless_inbounds},
                    "status": "active",
                },
            )
            if not put_resp.is_success:
                logger.warning(
                    "PUT /api/user/%s returned %s: %s",
                    safe_name, put_resp.status_code, put_resp.text[:200],
                )
            get_resp = await self._client.get(
                f"{self._base}/api/user/{safe_name}", headers=headers
            )
            get_resp.raise_for_status()
            data = get_resp.json()
        else:
            resp.raise_for_status()
            data = resp.json()

        # ── 3. Extract VLESS link ────────────────────────────────────────────
        links: list[str] = data.get("links", [])
        vless_links = [lnk for lnk in links if lnk.startswith("vless://")]

        # Fallback A: decode Marzban subscription URL (base64-encoded link list)
        if not vless_links:
            sub_path = data.get("subscription_url", "")
            if sub_path:
                full_sub = (
                    sub_path if sub_path.startswith("http") else f"{self._base}{sub_path}"
                )
                try:
                    sub_resp = await self._client.get(full_sub, headers=headers)
                    if sub_resp.status_code == 200:
                        decoded = base64.b64decode(sub_resp.text.strip()).decode("utf-8")
                        vless_links = [
                            ln for ln in decoded.strip().split("\n")
                            if ln.startswith("vless://")
                        ]
                        if vless_links:
                            logger.info(
                                "Got VLESS link for %s via subscription URL", safe_name
                            )
                except Exception as exc:
                    logger.warning(
                        "Subscription URL decode failed for %s: %s", safe_name, exc
                    )

        # Fallback B: build minimal link from UUID + actual port from XRay config
        if not vless_links:
            uuid = data.get("proxies", {}).get("vless", {}).get("id", "")
            if uuid:
                m = re.match(r'https?://([^:/]+)', self._base)
                host = m.group(1) if m else self._base
                port = await self._get_vless_port()  # read from /api/core/config
                access_url = (
                    f"vless://{uuid}@{host}:{port}"
                    f"?type=tcp&security=none#{safe_name}"
                )
                logger.warning(
                    "No links from Marzban for %s — built fallback URL from UUID "
                    "(host=%s port=%d). Check Marzban hosts/nodes configuration.",
                    safe_name, host, port,
                )
                vless_links = [access_url]
            else:
                # Absolutely nothing to work with — fail loud
                raise MarzbanConfigError(
                    f"Marzban returned no VLESS links and no UUID for user '{safe_name}'. "
                    "Check that the inbound tag is correct and Marzban nodes/hosts are "
                    "configured."
                )

        access_url = vless_links[0]
        access_url = self._fix_bind_address(access_url)

        # ── 4. Always inject Reality params (single source of truth) ────────
        reality = await self.get_reality_params()
        if reality:
            public_key, short_id, sni = reality
            access_url = self._inject_reality(access_url, public_key, short_id, sni)
            # Log the sni that was actually put into the URL (already cleaned inside _inject_reality)
            from urllib.parse import parse_qs as _pqs, urlparse as _uparse
            _actual_sni = _pqs(_uparse(access_url).query).get("sni", [sni])[0]
            logger.info(
                "Applied Reality params for %s: pbk=%.8s... sid=%s sni_in_url=%s (raw=%s)",
                safe_name, public_key, short_id, _actual_sni, sni,
            )
        else:
            logger.error(
                "Could not fetch Reality params for %s from %s — "
                "stored URL may be incomplete. Check /api/core/config accessibility.",
                safe_name, self._base,
            )
            # Don't silently continue with a broken URL — raise so retry can happen
            raise MarzbanConfigError(
                f"Could not fetch Reality params from {self._base}. "
                "Ensure Marzban is running and /api/core/config is accessible."
            )

        if not access_url.startswith("vless://"):
            raise MarzbanConfigError(
                f"Final access URL for '{safe_name}' does not start with vless://: "
                f"{access_url[:80]}"
            )

        logger.info(
            "Key ready for %s: %s...%s",
            safe_name,
            access_url[:40],
            access_url[-20:] if len(access_url) > 60 else "",
        )
        return MarzbanKey(id=data["username"], name=name, access_url=access_url, port=None)

    # ─── Repair ────────────────────────────────────────────────────────────────

    async def repair_all_users_inbounds(self) -> tuple[int, int]:
        """Fix every Marzban user whose inbounds don't match the server's actual config.

        Returns (repaired_count, error_count).
        """
        headers = await self._headers()
        inbounds_map = await self.get_inbounds()
        desired_vless = set(inbounds_map.get("vless") or [])

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
                continue
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
                        "Repaired inbounds for '%s': %s → %s",
                        username, sorted(current_vless), sorted(desired_vless),
                    )
                    repaired += 1
                else:
                    logger.warning(
                        "Failed to repair '%s': HTTP %s",
                        username, put_resp.status_code,
                    )
                    errors += 1
            except Exception as exc:
                logger.warning("Error repairing Marzban user '%s': %s", username, exc)
                errors += 1

        logger.info(
            "Marzban inbound repair: %d repaired, %d errors (%d total users)",
            repaired, errors, len(users),
        )
        return repaired, errors

    # ─── Other API methods ─────────────────────────────────────────────────────

    async def delete_key(self, username: str) -> None:
        headers = await self._headers()
        resp = await self._client.delete(
            f"{self._base}/api/user/{username}", headers=headers
        )
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


class MarzbanConfigError(RuntimeError):
    """Raised when Marzban configuration is missing or unreachable.

    This is a retryable error — the circuit breaker counts it as a failure
    and the pending-operation queue will retry the key creation.
    """
