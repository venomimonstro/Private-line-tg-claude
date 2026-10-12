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


    def test_streaming_personalization_does_not_require_full_installer_copy(self):
        old = native.BASE_INSTALLER
        try:
            with tempfile.TemporaryDirectory() as td:
                base = Path(td) / "PrivateLineSetup.exe"
                base.write_bytes(b"MZ" + b"A" * (3 * 1024 * 1024))
                native.BASE_INSTALLER = base
                overlay = native.build_personalization_overlay(
                    base_url="https://198.51.100.10",
                    pairing_code="x" * 48,
                    tls_pin="sha256//" + "A" * 44,
                )
                chunks = list(native.iter_personal_windows_exe(
                    overlay=overlay, chunk_size=256 * 1024
                ))
                self.assertGreater(len(chunks), 2)
                self.assertTrue(all(len(x) <= 256 * 1024 for x in chunks[:-1]))
                self.assertEqual(chunks[-1], overlay)
                self.assertEqual(
                    sum(map(len, chunks)),
                    base.stat().st_size + len(overlay),
                )
        finally:
            native.BASE_INSTALLER = old

    def test_stream_snapshot_survives_atomic_replacement(self):
        old = native.BASE_INSTALLER
        try:
            with tempfile.TemporaryDirectory() as td:
                base = Path(td) / "PrivateLineSetup.exe"
                base.write_bytes(b"MZ" + b"A" * 150_000)
                native.BASE_INSTALLER = base
                overlay = native.build_personalization_overlay(
                    base_url="https://198.51.100.10",
                    pairing_code="x" * 48,
                    tls_pin="sha256//" + "A" * 44,
                )
                size, stream = native.open_personal_windows_exe(
                    overlay=overlay, chunk_size=64 * 1024
                )
                replacement = Path(td) / "new.exe"
                replacement.write_bytes(b"MZ" + b"B" * 200_000)
                replacement.replace(base)
                result = b"".join(stream)
                self.assertEqual(len(result), size)
                self.assertTrue(result.startswith(b"MZ" + b"A" * 100))
                self.assertTrue(result.endswith(overlay))
        finally:
            native.BASE_INSTALLER = old

    def test_native_installer_is_single_instance_atomic_and_low_load(self):
        root = Path("/app")
        installer = (root / "windows_native/PrivateLine.Installer/Program.cs").read_text(encoding="utf-8")
        route = (root / "admin/routes/windows_diagnostics.py").read_text(encoding="utf-8")
        self.assertIn('Path.Combine(dir,"install.lock")', installer)
        self.assertIn("FileShare.None", installer)
        self.assertIn("PrivateLineClientV2", installer)
        self.assertIn("CLIENT_RUNNING_CLOSE_FIRST", installer)
        self.assertIn("InstallEmbeddedXrayAsync", installer)
        self.assertIn("File.Move(temp,destination,true)", installer)
        self.assertIn("/api/windows/enroll", installer)
        pair_block = installer[installer.index("static async Task PairAsync"):]
        self.assertNotIn("/api/windows/profile", pair_block)
        self.assertIn("open_personal_windows_exe", route)
        self.assertIn("Content-Length", route)
        self.assertIn('"X-Accel-Buffering": "no"', route)
        self.assertNotIn("BytesIO(payload)", route)

    def test_native_client_recovers_proxy_and_uses_local_failover(self):
        root = Path("/app/windows_native")
        client = (root / "PrivateLine.Client/MainForm.cs").read_text(encoding="utf-8")
        program = (root / "PrivateLine.Client/Program.cs").read_text(encoding="utf-8")
        self.assertIn("PrivateLineClientV2", program)
        self.assertIn("FetchProfilesAsync", client)
        self.assertIn("vless_urls", client)
        self.assertIn("AllocateSocksPort", client)
        self.assertIn("RecoverStaleProxy", client)
        self.assertIn("CleanupOrphanXray", client)
        self.assertIn("ProxyState", client)
        self.assertIn("tunnelWatchdog", client)
        self.assertIn("CurlProbeAsync", client)
        self.assertIn("success<2", client.replace(" ", ""))
        self.assertIn("diagnostics.Checked=false", client.replace(" ", ""))
        self.assertNotIn("_ = FlushPendingAsync()", client)

    def test_windows_build_verifies_official_xray_digest_and_reuses_cache(self):
        build = Path("/app/windows_native/build.sh").read_text(encoding="utf-8")
        self.assertIn("Xray-windows-64.zip.dgst", build)
        self.assertIn("SHA2-256", build)
        self.assertIn("sha256sum", build)
        self.assertIn("windows_cache", build)
        self.assertIn("dotnet publish", build)
        self.assertIn("dotnet publish", build)
        self.assertIn('STAGED="$OUT/.PrivateLineSetup.', build)
        self.assertIn('mv -f "$STAGED" "$OUT/PrivateLineSetup.exe"', build)


    def test_existing_paid_keys_remain_usable_when_health_check_is_stale(self):
        root = Path("/app")
        enrollment = (root / "app/windows_enrollment.py").read_text(encoding="utf-8")
        route = (root / "admin/routes/windows_diagnostics.py").read_text(encoding="utf-8")
        self.assertIn("async def active_user_servers", enrollment)
        self.assertIn("VpnServer.status != VpnServerStatus.disabled", enrollment)
        self.assertIn('item["status"] != "healthy"', enrollment)
        self.assertNotIn("last_e2e_success_at >= fresh_after", enrollment)
        self.assertIn('"servers": servers', route)
        self.assertIn('"vless_urls": urls', route)

    def test_windows_can_select_named_server_or_direct_telegram_key(self):
        root = Path("/app")
        client = (root / "windows_native/PrivateLine.Client/MainForm.cs").read_text(encoding="utf-8")
        self.assertIn("serverSelector", client)
        self.assertIn("Автоматически (лучший доступный)", client)
        self.assertIn("SelectedServerId()", client)
        self.assertIn("PasteTelegramKey()", client)
        self.assertIn("DataProtectionScope.CurrentUser", client)
        self.assertIn("Ключ из Telegram", client)
        self.assertIn("candidates=availableServers.Where", client)

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
