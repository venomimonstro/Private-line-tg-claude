from __future__ import annotations

import hashlib
import logging
import random
import secrets
from datetime import datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
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


_PROVIDER_META: dict[str, tuple[str, str, str]] = {
    # code → (icon, display_name, description)
    "yookassa":  ("🏦", "ЮKassa",    "Карта, СБП, ЮMoney"),
    "yoomoney":  ("💛", "ЮMoney",    "Кошелёк ЮMoney"),
    "freekassa": ("⚡", "FreeKassa", "Карта, криптовалюта"),
    "lava":      ("🔥", "LAVA",      "Карта, СБП"),
    "robokassa": ("💳", "Robokassa", "Карта, СБП"),
    "prodamus":  ("💳", "Prodamus",  "Карта"),
    "crypto":    ("₿",  "CryptoBot", "USDT, TON, BTC и другие"),
    "manual":    ("🏧", "Перевод",   "Реквизиты — ручное подтверждение"),
}


async def _web_providers(session) -> list[dict]:
    """Return enabled providers that work outside Telegram (no Stars)."""
    from app.payments.services import get_enabled_providers
    enabled = await get_enabled_providers(session)
    result = []
    for code in enabled:
        if code == "stars":
            continue
        icon, name, desc = _PROVIDER_META.get(code, ("💳", code, ""))
        result.append({"code": code, "icon": icon, "name": name, "desc": desc})
    return result


def _make_fake_tg_id() -> int:
    return -abs(int(hashlib.sha256(str(random.getrandbits(64)).encode()).hexdigest(), 16) % (10 ** 12))


def _bot_url() -> str:
    u = settings.BOT_USERNAME.lstrip("@") if settings.BOT_USERNAME else ""
    return f"https://t.me/{u}" if u else ""


def _portal_token() -> str:
    return secrets.token_urlsafe(32)


async def _attach_referral(user: User, ref: str, session: AsyncSession) -> None:
    ref_username = ref.lstrip("@").strip()
    if not ref_username or user.referrer_id is not None:
        return
    referrer_result = await session.execute(select(User).where(User.username == ref_username))
    referrer = referrer_result.scalar_one_or_none()
    if referrer is None or referrer.id == user.id:
        return
    existing_ref = await session.execute(select(Referral).where(Referral.invited_user_id == user.id))
    if existing_ref.scalar_one_or_none() is not None:
        return
    user.referrer_id = referrer.id
    referral = Referral(
        referrer_id=referrer.id, invited_user_id=user.id,
        bonus_days_awarded=14, status=ReferralStatus.pending, created_at=datetime.utcnow(),
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
        select(Tariff).where(Tariff.is_active == True, Tariff.is_visible == True).order_by(Tariff.sort_order)
    )
    tariffs = result.scalars().all()
    return templates.TemplateResponse("portal/index.html", {
        "request": request, "tariffs": tariffs, "ref": ref, "bot_url": _bot_url(),
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
            "request": request, "tariffs": [], "ref": "", "bot_url": _bot_url(),
            "error": "Тариф не найден или недоступен.",
        }, status_code=404)
    providers = await _web_providers(session)
    return templates.TemplateResponse("portal/checkout.html", {
        "request": request, "tariff": tariff, "bot_url": _bot_url(),
        "error": None, "prefill_email": None, "prefill_tg": None, "ref": ref,
        "providers": providers,
    })


