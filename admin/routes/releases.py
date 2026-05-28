from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from admin.dependencies import get_current_admin
from app.database import get_async_session
from app.models.admin_log import AdminLog
from app.models.release import Release
from app.models.user import User

router = APIRouter(prefix="/admin/releases")
templates = Jinja2Templates(directory="admin/templates")

_PAGE_SIZE = 30


@router.get("", response_class=HTMLResponse)
async def releases_list(
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    admin: User = Depends(get_current_admin),
) -> HTMLResponse:
    result = await session.execute(
        select(Release).order_by(Release.created_at.desc()).limit(_PAGE_SIZE)
    )
    releases = result.scalars().all()

    return templates.TemplateResponse("releases_list.html", {
        "request": request,
        "admin": admin,
        "releases": releases,
        "page_title": "Релизы",
    })


@router.get("/{release_id}", response_class=HTMLResponse)
async def release_detail(
    release_id: int,
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    admin: User = Depends(get_current_admin),
) -> HTMLResponse:
    release = await session.get(Release, release_id)
    if release is None:
        return HTMLResponse("<h1>404 — Release not found</h1>", status_code=404)

    return templates.TemplateResponse("releases_list.html", {
        "request": request,
        "admin": admin,
        "releases": [release],
        "detail": release,
        "page_title": f"Релиз #{release_id}",
    })


@router.post("/{release_id}/mark-current")
async def mark_current(
    release_id: int,
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    admin: User = Depends(get_current_admin),
) -> RedirectResponse:
    """Manually mark a release as current (for when deploy was done outside this UI)."""
    from sqlalchemy import update
    await session.execute(update(Release).values(is_current=False))
    release = await session.get(Release, release_id)
    if release:
        release.is_current = True

    log = AdminLog(
        admin_user_id=admin.id,
        action="mark_release_current",
        target_entity="release",
        target_entity_id=release_id,
        ip_address=request.client.host if request.client else None,
    )
    session.add(log)
    await session.commit()
    return RedirectResponse("/admin/releases", status_code=303)


@router.post("/create-manual")
async def create_manual_release(
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    admin: User = Depends(get_current_admin),
) -> JSONResponse:
    """Create a release record for the current state (for tracking manual deploys)."""
    import subprocess
    from datetime import datetime
    from sqlalchemy import update

    try:
        commit = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True, timeout=5
        ).strip()
    except Exception:
        commit = "unknown"

    try:
        prev = subprocess.check_output(
            ["git", "rev-parse", "HEAD~1"], text=True, timeout=5
        ).strip()
    except Exception:
        prev = ""

    try:
        changelog = subprocess.check_output(
            ["git", "log", "--oneline", f"{prev}..{commit}"],
            text=True, timeout=5
        ).strip() if prev else ""
    except Exception:
        changelog = ""

    await session.execute(update(Release).values(is_current=False))
    release = Release(
        version=f"{datetime.utcnow().strftime('%Y.%m.%d')}-{commit[:7]}",
        commit_hash=commit,
        previous_commit=prev,
        status="success",
        changelog=changelog,
        triggered_by=f"admin:{admin.username}",
        created_at=datetime.utcnow(),
        deployed_at=datetime.utcnow(),
        is_current=True,
    )
    session.add(release)
    log = AdminLog(
        admin_user_id=admin.id,
        action="create_manual_release",
        target_entity="release",
        ip_address=request.client.host if request.client else None,
    )
    session.add(log)
    await session.commit()

    return JSONResponse({"ok": True, "release_id": release.id, "version": release.version})
