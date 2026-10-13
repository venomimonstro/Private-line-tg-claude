#!/usr/bin/env bash
# Run via update_production.sh over SSH; no panel patching or CI involved.
set -Eeuo pipefail
umask 077
[[ "$(id -u)" -eq 0 ]] || { echo 'FAIL: root required' >&2; exit 2; }
SOURCE="$(cd "$(dirname "$0")/.." && pwd)"
DEST=/opt/private-line-node
DATA=/var/lib/private-line-node
CREDENTIALS="$SOURCE/node-credentials.json"
XRAY_VERSION=26.6.27
XRAY_IMAGE='ghcr.io/xtls/xray-core:26.6.27@sha256:c98160906f3fe1d9d16e950c2a7997dc0c542c71816aa92c21e510412f90e1a2'
mkdir -p "$DEST" "$DATA"
chmod 700 "$DEST" "$DATA"
exec 8>/var/lock/private-line-node-install.lock
flock -n 8 || { echo 'FAIL: another node installation is running' >&2; exit 2; }
STAGE="$(mktemp -d "$DATA/stage-XXXXXXXX")"
PANEL_CID=''
PROXY_WAS_ACTIVE=0
CUTOVER=0
BLOCKED=0
SUCCESS=0
WAS_DIRECT=0
[[ ! -f "$DATA/state.json" ]] || WAS_DIRECT=1
cleanup() {
    rc=$?
    if [[ "$SUCCESS" -ne 1 && "$CUTOVER" -eq 1 ]]; then
        echo 'FAIL: direct Xray startup failed; restoring previous runtime' >&2
        systemctl stop private-line-node.service || true
        if [[ "$WAS_DIRECT" -eq 1 ]]; then
            cp "$STAGE/agent.before.py" "$DEST/agent.py"
            systemctl start private-line-node.service || true
        else
            rm -f "$DATA/state.json"
            systemctl disable private-line-node.service >/dev/null 2>&1 || true
            docker update --restart=unless-stopped "$PANEL_CID" >/dev/null || true
            docker start "$PANEL_CID" >/dev/null || true
            if [[ "$PROXY_WAS_ACTIVE" -eq 1 ]]; then
                systemctl enable --now marzban-proxy.service >/dev/null 2>&1 || true
            fi
        fi
    fi
    if [[ "$BLOCKED" -eq 1 ]]; then
        iptables -D INPUT -p tcp --dport 2096 ! -s 127.0.0.1 -m comment --comment private-line-migration -j REJECT || true
    fi
    rm -rf "$STAGE"
    exit "$rc"
}
trap cleanup EXIT

# Reuse the pinned executable, independently of the broken Marzban patcher.
CORE="/var/lib/marzban/xray-core/$XRAY_VERSION/xray"
if [[ -x "$DEST/xray" ]] && [[ "$("$DEST/xray" version)" == "Xray $XRAY_VERSION"* ]]; then
    CORE="$DEST/xray"
elif [[ ! -x "$CORE" ]] || [[ "$("$CORE" version 2>/dev/null || true)" != "Xray $XRAY_VERSION"* ]]; then
    docker image inspect "$XRAY_IMAGE" >/dev/null 2>&1 || docker pull "$XRAY_IMAGE"
    BIN_CID="$(docker create "$XRAY_IMAGE")"
    docker cp "$BIN_CID:/usr/local/bin/xray" "$STAGE/xray"
    docker rm "$BIN_CID" >/dev/null
    chmod 755 "$STAGE/xray"
    CORE="$STAGE/xray"
fi
[[ "$("$CORE" version)" == "Xray $XRAY_VERSION"* ]] || { echo 'FAIL: wrong Xray version'; exit 2; }
[[ "$CORE" == "$DEST/xray" ]] || install -m 755 "$CORE" "$DEST/xray"
python3 -m py_compile "$SOURCE/node/agent.py" "$SOURCE/node/migrate.py"
echo "=== ISOLATED REAL XRAY ACCEPTANCE BEFORE PRODUCTION CUTOVER ==="
python3 "$SOURCE/node/selftest.py" --binary "$DEST/xray"

