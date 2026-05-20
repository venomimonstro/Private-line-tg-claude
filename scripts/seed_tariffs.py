"""Idempotent tariff seeding. Inserts default tariffs if they don't exist."""
from __future__ import annotations

import asyncio
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import select

from app.database import async_session_maker
from app.models.tariff import Tariff

_DEFAULT_TARIFFS = [
    {
        "name": "Старт",
        "slug": "start",
        "description": "Базовый тариф. Первый месяц — 99 ₽.",
        "price_monthly": Decimal("299.00"),
        "price_first_month": Decimal("99.00"),
        "duration_days": 30,
        "traffic_limit_gb": 50,
        "max_keys": 1,
        "is_active": True,
        "is_visible": True,
        "sort_order": 1,
    },
    {
        "name": "Стандарт",
        "slug": "standard",
        "description": "Стандартный тариф без ограничений трафика.",
        "price_monthly": Decimal("499.00"),
        "price_first_month": None,
        "duration_days": 30,
        "traffic_limit_gb": None,
        "max_keys": 2,
        "is_active": True,
        "is_visible": True,
        "sort_order": 2,
    },
    {
        "name": "Про",
        "slug": "pro",
        "description": "Максимальный тариф. До 5 устройств.",
        "price_monthly": Decimal("799.00"),
        "price_first_month": None,
        "duration_days": 30,
        "traffic_limit_gb": None,
        "max_keys": 5,
        "is_active": True,
        "is_visible": True,
        "sort_order": 3,
    },
]


async def seed_tariffs() -> None:
    from datetime import datetime, timezone

    async with async_session_maker() as session:
        for data in _DEFAULT_TARIFFS:
            existing = await session.execute(
                select(Tariff).where(Tariff.slug == data["slug"])
            )
            if existing.scalar_one_or_none() is not None:
                continue

            now = datetime.now(timezone.utc)
            tariff = Tariff(
                **data,
                created_at=now,
                updated_at=now,
            )
            session.add(tariff)

        await session.commit()
    print("Tariffs seeded.")


if __name__ == "__main__":
    asyncio.run(seed_tariffs())
