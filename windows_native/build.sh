#!/usr/bin/env bash
# One offline-bundled Windows installer build per source revision.
# Do not remove a published installer until a fully validated replacement exists.
set -Eeuo pipefail
umask 077
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$ROOT/data/windows_dist"
CACHE="$ROOT/data/windows_cache"
WORK="$ROOT/windows_native"
PAYLOAD="$WORK/PrivateLine.Installer/Payload"
mkdir -p "$OUT" "$CACHE" "$PAYLOAD"
for cmd in curl unzip sha256sum dotnet; do
  command -v "$cmd" >/dev/null 2>&1 || { echo "FAIL: missing $cmd" >&2; exit 2; }
done
XRAY_VERSION=26.6.27
BASE="https://github.com/XTLS/Xray-core/releases/download/v${XRAY_VERSION}"
ZIP="$CACHE/Xray-windows-64-v${XRAY_VERSION}.zip"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"; rm -f "$PAYLOAD/xray.exe"' EXIT

echo "[windows 1/4] Verify official Xray archive checksum"
curl -fL --retry 3 --retry-delay 2 --connect-timeout 15 --max-time 60 \
  "$BASE/Xray-windows-64.zip.dgst" -o "$TMP/digest"
SHA="$(awk -F '=' 'tolower($1) ~ /^[[:space:]]*sha2-256[[:space:]]*$/ {gsub(/[[:space:]]/,"",$2); print tolower($2);exit}' "$TMP/digest")"
[[ "$SHA" =~ ^[a-f0-9]{64}$ ]] || { echo "FAIL: upstream SHA2-256 digest invalid" >&2; exit 2; }
if [[ ! -s "$ZIP" ]] || [[ "$(sha256sum "$ZIP" | awk '{print $1}')" != "$SHA" ]]; then
  curl -fL --retry 3 --retry-delay 2 --connect-timeout 15 --max-time 180 \
    "$BASE/Xray-windows-64.zip" -o "$TMP/xray.zip"
  [[ "$(stat -c%s "$TMP/xray.zip")" -gt 1000000 ]] || { echo "FAIL: tiny Xray archive" >&2; exit 2; }
  [[ "$(sha256sum "$TMP/xray.zip" | awk '{print $1}')" == "$SHA" ]] || { echo "FAIL: Xray archive checksum mismatch" >&2; exit 2; }
  install -m 600 "$TMP/xray.zip" "$ZIP"
fi
echo "[windows 2/4] Extract verified Xray"
unzip -q "$ZIP" -d "$TMP/xray"
BIN="$(find "$TMP/xray" -type f -iname xray.exe -print -quit)"
[[ -n "$BIN" && -s "$BIN" ]] || { echo "FAIL: xray.exe missing" >&2; exit 2; }
install -m 600 "$BIN" "$PAYLOAD/xray.exe"

echo "[windows 3/4] Compile native self-contained installer"
dotnet publish "$WORK/PrivateLine.Installer/PrivateLine.Installer.csproj" \
  -c Release -r win-x64 --self-contained true -o "$TMP/out" \
  -p:PublishSingleFile=true -p:DebugType=None -p:DebugSymbols=false
CANDIDATE="$TMP/out/PrivateLineSetup.exe"
[[ -s "$CANDIDATE" && "$(head -c2 "$CANDIDATE")" == "MZ" ]] || {
  echo "FAIL: Windows PE not produced" >&2; exit 2;
}
SIZE="$(stat -c%s "$CANDIDATE")"
(( SIZE >= 100000 && SIZE <= 250*1024*1024 )) || { echo "FAIL: PE size invalid" >&2; exit 2; }

echo "[windows 4/4] Publish verified EXE atomically"
STAGED="$OUT/.PrivateLineSetup.$$.new"
install -m 644 "$CANDIDATE" "$STAGED"
mv -f "$STAGED" "$OUT/PrivateLineSetup.exe"
echo "PASS: native Windows installer atomically built ($SIZE bytes)"
