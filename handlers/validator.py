# handlers/validator.py
import os
import asyncio
from aiogram import types, Dispatcher, F, Bot
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.filters import StateFilter
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from config import (
    TEMP_DIR,
    VALIDATOR_DOMAINS,
    VALIDATOR_THREADS,
    VALIDATOR_TIMEOUT,
    VALIDATOR_PROXY,
    VALIDATOR_PROXIES,
    VALIDATOR_BATCH_SIZE,
    VALIDATOR_MAX_REQUEUE,
    VALIDATOR_FROM_EMAIL,
    VALIDATOR_HELO_HOST,
)
from database import add_recipients_batch
from utils.email_validator import BatchEmailValidator


class ValidatorStates(StatesGroup):
    waiting_for_nicknames_file = State()
    validating = State()


async def validator_menu(callback: CallbackQuery):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📤 Загрузить файл с никами", callback_data="validator_upload")],
        [InlineKeyboardButton(text="📋 Список валидных почт", callback_data="validator_list")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_main")]
    ])
    text = (
        "🔍 *Валидатор Gmail (через SOCKS5)*\n\n"
        f"Проверяемые домены: {', '.join(VALIDATOR_DOMAINS)}\n"
        f"Потоков: {VALIDATOR_THREADS}\n\n"
        "📄 Отправьте файл .txt в формате:\n"
        "`ник | заголовок`  (можно и без `|`, тогда заголовок будет пустым)"
    )
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="Markdown")
    await callback.answer()


async def ask_for_nicknames_file(callback: CallbackQuery, state: FSMContext):
    await state.set_state(ValidatorStates.waiting_for_nicknames_file)
    await callback.message.answer(
        "📄 Отправьте текстовый файл с никами в формате:\n"
        "`ник | заголовок`\n"
        "Если символа `|` нет, вся строка считается ником.",
        parse_mode="Markdown"
    )
    await callback.answer()


async def process_nicknames_file(message: types.Message, state: FSMContext, bot: Bot):
    if not message.document.file_name.endswith('.txt'):
        await message.reply("❌ Нужен .txt файл")
        return

    file_path = os.path.join(TEMP_DIR, f"nicknames_{message.from_user.id}.txt")
    await bot.download(message.document, destination=file_path)

    nicknames_with_titles = []  # список кортежей (nick, title)
    with open(file_path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            if '|' in line:
                parts = line.split('|', 1)
                nick = parts[0].strip()
                title = parts[1].strip() if len(parts) > 1 else ''
            else:
                nick = line
                title = ''
            if nick:
                nicknames_with_titles.append((nick, title))

    if not nicknames_with_titles:
        await message.reply("❌ Файл пуст или не содержит корректных строк")
        await state.clear()
        return

    unique_nicks = list({nick for nick, _ in nicknames_with_titles})
    total_checks = len(unique_nicks) * len(VALIDATOR_DOMAINS)

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 Начать", callback_data="validator_start")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="validator_cancel")]
    ])

    await message.reply(
        f"📊 Уникальных ников: {len(unique_nicks)}\n"
        f"Всего проверок: {total_checks}\n"
        f"Записей с заголовками: {len(nicknames_with_titles)}\n\n"
        "Начинаем?",
        reply_markup=keyboard
    )
    await state.update_data(nicknames_with_titles=nicknames_with_titles)
    await state.set_state(ValidatorStates.validating)


