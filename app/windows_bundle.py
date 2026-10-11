"""Credential-free Windows client installation kit."""
from __future__ import annotations

import io
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "windows_client"
FILES = ("INSTALL-WINDOWS.cmd","Install-PrivateLine.ps1","PrivateLine.ps1","README_RU.md")


def build_windows_client() -> bytes:
    buffer=io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED, compresslevel=8) as archive:
        for relative in FILES:
            path=ROOT / relative
            payload=path.read_bytes()
            if len(payload)>512_000:
                raise ValueError("Client installation source too large")
            archive.writestr(relative,payload)
    return buffer.getvalue()
