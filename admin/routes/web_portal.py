from __future__ import annotations

import hashlib
import logging
import random
from datetime import datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_async_session
from app.enums import PaymentStatus, ReferralStatus, SubscriptionStatus, UserStatus
from app.models.payment import Payment
from app.models.referral import Referral
from app.models.subscription import Subscription
from app.models.tariff import Tariff
from app.models.user import User
from app.services.vpn_key import get_access_url

router = APIRouter(prefix="/portal")
templates = Jinja2Templates(directory="admin/templates")
logger = logging.getLogger(__name__)

_STATUS_LABELS = {
    "created": "Создан",
    "pending": "Ожидание",
    "manual_review": "На проверке",
    "paid": "Оплачен",
    "failed": "Ошибка",
    "cancelled": "Отменён",
    "refunded": "Возврат",
}


def _make_fake_tg_id() -> int:
    """Synthetic negative telegram_id for web portal users."""
    return -abs(int(hashlib.sha256(str(random.getrandbits(64)).encode()).hexdigest(), 16) % (10 ** 12))


async def _attach_referral(
    user: User,
    ref: str,
    session: AsyncSession,
) -> None:
    """Look up referrer by username and create Referral + set referrer_id if not already set."""
    ref_username = ref.lstrip("@").strip()
    if not ref_username or user.referrer_id is not None:
        return

    referrer_result = await session.execute(
        select(User).where(User.username == ref_username)
    )
    referrer = referrer_result.scalar_one_or_none()
    if referrer is None or referrer.id == user.id:
        return

    # Don't create duplicate referral records
    existing_ref = await session.execute(
        select(Referral).where(Referral.invited_user_id == user.id)
    )
    if existing_ref.scalar_one_or_none() is not None:
        return

    user.referrer_id = referrer.id
    referral = Referral(
        referrer_id=referrer.id,
        invited_user_id=user.id,
        bonus_days_awarded=14,
        status=ReferralStatus.pending,
        created_at=datetime.utcnow(),
    )
    session.add(referral)
    await session.flush()
    logger.info("Web referral: user %d referred by %s (%d)", user.id, ref_username, referrer.id)


@router.get("/", response_class=HTMLResponse)
async def portal_index(
    request: Request,
    ref: str = Query(default=""),
    session: AsyncSession = Depends(get_async_session),
) -> HTMLResponse:
    result = await session.execute(
        select(Tariff)
        .where(Tariff.is_active == True, Tariff.is_visible == True)
        .order_by(Tariff.sort_order)
    )
    tariffs = result.scalars().all()
    return templates.TemplateResponse("portal/index.html", {
        "request": request, "tariffs": tariffs, "ref": ref,
    })


@router.get("/pay/{slug}", response_class=HTMLResponse)
async def portal_checkout(
    slug: str,
    request: Request,
    ref: str = Query(default=""),
    session: AsyncSession = Depends(get_async_session),
) -> HTMLResponse:
    result = await session.execute(
        select(Tariff).where(Tariff.slug == slug, Tariff.is_active == True, Tariff.is_visible == True)
    )
    tariff = result.scalar_one_or_none()
    if tariff is None:
        return templates.TemplateResponse("portal/index.html", {
            "request": request, "tariffs": [], "ref": "",
            "error": "Тариф не найден или недоступен.",
        }, status_code=404)
    return templates.TemplateResponse("portal/checkout.html", {
        "request": request, "tariff": tariff,
        "error": None, "prefill_email": None, "ref": ref,
    })