async def start_validation(callback: CallbackQuery, state: FSMContext, bot: Bot):
    data = await state.get_data()
    nicknames_with_titles = data.get('nicknames_with_titles', [])

    if not nicknames_with_titles:
        await callback.message.edit_text("❌ Нет данных")
        await state.clear()
        return

    # Строим словарь ник -> заголовок (если ник встречается несколько раз, берём первый заголовок)
    nick_to_title = {}
    for nick, title in nicknames_with_titles:
        if nick not in nick_to_title:
            nick_to_title[nick] = title

    unique_nicks = list(nick_to_title.keys())
    total_checks = len(unique_nicks) * len(VALIDATOR_DOMAINS)

    progress_msg = await callback.message.edit_text("🔄 Запуск валидации...")
    valid_emails = []
    loop = asyncio.get_event_loop()
    progress_queue = asyncio.Queue()
    validation_done = asyncio.Event()

    async def progress_updater():
        while True:
            try:
                data_item = await asyncio.wait_for(progress_queue.get(), timeout=0.5)
                current, total, email, status = data_item
                percent = (current / total) * 100
                await progress_msg.edit_text(
                    f"🔄 Проверка: {current}/{total} ({percent:.1f}%)\n"
                    f"✓ Найдено: {len(valid_emails)}\n"
                    f"Последний: {email} – {status}"
                )
            except asyncio.TimeoutError:
                if validation_done.is_set():
                    break
                continue

    updater_task = asyncio.create_task(progress_updater())

    def progress_callback(current, total, email, status):
        loop.call_soon_threadsafe(progress_queue.put_nowait, (current, total, email, status))

    validator = BatchEmailValidator(
        domains=VALIDATOR_DOMAINS,
        threads=VALIDATOR_THREADS,
        timeout=VALIDATOR_TIMEOUT,
        proxy_url=VALIDATOR_PROXY,
        proxies=VALIDATOR_PROXIES or None,
        from_email=VALIDATOR_FROM_EMAIL,
        helo_host=VALIDATOR_HELO_HOST,
        batch_size=VALIDATOR_BATCH_SIZE,
        max_requeue=VALIDATOR_MAX_REQUEUE,
    )
    validator.set_progress_callback(progress_callback)

    valid_emails = await loop.run_in_executor(None, validator.validate_nicknames, unique_nicks)

    validation_done.set()
    await updater_task

    # Формируем итоговый список строк "почта | заголовок"
    result_lines = []
    for email in valid_emails:
        nick = email.split('@')[0]  # ник из email
        title = nick_to_title.get(nick, '')
        if title:
            result_lines.append(f"{email} | {title}")
        else:
            result_lines.append(email)

    # Сохраняем валидные email в БД получателей (без заголовков, как и раньше)
    added_count = await add_recipients_batch(valid_emails)

    output_file = os.path.join(TEMP_DIR, f"valid_emails_{callback.from_user.id}.txt")
    with open(output_file, 'w', encoding='utf-8') as f:
        f.write('\n'.join(result_lines))

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📥 Скачать результат", callback_data=f"download_valid_{callback.from_user.id}")]
    ])

    await progress_msg.edit_text(
        f"✅ Готово!\n"
        f"Проверено уникальных ников: {len(unique_nicks)}\n"
        f"Найдено валидных почт: {len(valid_emails)}\n"
        f"Добавлено в базу получателей: {added_count}\n\n"
        "Файл содержит строки вида `почта | заголовок`.",
        reply_markup=keyboard
    )
    await state.clear()


async def cancel_validation(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("❌ Отменено")
    await callback.answer()


async def download_valid_file(callback: CallbackQuery, bot: Bot):
    # Извлекаем user_id из callback_data (формат: download_valid_123456)
    parts = callback.data.split('_')
    if len(parts) < 3:
        await callback.answer("Неверный запрос")
        return

    try:
        user_id = int(parts[2])
    except ValueError:
        await callback.answer("Ошибка идентификатора")
        return

    file_path = os.path.join(TEMP_DIR, f"valid_emails_{user_id}.txt")

    if not os.path.exists(file_path):
        await callback.answer("Файл не найден, возможно, он был удалён.")
        return

    await callback.answer()
    await bot.send_document(
        chat_id=callback.from_user.id,
        document=types.FSInputFile(file_path, filename="valid_emails.txt"),
        caption="✅ Результат валидации (почта | заголовок)"
    )


def register_handlers(dp: Dispatcher):
    dp.callback_query.register(validator_menu, F.data == "menu_validator")
    dp.callback_query.register(ask_for_nicknames_file, F.data == "validator_upload")
    dp.callback_query.register(start_validation, F.data == "validator_start", StateFilter(ValidatorStates.validating))
    dp.callback_query.register(cancel_validation, F.data == "validator_cancel")
    dp.message.register(process_nicknames_file, StateFilter(ValidatorStates.waiting_for_nicknames_file))
    dp.callback_query.register(download_valid_file, F.data.startswith("download_valid_"))