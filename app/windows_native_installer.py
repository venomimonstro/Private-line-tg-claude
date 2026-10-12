"""Personalize the prebuilt native Windows installer without recompiling it.

The large PE stays on disk and is streamed in fixed-size chunks. Only the tiny
one-time bootstrap overlay is built per user. This keeps concurrent Windows
downloads O(1) memory per connection instead of copying the full installer into
RAM for every request.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator

MARKER = b"PRIVATE_LINE_BOOTSTRAP_V1:"
BASE_INSTALLER = Path("/app/data/windows_dist/PrivateLineSetup.exe")
MAX_BASE_SIZE = 250 * 1024 * 1024
STREAM_CHUNK = 1024 * 1024


def _validate_base_installer() -> int:
    if not BASE_INSTALLER.is_file():
        raise FileNotFoundError("Native Windows installer has not been built")
    size = BASE_INSTALLER.stat().st_size
    if size < 100_000 or size > MAX_BASE_SIZE:
        raise ValueError("Invalid native Windows installer artifact")
    with BASE_INSTALLER.open("rb") as fh:
        if fh.read(2) != b"MZ":
            raise ValueError("Invalid native Windows installer artifact")
        tail_len = min(size, 64 * 1024)
        fh.seek(size - tail_len)
        if MARKER in fh.read(tail_len):
            raise ValueError("Base installer unexpectedly contains personalization overlay")
    return size


def build_personalization_overlay(*, base_url: str, pairing_code: str, tls_pin: str) -> bytes:
    payload = json.dumps(
        {"api_base": base_url, "pair_code": pairing_code, "tls_pin": tls_pin},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(payload) > 4096:
        raise ValueError("Windows personalization payload too large")
    return MARKER + payload


def personalized_installer_size(*, overlay: bytes) -> int:
    return _validate_base_installer() + len(overlay)


def iter_personal_windows_exe(*, overlay: bytes, chunk_size: int = STREAM_CHUNK) -> Iterator[bytes]:
    _validate_base_installer()
    if not overlay.startswith(MARKER) or len(overlay) > len(MARKER) + 4096:
        raise ValueError("Invalid Windows personalization overlay")
    if chunk_size < 64 * 1024 or chunk_size > 4 * 1024 * 1024:
        raise ValueError("Invalid Windows installer stream chunk")
    with BASE_INSTALLER.open("rb") as fh:
        while True:
            chunk = fh.read(chunk_size)
            if not chunk:
                break
            yield chunk
    yield overlay


def build_personal_windows_exe(*, base_url: str, pairing_code: str, tls_pin: str) -> bytes:
    """Compatibility helper for tests/tools; production HTTP uses streaming."""
    overlay = build_personalization_overlay(
        base_url=base_url, pairing_code=pairing_code, tls_pin=tls_pin
    )
    return b"".join(iter_personal_windows_exe(overlay=overlay))