@router.post("/start/{slug}", response_model=None)
async def portal_start_payment(
    slug: str,
    request: Request,
    email: str = Form(...),
    first_name: str = Form(default=""),
    ref: str = Form(default=""),
    session: AsyncSession = Depends(get_async_session),
) -> RedirectResponse | HTMLResponse:
    from app.payments.services import create_payment

    result = await session.execute(
        select(Tariff).where(Tariff.slug == slug, Tariff.is_active == True, Tariff.is_visible == True)
    )
    tariff = result.scalar_one_or_none()
    if tariff is None:
        return RedirectResponse("/portal/", status_code=303)

    email = email.strip().lower()

    def checkout_error(msg: str) -> HTMLResponse:
        return templates.TemplateResponse("portal/checkout.html", {
            "request": request, "tariff": tariff,
            "error": msg, "prefill_email": email, "ref": ref,
        })

    if not email or "@" not in email:
        return checkout_error("Введите корректный email.")

    # Find or create web user by email
    existing_result = await session.execute(
        select(User).where(User.email == email, User.source == "web")
    )
    user = existing_result.scalar_one_or_none()
    is_new = user is None

    if is_new:
        tg_id = _make_fake_tg_id()
        while True:
            clash = await session.execute(select(User).where(User.telegram_id == tg_id))
            if clash.scalar_one_or_none() is None:
                break
            tg_id = _make_fake_tg_id()

        user = User(
            telegram_id=tg_id,
            email=email,
            first_name=first_name.strip() or None,
            source="web",
            status=UserStatus.active,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
            last_activity_at=datetime.utcnow(),
        )
        session.add(user)
        await session.flush()

    # Attach referral for new web users
    if is_new and ref:
        await _attach_referral(user, ref, session)

    try:
        payment, payment_result = await create_payment(
            user_id=user.id,
            tariff_slug=slug,
            session=session,
        )
        await session.commit()
    except Exception as exc:
        logger.error("Portal payment creation failed: %s", exc, exc_info=True)
        return checkout_error("Ошибка создания платежа. Попробуйте позже.")

    return RedirectResponse(f"/portal/waiting/{payment.id}", status_code=303)


@router.get("/waiting/{payment_id}", response_class=HTMLResponse)
async def portal_waiting(
    payment_id: int,
    request: Request,
    session: AsyncSession = Depends(get_async_session),
) -> HTMLResponse:
    result = await session.execute(
        select(Payment)
        .options(selectinload(Payment.tariff))
        .where(Payment.id == payment_id)
    )
    payment = result.scalar_one_or_none()
    if payment is None:
        return RedirectResponse("/portal/", status_code=303)

    return templates.TemplateResponse("portal/waiting.html", {
        "request": request,
        "payment_id": payment_id,
        "payment_url": payment.payment_url,
        "tariff_name": payment.tariff.name if payment.tariff else "—",
        "amount": payment.amount,
        "currency": payment.currency,
    })


@router.get("/api/status/{payment_id}")
async def portal_payment_status(
    payment_id: int,
    session: AsyncSession = Depends(get_async_session),
) -> JSONResponse:
    result = await session.execute(
        select(Payment)
        .options(
            selectinload(Payment.subscription).selectinload(Subscription.vpn_keys)
        )
        .where(Payment.id == payment_id)
    )
    payment = result.scalar_one_or_none()
    if payment is None:
        return JSONResponse({"status": "not_found"}, status_code=404)

    # For gift payments, check recipient's subscription
    if payment.gift_recipient_id is not None:
        gift_sub_result = await session.execute(
            select(Subscription)
            .options(selectinload(Subscription.vpn_keys))
            .where(Subscription.user_id == payment.gift_recipient_id)
            .order_by(Subscription.id.desc())
            .limit(1)
        )
        gift_sub = gift_sub_result.scalar_one_or_none()
    else:
        gift_sub = None

    status_val = payment.status.value if hasattr(payment.status, "value") else str(payment.status)
    response: dict = {
        "status": status_val,
        "status_label": _STATUS_LABELS.get(status_val, status_val),
    }

    # VPN key: check gift recipient sub first, then giver's sub
    active_sub = gift_sub or payment.subscription
    if status_val == "paid" and active_sub is not None:
        for key in active_sub.vpn_keys:
            if key.access_url_encrypted:
                try:
                    response["vpn_key"] = get_access_url(key)
                except Exception:
                    pass
                break

    return JSONResponse(response)
