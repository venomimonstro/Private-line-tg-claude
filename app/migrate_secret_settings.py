"""Idempotently migrate sensitive AppSetting values to Fernet encryption.

Run inside the application container:
  python -m app.migrate_secret_settings
  python -m app.migrate_secret_settings --apply

No secret values are printed.
"""
from __future__ import annotations

import argparse
import asyncio

from sqlalchemy import select

from app.database import async_session_maker
from app.models.app_setting import AppSetting
from app.services.secret_settings import (
    SECRET_SETTING_KEYS,
    encode_setting_value,
    is_encrypted_setting_value,
)


async def run(*, apply: bool) -> int:
    async with async_session_maker() as session:
        rows = (await session.execute(
            select(AppSetting).where(AppSetting.key.in_(SECRET_SETTING_KEYS))
        )).scalars().all()

        legacy = [
            row for row in rows
            if row.value and not is_encrypted_setting_value(row.key, row.value)
        ]
        encrypted = [
            row for row in rows
            if row.value and is_encrypted_setting_value(row.key, row.value)
        ]

        print(
            f"SECRET SETTINGS: configured={len(rows)} "
            f"encrypted={len(encrypted)} legacy_plaintext={len(legacy)}"
        )

        if not legacy:
            print("PASS: no plaintext integration secrets remain in AppSetting")
            return 0

        if not apply:
            print("DRY RUN: rerun with --apply to encrypt legacy values")
            return 2

        for row in legacy:
            row.value = encode_setting_value(row.key, row.value or "")
        await session.commit()

        # Fail closed: verify the persisted representation, not only ORM state.
        verify_rows = (await session.execute(
            select(AppSetting).where(AppSetting.key.in_(SECRET_SETTING_KEYS))
        )).scalars().all()
        remaining = [
            row.key for row in verify_rows
            if row.value and not is_encrypted_setting_value(row.key, row.value)
        ]
        if remaining:
            await session.rollback()
            print(
                "FAIL: plaintext secret settings remain after migration: "
                + ",".join(sorted(remaining))
            )
            return 2

        print(f"PASS: encrypted {len(legacy)} integration secret setting(s)")
        return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(run(apply=args.apply)))
