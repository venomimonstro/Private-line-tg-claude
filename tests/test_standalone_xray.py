import inspect
from pathlib import Path
import unittest

from app.services import vpn_key

ROOT=Path("/app")

class StandaloneXrayTests(unittest.TestCase):
    def test_paid_provisioning_requires_direct_xray(self):
        source=inspect.getsource(vpn_key.create_vpn_key)
        self.assertIn('_server_protocol(server) != "direct_xray"', source)
        self.assertIn("DirectXrayClient", source)
        self.assertNotIn("Expected exactly one VLESS Reality inbound", source)

    def test_node_runtime_has_no_marzban_management_port(self):
        installer=(ROOT/"scripts/install_direct_xray_node.sh").read_text()
        manager=(ROOT/"scripts/direct_xray_node.py").read_text()
        self.assertIn("docker compose down --remove-orphans", installer)
        self.assertIn("ufw delete allow 2096/tcp", installer)
        self.assertNotIn("2096", manager)
        self.assertIn('"port":443', manager.replace(" ", ""))

    def test_node_mutation_is_atomic_test_restart_rollback(self):
        manager=(ROOT/"scripts/direct_xray_node.py").read_text()
        self.assertIn('"run","-test","-config"', manager.replace(" ", ""))
        self.assertIn('systemctl","restart",SERVICE', manager.replace(" ", ""))
        self.assertIn("old_cfg", manager)
        self.assertIn("old_state", manager)
        self.assertIn("XRAY_LISTENER_NOT_READY", manager)

    def test_rollout_has_one_direct_path_and_real_tunnel_gate(self):
        source=(ROOT/"scripts/update_production.sh").read_text()
        start=source.index("=== DISCOVER DEDICATED VPN NODES ===")
        block=source[start:]
        self.assertIn("install_direct_xray_node.sh", block)
        self.assertIn("app.direct_xray_migrate", block)
        self.assertIn("app.direct_xray_canary", block)
        self.assertIn('probe_vless_e2e.sh --stdin-url "$SERVER_ID"', block)
        self.assertIn('probe_vless_e2e.sh "$SUB_ID" "$SERVER_ID"', block)
        self.assertNotIn("app.stabilize_vless", block)
        self.assertNotIn("smoke_vpn_node.sh", block)
        self.assertNotIn("prepare_marzban_runtime.sh", block)

    def test_health_watch_is_read_only(self):
        source=(ROOT/"scripts/vpn_health_watch.sh").read_text()
        self.assertIn('_server_protocol(server)!="direct_xray"', source)
        self.assertIn("Read-only paid VLESS/REALITY HTTPS E2E", source)
        self.assertNotIn("direct_xray_canary", source)
        self.assertNotIn("smoke_vpn_node.sh", source)

    def test_control_plane_uses_existing_encrypted_ssh(self):
        client=(ROOT/"app/vpn/direct_xray.py").read_text()
        self.assertIn("get_node_ssh_access", client)
        self.assertIn("StrictHostKeyChecking=accept-new", client)
        self.assertIn("/opt/private-line-node/manage.py", client)

if __name__=="__main__":
    unittest.main()
