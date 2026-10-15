"""Windows remote probe accepts only bounded, credential-free trace data."""
import unittest
import hashlib
import tempfile
from pathlib import Path
from unittest.mock import patch
from pydantic import ValidationError

from admin.routes.vpn_probe import ProbeReport, explain, safe_steps
from app.vpn import probe_package


class ProbeReportTests(unittest.TestCase):
    def test_report_accepts_bounded_stage_codes(self):
        report = ProbeReport(test_id="a" * 24, profile_sha256="b" * 64,
                             windows="10.0.26100", steps=[{"stage": "vpn_dns_853",
                             "code": "DNS853_TIMEOUT", "elapsed_ms": 7000}])
        self.assertEqual(safe_steps(report)[0]["code"], "DNS853_TIMEOUT")

    def test_raw_log_and_vless_credential_rejected(self):
        base = dict(test_id="a" * 24, profile_sha256="b" * 64, windows="10.0")
        with self.assertRaises(ValidationError):
            ProbeReport(**base, steps=[{"stage": "vpn_site", "code": "OK",
                         "elapsed_ms": 1, "raw_log": "vless://private"}])
        with self.assertRaises(ValidationError):
            ProbeReport(**base, steps=[{"stage": "vpn_site", "code": "OK", "elapsed_ms": 1}],
                        vless_url="vless://private")
        with self.assertRaises(ValueError):
            safe_steps(ProbeReport(**base, steps=[{"stage": "vpn_site",
                                      "code": "vless://private", "elapsed_ms": 1}]))

    def test_public_ingress_and_disposable_script(self):
        root = Path("/app") if Path("/app/nginx/nginx.conf").exists() else Path(__file__).resolve().parents[1]
        nginx = (root / "nginx/nginx.conf").read_text()
        script = (root / "windows_probe/Probe-PrivateLine.ps1").read_text()
        self.assertIn("location = /api/vpn-probe/report {", nginx)
        self.assertIn("Get-FileHash -Algorithm SHA256", script)
        self.assertIn("Test-Dns853", script)
        self.assertIn("Remove-Item $Workspace -Recurse", script)
        self.assertNotIn("Set-ItemProperty -Path 'HKCU", script)

    def test_dns_failure_after_working_https_identifies_client_dns_path(self):
        steps = [{"stage": "vpn_site", "code": "OK"},
                 {"stage": "vpn_dns_853", "code": "DNS853_TIMEOUT"}]
        summary = explain(steps, {"status": "observed", "listener": True,
                                  "key_on_node": True}, True)
        self.assertIn("8.8.8.8:853", summary)

    def test_node_key_mismatch_takes_priority_over_client_errors(self):
        steps = [{"stage": "node_tcp", "code": "TCP_TIMEOUT"}]
        summary = explain(steps, {"status": "observed", "listener": True,
                                  "key_on_node": False}, True)
        self.assertIn("отличается на VPN-ноде", summary)

    def test_cached_windows_binary_is_never_served_after_digest_mismatch(self):
        with tempfile.TemporaryDirectory() as folder:
            cache = Path(folder)
            archive = cache / "xray.zip"
            digest = cache / "xray.sha256"
            archive.write_bytes(b"tampered archive")
            digest.write_text(hashlib.sha256(b"official archive").hexdigest())
            with patch.object(probe_package, "CACHE", cache), \
                 patch.object(probe_package, "ZIP", archive), \
                 patch.object(probe_package, "DIGEST", digest), \
                 patch.object(probe_package, "_download", side_effect=OSError("offline")):
                with self.assertRaises(OSError):
                    probe_package.verified_xray_archive()


if __name__ == "__main__":
    unittest.main()
