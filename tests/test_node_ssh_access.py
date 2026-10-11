import os
import unittest

from cryptography.fernet import Fernet

os.environ.setdefault("BOT_TOKEN", "test")
os.environ.setdefault("ADMIN_CHAT_ID", "1")
os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("SECRET_KEY", "test")

from app.models.vpn_server import VpnServer
from app.services.node_ssh import (
    clear_node_ssh_access,
    get_node_ssh_access,
    set_node_ssh_access,
)


def make_server() -> VpnServer:
    return VpnServer(
        id=1,
        name="Node 1",
        country="Test",
        provider="marzban",
        api_url="http://185.236.25.152:2096",
        api_secret_encrypted="x",
    )


class NodeSshAccessTests(unittest.TestCase):
    def test_password_roundtrip_is_encrypted_at_rest(self):
        server = make_server()
        password = "correct horse battery staple"
        set_node_ssh_access(
            server,
            host="185.236.25.152",
            port=22,
            username="root",
            auth_type="password",
            secret=password,
        )
        self.assertTrue(server.ssh_configured)
        self.assertNotEqual(server.ssh_secret_encrypted, password)
        access = get_node_ssh_access(server)
        self.assertEqual(access.host, "185.236.25.152")
        self.assertEqual(access.port, 22)
        self.assertEqual(access.username, "root")
        self.assertEqual(access.auth_type, "password")
        self.assertEqual(access.secret, password)

    def test_private_key_roundtrip(self):
        server = make_server()
        key = "-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n-----END OPENSSH PRIVATE KEY-----\n"
        set_node_ssh_access(
            server,
            host="vpn.example.com",
            port=2222,
            username="root",
            auth_type="private_key",
            secret=key,
        )
        access = get_node_ssh_access(server)
        self.assertEqual(access.host, "vpn.example.com")
        self.assertEqual(access.port, 2222)
        self.assertEqual(access.secret, key)

    def test_invalid_username_rejected(self):
        server = make_server()
        with self.assertRaises(ValueError):
            set_node_ssh_access(
                server,
                host="185.236.25.152",
                port=22,
                username="root;rm",
                auth_type="password",
                secret="secret",
            )

    def test_multiline_password_rejected(self):
        server = make_server()
        with self.assertRaises(ValueError):
            set_node_ssh_access(
                server,
                host="185.236.25.152",
                port=22,
                username="root",
                auth_type="password",
                secret="one\ntwo",
            )

    def test_clear_removes_every_secret_field(self):
        server = make_server()
        set_node_ssh_access(
            server,
            host="185.236.25.152",
            port=22,
            username="root",
            auth_type="password",
            secret="secret",
        )
        clear_node_ssh_access(server)
        self.assertFalse(server.ssh_configured)
        self.assertIsNone(server.ssh_secret_encrypted)


if __name__ == "__main__":
    unittest.main()
