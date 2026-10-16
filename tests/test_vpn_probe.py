"""Windows remote probe accepts only bounded, credential-free trace data."""
import unittest
import hashlib
import tempfile
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace
from pydantic import ValidationError

from admin.routes.vpn_probe import CMD_LAUNCHER, ProbeReport, explain, safe_steps
from app.vpn import probe_package
from app.services.vpn_link import canonical_client_link
from scripts import direct_xray_node as node


class ProbeReportTests(unittest.TestCase):
    def test_node_profile_parameter_order_does_not_look_like_missing_key(self):
        raw = ("vless://11111111-1111-1111-1111-111111111111@185.236.25.152:443"
               "?sid=aa&pbk=" + "A" * 43 + "&sni=example.com&security=reality"
               "&flow=xtls-rprx-vision&type=tcp&fp=chrome")
        node = ("vless://11111111-1111-1111-1111-111111111111@185.236.25.152:443"
                "?security=reality&type=raw&sni=example.com&fp=chrome&pbk=" + "A" * 43
                + "&sid=aa&flow=xtls-rprx-vision")
        displayed = canonical_client_link(raw).split("#", 1)[0]
        self.assertEqual(hashlib.sha256(displayed.encode()).hexdigest(),
                         hashlib.sha256(canonical_client_link(node).split("#", 1)[0].encode()).hexdigest())
        self.assertNotEqual(raw, displayed)

    def test_report_accepts_bounded_stage_codes(self):
        report = ProbeReport(test_id="a" * 24, profile_sha256="b" * 64,
                             windows="10.0.26100", steps=[{"stage": "vpn_dns_853",
                             "code": "DNS853_TIMEOUT", "elapsed_ms": 7000}])
        self.assertEqual(safe_steps(report)[0]["code"], "DNS853_TIMEOUT")

    def test_unexpected_client_failure_can_be_uploaded_without_raw_error(self):
        report = ProbeReport(test_id="a" * 24, profile_sha256="b" * 64,
                             windows="10.0.26100", steps=[{"stage": "client_runtime",
                             "code": "CLIENT_ERROR", "elapsed_ms": 0}])
        self.assertEqual(safe_steps(report)[0]["code"], "CLIENT_ERROR")
        self.assertIn("прервалась", explain(safe_steps(report), {"status": "pending"}, True))

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
        self.assertNotIn("-OutFile $zipPath -PassThru", script)
        self.assertNotIn("Set-ItemProperty -Path 'HKCU", script)
        self.assertIn('REPORT STORED ON SERVER', script)
        self.assertIn('Local report:', script)
        self.assertIn("--noproxy '0.0.0.0' --socks5-hostname", script)
        self.assertIn("Add-Step 'vpn_ipv4'", script)
        self.assertIn("Add-Step 'socks_connect'", script)
        self.assertIn("Add-Step 'fallback_ipv4'", script)
        self.assertIn("Add-Step 'fallback_site'", script)
        self.assertIn("path='/vpn-tunnel'", script)
        self.assertIn("xray.stdout.log", script)

    def test_download_is_a_single_double_clickable_cmd_with_embedded_probe(self):
        self.assertTrue(CMD_LAUNCHER.startswith('@echo off\r\n'))
        self.assertIn('powershell.exe -NoProfile -ExecutionPolicy Bypass -Command', CMD_LAUNCHER)
        self.assertIn('ReadAllText($env:PL_PROBE_FILE,[Text.Encoding]::UTF8)', CMD_LAUNCHER)
        self.assertIn('$text.LastIndexOf($marker)', CMD_LAUNCHER)
        self.assertTrue(CMD_LAUNCHER.endswith(':__PROBE_POWERSHELL__\r\n'))
        combined = CMD_LAUNCHER + 'param()\nWrite-Host "probe"\n'
        marker = ':__PROBE_POWERSHELL__'
        self.assertEqual(combined[combined.rfind(marker) + len(marker):].strip(),
                         'param()\nWrite-Host "probe"')

    def test_dns_failure_after_working_https_identifies_client_dns_path(self):
        steps = [{"stage": "vpn_site", "code": "OK"},
                 {"stage": "vpn_dns_853", "code": "DNS853_TIMEOUT"}]
        summary = explain(steps, {"status": "observed", "listener": True,
                                  "key_on_node": True}, True)
        self.assertIn("8.8.8.8:853", summary)

    def test_accepted_https_fallback_overrides_direct_reality_rejection(self):
        steps = [{"stage": "vpn_ipv4", "code": "HTTPS_TIMEOUT"},
                 {"stage": "fallback_ipv4", "code": "OK"},
                 {"stage": "fallback_site", "code": "OK"}]
        summary = explain(steps, {"status": "observed", "key_on_node": True,
                                  "reality_rejected": 6}, True)
        self.assertIn("Резервный HTTPS-маршрут", summary)
        self.assertIn("работает", summary)

    def test_ip_probe_distinguishes_site_failure_from_tunnel_failure(self):
        site_only = [{"stage": "vpn_ipv4", "code": "OK"},
                     {"stage": "vpn_site", "code": "HTTPS_TIMEOUT"}]
        both = [{"stage": "vpn_ipv4", "code": "HTTPS_TIMEOUT"},
                {"stage": "vpn_site", "code": "HTTPS_TIMEOUT"}]
        self.assertIn("выбранный сайт", explain(site_only, {"status": "pending"}, True))
        self.assertIn("ни HTTPS до IP", explain(both, {"status": "pending"}, True))
        old_report = [{"stage": "vpn_site", "code": "HTTPS_TIMEOUT"}]
        self.assertNotIn("выбранный сайт", explain(old_report, {"status": "pending"}, True))

    def test_socks_connect_timeout_is_not_reported_as_working_vpn(self):
        steps = [{"stage": "socks", "code": "OK"},
                 {"stage": "socks_connect", "code": "SOCKS_CONNECT_TIMEOUT"},
                 {"stage": "vpn_ipv4", "code": "HTTPS_TIMEOUT"}]
        summary = explain(steps, {"status": "observed", "key_on_node": True}, True)
        self.assertIn("не смог установить соединение", summary)

    def test_node_key_mismatch_takes_priority_over_client_errors(self):
        steps = [{"stage": "node_tcp", "code": "TCP_TIMEOUT"}]
        summary = explain(steps, {"status": "observed", "listener": True,
                                  "key_on_node": False}, True)
        self.assertIn("отличается на VPN-ноде", summary)

    def test_node_trace_separates_rejected_reality_from_accepted_key(self):
        lines = ("2026/10/16 [Info] transport/internet/tcp: REALITY: processed invalid connection "
                 "from 8.8.4.4:50210: failed to read client hello\n"
                 "2026/10/16 [Info] transport/internet/tcp: REALITY: processed invalid connection "
                 "from 8.8.4.4:50213: authentication failed\n"
                 "2026/10/16 from 8.8.4.4:50211 accepted tcp:1.1.1.1:443 "
                 "[vless-reality >> direct] email: u1s3\n"
                 "2026/10/16 from 8.8.4.4:50212 accepted tcp:1.1.1.1:443 "
                 "[vless-reality >> direct] email: other\n")
        with patch.object(node, "run", return_value=SimpleNamespace(returncode=0, stdout=lines)), \
             patch.object(node.time, "time", return_value=1_800_000_000):
            trace = node.trace_client("8.8.4.4", 1_799_999_900, "u1s3")
        self.assertEqual(trace["reality_rejected"], 1)
        self.assertEqual(trace["rejection_reasons"]["hello_incomplete"], 0)
        self.assertEqual(trace["rejection_reasons"]["authentication_failed"], 1)
        self.assertEqual(trace["vless_accepted"], 1)
        diagnosis = explain([{"stage": "vpn_ipv4", "code": "HTTPS_TIMEOUT"}],
                            {"status": "observed", "key_on_node": True, **trace}, True)
        self.assertIn("нода приняла", diagnosis)

    def test_reality_trace_does_not_hide_auth_failure_without_bare_tcp(self):
        lines = ("2026/10/16 REALITY: processed invalid connection from "
                 "8.8.4.4:50111: authentication failed or validation criteria not met\n")
        with patch.object(node, "run", return_value=SimpleNamespace(returncode=0, stdout=lines)), \
             patch.object(node.time, "time", return_value=1_800_000_000):
            trace = node.trace_client("8.8.4.4", 1_799_999_900, "u1s3")
        self.assertEqual(trace["reality_rejected"], 1)
        self.assertEqual(trace["rejection_reasons"]["authentication_failed"], 1)

    def test_unchanged_node_state_does_not_restart_active_service(self):
        state = {"host": "185.236.25.152", "port": 443, "sni": "dl.google.com",
                 "target": "dl.google.com:443", "private_key": "A" * 43,
                 "public_key": "B" * 43, "short_id": "12345678",
                 "users": {"u1s3": "11111111-1111-4111-8111-111111111111"}}
        config = __import__("json").dumps(node.render(state), indent=2) + "\n"
        fake_config = SimpleNamespace(is_file=lambda: True, read_text=lambda: config)
        with patch.object(node, "CONFIG", fake_config), \
             patch.object(node, "run", return_value=SimpleNamespace(returncode=0)) as run, \
             patch.object(node, "port_ok", return_value=True), \
             patch.object(node, "atomic", side_effect=AssertionError("unexpected write")):
            node.apply_state(state, dict(state))
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.args[0][:2], ["systemctl", "is-active"])

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
