from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import delete as sa_delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from admin.dependencies import get_current_admin
from app.database import get_async_session
from app.enums import SubscriptionStatus, VpnKeyStatus
from app.models.admin_log import AdminLog
from app.models.subscription import Subscription
from app.models.tariff import Tariff
from app.models.user import User
from app.models.vpn_server import VpnServer
from app.services import vpn_key as vpn_key_service
from app.services.subscription import activate_subscription, extend_subscription

router = APIRouter(prefix="/subscriptions")
templates = Jinja2Templates(directory="admin/templates")

_PAGE_SIZE = 25


@router.get("", response_class=HTMLResponse)
async def list_subscriptions(
    request: Request,
    page: int = 1,
    status_filter: str = "",
    session: AsyncSession = Depends(get_async_session),
    admin: User = Depends(get_current_admin),
) -> HTMLResponse:
    query = select(Subscription)
    if status_filter:
        try:
            query = query.where(Subscription.status == SubscriptionStatus(status_filter))
        except ValueError:
            pass

    total_result = await session.execute(select(func.count()).select_from(query.subquery()))
    total = total_result.scalar_one() or 0

    offset = (page - 1) * _PAGE_SIZE
    result = await session.execute(
        query.options(selectinload(Subscription.tariff))
        .order_by(Subscription.created_at.desc()).offset(offset).limit(_PAGE_SIZE)
    )
    subscriptions = result.scalars().all()

    return templates.TemplateResponse("subscriptions_list.html", {
        "request": request, "admin": admin, "subscriptions": subscriptions,
        "page": page, "total": total, "page_size": _PAGE_SIZE,
        "status_filter": status_filter, "statuses": list(SubscriptionStatus),
        "page_title": "Подписки",
    })


@router.post("/cleanup-duplicates")
async def cleanup_duplicate_subscriptions(
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    admin: User = Depends(get_current_admin),
) -> RedirectResponse:
    """For every user with >1 subscription: keep the best one, delete the rest."""
    from app.models.vpn_key import VpnKey

    _STATUS_PRIORITY = {
        SubscriptionStatus.active: 0,
        SubscriptionStatus.expires_soon: 1,
        SubscriptionStatus.activation_error: 2,
        SubscriptionStatus.pending_payment: 3,
        SubscriptionStatus.expired: 4,
        SubscriptionStatus.disabled: 5,
    }

    dup_result = await session.execute(
        select(Subscription.user_id)
        .group_by(Subscription.user_id)
        .having(func.count() > 1)
    )
    user_ids = [row[0] for row in dup_result.all()]

    removed = 0
    for user_id in user_ids:
        subs_result = await session.execute(
            select(Subscription)
            .options(selectinload(Subscription.vpn_keys))
            .where(Subscription.user_id == user_id)
        )
        subs = list(subs_result.scalars().all())
        if len(subs) <= 1:
            continue

        best = min(subs, key=lambda s: (_STATUS_PRIORITY.get(s.status, 99), -s.id))
        for sub in subs:
            if sub.id == best.id:
                continue
            for key in sub.vpn_keys:
                if key.status == VpnKeyStatus.active:
                    srv = await session.get(VpnServer, key.server_id)
                    if srv:
                        try:
                            await vpn_key_service.disable_vpn_key(key, srv, session)
                            srv.current_users = max(0, srv.current_users - 1)
                        except Exception:
                            pass
            await session.execute(sa_delete(VpnKey).where(VpnKey.subscription_id == sub.id))
            await session.delete(sub)
            removed += 1

    log = AdminLog(
        admin_user_id=admin.id,
        action="cleanup_duplicate_subscriptions",
        target_entity="subscription",
        target_entity_id=0,
        details={"users_cleaned": len(user_ids), "removed": removed},
        ip_address=request.client.host if request.client else None,
    )
    session.add(log)
    await session.commit()
    return RedirectResponse(f"/admin/subscriptions?msg=Удалено+дубликатов%3A+{removed}", status_code=303)