if [[ "$WAS_DIRECT" -eq 0 ]]; then
    [[ -s "$CREDENTIALS" ]] || { echo 'FAIL: migration credentials missing'; exit 2; }
    PANEL_CID="$(cd /opt/marzban && docker compose ps -q marzban)"
    [[ -n "$PANEL_CID" ]] || { echo 'FAIL: original panel is not running; cannot export users'; exit 2; }
    # Block new management writes while exporting. Loopback export remains
    # available; Telegram retries provisioning when management becomes ready.
    iptables -I INPUT 1 -p tcp --dport 2096 ! -s 127.0.0.1 -m comment --comment private-line-migration -j REJECT
    BLOCKED=1
    sleep 16
    python3 "$SOURCE/node/migrate.py" --credentials "$CREDENTIALS" --directory "$STAGE" --binary "$DEST/xray"
    BACKUP="$DATA/backup-$(date -u +%Y%m%dT%H%M%SZ)"
    mkdir -m 700 "$BACKUP"
    cp "$STAGE/marzban-export.json" "$BACKUP/"
    cp -a /opt/marzban "$BACKUP/compose"
    # Native files are also retained in place; no delete/migrate of panel DB.
    docker update --restart=no "$PANEL_CID" >/dev/null
    CUTOVER=1
    if systemctl is-active --quiet marzban-proxy.service; then
        PROXY_WAS_ACTIVE=1
        systemctl disable --now marzban-proxy.service
    fi
    docker stop "$PANEL_CID" >/dev/null
    cp "$STAGE/state.json" "$DATA/state.json"
    echo "Original panel retained, export backup: $BACKUP"
else
    cp "$DEST/agent.py" "$STAGE/agent.before.py"
fi

# Defense in depth: agent itself also allows only control_ip and loopback.
if command -v ufw >/dev/null 2>&1; then
    ufw allow 443/tcp >/dev/null
    ufw allow from 194.146.223.104 to any port 2096 proto tcp >/dev/null
fi
CHANGED=0
cmp -s "$SOURCE/node/agent.py" "$DEST/agent.py" || CHANGED=1
install -m 600 "$SOURCE/node/agent.py" "$DEST/agent.py"
cat > "$STAGE/private-line-node.service" <<'UNIT'
[Unit]
Description=Private Line direct Xray node
After=network-online.target
Wants=network-online.target
[Service]
Type=simple
ExecStart=/usr/bin/python3 /opt/private-line-node/agent.py
Restart=always
RestartSec=3
TimeoutStopSec=15
KillMode=control-group
UMask=0077
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths=/var/lib/private-line-node
ProtectHome=true
PrivateTmp=true
LimitNOFILE=65536
[Install]
WantedBy=multi-user.target
UNIT
cmp -s "$STAGE/private-line-node.service" /etc/systemd/system/private-line-node.service || CHANGED=1
install -m 644 "$STAGE/private-line-node.service" /etc/systemd/system/private-line-node.service
systemctl daemon-reload
systemctl enable private-line-node.service >/dev/null
if [[ "$CHANGED" -eq 1 ]] || ! systemctl is-active --quiet private-line-node.service; then
    CUTOVER=1
    systemctl restart private-line-node.service
fi
# Authentication + live HandlerService round trip, not merely an open port.
python3 - "$DATA/state.json" <<'PY'
import json, sys, time, urllib.request, urllib.parse
state=json.load(open(sys.argv[1]))
opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
for _ in range(25):
    try:
        request=urllib.request.Request('http://127.0.0.1:2096/api/admin/token',
            data=urllib.parse.urlencode(state['auth']).encode())
        token=json.load(opener.open(request, timeout=2))['access_token']
        request=urllib.request.Request('http://127.0.0.1:2096/api/core',
            headers={'Authorization':'Bearer '+token})
        core=json.load(opener.open(request, timeout=3))
        if core.get('backend') == 'private-line-xray' and core.get('started') is True:
            break
    except Exception:
        pass
    time.sleep(1)
else:
    raise SystemExit('FAIL: direct Xray API/core did not become ready')
print('DIRECT XRAY LOCAL PASS; external VPN tunnel acceptance follows on main server')
PY
SUCCESS=1
