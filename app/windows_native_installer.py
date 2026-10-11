"""Personalize the prebuilt native Windows installer without recompiling it.

PE executables permit arbitrary overlay data after the image. The installer
reads the final PRIVATE_LINE_BOOTSTRAP_V1 marker and JSON payload from its own
file. The payload contains only HTTPS control-plane origin + one-time pairing
code (no VLESS credential).
"""
from __future__ import annotations

import json
from pathlib import Path

MARKER = b"PRIVATE_LINE_BOOTSTRAP_V1:"
BASE_INSTALLER = Path("/app/data/windows_dist/PrivateLineSetup.exe")
MAX_BASE_SIZE = 250 * 1024 * 1024


def build_personal_windows_exe(*, base_url: str, pairing_code: str) -> bytes:
    if not BASE_INSTALLER.is_file():
        raise FileNotFoundError("Native Windows installer has not been built")
    raw = BASE_INSTALLER.read_bytes()
    if not raw.startswith(b"MZ") or len(raw) < 100_000 or len(raw) > MAX_BASE_SIZE:
        raise ValueError("Invalid native Windows installer artifact")
    if MARKER in raw[-64 * 1024:]:
        raise ValueError("Base installer unexpectedly contains personalization overlay")
    payload = json.dumps(
        {"api_base": base_url, "pair_code": pairing_code},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(payload) > 4096:
        raise ValueError("Windows personalization payload too large")
    return raw + MARKER + payload
