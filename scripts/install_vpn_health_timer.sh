#!/usr/bin/env bash
# Idempotently install the control-plane VPN health watchdog.
set -Eeuo pipefail
[[ "$(id -u)" -eq 0 ]] || { echo "FAIL: run as root" >&2; exit 2; }
[[ -x /bin/bash && -f /opt/private-line/scripts/vpn_health_watch.sh ]] || {
    echo "FAIL: Private Line health-watch script is missing" >&2
    exit 2
}

cat >/etc/systemd/system/private-line-vpn-health.service <<'EOF'
[Unit]
Description=Private Line real VPN data-plane health check
After=docker.service
Wants=docker.service

[Service]
Type=oneshot
WorkingDirectory=/opt/private-line
ExecStart=/bin/bash /opt/private-line/scripts/vpn_health_watch.sh
TimeoutStartSec=240
Nice=10
IOSchedulingClass=best-effort
IOSchedulingPriority=7
EOF

cat >/etc/systemd/system/private-line-vpn-health.timer <<'EOF'
[Unit]
Description=Run Private Line VPN health check every minute

[Timer]
OnBootSec=1min
OnUnitActiveSec=1min
RandomizedDelaySec=20s
Persistent=true
Unit=private-line-vpn-health.service

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now private-line-vpn-health.timer >/dev/null
systemctl is-enabled private-line-vpn-health.timer >/dev/null
echo "PASS: Private Line real VPN health timer enabled (1m)"
