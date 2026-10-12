#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$ROOT/data/windows_dist"
CACHE="$ROOT/data/windows_cache"
WORK="$ROOT/windows_native"
PAYLOAD="$WORK/PrivateLine.Installer/Payload"

mkdir -p "$OUT" "$CACHE" "$PAYLOAD"
rm -f "$PAYLOAD/xray.exe" "$OUT/PrivateLineSetup.exe.new"

if ! command -v curl >/dev/null 2>&1 \
   || ! command -v unzip >/dev/null 2>&1 \
   || ! command -v sha256sum >/dev/null 2>&1; then
  apt-get update -qq
  apt-get install -y -qq --no-install-recommends \
    curl unzip ca-certificates coreutils >/dev/null
fi

XRAY_VERSION="26.6.27"
XRAY_BASE="https://github.com/XTLS/Xray-core/releases/download/v${XRAY_VERSION}"
XRAY_URL="$XRAY_BASE/Xray-windows-64.zip"
XRAY_DGST_URL="$XRAY_BASE/Xray-windows-64.zip.dgst"
XRAY_CACHE="$CACHE/Xray-windows-64-v${XRAY_VERSION}.zip"
DGST_CACHE="$CACHE/Xray-windows-64-v${XRAY_VERSION}.zip.dgst"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

echo "[windows 1/4] Verify cached official Xray payload"
curl -fL --retry 3 --retry-delay 2 \
  --connect-timeout 15 --max-time 60 \
  "$XRAY_DGST_URL" -o "$TMP/xray.dgst"

EXPECTED_SHA256="$(
  grep -Ei 'sha2-256|sha-256|sha256' "$TMP/xray.dgst" \
    | grep -Eo '[A-Fa-f0-9]{64}' | head -n1 | tr 'A-F' 'a-f'
)"
if [[ ! "$EXPECTED_SHA256" =~ ^[a-f0-9]{64}$ ]]; then
  echo "FAIL: official Xray digest file does not expose SHA256" >&2
  cat "$TMP/xray.dgst" >&2
  exit 2
fi

CACHE_OK=false
if [[ -s "$XRAY_CACHE" ]]; then
  ACTUAL="$(sha256sum "$XRAY_CACHE" | awk '{print $1}')"
  if [[ "$ACTUAL" == "$EXPECTED_SHA256" ]]; then
    CACHE_OK=true
    echo "PASS: cached Xray archive checksum verified"
  fi
fi

if [[ "$CACHE_OK" != true ]]; then
  echo "Downloading official Xray $XRAY_VERSION once..."
  curl -fL --retry 3 --retry-delay 2 \
    --connect-timeout 15 --max-time 180 \
    "$XRAY_URL" -o "$TMP/xray.zip"
  [[ "$(stat -c%s "$TMP/xray.zip")" -gt 1000000 ]] || {
    echo "FAIL: Xray archive is too small" >&2
    exit 2
  }
  ACTUAL="$(sha256sum "$TMP/xray.zip" | awk '{print $1}')"
  [[ "$ACTUAL" == "$EXPECTED_SHA256" ]] || {
    echo "FAIL: Xray archive SHA256 mismatch" >&2
    exit 2
  }
  install -m 600 "$TMP/xray.zip" "$XRAY_CACHE"
fi
install -m 600 "$TMP/xray.dgst" "$DGST_CACHE"

echo "[windows 2/4] Extract and verify Xray binary"
unzip -q "$XRAY_CACHE" -d "$TMP/xray"
XRAY_BIN="$(find "$TMP/xray" -type f -iname 'xray.exe' | head -1)"
[[ -n "$XRAY_BIN" && -s "$XRAY_BIN" ]] || {
  echo "FAIL: xray.exe missing from XTLS archive" >&2
  exit 2
}
cp "$XRAY_BIN" "$PAYLOAD/xray.exe"

echo "[windows 3/4] Compile native client and installer"
dotnet build "$WORK/PrivateLine.Client/PrivateLine.Client.csproj" \
  -c Release -r win-x64 \
  -p:DebugType=None -p:DebugSymbols=false

rm -rf "$WORK/out-installer"
dotnet publish "$WORK/PrivateLine.Installer/PrivateLine.Installer.csproj" \
  -c Release -r win-x64 --self-contained true \
  -o "$WORK/out-installer" \
  -p:PublishSingleFile=true \
  -p:DebugType=None -p:DebugSymbols=false

echo "[windows 4/4] Verify final PE artifact"
cp "$WORK/out-installer/PrivateLineSetup.exe" "$OUT/PrivateLineSetup.exe.new"
test -s "$OUT/PrivateLineSetup.exe.new"
[[ "$(head -c2 "$OUT/PrivateLineSetup.exe.new")" == "MZ" ]] || {
  echo "FAIL: installer is not a Windows PE file" >&2
  exit 2
}
SIZE="$(stat -c%s "$OUT/PrivateLineSetup.exe.new")"
(( SIZE >= 100000 && SIZE <= 250*1024*1024 )) || {
  echo "FAIL: installer size is outside expected bounds: $SIZE" >&2
  exit 2
}
chmod 644 "$OUT/PrivateLineSetup.exe.new"
mv -f "$OUT/PrivateLineSetup.exe.new" "$OUT/PrivateLineSetup.exe"

echo "PASS: native Windows installer atomically published: $SIZE bytes"
