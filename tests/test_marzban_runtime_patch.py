"""Execute the installer patch, including a public API between decorated helpers."""
import ast
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
# This layout reproduces upstream 0.8.4: the three private helpers are
# followed by ordinary public methods before the next decorated node method.
SOURCE = '''from functools import lru_cache
@threaded_function
def _add_user_to_inbound(api, inbound_tag, account):
    pass
@threaded_function
def _remove_user_from_inbound(api, inbound_tag, email):
    pass
@threaded_function
def _alter_inbound_user(api, inbound_tag, account):
    pass

def add_user(dbuser):
    return "add"
def remove_user(dbuser):
    return "remove"
def update_user(dbuser):
    return "update"
def remove_node(node_id):
    return "remove_node"
def add_node(dbnode):
    return "add_node"
def _change_node_status(node_id, status):
    return status


@threaded_function
def connect_node(node_id, config=None):
    return "connect"
@threaded_function
def restart_node(node_id, config=None):
    return "restart"
'''


def install_patch(source):
    script = (ROOT / 'scripts/prepare_marzban_runtime.sh').read_text()
    code = script.split("python3 - <<'PY'\n", 1)[1].split('\nPY\n', 1)[0]
    with tempfile.TemporaryDirectory() as tmp:
        src, dst = Path(tmp) / 'source.py', Path(tmp) / 'patched.py'
        src.write_text(source)
        with patch.dict(os.environ, SOURCE=str(src), DEST=str(dst), MARKER='PRIVATE_LINE_XRAY_MUTATION_SYNC_V3'):
            exec(compile(code, 'runtime installer patch', 'exec'), {})
        return dst.read_text()


class RuntimePatchTests(unittest.TestCase):
    def test_public_operations_survive_with_unchanged_bodies(self):
        before = {n.name: n for n in ast.parse(SOURCE).body if isinstance(n, ast.FunctionDef)}
        after = {n.name: n for n in ast.parse(install_patch(SOURCE)).body if isinstance(n, ast.FunctionDef)}
        self.assertEqual(set(before), set(after))
        for name in before:
            if name.startswith(('_add_user_', '_remove_user_', '_alter_inbound_')):
                self.assertFalse(after[name].decorator_list)
            else:
                self.assertEqual(ast.dump(before[name]), ast.dump(after[name]))

    def helpers(self):
        tree = ast.parse(install_patch(SOURCE))
        names = {'_add_user_to_inbound', '_remove_user_from_inbound', '_alter_inbound_user'}
        module = ast.Module(body=[n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names], type_ignores=[])
        class Exists(Exception): pass
        class Missing(Exception): pass
        class Offline(Exception): pass
        ns = dict(XRayAPI=object, Account=object, sleep=Mock(), xray=SimpleNamespace(
            exc=SimpleNamespace(EmailExistsError=Exists, EmailNotFoundError=Missing, ConnectionError=Offline)))
        exec(compile(module, 'patched operations', 'exec'), ns)
        return ns, Exists, Missing, Offline

    def test_live_add_waits_for_grpc_and_propagates_exhausted_retries(self):
        ns, _, _, offline = self.helpers()
        api = Mock()
        api.add_inbound_user.side_effect = [offline(), None]
        ns['_add_user_to_inbound'](api, 'VLESS-TCP', 'account')
        self.assertEqual(api.add_inbound_user.call_count, 2)
        api.reset_mock()
        api.add_inbound_user.side_effect = offline()
        with self.assertRaises(offline):
            ns['_add_user_to_inbound'](api, 'VLESS-TCP', 'account')
        self.assertEqual(api.add_inbound_user.call_count, 3)

    def test_update_removes_then_adds_and_revocation_is_synchronous(self):
        ns, _, missing, _ = self.helpers()
        api = Mock()
        account = SimpleNamespace(email='user@example')
        ns['_alter_inbound_user'](api, 'VLESS-TCP', account)
        self.assertEqual([c[0] for c in api.mock_calls], ['remove_inbound_user', 'add_inbound_user'])
        api.reset_mock()
        api.remove_inbound_user.side_effect = missing()
        ns['_remove_user_from_inbound'](api, 'VLESS-TCP', account.email)
        api.remove_inbound_user.assert_called_once()