@router.get("/{sub_id}", response_class=HTMLResponse)
async def subscription_detail(
    sub_id: int,
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    admin: User = Depends(get_current_admin),
) -> HTMLResponse:
    result = await session.execute(
        select(Subscription)
        .options(selectinload(Subscription.vpn_keys), selectinload(Subscription.tariff))
        .where(Subscription.id == sub_id)
    )
    sub = result.scalar_one_or_none()
    if sub is None:
        raise HTTPException(status_code=404)

    from app.encryption import decrypt as _decrypt
    from app.enums import VpnKeyStatus
    from app.models.vpn_server import VpnServer

    key_details = []
    for key in sub.vpn_keys:
        server = await session.get(VpnServer, key.server_id) if key.server_id else None
        access_url = ""
        if key.status == VpnKeyStatus.active and key.access_url_encrypted:
            try:
                access_url = _decrypt(key.access_url_encrypted)
            except Exception:
                pass
        key_details.append({
            "key": key,
            "server": server,
            "access_url": access_url,
        })

    tariffs_result = await session.execute(select(Tariff).order_by(Tariff.price_monthly))
    tariffs = tariffs_result.scalars().all()

    return templates.TemplateResponse("subscription_detail.html", {
        "request": request, "admin": admin, "sub": sub,
        "key_details": key_details,
        "tariffs": tariffs,
        "page_title": f"Подписка #{sub_id}",
    })


@router.post("/{sub_id}/extend")
async def extend_subscription_route(
    sub_id: int,
    request: Request,
    days: int = Form(...),
    session: AsyncSession = Depends(get_async_session),
    admin: User = Depends(get_current_admin),
) -> RedirectResponse:
    result = await session.execute(select(Subscription).where(Subscription.id == sub_id))
    sub = result.scalar_one_or_none()
    if sub is None:
        raise HTTPException(status_code=404)

    await extend_subscription(sub, days, session)
    log = AdminLog(
        admin_user_id=admin.id,
        action="extend_subscription",
        target_entity="subscription",
        target_entity_id=sub_id,
        details={"days": days},
        ip_address=request.client.host if request.client else None,
    )
    session.add(log)
    await session.commit()
    return RedirectResponse(f"/admin/subscriptions/{sub_id}", status_code=303)


@router.post("/{sub_id}/change-tariff")
async def change_tariff(
    sub_id: int,
    request: Request,
    tariff_id: int = Form(...),
    session: AsyncSession = Depends(get_async_session),
    admin: User = Depends(get_current_admin),
) -> RedirectResponse:
    result = await session.execute(
        select(Subscription).options(selectinload(Subscription.vpn_keys)).where(Subscription.id == sub_id)
    )
    sub = result.scalar_one_or_none()
    if sub is None:
        raise HTTPException(status_code=404)

    tariff = await session.get(Tariff, tariff_id)
    if tariff is None:
        raise HTTPException(status_code=400, detail="Tariff not found")

    old_tariff_id = sub.tariff_id
    sub.tariff_id = tariff.id

    log = AdminLog(
        admin_user_id=admin.id,
        action="change_tariff",
        target_entity="subscription",
        target_entity_id=sub_id,
        details={"old_tariff_id": old_tariff_id, "new_tariff_id": tariff.id, "new_tariff_slug": tariff.slug},
        ip_address=request.client.host if request.client else None,
    )
    session.add(log)
    await session.commit()
    return RedirectResponse(f"/admin/subscriptions/{sub_id}", status_code=303)


@router.post("/{sub_id}/activate")
async def admin_activate_subscription(
    sub_id: int,
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    admin: User = Depends(get_current_admin),
) -> RedirectResponse:
    """Force-activate any subscription: disable existing keys, create fresh key, send to user."""
    result = await session.execute(
        select(Subscription)
        .options(selectinload(Subscription.tariff), selectinload(Subscription.vpn_keys))
        .where(Subscription.id == sub_id)
    )
    sub = result.scalar_one_or_none()
    if sub is None:
        raise HTTPException(status_code=404)
    if sub.tariff is None:
        raise HTTPException(status_code=400, detail="Тариф не найден")

    from datetime import datetime as _dt
    from app.models.vpn_key import VpnKey

    # Disable all existing active keys cleanly
    for key in sub.vpn_keys:
        if key.status == VpnKeyStatus.active:
            srv = await session.get(VpnServer, key.server_id)
            if srv:
                try:
                    await vpn_key_service.disable_vpn_key(key, srv, session)
                    srv.current_users = max(0, srv.current_users - 1)
                except Exception:
                    key.status = VpnKeyStatus.disabled
                    key.disabled_at = _dt.utcnow()
            else:
                key.status = VpnKeyStatus.disabled
                key.disabled_at = _dt.utcnow()

    sub.activation_attempts = 0
    sub.status = SubscriptionStatus.pending_payment
    await session.flush()

    from app.services.user_notifications import send_key_to_user
    try:
        vpn_key = await activate_subscription(
            sub, sub.tariff, session, send_key_callback=send_key_to_user,
        )
    except Exception as exc:
        import logging as _logging
        _logging.getLogger(__name__).error("Admin activate sub %d: %s", sub_id, exc)
        raise HTTPException(status_code=503, detail=f"VPN сервер недоступен: {exc}")

    log = AdminLog(
        admin_user_id=admin.id,
        action="admin_activate_subscription",
        target_entity="subscription",
        target_entity_id=sub_id,
        details={"success": vpn_key is not None, "tariff": sub.tariff.slug},
        ip_address=request.client.host if request.client else None,
    )
    session.add(log)
    await session.commit()
    return RedirectResponse(f"/admin/subscriptions/{sub_id}", status_code=303)


