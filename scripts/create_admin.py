"""Interactive script to create or update an admin account."""
from __future__ import annotations

import asyncio
import getpass
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import select

from admin.auth import hash_password
from app.database import async_session_maker
from app.enums import AdminRole, UserStatus
from app.models.user import User


async def create_admin() -> None:
    print("=== Private Line — Create Admin ===")

    try:
        telegram_id = int(input("Telegram user ID: ").strip())
    except ValueError:
        print("Error: Telegram ID must be an integer.")
        sys.exit(1)

    role_input = input(f"Role [{AdminRole.super_admin.value}/{AdminRole.support_admin.value}/{AdminRole.finance_admin.value}] (default: super_admin): ").strip()
    role = AdminRole(role_input) if role_input else AdminRole.super_admin

    password = getpass.getpass("Password: ")
    if len(password) < 8:
        print("Error: Password must be at least 8 characters.")
        sys.exit(1)
    confirm = getpass.getpass("Confirm password: ")
    if password != confirm:
        print("Error: Passwords do not match.")
        sys.exit(1)

    password_hash = hash_password(password)
    now = datetime.now(timezone.utc)

    async with async_session_maker() as session:
        result = await session.execute(
            select(User).where(User.telegram_id == telegram_id)
        )
        user = result.scalar_one_or_none()

        if user is None:
            user = User(
                telegram_id=telegram_id,
                status=UserStatus.new,
                admin_role=role,
                admin_password_hash=password_hash,
                admin_login_attempts=0,
                created_at=now,
                updated_at=now,
            )
            session.add(user)
            print(f"Created new user with admin role '{role.value}'.")
        else:
            user.admin_role = role
            user.admin_password_hash = password_hash
            user.admin_login_attempts = 0
            user.admin_locked_until = None
            user.updated_at = now
            print(f"Updated existing user {telegram_id} with admin role '{role.value}'.")

        await session.commit()

    print("Done.")


if __name__ == "__main__":
    asyncio.run(create_admin())
