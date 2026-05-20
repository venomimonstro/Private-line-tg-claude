from __future__ import annotations

import logging
from datetime import datetime, timedelta

import bcrypt
from itsdangerous import BadSignature, SignatureExpired, TimestampSigner
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.enums import UserStatus
from app.models.admin_log import AdminLog
from app.models.user import User

logger = logging.getLogger(__name__)

_signer = TimestampSigner(settings.SECRET_KEY)


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt()).decode()


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode(), hashed.encode())
    except Exception:
        return False


def create_session_token(user_id: int) -> str:
    return _signer.sign(str(user_id)).decode()


def verify_session_token(token: str) -> int | None:
    try:
        value = _signer.unsign(
            token,
            max_age=settings.SESSION_TIMEOUT_MINUTES * 60,
        )
        return int(value.decode())
    except (SignatureExpired, BadSignature, ValueError):
        return None


async def authenticate_admin(
    login: str,
    password: str,
    ip: str,
    session: AsyncSession,
) -> User | None:
    result = await session.execute(
        select(User).where(User.username == login, User.is_admin == True)  # noqa: E712
    )
    user = result.scalar_one_or_none()

    log_entry = AdminLog(
        admin_id=user.id if user else None,
        action="login_attempt",
        details={"login": login, "success": False},
        ip_address=ip,
    )

    if user is None:
        session.add(log_entry)
        await session.commit()
        return None

    if user.admin_locked_until and user.admin_locked_until > datetime.utcnow():
        session.add(log_entry)
        await session.commit()
        return None

    if not user.admin_password_hash or not verify_password(password, user.admin_password_hash):
        user.admin_login_attempts += 1
        if user.admin_login_attempts >= settings.MAX_LOGIN_ATTEMPTS:
            user.admin_locked_until = datetime.utcnow() + timedelta(minutes=settings.LOGIN_LOCKOUT_MINUTES)
            logger.warning("Admin account %s locked after %d failed attempts", login, user.admin_login_attempts)
        session.add(log_entry)
        await session.flush()
        await session.commit()
        return None

    user.admin_login_attempts = 0
    user.admin_locked_until = None
    log_entry.details = {"login": login, "success": True}
    session.add(log_entry)
    await session.flush()
    await session.commit()
    return user
