#!/usr/bin/env bash
# Supported Linux/macOS native host installer. Requires Python3 and Xray 26.6.27.
set -Eeuo pipefail
[[ $# -ge 1 ]] || { echo 'Usage: bash native/install_unix.sh EXTENSION_ID [XRAY_PATH]' >&2; exit 2; }
ID="$1"
[[ "$ID" =~ ^[a-p]{32}$ ]] || { echo "Invalid Chrome extension ID" >&2; exit 2; }
command -v python3 >/dev/null || { echo "Python3 is required" >&2; exit 2; }
XRAY="${2:-$(command -v xray || true)}"
[[ -f "$XRAY" && -x "$XRAY" ]] || { echo 'Install Xray 26.6.27 and pass its absolute executable path as argument 2' >&2; exit 2; }
"$XRAY" version | head -n1 | grep -Eq '^Xray 26\.6\.27([[:space:]]|$)' || { echo 'Xray 26.6.27 required' >&2; exit 2; }
BASE="$HOME/.config/private-line-chrome"
mkdir -p "$BASE"
chmod 700 "$BASE"
install -m 700 "$(dirname "$0")/bridge.py" "$BASE/bridge.py"
printf '%s\n' "$(realpath "$XRAY")" > "$BASE/xray-path.txt"
chmod 600 "$BASE/xray-path.txt"
LAUNCHER="$BASE/host.sh"
printf '#!/bin/sh\nexec python3 "%s/bridge.py"\n' "$BASE" > "$LAUNCHER"
chmod 700 "$LAUNCHER"
if [[ "$(uname)" == "Darwin" ]]; then
    HOSTDIR="$HOME/Library/Application Support/Google/Chrome/NativeMessagingHosts"
else
    HOSTDIR="$HOME/.config/google-chrome/NativeMessagingHosts"
fi
mkdir -p "$HOSTDIR"
python3 - "$HOSTDIR/ru.privateline.chrome.json" "$LAUNCHER" "$ID" <<'PY'
import json,sys
out,path,extension_id=sys.argv[1:]
with open(out,"w",encoding="utf-8") as f:
    json.dump({"name":"ru.privateline.chrome","description":"Private Line Xray host",
               "path":path,"type":"stdio",
               "allowed_origins":["chrome-extension://"+extension_id+"/"]},f)
PY
chmod 600 "$HOSTDIR/ru.privateline.chrome.json"
echo "Installed. Restart Chrome and enter your VLESS link."
