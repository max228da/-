import asyncio
from datetime import datetime
from aiogram import Dispatcher, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.filters import StateFilter
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, Message

from database import clear_recipients, clear_accounts, get_setting, set_setting, get_accounts, get_account_by_id, log_outgoing_email
from mailer import send_email_sync


class SettingsStates(StatesGroup):
    waiting_for_test_email = State()


def _settings_keyboard(sender_mode: str = "random"):
    sender_label = "🎲 Имя: рандом (женское)" if sender_mode != "fixed" else "📌 Имя: фиксированное"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=sender_label, callback_data="settings_toggle_sender")],
        [InlineKeyboardButton(text="🧹 Сбросить получателей", callback_data="settings_clear_recipients")],
        [InlineKeyboardButton(text="🗑 Сбросить все аккаунты", callback_data="settings_clear_accounts")],
        [InlineKeyboardButton(text="📮 Почта для теста", callback_data="settings_set_test_email")],
        [InlineKeyboardButton(text="✉️ Тест письмо", callback_data="settings_test_email")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_main")],
    ])


async def _settings_text() -> str:
    test_email = await get_setting("test_email") or "не задана"
    sender_mode = await get_setting("sender_mode") or "random"
    if sender_mode != "fixed":
        sender_desc = "случайное женское имя на каждое письмо"
    else:
        sender_desc = (await get_setting("global_from_name") or "Mailer") + " (фиксированное)"
    return (
        "⚙️ Настройки\n\n"
        f"Почта для теста: {test_email}\n"
        f"Имя отправителя: {sender_desc}\n\n"
        "Выберите действие:"
    )


async def settings_menu(callback: CallbackQuery):
    await callback.answer()
    sender_mode = await get_setting("sender_mode") or "random"
    await callback.message.edit_text(await _settings_text(), reply_markup=_settings_keyboard(sender_mode))


async def toggle_sender_mode(callback: CallbackQuery):
    current = await get_setting("sender_mode") or "random"
    new_mode = "fixed" if current != "fixed" else "random"
    await set_setting("sender_mode", new_mode)
    await callback.answer("Рандомное женское имя" if new_mode == "random" else "Фиксированное имя")
    await callback.message.edit_text(await _settings_text(), reply_markup=_settings_keyboard(new_mode))


async def clear_recipients_action(callback: CallbackQuery):
    await clear_recipients()
    await callback.answer("Готово")
    sender_mode = await get_setting("sender_mode") or "random"
    await callback.message.edit_text(
        await _settings_text() + "\n\n✅ Получатели очищены",
        reply_markup=_settings_keyboard(sender_mode),
    )


async def clear_accounts_action(callback: CallbackQuery):
    await clear_accounts()
    await callback.answer("Готово")
    sender_mode = await get_setting("sender_mode") or "random"
    await callback.message.edit_text(
        await _settings_text() + "\n\n✅ Все аккаунты очищены",
        reply_markup=_settings_keyboard(sender_mode),
    )


async def ask_test_email(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.set_state(SettingsStates.waiting_for_test_email)
    await callback.message.answer("Отправьте в чат email для тестовых писем:")


async def save_test_email(message: Message, state: FSMContext):
    email = message.text.strip()
    await set_setting("test_email", email)
    await message.answer(f"✅ Почта для теста сохранена: {email}")
    await state.clear()


def _build_test_accounts_keyboard(accounts):
    rows = []
    for account in accounts:
        rows.append([
            InlineKeyboardButton(text=account['email'], callback_data="noop"),
            InlineKeyboardButton(text="✅", callback_data=f"settings_send_test:{account['id']}")
        ])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="menu_settings")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def show_test_accounts(callback: CallbackQuery):
    await callback.answer()
    test_email = await get_setting("test_email")
    if not test_email:
        await callback.message.answer("❌ Сначала укажи почту для теста")
        return
    accounts = await get_accounts(100)
    if not accounts:
        await callback.message.answer("❌ Нет загруженных аккаунтов")
        return
    await callback.message.edit_text(
        f"✉️ Тестовое письмо будет отправлено на: {test_email}\n\nВыбери аккаунт:",
        reply_markup=_build_test_accounts_keyboard(accounts),
    )


async def send_test_email_action(callback: CallbackQuery):
    await callback.answer("Отправляю...")
    account_id = int(callback.data.split(':', 1)[1])
    account = await get_account_by_id(account_id)
    if not account:
        await callback.message.answer("❌ Аккаунт не найден")
        return

    test_email = await get_setting("test_email")
    if not test_email:
        await callback.message.answer("❌ Почта для теста не задана")
        return

    global_from_name = await get_setting("global_from_name") or account['from_name'] or "Mailer"
    proxy_config = None
    if account['host']:
        proxy_config = {
            'host': account['host'],
            'port': account['port'],
            'username': account['proxy_user'],
            'password': account['proxy_pass'],
        }

    subject = f"Test email from {account['email']}"
    body = (
        f"Это тестовое письмо.\n\n"
        f"Аккаунт: {account['email']}\n"
        f"Время: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    )

    loop = asyncio.get_event_loop()
    success, result = await loop.run_in_executor(
        None,
        send_email_sync,
        account['email'],
        account['app_password'],
        global_from_name,
        test_email,
        subject,
        body,
        proxy_config,
    )
    if success:
        await log_outgoing_email(account['id'], test_email, subject, body, result)
        await callback.message.answer(f"✅ Тестовое письмо отправлено с {account['email']} на {test_email}")
    else:
        await callback.message.answer(f"❌ Ошибка тестового письма: {result}")


def register_handlers(dp: Dispatcher):
    dp.callback_query.register(settings_menu, F.data == "menu_settings")
    dp.callback_query.register(toggle_sender_mode, F.data == "settings_toggle_sender")
    dp.callback_query.register(clear_recipients_action, F.data == "settings_clear_recipients")
    dp.callback_query.register(clear_accounts_action, F.data == "settings_clear_accounts")
    dp.callback_query.register(ask_test_email, F.data == "settings_set_test_email")
    dp.callback_query.register(show_test_accounts, F.data == "settings_test_email")
    dp.callback_query.register(send_test_email_action, F.data.startswith("settings_send_test:"))
    dp.message.register(save_test_email, StateFilter(SettingsStates.waiting_for_test_email))
