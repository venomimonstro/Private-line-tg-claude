#!/usr/bin/env bash
# Ensure encrypted control-plane ingress exists even without a DNS name.
# Nodes pin the certificate SPKI, so a self-signed certificate cannot be
# silently replaced by a network attacker during node registration.
set -Eeuo pipefail
umask 077

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
[[ -f .env && -f docker-compose.yml ]] || {
  echo "FAIL: run from a valid Private Line checkout" >&2
  exit 2
}
command -v openssl >/dev/null || {
  echo "FAIL: openssl is required" >&2
  exit 2
}

get_env() {
  local key="$1"
  sed -n "s/^${key}=//p" .env | tail -1
}

PUBLIC_URL="$(get_env CONTROL_PLANE_PUBLIC_URL)"
if [[ -n "$PUBLIC_URL" ]]; then
  case "$PUBLIC_URL" in
    https://*) ;;
    *) echo "FAIL: CONTROL_PLANE_PUBLIC_URL must use https://" >&2; exit 2 ;;
  esac
fi

PUBLIC_IP=""
if [[ -z "$PUBLIC_URL" ]]; then
  PUBLIC_IP="$(curl -4 -q -fsS --max-time 8 https://api.ipify.org 2>/dev/null || true)"
  if ! PUBLIC_IP="$PUBLIC_IP" python3 - <<'PY'
import ipaddress, os
try:
    assert ipaddress.IPv4Address(os.environ["PUBLIC_IP"]).is_global
except Exception:
    raise SystemExit(1)
PY
  then
    PUBLIC_IP="$(hostname -I | tr ' ' '\n' | python3 -c '
import ipaddress, sys
for raw in sys.stdin:
    try:
        ip=ipaddress.ip_address(raw.strip())
        if isinstance(ip, ipaddress.IPv4Address) and ip.is_global:
            print(ip); break
    except ValueError:
        pass
' || true)"
  fi
  [[ -n "$PUBLIC_IP" ]] || {
    echo "FAIL: cannot determine global control-plane IPv4; set CONTROL_PLANE_PUBLIC_URL=https://..." >&2
    exit 2
  }
  PUBLIC_URL="https://${PUBLIC_IP}"
else
  PUBLIC_IP="$(PUBLIC_URL="$PUBLIC_URL" python3 - <<'PY'
import ipaddress, os
from urllib.parse import urlsplit
host=urlsplit(os.environ["PUBLIC_URL"]).hostname or ""
try:
    ip=ipaddress.ip_address(host)
    print(ip if isinstance(ip, ipaddress.IPv4Address) else "")
except ValueError:
    print("")
PY
)"
fi

mkdir -p nginx/ssl
CERT=nginx/ssl/cert.pem
KEY=nginx/ssl/key.pem

VALID=false
if [[ -s "$CERT" && -s "$KEY" ]]; then
  if openssl x509 -in "$CERT" -noout -checkend 604800 >/dev/null 2>&1 \
     && openssl pkey -in "$KEY" -noout >/dev/null 2>&1; then
    VALID=true
  fi
fi

if [[ "$VALID" != true ]]; then
  echo "Generating control-plane TLS certificate..."
  TMP="$(mktemp -d)"
  trap 'rm -rf "$TMP"' EXIT

  if [[ -n "$PUBLIC_IP" ]]; then
    cat >"$TMP/openssl.cnf" <<EOF
[req]
distinguished_name=req_dn
x509_extensions=v3
prompt=no
[req_dn]
CN=${PUBLIC_IP}
[v3]
subjectAltName=IP:${PUBLIC_IP}
keyUsage=digitalSignature,keyEncipherment
extendedKeyUsage=serverAuth
EOF
  else
    HOST="$(PUBLIC_URL="$PUBLIC_URL" python3 -c 'from urllib.parse import urlsplit; import os; print(urlsplit(os.environ["PUBLIC_URL"]).hostname or "")')"
    [[ -n "$HOST" ]] || { echo "FAIL: invalid CONTROL_PLANE_PUBLIC_URL" >&2; exit 2; }
    cat >"$TMP/openssl.cnf" <<EOF
[req]
distinguished_name=req_dn
x509_extensions=v3
prompt=no
[req_dn]
CN=${HOST}
[v3]
subjectAltName=DNS:${HOST}
keyUsage=digitalSignature,keyEncipherment
extendedKeyUsage=serverAuth
EOF
  fi

  openssl req -x509 -nodes -newkey rsa:3072 -days 365 \
    -keyout "$TMP/key.pem" -out "$TMP/cert.pem" \
    -config "$TMP/openssl.cnf" >/dev/null 2>&1
  install -m 600 "$TMP/key.pem" "$KEY"
  install -m 644 "$TMP/cert.pem" "$CERT"
  rm -rf "$TMP"
  trap - EXIT
fi

PIN="$(
  openssl x509 -in "$CERT" -pubkey -noout \
  | openssl pkey -pubin -outform DER 2>/dev/null \
  | openssl dgst -sha256 -binary \
  | base64 | tr -d '\n'
)"
[[ "$PIN" =~ ^[A-Za-z0-9+/]{43}=$ ]] || {
  echo "FAIL: cannot derive TLS SPKI pin" >&2
  exit 2
}
PIN="sha256//$PIN"

python3 - "$PUBLIC_URL" "$PIN" <<'PY'
from pathlib import Path
import sys

path=Path(".env")
url,pin=sys.argv[1:3]
updates={
    "CONTROL_PLANE_PUBLIC_URL": url,
    "CONTROL_PLANE_TLS_PIN": pin,
}
lines=path.read_text().splitlines()
seen=set()
out=[]
for line in lines:
    key=line.split("=",1)[0] if "=" in line else ""
    if key in updates:
        if key not in seen:
            out.append(f"{key}={updates[key]}")
            seen.add(key)
        continue
    out.append(line)
for key,val in updates.items():
    if key not in seen:
        out.append(f"{key}={val}")
path.write_text("\n".join(out)+"\n")
PY
chmod 600 .env

echo "TLS PASS: $PUBLIC_URL"
echo "Node registration SPKI pin stored in .env (value not printed)."