@router.post("/{sub_id}/disable")
async def disable_subscription(
    sub_id: int,
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    admin: User = Depends(get_current_admin),
) -> RedirectResponse:
    result = await session.execute(
        select(Subscription).options(selectinload(Subscription.vpn_keys)).where(Subscription.id == sub_id)
    )
    sub = result.scalar_one_or_none()
    if sub is None:
        raise HTTPException(status_code=404)

    for key in sub.vpn_keys:
        if key.status == VpnKeyStatus.active:
            server_result = await session.execute(select(VpnServer).where(VpnServer.id == key.server_id))
            server = server_result.scalar_one_or_none()
            if server:
                await vpn_key_service.disable_vpn_key(key, server, session)
                server.current_users = max(0, server.current_users - 1)

    sub.status = SubscriptionStatus.disabled
    log = AdminLog(
        admin_user_id=admin.id,
        action="disable_subscription",
        target_entity="subscription",
        target_entity_id=sub_id,
        ip_address=request.client.host if request.client else None,
    )
    session.add(log)
    await session.commit()
    return RedirectResponse(f"/admin/subscriptions/{sub_id}", status_code=303)


@router.post("/{sub_id}/retry")
async def retry_activation(
    sub_id: int,
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    admin: User = Depends(get_current_admin),
) -> RedirectResponse:
    result = await session.execute(
        select(Subscription).options(selectinload(Subscription.tariff)).where(Subscription.id == sub_id)
    )
    sub = result.scalar_one_or_none()
    if sub is None:
        raise HTTPException(status_code=404)

    tariff = sub.tariff
    if tariff is None:
        raise HTTPException(status_code=400, detail="Tariff not found")

    # Reset attempt counter and circuit breakers so activation can proceed
    sub.activation_attempts = 0
    sub.status = SubscriptionStatus.activation_error
    await session.flush()

    from app.services.circuit_breaker import _registry, reset_circuit
    for sid in list(_registry.keys()):
        reset_circuit(sid)

    # Disable any existing active keys (may have empty URL from previous failed attempts)
    from app.enums import VpnKeyStatus
    from app.models.vpn_key import VpnKey
    keys_result = await session.execute(
        select(VpnKey).where(
            VpnKey.subscription_id == sub_id,
            VpnKey.status == VpnKeyStatus.active,
        )
    )
    from datetime import datetime as _dt
    for stale_key in keys_result.scalars().all():
        stale_key.status = VpnKeyStatus.disabled
        stale_key.disabled_at = _dt.utcnow()
    await session.flush()

    from app.services.user_notifications import send_key_to_user
    try:
        vpn_key = await activate_subscription(sub, tariff, session, send_key_callback=send_key_to_user)
    except Exception as exc:
        import logging as _logging
        _logging.getLogger(__name__).error("Retry activation sub %d failed: %s", sub_id, exc)
        raise HTTPException(status_code=503, detail=f"VPN сервер недоступен: {exc}")

    log = AdminLog(
        admin_user_id=admin.id,
        action="retry_activation",
        target_entity="subscription",
        target_entity_id=sub_id,
        details={"success": vpn_key is not None},
        ip_address=request.client.host if request.client else None,
    )
    session.add(log)
    await session.commit()
    return RedirectResponse(f"/admin/subscriptions/{sub_id}", status_code=303)


