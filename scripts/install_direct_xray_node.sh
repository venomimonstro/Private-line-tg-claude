#!/usr/bin/env bash
# Convert a dedicated VPN node to the simplest Private Line runtime:
# standalone Xray + static user list + atomic restart/rollback.
set -Eeuo pipefail
umask 077
[[ "$(id -u)" -eq 0 ]] || { echo "FAIL: run as root" >&2; exit 2; }

ROOT=/opt/private-line-node
DATA=/var/lib/private-line-node
XRAY="$ROOT/xray"
MANAGER="$ROOT/manage.py"
PORT=443
HOST="${VPN_PUBLIC_HOST:-$(hostname -I | awk '{print $1}')}"
SNI="${REALITY_TARGET_HOST:-dl.google.com}"
TARGET="${SNI}:443"

mkdir -p "$ROOT" "$DATA"
chmod 700 "$ROOT" "$DATA"

# Reuse the already installed official Xray first. Avoid downloads during migration.
if [[ -x /var/lib/marzban/xray-core/26.6.27/xray ]]; then
  install -m 755 /var/lib/marzban/xray-core/26.6.27/xray "$XRAY"
elif command -v xray >/dev/null 2>&1; then
  install -m 755 "$(command -v xray)" "$XRAY"
else
  echo "FAIL: official Xray binary not found on node; run current updater once first" >&2
  exit 2
fi
"$XRAY" version | head -1 | grep -q '^Xray ' || { echo "FAIL: invalid Xray binary" >&2; exit 2; }

# Preserve existing REALITY identity when migrating from Marzban.
python3 - "$DATA/state.json" /var/lib/marzban/xray_config.json "$XRAY" "$HOST" "$PORT" "$SNI" "$TARGET" <<'PY'
import json, os, re, subprocess, sys
from pathlib import Path
state_path=Path(sys.argv[1]); old_path=Path(sys.argv[2]); xray=sys.argv[3]
host=sys.argv[4]; port=int(sys.argv[5]); sni=sys.argv[6]; target=sys.argv[7]
if state_path.exists():
    raise SystemExit(0)
private=public=sid=""
if old_path.exists():
    try:
        cfg=json.loads(old_path.read_text())
        for inbound in cfg.get("inbounds",[]):
            ss=inbound.get("streamSettings",{})
            if inbound.get("protocol")=="vless" and ss.get("security")=="reality":
                r=ss.get("realitySettings",{})
                private=str(r.get("privateKey") or "")
                public=str(r.get("publicKey") or "")
                ids=r.get("shortIds") or []
                sid=str(ids[0]) if ids else ""
                names=r.get("serverNames") or []
                if names: sni=str(names[0])
                target=str(r.get("target") or r.get("dest") or target)
                break
    except Exception:
        pass
if not re.fullmatch(r"[A-Za-z0-9_-]{43,44}",private):
    p=subprocess.run([xray,"x25519"],text=True,capture_output=True,check=True)
    for line in p.stdout.splitlines():
        k,_,v=line.partition(":"); key=re.sub(r"\s+","",k).lower()
        if key=="privatekey": private=v.strip()
        if key in ("publickey","password","password(publickey)"): public=v.strip()
if private and not re.fullmatch(r"[A-Za-z0-9_-]{43,44}",public):
    p=subprocess.run([xray,"x25519","-i",private],text=True,capture_output=True,check=True)
    for line in p.stdout.splitlines():
        k,_,v=line.partition(":"); key=re.sub(r"\s+","",k).lower()
        if key in ("publickey","password","password(publickey)"): public=v.strip()
if not re.fullmatch(r"[A-Za-z0-9_-]{43,44}",private) or not re.fullmatch(r"[A-Za-z0-9_-]{43,44}",public):
    raise SystemExit("FAIL: REALITY keypair invalid")
if not re.fullmatch(r"(?:[0-9a-fA-F]{2}){1,8}",sid):
    sid=os.urandom(8).hex()
state={"host":host,"port":port,"sni":sni,"target":target,"private_key":private,
       "public_key":public,"short_id":sid,"users":{}}
state_path.write_text(json.dumps(state,indent=2)+"\n")
os.chmod(state_path,0o600)
PY

cat > /etc/systemd/system/private-line-xray.service <<EOF
[Unit]
Description=Private Line standalone Xray
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
ExecStart=$XRAY run -config $DATA/xray.json
Restart=on-failure
RestartSec=2
LimitNOFILE=1048576

[Install]
WantedBy=multi-user.target
EOF

# Disable Marzban completely; it is no longer in the customer traffic path.
if [[ -f /opt/marzban/docker-compose.yml ]]; then
  (cd /opt/marzban && docker compose down --remove-orphans) >/dev/null 2>&1 || true
fi
systemctl disable --now marzban-proxy.service >/dev/null 2>&1 || true

# Manager file is installed by the rollout before this script runs.
[[ -s "$MANAGER" ]] || { echo "FAIL: $MANAGER missing" >&2; exit 2; }
chmod 700 "$MANAGER"

systemctl daemon-reload
systemctl enable private-line-xray.service >/dev/null

# First installation needs one rendered config. Repeated updates must NEVER
# clear the durable user list; migration/sync runs explicitly from control-plane.
if [[ ! -s "$DATA/xray.json" ]]; then
  "$MANAGER" sync e30= >/dev/null
fi

ufw allow 22/tcp comment "SSH" >/dev/null 2>&1 || true
ufw allow 443/tcp comment "Private Line VLESS Reality" >/dev/null 2>&1 || true
ufw delete allow 2096/tcp >/dev/null 2>&1 || true
ufw --force enable >/dev/null 2>&1 || true

systemctl restart private-line-xray.service
sleep 1
systemctl is-active --quiet private-line-xray.service || { journalctl -u private-line-xray -n 40 --no-pager >&2; exit 2; }
"$MANAGER" status
