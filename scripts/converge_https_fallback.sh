#!/usr/bin/env bash
# Called under the production/watchdog lock after node credentials are synced.
set -Eeuo pipefail
umask 077
cd "$(dirname "$0")/.."
[[ "${1:-}" == "" || "${1:-}" == "--changed-only" ]] || { echo "Usage: $0 [--changed-only]" >&2; exit 2; }
trap 'rm -f data/vpn_relay/healthy' ERR
STATE="$(docker compose exec -T admin python -m app.sync_https_fallback)"
mapfile -t FIELDS < <(STATE="$STATE" python3 - <<'PY'
import json,os
data=json.loads(os.environ['STATE'])
print('1' if data['changed'] else '0')
print(int(data['users']))
PY
)
[[ "${#FIELDS[@]}" -eq 2 ]] || { echo "FAIL: invalid HTTPS relay sync" >&2; exit 1; }
mkdir -p data/vpn_relay
if [[ "${FIELDS[1]}" -eq 0 ]]; then
  rm -f data/vpn_relay/healthy
  docker compose stop vpn-relay >/dev/null 2>&1 || true
  echo "SKIP: no paid users for HTTPS relay"
  exit 0
fi
if [[ "${1:-}" == "--changed-only" && "${FIELDS[0]}" -eq 0 \
      && -f data/vpn_relay/healthy ]]; then
  CID="$(docker compose ps -q vpn-relay 2>/dev/null || true)"
  if [[ -n "$CID" && "$(docker inspect -f '{{.State.Running}}' "$CID" 2>/dev/null || true)" == true ]]; then
    echo "PASS: paid HTTPS relay unchanged and running"
    exit 0
  fi
fi
rm -f data/vpn_relay/healthy
if [[ "${FIELDS[0]}" -eq 1 ]]; then
  docker run --rm --network none --user 0:0 --read-only --cap-drop ALL \
    --security-opt no-new-privileges \
    -v "$(pwd)/data/vpn_relay/config.json:/tmp/relay.json:ro" \
    ghcr.io/xtls/xray-core:26.6.27@sha256:c98160906f3fe1d9d16e950c2a7997dc0c542c71816aa92c21e510412f90e1a2 \
    run -test -c /tmp/relay.json >/dev/null \
    || { echo "FAIL: new HTTPS relay configuration rejected by Xray" >&2; exit 1; }
fi
if [[ "${FIELDS[0]}" -eq 1 ]]; then
  echo "HTTPS relay paid users changed; recreating only the relay"
  docker compose up -d --no-deps --force-recreate vpn-relay
else
  docker compose up -d --no-deps vpn-relay
fi
if ! bash scripts/probe_https_fallback.sh; then
  rm -f data/vpn_relay/healthy
  echo "FAIL: HTTPS fallback not advertised because the full VPN path failed" >&2
  exit 1
fi
touch data/vpn_relay/healthy
chmod 600 data/vpn_relay/healthy
