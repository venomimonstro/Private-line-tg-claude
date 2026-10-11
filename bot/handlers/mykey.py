from __future__ import annotations

from html import escape

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.models.user import User
from app.services.vpn_key import get_client_access_url
from bot.handlers.access import _get_active_subscription

router = Router(name="mykey")


def _keys_keyboard(keys: list) -> InlineKeyboardMarkup:
    rows = []
    for idx, key in enumerate(keys, start=1):
        label = key.label or f"Ключ {idx}"
        rows.append(
            [InlineKeyboardButton(text=f"🔑 {label}", callback_data=f"get_key:{key.id}")]
        )
        rows.append(
            [InlineKeyboardButton(text=f"📨 Переслать {label}", callback_data=f"resend_key:{key.id}")]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(Command("windows"))
async def send_windows_installer(message: Message) -> None:
    from app.windows_bundle import build_windows_client

    await message.answer_document(
        BufferedInputFile(build_windows_client(), filename="private-line-windows.zip"),
        caption=(
            "🖥 <b>Private Line для Windows</b>\n\n"
            "Скачивание ZIP ещё не означает установку. Выберите «Извлечь все», "
            "запустите INSTALL-WINDOWS.cmd и дождитесь зелёного подтверждения. "
            "Затем запустите CHECK-INSTALL.cmd — он проверит Xray и ярлык на рабочем столе. "
            "Вставьте ключ из /mykey. Для отправки диагностики используйте "
            "отдельную кнопку в приложении; VPN-ключ не отправляется."
        ),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "windows_download")
async def send_windows_installer_callback(callback: CallbackQuery) -> None:
    from app.windows_bundle import build_windows_client

    await callback.answer("Отправляю Windows-клиент")
    await callback.message.answer_document(
        BufferedInputFile(build_windows_client(), filename="private-line-windows.zip"),
        caption="🖥 Извлеките весь ZIP, запустите INSTALL-WINDOWS.cmd, затем CHECK-INSTALL.cmd. После подтверждения установки вставьте VLESS Reality ключ в Private Line.",
    )


@router.message(Command("chrome_extension"))
async def send_chrome_extension_only(message: Message) -> None:
    from app.chrome_bundle import build_chrome_extension_zip

    await message.answer_document(
        BufferedInputFile(build_chrome_extension_zip(), filename="private-line-extension.zip"),
        caption=(
            "🌐 Расширение Private Line для Google Chrome. Распакуйте архив, "
            "откройте chrome://extensions, включите режим разработчика и нажмите "
            "«Загрузить распакованное». Для подключения VLESS Reality нужен "
            "отдельный локальный Xray из комплекта /chrome."
        ),
    )


@router.message(Command("chrome"))
async def send_chrome_extension(message: Message) -> None:
    """Send the generic, credential-free Chrome client installation kit."""
    from app.chrome_bundle import build_chrome_bundle

    data = build_chrome_bundle()
    await message.answer_document(
        BufferedInputFile(data, filename="private-line-chrome.zip"),
        caption=(
            "🌐 <b>Private Line для Google Chrome</b>\n\n"
            "Архив содержит расширение и установщик локального Xray. "
            "Сначала прочтите README_RU.md. Для подключения вставьте "
            "свой VLESS Reality ключ из /mykey.\n\n"
            "⚠️ Chrome без локального Xray не умеет самостоятельно "
            "устанавливать VLESS Reality соединение."
        ),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "chrome_download")
async def chrome_extension_callback(callback: CallbackQuery) -> None:
    from app.chrome_bundle import build_chrome_bundle

    await callback.answer("Отправляю расширение Chrome")
    await callback.message.answer_document(
        BufferedInputFile(build_chrome_bundle(), filename="private-line-chrome.zip"),
        caption="🌐 Распакуйте архив, прочитайте README_RU.md, установите расширение и локальный Xray.",
    )


@router.message(Command("mykey"))
@router.message(F.text == "🔑 Мой ключ")
async def cmd_mykey(message: Message, db_user: User) -> None:
    _sub, active_keys, _tariff = await _get_active_subscription(db_user.id)

    if not active_keys:
        await message.answer(
            "У вас нет активных ключей.\n\nНажмите «💳 Купить доступ», чтобы оформить подписку."
        )
        return

    if len(active_keys) == 1:
        key = active_keys[0]
        try:
            url = get_client_access_url(key)
        except Exception:
            await message.answer("Ошибка получения ключа. Обратитесь в поддержку.")
            return
        label_part = f" ({escape(key.label)})" if key.label else ""
        await message.answer(
            f"🔑 <b>Ваш ключ доступа{label_part}:</b>\n\n"
            f"<code>{escape(url)}</code>\n\n"
            "Импортируйте в V2RayNG, Hiddify, Streisand или V2Box.\n"
            "Outline не поддерживает VLESS Reality.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📄 Получить файл VLESS", callback_data=f"vless_file:{key.id}")],
                [InlineKeyboardButton(text="📷 QR-код ключа", callback_data="get_key_qr")],
                [InlineKeyboardButton(text="🌐 Chrome VPN (ZIP + Xray)", callback_data="chrome_download")],
                [InlineKeyboardButton(text="🖥 Windows клиент", callback_data="windows_download")],
            ]),
        )
    else:
        lines = ["🔑 <b>Ваши ключи доступа:</b>\n"]
        for key in active_keys:
            try:
                url = get_client_access_url(key)
            except Exception:
                continue
            label = escape(key.label or "VPN")
            lines.append(f"<b>{label}:</b>\n<code>{escape(url)}</code>")
        lines.append(
            "\n📱 Скопируйте нужный ключ и добавьте в приложение:\n"
            "• <b>V2RayNG</b> (Android)\n• <b>Streisand / FoXray</b> (iPhone)\n"
            "• <b>Hiddify</b> (Windows/Mac)"
        )
        await message.answer("\n\n".join(lines), parse_mode="HTML")


