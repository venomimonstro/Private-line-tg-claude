#!/usr/bin/env bash
# Trusted HTTPS for an IP-only admin endpoint. Run `install` once interactively;
# the installed systemd timer invokes `renew` twice daily thereafter.
set -Eeuo pipefail
umask 077
cd "$(dirname "$0")/.."
MODE="${1:-}"
[[ "$MODE" == install || "$MODE" == renew ]] || {
  echo "Usage: bash scripts/admin_public_tls.sh install|renew" >&2; exit 2;
}
[[ "$(id -u)" == 0 ]] || { echo "Run as root" >&2; exit 2; }
command -v docker >/dev/null
command -v openssl >/dev/null

PUBLIC_URL="$(sed -n 's/^CONTROL_PLANE_PUBLIC_URL=//p' .env | tail -1)"
IP="$(PUBLIC_URL="$PUBLIC_URL" python3 - <<'PY'
from urllib.parse import urlsplit
import ipaddress, os
u = urlsplit(os.environ['PUBLIC_URL'])
try:
    ip = ipaddress.IPv4Address(u.hostname or '')
    assert ip.is_global and u.scheme == 'https' and u.port is None
    print(ip)
except (ValueError, AssertionError):
    raise SystemExit('CONTROL_PLANE_PUBLIC_URL must be https://<public IPv4>')
PY
)"
mkdir -p data/acme-webroot/.well-known/acme-challenge data/letsencrypt nginx/ssl
# Only this webroot contains public ACME tokens. The script's umask 077 is
# retained for certificates and private keys, but Nginx workers must traverse
# the challenge directories and read the individual token file.
chmod 755 data/acme-webroot data/acme-webroot/.well-known data/acme-webroot/.well-known/acme-challenge
IMAGE=certbot/certbot:v5.4.0
CERT="data/letsencrypt/live/$IP/fullchain.pem"
KEY="data/letsencrypt/live/$IP/privkey.pem"
WEBROOT="$PWD/data/acme-webroot"
CONFIG="$PWD/data/letsencrypt"

challenge_response() {
  local attempt answer
  for attempt in 1 2 3 4 5; do
    answer="$(curl -q --noproxy '*' --resolve "$IP:80:127.0.0.1" \
        --max-time 3 -fsS "$challenge_url" 2>/dev/null || true)"
    if [[ "$answer" == "$probe" ]]; then printf '%s' "$answer"; return 0; fi
    sleep 1
  done
  return 1
}

if [[ "$MODE" == install ]]; then
  docker compose up -d --no-deps nginx
  probe="admin-$(openssl rand -hex 12)"
  printf '%s' "$probe" >"$WEBROOT/.well-known/acme-challenge/$probe"
  chmod 644 "$WEBROOT/.well-known/acme-challenge/$probe"
  challenge_url="http://$IP/.well-known/acme-challenge/$probe"
  response="$(challenge_response || true)"
  if [[ "$response" != "$probe" ]]; then
    # A git pull can replace nginx.conf's inode while a long-running Docker
    # container still has the previous file mounted. `up` and `nginx -s reload`
    # cannot see the new ACME location in that case.
    echo "Refreshing Nginx config and ACME webroot mount after failed challenge check..."
    docker compose up -d --no-deps --force-recreate nginx
    docker compose exec -T nginx nginx -t
    response="$(challenge_response || true)"
  fi
  rm -f "$WEBROOT/.well-known/acme-challenge/$probe"
  [[ "$response" == "$probe" ]] || {
    echo "HTTP-01 challenge is unreachable on port 80; existing admin unchanged" >&2; exit 2;
  }
  echo "Certbot will ask you to accept Let's Encrypt terms. Existing HTTPS remains online."
  docker run --rm -it -v "$CONFIG:/etc/letsencrypt" -v "$WEBROOT:/webroot" \
    "$IMAGE" certonly --preferred-profile shortlived --webroot \
    --webroot-path /webroot --ip-address "$IP" --reuse-key
else
  [[ -s "$CERT" && -s "$KEY" ]] || { echo "No issued IP certificate" >&2; exit 2; }
  docker run --rm -v "$CONFIG:/etc/letsencrypt" -v "$WEBROOT:/webroot" \
    "$IMAGE" renew --non-interactive --cert-name "$IP" --reuse-key
fi

[[ -s "$CERT" && -s "$KEY" ]] || { echo "Certbot did not issue a certificate" >&2; exit 2; }
openssl x509 -in "$CERT" -noout -checkip "$IP" | grep -q 'does match'
openssl x509 -in "$CERT" -noout -checkend 86400 >/dev/null
openssl verify -purpose sslserver -untrusted "$CERT" -CApath /etc/ssl/certs "$CERT" >/dev/null
cert_pub="$(openssl x509 -in "$CERT" -pubkey -noout | openssl pkey -pubin -outform DER | openssl dgst -sha256)"
key_pub="$(openssl pkey -in "$KEY" -pubout -outform DER | openssl dgst -sha256)"
[[ "$cert_pub" == "$key_pub" ]] || { echo "TLS certificate and key do not match" >&2; exit 2; }

old="$(openssl x509 -in nginx/ssl/cert.pem -noout -fingerprint -sha256 2>/dev/null || true)"
new="$(openssl x509 -in "$CERT" -noout -fingerprint -sha256)"
if [[ "$old" != "$new" ]]; then
  backup="$(mktemp -d)"
  cp -p nginx/ssl/cert.pem nginx/ssl/key.pem "$backup/"
  install -m 644 "$CERT" nginx/ssl/cert.pem
  install -m 600 "$KEY" nginx/ssl/key.pem
  if ! docker compose exec -T nginx nginx -t \
     || ! docker compose exec -T nginx nginx -s reload \
     || ! curl -q --noproxy '*' --resolve "$IP:443:127.0.0.1" \
         --max-time 8 -fsS "https://$IP/admin/login" >/dev/null; then
    cp -p "$backup/cert.pem" "$backup/key.pem" nginx/ssl/
    docker compose exec -T nginx nginx -s reload || true
    rm -rf "$backup"
    echo "TLS activation failed; restored previous certificate" >&2
    exit 2
  fi
  rm -rf "$backup"
fi
bash scripts/ensure_control_plane_tls.sh >/dev/null

if [[ "$MODE" == install ]]; then
  cat >/etc/systemd/system/private-line-admin-tls.service <<EOF
[Unit]
Description=Renew Private Line public admin IP certificate
[Service]
Type=oneshot
WorkingDirectory=$PWD
ExecStart=/bin/bash $PWD/scripts/admin_public_tls.sh renew
EOF
  cat >/etc/systemd/system/private-line-admin-tls.timer <<'EOF'
[Unit]
Description=Renew Private Line admin TLS twice per day
[Timer]
OnCalendar=*-*-* 00,12:15:00
RandomizedDelaySec=30m
Persistent=true
[Install]
WantedBy=timers.target
EOF
  systemctl daemon-reload
  systemctl enable --now private-line-admin-tls.timer
fi
echo "PASS: trusted HTTPS certificate for $IP; renewal timer enabled"
