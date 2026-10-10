#!/usr/bin/env bash
# Dedicated temporary VLESS REALITY node co-located on Private Line main VPS.
# Safe: does not touch main docker-compose.yml, PostgreSQL, admin, Nginx, UFW,
#       Spanish server, or existing Reality keys. Refuses if port 443 is busy.
# Must be invoked manually as root on the MAIN SERVER:
#   bash scripts/enable_primary_vpn.sh
set -Eeuo pipefail
umask 077

cd "$(dirname "$0")/.."
[[ "$(id -u)" -eq 0 ]] || { echo "ERROR: run as root"; exit 1; }
MAIN_DIR="$(pwd -P)"
NODE_DIR="/opt/private-line-primary-node"
DATA_DIR="$NODE_DIR/data"
PRIMARY_IP="${PRIMARY_PUBLIC_IP:-}"
MARZBAN_IMAGE="gozargah/marzban:latest"
XRAY_IMAGE="ghcr.io/xtls/xray-core:26.9.9"
XRAY_BIN="$DATA_DIR/xray-core/xray"
ENV_FILE="$NODE_DIR/.env"

[[ -f "$MAIN_DIR/docker-compose.yml" ]] || { echo "ERROR: missing control plane"; exit 1; }
[[ ! -L "$NODE_DIR" ]] || { echo "ERROR: node directory may not be symlink"; exit 1; }
command -v docker >/dev/null && command -v python3 >/dev/null || {
    echo "ERROR: Docker and python3 are required"; exit 1;
}

if [[ -z "$PRIMARY_IP" ]]; then
    PRIMARY_IP="$(curl -q -4 -fsS --max-time 12 https://api.ipify.org || true)"
fi
python3 - "$PRIMARY_IP" <<'PY'
import ipaddress,sys
try:
    ip=ipaddress.IPv4Address(sys.argv[1])
    if not ip.is_global: raise ValueError("not a global IPv4")
except ValueError:
    raise SystemExit("ERROR: PRIMARY_PUBLIC_IP must be a public IPv4 address")
PY

ADMIN_CONTAINER="$(docker compose -f "$MAIN_DIR/docker-compose.yml" ps -q admin)"
[[ -n "$ADMIN_CONTAINER" ]] || { echo "ERROR: main admin container is stopped"; exit 1; }
NETWORK="$(docker inspect "$ADMIN_CONTAINER" --format '{{range $name, $v := .NetworkSettings.Networks}}{{println $name}}{{end}}' | grep '_privateline$' | head -n 1)"
if [[ -z "$NETWORK" ]]; then
    echo "ERROR: cannot identify main application's privateline Docker network"
    exit 1
fi

if [[ ! -f "$ENV_FILE" ]]; then
    # Fresh deployment: check port and resources BEFORE pulling/installing.
    if ss -ltn '( sport = :443 )' | grep -q ':443'; then
        echo "STOP: main VPS port 443 is already occupied; no modifications"
        exit 2
    fi
    MEM_AVAIL="$(awk '/MemAvailable/ {print $2}' /proc/meminfo)"
    if (( MEM_AVAIL < 524288 )); then
        echo "STOP: main VPS needs at least 512 MiB available RAM"
        exit 2
    fi
    FREE_KB="$(df -Pk /opt | awk 'NR==2 {print $4}')"
    if (( FREE_KB < 1572864 )); then
        echo "STOP: main VPS needs at least 1.5 GiB free disk space"
        exit 2
    fi

    echo "[1/5] Downloading container images and Xray 26.9.9..."
    docker pull "$MARZBAN_IMAGE" >/dev/null
    docker pull "$XRAY_IMAGE" >/dev/null
    mkdir -p "$DATA_DIR/xray-core"
    chmod 700 "$NODE_DIR"
    CID="$(docker create "$XRAY_IMAGE")"
    trap 'docker rm -f "${CID:-}" >/dev/null 2>&1 || true' EXIT
    docker cp "$CID:/usr/local/bin/xray" "$XRAY_BIN"
    docker rm "$CID" >/dev/null 2>&1
    CID=""
    chmod 755 "$XRAY_BIN"
    "$XRAY_BIN" version | head -n 1 | grep -q 'Xray 26.9.9' || {
        echo "ERROR: unexpected Xray version"; exit 2;
    }

    # Generate secrets in private root-owned files and never show credentials.
    echo "[2/5] Creating independent Reality credentials and configuration..."
    python3 - "$NODE_DIR" "$PRIMARY_IP" "$NETWORK" "$XRAY_BIN" <<'PY'
import json, os, pathlib, re, secrets, subprocess, sys
node=pathlib.Path(sys.argv[1])
ip=sys.argv[2]
network=sys.argv[3]
binary=sys.argv[4]
result=subprocess.run([binary,"x25519"],capture_output=True,text=True,check=True)
keys={}
for line in result.stdout.splitlines():
    if ":" in line:
        k,v=line.split(":",1)
        keys[k.strip().lower().replace(" ","")]=v.strip()
