from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)


@dataclass
class OutlineKey:
    id: str
    name: str
    access_url: str
    port: int | None


class OutlineClient:
    """Client for the Outline Server Management API."""

    def __init__(self, api_url: str) -> None:
        self._base_url = api_url.rstrip("/")
        self._client = httpx.AsyncClient(verify=False, timeout=15.0)

    async def _get(self, path: str) -> dict:
        response = await self._client.get(f"{self._base_url}{path}")
        response.raise_for_status()
        return response.json()

    async def _post(self, path: str, json: dict | None = None) -> dict:
        response = await self._client.post(f"{self._base_url}{path}", json=json or {})
        response.raise_for_status()
        return response.json()

    async def _put(self, path: str, json: dict) -> None:
        response = await self._client.put(f"{self._base_url}{path}", json=json)
        response.raise_for_status()

    async def _delete(self, path: str) -> None:
        response = await self._client.delete(f"{self._base_url}{path}")
        response.raise_for_status()

    async def create_key(self, name: str) -> OutlineKey:
        data = await self._post("/access-keys")
        key_id = str(data["id"])
        response = await self._client.put(
            f"{self._base_url}/access-keys/{key_id}/name",
            json={"name": name},
        )
        response.raise_for_status()
        return OutlineKey(
            id=key_id,
            name=name,
            access_url=data["accessUrl"],
            port=data.get("port"),
        )

    async def delete_key(self, key_id: str) -> None:
        await self._delete(f"/access-keys/{key_id}")

    async def set_data_limit(self, key_id: str, limit_bytes: int) -> None:
        await self._put(
            f"/access-keys/{key_id}/data-limit",
            json={"limit": {"bytes": limit_bytes}},
        )

    async def remove_data_limit(self, key_id: str) -> None:
        await self._delete(f"/access-keys/{key_id}/data-limit")

    async def get_transfer_stats(self) -> dict[str, int]:
        data = await self._get("/metrics/transfer")
        return {str(k): int(v) for k, v in data.get("bytesTransferredByUserId", {}).items()}

    async def get_server_info(self) -> dict:
        return await self._get("/server")

    async def list_keys(self) -> list[OutlineKey]:
        data = await self._get("/access-keys")
        return [
            OutlineKey(
                id=str(k["id"]),
                name=k.get("name", ""),
                access_url=k["accessUrl"],
                port=k.get("port"),
            )
            for k in data.get("accessKeys", [])
        ]

    async def check_connection(self) -> bool:
        try:
            await self.get_server_info()
            return True
        except Exception as exc:
            logger.warning("Outline API connection failed: %s", exc)
            return False

    async def aclose(self) -> None:
        await self._client.aclose()
