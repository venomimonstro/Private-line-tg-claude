#!/usr/bin/env bash
# Read-only diagnostics of the live Marzban/Xray VPN node.
# Run from MAIN server:
#   ssh root@185.236.25.152 'bash -s' < scripts/inspect_node_vless.sh
set -u

echo "=== NODE CLOCK ==="
date -u '+UTC %Y-%m-%d %H:%M:%S'
if command -v timedatectl >/dev/null 2>&1; then
    timedatectl show -p NTPSynchronized -p NTP --no-pager 2>/dev/null || true
fi

echo "=== HOST NETWORK ==="
ip -4 -brief address show scope global 2>/dev/null || true
echo "Public IPv4:"
curl -4 -fsS --max-time 10 https://api.ipify.org 2>/dev/null || echo "(unavailable)"
echo

echo "=== VPN LISTENER / FIREWALL ==="
ss -ltnp '( sport = :443 )' 2>/dev/null || true
ufw status 2>/dev/null | head -n 14 || true
echo "=== MARZBAN CONTAINER / XRAY CORE VERSION ==="
docker ps --filter name=marzban --format 'table {{.Names}}\t{{.Status}}' 2>/dev/null || true
docker exec marzban-marzban-1 xray version 2>/dev/null | head -n 1 || true
echo "Running Xray process:"
ps -eo comm,etime,args | grep -E '[x]ray (run|-[c])' | sed -E 's@/[[:alnum:]_/-]*secret[[:alnum:]_/-]*@[REDACTED_PATH]@g' | head -n 4 || true

echo "=== XRAY STATIC CONFIG (NO SECRETS) ==="
python3 - <<'PY'
import json
from pathlib import Path

p = Path("/var/lib/marzban/xray_config.json")
if not p.exists():
    print("FAIL: Xray config file missing")
    raise SystemExit(0)
try:
    config = json.loads(p.read_text())
    for i in config.get("inbounds", []):
        s = i.get("streamSettings", {})
        r = s.get("realitySettings", {})
        if i.get("protocol") == "vless":
            print(
                "tag=%r port=%r listen=%r network=%r security=%r dest=%r sni=%r "
                "shortIds=%s privateKey_present=%s"
                % (
                    i.get("tag"), i.get("port"), i.get("listen"),
                    s.get("network"), s.get("security"), r.get("dest"),
                    r.get("serverNames"), len(r.get("shortIds", [])),
                    bool(r.get("privateKey")),
                )
            )
    print("outbound_protocols:", [x.get("protocol") for x in config.get("outbounds", [])])
except (OSError, ValueError, TypeError) as exc:
    print("FAIL: config parse", type(exc).__name__)
PY

echo "=== HTTPS EGRESS TEST ==="
for host in api.ipify.org www.microsoft.com; do
    http=$(curl -4 -L -sS -o /dev/null -w '%{http_code}' --connect-timeout 5 --max-time 12 "https://$host/" 2>/dev/null)
    result=$?
    echo "$host: curl_exit=$result http_code=$http"
done

echo "=== TLS TARGET HANDSHAKE ==="
if command -v openssl >/dev/null 2>&1; then
    if timeout 12 openssl s_client -connect www.microsoft.com:443 \
        -servername www.microsoft.com -tls1_3 </dev/null \
        >/dev/null 2>&1; then
        echo "www.microsoft.com:443 TLS1.3 handshake: OK"
    else
        echo "www.microsoft.com:443 TLS1.3 handshake: FAIL"
    fi
fi

echo "=== RECENT MARZBAN/XRAY ERRORS (REDACTED) ==="
docker logs --tail 120 marzban-marzban-1 2>&1 \
    | grep -Ei '(reality|xray|timeout|error|handshake|denied|invalid|failed|panic|traceback)' \
    | sed -E \
       -e 's/[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4,12}/[UUID]/g' \
       -e 's/(token|password|secret|privateKey)[=:][^[:space:]]+/\1=[REDACTED]/Ig' \
       -e 's/[A-Za-z0-9_-]{43,100}/[REDACTED_LONG_STRING]/g' \
    | tail -n 25 || true
