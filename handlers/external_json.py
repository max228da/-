# handlers/external_json.py
"""
Приём JSON-файла стороннего парсера (void-parser и т.п.), присланного в чат
БЕЗ команд/меню. Файл разбирается, лиды кладутся в общую очередь и
обрабатываются (валидация → рассылка) даже если авто-процесс уже запущен.
"""

import asyncio
import os
import logging

from aiogram import Dispatcher, F, Bot
from aiogram.types import Message
from aiogram.filters import StateFilter

from config import TEMP_DIR
from pipeline import process_external_json_sync, process_leads_now

logger = logging.getLogger(__name__)


async def handle_json_file(message: Message, bot: Bot):
    doc = message.document
    # только .json; всё остальное игнорируем (не мешаем другим потокам)
    if not doc or not doc.file_name or not doc.file_name.lower().endswith(".json"):
        return

    path = os.path.join(TEMP_DIR, f"ext_{message.from_user.id}_{doc.file_name}")
    try:
        await bot.download(doc, destination=path)
    except Exception as e:
        await message.reply(f"❌ Не удалось скачать файл: {str(e)[:120]}")
        return

    status = await message.reply("📥 JSON принят, разбираю…")
    loop = asyncio.get_event_loop()
    try:
        leads, total = await loop.run_in_executor(None, process_external_json_sync, path)
    except Exception as e:
        await status.edit_text(f"❌ Не смог разобрать JSON: {str(e)[:150]}")
        return

    if not leads:
        await status.edit_text(
            f"❌ В файле {total} записей, но подходящих продавцов не найдено "
            "(пустые ники / слишком короткие / служебные)."
        )
        return

    # Запускаем СРАЗУ, вне очереди, отдельным таском (с приоритетом над авто-циклом)
    asyncio.create_task(process_leads_now(bot, message.from_user.id, leads, doc.file_name))
    await status.edit_text(
        f"✅ Принято лидов из файла: {len(leads)} (из {total} записей)\n"
        f"⚡ Обрабатываю ВНЕ ОЧЕРЕДИ прямо сейчас (валидация → рассылка).\n"
        f"Авто-цикл уступит приоритет этому файлу."
    )


def register_handlers(dp: Dispatcher):
    # только вне активных меню-флоу (StateFilter(None)) и только документы
    dp.message.register(handle_json_file, StateFilter(None), F.document)
