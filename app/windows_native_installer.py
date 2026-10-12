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


def _has_bootstrap_overlay(tail: bytes) -> bool:
    """Distinguish a real appended JSON overlay from a PE string constant."""
    offset = tail.rfind(MARKER)
    if offset < 0:
        return False
    try:
        data = json.loads(tail[offset + len(MARKER):].decode("utf-8"))
    except (UnicodeDecodeError, ValueError, TypeError):
        return False
    return (
        isinstance(data, dict)
        and all(isinstance(data.get(k), str) for k in (
            "api_base", "pair_code", "tls_pin"
        ))
    )


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
        if _has_bootstrap_overlay(fh.read(tail_len)):
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


def open_personal_windows_exe(*, overlay: bytes, chunk_size: int = STREAM_CHUNK) -> tuple[int, Iterator[bytes]]:
    """Open one immutable on-disk snapshot before StreamingResponse is created.

    The file handle retains the original inode if the build atomically publishes
    a newer version during an active download. Content-Length always matches.
    """
    if not overlay.startswith(MARKER) or len(overlay) > len(MARKER) + 4096:
        raise ValueError("Invalid Windows personalization overlay")
    if not 64 * 1024 <= chunk_size <= 4 * 1024 * 1024:
        raise ValueError("Invalid installer stream chunk")
    fh = BASE_INSTALLER.open("rb")
    try:
        size = fh.seek(0, 2)
        if not 100_000 <= size <= MAX_BASE_SIZE:
            raise ValueError("Invalid native Windows installer artifact")
        fh.seek(0)
        if fh.read(2) != b"MZ":
            raise ValueError("Invalid native Windows installer artifact")
        fh.seek(max(0, size - 64 * 1024))
        if _has_bootstrap_overlay(fh.read()):
            raise ValueError("Base installer unexpectedly contains personalization overlay")
        fh.seek(0)
    except Exception:
        fh.close()
        raise

    def chunks() -> Iterator[bytes]:
        with fh:
            while True:
                data = fh.read(chunk_size)
                if not data:
                    break
                yield data
            yield overlay

    return size + len(overlay), chunks()


def build_personal_windows_exe(*, base_url: str, pairing_code: str, tls_pin: str) -> bytes:
    """Compatibility helper for tests/tools; production HTTP uses streaming."""
    overlay = build_personalization_overlay(
        base_url=base_url, pairing_code=pairing_code, tls_pin=tls_pin
    )
    return b"".join(iter_personal_windows_exe(overlay=overlay))
