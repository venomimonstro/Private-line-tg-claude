"""Direct standalone Xray node backend.

No Marzban/panel/API sits between the control plane and the Xray process.
Every mutation is executed over the node's already-configured SSH access by
/opt/private-line-node/manage.py. The node manager validates the complete Xray
config, atomically replaces it, restarts Xray, waits for :443 and rolls back on
failure before returning success.
"""
from __future__ import annotations

import asyncio
import json
import os
import shlex
import tempfile
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from app.models.vpn_server import VpnServer
from app.services.node_ssh import get_node_ssh_access


@dataclass(frozen=True)
class DirectXrayKey:
    id: str
    name: str
    access_url: str
    port: int | None = None


class DirectXrayError(RuntimeError):
    pass


class DirectXrayClient:
    def __init__(self, server: VpnServer):
        self.server = server
        self.access = get_node_ssh_access(server)

    async def aclose(self) -> None:
        return None

    async def _run(self, *args: str, timeout: int = 45) -> dict:
        access = self.access
        known_hosts = Path("/app/data/node_known_hosts")
        known_hosts.parent.mkdir(parents=True, exist_ok=True)

        env = os.environ.copy()
        key_path: str | None = None
        cmd: list[str] = []
        if access.auth_type == "password":
            env["SSHPASS"] = access.secret
            cmd.extend(["sshpass", "-e"])
        else:
            fd, key_path = tempfile.mkstemp(prefix="pl-node-key-", dir="/tmp")
            os.write(fd, access.secret.encode())
            os.close(fd)
            os.chmod(key_path, 0o600)

        cmd.extend([
            "ssh",
            "-o", "BatchMode=no",
            "-o", "ConnectTimeout=10",
            "-o", "ServerAliveInterval=10",
            "-o", "ServerAliveCountMax=2",
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", f"UserKnownHostsFile={known_hosts}",
            "-p", str(access.port),
        ])
        if key_path:
            cmd.extend(["-i", key_path])
        cmd.append(f"{access.username}@{access.host}")
        remote = " ".join(
            shlex.quote(x)
            for x in (
                "/usr/bin/python3",
                "/opt/private-line-node/manage.py",
                *args,
            )
        )
        cmd.append(remote)

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout
                )
            except asyncio.TimeoutError:
                proc.kill()
                await proc.communicate()
                raise DirectXrayError("direct Xray SSH command timed out")
            text = stdout.decode("utf-8", "replace").strip()
            if proc.returncode != 0:
                err = stderr.decode("utf-8", "replace").strip()
                # Never expose credentials or full remote shell diagnostics.
                raise DirectXrayError(
                    f"direct Xray node mutation failed ({proc.returncode}): "
                    f"{(err or text)[-300:]}"
                )
            try:
                payload = json.loads(text.splitlines()[-1])
            except Exception as exc:
                raise DirectXrayError("direct Xray node returned invalid JSON") from exc
            if payload.get("ok") is not True:
                raise DirectXrayError(
                    "direct Xray node rejected operation: "
                    + str(payload.get("error") or "unknown")
                )
            return payload
        finally:
            env.pop("SSHPASS", None)
            if key_path:
                try:
                    os.unlink(key_path)
                except FileNotFoundError:
                    pass

    async def check_connection(self) -> bool:
        try:
            out = await self._run("status", timeout=20)
            return bool(out.get("service_active") and out.get("listener"))
        except Exception:
            return False

    async def status(self) -> dict:
        return await self._run("status", timeout=20)

    async def create_key(
        self,
        *,
        name: str,
        traffic_limit_gb: int | None = None,
        ip_limit: int = 0,
        restart_core: bool = False,
    ) -> DirectXrayKey:
        del traffic_limit_gb, ip_limit, restart_core
        uid = str(uuid4())
        out = await self._run("add", name, uid)
        url = str(out.get("url") or "")
        if not url.startswith("vless://"):
            raise DirectXrayError("direct Xray node did not return VLESS URL")
        return DirectXrayKey(id=name, name=name, access_url=url)

    async def get_key(self, name: str) -> DirectXrayKey:
        out = await self._run("get", name, timeout=20)
        url = str(out.get("url") or "")
        if not url.startswith("vless://"):
            raise DirectXrayError("direct Xray node user has no VLESS URL")
        return DirectXrayKey(id=name, name=name, access_url=url)

    async def add_existing(self, name: str, uuid: str) -> DirectXrayKey:
        out = await self._run("add", name, uuid)
        url = str(out.get("url") or "")
        if not url.startswith("vless://"):
            raise DirectXrayError("direct Xray migration returned invalid URL")
        return DirectXrayKey(id=name, name=name, access_url=url)

    async def sync_users(self, users: dict[str, str]) -> dict:
        import base64
        raw = json.dumps(users, separators=(",", ":"), sort_keys=True).encode()
        payload = base64.urlsafe_b64encode(raw).decode().rstrip("=")
        return await self._run("sync", payload, timeout=60)

    async def disable_key(self, name: str, **_: object) -> None:
        await self._run("remove", name)

    async def delete_key(self, name: str) -> None:
        await self.disable_key(name)
