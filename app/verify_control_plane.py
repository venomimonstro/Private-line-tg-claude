"""Verify control-plane dependencies without printing secrets."""
from __future__ import annotations

import asyncio
import httpx
from sqlalchemy import text

from app.config import settings
from app.database import async_session_maker


async def main() -> int:
    failures: list[str] = []
    async with async_session_maker() as db:
        try:
            await db.execute(text("SELECT 1"))
            print("PASS: PostgreSQL")
        except Exception as exc:
            failures.append(f"database:{type(exc).__name__}")

    async with httpx.AsyncClient(timeout=8.0, trust_env=False) as client:
        try:
            r = await client.get("http://127.0.0.1:8000/_health")
            if r.status_code != 200:
                failures.append(f"admin_http:{r.status_code}")
            else:
                print("PASS: FastAPI admin backend")
        except Exception as exc:
            failures.append(f"admin:{type(exc).__name__}")
        try:
            r = await client.get("http://nginx:29148/admin/login")
            if r.status_code != 200:
                failures.append(f"nginx_http:{r.status_code}")
            else:
                print("PASS: Nginx -> admin")
        except Exception as exc:
            failures.append(f"nginx:{type(exc).__name__}")
        try:
            r = await client.get(
                f"https://api.telegram.org/bot{settings.BOT_TOKEN}/getMe"
            )
            if r.status_code != 200 or not r.json().get("ok"):
                failures.append(f"telegram_http:{r.status_code}")
            else:
                print("PASS: Telegram bot token/API")
        except Exception as exc:
            failures.append(f"telegram:{type(exc).__name__}")

    if failures:
        print("CONTROL PLANE FAIL: " + ", ".join(failures))
        return 2
    print("CONTROL PLANE PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
