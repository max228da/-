# handlers/mailing.py
import asyncio
import os
import random
from aiogram import Dispatcher, F, Bot, types
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.filters import StateFilter
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, Message

from config import (
    MAILING_THREADS,
    MAILING_COOLDOWN,
    MAILING_PROXY,
    MAILING_BATCH_DELAY,
    MAILING_ACCOUNT_RESERVE,
    MAILING_RETRIES,
    MAILING_RETRY_DELAY,
    MAILING_REQUEUE_MAX,
    MAILING_DAILY_LIMIT,
    MAILING_COOLDOWN_JITTER,
    MAILING_PROXIES,
    TEMP_DIR,
)
from database import (
    claim_available_account,
    increment_sent_count,
    set_account_cooldown,
    get_presets,
    get_setting,
    log_mailing_error,
    log_outgoing_email,
)
from mailer import send_email_sync
from namegen import random_female_name


class MailingStates(StatesGroup):
    waiting_for_file = State()
    confirming = State()


async def mailing_menu(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    presets = await get_presets()
    if not presets:
        await callback.message.edit_text("❌ Нет пресетов! Добавьте шаблоны писем.")
        return

    await state.update_data(presets=presets)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📤 Загрузить файл с почтами и товарами", callback_data="mailing_upload")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_main")],
    ])
    await callback.message.edit_text(
        "📧 *Рассылка по товарам*\n\n"
        "Отправьте текстовый файл (.txt) со строками:\n"
        "`почта@gmail.com | Название товара`\n\n"
        "Символ `|` обязателен. Если товара нет, оставьте поле пустым.\n"
        "Пресеты будут чередоваться, слово `OFFER` заменится на название товара.",
        reply_markup=keyboard,
        parse_mode="Markdown",
    )
    await state.set_state(MailingStates.waiting_for_file)


async def ask_for_recipients_file(callback: CallbackQuery, state: FSMContext):
    await state.set_state(MailingStates.waiting_for_file)
    await callback.message.answer("📄 Отправьте .txt файл с парами `почта | товар`")
    await callback.answer()


async def process_recipients_file(message: Message, state: FSMContext, bot: Bot):
    if not message.document or not message.document.file_name.endswith('.txt'):
        await message.reply("❌ Нужен .txt файл")
        return

    file_path = os.path.join(TEMP_DIR, f"mailing_{message.from_user.id}.txt")
    await bot.download(message.document, destination=file_path)

    recipients_with_titles = []
    with open(file_path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            if '|' in line:
                parts = line.split('|', 1)
                email = parts[0].strip()
                title = parts[1].strip() if len(parts) > 1 else ''
            else:
                email = line
                title = ''
            if email and '@' in email:
                recipients_with_titles.append((email, title))

    if not recipients_with_titles:
        await message.reply("❌ Файл пуст или не содержит корректных email-адресов")
        await state.clear()
        return

    data = await state.get_data()
    presets = data['presets']

    await state.update_data(recipients=recipients_with_titles)

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Подтвердить и начать", callback_data="mailing_start")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="mailing_cancel")],
    ])

    await message.reply(
        f"📊 Загружено получателей: {len(recipients_with_titles)}\n"
        f"📝 Пресетов: {len(presets)}\n"
        f"Потоков: {MAILING_THREADS} | Пауза каждые {MAILING_THREADS} писем: {MAILING_BATCH_DELAY} сек\n\n"
        "Начать рассылку?",
        reply_markup=keyboard,
    )
    await state.set_state(MailingStates.confirming)


