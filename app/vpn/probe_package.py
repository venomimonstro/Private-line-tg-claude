"""Bounded official Xray Windows ZIP cache for one-time remote diagnostics."""
from __future__ import annotations

import hashlib
import os
import re
import tempfile
from pathlib import Path
from urllib.request import Request, urlopen

VERSION = "26.6.27"
BASE = f"https://github.com/XTLS/Xray-core/releases/download/v{VERSION}/"
CACHE = Path("data/windows_cache")
ZIP = CACHE / f"Xray-windows-64-v{VERSION}.zip"
DIGEST = CACHE / f"Xray-windows-64-v{VERSION}.sha256"
MAX_ZIP = 90 * 1024 * 1024


def _download(url: str, limit: int, timeout: int) -> bytes:
    with urlopen(Request(url, headers={"User-Agent": "PrivateLine-Probe/1"}), timeout=timeout) as response:
        if response.status != 200 or int(response.headers.get("Content-Length") or 0) > limit:
            raise ValueError("Unexpected upstream response")
        data = response.read(limit + 1)
    if len(data) > limit:
        raise ValueError("Upstream archive too large")
    return data


def verified_xray_archive() -> tuple[Path, str]:
    CACHE.mkdir(parents=True, exist_ok=True)
    if ZIP.is_file() and DIGEST.is_file():
        expected = DIGEST.read_text().strip().lower()
        if re.fullmatch(r"[a-f0-9]{64}", expected) and ZIP.stat().st_size <= MAX_ZIP:
            with ZIP.open("rb") as stream:
                actual = hashlib.file_digest(stream, "sha256").hexdigest()
            if actual == expected:
                return ZIP, expected

    digest = _download(BASE + "Xray-windows-64.zip.dgst", 4096, 20).decode("ascii")
    match = re.search(r"^\s*SHA2-256\s*=\s*([a-f0-9]{64})\s*$", digest, re.I | re.M)
    if not match:
        raise ValueError("Official Xray digest unavailable")
    expected = match.group(1).lower()
    archive = _download(BASE + "Xray-windows-64.zip", MAX_ZIP, 90)
    if len(archive) < 1000000 or hashlib.sha256(archive).hexdigest() != expected:
        raise ValueError("Official Xray ZIP checksum mismatch")
    fd, temporary = tempfile.mkstemp(prefix=".xray-probe-", dir=CACHE)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(archive)
        os.replace(temporary, ZIP)
        DIGEST.write_text(expected + "\n")
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return ZIP, expected
