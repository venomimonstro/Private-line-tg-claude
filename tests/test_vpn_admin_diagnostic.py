import socket
import unittest
from unittest.mock import patch

from app.vpn.admin_diagnostic import _public_site_ips, _site, explain, run_diagnostic


class AdminVpnDiagnosticTests(unittest.TestCase):
    def test_site_rejects_local_targets_and_credentials(self):
        for url in ("http://example.com", "https://user:pass@example.com",
                    "https://example.com:8000", "https://example.com/#fragment",
                    "https://example.com/\r\nHost:internal"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                _site(url)
        with patch("socket.getaddrinfo", return_value=[
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))
        ]):
            with self.assertRaises(ValueError):
                _public_site_ips("localhost")

    def test_site_ip_is_pinned_to_public_resolution(self):
        self.assertEqual(_site("https://example.com/path")[0], "example.com")
        with patch("socket.getaddrinfo", return_value=[
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.1.1.1", 443))
        ]):
            self.assertEqual(_public_site_ips("example.com"), ["1.1.1.1"])

    def test_failure_is_structured_and_does_not_repeat_secret(self):
        result = run_diagnostic("vless://secret@example.com:443", "https://example.com")
        self.assertFalse(result[0]["ok"])
        self.assertNotIn("secret", repr(result))
        analysis = explain([
            {"stage": "DNS 8.8.8.8:853", "ok": False},
            {"stage": "DNS 1.1.1.1:443", "ok": True},
        ], "dns: exchange failed; outbound to 8.8.8.8:853")
        self.assertTrue(any("https://1.1.1.1/dns-query" in line for line in analysis))


if __name__ == "__main__":
    unittest.main()