async def start_mailing(callback: CallbackQuery, state: FSMContext, bot: Bot):
    await callback.answer()
    data = await state.get_data()
    recipients_with_titles = data['recipients']
    presets = data['presets']
    global_from_name = await get_setting("global_from_name") or "Mailer"
    sender_mode = await get_setting("sender_mode") or "random"
    use_random_sender = sender_mode != "fixed"

    total = len(recipients_with_titles)
    progress_msg = await callback.message.edit_text(f"🚀 Рассылка начата... 0/{total}")

    loop = asyncio.get_event_loop()
    queue: asyncio.Queue = asyncio.Queue()
    # (email, title, retry_count)
    for email, title in recipients_with_titles:
        await queue.put((email, title, 0))

    counters = {"sent": 0, "failed": 0}
    error_samples: list = []          # первые N ошибок для итогового отчёта
    lock = asyncio.Lock()
    preset_index_holder = {"i": 0}
    chat_id = callback.from_user.id

    batch_counter = {"n": 0}
    batch_lock = asyncio.Lock()

    def is_transient_err(err_msg: str) -> bool:
        # Быстрая эвристика: 4xx SMTP или сетевая ошибка
        low = err_msg.lower()
        if "smtp 4" in low:
            return True
        for k in ("timeout", "disconnect", "reset", "aborted", "connection", "oserror"):
            if k in low:
                return True
        return False

    async def refresh_progress(force: bool = False):
        done = counters["sent"] + counters["failed"]
        if force or done % 5 == 0:
            try:
                await progress_msg.edit_text(
                    f"🚀 Прогресс: {done}/{total}\n"
                    f"✅ Отправлено: {counters['sent']}\n❌ Ошибок: {counters['failed']}"
                )
            except Exception:
                pass  # rate limit / no changes

    async def worker(worker_id: int):
        while True:
            try:
                email, title, retry_count = queue.get_nowait()
            except asyncio.QueueEmpty:
                return

            # Атомарно захватываем аккаунт (бронь на MAILING_ACCOUNT_RESERVE сек)
            account = None
            wait_attempts = 0
            max_wait_attempts = 12  # ~60 сек ожидания
            while account is None:
                account = await claim_available_account(MAILING_ACCOUNT_RESERVE, MAILING_DAILY_LIMIT)
                if account is None:
                    wait_attempts += 1
                    if wait_attempts > max_wait_attempts:
                        break
                    await asyncio.sleep(5)

            if account is None:
                async with lock:
                    counters["failed"] += 1
                    if len(error_samples) < 20:
                        error_samples.append(f"{email}: нет свободных аккаунтов")
                queue.task_done()
                await refresh_progress()
                continue

            async with lock:
                preset = presets[preset_index_holder["i"] % len(presets)]
                preset_index_holder["i"] += 1

            subject = title if title else preset['subject']
            body = preset['body'].replace('OFFER', title) if title else preset['body']
            effective_from = random_female_name() if use_random_sender else global_from_name

            proxy_config = None
            if account['host']:
                proxy_config = {
                    'host': account['host'],
                    'port': account['port'],
                    'username': account['proxy_user'],
                    'password': account['proxy_pass'],
                }
            elif MAILING_PROXIES:
                proxy_config = random.choice(MAILING_PROXIES)  # разный IP на каждое письмо
            elif MAILING_PROXY:
                proxy_config = MAILING_PROXY

            success, result = await loop.run_in_executor(
                None,
                lambda ef=effective_from: send_email_sync(
                    account['email'],
                    account['app_password'],
                    ef,
                    email,
                    subject,
                    body,
                    proxy_config,
                    False,             # debug
                    MAILING_RETRIES,   # retries
                    MAILING_RETRY_DELAY,
                ),
            )

            cooldown = MAILING_COOLDOWN + random.randint(0, MAILING_COOLDOWN_JITTER)
            if success:
                await increment_sent_count(account['id'])
                await log_outgoing_email(account['id'], email, subject, body, result)
                async with lock:
                    counters["sent"] += 1
                # Реальный cooldown после отправки — перезапишет временную бронь
                await set_account_cooldown(account['id'], cooldown)
            else:
                # Освобождаем аккаунт быстрее (короткий cooldown вместо длинной брони),
                # чтобы он не простаивал MAILING_ACCOUNT_RESERVE секунд из-за нашей ошибки
                await set_account_cooldown(account['id'], cooldown)
                await log_mailing_error(account['id'], email, preset['id'], result)

                if is_transient_err(result) and retry_count < MAILING_REQUEUE_MAX:
                    # Возвращаем письмо в очередь — попробует другой аккаунт / позже
                    await queue.put((email, title, retry_count + 1))
                    # Не увеличиваем failed — это ещё не финальный провал
                else:
                    async with lock:
                        counters["failed"] += 1
                        if len(error_samples) < 20:
                            error_samples.append(
                                f"{account['email']} → {email}: {result[:120]}"
                            )

            # Батчевая пауза
            async with batch_lock:
                batch_counter["n"] += 1
                if batch_counter["n"] >= MAILING_THREADS:
                    batch_counter["n"] = 0
                    await asyncio.sleep(MAILING_BATCH_DELAY)

            await refresh_progress()
            queue.task_done()

    tasks = [asyncio.create_task(worker(i)) for i in range(MAILING_THREADS)]
    await asyncio.gather(*tasks, return_exceptions=True)

    # Финальный отчёт
    summary = (
        f"✅ Рассылка завершена!\n"
        f"📨 Отправлено: {counters['sent']}\n"
        f"❌ Ошибок: {counters['failed']}"
    )
    try:
        await progress_msg.edit_text(summary)
    except Exception:
        await bot.send_message(chat_id, summary)

    if error_samples:
        # Одним сообщением, а не спамом
        chunk = "\n".join(f"• {e}" for e in error_samples[:20])
        try:
            await bot.send_message(
                chat_id,
                f"⚠️ Примеры ошибок (первые {min(len(error_samples), 20)}):\n{chunk}",
            )
        except Exception:
            pass

    await state.clear()


async def cancel_mailing(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.clear()
    await callback.message.edit_text("❌ Рассылка отменена")


def register_handlers(dp: Dispatcher):
    dp.callback_query.register(mailing_menu, F.data == "menu_mailing")
    dp.callback_query.register(ask_for_recipients_file, F.data == "mailing_upload")
    dp.message.register(process_recipients_file, StateFilter(MailingStates.waiting_for_file))
    dp.callback_query.register(start_mailing, F.data == "mailing_start", StateFilter(MailingStates.confirming))
    dp.callback_query.register(cancel_mailing, F.data == "mailing_cancel")
