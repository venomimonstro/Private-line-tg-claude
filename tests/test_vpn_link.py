"""Regression checks for client-ready VLESS REALITY share links. No live probes."""
import unittest
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

from app.services.vpn_link import canonical_client_link


class VlessShareLinkTests(unittest.TestCase):
    URI = (
        "vless://11111111-2222-4333-8444-555555555555@185.236.25.152:443"
        "?security=reality&encryption=none&pbk=" + "A" * 43
        + "&sni=www.microsoft.com&fp=chrome&type=tcp"
        "&flow=xtls-rprx-vision&sid=abcdef0123456789#old-profile"
    )

    def test_drops_old_fragment_and_encodes_one_new_label(self):
        shared = canonical_client_link(self.URI, "Испания VPN")
        self.assertEqual(shared.count("#"), 1)
        self.assertNotIn("old-profile", shared)
        self.assertEqual(urlsplit(shared).fragment, "%D0%98%D1%81%D0%BF%D0%B0%D0%BD%D0%B8%D1%8F%20VPN")
        self.assertEqual(str(UUID(urlsplit(shared).username)), "11111111-2222-4333-8444-555555555555")
        self.assertEqual(urlsplit(shared).hostname, "185.236.25.152")
        self.assertEqual(urlsplit(shared).port, 443)

    def test_exact_client_transport_settings(self):
        q = parse_qs(urlsplit(canonical_client_link(self.URI)).query)
        for name, expected in (
            ("security", "reality"), ("flow", "xtls-rprx-vision"),
            ("encryption", "none"), ("fp", "chrome"),
            ("type", "tcp"), ("headerType", "none"),
            ("sni", "www.microsoft.com"), ("sid", "abcdef0123456789"),
        ):
            self.assertEqual(q[name], [expected])

    def test_idempotent_and_no_secret_rotation(self):
        one = canonical_client_link(self.URI)
        two = canonical_client_link(one)
        self.assertEqual(one, two)
        self.assertEqual(parse_qs(urlsplit(one).query)["pbk"], ["A" * 43])

    def test_rejects_wrong_client_fingerprint(self):
        with self.assertRaises(ValueError):
            canonical_client_link(self.URI.replace("fp=chrome", "fp=none"))

    def test_rejects_duplicate_security_params(self):
        with self.assertRaises(ValueError):
            canonical_client_link(self.URI.replace("#old-profile", "&security=tls#old-profile"))

    def test_empty_short_id_supported(self):
        raw = self.URI.replace("sid=abcdef0123456789", "sid=")
        out = canonical_client_link(raw)
        self.assertIn("sid=", urlsplit(out).query)

    def test_private_line_brand_is_default_profile_name(self):
        shared = canonical_client_link(self.URI)
        self.assertEqual(urlsplit(shared).fragment, "Private%20Line%20VPN")

    def test_outline_url_left_alone(self):
        self.assertEqual(canonical_client_link("ss://abc@example.org:1111#old"), "ss://abc@example.org:1111#old")


if __name__ == "__main__":
    unittest.main()
