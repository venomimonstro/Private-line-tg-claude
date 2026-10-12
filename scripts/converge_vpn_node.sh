#!/usr/bin/env bash
# Canonical idempotent convergence for a Private Line Marzban VPN node.
#
# Goal: boring production infrastructure.
# - exactly one VLESS + TCP + REALITY inbound;
# - preserve inbound tag, Reality private key and every existing shortId;
# - pin the tested Xray core;
# - never rotate customer UUIDs or Reality credentials;
# - restart Marzban/Xray only when node configuration actually changed;
# - verify target, firewall, API and live :443 before returning success.
set -Eeuo pipefail
umask 077

[[ "$(id -u)" -eq 0 ]] || { echo "FAIL: run as root" >&2; exit 2; }
cd /opt/marzban
[[ -f .env && -f docker-compose.yml ]] || {
    echo "FAIL: /opt/marzban is not an installed Marzban node" >&2
    exit 2
}

CONFIG=/var/lib/marzban/xray_config.json
[[ -s "$CONFIG" ]] || { echo "FAIL: $CONFIG is missing" >&2; exit 2; }

CORE="/usr/local/bin/xray"
MARZBAN_IMAGE="$(docker compose config --images 2>/dev/null | head -n1)"
[[ -n "$MARZBAN_IMAGE" ]] || { echo "FAIL: cannot determine Marzban Docker image" >&2; exit 2; }
case "$(uname -m)" in
    x86_64)
        EXPECTED_MARZBAN="ghcr.io/gozargah/marzban:v0.8.4@sha256:953700b0b91b35dcd5165072fc7029fc3c5bd6ed10d644f80d7cb9a84a9a27f1"
        ;;
    aarch64|arm64)
        EXPECTED_MARZBAN="ghcr.io/gozargah/marzban:v0.8.4@sha256:886f11d701005a80d381d4e4d659ae376d2fff55470578f115b518687d86453e"
        ;;
    *)
        echo "FAIL: unsupported Marzban architecture $(uname -m)" >&2
        exit 2
        ;;
esac
[[ "$MARZBAN_IMAGE" == "$EXPECTED_MARZBAN" ]] || {
    echo "FAIL: Marzban image is not the tested digest-pinned v0.8.4 runtime" >&2
    echo "Current image: $MARZBAN_IMAGE" >&2
    exit 2
}
docker image inspect "$MARZBAN_IMAGE" >/dev/null 2>&1 || docker pull "$MARZBAN_IMAGE" >/dev/null
TARGET_HOST="${REALITY_TARGET_HOST:-dl.google.com}"
TARGET="${TARGET_HOST}:443"
API_PORT=2096
CONTROL_IP="${MAIN_SERVER_IP:-${SSH_CONNECTION%% *}}"

case "$TARGET_HOST" in
    ""|*[!A-Za-z0-9.-]*) echo "FAIL: invalid REALITY target hostname" >&2; exit 2 ;;
esac

if ! CONTROL_IP="$CONTROL_IP" python3 - <<'PY' >/dev/null
import ipaddress, os
ip=ipaddress.ip_address(os.environ["CONTROL_IP"])
assert ip.version == 4 and ip.is_global
PY
then
    echo "FAIL: cannot determine public control-plane IPv4 from MAIN_SERVER_IP/SSH_CONNECTION" >&2
    exit 2
fi

echo "=== PRIVATE LINE GOLDEN NODE CONVERGENCE ==="
echo "Control plane: $CONTROL_IP; Marzban API: TCP/$API_PORT (firewall-restricted)"

# REALITY depends on sane TLS time. Enable the OS time service when available.
if command -v timedatectl >/dev/null 2>&1; then
    timedatectl set-ntp true >/dev/null 2>&1 || true
fi

echo "[1/7] Verify REALITY target TLS 1.3 + HTTP/2"
TLS_LOG="$(mktemp)"
trap 'rm -f "$TLS_LOG"' EXIT
if ! timeout 15 openssl s_client -connect "$TARGET" -servername "$TARGET_HOST" \
    -tls1_3 -alpn h2 </dev/null >"$TLS_LOG" 2>&1; then
    echo "FAIL: REALITY target $TARGET TLS 1.3 handshake failed" >&2
    exit 2
fi
grep -q 'ALPN protocol: h2' "$TLS_LOG" || {
    echo "FAIL: REALITY target $TARGET did not negotiate h2" >&2
    exit 2
}
rm -f "$TLS_LOG"
trap - EXIT

