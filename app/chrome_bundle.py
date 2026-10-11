"""Build a credential-free distributable Chrome extension + native Xray bridge."""
from __future__ import annotations

import io
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "chrome_client"

FILES = (
    "README_RU.md",
    "SETUP-WINDOWS.cmd",
    "extension/manifest.json",
    "extension/background.js",
    "extension/popup.html",
    "extension/popup.css",
    "extension/popup.js",
    "native/bridge.py",
    "native/install_windows.ps1",
    "native/install_unix.sh",
)


def build_chrome_bundle() -> bytes:
    """Generate user-downloadable ZIP from allowlisted tracked files only.

    No .env, user VLESS key, database, SSH key, or runtime state is packaged.
    """
    sink = io.BytesIO()
    with zipfile.ZipFile(sink, mode="w", compression=zipfile.ZIP_DEFLATED, compresslevel=8) as archive:
        for relative in FILES:
            path = ROOT / relative
            data = path.read_bytes()
            if len(data) > 512_000:
                raise ValueError(f"Chrome client source too large: {relative}")
            archive.writestr(relative, data)
    return sink.getvalue()
