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
        self.assertIn("Reusing verified VPN key", source)
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

    def test_new_nodes_are_staged_until_real_smoke(self):
        servers = (ROOT / "admin" / "routes" / "servers.py").read_text(encoding="utf-8")
        smoke = (ROOT / "scripts" / "smoke_vpn_node.sh").read_text(encoding="utf-8")
        self.assertIn("is_selectable=False", servers)
        self.assertIn("app.promote_vpn_node", smoke)
        self.assertIn("--publish", smoke)

    def test_paid_payment_has_durable_fulfillment_recovery(self):
        model = (ROOT / "app" / "models" / "payment.py").read_text(encoding="utf-8")
        payments = (ROOT / "app" / "payments" / "services.py").read_text(encoding="utf-8")
        scheduler = (ROOT / "scheduler" / "tasks.py").read_text(encoding="utf-8")
        self.assertIn("fulfilled_at", model)
        self.assertIn("_fulfill_paid_payment", payments)
        self.assertIn("with_for_update()", payments)
        self.assertIn("recover_paid_unfulfilled_payments", scheduler)

    def test_trials_require_real_vpn_activation(self):
        promo = (ROOT / "app" / "services" / "promo.py").read_text(encoding="utf-8")
        self.assertGreaterEqual(promo.count("activate_subscription("), 2)
        self.assertIn("if key is None:", promo)
        self.assertIn("with_for_update()", promo)

    def test_admin_retry_does_not_locally_disable_live_key(self):
        route = (ROOT / "admin" / "routes" / "subscriptions.py").read_text(encoding="utf-8")
        retry = route[route.index('router.post("/{sub_id}/retry")'):]
        retry = retry[:retry.index('router.post("/{sub_id}/keys/{key_id}/disable")')]
        self.assertNotIn("stale_key.status", retry)
        self.assertIn("reconcile_marzban_server", retry)


    def test_manual_node_add_does_not_double_count_capacity(self):
        text = (ROOT / "app" / "services" / "server_access.py").read_text(encoding="utf-8")
        self.assertNotIn("server.current_users += 1", text)
        self.assertIn("create_vpn_key(", text)

    def test_existing_paid_access_does_not_depend_on_is_selectable(self):
        text = (ROOT / "bot" / "handlers" / "access.py").read_text(encoding="utf-8")
        start = text.index("async def _get_active_subscription")
        end = text.index("@router.message", start)
        block = text[start:end]
        self.assertNotIn("VpnServer.is_selectable.is_(True)", block)
        self.assertIn("VpnServer.status != VpnServerStatus.disabled", block)

    def test_public_subscription_does_not_hide_existing_key_when_admission_paused(self):
        text = (ROOT / "admin" / "routes" / "vpn_subscription.py").read_text(encoding="utf-8")
        self.assertNotIn("VpnServer.is_selectable.is_(True)", text)
        self.assertIn("VpnServer.status != VpnServerStatus.disabled", text)

    def test_node_smoke_publish_mode_is_initialized(self):
        text = (ROOT / "scripts" / "smoke_vpn_node.sh").read_text(encoding="utf-8")
        self.assertIn('MODE="${2:-}"', text)
        self.assertIn('[[ -z "$MODE" || "$MODE" == "--publish" ]]', text)

    def test_subscription_probe_joins_key_before_subscription(self):
        text = (ROOT / "app" / "subscription_probe_targets.py").read_text(encoding="utf-8")
        first = text.index(".join(VpnKey")
        second = text.index(".join(Subscription")
        self.assertLess(first, second)

    def test_renewal_verifies_vpn_before_extending_expiry(self):
        text = (ROOT / "app" / "payments" / "services.py").read_text(encoding="utf-8")
        start = text.index("if subscription.status in (SubscriptionStatus.active")
        block = text[start:start + 2200]
        ensure_pos = block.index("ensure_subscription_vpn_key")
        extend_pos = block.index("extend_subscription")
        self.assertLess(ensure_pos, extend_pos)

    def test_legacy_scheduler_repair_hook_is_non_mutating(self):
        text = (ROOT / "scheduler" / "tasks.py").read_text(encoding="utf-8")
        start = text.index("async def _job_repair_marzban_inbounds")
        end = text.index("async def _job_vpn_integrity_audit", start)
        block = text[start:end]
        self.assertNotIn("repair_marzban_keys", block)
        self.assertIn("is disabled", block)


    def test_admin_vpn_key_disable_does_not_adjust_capacity_twice(self):
        text = (ROOT / "admin" / "routes" / "vpn_keys.py").read_text(encoding="utf-8")
        start = text.index('router.post("/{key_id}/disable")')
        end = text.index('router.post("/{key_id}/reissue")', start)
        block = text[start:end]
        self.assertNotIn("current_users", block)
        self.assertIn("disable_vpn_key", block)

    def test_server_migration_has_no_manual_capacity_mutation(self):
        text = (ROOT / "app" / "services" / "server_migration.py").read_text(encoding="utf-8")
        self.assertNotIn("current_users +=", text)
        self.assertNotIn("current_users = max", text)
        self.assertIn("if not disabled:", text)

    def test_legacy_update_script_delegates_to_production_gate(self):
        text = (ROOT / "scripts" / "update_vpn_stability.sh").read_text(encoding="utf-8")
        self.assertIn("production_vpn_gate.sh", text)
        self.assertNotIn("app.stabilize_vless", text)

    def test_compose_never_creates_missing_bind_sources(self):
        compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        self.assertIn("create_host_path: false", compose)
        self.assertNotIn("POSTGRES_PASSWORD:-changeme", compose)

    def test_release_has_atomic_backup_rollback_guards(self):
        source = (ROOT / "release.sh").read_text(encoding="utf-8")
        self.assertIn("fail() {", source)
        self.assertIn("mandatory pre-release database backup", source)
        self.assertIn("rollback-plan", source)
        self.assertIn("MIGRATION_TOUCHED=1", source)
        self.assertIn("restore_postgres_backup.sh", source)

    def test_deploys_validate_checkout_layout_before_docker(self):
        for name in ("deploy.sh", "release.sh", "scripts/deploy_low_memory.sh"):
            text = (ROOT / name).read_text(encoding="utf-8")
            self.assertIn("validate_install_layout.sh", text)

    def test_new_nodes_avoid_known_bad_microsoft_reality_target(self):
        installer = (ROOT / "install_node.sh").read_text(encoding="utf-8")
        inspect = (ROOT / "scripts" / "inspect_node_vless.sh").read_text(encoding="utf-8")
        self.assertIn('REALITY_TARGET_HOST="${REALITY_TARGET_HOST:-dl.google.com}"', installer)
        self.assertNotIn('REALITY_SNI="www.microsoft.com"', installer)
        self.assertNotIn('openssl s_client -connect www.microsoft.com:443', inspect)

    def test_reality_target_migration_preserves_credentials(self):
        text = (ROOT / "scripts" / "migrate_reality_target.sh").read_text(encoding="utf-8")
        self.assertIn("preserving credentials", text)
        self.assertNotIn("x25519", text)
        self.assertIn('r.get("privateKey")', text)
        self.assertIn('r.get("shortIds")', text)

    def test_marzban_node_installer_migrates_env_admin_to_db(self):
        installer = (ROOT / "install_node.sh").read_text(encoding="utf-8")
        self.assertIn("marzban-cli admin import-from-env -y", installer)
        self.assertIn("SUDO_USERNAME/SUDO_PASSWORD", installer)

    def test_one_command_updater_rolls_nodes_from_control_plane(self):
        top = (ROOT / "update.sh").read_text(encoding="utf-8")
        orchestrator = (ROOT / "scripts" / "update_production.sh").read_text(encoding="utf-8")
        deploy = (ROOT / "scripts" / "deploy_low_memory.sh").read_text(encoding="utf-8")
        self.assertIn("git pull --ff-only", top)
        self.assertIn("update_production.sh", top)
        self.assertIn(".node_deploy_commit", orchestrator)
        self.assertIn("app.node_ssh_access", orchestrator)
        self.assertNotIn('"root@$NODE_HOST"', orchestrator)
        self.assertNotIn('bash "$tmp/scripts/migrate_marzban_env_admin.sh"', orchestrator)
        self.assertIn("update_node_xray.sh", orchestrator)
        self.assertIn("migrate_reality_target.sh", orchestrator)
        self.assertIn("smoke_vpn_node.sh", orchestrator)
        self.assertIn("probe_vless_e2e.sh", orchestrator)
        self.assertIn("docker compose build admin", deploy)

    def test_xray_updater_checks_live_runtime_before_pull(self):
        source = (ROOT / "scripts" / "update_node_xray.sh").read_text(encoding="utf-8")
        self.assertIn('version_matches() {', source)
        self.assertIn('with socket.create_connection(("127.0.0.1", 443)', source)
        self.assertIn("no pull required", source)
        self.assertIn('version_matches "$WORKDIR/xray"', source)
        self.assertNotIn('version | head -n 1 | grep', source)

    def test_marzban_create_key_has_live_return_path(self):
        source = (ROOT / "app" / "vpn" / "marzban.py").read_text(encoding="utf-8")
        self.assertIn("return MarzbanKey(", source)
        self.assertNotIn('background task and may swallow\\n', source)
        self.assertNotIn('restart_core_with_db_users(reason=f"provision {safe_name}")\\n', source)

    def test_marzban_customer_link_is_built_from_authoritative_reality_state(self):
        source = (ROOT / "app" / "vpn" / "marzban.py").read_text(encoding="utf-8")
        self.assertIn("Expected exactly one VLESS Reality inbound", source)
        self.assertIn("authoritative_base =", source)
        self.assertIn("self._inject_reality(", source)
        self.assertIn("Marzban emitted no matching VLESS share URI", source)
        self.assertIn("after core restart", source)

    def test_node_reality_metadata_is_self_contained(self):
        installer = (ROOT / "install_node.sh").read_text(encoding="utf-8")
        updater = (ROOT / "scripts" / "update_node_xray.sh").read_text(encoding="utf-8")
        self.assertIn('PUBLIC_KEY_VAL="$PUBLIC_KEY"', installer)
        self.assertIn('"publicKey": os.environ["PUBLIC_KEY_VAL"]', installer)
        self.assertIn('"target": os.environ["REALITY_DEST_VAL"]', installer)
        self.assertIn("reality['publicKey'] = public_key", updater)
        self.assertIn("derived != public_key", updater)

    def test_production_updater_uses_encrypted_node_ssh_access(self):
        text = (ROOT / "scripts" / "update_production.sh").read_text(encoding="utf-8")
        self.assertIn("app.node_ssh_access", text)
        self.assertIn("SSH_ASKPASS_REQUIRE=force", text)
        self.assertIn("StrictHostKeyChecking=accept-new", text)
        self.assertNotIn('"root@$NODE_HOST"', text)

    def test_direct_ipv4_repair_script_is_retired(self):
        text = (ROOT / "scripts" / "repair_registered_node_ipv4.sh").read_text(encoding="utf-8")
        self.assertIn("retired", text.lower())
        self.assertNotIn("UPDATE vpn_servers", text)
        self.assertNotIn("/api/hosts", text.split("EOF")[0])

    def test_public_subscription_brand(self):
        route = (ROOT / "admin" / "routes" / "vpn_subscription.py").read_text(encoding="utf-8")
        self.assertIn('"profile-title": "Private Line VPN"', route)
        self.assertIn('VpnServer.provider != "private-line-primary"', route)


if __name__ == "__main__":
    unittest.main()
