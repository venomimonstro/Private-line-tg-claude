from __future__ import annotations

import os
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

os.environ.setdefault("BOT_TOKEN", "test")
os.environ.setdefault("ADMIN_CHAT_ID", "1")
os.environ.setdefault("ENCRYPTION_KEY", "A" * 43 + "=")
os.environ.setdefault("SECRET_KEY", "test")

from app.enums import VpnServerStatus
from app.services.node_selection import node_is_ready, rank_nodes, vpn_latency
from app.vpn.marzban import MarzbanClient, MarzbanConfigError


def node(id=1):
    return SimpleNamespace(id=id, status=VpnServerStatus.active, is_selectable=True,
        provider="marzban", api_url="http://185.236.25.152:2096", current_users=0,
        last_e2e_success_at=datetime.utcnow(), last_hot_add_success_at=datetime.utcnow())


class SniTests(unittest.TestCase):
    def test_real_production_hostnames_are_accepted(self):
        for hostname in ("dl.google.com", "www.cloudflare.com", "example.org"):
            self.assertEqual(MarzbanClient._clean_sni(None, hostname), hostname)

    def test_invalid_hostnames_are_rejected(self):
        for hostname in ("https://dl.google.com", "[dl.google.com]", "a..com", "-host.com", "host_.com"):
            with self.assertRaises(MarzbanConfigError):
                MarzbanClient._clean_sni(None, hostname)


class SelectionTests(unittest.IsolatedAsyncioTestCase):
    @patch("app.services.node_selection._server_protocol", return_value="marzban")
    async def test_stale_or_quarantined_node_cannot_be_recommended(self, protocol):
        s = node()
        self.assertTrue(node_is_ready(s))
        s.last_e2e_success_at -= timedelta(minutes=4)
        self.assertFalse(node_is_ready(s))
        s.last_e2e_success_at = datetime.utcnow()
        s.is_selectable = False
        self.assertFalse(node_is_ready(s))

    async def test_probe_uses_vpn_port_not_management_port(self):
        client = SimpleNamespace(_get_core_config=AsyncMock(return_value={"inbounds": [
            {"protocol": "vless", "port": 443, "streamSettings": {"security": "reality"}}
        ]}), aclose=AsyncMock())
        writer = Mock()
        connect = AsyncMock(return_value=(Mock(), writer))
        with patch("app.services.node_selection._make_client", return_value=client), patch(
            "app.services.node_selection.asyncio.open_connection", connect
        ):
            self.assertIsNotNone(await vpn_latency(node()))
        connect.assert_awaited_once_with("185.236.25.152", 443)
        client.aclose.assert_awaited_once()
        writer.close.assert_called_once()

    async def test_unavailable_config_does_not_fall_back_to_api(self):
        client = SimpleNamespace(_get_core_config=AsyncMock(return_value=None), aclose=AsyncMock())
        with patch("app.services.node_selection._make_client", return_value=client), patch(
            "app.services.node_selection.asyncio.open_connection", AsyncMock()
        ) as connect:
            self.assertIsNone(await vpn_latency(node()))
        connect.assert_not_called()
        client.aclose.assert_awaited_once()

    @patch("app.services.node_selection._server_protocol", return_value="marzban")
    async def test_reachable_lower_latency_node_is_ranked_first(self, protocol):
        nodes = [node(1), node(2), node(3)]
        async def latency(s):
            return {1: None, 2: 40, 3: 10}[s.id]
        with patch("app.services.node_selection.vpn_latency", side_effect=latency):
            ranked = await rank_nodes(nodes)
        self.assertEqual([s.id for s, ms in ranked], [3, 2, 1])
