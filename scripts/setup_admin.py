"""Create or reset the admin user using ADMIN_USERNAME / ADMIN_PASSWORD from env."""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import select

from admin.auth import hash_password
from app.database import async_session_maker
from app.enums import AdminRole, UserStatus
from app.models.user import User


async def main() -> None:
    username = os.environ.get("ADMIN_USERNAME", "").strip()
    password = os.environ.get("ADMIN_PASSWORD", "").strip()

    if not username or not password:
        print("ERROR: ADMIN_USERNAME and ADMIN_PASSWORD must be set in .env")
        sys.exit(1)

    if len(password) < 8:
        print("ERROR: ADMIN_PASSWORD must be at least 8 characters")
        sys.exit(1)

    pw_hash = hash_password(password)
    now = datetime.utcnow()

    async with async_session_maker() as session:
        result = await session.execute(
            select(User).where(User.username == username)
        )
        user = result.scalar_one_or_none()

        if user is not None:
            user.admin_role = AdminRole.super_admin
            user.admin_password_hash = pw_hash
            user.admin_login_attempts = 0
            user.admin_locked_until = None
            user.updated_at = now
            print(f"Updated existing user '{username}' → super_admin")
        else:
            # Use a reserved negative-range telegram_id for admin-only accounts
            result2 = await session.execute(
                select(User).where(User.telegram_id < 0).order_by(User.telegram_id)
            )
            existing_admins = result2.scalars().all()
            fake_tg_id = -1 - len(existing_admins)

            user = User(
                telegram_id=fake_tg_id,
                username=username,
                status=UserStatus.active,
                admin_role=AdminRole.super_admin,
                admin_password_hash=pw_hash,
                admin_login_attempts=0,
                created_at=now,
                updated_at=now,
            )
            session.add(user)
            print(f"Created admin user '{username}' (super_admin)")

        await session.commit()

    print(f"Done. Login at /admin/login with username='{username}'")


if __name__ == "__main__":
    asyncio.run(main())
