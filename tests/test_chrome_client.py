"""Chrome client distribution and VLESS Reality profile safety regressions."""
from __future__ import annotations

import importlib.util
import io
import json
import pathlib
import unittest
import zipfile

from app.chrome_bundle import build_chrome_bundle, FILES, ROOT

BRIDGE_SOURCE = ROOT / "native" / "bridge.py"
spec = importlib.util.spec_from_file_location("private_line_chrome_bridge", BRIDGE_SOURCE)
assert spec and spec.loader
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)

TEST_URL = (
    "vless://11111111-1111-4111-8111-111111111111@185.236.25.152:443"
    "?encryption=none&security=reality&sni=dl.google.com&fp=chrome&pbk="
    + "A" * 43
    + "&sid=12&type=tcp&headerType=none&flow=xtls-rprx-vision#Private%20Line%20VPN"
)


class ChromeClientTests(unittest.TestCase):
    def test_bundle_is_valid_and_contains_only_allowlisted_files(self):
        with zipfile.ZipFile(io.BytesIO(build_chrome_bundle())) as archive:
            self.assertEqual(set(archive.namelist()), set(FILES))
            self.assertFalse(any(name.startswith("/") or ".." in pathlib.PurePosixPath(name).parts for name in archive.namelist()))
            manifest = json.loads(archive.read("extension/manifest.json"))
            self.assertEqual(manifest["manifest_version"], 3)
            self.assertIn("nativeMessaging", manifest["permissions"])
            self.assertIn("proxy", manifest["permissions"])
            self.assertIn("background.js", manifest["background"]["service_worker"])
            self.assertNotIn("client.json", archive.namelist())
            self.assertNotIn(".env", archive.namelist())

    def test_reality_url_builds_isolated_local_socks_config(self):
        config = bridge.config_from_vless(TEST_URL)
        inbound = config["inbounds"][0]
        self.assertEqual((inbound["listen"], inbound["port"]), ("127.0.0.1", 19590))
        out = config["outbounds"][0]
        self.assertEqual(out["protocol"], "vless")
        self.assertEqual(out["settings"]["vnext"][0]["address"], "185.236.25.152")
        self.assertEqual(out["streamSettings"]["realitySettings"]["serverName"], "dl.google.com")
        self.assertEqual(out["streamSettings"]["realitySettings"]["password"], "A" * 43)

    def test_rejects_wrong_vision_flow(self):
        with self.assertRaises(ValueError):
            bridge.config_from_vless(TEST_URL.replace("xtls-rprx-vision", "none"))

    def test_rejects_non_global_vpn_endpoint(self):
        with self.assertRaises(ValueError):
            bridge.config_from_vless(TEST_URL.replace("185.236.25.152", "127.0.0.1"))

    def test_rejects_duplicate_or_missing_reality_param(self):
        with self.assertRaises(ValueError):
            bridge.config_from_vless(TEST_URL + "&sni=evil.example")
        with self.assertRaises(ValueError):
            bridge.config_from_vless(TEST_URL.replace("&pbk=" + "A" * 43, ""))

    def test_rejects_non_vless_schemes(self):
        with self.assertRaises(ValueError):
            bridge.config_from_vless("socks://127.0.0.1:1080")

    def test_extension_never_stores_credentials(self):
        script = (ROOT / "extension" / "background.js").read_text(encoding="utf-8")
        popup = (ROOT / "extension" / "popup.js").read_text(encoding="utf-8")
        self.assertIn("sendNativeMessage", script)
        self.assertNotIn("storage.local.set", script)
        self.assertNotIn("storage.local.set", popup)
        self.assertIn('$("vless").value = ""', popup)


if __name__ == "__main__":
    unittest.main()
