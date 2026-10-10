"""Safety regressions for Private Line production VPN architecture.

These tests are intentionally local/static: they catch code paths that once
caused live credentials to rotate, control-plane resource exhaustion, or
automatic repair races.
"""
from pathlib import Path
import inspect
import unittest

from app.services import vpn_key
from app.services.vpn_link import canonical_client_link


ROOT = Path("/app")


class ProductionVpnSafetyTests(unittest.TestCase):
    def test_client_profile_name_is_always_private_line_vpn(self):
        source = inspect.getsource(vpn_key.get_client_access_url)
        self.assertIn('label="Private Line VPN"', source)

    def test_key_creation_is_idempotent_not_delete_first(self):
        source = inspect.getsource(vpn_key.create_vpn_key)
        self.assertIn("Reusing active VPN key", source)
        self.assertNotIn("session.delete(old_key)", source)
        self.assertNotIn("delete_key(", source)

    def test_legacy_destructive_reality_helper_is_disabled(self):
        text = (ROOT / "fix_reality.sh").read_text(encoding="utf-8")
        self.assertIn("intentionally disabled", text)
        self.assertNotIn("docker compose down", text)
        self.assertNotIn("PRIVATE_KEY=$(echo", text)

    def test_scheduler_does_not_auto_repair_credentials(self):
        text = (ROOT / "scheduler" / "tasks.py").read_text(encoding="utf-8")
        scheduled = text[text.index("def create_scheduler"):]
        self.assertNotIn("_job_repair_marzban_inbounds,", scheduled)
        self.assertIn("_job_vpn_integrity_audit", scheduled)

    def test_control_plane_has_no_vpn_service(self):
        compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        self.assertNotIn("private-line-primary-node", compose)
        self.assertNotIn("enable_primary_vpn", compose)
        self.assertIn('profiles: ["monitoring"]', compose)

    def test_public_subscription_brand(self):
        route = (ROOT / "admin" / "routes" / "vpn_subscription.py").read_text(encoding="utf-8")
        self.assertIn('"profile-title": "Private Line VPN"', route)
        self.assertIn('VpnServer.provider != "private-line-primary"', route)


if __name__ == "__main__":
    unittest.main()
