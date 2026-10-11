#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$ROOT/data/windows_dist"
WORK="$ROOT/windows_native"
mkdir -p "$OUT" "$WORK/PrivateLine.Installer/Payload"
rm -f "$OUT/PrivateLineSetup.exe" "$WORK/PrivateLine.Installer/Payload/PrivateLine.exe"
dotnet publish "$WORK/PrivateLine.Client/PrivateLine.Client.csproj" -c Release -r win-x64 --self-contained true -o "$WORK/out-client" -p:PublishSingleFile=true
cp "$WORK/out-client/PrivateLine.exe" "$WORK/PrivateLine.Installer/Payload/PrivateLine.exe"
dotnet publish "$WORK/PrivateLine.Installer/PrivateLine.Installer.csproj" -c Release -r win-x64 --self-contained true -o "$WORK/out-installer" -p:PublishSingleFile=true
cp "$WORK/out-installer/PrivateLineSetup.exe" "$OUT/PrivateLineSetup.exe"
test -s "$OUT/PrivateLineSetup.exe"
echo "PASS: Windows native installer built: $(stat -c%s "$OUT/PrivateLineSetup.exe") bytes"
