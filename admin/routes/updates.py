from __future__ import annotations

import os
import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from admin.dependencies import get_current_admin, require_super_admin
from app.config import settings
from app.database import get_async_session, async_session_maker
from app.models.system_update import SystemUpdate
from app.models.user import User
from update_manager.installer import install_update
from update_manager.preflight import run_preflight_checks

router = APIRouter(prefix="/updates")
templates = Jinja2Templates(directory="admin/templates")

_UPLOAD_DIR = Path(settings.BACKUP_DIR) / "uploads"


@router.get("", response_class=HTMLResponse)
async def updates_page(
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    admin: User = Depends(get_current_admin),
) -> HTMLResponse:
    result = await session.execute(select(SystemUpdate).order_by(SystemUpdate.started_at.desc()).limit(20))
    updates = result.scalars().all()
    return templates.TemplateResponse("updates.html", {
        "request": request, "admin": admin, "updates": updates,
        "current_version": settings.APP_VERSION, "page_title": "Обновления",
    })


@router.post("/upload")
async def upload_update(
    request: Request,
    file: UploadFile = File(...),
    session: AsyncSession = Depends(get_async_session),
    admin: User = Depends(require_super_admin),
) -> RedirectResponse:
    # In-process ZIP updates cannot atomically restart both admin and bot,
    # and cannot safely roll PostgreSQL schema back from inside a DB-connected
    # process. Production updates are host-orchestrated through release scripts.
    raise HTTPException(
        status_code=409,
        detail=(
            "ZIP-обновления из админки отключены для безопасности. "
            "Используйте host-level git/release deployment."
        ),
    )

    if not file.filename or not file.filename.endswith(".zip"):
        raise HTTPException(status_code=400, detail="Файл должен иметь расширение .zip")

    _UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    tmp_path = _UPLOAD_DIR / file.filename

    content = await file.read()
    if len(content) > 500 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Файл превышает 500 МБ")

    with tmp_path.open("wb") as f:
        f.write(content)

    failures = await run_preflight_checks(str(tmp_path), session)
    if failures:
        tmp_path.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail="Preflight failed:\n" + "\n".join(failures))

    import asyncio
    asyncio.create_task(_run_install_in_background(str(tmp_path), admin.id))
    return RedirectResponse("/admin/updates", status_code=303)


async def _run_install_in_background(zip_path: str, admin_id: int) -> None:
    async with async_session_maker() as session:
        await install_update(zip_path, admin_id, session)


@router.get("/{update_id}/status")
async def update_status(
    update_id: int,
    session: AsyncSession = Depends(get_async_session),
    admin: User = Depends(get_current_admin),
) -> JSONResponse:
    result = await session.execute(select(SystemUpdate).where(SystemUpdate.id == update_id))
    update = result.scalar_one_or_none()
    if update is None:
        raise HTTPException(status_code=404)
    return JSONResponse({"id": update.id, "status": update.status.value, "error": update.error_message})