private=keys.get("privatekey")
if not private or not re.fullmatch(r"[A-Za-z0-9_-]{43,44}",private):
    raise SystemExit("ERROR: Xray x25519 did not return a valid private key")
password=secrets.token_urlsafe(30)
username="admin"
config={
    "log":{"loglevel":"warning"},
    "dns":{"servers":["1.1.1.1","8.8.8.8"]},
    "inbounds":[{
        "tag":"VLESS-TCP","listen":"0.0.0.0","port":443,"protocol":"vless",
        "settings":{"clients":[],"decryption":"none"},
        "streamSettings":{
            "network":"tcp","security":"reality",
            "realitySettings":{
                "show":False,
                "dest":"www.microsoft.com:443",
                "serverNames":["www.microsoft.com"],
                "privateKey":private,
                "shortIds":[secrets.token_hex(8) for _ in range(4)]
            }
        }
    }],
    "outbounds":[
        {"tag":"direct","protocol":"freedom"},
        {"tag":"block","protocol":"blackhole"}
    ],
    "routing":{"rules":[
        {"type":"field","ip":["geoip:private"],"outboundTag":"block"},
        {"type":"field","network":"udp,tcp","outboundTag":"direct"}
    ]}
}
cfg=node/"data/xray_config.json"
cfg.write_text(json.dumps(config,indent=2))
os.chmod(cfg,0o600)
env=node/".env"
env.write_text(
    f"SUDO_USERNAME={username}\n"
    f"SUDO_PASSWORD={password}\n"
    "UVICORN_HOST=0.0.0.0\nUVICORN_PORT=8000\n"
    "XRAY_JSON=/var/lib/marzban/xray_config.json\n"
    "XRAY_EXECUTABLE_PATH=/var/lib/marzban/xray-core/xray\n"
    f"PRIMARY_VPN_NETWORK={network}\n"
)
os.chmod(env,0o600)
credentials=node/"credentials.json"
credentials.write_text(json.dumps(
    {"username":username,"password":password,"public_ip":ip},
))
os.chmod(credentials,0o600)
PY
else
    echo "[1/5] Existing primary VPN configuration found; reusing credentials and keys."
    # Do not use a different Docker network after manual migration.
    if ! grep -Fxq "PRIMARY_VPN_NETWORK=$NETWORK" "$ENV_FILE"; then
        echo "STOP: primary node was provisioned for a different Docker network"
        exit 2
    fi
    if [[ ! -f "$NODE_DIR/credentials.json" || ! -f "$DATA_DIR/xray_config.json" ]]; then
        echo "STOP: incomplete existing primary node; no files overwritten"
        exit 2
    fi
fi

cat > "$NODE_DIR/docker-compose.yml" <<'YAML'
services:
  marzban:
    image: gozargah/marzban:latest
    container_name: private-line-primary-node
    restart: unless-stopped
    env_file: .env
    volumes:
      - ./data:/var/lib/marzban
    ports:
      - "443:443/tcp"
    networks:
      - main_private
networks:
  main_private:
    external: true
    name: ${PRIMARY_VPN_NETWORK}
YAML

echo "[3/5] Starting co-located Marzban without exposing its API port..."
docker compose -p private-line-primary --project-directory "$NODE_DIR" \
    -f "$NODE_DIR/docker-compose.yml" up -d marzban

echo "[4/5] Waiting for Marzban and Xray to start..."
STARTED=0
for i in $(seq 1 36); do
    if docker compose -f "$MAIN_DIR/docker-compose.yml" exec -T admin python -c \
        'import httpx; r=httpx.get("http://private-line-primary-node:8000/api/core",timeout=3); assert r.status_code in (200,401,403)' \
        >/dev/null 2>&1 \
        && ss -ltn '( sport = :443 )' | grep -q ':443'; then
        STARTED=1
        break
    fi
    sleep 5
done
if [[ "$STARTED" != 1 ]]; then
    echo "FAIL: primary Marzban or Xray did not become reachable; NOT registered for users"
    docker logs --tail 35 private-line-primary-node 2>&1 | \
        grep -E 'ERROR|WARNING|Traceback|Xray|failed' | tail -n 20 || true
    exit 2
fi

echo "[5/5] Verifying primary node, configuring public hosts and registering..."
if ! docker compose -f "$MAIN_DIR/docker-compose.yml" exec -T admin \
    python -m app.bootstrap_primary_node --apply < "$NODE_DIR/credentials.json"; then
    echo "STOP: node NOT registered; check primary Marzban logs; admin unchanged"
    exit 2
fi
echo "DONE: primary main VPS is now a selectable VPN server."
echo "Telegram: Мой доступ -> Сменить страну -> Основной сервер (временно)"
echo "Do NOT re-run the remote VPN node installer: it resets the firewall."