echo "[2/7] Verify Xray bundled with the pinned Marzban image"
BUNDLED_XRAY_VERSION="$(docker run --rm --entrypoint "$CORE" "$MARZBAN_IMAGE" version 2>/dev/null | head -n1 || true)"
[[ "$BUNDLED_XRAY_VERSION" == Xray* ]] || {
    echo "FAIL: bundled Marzban Xray binary is unavailable" >&2
    exit 2
}
echo "Bundled runtime: $BUNDLED_XRAY_VERSION"

RUNTIME_CHANGED=0
# A prior custom install may still point Marzban at an unrelated Xray. The
# standard runtime must use the binary packaged inside this exact Marzban image.
if ! docker compose exec -T marzban python - "$CORE" <<'PY' >/dev/null 2>&1
import os, pathlib, sys
expected=sys.argv[1]
for p in pathlib.Path("/proc").iterdir():
    if not p.name.isdigit():
        continue
    try:
        if os.readlink(p / "exe") == expected:
            raise SystemExit(0)
    except OSError:
        pass
raise SystemExit(1)
PY
then
    RUNTIME_CHANGED=1
fi

echo "[3/7] Converge one canonical VLESS TCP REALITY inbound"
BACKUP_DIR="/var/lib/marzban/backups/golden-node-$(date -u +%Y%m%dT%H%M%SZ)-$$"
mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"
cp -p "$CONFIG" "$BACKUP_DIR/xray_config.before.json"
CANDIDATE="$BACKUP_DIR/xray_config.candidate.json"

PRIVATE_KEY="$(python3 - "$CONFIG" <<'PY'
import json, sys
cfg=json.load(open(sys.argv[1]))
rows=[i for i in cfg.get("inbounds", []) if i.get("protocol")=="vless" and i.get("streamSettings",{}).get("security")=="reality"]
if len(rows)!=1:
    raise SystemExit(2)
print(rows[0].get("streamSettings",{}).get("realitySettings",{}).get("privateKey",""))
PY
)"
[[ "$PRIVATE_KEY" =~ ^[A-Za-z0-9_-]{43,44}$ ]] || {
    echo "FAIL: existing REALITY privateKey missing/invalid; refusing rotation" >&2
    exit 2
}
XRAY_KEYS="$(docker run --rm --entrypoint "$CORE" "$MARZBAN_IMAGE" x25519 -i "$PRIVATE_KEY" 2>/dev/null || true)"
PUBLIC_KEY="$(printf '%s\n' "$XRAY_KEYS" | awk -F ':[[:space:]]*' 'tolower($1) ~ /^(public ?key|password( \(publickey\))?)$/ { print $2; exit }')"
[[ "$PUBLIC_KEY" =~ ^[A-Za-z0-9_-]{43,44}$ ]] || {
    echo "FAIL: bundled Xray could not derive REALITY public key" >&2
    exit 2
}

CONFIG="$CONFIG" CANDIDATE="$CANDIDATE" TARGET="$TARGET" TARGET_HOST="$TARGET_HOST" PUBLIC_KEY="$PUBLIC_KEY" \
python3 - <<'PY'
import base64
import json
import os
import pathlib
import re

src = pathlib.Path(os.environ["CONFIG"])
dst = pathlib.Path(os.environ["CANDIDATE"])
target = os.environ["TARGET"]
target_host = os.environ["TARGET_HOST"]

cfg = json.loads(src.read_text())
rows = []
for inbound in cfg.get("inbounds", []):
    stream = inbound.get("streamSettings", {})
    if inbound.get("protocol") == "vless" and stream.get("security") == "reality":
        rows.append(inbound)

if len(rows) != 1:
    raise SystemExit(f"FAIL: expected exactly one VLESS REALITY inbound, found {len(rows)}")

inbound = rows[0]
reality = inbound.setdefault("streamSettings", {}).setdefault("realitySettings", {})
private_key = reality.get("privateKey")
short_ids = reality.get("shortIds")
if not isinstance(private_key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43,44}", private_key):
    raise SystemExit("FAIL: existing REALITY privateKey is missing/invalid; refusing credential rotation")
if not isinstance(short_ids, list) or not short_ids:
    raise SystemExit("FAIL: existing REALITY shortIds are missing; refusing credential rotation")
