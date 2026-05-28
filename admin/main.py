from __future__ import annotations

import csv
import io
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import Depends, FastAPI, Form, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

from admin.auth import authenticate_admin, create_session_token
from admin.dependencies import get_current_admin
from admin.routes import (
    abuse,
    analytics,
    backups,
    bulk_actions,
    dashboard,
    filtering,
    health,
    integrations,
    load_analytics,
    logs,
    payments,
    promo_codes,
    referrals,
    refund_requests,
    releases,
    servers,
    subscriptions,
    tariffs,
    tickets,
    updates,
    users,
    vpn_keys,
    web_portal,
    withdrawals,
)
from app.database import get_async_session
from app.models.user import User
from app.payments.webhook import router as webhook_router
from prometheus_fastapi_instrumentator import Instrumentator
from sqlalchemy.ext.asyncio import AsyncSession


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    yield
    try:
        from app.payments.registry import close_all
        await close_all()
    except Exception:
        pass


app = FastAPI(title="Private Line Admin", docs_url=None, redoc_url=None, lifespan=lifespan)

try:
    Instrumentator().instrument(app).expose(app, endpoint="/metrics")
except Exception:
    pass  # metrics not critical — skip if prometheus fails


_error_templates = Jinja2Templates(directory="admin/templates")


@app.exception_handler(500)
async def internal_error_handler(request: Request, exc: Exception) -> HTMLResponse:
    import logging
    logging.getLogger(__name__).exception("Unhandled 500 error: %s %s", request.method, request.url)
    try:
        return _error_templates.TemplateResponse(
            "error.html",
            {"request": request, "code": 500, "message": "Внутренняя ошибка сервера. Подробности записаны в лог."},
            status_code=500,
        )
    except Exception:
        return HTMLResponse("<h1>500 — Internal Server Error</h1>", status_code=500)


@app.exception_handler(404)
async def not_found_handler(request: Request, exc: Exception) -> HTMLResponse:
    try:
        return _error_templates.TemplateResponse(
            "error.html",
            {"request": request, "code": 404, "message": "Страница не найдена."},
            status_code=404,
        )
    except Exception:
        return HTMLResponse("<h1>404 — Not Found</h1>", status_code=404)


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError) -> HTMLResponse | JSONResponse:
    if request.url.path.rstrip("/").endswith("/login"):
        _tmpl = Jinja2Templates(directory="admin/templates")
        return _tmpl.TemplateResponse(
            "login.html",
            {"request": request, "error": "Ошибка формы. Пожалуйста, войдите заново."},
            status_code=400,
        )
    return JSONResponse({"detail": str(exc)}, status_code=422)
templates = Jinja2Templates(directory="admin/templates")

app.include_router(dashboard.router)
app.include_router(users.router)
app.include_router(subscriptions.router)
app.include_router(payments.router)
app.include_router(vpn_keys.router)
app.include_router(servers.router)
app.include_router(servers.api_router)
app.include_router(tariffs.router)
app.include_router(referrals.router)
app.include_router(withdrawals.router)
app.include_router(refund_requests.router)
app.include_router(tickets.router)
app.include_router(analytics.router)
app.include_router(load_analytics.router)
app.include_router(health.router)
app.include_router(backups.router)
app.include_router(releases.router)
app.include_router(updates.router)
app.include_router(filtering.router)
app.include_router(abuse.router)
app.include_router(logs.router)
app.include_router(promo_codes.router)
app.include_router(bulk_actions.router)
app.include_router(integrations.router)
app.include_router(webhook_router)
app.include_router(web_portal.router)


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse("login.html", {"request": request, "error": None})


@app.post("/login", response_model=None)
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


@app.get("/_health")
async def health_check() -> dict:
    return {"status": "ok"}


@app.get("/export/users")
async def export_users_csv(
    session: AsyncSession = Depends(get_async_session),
    admin: User = Depends(get_current_admin),
) -> StreamingResponse:
    result = await session.execute(select(User).order_by(User.created_at.desc()))
    users = result.scalars().all()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["ID", "Telegram ID", "Username", "Name", "Status", "Created"])
    for u in users:
        writer.writerow([
            u.id,
            u.telegram_id,
            u.username or "",
            f"{u.first_name or ''} {u.last_name or ''}".strip(),
            u.status.value,
            u.created_at.strftime("%Y-%m-%d %H:%M"),
        ])

    output.seek(0)
    return StreamingResponse(
        io.BytesIO(output.getvalue().encode("utf-8-sig")),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=users.csv"},
    )


@app.post("/alertmanager-webhook")
async def alertmanager_webhook(request: Request) -> dict:
    """Receive Alertmanager alerts and forward to admin Telegram chat."""
    from app.services.notification import notify_admin

    try:
        data = await request.json()
        alerts = data.get("alerts", [])
        for alert in alerts:
            alert_status = alert.get("status", "firing")
            name = alert.get("labels", {}).get("alertname", "Unknown")
            summary = alert.get("annotations", {}).get("summary", "")
            emoji = "🔴" if alert_status == "firing" else "✅"
            await notify_admin(f"{emoji} Alert {alert_status.upper()}: {name}\n{summary}")
    except Exception:
        pass
    return {"status": "ok"}
