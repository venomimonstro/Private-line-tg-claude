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
    """Non-destructive duplicate cleanup.

    Never deletes subscriptions or VPN credentials automatically. Only stale
    duplicate records with no live/error key may be marked disabled.
    """
    from app.models.vpn_key import VpnKey

    dup_result = await session.execute(
        select(Subscription.user_id)
        .group_by(Subscription.user_id)
        .having(func.count() > 1)
    )
    user_ids = [row[0] for row in dup_result.all()]
    disabled = 0
    skipped = 0

    for user_id in user_ids:
        subs = list((await session.execute(
            select(Subscription)
            .options(selectinload(Subscription.vpn_keys))
            .where(Subscription.user_id == user_id)
            .order_by(Subscription.id.desc())
        )).scalars().all())
        if len(subs) <= 1:
            continue
        keep = next((
            sub for sub in subs
            if sub.status in (SubscriptionStatus.active, SubscriptionStatus.expires_soon)
        ), subs[0])
        for sub in subs:
            if sub.id == keep.id:
                continue
            live = any(
                key.status in (VpnKeyStatus.active, VpnKeyStatus.error)
                for key in sub.vpn_keys
            )
            if live:
                skipped += 1
                continue
            if sub.status not in (SubscriptionStatus.expired, SubscriptionStatus.disabled):
                sub.status = SubscriptionStatus.disabled
                disabled += 1

    log = AdminLog(
        admin_user_id=admin.id,
        action="cleanup_duplicate_subscriptions_safe",
        target_entity="subscription",
        target_entity_id=0,
        details={"users_checked": len(user_ids), "disabled": disabled, "skipped_live": skipped},
        ip_address=request.client.host if request.client else None,
    )
    session.add(log)
    await session.commit()
    return RedirectResponse(
        f"/admin/subscriptions?msg=Безопасно+отключено%3A+{disabled}%2C+с+живыми+ключами+пропущено%3A+{skipped}",
        status_code=303,
    )

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
    """Activate preserve-first: reconcile an existing credential or create only if absent."""
    from datetime import datetime as _dt, timedelta as _td
    from app.services.user_notifications import send_key_to_user
    from app.services.vpn_reconcile import reconcile_marzban_server

    sub = (await session.execute(
        select(Subscription)
        .options(selectinload(Subscription.tariff), selectinload(Subscription.vpn_keys))
        .where(Subscription.id == sub_id)
        .with_for_update()
    )).scalar_one_or_none()
    if sub is None:
        raise HTTPException(status_code=404)
    if sub.tariff is None:
        raise HTTPException(status_code=400, detail="Тариф не найден")

    active_keys = [k for k in sub.vpn_keys if k.status == VpnKeyStatus.active]
    if active_keys:
        for key in active_keys:
            check = await reconcile_marzban_server(
                session, key.server_id, apply=True, only_subscription_id=sub.id
            )
            if check.errors:
                raise HTTPException(
                    status_code=503,
                    detail="Существующий VPN-ключ не прошёл безопасную сверку; он не удалён.",
                )
        now = _dt.utcnow()
        sub.status = SubscriptionStatus.active
        sub.started_at = sub.started_at or now
        if sub.expires_at is None or sub.expires_at <= now:
            sub.expires_at = now + _td(days=sub.tariff.duration_days)
        sub.activation_attempts = 0
        vpn_key = active_keys[0]
        user = await session.get(User, sub.user_id)
        if user and user.telegram_id:
            await send_key_to_user(user.telegram_id, vpn_key)
    else:
        sub.activation_attempts = 0
        sub.status = SubscriptionStatus.pending_payment
        vpn_key = await activate_subscription(
            sub, sub.tariff, session, send_key_callback=send_key_to_user,
        )
        if vpn_key is None:
            raise HTTPException(
                status_code=503,
                detail="Ни одна проверенная VPN-нода не смогла выдать ключ.",
            )

    session.add(AdminLog(
        admin_user_id=admin.id,
        action="admin_activate_subscription_safe",
        target_entity="subscription",
        target_entity_id=sub_id,
        details={"preserved_existing": bool(active_keys), "tariff": sub.tariff.slug},
        ip_address=request.client.host if request.client else None,
    ))
    await session.commit()
    return RedirectResponse(f"/admin/subscriptions/{sub_id}", status_code=303)

@router.post("/{sub_id}/disable")
async def disable_subscription(
    sub_id: int,
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    admin: User = Depends(get_current_admin),
) -> RedirectResponse:
    sub = (await session.execute(
        select(Subscription)
        .options(selectinload(Subscription.vpn_keys))
        .where(Subscription.id == sub_id)
        .with_for_update()
    )).scalar_one_or_none()
    if sub is None:
        raise HTTPException(status_code=404)

    pending_remote = 0
    for key in sub.vpn_keys:
        if key.status == VpnKeyStatus.active:
            server = await session.get(VpnServer, key.server_id)
            if server is None:
                key.status = VpnKeyStatus.error
                pending_remote += 1
                continue
            disabled = await vpn_key_service.disable_vpn_key(key, server, session)
            if disabled:
                server.current_users = max(0, server.current_users - 1)
            else:
                pending_remote += 1

    # Hide access immediately in control-plane even if a remote retry is pending.
    sub.status = SubscriptionStatus.disabled
    session.add(AdminLog(
        admin_user_id=admin.id,
        action="disable_subscription",
        target_entity="subscription",
        target_entity_id=sub_id,
        details={"pending_remote_disables": pending_remote},
        ip_address=request.client.host if request.client else None,
    ))
    await session.commit()
    return RedirectResponse(f"/admin/subscriptions/{sub_id}", status_code=303)

