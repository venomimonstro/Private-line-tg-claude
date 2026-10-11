#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$ROOT/data/windows_dist"
WORK="$ROOT/windows_native"
PAYLOAD="$WORK/PrivateLine.Installer/Payload"
mkdir -p "$OUT" "$PAYLOAD"
rm -f "$OUT/PrivateLineSetup.exe" "$PAYLOAD/PrivateLine.exe" "$PAYLOAD/xray.exe"

if ! command -v curl >/dev/null 2>&1 || ! command -v unzip >/dev/null 2>&1; then
  apt-get update -qq
  apt-get install -y -qq --no-install-recommends curl unzip ca-certificates >/dev/null
fi

dotnet publish "$WORK/PrivateLine.Client/PrivateLine.Client.csproj" -c Release -r win-x64 --self-contained false -o "$WORK/out-client" -p:PublishSingleFile=true -p:DebugType=None -p:DebugSymbols=false
cp "$WORK/out-client/PrivateLine.exe" "$PAYLOAD/PrivateLine.exe"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
XRAY_URL="https://github.com/XTLS/Xray-core/releases/download/v26.6.27/Xray-windows-64.zip"
curl -fL --retry 3 --connect-timeout 15 --max-time 180 "$XRAY_URL" -o "$TMP/xray.zip"
[[ "$(stat -c%s "$TMP/xray.zip")" -gt 1000000 ]] || { echo "FAIL: Xray archive is too small" >&2; exit 2; }
unzip -q "$TMP/xray.zip" -d "$TMP/xray"
XRAY_BIN="$(find "$TMP/xray" -type f -iname 'xray.exe' | head -1)"
[[ -n "$XRAY_BIN" && -s "$XRAY_BIN" ]] || { echo "FAIL: xray.exe missing from XTLS archive" >&2; exit 2; }
cp "$XRAY_BIN" "$PAYLOAD/xray.exe"

dotnet publish "$WORK/PrivateLine.Installer/PrivateLine.Installer.csproj" -c Release -r win-x64 --self-contained true -o "$WORK/out-installer" -p:PublishSingleFile=true -p:DebugType=None -p:DebugSymbols=false
cp "$WORK/out-installer/PrivateLineSetup.exe" "$OUT/PrivateLineSetup.exe"
test -s "$OUT/PrivateLineSetup.exe"
[[ "$(head -c2 "$OUT/PrivateLineSetup.exe")" == "MZ" ]] || { echo "FAIL: installer is not a Windows PE file" >&2; exit 2; }
echo "PASS: native Windows installer built: $(stat -c%s "$OUT/PrivateLineSetup.exe") bytes"
