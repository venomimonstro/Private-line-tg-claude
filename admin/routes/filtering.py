from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from admin.dependencies import get_current_admin
from app.database import get_async_session
from app.enums import DomainStatus
from app.models.admin_log import AdminLog
from app.models.blocked_domain import BlockedDomain
from app.models.blocked_ip import BlockedIp
from app.models.user import User

router = APIRouter(prefix="/filtering")
templates = Jinja2Templates(directory="admin/templates")


@router.get("", response_class=HTMLResponse)
async def filtering_page(
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    admin: User = Depends(get_current_admin),
) -> HTMLResponse:
    domains_result = await session.execute(
        select(BlockedDomain).order_by(BlockedDomain.created_at.desc()).limit(100)
    )
    domains = domains_result.scalars().all()
    ips_result = await session.execute(
        select(BlockedIp).order_by(BlockedIp.created_at.desc()).limit(100)
    )
    ips = ips_result.scalars().all()
    domain_count = (await session.execute(select(func.count(BlockedDomain.id)).where(
        BlockedDomain.status == DomainStatus.active))).scalar_one() or 0
    ip_count = (await session.execute(select(func.count(BlockedIp.id)).where(
        BlockedIp.status == DomainStatus.active))).scalar_one() or 0

    return templates.TemplateResponse("filtering.html", {
        "request": request, "admin": admin, "domains": domains, "ips": ips,
        "domain_count": domain_count, "ip_count": ip_count, "page_title": "Фильтрация трафика",
    })


@router.post("/domains/add")
async def add_domain(
    request: Request,
    domain: str = Form(...),
    category: str = Form(default="manual"),
    comment: str = Form(default=""),
    session: AsyncSession = Depends(get_async_session),
    admin: User = Depends(get_current_admin),
) -> RedirectResponse:
    domain = domain.strip().lower()
    existing = await session.execute(select(BlockedDomain).where(BlockedDomain.domain == domain))
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(status_code=400, detail="Домен уже в списке")
    record = BlockedDomain(domain=domain, category=category, source="manual", comment=comment or None)
    session.add(record)
    log = AdminLog(admin_id=admin.id, action="add_blocked_domain", details={"domain": domain},
                   ip_address=request.client.host if request.client else None)
    session.add(log)
    await session.commit()
    return RedirectResponse("/admin/filtering", status_code=303)


@router.post("/domains/{domain_id}/delete")
async def delete_domain(
    domain_id: int,
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    admin: User = Depends(get_current_admin),
) -> RedirectResponse:
    result = await session.execute(select(BlockedDomain).where(BlockedDomain.id == domain_id))
    record = result.scalar_one_or_none()
    if record is None:
        raise HTTPException(status_code=404)
    await session.delete(record)
    await session.commit()
    return RedirectResponse("/admin/filtering", status_code=303)


@router.post("/ips/add")
async def add_ip(
    request: Request,
    ip_or_cidr: str = Form(...),
    category: str = Form(default="manual"),
    comment: str = Form(default=""),
    session: AsyncSession = Depends(get_async_session),
    admin: User = Depends(get_current_admin),
) -> RedirectResponse:
    ip_or_cidr = ip_or_cidr.strip()
    existing = await session.execute(select(BlockedIp).where(BlockedIp.ip_or_cidr == ip_or_cidr))
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(status_code=400, detail="IP/CIDR уже в списке")
    record = BlockedIp(ip_or_cidr=ip_or_cidr, category=category, source="manual", comment=comment or None)
    session.add(record)
    log = AdminLog(admin_id=admin.id, action="add_blocked_ip", details={"ip": ip_or_cidr},
                   ip_address=request.client.host if request.client else None)
    session.add(log)
    await session.commit()
    return RedirectResponse("/admin/filtering", status_code=303)


@router.post("/ips/{ip_id}/delete")
async def delete_ip(
    ip_id: int,
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    admin: User = Depends(get_current_admin),
) -> RedirectResponse:
    result = await session.execute(select(BlockedIp).where(BlockedIp.id == ip_id))
    record = result.scalar_one_or_none()
    if record is None:
        raise HTTPException(status_code=404)
    await session.delete(record)
    await session.commit()
    return RedirectResponse("/admin/filtering", status_code=303)


@router.post("/import-blacklist")
async def import_blacklist(
    request: Request,
    category: str = Form(default="blacklist"),
    file: UploadFile = File(...),
    session: AsyncSession = Depends(get_async_session),
    admin: User = Depends(get_current_admin),
) -> RedirectResponse:
    content = await file.read()
    lines = content.decode(errors="replace").splitlines()
    added = 0
    for line in lines:
        domain = line.strip().lower()
        if not domain or domain.startswith("#"):
            continue
        existing = await session.execute(select(BlockedDomain).where(BlockedDomain.domain == domain))
        if existing.scalar_one_or_none() is None:
            session.add(BlockedDomain(domain=domain, category=category, source="import"))
            added += 1

    log = AdminLog(admin_id=admin.id, action="import_blacklist", details={"added": added, "category": category},
                   ip_address=request.client.host if request.client else None)
    session.add(log)
    await session.commit()
    return RedirectResponse("/admin/filtering", status_code=303)
