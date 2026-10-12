from __future__ import annotations

import asyncio
import base64
import ipaddress
import logging
import re
from dataclasses import dataclass
from uuid import UUID
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
        # HTTP is used for the firewall-restricted node API in the default
        # topology. If an operator supplies HTTPS, certificate verification must
        # remain enabled; never silently downgrade TLS authenticity.
        self._client = httpx.AsyncClient(timeout=15.0, trust_env=False)
        # Caches — populated lazily, shared by all methods that need config data
        self._core_config_cache: dict | None = None
        self._inbounds_cache: dict[str, list[str]] | None = None
        self._reality_cache: tuple[str, str, str] | None = None  # (pubkey, short_id, sni)

    async def _reset_http_client(self) -> None:
        old = self._client
        self._client = httpx.AsyncClient(timeout=15.0, trust_env=False)
        self._token = None
        try:
            await old.aclose()
        except Exception:
            pass

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

    async def restart_core_with_db_users(self, *, reason: str = "") -> None:
        """Force Marzban to rebuild the live Xray runtime from its database.

        Marzban's POST/PUT user endpoints update the DB first and enqueue Xray
        changes as background work. A successful REST response therefore does
        not prove the live Xray process accepted the user. POST /api/core/restart
        uses include_db_users() upstream, making the DB and runtime converge.
        """
        response = None
        last_transport_error: Exception | None = None
        for attempt in range(2):
            try:
                headers = await self._headers()
                response = await self._client.post(
                    f"{self._base}/api/core/restart",
                    headers=headers,
                    timeout=60.0,
                )
                break
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                last_transport_error = exc
                if attempt == 0:
                    await self._reset_http_client()
                    continue
                raise MarzbanConfigError(
                    "Marzban Xray runtime resync transport failed"
                ) from exc

        if response is None:
            raise MarzbanConfigError(
                "Marzban Xray runtime resync produced no response"
            ) from last_transport_error
        if response.status_code not in (200, 204):
            raise MarzbanConfigError(
                f"Marzban Xray runtime resync failed HTTP {response.status_code}"
            )

        for _ in range(30):
            try:
                headers = await self._headers()
                status = await self._client.get(
                    f"{self._base}/api/core",
                    headers=headers,
                    timeout=4.0,
                )
                if status.status_code == 200 and status.json().get("started") is True:
                    logger.info(
                        "Marzban Xray runtime resynced%s",
                        f" ({reason})" if reason else "",
                    )
                    return
            except (httpx.TransportError, httpx.TimeoutException):
                pass
            await asyncio.sleep(1)

        raise MarzbanConfigError(
            "Marzban Xray core did not return to started state after runtime resync"
        )

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
        """Return one strict DNS hostname; never guess or rewrite REALITY SNI."""
        cleaned = (raw or "").strip().rstrip(".").lower()
        if (
            not cleaned
            or len(cleaned) > 253
            or not re.fullmatch(
                r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\\.)+"
                r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?",
                cleaned,
            )
        ):
            raise MarzbanConfigError("Reality serverName/SNI must be a plain DNS hostname")
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
            reality_inbounds = [
                inbound for inbound in config.get("inbounds", [])
                if inbound.get("protocol") == "vless"
                and inbound.get("streamSettings", {}).get("security") == "reality"
            ]
            if len(reality_inbounds) != 1:
                raise MarzbanConfigError(
                    f"Expected exactly one VLESS Reality inbound, found {len(reality_inbounds)}"
                )

            for inbound in reality_inbounds:
                stream = inbound.get("streamSettings", {})
                reality = stream.get("realitySettings", {})
                priv_b64 = reality.get("privateKey", "")
                short_ids = reality.get("shortIds", [])
                server_names = reality.get("serverNames", [])
                if (
                    not priv_b64
                    or not isinstance(short_ids, list)
                    or not any(isinstance(item, str) and item for item in short_ids)
                    or not isinstance(server_names, list)
                    or len(server_names) != 1
                    or not isinstance(server_names[0], str)
                    or not server_names[0].strip()
                ):
                    continue

                from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
                from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

                padded = priv_b64 + "=" * (-len(priv_b64) % 4)
                priv_key = X25519PrivateKey.from_private_bytes(base64.urlsafe_b64decode(padded))
                pub_bytes = priv_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
                public_key = base64.urlsafe_b64encode(pub_bytes).rstrip(b"=").decode()

                short_id = next((s for s in short_ids if isinstance(s, str) and s), "")
                raw_sni = server_names[0]
                sni = self._clean_sni(raw_sni)
                if not re.fullmatch(r"[A-Za-z0-9.-]{1,253}", sni):
                    raise MarzbanConfigError("Reality serverName/SNI is invalid")
                if not re.fullmatch(r"(?:[a-fA-F0-9]{2}){1,8}", short_id):
                    raise MarzbanConfigError("Reality shortId is invalid")

                configured_public = reality.get("publicKey")
                if configured_public and configured_public != public_key:
                    raise MarzbanConfigError(
                        "Reality publicKey metadata does not match privateKey"
                    )

                self._reality_cache = (public_key, short_id, sni)
                return self._reality_cache
        except MarzbanConfigError:
            raise
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
        qs["encryption"] = "none"
        qs["pbk"] = public_key
        qs["sni"] = self._clean_sni(sni)  # always sanitize — defence against Markdown in config
        qs["fp"] = "chrome"
        qs["type"] = "tcp"
        qs["flow"] = "xtls-rprx-vision"
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
        uuid: str | None = None,
        *,
        restart_core: bool = True,
    ) -> MarzbanKey:
        headers = await self._headers()
        safe_name = name[:32]

        # ── 1. Resolve the single production VLESS+REALITY inbound ─────────
        # Do NOT assign a paid user to every VLESS inbound returned by Marzban.
        # A second VLESS transport/fallback would make the panel generate an
        # arbitrary share link and can pair a UUID with the wrong transport.
        # The static Xray config is authoritative for the production endpoint.
        config = await self._get_core_config()
        reality_inbounds = [
            item for item in (config or {}).get("inbounds", [])
            if item.get("protocol") == "vless"
            and item.get("streamSettings", {}).get("security") == "reality"
        ]
        if len(reality_inbounds) != 1:
            raise MarzbanConfigError(
                f"Expected exactly one VLESS Reality inbound on {self._base}; "
                f"found {len(reality_inbounds)}"
            )
        reality_inbound = reality_inbounds[0]
        reality_tag = reality_inbound.get("tag")
        reality_port = reality_inbound.get("port")
        if (
            not isinstance(reality_tag, str)
            or not reality_tag
            or not isinstance(reality_port, int)
            or not (1 <= reality_port <= 65535)
        ):
            raise MarzbanConfigError("VLESS Reality inbound has invalid tag/port")
        vless_inbounds = [reality_tag]
        logger.info(
            "Creating key %s on verified Reality inbound %s:%d",
            safe_name, reality_tag, reality_port,
        )

        # ── 2. Create / update user in Marzban ──────────────────────────────
        vless_proxy: dict = {"flow": "xtls-rprx-vision"}
        if uuid:
            vless_proxy["id"] = uuid  # preserve UUID so existing client keys keep working
        body: dict = {
            "username": safe_name,
            "proxies": {"vless": vless_proxy},
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
            # Explicitly retain the server UUID. Omitting id can make Marzban's
            # proxy model generate a new UUID while processing an update.
            user_url = f"{self._base}/api/user/{safe_name}"
            existing = await self._client.get(user_url, headers=headers)
            existing.raise_for_status()
            current = existing.json()
            current_id = current.get("proxies", {}).get("vless", {}).get("id")
            expected_id = uuid or current_id
            try:
                expected_id = str(UUID(expected_id))
            except (ValueError, TypeError, AttributeError):
                raise MarzbanConfigError("Existing Marzban user has no valid VLESS UUID")
            update = {
                "proxies": {**current.get("proxies", {}), "vless": {
                    "id": expected_id, "flow": "xtls-rprx-vision",
                }},
                "inbounds": {**current.get("inbounds", {}), "vless": vless_inbounds},
                "status": "active",
            }
            put_resp = await self._client.put(user_url, headers=headers, json=update)
            put_resp.raise_for_status()
            get_resp = await self._client.get(user_url, headers=headers)
            get_resp.raise_for_status()
            data = get_resp.json()
            if data.get("proxies", {}).get("vless", {}).get("id") != expected_id:
                raise MarzbanConfigError("Marzban did not preserve the requested VLESS UUID")
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

        expected_uuid = data.get("proxies", {}).get("vless", {}).get("id")
        try:
            expected_uuid = str(UUID(expected_uuid))
        except (ValueError, TypeError, AttributeError):
            raise MarzbanConfigError("Marzban returned no valid VLESS UUID")
        matching_links = [
            link for link in vless_links
            if urlparse(link).username == expected_uuid
        ]
        if matching_links:
            # Panel-generated links are advisory only. Marzban 0.8.x has had
            # REALITY share-link regressions (notably shortId/public-key
            # rendering), so this value is never trusted as the final client
            # credential. It is retained here only for diagnostic logging.
            access_url = self._fix_bind_address(matching_links[0])
        else:
            # A wrong/missing Marzban share URI must not invalidate an otherwise
            # healthy user. Build a temporary URI from authoritative UUID +
            # inbound endpoint; it will be replaced below from /api/hosts.
            m = re.match(r'https?://([^:/]+)', self._base)
            host = m.group(1) if m else self._base
            access_url = (
                f"vless://{expected_uuid}@{host}:{reality_port}"
                f"?type=tcp&security=none#{safe_name}"
            )
            logger.warning(
                "Marzban emitted no matching VLESS share URI for %s; "
                "using authoritative UUID/config instead",
                safe_name,
            )

        # ── 4. Always inject Reality params (single source of truth) ────────
        reality = await self.get_reality_params()
        if reality:
            public_key, short_id, sni = reality
            access_url = self._inject_reality(access_url, public_key, short_id, sni)
            # Log the sni that was actually put into the URL (already cleaned inside _inject_reality)
            from urllib.parse import parse_qs as _pqs, urlparse as _uparse
            _actual_sni = _pqs(_uparse(access_url).query).get("sni", [sni])[0]
            logger.info(
                "Applied verified Reality params for %s (sni=%s)",
                safe_name, _actual_sni,
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
                f"Final access URL for '{safe_name}' is not a VLESS share URI"
            )

        # Fail closed if Marzban generated a client endpoint that differs from
        # its published public host. A syntactically valid VLESS URI pointing at
        # localhost/API/private IP must never be stored as an active customer key.
        config = await self._get_core_config()
        reality_inbounds = [
            item for item in (config or {}).get("inbounds", [])
            if item.get("protocol") == "vless"
            and item.get("streamSettings", {}).get("security") == "reality"
        ]
        if len(reality_inbounds) != 1:
            raise MarzbanConfigError("Expected exactly one VLESS Reality inbound")
        inbound_tag = reality_inbounds[0].get("tag")
        inbound_port = reality_inbounds[0].get("port")
        if not inbound_tag or not isinstance(inbound_port, int):
            raise MarzbanConfigError("Invalid VLESS Reality inbound metadata")

        hosts_resp = await self._client.get(
            f"{self._base}/api/hosts", headers=headers
        )
        if hosts_resp.status_code != 200 or not isinstance(hosts_resp.json(), dict):
            raise MarzbanConfigError("Marzban public hosts are unavailable")
        host_entries = hosts_resp.json().get(inbound_tag, [])
        public_hosts = []
        for item in host_entries if isinstance(host_entries, list) else []:
            if not isinstance(item, dict) or item.get("is_disabled") in (True, "true", "True"):
                continue
            address = item.get("address")
            try:
                if isinstance(address, str) and ipaddress.ip_address(address).is_global:
                    public_hosts.append(item)
            except ValueError:
                continue
        if len(public_hosts) != 1:
            raise MarzbanConfigError("Expected exactly one active globally routable Marzban host")

        expected_host = public_hosts[0].get("address")
        configured_port = public_hosts[0].get("port")
        expected_port = (
            inbound_port
            if configured_port in (None, "", 0, "0")
            else int(configured_port)
        )

        # IMPORTANT: construct the customer credential ourselves from the
        # verified node facts. This deliberately bypasses Marzban's share-link
        # renderer, which is not the source of truth for REALITY parameters.
        authoritative_base = (
            f"vless://{expected_uuid}@{expected_host}:{expected_port}"
            f"?type=tcp&security=none#{safe_name}"
        )
        access_url = self._inject_reality(
            authoritative_base, public_key, short_id, sni
        )
        final = urlparse(access_url)
        if final.hostname != expected_host or final.port != expected_port:
            raise MarzbanConfigError(
                "Authoritative VLESS endpoint differs from published Marzban host"
            )

        # Marzban 0.8.4 schedules HandlerService hot-add as a background task.
        # Production-selectable nodes are required to pass a fresh-user E2E
        # canary without restart. Keep restart_core=True only for explicit
        # recovery/legacy call sites; normal paid provisioning uses the proven
        # live-add path and therefore does not interrupt existing sessions.
        if restart_core:
            await self.restart_core_with_db_users(reason=f"provision {safe_name}")

        # Re-read authoritative Marzban DB state. Live-runtime membership is
        # proven separately by the hot-add E2E admission gate.
        verify_user = await self._client.get(
            f"{self._base}/api/user/{safe_name}", headers=await self._headers()
        )
        if verify_user.status_code != 200:
            raise MarzbanConfigError(
                f"Marzban user disappeared after core restart (HTTP {verify_user.status_code})"
            )
        verified = verify_user.json()
        verified_proxy = verified.get("proxies", {}).get("vless", {})
        verified_inbounds = verified.get("inbounds", {}).get("vless", [])
        if (
            verified_proxy.get("id") != expected_uuid
            or verified_proxy.get("flow") != "xtls-rprx-vision"
            or reality_tag not in verified_inbounds
            or verified.get("status") != "active"
        ):
            raise MarzbanConfigError(
                "Marzban user is not active on the verified Reality/Vision inbound"
            )

        logger.info("Key ready for %s (credentials omitted)", safe_name)
        return MarzbanKey(
            id=data["username"],
            name=name,
            access_url=access_url,
            port=None,
        )

    async def get_key(self, name: str) -> MarzbanKey:
        """Read one existing active VLESS/REALITY credential without mutation.

        Used by synthetic health probes and other read-only checks. It never
        restarts Xray, edits the Marzban user, or rotates any credential.
        """
        safe_name = name[:32]
        headers = await self._headers()
        response = await self._client.get(
            f"{self._base}/api/user/{safe_name}", headers=headers
        )
        response.raise_for_status()
        data = response.json()

        config = await self._get_core_config()
        reality_inbounds = [
            item for item in (config or {}).get("inbounds", [])
            if item.get("protocol") == "vless"
            and item.get("streamSettings", {}).get("security") == "reality"
        ]
        if len(reality_inbounds) != 1:
            raise MarzbanConfigError("Expected exactly one VLESS Reality inbound")
        inbound = reality_inbounds[0]
        tag = inbound.get("tag")
        port = inbound.get("port")
        if not isinstance(tag, str) or not tag or not isinstance(port, int):
            raise MarzbanConfigError("Invalid VLESS Reality inbound")

        proxy = data.get("proxies", {}).get("vless", {})
        try:
            user_uuid = str(UUID(proxy.get("id") or ""))
        except (ValueError, TypeError, AttributeError) as exc:
            raise MarzbanConfigError("Existing Marzban user has invalid UUID") from exc
        if (
            data.get("status") != "active"
            or proxy.get("flow") != "xtls-rprx-vision"
            or tag not in data.get("inbounds", {}).get("vless", [])
        ):
            raise MarzbanConfigError(
                "Existing Marzban user is not active on the production Reality/Vision inbound"
            )

        reality = await self.get_reality_params()
        if reality is None:
            raise MarzbanConfigError("Reality configuration unavailable")
        public_key, short_id, sni = reality

        hosts = await self._client.get(f"{self._base}/api/hosts", headers=headers)
        if hosts.status_code != 200 or not isinstance(hosts.json(), dict):
            raise MarzbanConfigError("Marzban public hosts unavailable")
        entries = hosts.json().get(tag, [])
        public = []
        for item in entries if isinstance(entries, list) else []:
            if not isinstance(item, dict) or item.get("is_disabled") in (True, "true", "True"):
                continue
            address = item.get("address")
            try:
                if isinstance(address, str) and ipaddress.ip_address(address).is_global:
                    public.append(item)
            except ValueError:
                continue
        if len(public) != 1:
            raise MarzbanConfigError("Expected exactly one public host for Reality probe")

        address = public[0]["address"]
        configured_port = public[0].get("port")
        public_port = port if configured_port in (None, "", 0, "0") else int(configured_port)
        base = (
            f"vless://{user_uuid}@{address}:{public_port}"
            f"?type=tcp&security=none#{safe_name}"
        )
        access_url = self._inject_reality(base, public_key, short_id, sni)
        return MarzbanKey(id=safe_name, name=safe_name, access_url=access_url, port=None)

    # ─── Repair ────────────────────────────────────────────────────────────────

    async def repair_all_users_inbounds(self) -> tuple[int, int]:
        """Fix every Marzban user whose inbounds don't match the server's actual config.

        Returns (repaired_count, error_count).
        """
        headers = await self._headers()
        inbounds_map = await self.get_inbounds()
        desired_vless = set(inbounds_map.get("vless") or [])

        if not desired_vless:
            raise MarzbanConfigError("No VLESS inbounds available for repair")
        users = list((await self.get_users()).values())

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
                proxy = user.get("proxies", {}).get("vless", {})
                if not proxy.get("id"):
                    raise MarzbanConfigError("Cannot repair inbounds without preserving UUID")
                put_resp = await self._client.put(
                    f"{self._base}/api/user/{username}",
                    headers=headers,
                    json={
                        "proxies": {**user.get("proxies", {}), "vless": {
                            **proxy, "flow": "xtls-rprx-vision",
                        }},
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

    async def disable_key(self, username: str) -> None:
        """Disable a Marzban user without invoking the buggy DELETE/report path."""
        headers = await self._headers()
        url = f"{self._base}/api/user/{username}"
        current = await self._client.get(url, headers=headers)
        if current.status_code == 404:
            return
        current.raise_for_status()
        data = current.json()
        payload = {
            "proxies": data.get("proxies", {}),
            "inbounds": data.get("inbounds", {}),
            "status": "disabled",
        }
        response = await self._client.put(url, headers=headers, json=payload)
        response.raise_for_status()
        verify = await self._client.get(url, headers=headers)
        verify.raise_for_status()
        if verify.json().get("status") != "disabled":
            raise MarzbanConfigError("Marzban user disable did not persist")
        # Revocation must reach the live Xray process, not only the Marzban DB.
        await self.restart_core_with_db_users(reason=f"disable {username}")

    async def user_absent(self, username: str, *, attempts: int = 8) -> bool:
        """Confirm deletion with a fresh HTTP session.

        Marzban 0.8.x may close a DELETE response after committing the database
        change. A fresh authenticated session avoids reusing a stale keep-alive
        connection and lets callers distinguish "transport broke" from
        "user still exists".
        """
        delay = 0.35
        for attempt in range(max(1, attempts)):
            try:
                async with httpx.AsyncClient(timeout=8.0, trust_env=False) as probe:
                    auth = await probe.post(
                        f"{self._base}/api/admin/token",
                        data={"username": self._username, "password": self._password},
                    )
                    if auth.status_code != 200:
                        raise MarzbanConfigError(
                            f"Marzban authentication failed while verifying deletion "
                            f"(HTTP {auth.status_code})"
                        )
                    token = auth.json().get("access_token")
                    if not token:
                        raise MarzbanConfigError(
                            "Marzban returned no access token while verifying deletion"
                        )
                    verify = await probe.get(
                        f"{self._base}/api/user/{username}",
                        headers={"Authorization": f"Bearer {token}"},
                    )
                    if verify.status_code == 404:
                        return True
                    if verify.status_code == 200:
                        return False
                    if verify.status_code not in (502, 503, 504):
                        verify.raise_for_status()
            except (httpx.TransportError, httpx.TimeoutException):
                pass
            if attempt + 1 < max(1, attempts):
                await asyncio.sleep(delay)
                delay = min(delay * 1.7, 2.0)
        return False

    async def delete_key(self, username: str, *, restart_core: bool = True) -> None:
        headers = await self._headers()
        resp = None
        transport_error: Exception | None = None
        try:
            resp = await self._client.delete(
                f"{self._base}/api/user/{username}", headers=headers
            )
        except (httpx.TransportError, httpx.TimeoutException) as exc:
            # Older Marzban can commit the delete and then tear down the HTTP
            # response. Never retry DELETE blindly before checking authoritative
            # state, otherwise cleanup becomes ambiguous.
            transport_error = exc

        deleted = bool(resp is not None and resp.status_code == 404)

        if resp is not None and resp.is_success:
            deleted = True
        elif resp is None or (resp is not None and resp.status_code >= 500):
            # Known Marzban 0.8.x behaviour: deletion may be committed even when
            # the endpoint returns 500 or the connection closes. Confirm through
            # a fresh authenticated session before accepting success.
            if await self.user_absent(username):
                if resp is None:
                    logger.warning(
                        "Marzban DELETE /api/user/%s lost its HTTP response, "
                        "but fresh GET confirmed deletion",
                        username,
                    )
                else:
                    logger.warning(
                        "Marzban DELETE /api/user/%s returned HTTP %s, "
                        "but fresh GET confirmed deletion",
                        username, resp.status_code,
                    )
                deleted = True

        if not deleted:
            if resp is not None:
                resp.raise_for_status()
            raise MarzbanConfigError(
                f"Marzban user deletion could not be confirmed for {username}"
            ) from transport_error

        # The DELETE endpoint also schedules removal from Xray in background.
        # Explicit revocation keeps the conservative restart fallback. Ephemeral
        # hot-add canaries pass restart_core=False so the admission test itself
        # never creates downtime.
        if restart_core:
            await self.restart_core_with_db_users(reason=f"delete {username}")

    async def get_users(self) -> dict[str, dict]:
        """Read every page; never mistake a truncated list for deleted users."""
        headers = await self._headers()
        users: dict[str, dict] = {}
        offset = 0
        while True:
            resp = await self._client.get(
                f"{self._base}/api/users", headers=headers,
                params={"limit": 1000, "offset": offset},
            )
            resp.raise_for_status()
            page = resp.json().get("users")
            if not isinstance(page, list):
                raise MarzbanConfigError("Invalid Marzban users response")
            if not page:
                return users
            before = len(users)
            users.update({u["username"]: u for u in page})
            if len(users) == before:
                raise MarzbanConfigError("Marzban pagination did not advance")
            offset += len(page)

    async def get_transfer_stats(self) -> dict[str, int]:
        return {name: user.get("used_traffic", 0)
                for name, user in (await self.get_users()).items()}

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

