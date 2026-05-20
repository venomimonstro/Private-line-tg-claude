from __future__ import annotations

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from admin.auth import authenticate_admin, create_session_token
from admin.dependencies import get_current_admin
from admin.routes import (
    abuse,
    analytics,
    backups,
    dashboard,
    filtering,
    health,
    load_analytics,
    logs,
    payments,
    referrals,
    servers,
    subscriptions,
    tariffs,
    updates,
    users,
    vpn_keys,
)
from app.database import get_async_session
from app.models.user import User
from sqlalchemy.ext.asyncio import AsyncSession

app = FastAPI(title="Private Line Admin", docs_url=None, redoc_url=None)
templates = Jinja2Templates(directory="admin/templates")

app.include_router(dashboard.router)
app.include_router(users.router)
app.include_router(subscriptions.router)
app.include_router(payments.router)
app.include_router(vpn_keys.router)
app.include_router(servers.router)
app.include_router(tariffs.router)
app.include_router(referrals.router)
app.include_router(analytics.router)
app.include_router(load_analytics.router)
app.include_router(health.router)
app.include_router(backups.router)
app.include_router(updates.router)
app.include_router(filtering.router)
app.include_router(abuse.router)
app.include_router(logs.router)


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse("login.html", {"request": request, "error": None})


@app.post("/login")
async def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    session: AsyncSession = Depends(get_async_session),
) -> RedirectResponse | HTMLResponse:
    client_ip = request.client.host if request.client else ""
    user = await authenticate_admin(username, password, client_ip, session)

    if user is None:
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": "Неверный логин или пароль, либо аккаунт заблокирован."},
            status_code=401,
        )

    token = create_session_token(user.id)
    response = RedirectResponse("/admin/", status_code=303)
    response.set_cookie(
        key="admin_session",
        value=token,
        httponly=True,
        samesite="lax",
        secure=False,  # Set True when HTTPS is configured
        max_age=3600 * 24,
    )
    return response


@app.get("/logout")
async def logout() -> RedirectResponse:
    response = RedirectResponse("/admin/login", status_code=303)
    response.delete_cookie("admin_session")
    return response
