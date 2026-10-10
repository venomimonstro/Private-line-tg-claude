import json
import os
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from cryptography.fernet import Fernet

os.environ.setdefault('BOT_TOKEN', 'test')
os.environ.setdefault('ADMIN_CHAT_ID', '1')
os.environ.setdefault('ENCRYPTION_KEY', Fernet.generate_key().decode())
os.environ.setdefault('SECRET_KEY', 'test')

from app.vpn.marzban import MarzbanClient, MarzbanConfigError, MarzbanKey
from app.services.vpn_key import repair_marzban_keys
from app.enums import SubscriptionStatus, VpnKeyStatus

UUID = '11111111-1111-4111-8111-111111111111'
OTHER = '22222222-2222-4222-8222-222222222222'
LINK = f'vless://{UUID}@185.236.25.152:443?security=reality&pbk=pub&sid=12&sni=example.org&flow=xtls-rprx-vision'


class ClientTests(unittest.IsolatedAsyncioTestCase):
    async def make_client(self, handler):
        with patch.dict(os.environ, {k: '' for k in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy')}):
            client = MarzbanClient('http://node:2096', 'admin', 'test')
        await client._client.aclose()
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        client._token = 'test'
        client._inbounds_cache = {'vless': ['VLESS-TCP']}
        client._reality_cache = ('pub', '12', 'example.org')
        self.addAsyncCleanup(client.aclose)
        return client

    async def conflict(self, explicit=None, put_status=200, stale_link=False):
        state = {'id': OTHER if explicit else UUID, 'writes': []}
        def handler(req):
            if req.method == 'POST':
                return httpx.Response(409)
            if req.method == 'PUT':
                body = json.loads(req.content)
                state['writes'].append(body)
                state['id'] = body['proxies']['vless']['id']
                return httpx.Response(put_status, json={})
            uid = state['id']
            return httpx.Response(200, json={
                'username': 'u1s1', 'proxies': {'vless': {'id': uid}},
                'links': [LINK.replace(UUID, OTHER if stale_link else uid)],
            })
        client = await self.make_client(handler)
        return client, state

    async def test_conflict_preserves_existing_uuid(self):
        client, state = await self.conflict()
        key = await client.create_key('u1s1')
        self.assertIn(UUID, key.access_url)
        self.assertEqual(state['writes'][0]['proxies']['vless']['id'], UUID)

    async def test_conflict_restores_issued_uuid(self):
        client, state = await self.conflict(explicit=UUID)
        await client.create_key('u1s1', uuid=UUID)
        self.assertEqual(state['id'], UUID)

    async def test_failed_update_never_returns_key(self):
        client, _ = await self.conflict(put_status=500)
        with self.assertRaises(httpx.HTTPStatusError):
            await client.create_key('u1s1')

    async def test_wrong_uuid_link_rejected(self):
        client, _ = await self.conflict(stale_link=True)
        with self.assertRaises(MarzbanConfigError):
            await client.create_key('u1s1')

    async def test_pagination_includes_every_user(self):
        offsets = []
        def handler(req):
            offset = int(req.url.params['offset'])
            offsets.append(offset)
            rows = [{'username': f'u{i}', 'used_traffic': i}
                    for i in range(offset, min(offset + 1000, 1001))]
            return httpx.Response(200, json={'users': rows})
        client = await self.make_client(handler)
        users = await client.get_transfer_stats()
        self.assertEqual(len(users), 1001)
        self.assertEqual(offsets, [0, 1000, 1001])

    async def test_broken_pagination_fails_closed(self):
        client = await self.make_client(lambda req: httpx.Response(
            200, json={'users': [{'username': 'repeated'}]}))
        with self.assertRaises(MarzbanConfigError):
            await client.get_users()


class RepairTests(unittest.IsolatedAsyncioTestCase):
    async def run_repair(self, current=None, expired=False, used=0):
        server = SimpleNamespace(id=1)
        key = SimpleNamespace(id=1, server_id=1, subscription_id=1, user_id=1,
                              status=VpnKeyStatus.active, outline_key_id='u1s1',
                              traffic_used_gb=used, traffic_limit_gb=10)
        sub = SimpleNamespace(user_id=1, status=SubscriptionStatus.active,
                              expires_at=datetime.utcnow() + timedelta(days=-1 if expired else 1))
        rows = lambda values: SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: values))
        session = AsyncMock()
        session.execute.side_effect = [rows([server]), rows([key]),
                                      SimpleNamespace(scalar_one_or_none=lambda: sub)]
        client = AsyncMock()
        client.get_users.return_value = {} if current is None else {'u1s1': current}
        client.get_inbounds.return_value = {'vless': ['VLESS-TCP']}
        client.create_key.return_value = MarzbanKey('u1s1', 'u1s1', LINK, 443)
        with patch('app.services.vpn_key._server_protocol', return_value='marzban'), \
             patch('app.services.vpn_key._make_client', return_value=client), \
             patch('app.services.vpn_key.get_access_url', return_value=LINK):
            result = await repair_marzban_keys(session)
        return client, result

    async def test_missing_user_preserves_uuid_and_remaining_quota(self):
        client, result = await self.run_repair(used=3)
        client.create_key.assert_awaited_once_with(name='u1s1', uuid=UUID, traffic_limit_gb=7)
        self.assertEqual(result[:2], (1, 0))

    async def test_expired_subscription_not_restored(self):
        client, _ = await self.run_repair(expired=True)
        client.create_key.assert_not_awaited()

    async def test_exhausted_subscription_not_restored(self):
        client, _ = await self.run_repair(used=10)
        client.create_key.assert_not_awaited()

    async def test_healthy_user_not_modified(self):
        client, result = await self.run_repair(current={
            'status': 'active', 'inbounds': {'vless': ['VLESS-TCP']},
            'proxies': {'vless': {'id': UUID, 'flow': 'xtls-rprx-vision'}}})
        client.create_key.assert_not_awaited()
        self.assertEqual(result[:2], (0, 0))

    async def test_wrong_uuid_repaired(self):
        client, _ = await self.run_repair(current={
            'status': 'active', 'proxies': {'vless': {'id': OTHER}}})
        self.assertEqual(client.create_key.await_args.kwargs['uuid'], UUID)

    async def test_disabled_node_user_not_reactivated(self):
        client, _ = await self.run_repair(current={'status': 'disabled'})
        client.create_key.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
