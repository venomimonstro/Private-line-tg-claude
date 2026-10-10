import json
import unittest
from types import SimpleNamespace

from app.encryption import encrypt
from app.services.vpn_key import _make_client, _server_protocol


class VpnProviderFailClosedTests(unittest.TestCase):
    def server(self, encrypted):
        return SimpleNamespace(id=99, api_url="http://203.0.113.10:2096", api_secret_encrypted=encrypted)

    def test_corrupt_credentials_do_not_fallback_to_outline(self):
        server = self.server("definitely-not-valid-encrypted-data")
        self.assertEqual(_server_protocol(server), "invalid")
        with self.assertRaises(ValueError):
            _make_client(server)

    def test_unknown_protocol_is_invalid(self):
        server = self.server(encrypt(json.dumps({"protocol": "mystery"})))
        self.assertEqual(_server_protocol(server), "invalid")
        with self.assertRaises(ValueError):
            _make_client(server)

    def test_explicit_marzban_protocol_is_detected(self):
        server = self.server(encrypt(json.dumps({
            "protocol": "marzban", "username": "admin", "password": "secret-value",
        })))
        self.assertEqual(_server_protocol(server), "marzban")


if __name__ == "__main__":
    unittest.main()
