from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from admin.dependencies import get_current_admin, require_super_admin
from app.database import get_async_session
from app.enums import BackupType
from app.models.admin_log import AdminLog
from app.models.backup import Backup
from app.models.user import User
from backup_manager.backup import create_backup, restore_backup

router = APIRouter(prefix="/backups")
templates = Jinja2Templates(directory="admin/templates")


@router.get("", response_class=HTMLResponse)
async def list_backups(
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    admin: User = Depends(get_current_admin),
) -> HTMLResponse:
    result = await session.execute(select(Backup).order_by(Backup.created_at.desc()))
    backups = result.scalars().all()
    return templates.TemplateResponse("backups_list.html", {
        "request": request, "admin": admin, "backups": backups, "page_title": "Бэкапы",
    })


@router.post("/create")
async def create_manual_backup(
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    admin: User = Depends(get_current_admin),
) -> RedirectResponse:
    try:
        backup = await create_backup(BackupType.manual, session, created_by=f"admin:{admin.id}")
        log = AdminLog(
            admin_user_id=admin.id,
            action="create_backup",
            target_entity="backup",
            target_entity_id=backup.id,
            ip_address=request.client.host if request.client else None,
        )
        session.add(log)
        await session.commit()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Backup failed: {exc}")
    return RedirectResponse("/admin/backups", status_code=303)


@router.post("/{backup_id}/restore")
async def restore_backup_route(
    backup_id: int,
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    admin: User = Depends(require_super_admin),
) -> RedirectResponse:
    try:
        await restore_backup(backup_id, session)
        log = AdminLog(
            admin_user_id=admin.id,
            action="restore_backup",
            target_entity="backup",
            target_entity_id=backup_id,
            ip_address=request.client.host if request.client else None,
        )
        session.add(log)
        await session.commit()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    return RedirectResponse("/admin/backups", status_code=303)


@router.get("/{backup_id}/download")
async def download_backup(
    backup_id: int,
    session: AsyncSession = Depends(get_async_session),
    admin: User = Depends(require_super_admin),
) -> FileResponse:
    result = await session.execute(select(Backup).where(Backup.id == backup_id))
    backup = result.scalar_one_or_none()
    if backup is None or not backup.file_path:
        raise HTTPException(status_code=404)
    from pathlib import Path
    if not Path(backup.file_path).exists():
        raise HTTPException(status_code=404, detail="Backup file not found on disk")
    return FileResponse(backup.file_path, filename=Path(backup.file_path).name)