for sid in short_ids:
    if not isinstance(sid, str) or not re.fullmatch(r"(?:[0-9a-fA-F]{2}){1,8}", sid):
        raise SystemExit("FAIL: existing REALITY shortId is invalid")

public_key = os.environ["PUBLIC_KEY"]
if not re.fullmatch(r"[A-Za-z0-9_-]{43,44}", public_key):
    raise SystemExit("FAIL: derived REALITY public key is invalid")

# Preserve the tag because Marzban stores inbound assignment by tag.
tag = inbound.get("tag")
if not isinstance(tag, str) or not tag:
    raise SystemExit("FAIL: VLESS inbound tag is missing")

inbound["listen"] = "0.0.0.0"
inbound["port"] = 443
inbound["protocol"] = "vless"
inbound["settings"] = {
    "clients": [],
    "decryption": "none",
}
inbound["sniffing"] = {"enabled": False}
inbound["streamSettings"] = {
    "network": "tcp",
    "tcpSettings": {},
    "security": "reality",
    "realitySettings": {
        "show": False,
        "target": target,
        "xver": 0,
        "serverNames": [target_host],
        "privateKey": private_key,
        # Metadata used by Marzban 0.8.4 to build client settings without
        # relying on its obsolete x25519 stdout parser.
        "publicKey": public_key,
        # Explicitly accept older/non-Xray client implementations. Newer
        # Xray releases changed the implicit default and broke clients when
        # this field was omitted.
        "minClientVer": "",
        "maxClientVer": "",
        "maxTimeDiff": 0,
        "shortIds": short_ids,
    },
}

# Keep the static config deliberately small. Marzban adds its API inbound and
# database users to the runtime config itself.
cfg["log"] = {"loglevel": "warning"}
cfg["outbounds"] = [
    {"tag": "direct", "protocol": "freedom", "settings": {}},
    {"tag": "block", "protocol": "blackhole", "settings": {}},
]
cfg["routing"] = {
    "domainStrategy": "AsIs",
    "rules": [
        {"type": "field", "ip": [
            "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
            "127.0.0.0/8", "169.254.0.0/16",
            "::1/128", "fc00::/7", "fe80::/10"
        ], "outboundTag": "block"},
        {"type": "field", "network": "tcp,udp", "outboundTag": "direct"},
    ],
}

dst.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n")
os.chmod(dst, 0o600)
print(f"PASS: canonical inbound tag={tag!r}; credentials preserved")
PY

# Test the exact candidate before touching production.
docker run --rm \
    -v "$CANDIDATE:/tmp/private-line-xray.json:ro" \
    --entrypoint "$CORE" "$MARZBAN_IMAGE" \
    run -test -config /tmp/private-line-xray.json </dev/null >/dev/null \
    || { echo "FAIL: bundled Xray rejected canonical candidate" >&2; exit 2; }

CHANGED=0
if ! cmp -s "$CONFIG" "$CANDIDATE"; then
    CHANGED=1
    install -m 600 "$CANDIDATE" "$CONFIG"
fi

# Keep Marzban's own core launcher deterministic.
ENV_CHANGED=0
grep -qxF "XRAY_JSON=/var/lib/marzban/xray_config.json" .env || ENV_CHANGED=1
grep -qxF "XRAY_EXECUTABLE_PATH=/usr/local/bin/xray" .env || ENV_CHANGED=1
grep -qxF "XRAY_ASSETS_PATH=/usr/local/share/xray" .env || ENV_CHANGED=1
grep -qxF "UVICORN_HOST=0.0.0.0" .env || ENV_CHANGED=1
grep -qxF "UVICORN_PORT=$API_PORT" .env || ENV_CHANGED=1

python3 - <<PY
from pathlib import Path
import os, re
p=Path("/opt/marzban/.env")
lines=p.read_text().splitlines()
updates={
    "XRAY_JSON": "/var/lib/marzban/xray_config.json",
    "XRAY_EXECUTABLE_PATH": "/usr/local/bin/xray",
    "XRAY_ASSETS_PATH": "/usr/local/share/xray",
    "UVICORN_HOST": "0.0.0.0",
    "UVICORN_PORT": "$API_PORT",
}
out=[]
seen=set()
for line in lines:
    m=re.match(r"^\s*([A-Z0-9_]+)=", line)
    if m and m.group(1) in updates:
        key=m.group(1)
        out.append(f"{key}={updates[key]}")
        seen.add(key)
    else:
        out.append(line)
