"""Safety and packaging checks for Windows diagnosis uploads."""
from __future__ import annotations

import io
import json
import unittest
import zipfile
from pathlib import Path

from app.windows_bundle import build_windows_client
from admin.routes.windows_diagnostics import DiagnosticReport, sanitize

ROOT=Path("/app")


class WindowsDiagnosticsTests(unittest.TestCase):
    def test_installer_bundle_contains_only_allowlisted_sources(self):
        with zipfile.ZipFile(io.BytesIO(build_windows_client())) as archive:
            self.assertEqual(set(archive.namelist()), {
                "INSTALL-WINDOWS.cmd", "Install-PrivateLine.ps1",
                "PrivateLine.ps1", "README_RU.md",
                "CHECK-INSTALL.cmd", "Check-Install.ps1",
                "Pair-PrivateLine.ps1",
            })
            self.assertIsNone(archive.testzip())
            for source in ("Install-PrivateLine.ps1", "PrivateLine.ps1", "Check-Install.ps1", "Pair-PrivateLine.ps1"):
                self.assertTrue(archive.read(source).startswith(bytes((0xEF,0xBB,0xBF))))
            self.assertNotIn("profile.json", archive.namelist())
            self.assertNotIn("xray.exe", archive.namelist())

    def test_installer_success_requires_verified_app_window(self):
        installer = (ROOT/"windows_client/Install-PrivateLine.ps1").read_text()
        self.assertIn("gui-ready.json", installer)
        self.assertIn("CLIENT_SCRIPT_SYNTAX", installer)
        self.assertIn("WINDOW_NOT_CONFIRMED", installer)
        self.assertIn("gui_confirmed = $true", installer)
        status_check = (ROOT/"windows_client/Check-Install.ps1").read_text()
        self.assertIn("INSTALL_NOT_CONFIRMED", status_check)
        self.assertIn("XRAY_VERSION_INVALID", status_check)

    def test_windows_proxy_updates_notify_running_apps(self):
        client = (ROOT/"windows_client/PrivateLine.ps1").read_text()
        self.assertIn("InternetSetOption", client)
        self.assertIn("Refresh-WindowsProxy", client)
        self.assertIn("gui-ready.json", client)
        self.assertIn("Проверить установку", client)

    def test_consented_allowlisted_diagnostics(self):
        report=DiagnosticReport(
            version="1.0",windows="10.0",consent=True,
            stages=[{"stage":"tcp","result":"failed","code":"NODE_UNREACHABLE"}],
        )
        self.assertEqual(sanitize(report)["stages"][0]["code"],"NODE_UNREACHABLE")

    def test_rejects_raw_logs_and_secrets(self):
        for code in ("vless://SECRET", "secret@host", "user=123", "UUID-1234 with spaces"):
            with self.assertRaises(ValueError):
                sanitize(DiagnosticReport(
                    version="1",windows="10",consent=True,
                    stages=[{"stage":"tcp","result":"failed","code":code}],
                ))

    def test_requires_opt_in(self):
        with self.assertRaises(ValueError):
            sanitize(DiagnosticReport(
                version="1",windows="10",consent=False,
                stages=[{"stage":"https","result":"failed","code":"VPN_EGRESS_FAILED"}],
            ))

    def test_nginx_public_diagnostics_route_explicit(self):
        text=(ROOT/"nginx/nginx.conf").read_text()
        self.assertIn("location = /api/windows-diagnostics {",text)

    def test_admin_routes_match_nginx_prefix_rewrite(self):
        source=(ROOT/"admin/routes/windows_diagnostics.py").read_text()
        self.assertIn('@router.get("/windows-diagnostics"',source)
        self.assertIn('@router.get("/windows-client/download"',source)
        self.assertNotIn('@router.get("/admin/windows-diagnostics"',source)
        self.assertNotIn('@router.get("/admin/windows-client/download"',source)

    def test_native_windows_diagnostics_is_manual_only(self):
        source=(ROOT/"windows_native/PrivateLine.Client/MainForm.cs").read_text()
        self.assertIn("diagnostics.Checked=false", source.replace(" ", ""))
        self.assertIn("Отправить диагностику сейчас", source)
        self.assertNotIn("_ = FlushPendingAsync()", source)
        self.assertNotIn("await SendReportAsync();", source)

    def test_windows_script_does_not_auto_send(self):
        source=(ROOT/"windows_client/PrivateLine.ps1").read_text()
        self.assertIn("$send.Add_Click(", source)
        self.assertIn("Send-Diagnostics", source)
        self.assertIn("if(-not $Consent.Checked)",source)
        self.assertIn("$Consent.Checked=$false", source)
        self.assertNotIn("Send-Diagnostics -Automatic", source)
        self.assertNotIn("Invoke-WebRequest -Uri $endpoint",source)


if __name__=="__main__":
    unittest.main()