@router.post("/start/{slug}", response_model=None)
async def portal_start_payment(
    slug: str,
    request: Request,
    email: str = Form(...),
    first_name: str = Form(default=""),
    tg_username: str = Form(default=""),
    provider: str = Form(default=""),
    ref: str = Form(default=""),
    session: AsyncSession = Depends(get_async_session),
) -> RedirectResponse | HTMLResponse:
    from app.payments.services import create_payment, get_enabled_providers

    result = await session.execute(
        select(Tariff).where(Tariff.slug == slug, Tariff.is_active == True, Tariff.is_visible == True)
    )
    tariff = result.scalar_one_or_none()
    if tariff is None:
        return RedirectResponse("/portal/", status_code=303)

    email = email.strip().lower()
    tg_clean = tg_username.lstrip("@").strip() if tg_username else ""
    providers = await _web_providers(session)

    def checkout_error(msg: str) -> HTMLResponse:
        return templates.TemplateResponse("portal/checkout.html", {
            "request": request, "tariff": tariff, "bot_url": _bot_url(),
            "error": msg, "prefill_email": email, "prefill_tg": tg_clean, "ref": ref,
            "providers": providers,
        })

    # Validate and resolve selected provider
    valid_codes = [p["code"] for p in providers]
    if provider not in valid_codes:
        provider = valid_codes[0] if valid_codes else "manual"

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
            username=tg_clean or None,
            first_name=first_name.strip() or None,
            source="web",
            status=UserStatus.active,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
            last_activity_at=datetime.utcnow(),
            portal_token=_portal_token(),
        )
        session.add(user)
        await session.flush()
    else:
        # Update username if provided and not already set
        if tg_clean and not user.username:
            user.username = tg_clean
        # Ensure portal_token exists
        if not user.portal_token:
            user.portal_token = _portal_token()

    # Attach referral for new web users
    if is_new and ref:
        await _attach_referral(user, ref, session)

    try:
        payment, payment_result = await create_payment(
            user_id=user.id,
            tariff_slug=slug,
            session=session,
            provider_name=provider,
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

    is_manual = (not payment.payment_url)
    return templates.TemplateResponse("portal/waiting.html", {
        "request": request,
        "payment_id": payment_id,
        "payment_url": payment.payment_url,
        "tariff_name": payment.tariff.name if payment.tariff else "—",
        "amount": payment.amount,
        "currency": payment.currency,
        "bot_url": _bot_url(),
        "is_manual": is_manual,
        "payment_instructions": settings.PAYMENT_INSTRUCTIONS if is_manual else "",
    })


@router.get("/api/status/{payment_id}")
async def portal_payment_status(
    payment_id: int,
    request: Request,
    session: AsyncSession = Depends(get_async_session),
) -> JSONResponse:
    result = await session.execute(
        select(Payment)
        .options(selectinload(Payment.subscription).selectinload(Subscription.vpn_keys))
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

    # VPN key + portal URL when paid
    active_sub = gift_sub or payment.subscription
    if status_val == "paid" and active_sub is not None:
        for key in active_sub.vpn_keys:
            if key.access_url_encrypted:
                try:
                    response["vpn_key"] = get_access_url(key)
                except Exception:
                    pass
                break
        # Include portal URL so user can save it
        user = await session.get(User, payment.user_id)
        if user is not None:
            if not user.portal_token:
                user.portal_token = _portal_token()
                await session.commit()
            base = str(request.base_url).rstrip("/")
            response["portal_url"] = f"{base}/portal/me/{user.portal_token}"
            response["bot_url"] = _bot_url()

    return JSONResponse(response)


# ── Login by email (for users who lost their portal link) ─────────────────────

@router.get("/login", response_class=HTMLResponse)
async def portal_login_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse("portal/login.html", {
        "request": request, "bot_url": _bot_url(), "error": None, "email": "",
    })


@router.post("/login", response_class=HTMLResponse, response_model=None)
async def portal_login_submit(
    request: Request,
    email: str = Form(...),
    session: AsyncSession = Depends(get_async_session),
) -> HTMLResponse | RedirectResponse:
    email = email.strip().lower()
    if not email or "@" not in email:
        return templates.TemplateResponse("portal/login.html", {
            "request": request, "bot_url": _bot_url(),
            "error": "Введите корректный email.", "email": email,
        })

    user_result = await session.execute(
        select(User).where(User.email == email)
    )
    user = user_result.scalar_one_or_none()
    if user is None:
        return templates.TemplateResponse("portal/login.html", {
            "request": request, "bot_url": _bot_url(),
            "error": "Аккаунт с таким email не найден.", "email": email,
        })

    if not user.portal_token:
        user.portal_token = _portal_token()
        await session.commit()

    return RedirectResponse(f"/portal/me/{user.portal_token}", status_code=303)


@router.get("/me/{token}", response_class=HTMLResponse)
async def portal_dashboard(
    token: str,
    request: Request,
    session: AsyncSession = Depends(get_async_session),
) -> HTMLResponse:
    from app.models.vpn_key import VpnKey
    from app.enums import SubscriptionStatus, VpnKeyStatus

    user_result = await session.execute(select(User).where(User.portal_token == token))
    user = user_result.scalar_one_or_none()
    if user is None:
        return templates.TemplateResponse("portal/index.html", {
            "request": request, "tariffs": [], "ref": "",
            "error": "Ссылка недействительна. Получите новую в Telegram-боте.",
        }, status_code=404)

    # Get active subscription
    sub_result = await session.execute(
        select(Subscription)
        .where(
            Subscription.user_id == user.id,
            Subscription.status.in_([
                SubscriptionStatus.active,
                SubscriptionStatus.expires_soon,
            ]),
        )
        .order_by(Subscription.expires_at.desc())
        .limit(1)
    )
    sub = sub_result.scalar_one_or_none()

    return templates.TemplateResponse("portal/dashboard.html", {
        "request": request,
        "token": token,
        "user": user,
        "has_subscription": sub is not None,
        "sub_expires": sub.expires_at.strftime("%d.%m.%Y") if sub and sub.expires_at else None,
        "sub_status": sub.status.value if sub else None,
    })


@router.post("/me/{token}/cmd")
async def portal_command(
    token: str,
    request: Request,
    session: AsyncSession = Depends(get_async_session),
) -> JSONResponse:
    from app.models.vpn_key import VpnKey
    from app.enums import SubscriptionStatus, VpnKeyStatus, PaymentStatus
    from app.models.tariff import Tariff
    from app.models.payment import Payment

    user_result = await session.execute(select(User).where(User.portal_token == token))
    user = user_result.scalar_one_or_none()
    if user is None:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    try:
        body = await request.json()
        cmd = body.get("cmd", "")
    except Exception:
        return JSONResponse({"error": "Bad request"}, status_code=400)

    _ACTIVE_STATUSES = [SubscriptionStatus.active, SubscriptionStatus.expires_soon]
    _STATUS_LABELS = {
        "active": "✅ Активна", "expires_soon": "⚠️ Истекает скоро",
        "expired": "❌ Истекла", "pending_payment": "⏳ Ожидает оплаты",
        "disabled": "🔒 Отключена", "blocked": "🚫 Заблокирована",
        "activation_error": "⚠️ Ошибка активации",
    }

    async def _active_sub():
        r = await session.execute(
            select(Subscription)
            .where(Subscription.user_id == user.id, Subscription.status.in_(_ACTIVE_STATUSES))
            .order_by(Subscription.expires_at.desc()).limit(1)
        )
        return r.scalar_one_or_none()

    async def _active_key(sub_id: int):
        r = await session.execute(
            select(VpnKey).where(VpnKey.subscription_id == sub_id, VpnKey.status == VpnKeyStatus.active)
        )
        return r.scalars().first()

    # ── 🔑 Мой ключ ──────────────────────────────────────────────────────────
    if cmd == "key":
        sub = await _active_sub()
        if sub is None:
            return JSONResponse({"text": "У вас нет активной подписки.\n\nНажмите «💳 Купить доступ» для оформления."})
        key = await _active_key(sub.id)
        if key is None:
            return JSONResponse({"text": "Ключ ещё создаётся. Подождите 1–2 минуты и нажмите снова."})
        try:
            access_url = get_access_url(key)
        except Exception:
            return JSONResponse({"text": "Ошибка получения ключа. Обратитесь в поддержку."})
        return JSONResponse({"type": "key", "key": access_url})

    # ── 🔑 Мой доступ ────────────────────────────────────────────────────────
    elif cmd == "access":
        sub_r = await session.execute(
            select(Subscription)
            .where(Subscription.user_id == user.id, Subscription.status.in_(_ACTIVE_STATUSES))
            .order_by(Subscription.expires_at.desc()).limit(1)
        )
        sub = sub_r.scalar_one_or_none()
        if sub is None:
            return JSONResponse({"text": "У вас нет активного доступа.\n\nНажмите «💳 Купить доступ» чтобы оформить подписку."})
        tariff = await session.get(Tariff, sub.tariff_id)
        key = await _active_key(sub.id)
        sl = _STATUS_LABELS.get(sub.status.value if hasattr(sub.status, "value") else str(sub.status), str(sub.status))
        expires = sub.expires_at.strftime("%d.%m.%Y") if sub.expires_at else "—"
        tariff_name = tariff.name if tariff else "—"
        traffic_limit = f"{tariff.traffic_limit_gb} ГБ/мес" if (tariff and tariff.traffic_limit_gb) else "Без ограничений"
        used_gb = f"{key.traffic_used_gb:.1f}" if key else "—"
        lines = (
            f"<b>Ваш доступ</b>\n\n"
            f"📋 Тариф: <b>{tariff_name}</b>\n"
            f"📊 Статус: {sl}\n"
            f"📅 До: <b>{expires}</b>\n"
            f"📦 Лимит: <b>{traffic_limit}</b>\n"
            f"📈 Использовано: {used_gb} ГБ"
        )
        resp: dict = {"html": lines.replace("\n", "<br>")}
        if key:
            try:
                resp["key"] = get_access_url(key)
                resp["type"] = "key"
                resp["text"] = lines
            except Exception:
                pass
        return JSONResponse(resp)

    # ── 📊 Статус подписки ───────────────────────────────────────────────────
    elif cmd == "status":
        sub_r = await session.execute(
            select(Subscription).where(Subscription.user_id == user.id)
            .order_by(Subscription.created_at.desc()).limit(1)
        )
        sub = sub_r.scalar_one_or_none()
        if sub is None:
            return JSONResponse({"text": "У вас ещё нет подписки."})
        sl = _STATUS_LABELS.get(sub.status.value if hasattr(sub.status, "value") else str(sub.status), str(sub.status))
        expires = sub.expires_at.strftime("%d.%m.%Y") if sub.expires_at else "—"
        return JSONResponse({"text": f"📋 Статус: {sl}\n📅 Действует до: {expires}"})

    # ── 📊 Проверить лимит ───────────────────────────────────────────────────
    elif cmd == "traffic":
        sub = await _active_sub()
        if sub is None:
            return JSONResponse({"text": "У вас нет активной подписки."})
        key = await _active_key(sub.id)
        if key is None:
            return JSONResponse({"text": "Активный ключ не найден."})
        tariff = await session.get(Tariff, sub.tariff_id)
        used = key.traffic_used_gb or 0.0
        limit = tariff.traffic_limit_gb if tariff else None
        if limit and limit > 0:
            pct = min(100.0, used / limit * 100)
            filled = int(pct // 10)
            bar = "▓" * filled + "░" * (10 - filled)
            text = (
                f"📊 <b>Использование трафика</b>\n\n"
                f"Использовано: <b>{used:.2f} ГБ</b> из {limit} ГБ\n"
                f"Осталось: <b>{max(0.0, limit - used):.2f} ГБ</b>\n"
                f"{bar} {pct:.1f}%\n\n"
                f"<i>Данные обновляются раз в 2 часа</i>"
            )
        else:
            text = f"📊 <b>Использование трафика</b>\n\nИспользовано: <b>{used:.2f} ГБ</b>\nЛимит: <b>Без ограничений</b>"
        return JSONResponse({"html": text.replace("\n", "<br>")})

    # ── 💳 Купить / Продлить ─────────────────────────────────────────────────
    elif cmd == "pay":
        tariffs_r = await session.execute(
            select(Tariff).where(Tariff.is_active == True, Tariff.is_visible == True).order_by(Tariff.sort_order)
        )
        tariffs = list(tariffs_r.scalars().all())
        if not tariffs:
            return JSONResponse({"text": "Тарифы временно недоступны."})
        buttons = []
        for t in tariffs:
            price = int(t.price_first_month) if t.price_first_month else int(t.price_monthly)
            label = f"{t.name} — {price} ₽"
            if t.price_first_month:
                label += f" (потом {int(t.price_monthly)} ₽)"
            buttons.append({"label": label, "url": f"/portal/pay/{t.slug}"})
        return JSONResponse({"text": "Выберите тариф:", "buttons": buttons})

    # ── 🆓 Пробный период ────────────────────────────────────────────────────
    elif cmd == "trial":
        # Check if user already has any subscription
        any_sub_r = await session.execute(
            select(Subscription).where(Subscription.user_id == user.id).limit(1)
        )
        if any_sub_r.scalar_one_or_none() is not None:
            return JSONResponse({"text": "⚠️ Пробный период доступен только новым пользователям.\n\nВы уже использовали подписку."})
        # Find tariff with trial
        trial_r = await session.execute(
            select(Tariff).where(Tariff.is_active == True, Tariff.trial_days.isnot(None)).order_by(Tariff.sort_order).limit(1)
        )
        tariff = trial_r.scalar_one_or_none()
        if tariff is None:
            return JSONResponse({"text": "Пробный период сейчас недоступен."})
        try:
            from app.services.promo import activate_tariff_trial
            sub = await activate_tariff_trial(user.id, tariff.slug, session)
            await session.commit()
            return JSONResponse({
                "text": f"🎉 Пробный период на {tariff.trial_days} дней активирован!\n\nНажмите «🔑 Мой ключ» чтобы получить ключ доступа."
            })
        except Exception as exc:
            logger.error("Portal trial failed for user %d: %s", user.id, exc, exc_info=True)
            return JSONResponse({"text": "❌ Не удалось активировать пробный период. Обратитесь в поддержку."})

    # ── 💳 Мои платежи ───────────────────────────────────────────────────────
    elif cmd == "payments":
        from sqlalchemy.orm import selectinload
        pay_r = await session.execute(
            select(Payment)
            .where(Payment.user_id == user.id)
            .options(selectinload(Payment.tariff), selectinload(Payment.subscription))
            .order_by(Payment.created_at.desc())
            .limit(10)
        )
        payments = pay_r.scalars().all()
        if not payments:
            return JSONResponse({"text": "У вас пока нет платежей."})
        _pstatus = {
            "created": "🆕 Создан", "pending": "⏳ Ожидает", "manual_review": "🔍 На проверке",
            "paid": "✅ Оплачен", "failed": "❌ Ошибка", "cancelled": "🚫 Отменён", "refunded": "↩️ Возврат",
        }
        lines = ["<b>💳 История платежей:</b><br>"]
        for p in payments:
            sv = p.status.value if hasattr(p.status, "value") else str(p.status)
            sl = _pstatus.get(sv, sv)
            tname = p.tariff.name if p.tariff else "—"
            dt = p.created_at.strftime("%d.%m.%Y")
            lines.append(f"{sl} &nbsp;<b>{dt}</b> — {tname} — {int(p.amount)} ₽")
        return JSONResponse({"html": "<br>".join(lines)})

    # ── 👥 Пригласить друга ──────────────────────────────────────────────────
    elif cmd == "referral":
        from decimal import Decimal as _D
        base = request.base_url
        portal_base = str(base).rstrip("/")
        ref_param = user.username or str(user.telegram_id)
        ref_link = f"{portal_base}/portal/?ref=@{ref_param}" if user.username else f"{portal_base}/portal/"
        balance = user.referral_balance or _D("0")
        html = (
            f"<b>👥 Реферальная программа</b><br><br>"
            f"Приглашайте друзей и получайте <b>10% от каждого их платежа</b> пожизненно!<br><br>"
            f"Ваша ссылка для приглашения:<br>"
            f"<div class='key-box' style='font-size:.8rem' onclick=\"navigator.clipboard.writeText('{ref_link}').then(()=>this.style.borderColor='var(--green)')\">{ref_link}</div>"
            f"<br>💰 Ваш баланс: <b>{balance:.2f} ₽</b><br>"
            f"<i>Для вывода минимум 500 ₽ — обратитесь через 🆘 Поддержку</i>"
        )
        return JSONResponse({"html": html})

    # ── 🔄 Новый ключ (с подтверждением) ─────────────────────────────────────
    elif cmd == "reissue":
        sub = await _active_sub()
        if sub is None:
            return JSONResponse({"text": "Нет активной подписки."})
        key = await _active_key(sub.id)
        if key is None:
            return JSONResponse({"text": "Активный ключ не найден."})
        from datetime import date as _date
        today = _date.today()
        used = key.reissue_count_today if key.reissue_date == today else 0
        if used >= 5:
            return JSONResponse({"text": "⛔ Лимит 5 обновлений в день исчерпан. Попробуйте завтра."})
        remaining = 5 - used
        return JSONResponse({
            "text": f"🔄 Обновить ключ доступа?\n\nТекущий ключ перестанет работать сразу.\nОсталось обновлений сегодня: {remaining}/5",
            "buttons": [
                {"label": "✅ Да, обновить", "cmd": "reissue_confirm"},
                {"label": "← Отмена", "cmd": "cancel"},
            ],
        })

    elif cmd == "reissue_confirm":
        from app.services.vpn_key import create_vpn_key
        from app.models.vpn_server import VpnServer
        from datetime import date as _date, datetime as _dt
        sub = await _active_sub()
        if sub is None:
            return JSONResponse({"text": "Нет активной подписки."})
        key = await _active_key(sub.id)
        if key is None:
            return JSONResponse({"text": "Активный ключ не найден."})
        today = _date.today()
        used = key.reissue_count_today if key.reissue_date == today else 0
        if used >= 5:
            return JSONResponse({"text": "⛔ Лимит исчерпан."})
        server = await session.get(VpnServer, key.server_id)
        if server is None:
            return JSONResponse({"text": "Сервер не найден. Обратитесь в поддержку."})
        tariff = await session.get(Tariff, sub.tariff_id)
        try:
            new_key = await create_vpn_key(
                user_id=user.id, subscription_id=sub.id, server=server,
                traffic_limit_gb=tariff.traffic_limit_gb if tariff else None, session=session,
            )
            new_key.reissue_count_today = used + 1
            new_key.reissue_date = today
            new_key.last_reissued_at = _dt.utcnow()
            await session.commit()
            return JSONResponse({
                "type": "key", "key": get_access_url(new_key),
                "text": f"✅ Ключ обновлён! Осталось обновлений: {5 - (used+1)}/5",
            })
        except Exception as exc:
            logger.error("Portal reissue failed for user %d: %s", user.id, exc, exc_info=True)
            return JSONResponse({"text": "❌ Не удалось обновить ключ. Попробуйте позже."})

    elif cmd == "cancel":
        return JSONResponse({"text": "Отменено."})

    # ── 📱 Как подключиться ──────────────────────────────────────────────────
    elif cmd == "guide":
        return JSONResponse({"html": (
            "<b>📱 Рекомендуемые приложения:</b><br><br>"
            "<b>Android:</b> "
            "<a href='https://play.google.com/store/apps/details?id=app.hiddify.com' target='_blank'>Hiddify</a>, "
            "<a href='https://play.google.com/store/apps/details?id=com.v2ray.ang' target='_blank'>V2RayNG</a><br>"
            "<b>iPhone:</b> "
            "<a href='https://apps.apple.com/app/streisand/id6450534064' target='_blank'>Streisand</a>, "
            "<a href='https://apps.apple.com/app/foxray/id6448898396' target='_blank'>FoXray</a><br>"
            "<b>Windows/macOS:</b> "
            "<a href='https://github.com/hiddify/hiddify-app/releases/latest' target='_blank'>Hiddify</a><br>"
            "<b>Linux:</b> "
            "<a href='https://github.com/hiddify/hiddify-app/releases/latest' target='_blank'>Hiddify</a>, "
            "<a href='https://github.com/v2rayA/v2rayA/releases/latest' target='_blank'>v2rayA</a><br><br>"
            "Скопируйте ключ (кнопка «🔑 Мой ключ»), откройте приложение, нажмите «+» и вставьте."
        )})

    # ── 📋 Правила сервиса ───────────────────────────────────────────────────
    elif cmd == "rules":
        return JSONResponse({"html": (
            "<b>📋 Условия использования</b><br><br>"
            "Сервис предоставляет защищённое соединение для личного использования.<br><br>"
            "<b>Запрещено:</b><br>"
            "• Незаконная деятельность<br>"
            "• Нарушение авторских прав<br>"
            "• Рассылка спама<br>"
            "• Действия, нарушающие законодательство<br><br>"
            "Сервис предназначен для защиты соединения в публичных сетях и при работе из-за рубежа."
        )})

    # ── ❓ Помощь ────────────────────────────────────────────────────────────
    elif cmd == "help":
        return JSONResponse({"html": (
            "<b>❓ Частые вопросы</b><br><br>"
            "<b>Как подключиться?</b><br>"
            "Нажмите «📱 Приложения» и установите Hiddify или V2RayNG.<br><br>"
            "<b>Как получить ключ?</b><br>"
            "Нажмите «🔑 Мой ключ» — скопируйте и вставьте в приложение.<br><br>"
            "<b>Ключ не работает?</b><br>"
            "Нажмите «🔄 Новый ключ» — будет выдан свежий ключ.<br><br>"
            "<b>Как продлить?</b><br>"
            "Нажмите «💳 Купить доступ» и оформите тариф.<br><br>"
            "<b>Нужна помощь?</b><br>"
            "Нажмите «🆘 Поддержка» — ответим в Telegram-боте."
        )})

    # ── 🆘 Поддержка — показать форму ────────────────────────────────────────
    elif cmd == "support":
        # Show open tickets if any
        from app.models.ticket import Ticket
        from app.enums import TicketStatus as _TS
        open_r = await session.execute(
            select(Ticket)
            .where(Ticket.user_id == user.id, Ticket.status != _TS.closed)
            .order_by(Ticket.updated_at.desc()).limit(3)
        )
        open_tickets = open_r.scalars().all()
        tickets_html = ""
        if open_tickets:
            tickets_html = "<br><b>Ваши активные тикеты:</b><br>"
            for t in open_tickets:
                badge = "🟡" if t.status == _TS.open else "🟢"
                tickets_html += f"{badge} Тикет #{t.id} — {t.created_at.strftime('%d.%m.%Y')}<br>"
        return JSONResponse({
            "type": "support_form",
            "html": (
                "<b>🆘 Поддержка</b><br><br>"
                "Опишите вашу проблему или вопрос — мы ответим здесь же в чате."
                + tickets_html
            ),
        })

    # ── 🆘 Поддержка — отправить сообщение ───────────────────────────────────
    elif cmd == "support_submit":
        from app.models.ticket import Ticket
        from app.models.ticket_message import TicketMessage
        from app.enums import TicketStatus as _TS
        from app.services.notification import notify_admin

        msg_text = body.get("text", "").strip()
        if not msg_text:
            return JSONResponse({"text": "Введите сообщение."})

        now = datetime.utcnow()
        ticket = Ticket(user_id=user.id, status=_TS.open, created_at=now, updated_at=now)
        session.add(ticket)
        await session.flush()
        tmsg = TicketMessage(ticket_id=ticket.id, author_type="user", text=msg_text, created_at=now)
        session.add(tmsg)
        await session.commit()
        ticket_id = ticket.id

        name = user.first_name or user.username or f"ID:{user.id}"
        tg_ref = f"@{user.username}" if user.username else str(user.telegram_id)
        try:
            await notify_admin(
                f"🆘 <b>Новый тикет #{ticket_id}</b> (веб-портал)\n"
                f"Пользователь: {name} ({tg_ref})\n\n{msg_text[:500]}"
            )
        except Exception:
            pass
        return JSONResponse({
            "text": f"✅ Тикет #{ticket_id} создан.\n\nМы ответим вам здесь — обновите страницу через некоторое время."
        })

    return JSONResponse({"text": "Неизвестная команда."})
