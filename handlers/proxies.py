import os
import asyncio
from aiogram import types, Dispatcher, F, Bot
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.filters import StateFilter
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery

from config import TEMP_DIR
from database import add_proxy, get_proxies, delete_proxy, update_proxy_check
from mailer import check_proxy


class ProxyStates(StatesGroup):
    waiting_for_input = State()
    waiting_for_delete_id = State()


async def proxies_menu(callback: CallbackQuery):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📤 Загрузить/вставить прокси", callback_data="proxy_upload")],
        [InlineKeyboardButton(text="📋 Список прокси", callback_data="proxy_list")],
        [InlineKeyboardButton(text="🗑 Удалить прокси", callback_data="proxy_delete")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_main")],
    ])
    await callback.message.edit_text("🌐 Управление SOCKS5 прокси", reply_markup=keyboard)
    await callback.answer()


async def ask_for_proxy_input(callback: CallbackQuery, state: FSMContext):
    await state.set_state(ProxyStates.waiting_for_input)
    await callback.message.answer(
        "📄 Отправьте .txt файл с прокси или просто вставьте прокси в чат.\n"
        "Формат: host:port:user:pass socks5"
    )
    await callback.answer()


def _parse_proxy_line(line: str):
    line = line.strip()
    if not line or line.startswith('#'):
        return None
    if line.lower().endswith(' socks5'):
        line = line[:-7].strip()
    parts = line.split(':')
    if len(parts) == 2:
        host, port = parts
        user = passw = None
    elif len(parts) == 4:
        host, port, user, passw = parts
    else:
        return None
    try:
        port = int(port)
    except ValueError:
        return None
    return host, port, user, passw


async def _handle_proxy_lines(message: types.Message, lines):
    count = 0
    for line in lines:
        parsed = _parse_proxy_line(line)
        if not parsed:
            await message.answer(f"❌ Не удалось распознать прокси: {line}")
            continue

        host, port, user, passw = parsed
        added = await add_proxy(host, port, user, passw)
        if not added:
            await message.answer(f"⚠️ Прокси уже есть: {host}:{port}")
            continue

        count += 1
        proxy_config = {'host': host, 'port': port, 'username': user, 'password': passw}
        success, ip_or_error = await asyncio.get_event_loop().run_in_executor(None, check_proxy, proxy_config)
        proxies = await get_proxies(only_active=False)
        if proxies:
            last_id = max(p['id'] for p in proxies)
            await update_proxy_check(last_id, success, ip_or_error if success else None)
        if success:
            await message.answer(f"✅ Прокси {host}:{port} работает, IP: {ip_or_error}")
        else:
            await message.answer(f"❌ Прокси {host}:{port} ошибка: {ip_or_error}")
    return count


async def process_proxy_input(message: types.Message, state: FSMContext, bot: Bot):
    lines = []
    if message.document and message.document.file_name and message.document.file_name.endswith('.txt'):
        file_path = os.path.join(TEMP_DIR, f"proxies_{message.from_user.id}.txt")
        await bot.download(message.document, destination=file_path)
        with open(file_path, 'r', encoding='utf-8') as f:
            lines = [line.strip() for line in f if line.strip()]
    elif message.text:
        lines = [line.strip() for line in message.text.splitlines() if line.strip()]
    else:
        await message.reply("❌ Отправьте .txt файл или текст с прокси")
        return

    count = await _handle_proxy_lines(message, lines)
    await message.reply(f"📊 Загружено прокси: {count}")
    await state.clear()


async def list_proxies(callback: CallbackQuery):
    proxies = await get_proxies(only_active=False)
    if not proxies:
        text = "Нет прокси"
    else:
        text = "🌐 Список прокси:\n\n"
        for p in proxies[:20]:
            ip_info = f"IP: {p['external_ip']}" if p['external_ip'] else "не проверен"
            text += f"• {p['host']}:{p['port']} ({'активен' if p['is_active'] else 'неактивен'}) {ip_info}\n"
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="menu_proxies")]])
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()


async def ask_for_proxy_delete(callback: CallbackQuery, state: FSMContext):
    proxies = await get_proxies(only_active=False)
    if not proxies:
        await callback.answer("Нет прокси")
        return
    text = "Введите ID прокси для удаления:\n"
    for p in proxies[:10]:
        text += f"ID {p['id']}: {p['host']}:{p['port']}\n"
    await callback.message.answer(text)
    await state.set_state(ProxyStates.waiting_for_delete_id)
    await callback.answer()


async def process_proxy_delete(message: types.Message, state: FSMContext):
    try:
        proxy_id = int(message.text.strip())
        await delete_proxy(proxy_id)
        await message.reply("✅ Удалено")
    except Exception:
        await message.reply("❌ Неверный ID")
    await state.clear()


def register_handlers(dp: Dispatcher):
    dp.callback_query.register(proxies_menu, F.data == "menu_proxies")
    dp.callback_query.register(ask_for_proxy_input, F.data == "proxy_upload")
    dp.callback_query.register(list_proxies, F.data == "proxy_list")
    dp.callback_query.register(ask_for_proxy_delete, F.data == "proxy_delete")
    dp.message.register(process_proxy_input, StateFilter(ProxyStates.waiting_for_input))
    dp.message.register(process_proxy_delete, StateFilter(ProxyStates.waiting_for_delete_id))
