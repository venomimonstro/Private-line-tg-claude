"""The reserve profile is only published for paid UUIDs after a real gate."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.vpn import https_fallback as fallback


class HttpsFallbackTests(unittest.TestCase):
    def test_public_certificate_identity_and_paid_user_gate(self):
        primary = "vless://11111111-2222-4333-8444-555555555555@185.236.25.152:443"
        with tempfile.TemporaryDirectory() as folder, patch.object(fallback, "RELAY_DIR", Path(folder)):
            self.assertIsNone(fallback.published_fallback(primary, "https://194.146.223.104"))
            config = {"inbounds": [{"settings": {"clients": [{
                "id": "11111111-2222-4333-8444-555555555555"}]}}]}
            (Path(folder) / "config.json").write_text(json.dumps(config))
            (Path(folder) / "healthy").touch()
            link = fallback.published_fallback(primary, "https://194.146.223.104")
            self.assertIn("@194.146.223.104:443", link)
            self.assertIn("security=tls&type=ws", link)
            self.assertIn("path=%2Fvpn-tunnel", link)
            self.assertNotIn("flow=xtls-rprx-vision", link)
            self.assertIsNone(fallback.published_fallback(
                primary.replace("11111111", "99999999"), "https://194.146.223.104"))
            (Path(folder) / "healthy").unlink()
            self.assertIsNone(fallback.published_fallback(primary, "https://194.146.223.104"))

    def test_relay_credential_stable_and_separate_per_node(self):
        a = fallback.relay_uuid("test-secret", 1)
        self.assertEqual(a, fallback.relay_uuid("test-secret", 1))
        self.assertNotEqual(a, fallback.relay_uuid("test-secret", 2))
        self.assertNotEqual(a, "11111111-2222-4333-8444-555555555555")

    def test_invalid_public_url_is_never_advertised(self):
        primary = "vless://11111111-2222-4333-8444-555555555555@185.236.25.152:443"
        with self.assertRaises(ValueError):
            fallback.fallback_link(primary, "http://194.146.223.104")


if __name__ == "__main__":
    unittest.main()
