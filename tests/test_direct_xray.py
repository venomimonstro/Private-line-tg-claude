import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from node.agent import APIError, Node, atomic_json, normalize_user, runtime_config
from node.migrate import build_state


def state_fixture():
    config = {'inbounds': [{'tag': 'VLESS-TCP', 'protocol': 'vless', 'port': 443,
        'settings': {'clients': []}, 'streamSettings': {'security': 'reality',
        'realitySettings': {'privateKey': 'a' * 43, 'serverNames': ['dl.google.com'],
                           'shortIds': ['0123456789abcdef']}}}]}
    return build_state({'config': config, 'users': []},
                       {'username': 'admin', 'password': 'secret'}, '194.146.223.104', '185.236.25.152')


class FakeNode(Node):
    """Model CLI acknowledgements independently of desired state."""
    def __init__(self, directory):
        super().__init__(directory, '/not-executed')
        self.members = {}
        self.restarts = 0
        self.fail_add = False
        self.stats = []

    def api(self, action, *args, data=None):
        if action == 'inbounduser':
            return json.dumps({'users': list(self.members.values())})
        if action == 'adu':
            if self.fail_add:
                return 'error\nAdded 0 user(s) in total.'
            u = json.loads(data)['inbounds'][0]['settings']['clients'][0]
            self.members[u['email']] = {'email': u['email'], 'account': {'id': u['id'], 'flow': u['flow']}}
            return 'Added 1 user(s) in total.'
        if action == 'rmu':
            self.members.pop(args[-1])
            return 'Removed 1 user(s) in total.'
        if action == 'statsquery':
            return json.dumps({'stat': self.stats})
        raise AssertionError(action)

    def start_core(self):
        self.restarts += 1
        self.members = {u['username']: {'email': u['username'], 'account': u['proxies']['vless']}
                        for u in self.state['users'].values() if u['status'] == 'active'}


class DirectNodeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        atomic_json(Path(self.tmp.name) / 'state.json', state_fixture())
        self.node = FakeNode(self.tmp.name)
        self.headers = {'Authorization': 'Bearer ' + self.node.token}

    def call(self, method, path, body=None, source='194.146.223.104'):
        return self.node.dispatch(method, path, self.headers, json.dumps(body or {}).encode(), source)

    def create(self, name='u1s1'):
        return self.call('POST', '/api/user', {'username': name, 'proxies': {'vless': {'id': str(uuid4())}}})

    def test_create_update_delete_are_hot_and_durable(self):
        user = self.create()
        self.assertTrue(self.node.matches(self.node.members['u1s1'], user))
        updated = self.call('PUT', '/api/user/u1s1', {'data_limit': 1000})
        self.assertEqual(user['proxies'], updated['proxies'])
        reloaded = Node(self.tmp.name, '/unused')
        self.assertEqual(reloaded.state['users']['u1s1'], updated)
        self.call('DELETE', '/api/user/u1s1')
        self.assertEqual(self.node.members, {})
        self.assertEqual(self.node.restarts, 0)
        self.assertEqual(Node(self.tmp.name, '/unused').state['users'], {})

    def test_duplicate_post_does_not_rotate_uuid(self):
        first = self.create()
        with self.assertRaises(APIError) as e:
            self.create()
        self.assertEqual(e.exception.status, 409)
        self.assertEqual(self.node.state['users']['u1s1'], first)

    def test_uuid_collision_rejected(self):
        user = self.create()
        with self.assertRaises(APIError):
            self.call('POST', '/api/user', {**user, 'username': 'u2s2'})
        self.assertNotIn('u2s2', self.node.state['users'])

    def test_cli_zero_exit_with_failure_recovers_from_durable_desired_state(self):
        self.node.fail_add = True
        user = self.create()
        self.assertEqual(self.node.restarts, 1)
        self.assertTrue(self.node.matches(self.node.members['u1s1'], user))

    def test_failed_hotadd_and_failed_recovery_never_returns_success(self):
        self.node.fail_add = True
        with patch.object(self.node, 'start_core', side_effect=RuntimeError('failed')):
            with self.assertRaises(RuntimeError):
                self.create()
        self.assertIn('u1s1', Node(self.tmp.name, '/unused').state['users'])

    def test_fsync_failure_prevents_runtime_mutation(self):
        with patch.object(self.node, 'save', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.create()
        self.assertFalse(self.node.members)
        self.assertFalse(self.node.state['users'])

    def test_wrong_live_uuid_is_repaired_synchronously(self):
        user = self.create()
        self.node.members['u1s1']['account']['id'] = str(uuid4())
        self.call('PUT', '/api/user/u1s1', {'status': 'active'})
        self.assertTrue(self.node.matches(self.node.members['u1s1'], user))
        self.assertEqual(self.node.restarts, 0)

    def test_quota_is_counted_once_and_revokes_live_user(self):
        self.create()
        self.call('PUT', '/api/user/u1s1', {'data_limit': 100})
        self.node.stats = [{'name': 'user>>>u1s1>>>traffic>>>uplink', 'value': 60}]
        self.node.accounting()
        self.node.accounting()
        self.assertEqual(self.node.state['users']['u1s1']['used_traffic'], 60)
        self.node.stats[0]['value'] = 120
        self.node.accounting()
        self.assertEqual(self.node.state['users']['u1s1']['status'], 'limited')
        self.assertNotIn('u1s1', self.node.members)

    def test_expired_user_never_enters_boot_configuration(self):
        user = self.create()
        with patch('node.agent.time.time', return_value=100):
            expired = normalize_user({**user, 'expire': 99}, self.node.tag)
        self.node.state['users']['u1s1'] = expired
        self.assertFalse(runtime_config(self.node.state)['inbounds'][0]['settings']['clients'])

    def test_source_and_auth_required(self):
        with self.assertRaises(APIError):
            self.call('GET', '/api/users', source='203.0.113.10')
        with self.assertRaises(APIError):
            self.node.dispatch('GET', '/api/users', {}, b'', '194.146.223.104')
        token = self.node.dispatch('POST', '/api/admin/token', {},
                                  b'username=admin&password=secret', '194.146.223.104')
        self.assertEqual(token['access_token'], self.node.token)

    def test_pagination(self):
        self.create('u1s1')
        self.create('u2s2')
        self.assertEqual(len(self.call('GET', '/api/users?limit=1&offset=1')['users']), 1)
        self.assertFalse(self.call('GET', '/api/users?offset=2')['users'])

    def test_migration_preserves_keys_usage_and_disabled_status(self):
        user = self.create()
        user.update(status='disabled', used_traffic=555, data_limit=1000)
        source = state_fixture()
        result = build_state({'config': source['config'], 'users': [user]}, source['auth'],
                             source['control_ip'], '185.236.25.152')
        self.assertEqual(result['users']['u1s1'], user)
        self.assertEqual(result['config']['inbounds'][0]['streamSettings'],
                         source['config']['inbounds'][0]['streamSettings'])

    def test_migration_refuses_missing_uuid_and_on_hold_accounts(self):
        source = state_fixture()
        for user in ({'username': 'u1s1'}, {'username': 'u1s1', 'status': 'on_hold',
                     'proxies': {'vless': {'id': str(uuid4())}}}):
            with self.assertRaises((ValueError, APIError)):
                build_state({'config': source['config'], 'users': [user]}, source['auth'],
                            source['control_ip'], '185.236.25.152')

    def test_mutation_cannot_reset_accounting(self):
        self.create()
        self.node.state['users']['u1s1']['used_traffic'] = 500
        updated = self.call('PUT', '/api/user/u1s1', {'used_traffic': 0})
        self.assertEqual(updated['used_traffic'], 500)


if __name__ == '__main__':
    unittest.main()

class HTTPCompatibilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_existing_control_plane_client_issues_and_revokes_native_key(self):
        import asyncio
        import threading
        from urllib.parse import parse_qs, urlsplit
        from node.agent import serve
        from app.vpn.marzban import MarzbanClient
        with tempfile.TemporaryDirectory() as directory:
            atomic_json(Path(directory) / 'state.json', state_fixture())
            node = FakeNode(directory)
            server = serve(node, '127.0.0.1', 0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            client = MarzbanClient('http://127.0.0.1:' + str(server.server_port), 'admin', 'secret')
            try:
                key = await client.create_key('u1s1', restart_core=False)
                uri = urlsplit(key.access_url)
                self.assertEqual(uri.hostname, '185.236.25.152')
                self.assertEqual(uri.port, 443)
                self.assertEqual(parse_qs(uri.query)['flow'], ['xtls-rprx-vision'])
                self.assertEqual(parse_qs(uri.query)['sni'], ['dl.google.com'])
                self.assertEqual(uri.username, node.state['users']['u1s1']['proxies']['vless']['id'])
                repeated = await client.create_key('u1s1', restart_core=False)
                self.assertEqual(key.access_url, repeated.access_url)
                self.assertEqual((await client.get_key('u1s1')).access_url, key.access_url)
                self.assertEqual(len(await client.get_users()), 1)
                await client.disable_key('u1s1', restart_core=False)
                self.assertFalse(node.members)
                await client.delete_key('u1s1', restart_core=False)
                self.assertTrue(await client.user_absent('u1s1'))
                self.assertEqual(node.restarts, 0)
            finally:
                await client.aclose()
                await asyncio.to_thread(server.shutdown)
                server.server_close()
                thread.join(timeout=2)