@router.post("/{sub_id}/keys/{key_id}/disable")
async def admin_disable_key(
    sub_id: int,
    key_id: int,
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    admin: User = Depends(get_current_admin),
) -> RedirectResponse:
    from app.models.vpn_key import VpnKey

    result = await session.execute(
        select(VpnKey).where(VpnKey.id == key_id, VpnKey.subscription_id == sub_id)
    )
    key = result.scalar_one_or_none()
    if key is None:
        raise HTTPException(status_code=404)

    server_result = await session.execute(select(VpnServer).where(VpnServer.id == key.server_id))
    server = server_result.scalar_one_or_none()
    if server:
        await vpn_key_service.disable_vpn_key(key, server, session)
        server.current_users = max(0, server.current_users - 1)
    else:
        from app.enums import VpnKeyStatus
        from datetime import datetime as _dt
        key.status = VpnKeyStatus.disabled
        key.disabled_at = _dt.utcnow()

    log = AdminLog(
        admin_user_id=admin.id,
        action="disable_vpn_key",
        target_entity="vpn_key",
        target_entity_id=key_id,
        ip_address=request.client.host if request.client else None,
    )
    session.add(log)
    await session.commit()
    return RedirectResponse(f"/admin/subscriptions/{sub_id}", status_code=303)


@router.post("/{sub_id}/keys/{key_id}/reissue")
async def admin_reissue_key(
    sub_id: int,
    key_id: int,
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    admin: User = Depends(get_current_admin),
) -> RedirectResponse:
    from app.models.vpn_key import VpnKey

    result = await session.execute(
        select(VpnKey).where(VpnKey.id == key_id, VpnKey.subscription_id == sub_id)
    )
    key = result.scalar_one_or_none()
    if key is None:
        raise HTTPException(status_code=404)

    sub_result = await session.execute(
        select(Subscription).options(selectinload(Subscription.tariff)).where(Subscription.id == sub_id)
    )
    sub = sub_result.scalar_one_or_none()
    if sub is None or sub.tariff is None:
        raise HTTPException(status_code=404)

    server_result = await session.execute(select(VpnServer).where(VpnServer.id == key.server_id))
    server = server_result.scalar_one_or_none()
    if server is None:
        raise HTTPException(status_code=404, detail="Server not found")

    try:
        new_key = await vpn_key_service.reissue_vpn_key(
            key, server, sub.tariff.slug, sub.tariff.traffic_limit_gb, session
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        import logging as _logging
        _logging.getLogger(__name__).error("Reissue key %d failed: %s", key_id, exc)
        raise HTTPException(status_code=503, detail=f"VPN сервер недоступен: {exc}")

    log = AdminLog(
        admin_user_id=admin.id,
        action="reissue_vpn_key",
        target_entity="vpn_key",
        target_entity_id=key_id,
        ip_address=request.client.host if request.client else None,
    )
    session.add(log)
    await session.commit()

    # Notify the user in Telegram with their new key
    if new_key is not None:
        from app.services.user_notifications import send_key_to_user
        try:
            user_result = await session.execute(
                select(User).where(User.id == new_key.user_id)
            )
            key_user = user_result.scalar_one_or_none()
            if key_user:
                await send_key_to_user(key_user.telegram_id, new_key)
        except Exception as _notify_exc:
            import logging as _logging
            _logging.getLogger(__name__).warning(
                "Failed to notify user after admin reissue: %s", _notify_exc
            )

    return RedirectResponse(f"/admin/subscriptions/{sub_id}", status_code=303)


@router.post("/{sub_id}/delete")
async def delete_subscription(
    sub_id: int,
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    admin: User = Depends(get_current_admin),
) -> RedirectResponse:
    from app.models.vpn_key import VpnKey
    from sqlalchemy import delete as sa_delete

    result = await session.execute(
        select(Subscription).options(selectinload(Subscription.vpn_keys)).where(Subscription.id == sub_id)
    )
    sub = result.scalar_one_or_none()
    if sub is None:
        raise HTTPException(status_code=404)

    if sub.status == SubscriptionStatus.active:
        raise HTTPException(status_code=400, detail="Нельзя удалить активную подписку. Сначала отключите её.")

    # Disable active keys on VPN server before removing from DB
    for key in sub.vpn_keys:
        if key.status == VpnKeyStatus.active:
            server_result = await session.execute(select(VpnServer).where(VpnServer.id == key.server_id))
            server = server_result.scalar_one_or_none()
            if server:
                try:
                    await vpn_key_service.disable_vpn_key(key, server, session)
                except Exception:
                    pass

    await session.execute(sa_delete(VpnKey).where(VpnKey.subscription_id == sub_id))
    await session.delete(sub)

    log = AdminLog(
        admin_user_id=admin.id,
        action="delete_subscription",
        target_entity="subscription",
        target_entity_id=sub_id,
        ip_address=request.client.host if request.client else None,
    )
    session.add(log)
    await session.commit()
    return RedirectResponse("/admin/subscriptions", status_code=303)
