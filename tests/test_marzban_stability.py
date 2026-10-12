import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from cryptography.fernet import Fernet

os.environ.setdefault("BOT_TOKEN", "test")
os.environ.setdefault("ADMIN_CHAT_ID", "1")
os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("SECRET_KEY", "test")

from app.vpn.marzban import MarzbanClient, MarzbanConfigError
from app.services.vpn_key import repair_marzban_keys

UUID = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"
LINK = (
    f"vless://{UUID}@185.236.25.152:443"
    "?security=reality&pbk=" + "A" * 43
    + "&sid=12&sni=example.org&flow=xtls-rprx-vision&type=tcp&fp=chrome"
)


class ConstructorRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_client_always_initializes_http_transport(self):
        client = MarzbanClient("http://127.0.0.1:2096", "admin", "test")
        try:
            self.assertTrue(hasattr(client, "_client"))
            self.assertIsInstance(client._client, httpx.AsyncClient)
        finally:
            await client.aclose()


class ClientTests(unittest.IsolatedAsyncioTestCase):
    async def make_client(self, handler):
        def routed(req):
            if req.url.path == "/api/hosts":
                return httpx.Response(200, json={
                    "VLESS-TCP": [{
                        "remark": "Private Line VPN",
                        "address": "185.236.25.152",
                        "port": 443,
                        "security": "inbound_default",
                        "is_disabled": False,
                    }]
                })
            if req.url.path == "/api/core/restart" and req.method == "POST":
                return httpx.Response(200, json={"detail": "Core restarted"})
            if req.url.path == "/api/core" and req.method == "GET":
                return httpx.Response(200, json={"started": True, "version": "26.9.9"})
            return handler(req)

        with patch.dict(os.environ, {k: "" for k in (
            "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
            "http_proxy", "https_proxy", "all_proxy",
        )}):
            client = MarzbanClient("http://185.236.25.152:2096", "admin", "test")
        await client._client.aclose()
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(routed))
        client._token = "test"
        client._inbounds_cache = {"vless": ["VLESS-TCP"]}
        client._reality_cache = ("A" * 43, "12", "example.org")
        client._core_config_cache = {
            "inbounds": [{
                "tag": "VLESS-TCP",
                "protocol": "vless",
                "port": 443,
                "streamSettings": {
                    "security": "reality",
                    "realitySettings": {
                        "publicKey": "A" * 43,
                        "shortIds": ["12"],
                        "serverNames": ["example.org"],
                    },
                },
            }]
        }
        self.addAsyncCleanup(client.aclose)
        return client

    async def conflict(self, explicit=None, put_status=200, stale_link=False):
        state = {"id": OTHER if explicit else UUID, "writes": []}

        def handler(req):
            if req.method == "POST":
                return httpx.Response(409)
            if req.method == "PUT":
                body = json.loads(req.content)
                state["writes"].append(body)
                state["id"] = body["proxies"]["vless"]["id"]
                return httpx.Response(put_status, json={})
            uid = state["id"]
            link_uuid = OTHER if stale_link else uid
            return httpx.Response(200, json={
                "username": "u1s1",
                "status": "active",
                "inbounds": {"vless": ["VLESS-TCP"]},
                "proxies": {"vless": {"id": uid, "flow": "xtls-rprx-vision"}},
                "links": [LINK.replace(UUID, link_uuid)],
            })

        return await self.make_client(handler), state

    async def test_conflict_preserves_existing_uuid(self):
        client, state = await self.conflict()
        key = await client.create_key("u1s1")
        self.assertIn(UUID, key.access_url)
        self.assertEqual(state["writes"][0]["proxies"]["vless"]["id"], UUID)

    async def test_conflict_restores_issued_uuid(self):
        client, state = await self.conflict(explicit=UUID)
        await client.create_key("u1s1", uuid=UUID)
        self.assertEqual(state["id"], UUID)

    async def test_failed_update_never_returns_key(self):
        client, _ = await self.conflict(put_status=500)
        with self.assertRaises(httpx.HTTPStatusError):
            await client.create_key("u1s1")

    async def test_stale_panel_link_is_ignored_in_favour_of_authoritative_state(self):
        client, _ = await self.conflict(stale_link=True)
        key = await client.create_key("u1s1")
        self.assertIn(UUID, key.access_url)
        self.assertIn("@185.236.25.152:443", key.access_url)
        self.assertIn("security=reality", key.access_url)
        self.assertIn("flow=xtls-rprx-vision", key.access_url)
        self.assertIn("sid=12", key.access_url)

    async def test_panel_link_transport_fields_cannot_override_reality_profile(self):
        client, _ = await self.conflict()
        original = client._client

        # The mock Marzban response may contain syntactically valid but stale
        # share-link transport fields. create_key must still publish the
        # authoritative node profile.
        key = await client.create_key("u1s1")
        self.assertIn("sni=example.org", key.access_url)
        self.assertIn("pbk=" + "A" * 43, key.access_url)
        self.assertIn("type=tcp", key.access_url)

    async def test_create_key_fails_closed_when_runtime_resync_fails(self):
        def handler(req):
            if req.url.path == "/api/core/restart":
                return httpx.Response(503)
            if req.method == "POST":
                return httpx.Response(200, json={
                    "username": "u1s1",
                    "status": "active",
                    "inbounds": {"vless": ["VLESS-TCP"]},
                    "proxies": {"vless": {"id": UUID, "flow": "xtls-rprx-vision"}},
                    "links": [LINK],
                })
            return httpx.Response(200, json={})

        client = await self.make_client(handler)
        # Override the default successful core route so the failure is explicit.
        async def failing_resync(*, reason=""):
            raise MarzbanConfigError("runtime resync failed")
        client.restart_core_with_db_users = failing_resync
        with self.assertRaises(MarzbanConfigError):
            await client.create_key("u1s1")

    async def test_delete_accepts_transport_loss_when_fresh_get_confirms_absent(self):
        client = await self.make_client(lambda req: httpx.Response(200, json={}))
        client._client.delete = AsyncMock(
            side_effect=httpx.RemoteProtocolError("server disconnected")
        )
        client.user_absent = AsyncMock(return_value=True)
        client.restart_core_with_db_users = AsyncMock()

        await client.delete_key("pltest_0123456789abcdef")

        client.user_absent.assert_awaited_once_with("pltest_0123456789abcdef")
        client.restart_core_with_db_users.assert_awaited_once()

    async def test_delete_fails_closed_when_transport_loss_cannot_be_verified(self):
        client = await self.make_client(lambda req: httpx.Response(200, json={}))
        client._client.delete = AsyncMock(
            side_effect=httpx.RemoteProtocolError("server disconnected")
        )
        client.user_absent = AsyncMock(return_value=False)
        client.restart_core_with_db_users = AsyncMock()

        with self.assertRaises(MarzbanConfigError):
            await client.delete_key("pltest_0123456789abcdef")
        client.restart_core_with_db_users.assert_not_awaited()

    async def test_pagination_includes_every_user(self):
        offsets = []

        def handler(req):
            offset = int(req.url.params["offset"])
            offsets.append(offset)
            rows = [
                {"username": f"u{i}", "used_traffic": i}
                for i in range(offset, min(offset + 1000, 1001))
            ]
            return httpx.Response(200, json={"users": rows})

        client = await self.make_client(handler)
        users = await client.get_transfer_stats()
        self.assertEqual(len(users), 1001)
        self.assertEqual(offsets, [0, 1000, 1001])

    async def test_broken_pagination_fails_closed(self):
        client = await self.make_client(
            lambda req: httpx.Response(
                200, json={"users": [{"username": "repeated"}]}
            )
        )
        with self.assertRaises(MarzbanConfigError):
            await client.get_users()


class RepairCompatibilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_legacy_wrapper_uses_preserve_first_reconciler(self):
        server = SimpleNamespace(id=1, provider="some-host")
        result_rows = SimpleNamespace(
            scalars=lambda: SimpleNamespace(all=lambda: [server])
        )
        session = AsyncMock()
        session.execute.return_value = result_rows
        reconciled = SimpleNamespace(
            repaired_hosts=1,
            repaired_users=2,
            repaired_urls=3,
            errors=0,
        )
        with patch(
            "app.services.vpn_key._server_protocol", return_value="marzban"
        ), patch(
            "app.services.vpn_reconcile.reconcile_marzban_server",
            new=AsyncMock(return_value=reconciled),
        ) as reconcile:
            fixed, errors, notifications = await repair_marzban_keys(session)

        reconcile.assert_awaited_once_with(session, 1, apply=True)
        self.assertEqual((fixed, errors, notifications), (6, 0, []))
        session.commit.assert_awaited_once()

    async def test_legacy_wrapper_skips_non_marzban_nodes(self):
        server = SimpleNamespace(id=2, provider="legacy-host")
        session = AsyncMock()
        session.execute.return_value = SimpleNamespace(
            scalars=lambda: SimpleNamespace(all=lambda: [server])
        )
        with patch(
            "app.services.vpn_key._server_protocol", return_value="outline"
        ), patch(
            "app.services.vpn_reconcile.reconcile_marzban_server",
            new=AsyncMock(),
        ) as reconcile:
            fixed, errors, notifications = await repair_marzban_keys(session)
        reconcile.assert_not_awaited()
        self.assertEqual((fixed, errors, notifications), (0, 0, []))


if __name__ == "__main__":
    unittest.main()
