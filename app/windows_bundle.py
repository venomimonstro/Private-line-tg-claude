"""Credential-free Windows client installation kit."""
from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "windows_client"
FILES = ("INSTALL-WINDOWS.cmd", "Install-PrivateLine.ps1", "PrivateLine.ps1", "CHECK-INSTALL.cmd", "Check-Install.ps1", "Pair-PrivateLine.ps1", "README_RU.md")


def build_windows_client(*, base_url: str | None = None, pairing_code: str | None = None) -> bytes:
    if (base_url is None) != (pairing_code is None):
        raise ValueError("Both base_url and pairing_code must be set together")
    buffer=io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED, compresslevel=8) as archive:
        for relative in FILES:
            path=ROOT / relative
            payload=path.read_bytes()
            # Windows PowerShell 5.1 reads BOM-less .ps1 as a legacy code page.
            # Include UTF-8 BOM in the ZIP to preserve Russian UI messages.
            if relative.lower().endswith(".ps1") and not payload.startswith(bytes((0xEF, 0xBB, 0xBF))):
                payload=bytes((0xEF, 0xBB, 0xBF))+payload
            if len(payload)>512_000:
                raise ValueError("Client installation source too large")
            archive.writestr(relative,payload)
        if base_url is not None:
            archive.writestr("bootstrap.json", json.dumps({"api_base":base_url,"pair_code":pairing_code},separators=(",",":")))
    return buffer.getvalue()
