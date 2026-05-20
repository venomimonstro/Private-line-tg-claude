from __future__ import annotations

from fastapi import Cookie, Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from admin.auth import verify_session_token
from app.config import settings
from app.database import get_async_session
from app.enums import AdminRole
from app.models.user import User


async def get_current_admin(
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    admin_session: str | None = Cookie(default=None, alias="admin_session"),
) -> User:
    if settings.ADMIN_ALLOWED_IPS:
        client_ip = request.client.host if request.client else ""
        if client_ip not in settings.ADMIN_ALLOWED_IPS:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="IP not allowed")

    if admin_session is None:
        raise _redirect_to_login()

    user_id = verify_session_token(admin_session)
    if user_id is None:
        raise _redirect_to_login()

    result = await session.execute(
        select(User).where(User.id == user_id, User.is_admin == True)  # noqa: E712
    )
    user = result.scalar_one_or_none()
    if user is None:
        raise _redirect_to_login()

    return user


async def require_super_admin(
    admin: User = Depends(get_current_admin),
) -> User:
    if admin.admin_role != AdminRole.super_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Super admin required")
    return admin


def _redirect_to_login() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_307_TEMPORARY_REDIRECT,
        headers={"Location": "/admin/login"},
    )
