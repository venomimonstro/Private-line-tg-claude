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


def build_chrome_extension_zip() -> bytes:
    """Standard extension-only archive with manifest.json at ZIP root.

    Extract this ZIP and choose its extracted directory in chrome://extensions
    -> Load unpacked. Chrome won't one-click install arbitrary external ZIP/CRX.
    """
    sources = tuple(item for item in FILES if item.startswith("extension/"))
    result = io.BytesIO()
    with zipfile.ZipFile(result, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=8) as archive:
        for relative in sources:
            payload = (ROOT / relative).read_bytes()
            archive.writestr(relative.removeprefix("extension/"), payload)
    return result.getvalue()
