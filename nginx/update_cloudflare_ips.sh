#!/bin/bash
# Fetch current Cloudflare IP ranges and regenerate cloudflare_ips.conf
# Run on the server: ./nginx/update_cloudflare_ips.sh
# Then reload nginx: docker compose exec nginx nginx -s reload

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
OUT="$SCRIPT_DIR/cloudflare_ips.conf"

IPV4=$(curl -sf https://www.cloudflare.com/ips-v4/)
IPV6=$(curl -sf https://www.cloudflare.com/ips-v6/)

if [[ -z "$IPV4" || -z "$IPV6" ]]; then
    echo "ERROR: failed to fetch Cloudflare IP ranges" >&2
    exit 1
fi

{
    echo "# Cloudflare IP ranges — generated $(date -u '+%Y-%m-%d %H:%M UTC')"
    echo "# Update: $0 && docker compose exec nginx nginx -s reload"
    echo ""
    echo "# IPv4"
    while IFS= read -r cidr; do
        [[ -n "$cidr" ]] && echo "allow $cidr;"
    done <<< "$IPV4"
    echo ""
    echo "# IPv6"
    while IFS= read -r cidr; do
        [[ -n "$cidr" ]] && echo "allow $cidr;"
    done <<< "$IPV6"
    echo ""
    echo "# loopback"
    echo "allow 127.0.0.1;"
    echo ""
    echo "deny all;"
} > "$OUT"

echo "Updated $OUT"
echo "Reload nginx: docker compose exec nginx nginx -s reload"