@router.post("/{sub_id}/retry")
async def retry_activation(
    sub_id: int,
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    admin: User = Depends(get_current_admin),
) -> RedirectResponse:
    sub = (await session.execute(
        select(Subscription)
        .options(selectinload(Subscription.tariff), selectinload(Subscription.vpn_keys))
        .where(Subscription.id == sub_id)
        .with_for_update()
    )).scalar_one_or_none()
    if sub is None:
        raise HTTPException(status_code=404)
    if sub.tariff is None:
        raise HTTPException(status_code=400, detail="Tariff not found")

    from app.services.user_notifications import send_key_to_user
    from app.services.vpn_reconcile import reconcile_marzban_server

    active_keys = [k for k in sub.vpn_keys if k.status == VpnKeyStatus.active]
    if active_keys:
        for key in active_keys:
            result = await reconcile_marzban_server(
                session, key.server_id, apply=True, only_subscription_id=sub.id
            )
            if result.errors:
                raise HTTPException(
                    status_code=503,
                    detail="Существующий ключ не прошёл сверку; он сохранён без ротации.",
                )
        sub.status = SubscriptionStatus.active
        sub.activation_attempts = 0
        vpn_key = active_keys[0]
    else:
        sub.activation_attempts = 0
        sub.status = SubscriptionStatus.activation_error
        vpn_key = await activate_subscription(
            sub, sub.tariff, session, send_key_callback=send_key_to_user
        )
        if vpn_key is None:
            raise HTTPException(
                status_code=503,
                detail="Нет проверенной VPN-ноды для активации.",
            )

    session.add(AdminLog(
        admin_user_id=admin.id,
        action="retry_activation_safe",
        target_entity="subscription",
        target_entity_id=sub_id,
        details={"preserved_existing": bool(active_keys)},
        ip_address=request.client.host if request.client else None,
    ))
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

    key = (await session.execute(
        select(VpnKey).where(
            VpnKey.id == key_id,
            VpnKey.subscription_id == sub_id,
        ).with_for_update()
    )).scalar_one_or_none()
    if key is None:
        raise HTTPException(status_code=404)

    server = await session.get(VpnServer, key.server_id)
    if server is None:
        key.status = VpnKeyStatus.error
        await session.commit()
        raise HTTPException(
            status_code=409,
            detail="Запись VPN-ноды отсутствует; удалённое отключение не подтверждено.",
        )
    disabled = await vpn_key_service.disable_vpn_key(key, server, session)
    if disabled:
        server.current_users = max(0, server.current_users - 1)

    session.add(AdminLog(
        admin_user_id=admin.id,
        action="disable_vpn_key",
        target_entity="vpn_key",
        target_entity_id=key_id,
        details={"confirmed": bool(disabled)},
        ip_address=request.client.host if request.client else None,
    ))
    await session.commit()
    if not disabled:
        raise HTTPException(
            status_code=409,
            detail="Удалённое отключение пока не подтверждено; создан retry.",
        )
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
    from app.models.payment import Payment
    from app.models.vpn_key import VpnKey
    from sqlalchemy import delete as sa_delete

    sub = (await session.execute(
        select(Subscription)
        .options(selectinload(Subscription.vpn_keys))
        .where(Subscription.id == sub_id)
        .with_for_update()
    )).scalar_one_or_none()
    if sub is None:
        raise HTTPException(status_code=404)

    live = [
        key for key in sub.vpn_keys
        if key.status in (VpnKeyStatus.active, VpnKeyStatus.error)
    ]
    if live:
        raise HTTPException(
            status_code=409,
            detail="Нельзя удалить подписку с активным или неподтверждённо отключённым VPN-ключом.",
        )
    payment_exists = await session.scalar(
        select(Payment.id).where(Payment.subscription_id == sub_id).limit(1)
    )
    if payment_exists is not None:
        raise HTTPException(
            status_code=409,
            detail="Подписка связана с платежом и сохраняется для финансового аудита.",
        )

    await session.execute(sa_delete(VpnKey).where(VpnKey.subscription_id == sub_id))
    await session.delete(sub)
    session.add(AdminLog(
        admin_user_id=admin.id,
        action="delete_subscription_safe",
        target_entity="subscription",
        target_entity_id=sub_id,
        ip_address=request.client.host if request.client else None,
    ))
    await session.commit()
    return RedirectResponse("/admin/subscriptions", status_code=303)
