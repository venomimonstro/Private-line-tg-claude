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
docker exec marzban-marzban-1 python -c 'import os, subprocess; subprocess.run([os.environ.get("XRAY_EXECUTABLE_PATH", "/usr/local/bin/xray"), "version"], check=True)' 2>/dev/null | head -n 1 || true
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
    print("outbounds:", [(x.get("tag"), x.get("protocol")) for x in config.get("outbounds", [])])
    print("routing_rules:", [
        {
            "outboundTag": x.get("outboundTag"),
            "network": x.get("network"),
            "ip": x.get("ip"),
            "domain": x.get("domain"),
        }
        for x in config.get("routing", {}).get("rules", [])
    ])
    print("DNS servers configured:", bool(config.get("dns", {}).get("servers")))
except (OSError, ValueError, TypeError) as exc:
    print("FAIL: config parse", type(exc).__name__)
PY

echo "=== NODE OUTBOUND ROUTES AND DNS ==="
ip -4 route get 1.1.1.1 2>/dev/null | head -1 || true
ip -6 route get 2606:4700:4700::1111 2>&1 | head -1 || true
for host in api.ipify.org www.cloudflare.com www.google.com; do
    echo "$host resolves IPv4:"
    getent ahostsv4 "$host" 2>/dev/null | awk 'NR==1 {print $1}' || true
done

echo "=== NODE DIRECT EGRESS (NOT THROUGH VPN) ==="
for entry in \
    "ipify https://api.ipify.org/" \
    "cloudflare https://www.cloudflare.com/cdn-cgi/trace" \
    "google https://www.google.com/generate_204" \
    "cloudflare-ip-http http://1.1.1.1/cdn-cgi/trace" \
    "google-http http://connectivitycheck.gstatic.com/generate_204"; do
    set -- $entry
    code="$(curl -q -4 -L -sS -o /dev/null -w '%{http_code}' \
        --noproxy '*' --connect-timeout 6 --max-time 14 "$2" 2>/dev/null)"
    rc=$?
    echo "$1: exit=$rc http=$code"
done

echo "=== NODE IPv6 EGRESS ==="
code="$(curl -q -6 -sS -o /dev/null -w '%{http_code}' \
    --noproxy '*' --connect-timeout 4 --max-time 8 \
    https://www.cloudflare.com/cdn-cgi/trace 2>/dev/null)"
echo "cloudflare IPv6: exit=$? http=$code"

echo "=== TLS TARGET HANDSHAKE ==="
TARGET_HOST="$(python3 - <<'PY'
import json
from pathlib import Path
try:
    cfg = json.loads(Path("/var/lib/marzban/xray_config.json").read_text())
    for inbound in cfg.get("inbounds", []):
        reality = inbound.get("streamSettings", {}).get("realitySettings", {})
        target = reality.get("target") or reality.get("dest") or ""
        if target:
            print(str(target).rsplit(":", 1)[0].strip("[]"))
            break
except Exception:
    pass
PY
)"
if command -v openssl >/dev/null 2>&1 && [[ -n "$TARGET_HOST" ]]; then
    TLS_LOG="$(mktemp)"
    if timeout 12 openssl s_client -connect "$TARGET_HOST:443" \
        -servername "$TARGET_HOST" -tls1_3 -alpn h2 </dev/null \
        >"$TLS_LOG" 2>&1; then
        echo "$TARGET_HOST:443 TLS1.3 handshake: OK"
        if grep -q 'ALPN protocol: h2' "$TLS_LOG"; then
            echo "$TARGET_HOST:443 ALPN h2: OK"
        else
            echo "$TARGET_HOST:443 ALPN h2: WARN/not negotiated"
        fi
    else
        echo "$TARGET_HOST:443 TLS1.3 handshake: FAIL"
    fi
    rm -f "$TLS_LOG"
else
    echo "openssl/Reality target unavailable"
fi

echo "=== RECENT MARZBAN/XRAY ERRORS (REDACTED) ==="
docker logs --tail 120 marzban-marzban-1 2>&1 \
    | grep -Ei '(reality|xray|timeout|error|handshake|denied|invalid|failed|panic|traceback)' \
    | sed -E \
       -e 's/[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4,12}/[UUID]/g' \
       -e 's/(token|password|secret|privateKey)[=:][^[:space:]]+/\1=[REDACTED]/Ig' \
       -e 's/[A-Za-z0-9_-]{43,100}/[REDACTED_LONG_STRING]/g' \
    | tail -n 25 || true

