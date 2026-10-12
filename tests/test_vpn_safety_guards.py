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
        self.assertIn('"--hot-add"', text)

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

    def test_legacy_reality_target_migration_delegates_to_golden_converger(self):
        text = (ROOT / "scripts" / "migrate_reality_target.sh").read_text(encoding="utf-8")
        self.assertIn("converge_vpn_node.sh", text)
        self.assertIn("REALITY_TARGET_HOST", text)
        self.assertNotIn("docker compose restart", text)
        self.assertNotIn("privateKey", text)

    def test_server_reality_uses_xray_26627_target_schema(self):
        installer = (ROOT / "install_node.sh").read_text(encoding="utf-8")
        golden = (ROOT / "scripts" / "converge_vpn_node.sh").read_text(encoding="utf-8")
        self.assertIn('"target": os.environ["REALITY_DEST_VAL"]', installer)
        self.assertIn('"target": target', golden)
        self.assertNotIn('"dest": os.environ["REALITY_DEST_VAL"]', installer)
        self.assertNotIn('"dest": target', golden)

    def test_golden_converger_validates_reality_target_every_run(self):
        text = (ROOT / "scripts" / "converge_vpn_node.sh").read_text(encoding="utf-8")
        self.assertIn("-tls1_3 -alpn h2", text)
        self.assertIn("Verify REALITY target TLS 1.3 + HTTP/2", text)

    def test_disposable_e2e_uses_multiple_independent_https_destinations(self):
        text = (ROOT / "scripts" / "smoke_vpn_node.sh").read_text(encoding="utf-8")
        self.assertIn('probe "ipify"', text)
        self.assertIn('probe "cloudflare"', text)
        self.assertIn('probe "google"', text)
        self.assertIn("SUCCESS < 2", text)
        self.assertIn("Checking public VPN endpoint TCP reachability", text)

    def test_paid_e2e_separates_public_tcp_from_reality_tunnel(self):
        text = (ROOT / "scripts" / "probe_vless_e2e.sh").read_text(encoding="utf-8")
        tcp = text.index("Checking public VPN endpoint TCP reachability")
        xray = text.index("Starting ephemeral Xray client")
        self.assertLess(tcp, xray)
        self.assertIn("VLESS/REALITY tunnel", text)

    def test_marzban_node_installer_keeps_simple_bootstrap_admin(self):
        installer = (ROOT / "install_node.sh").read_text(encoding="utf-8")
        self.assertIn("SUDO_USERNAME=", installer)
        self.assertIn("SUDO_PASSWORD=", installer)
        self.assertNotIn("marzban-cli admin import-from-env -y", installer)
        self.assertIn("chmod 600", installer)

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
        self.assertIn("converge_vpn_node.sh", orchestrator)
        self.assertNotIn('bash "$tmp/scripts/update_node_xray.sh"', orchestrator)
        self.assertNotIn('bash "$tmp/scripts/migrate_reality_target.sh"', orchestrator)
        self.assertIn("smoke_vpn_node.sh", orchestrator)
        self.assertIn("probe_vless_e2e.sh", orchestrator)
        self.assertIn("docker compose build --build-arg", deploy)

    def test_operator_git_pull_cannot_leave_old_admin_image_running(self):
        updater = (ROOT / "scripts" / "update_production.sh").read_text(encoding="utf-8")
        deploy = (ROOT / "scripts" / "deploy_low_memory.sh").read_text(encoding="utf-8")
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn('"$IMAGE_REVISION" != "$HEAD"', updater)
        self.assertIn('"$ADMIN_IMAGE_ID" != "$CURRENT_IMAGE_ID"', updater)
        self.assertIn('"$BOT_IMAGE_ID" != "$CURRENT_IMAGE_ID"', updater)
        self.assertIn('"$LIVE_REVISION" == "$HEAD"', updater)
        self.assertIn('--build-arg "PRIVATE_LINE_REVISION=$SOURCE_REVISION"', deploy)
        self.assertIn('LABEL org.opencontainers.image.revision=', dockerfile)

    def test_legacy_xray_updater_delegates_to_golden_converger(self):
        source = (ROOT / "scripts" / "update_node_xray.sh").read_text(encoding="utf-8")
        self.assertIn("converge_vpn_node.sh", source)
        self.assertNotIn("docker pull", source)
        self.assertNotIn("xray.next", source)

    def test_server_and_probe_client_share_pinned_xray_26627_baseline(self):
        installer = (ROOT / "install_node.sh").read_text(encoding="utf-8")
        golden = (ROOT / "scripts" / "converge_vpn_node.sh").read_text(encoding="utf-8")
        runtime = (ROOT / "scripts" / "prepare_marzban_runtime.sh").read_text(encoding="utf-8")
        smoke = (ROOT / "scripts" / "smoke_vpn_node.sh").read_text(encoding="utf-8")
        paid = (ROOT / "scripts" / "probe_vless_e2e.sh").read_text(encoding="utf-8")
        self.assertIn("XRAY_EXECUTABLE_PATH=/var/lib/marzban/xray-core/26.6.27/xray", installer)
        self.assertIn("XRAY_VERSION=26.6.27", golden)
        self.assertIn("XRAY_VERSION=26.6.27", runtime)
        self.assertIn("xray-core:26.6.27@", runtime)
        self.assertIn("xray-core:26.6.27@", smoke)
        self.assertIn("xray-core:26.6.27@", paid)

    def test_golden_node_is_single_standard_reality_path(self):
        source = (ROOT / "scripts" / "converge_vpn_node.sh").read_text(encoding="utf-8")
        self.assertIn("exactly one VLESS + TCP + REALITY inbound", source)
        self.assertIn('"network": "tcp"', source)
        self.assertIn('"security": "reality"', source)
        self.assertIn('"minClientVer": ""', source)
        self.assertIn('"port"] = 443', source)
        self.assertIn("credentials preserved", source)
        self.assertNotIn("os.urandom", source)

    def test_install_fetches_runtime_preparer_over_pinned_control_plane(self):
        installer = (ROOT / "install_node.sh").read_text(encoding="utf-8")
        route = (ROOT / "admin" / "routes" / "servers.py").read_text(encoding="utf-8")
        self.assertIn("/admin/api/marzban-runtime-script", installer)
        self.assertIn("--pinnedpubkey", installer)
        self.assertIn("X-Node-Token", installer)
        self.assertIn('@api_router.get("/marzban-runtime-script"', route)
        self.assertIn("secrets.compare_digest", route)

    def test_installer_derives_domain_control_plane_ip_from_pinned_peer(self):
        installer = (ROOT / "install_node.sh").read_text(encoding="utf-8")
        self.assertIn("-w '%{remote_ip}'", installer)
        self.assertIn("--pinnedpubkey", installer)
        self.assertIn("Не удалось безопасно определить публичный IPv4 control-plane", installer)

    def test_node_management_has_no_socat_dependency(self):
        installer = (ROOT / "install_node.sh").read_text(encoding="utf-8")
        converger = (ROOT / "scripts" / "converge_vpn_node.sh").read_text(encoding="utf-8")
        self.assertNotIn("apt-get install -y -qq socat", installer)
        self.assertNotIn("ExecStart=/usr/bin/socat", installer)
        self.assertIn("UVICORN_PORT=", installer)
        self.assertIn("UVICORN_PORT", converger)
        self.assertIn("systemctl disable --now marzban-proxy.service", converger)

    def test_marzban_api_is_firewall_restricted_before_startup(self):
        installer = (ROOT / "install_node.sh").read_text(encoding="utf-8")
        allow = installer.index('ufw allow from "$MAIN_SERVER_IP"')
        start = installer.index('cd "$MARZBAN_DIR" && docker compose up -d')
        self.assertLess(allow, start)
        self.assertIn("UVICORN_HOST=0.0.0.0", installer)

    def test_golden_converger_repairs_public_443_firewall(self):
        source = (ROOT / "scripts" / "converge_vpn_node.sh").read_text(encoding="utf-8")
        self.assertIn('ufw allow 443/tcp comment "VLESS Reality VPN"', source)
        self.assertIn("UFW does not allow public TCP/443", source)

    def test_one_command_update_always_converges_node_drift(self):
        source = (ROOT / "scripts" / "update_production.sh").read_text(encoding="utf-8")
        self.assertIn("running idempotent node convergence anyway", source)
        self.assertIn("NODE_UPDATE=true", source)
        self.assertIn("converge_vpn_node.sh", source)
        self.assertNotIn('bash "$tmp/scripts/update_node_xray.sh"', source)
        self.assertNotIn('bash "$tmp/scripts/migrate_reality_target.sh"', source)
        self.assertIn("runtime/firewall drift", source)

    def test_ssh_management_failure_does_not_quarantine_data_plane(self):
        source = (ROOT / "scripts" / "update_production.sh").read_text(encoding="utf-8")
        self.assertIn("VPN admission state was not changed because SSH is not a data-plane proof", source)
        ssh_area = source[source.index('if [[ "$SSH_AUTH" == "private_key" ]]'):source.index('echo "[2/5]')]
        self.assertNotIn('quarantine_node "$SERVER_ID" "remote node convergence failed"', ssh_area)

    def test_subscription_activation_uses_tariff_multi_node_redundancy(self):
        source = (ROOT / "app" / "services" / "subscription.py").read_text(encoding="utf-8")
        start = source.index("async def activate_subscription")
        end = source.index("async def expire_subscriptions", start)
        block = source[start:end]
        self.assertIn("desired_keys", block)
        self.assertIn("tariff.max_keys", block)
        self.assertIn("len(created_keys) >= desired_keys", block)
        self.assertIn("Основной", block)
        self.assertIn("Резерв", block)
        self.assertNotIn("created_keys.append(key)\n            break", block)

    def test_node_lifecycle_separates_staging_quarantine_and_recovery(self):
        servers = (ROOT / "admin" / "routes" / "servers.py").read_text(encoding="utf-8")
        watch = (ROOT / "scripts" / "vpn_health_watch.sh").read_text(encoding="utf-8")
        state = (ROOT / "app" / "node_probe_state.py").read_text(encoding="utf-8")
        self.assertIn("server.status = VpnServerStatus.maintenance", servers)
        self.assertIn("status=VpnServerStatus.maintenance", servers)
        self.assertIn("VpnServerStatus.unreachable", watch)
        self.assertNotIn("VpnServerStatus.maintenance,", watch)
        self.assertIn("server.status = VpnServerStatus.active", state)
        self.assertIn("server.status = VpnServerStatus.unreachable", state)

    def test_hot_add_gate_proves_hot_remove_and_uses_unique_docker_name(self):
        smoke = (ROOT / "scripts" / "smoke_vpn_node.sh").read_text(encoding="utf-8")
        helper = (ROOT / "app" / "node_smoke_user.py").read_text(encoding="utf-8")
        self.assertIn('CONTAINER="private-line-node-smoke-${SERVER_ID}-${RUN_ID}"', smoke)
        self.assertIn('s.bind(("127.0.0.1", 0))', smoke)
        self.assertIn("delete-live", smoke)
        self.assertIn("hot-remove reached live Xray WITHOUT restart", smoke)
        self.assertIn('restart_core=(action == "delete")', helper)
        self.assertIn("for _ in $(seq 1 10)", smoke)

    def test_paid_probe_uses_random_local_port_and_container_identity(self):
        paid = (ROOT / "scripts" / "probe_vless_e2e.sh").read_text(encoding="utf-8")
        self.assertIn('s.bind(("127.0.0.1", 0))', paid)
        self.assertIn('PROBE_NAME="private-line-vless-probe-${SERVER_ID:-any}-${RUN_ID}"', paid)
        self.assertIn("server-scoped acceptance is non-interactive", paid)

    def test_paid_provisioning_requires_recent_one_minute_watchdog_proof(self):
        key_service = (ROOT / "app" / "services" / "vpn_key.py").read_text(encoding="utf-8")
        subscriptions = (ROOT / "app" / "services" / "subscription.py").read_text(encoding="utf-8")
        promote = (ROOT / "app" / "promote_vpn_node.py").read_text(encoding="utf-8")
        timer = (ROOT / "scripts" / "install_vpn_health_timer.sh").read_text(encoding="utf-8")
        self.assertIn("timedelta(minutes=3)", key_service)
        self.assertIn("timedelta(minutes=3)", subscriptions)
        self.assertIn("timedelta(minutes=3)", promote)
        self.assertIn("OnUnitActiveSec=1min", timer)

    def test_vpn_health_watch_is_real_e2e_and_non_mutating_after_probe_exists(self):
        watch = (ROOT / "scripts" / "vpn_health_watch.sh").read_text(encoding="utf-8")
        timer = (ROOT / "scripts" / "install_vpn_health_timer.sh").read_text(encoding="utf-8")
        self.assertIn("smoke_vpn_node.sh", watch)
        self.assertIn("app.node_probe_state", watch)
        self.assertNotIn("stabilize_vpn_fleet", watch)
        self.assertNotIn("restart_core", watch)
        self.assertIn("OnUnitActiveSec=1min", timer)

    def test_runtime_pins_marzban_and_xray_and_patches_silent_hot_add_failure(self):
        installer = (ROOT / "install_node.sh").read_text(encoding="utf-8")
        golden = (ROOT / "scripts" / "converge_vpn_node.sh").read_text(encoding="utf-8")
        runtime = (ROOT / "scripts" / "prepare_marzban_runtime.sh").read_text(encoding="utf-8")
        self.assertIn("marzban:v0.8.4@sha256:", installer)
        self.assertIn("EXPECTED_MARZBAN", golden)
        self.assertIn("XRAY_VERSION=26.6.27", golden)
        self.assertIn("PRIVATE_LINE_XRAY_MUTATION_SYNC_V2", runtime)
        self.assertIn("do not return HTTP success before live Xray accepts user", runtime)
        self.assertIn("/code/app/xray/operations.py:ro", runtime)
        self.assertIn("/code/app/routers/user.py:ro", runtime)
        self.assertIn("docker-compose.override.yml", runtime)

    def test_golden_converger_requires_both_sync_overrides_after_recreate(self):
        golden = (ROOT / "scripts" / "converge_vpn_node.sh").read_text(encoding="utf-8")
        self.assertIn('/code/app/xray/operations.py', golden)
        self.assertIn('/code/app/routers/user.py', golden)
        self.assertIn("synchronous Xray operations override is not active", golden)
        self.assertIn("synchronous user API override is not active", golden)

    def test_marzban_user_api_waits_for_live_xray_mutation(self):
        runtime = (ROOT / "scripts" / "prepare_marzban_runtime.sh").read_text(encoding="utf-8")
        self.assertIn("PRIVATE_LINE_XRAY_MUTATION_SYNC_V2", runtime)
        self.assertIn("xray.operations.add_user(dbuser=dbuser)", runtime)
        self.assertIn("xray.operations.update_user(dbuser=dbuser)", runtime)
        self.assertGreaterEqual(
            runtime.count("xray.operations.remove_user(dbuser=dbuser)"), 2
        )
        self.assertIn("deletion is acknowledged only after live Xray accepted hot-remove", runtime)
        self.assertIn("delays = (0.10, 0.35, 0.80)", runtime)
        self.assertIn("timeout=3", runtime)
        self.assertNotIn("timeout=30", runtime)

    def test_disable_uses_recent_hot_remove_proof_before_any_core_restart(self):
        source = inspect.getsource(vpn_key.disable_vpn_key)
        self.assertIn("hot_remove_proven", source)
        self.assertIn("last_hot_add_success_at", source)
        self.assertIn("timedelta(minutes=45)", source)
        self.assertIn("restart_core=not hot_remove_proven", source)

    def test_health_watch_control_flow_is_not_corrupted(self):
        source = (ROOT / "scripts" / "vpn_health_watch.sh").read_text(encoding="utf-8")
        self.assertIn("while IFS=$'\\t' read -r SERVER_ID SERVER_STATUS; do", source)
        self.assertIn("HOT_ADD_INTERVAL=1800", source)
        self.assertIn("--hot-add-success", source)
        self.assertNotIn("while IFS=\nif (( FAILED", source)

    def test_paid_provisioning_has_no_per_user_xray_restart(self):
        source = inspect.getsource(vpn_key.create_vpn_key)
        self.assertIn("restart_core=False", source)

    def test_node_admission_requires_fresh_user_hot_add_without_restart(self):
        updater = (ROOT / "scripts" / "update_production.sh").read_text(encoding="utf-8")
        gate = (ROOT / "scripts" / "production_vpn_gate.sh").read_text(encoding="utf-8")
        helper = (ROOT / "app" / "node_smoke_user.py").read_text(encoding="utf-8")
        self.assertIn('smoke_vpn_node.sh "$SERVER_ID" --hot-add', updater)
        self.assertIn('smoke_vpn_node.sh "$server_id" --hot-add', gate)
        self.assertIn("create-live", helper)
        self.assertIn("restart_core=(action == \"create\")", helper)

    def test_synthetic_smoke_user_is_persistent_and_read_only_after_first_create(self):
        smoke = (ROOT / "scripts" / "smoke_vpn_node.sh").read_text(encoding="utf-8")
        helper = (ROOT / "app" / "node_smoke_user.py").read_text(encoding="utf-8")
        marzban = (ROOT / "app" / "vpn" / "marzban.py").read_text(encoding="utf-8")
        self.assertIn('NAME="plprobe_s${SERVER_ID}"', smoke)
        self.assertIn("app.node_smoke_user ensure", smoke)
        self.assertIn("app.node_smoke_user delete-live", smoke)
        self.assertIn('if action == "ensure"', helper)
        self.assertIn("client.get_key(name)", helper)
        get_key = marzban[marzban.index("async def get_key"):marzban.index("# ─── Repair", marzban.index("async def get_key"))]
        self.assertNotIn("restart_core_with_db_users", get_key)
        self.assertNotIn(".put(", get_key)
        self.assertNotIn(".delete(", get_key)

    def test_reconcile_restarts_core_only_for_real_user_mutation(self):
        source = (ROOT / "app" / "services" / "vpn_reconcile.py").read_text(encoding="utf-8")
        self.assertIn("if apply and remote_changed:", source)
        self.assertNotIn("always rebuild the live core on --apply", source)

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
        self.assertIn("fresh-user E2E", source)

    def test_node_reality_metadata_is_self_contained(self):
        installer = (ROOT / "install_node.sh").read_text(encoding="utf-8")
        golden = (ROOT / "scripts" / "converge_vpn_node.sh").read_text(encoding="utf-8")
        self.assertIn('PUBLIC_KEY_VAL="$PUBLIC_KEY"', installer)
        self.assertIn('"publicKey": os.environ["PUBLIC_KEY_VAL"]', installer)
        self.assertIn('"target": os.environ["REALITY_DEST_VAL"]', installer)
        self.assertIn('"publicKey": public_key', golden)
        self.assertIn("PUBLIC_KEY=", golden)

    def test_production_updater_uses_encrypted_node_ssh_access(self):
        text = (ROOT / "scripts" / "update_production.sh").read_text(encoding="utf-8")
        self.assertIn("app.node_ssh_access", text)
        self.assertIn("SSH_ASKPASS_REQUIRE=force", text)
        self.assertIn("StrictHostKeyChecking=accept-new", text)
        self.assertNotIn('"root@$NODE_HOST"', text)

    def test_operator_gate_can_recover_quarantined_nodes(self):
        source = (ROOT / "app" / "production_probe_targets.py").read_text(encoding="utf-8")
        self.assertIn("VpnServer.status != VpnServerStatus.disabled", source)
        self.assertNotIn("VpnServer.status == VpnServerStatus.active", source)

    def test_production_update_never_skips_runtime_vpn_e2e(self):
        text = (ROOT / "scripts" / "update_production.sh").read_text(encoding="utf-8")
        discovery = text.index("=== DISCOVER REGISTERED MARZBAN NODES ===")
        e2e = text.index("Fresh-user hot-add E2E WITHOUT Xray restart", discovery)
        success = text.rindex("PRODUCTION UPDATE PASS")
        early = text[:discovery]
        self.assertNotIn('echo "PRODUCTION UPDATE PASS"', early)
        self.assertNotIn("exit 0", early)
        self.assertLess(discovery, e2e)
        self.assertLess(e2e, success)

    def test_failed_real_tunnel_quarantines_node_without_deleting_credentials(self):
        orchestrator = (ROOT / "scripts" / "update_production.sh").read_text(encoding="utf-8")
        state = (ROOT / "app" / "node_probe_state.py").read_text(encoding="utf-8")
        gate = (ROOT / "scripts" / "production_vpn_gate.sh").read_text(encoding="utf-8")
        self.assertIn("quarantine_node", orchestrator)
        self.assertIn("app.node_probe_state", orchestrator)
        self.assertIn("paid key still failed after one DB-user resync", orchestrator)
        self.assertIn("VpnServerStatus.unreachable", state)
        self.assertIn("server.is_selectable = False", state)
        self.assertNotIn("delete_key(", state)
        self.assertNotIn("session.delete", state)
        self.assertIn("app.node_probe_state", gate)

    def test_reality_params_are_bound_to_exact_vless_reality_inbound(self):
        source = (ROOT / "app" / "vpn" / "marzban.py").read_text(encoding="utf-8")
        self.assertIn('inbound.get("protocol") == "vless"', source)
        self.assertIn('get("security") == "reality"', source)
        self.assertIn("if len(reality_inbounds) != 1:", source)

    def test_paid_probe_skips_remote_limited_or_exhausted_credentials(self):
        source = (ROOT / "app" / "stabilize_vless.py").read_text(encoding="utf-8")
        self.assertIn('remote.get("status") != "active"', source)
        self.assertIn("limit > 0 and used >= limit", source)
        self.assertIn("no remotely active non-exhausted paid key", source)

    def test_production_gate_republishes_disposable_verified_nodes(self):
        text = (ROOT / "scripts" / "production_vpn_gate.sh").read_text(encoding="utf-8")
        disposable = text.index('bash scripts/smoke_vpn_node.sh "$server_id"')
        next_step = text.index('CURRENT_STEP="8/11', disposable)
        block = text[disposable:next_step]
        self.assertIn("app.node_probe_state", block)
        self.assertIn("--success", block)

    def test_paid_key_failure_gets_only_one_bounded_db_user_resync(self):
        update = (ROOT / "scripts" / "update_production.sh").read_text(encoding="utf-8")
        gate = (ROOT / "scripts" / "production_vpn_gate.sh").read_text(encoding="utf-8")
        recovery = (ROOT / "app" / "resync_vpn_node.py").read_text(encoding="utf-8")
        self.assertIn("app.resync_vpn_node", update)
        self.assertIn("app.resync_vpn_node", gate)
        self.assertIn("ONE emergency DB->live Xray resync", update)
        self.assertIn("one bounded DB-user resync", gate)
        self.assertIn("restart_core_with_db_users", recovery)
        self.assertIn("credentials unchanged", recovery)
        self.assertNotIn("create_key(", recovery)
        self.assertNotIn("delete_key(", recovery)

    def test_direct_ipv4_repair_script_is_retired(self):
        text = (ROOT / "scripts" / "repair_registered_node_ipv4.sh").read_text(encoding="utf-8")
        self.assertIn("retired", text.lower())
        self.assertNotIn("UPDATE vpn_servers", text)
        self.assertNotIn("curl ", text)

    def test_public_subscription_brand(self):
        route = (ROOT / "admin" / "routes" / "vpn_subscription.py").read_text(encoding="utf-8")
        self.assertIn('"profile-title": "Private Line VPN"', route)
        self.assertIn('VpnServer.provider != "private-line-primary"', route)


    def test_legacy_node_image_is_self_healed_by_compose_override(self):
        runtime = (ROOT / "scripts" / "prepare_marzban_runtime.sh").read_text(encoding="utf-8")
        golden = (ROOT / "scripts" / "converge_vpn_node.sh").read_text(encoding="utf-8")
        self.assertIn("image: $MARZBAN_IMAGE", runtime)
        self.assertIn("CURRENT_MARZBAN_IMAGE", golden)
        self.assertIn('MARZBAN_IMAGE="$EXPECTED_MARZBAN"', golden)
        self.assertIn("Legacy/unpinned Marzban image detected", golden)
        self.assertNotIn("Marzban image is not the tested digest-pinned", golden)


    def test_telegram_delivery_prefers_subscription_but_preserves_raw_key_fallback(self):
        notify = (ROOT / "app" / "services" / "user_notifications.py").read_text(encoding="utf-8")
        access = (ROOT / "bot" / "handlers" / "access.py").read_text(encoding="utf-8")
        token = (ROOT / "app" / "services" / "sub_token.py").read_text(encoding="utf-8")
        self.assertIn("make_subscription_url", notify)
        self.assertIn('subscription_url = ""', notify)
        self.assertIn("get_client_access_url", notify)
        self.assertIn("make_subscription_url", access)
        self.assertIn("subscription_block", access)
        self.assertIn("get_client_access_url", access)
        self.assertIn("CONTROL_PLANE_PUBLIC_URL", token)
        self.assertIn('base.startswith("https://")', token)
        self.assertIn('return f"{base}/sub/{token}"', token)

    def test_subscription_endpoint_is_private_no_store_and_keeps_multiple_links(self):
        route = (ROOT / "admin" / "routes" / "vpn_subscription.py").read_text(encoding="utf-8")
        self.assertIn('"cache-control": "no-store, private"', route)
        self.assertIn('"profile-update-interval": "6"', route)
        self.assertIn('"\\n".join(links)', route)
        self.assertIn("canonical_client_link", route)
        self.assertIn("key.label", route)
        self.assertIn("VpnServerStatus.unreachable", route)
        self.assertIn("case((VpnServer.status == VpnServerStatus.active, 0)", route)


if __name__ == "__main__":
    unittest.main()