@router.callback_query(F.data.startswith("resend_key:"))
async def on_resend_key(callback: CallbackQuery, db_user: User) -> None:
    try:
        key_id = int(callback.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await callback.answer("Некорректный запрос.", show_alert=True)
        return

    _sub, active_keys, _tariff = await _get_active_subscription(db_user.id)
    key = next((k for k in active_keys if k.id == key_id), None)

    if key is None:
        await callback.answer("Ключ не найден или не принадлежит вам.", show_alert=True)
        return

    try:
        url = get_client_access_url(key)
    except Exception:
        await callback.answer("Ошибка получения ключа. Обратитесь в поддержку.", show_alert=True)
        return

    label_part = f" ({escape(key.label)})" if key.label else ""
    await callback.message.answer(  # type: ignore[union-attr]
        f"📨 <b>Ключ доступа{label_part} повторно:</b>\n\n"
        f"<code>{escape(url)}</code>\n\n"
        f"Импортируйте ключ в V2RayNG, Hiddify, Streisand или V2Box (не Outline).",
        parse_mode="HTML",
    )
    await callback.answer("Ключ отправлен повторно.")


@router.callback_query(F.data.startswith("vless_file:"))
async def on_download_vless_file(callback: CallbackQuery, db_user: User) -> None:
    try:
        key_id = int((callback.data or "").split(":", 1)[1])
    except (IndexError, ValueError):
        await callback.answer("Неверный ключ.", show_alert=True)
        return
    _sub, active_keys, _tariff = await _get_active_subscription(db_user.id)
    key = next((k for k in active_keys if k.id == key_id), None)
    if key is None:
        await callback.answer("Ключ не принадлежит вашей активной подписке.", show_alert=True)
        return
    try:
        url = get_client_access_url(key)
    except Exception:
        await callback.answer("Ошибка формата ключа. Обратитесь в поддержку.", show_alert=True)
        return
    await callback.message.answer_document(
        BufferedInputFile((url + "\n").encode("utf-8"), filename="private-line-vless.txt"),
        caption="📄 Ключ VLESS Reality. Импортируйте файл в совместимое приложение либо скопируйте ссылку из сообщения /mykey. Не используйте Outline.",
    )
    await callback.answer("Файл отправлен.")