for key, value in updates.items():
    if key not in seen:
        out.append(f"{key}={value}")
tmp=p.with_name(".env.golden")
tmp.write_text("\n".join(out)+"\n")
os.chmod(tmp, 0o600)
os.replace(tmp, p)
PY

echo "[4/7] Ensure firewall: public VPN/443; API/$API_PORT only from control plane"
if command -v ufw >/dev/null 2>&1; then
    # Preserve existing rules/defaults. Never reset UFW over SSH.
    ufw allow 443/tcp comment "VLESS Reality VPN" >/dev/null 2>&1 || true
    # Delete only an old broad API rule; then add the narrow control-plane rule.
    ufw delete allow "$API_PORT/tcp" >/dev/null 2>&1 || true
    ufw allow from "$CONTROL_IP" to any port "$API_PORT" proto tcp \
        comment "Marzban API control plane" >/dev/null 2>&1 || {
            echo "FAIL: cannot add restricted Marzban API firewall rule" >&2
            exit 2
        }
    ufw --force enable >/dev/null 2>&1 || true
    ufw status 2>/dev/null | grep -Eq '^443/tcp[[:space:]]+ALLOW' || {
        echo "FAIL: UFW does not allow public TCP/443" >&2
        exit 2
    }
    ufw status 2>/dev/null | grep -F "$CONTROL_IP" | grep -q "$API_PORT" || {
        echo "FAIL: UFW does not allow control plane to Marzban API/$API_PORT" >&2
        exit 2
    }
fi

# Legacy installs used a localhost Marzban API plus socat. Direct Uvicorn on
# the firewall-restricted port removes that extra process and failure mode.
if systemctl list-unit-files 2>/dev/null | grep -q '^marzban-proxy.service'; then
    systemctl disable --now marzban-proxy.service >/dev/null 2>&1 || true
fi

echo "[5/7] Restart only when the canonical runtime changed"
if [[ "$CHANGED" -eq 1 || "$ENV_CHANGED" -eq 1 || "$RUNTIME_CHANGED" -eq 1 ]]; then
    echo "Runtime change detected: config=$CHANGED env=$ENV_CHANGED process=$RUNTIME_CHANGED"
    docker compose up -d --no-deps --force-recreate marzban </dev/null
else
    echo "PASS: canonical Xray runtime already current; no core restart"
fi

echo "[6/7] Wait for Marzban API and live Xray listener"
READY=0
for _ in $(seq 1 40); do
    if docker compose exec -T marzban python - <<'PY' >/dev/null 2>&1
import socket
import urllib.request
with socket.create_connection(("127.0.0.1", 443), timeout=2):
    pass
with urllib.request.urlopen("http://127.0.0.1:2096/openapi.json", timeout=3) as r:
    assert r.status == 200
PY
    then
        READY=1
        break
    fi
    sleep 2
done
[[ "$READY" -eq 1 ]] || {
    echo "FAIL: Marzban/Xray did not become ready" >&2
    docker compose logs --no-color --tail 80 marzban 2>&1 | \
        sed -E \
          -e 's/[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}/[UUID]/g' \
          -e 's/[A-Za-z0-9_-]{43,100}/[REDACTED_LONG_STRING]/g' | tail -n 35 >&2 || true
    exit 2
}

echo "[7/7] Verify running bundled core and canonical static config"
RUNNING_XRAY_VERSION="$(docker compose exec -T marzban "$CORE" version 2>/dev/null | head -n1 || true)"
[[ "$RUNNING_XRAY_VERSION" == "$BUNDLED_XRAY_VERSION" ]] || {
    echo "FAIL: running Xray does not match pinned Marzban image runtime" >&2
    echo "Bundled=$BUNDLED_XRAY_VERSION running=$RUNNING_XRAY_VERSION" >&2
    exit 2
}
docker compose exec -T marzban "$CORE" run -test -config "$CONFIG" </dev/null >/dev/null \
    || { echo "FAIL: live static config no longer validates" >&2; exit 2; }

echo "GOLDEN NODE PASS"
echo "Runtime: $RUNNING_XRAY_VERSION bundled with digest-pinned Marzban v0.8.4."
echo "Preserved: Marzban DB, user UUIDs, REALITY privateKey, shortIds, inbound tag."
echo "Canonical: VLESS/TCP/REALITY/Vision-ready on public TCP/443, target=$TARGET."
