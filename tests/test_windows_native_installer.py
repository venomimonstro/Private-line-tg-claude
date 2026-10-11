"""Regression checks for the native Windows installer delivery path."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import app.windows_native_installer as native


class NativeWindowsInstallerTests(unittest.TestCase):
    def test_personalization_appends_only_one_time_bootstrap(self):
        old = native.BASE_INSTALLER
        try:
            with tempfile.TemporaryDirectory() as td:
                base = Path(td) / "PrivateLineSetup.exe"
                base.write_bytes(b"MZ" + b"\0" * 120_000)
                native.BASE_INSTALLER = base
                result = native.build_personal_windows_exe(
                    base_url="https://198.51.100.10",
                    pairing_code="x" * 48,
                    tls_pin="sha256//" + "A" * 44,
                )
                self.assertTrue(result.startswith(b"MZ"))
                marker = result.rfind(native.MARKER)
                self.assertGreater(marker, 100_000)
                payload = json.loads(result[marker + len(native.MARKER):])
                self.assertEqual(payload["api_base"], "https://198.51.100.10")
                self.assertEqual(payload["pair_code"], "x" * 48)
                self.assertTrue(payload["tls_pin"].startswith("sha256//"))
                self.assertNotIn("vless", result[marker:].decode("utf-8").lower())
        finally:
            native.BASE_INSTALLER = old

    def test_native_sources_do_not_require_manual_credentials(self):
        root = Path("/app/windows_native")
        client = (root / "PrivateLine.Client/MainForm.cs").read_text(encoding="utf-8")
        installer = (root / "PrivateLine.Installer/Program.cs").read_text(encoding="utf-8")
        self.assertIn("/api/windows/profile", client)
        self.assertIn("/api/windows/report", client)
        self.assertIn("PinnedHttp", client)
        self.assertIn("EmbeddedResource", (root / "PrivateLine.Installer/PrivateLine.Installer.csproj").read_text())
        self.assertIn("xray.exe", (root / "PrivateLine.Installer/PrivateLine.Installer.csproj").read_text())
        self.assertIn("/api/windows/enroll", installer)
        self.assertIn("/api/windows/install-report", installer)
        self.assertNotIn("TextBox", installer)


if __name__ == "__main__":
    unittest.main()
