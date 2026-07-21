import os
import re
import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from aiogram import types, Dispatcher, F, Bot
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.filters import StateFilter
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from config import TEMP_DIR, TELEGRAM_PROXY
from database import add_account, get_accounts, delete_account, get_setting, set_setting, update_account_check, get_account_by_id
from mailer import check_smtp_login

logger = logging.getLogger(__name__)
executor = ThreadPoolExecutor(max_workers=3)
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


class AccountStates(StatesGroup):
    waiting_for_file = State()
    waiting_for_name = State()


async def accounts_menu(callback: CallbackQuery):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📤 Загрузить из файла", callback_data="acc_upload")],
        [InlineKeyboardButton(text="📋 Список аккаунтов", callback_data="acc_list")],
        [InlineKeyboardButton(text="👤 Установить имя отправителя", callback_data="acc_set_name")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_main")],
    ])
    await callback.message.edit_text("📧 Управление Gmail аккаунтами", reply_markup=keyboard)
    await callback.answer()


async def ask_for_accounts_file(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AccountStates.waiting_for_file)
    await callback.message.answer(
        "📄 Отправьте .txt файл с аккаунтами.\n"
        "Бот сам найдёт email и app password в строках файла."
    )
    await callback.answer()


def _extract_account_from_line(line: str):
    raw = line.strip()
    if not raw or raw.startswith('{'):
        return None

    parts = [p.strip() for p in raw.split(';')]
    if not parts:
        return None

    email = None
    for part in parts:
        if EMAIL_RE.fullmatch(part):
            email = part
            break

    if not email:
        return None

    app_pass = None
    if len(parts) >= 4 and parts[3] and not parts[3].startswith('http'):
        app_pass = parts[3]
    else:
        for idx, part in enumerate(parts):
            if part == email:
                continue
            if part.startswith('http'):
                continue
            if len(part) >= 8 and re.fullmatch(r"[A-Za-z0-9]+", part):
                if idx > 0:
                    app_pass = part
                    break

    if not app_pass:
        return None

    return email, app_pass


def _parse_telegram_proxy():
    if not TELEGRAM_PROXY:
        return None
    m = re.match(r'socks5://(?:([^:]+):([^@]+)@)?([^:]+):(\d+)', TELEGRAM_PROXY)
    if not m:
        return None
    user, passw, host, port = m.groups()
    return {'host': host, 'port': int(port), 'username': user, 'password': passw}


async def process_accounts_file(message: types.Message, state: FSMContext, bot: Bot):
    if not message.document or not message.document.file_name or not message.document.file_name.endswith('.txt'):
        await message.reply("❌ Нужен .txt файл")
        return

    status_msg = await message.reply("⏳ Скачиваю файл...")
    file_path = os.path.join(TEMP_DIR, f"accounts_{message.from_user.id}.txt")
    await bot.download(message.document, destination=file_path)

    with open(file_path, 'r', encoding='utf-8') as f:
        lines = [l.strip() for l in f if l.strip()]

    if not lines:
        await status_msg.edit_text("❌ Файл пуст")
        await state.clear()
        return

    proxy_config = _parse_telegram_proxy()
    found = [item for item in (_extract_account_from_line(line) for line in lines) if item]
    if not found:
        await status_msg.edit_text("❌ Бот не нашёл ни одного аккаунта в файле")
        await state.clear()
        return

    await status_msg.edit_text(f"📊 Найдено аккаунтов: {len(found)}. Начинаю загрузку...")
    added = 0
    for idx, (email, app_pass) in enumerate(found, 1):
        inserted = await add_account(email, app_pass)
        if not inserted:
            await message.answer(f"⚠️ {email} уже есть в базе")
            continue

        added += 1
        await status_msg.edit_text(f"⏳ Проверяю {email} ({idx}/{len(found)})...")
        loop = asyncio.get_event_loop()
        try:
            success, error = await loop.run_in_executor(executor, check_smtp_login, email, app_pass, proxy_config)
        except Exception as e:
            success, error = False, f"Исключение при проверке: {e}"

        await update_account_check(email, success, error)
        if success:
            await message.answer(f"✅ {email} — готов к рассылке")
        else:
            await message.answer(f"❌ {email} — ошибка: {error}")

    await status_msg.edit_text(f"✅ Загрузка завершена. Добавлено новых аккаунтов: {added}")
    await state.clear()


def _build_accounts_keyboard(accs):
    rows = []
    for a in accs:
        rows.append([
            InlineKeyboardButton(text=a['email'], callback_data="noop"),
            InlineKeyboardButton(text="❌", callback_data=f"acc_remove:{a['id']}")
        ])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="menu_accounts")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def list_accounts(callback: CallbackQuery):
    accs = await get_accounts(100)
    await callback.answer()
    if not accs:
        await callback.message.edit_text(
            "Нет аккаунтов",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="menu_accounts")]])
        )
        return
    await callback.message.edit_text("📧 Загруженные аккаунты:", reply_markup=_build_accounts_keyboard(accs))


async def delete_account_inline(callback: CallbackQuery):
    await callback.answer("Удаляю...")
    account_id = int(callback.data.split(':', 1)[1])
    account = await get_account_by_id(account_id)
    if not account:
        await callback.message.answer("❌ Аккаунт не найден")
        return
    await delete_account(account['email'])
    accs = await get_accounts(100)
    if not accs:
        await callback.message.edit_text(
            "Нет аккаунтов",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="menu_accounts")]])
        )
    else:
        await callback.message.edit_text("📧 Загруженные аккаунты:", reply_markup=_build_accounts_keyboard(accs))


async def ask_for_name(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AccountStates.waiting_for_name)
    current = await get_setting("global_from_name") or "не задано"
    await callback.message.answer(f"Текущее имя отправителя: {current}\nВведите новое имя (например: Leon Hartmann):")
    await callback.answer()


async def set_name(message: types.Message, state: FSMContext):
    name = message.text.strip()
    await set_setting("global_from_name", name)
    await message.reply(f"✅ Имя отправителя установлено: {name}")
    await state.clear()


def register_handlers(dp: Dispatcher):
    dp.callback_query.register(accounts_menu, F.data == "menu_accounts")
    dp.callback_query.register(ask_for_accounts_file, F.data == "acc_upload")
    dp.callback_query.register(list_accounts, F.data == "acc_list")
    dp.callback_query.register(delete_account_inline, F.data.startswith("acc_remove:"))
    dp.callback_query.register(ask_for_name, F.data == "acc_set_name")
    dp.message.register(process_accounts_file, StateFilter(AccountStates.waiting_for_file))
    dp.message.register(set_name, StateFilter(AccountStates.waiting_for_name))
