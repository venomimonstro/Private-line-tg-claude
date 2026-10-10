import unittest

from app.vless_e2e_probe_config import build_config_from_url


class VlessProbeConfigTests(unittest.TestCase):
    def good(self, host="185.236.25.152"):
        return (
            "vless://11111111-2222-4333-8444-555555555555@" + host + ":443"
            "?encryption=none&security=reality&sni=www.microsoft.com&fp=chrome"
            "&pbk=" + "A" * 43 +
            "&sid=abcdef0123456789&type=tcp&headerType=none"
            "&flow=xtls-rprx-vision#Private%20Line%20VPN"
        )

    def test_builds_exact_reality_vision_outbound(self):
        cfg = build_config_from_url(self.good(), 19580)
        outbound = cfg["outbounds"][0]
        self.assertEqual(outbound["protocol"], "vless")
        self.assertEqual(outbound["settings"]["vnext"][0]["address"], "185.236.25.152")
        self.assertEqual(outbound["settings"]["vnext"][0]["port"], 443)
        self.assertEqual(outbound["settings"]["vnext"][0]["users"][0]["flow"], "xtls-rprx-vision")
        reality = outbound["streamSettings"]["realitySettings"]
        self.assertEqual(reality["serverName"], "www.microsoft.com")
        self.assertEqual(reality["fingerprint"], "chrome")
        self.assertEqual(reality["shortId"], "abcdef0123456789")

    def test_rejects_non_public_endpoint(self):
        with self.assertRaises(ValueError):
            build_config_from_url(self.good("127.0.0.1"), 19580)

    def test_rejects_wrong_fingerprint(self):
        with self.assertRaises(ValueError):
            build_config_from_url(self.good().replace("fp=chrome", "fp=none"), 19580)


if __name__ == "__main__":
    unittest.main()
