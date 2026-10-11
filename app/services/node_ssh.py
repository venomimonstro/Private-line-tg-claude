"""Validated encrypted SSH access for dedicated VPN nodes."""
from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass

from app.encryption import decrypt, encrypt
from app.models.vpn_server import VpnServer

_HOST_RE = re.compile(r"^(?=.{1,253}$)(?:[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?)$")
_USER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,63}$")
_AUTH_TYPES = {"password", "private_key"}


@dataclass(frozen=True)
class NodeSshAccess:
    host: str
    port: int
    username: str
    auth_type: str
    secret: str


def normalize_host(value: str) -> str:
    host = (value or "").strip().strip("[]")
    if not host:
        raise ValueError("SSH host is required")
    try:
        ipaddress.ip_address(host)
        return host
    except ValueError:
        pass
    if not _HOST_RE.fullmatch(host) or ".." in host:
        raise ValueError("Invalid SSH host")
    return host.lower()


def validate_username(value: str) -> str:
    username = (value or "").strip()
    if not _USER_RE.fullmatch(username):
        raise ValueError("Invalid SSH username")
    return username


def validate_port(value: int) -> int:
    port = int(value)
    if not 1 <= port <= 65535:
        raise ValueError("Invalid SSH port")
    return port


def validate_auth_type(value: str) -> str:
    auth_type = (value or "").strip()
    if auth_type not in _AUTH_TYPES:
        raise ValueError("SSH auth type must be password or private_key")
    return auth_type


def validate_secret(auth_type: str, value: str) -> str:
    secret = value or ""
    if not secret:
        raise ValueError("SSH secret is required")
    if "\x00" in secret:
        raise ValueError("SSH secret contains NUL")
    if auth_type == "private_key":
        stripped = secret.strip()
        if (
            not stripped.startswith("-----BEGIN ")
            or "PRIVATE KEY-----" not in stripped.splitlines()[0]
            or "-----END " not in stripped
        ):
            raise ValueError("Private key does not look like an OpenSSH/PEM private key")
        return stripped + "\n"
    return secret


def set_node_ssh_access(
    server: VpnServer,
    *,
    host: str,
    port: int,
    username: str,
    auth_type: str,
    secret: str,
) -> None:
    auth = validate_auth_type(auth_type)
    server.ssh_host = normalize_host(host)
    server.ssh_port = validate_port(port)
    server.ssh_username = validate_username(username)
    server.ssh_auth_type = auth
    server.ssh_secret_encrypted = encrypt(validate_secret(auth, secret))


def clear_node_ssh_access(server: VpnServer) -> None:
    server.ssh_host = None
    server.ssh_port = None
    server.ssh_username = None
    server.ssh_auth_type = None
    server.ssh_secret_encrypted = None


def get_node_ssh_access(server: VpnServer) -> NodeSshAccess:
    if not server.ssh_configured:
        raise ValueError("SSH access is not configured for this node")
    auth = validate_auth_type(server.ssh_auth_type or "")
    secret = decrypt(server.ssh_secret_encrypted or "")
    return NodeSshAccess(
        host=normalize_host(server.ssh_host or ""),
        port=validate_port(server.ssh_port or 0),
        username=validate_username(server.ssh_username or ""),
        auth_type=auth,
        secret=validate_secret(auth, secret),
    )
