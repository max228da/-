import asyncio
import re
from aiogram import Dispatcher, F
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, Message, FSInputFile
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.filters import StateFilter

from database import get_setting, set_setting
from marktplaats_parser_fresh import MarktplaatsParserFresh
from parser_result_processor import process_parser_json


class ParserMpStates(StatesGroup):
    waiting_for_url = State()
    waiting_for_published_from = State()
    waiting_for_published_to = State()
    waiting_for_seller_from = State()
    waiting_for_seller_to = State()


def keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔗 Ссылка", callback_data="mp_set_url")],
        [InlineKeyboardButton(text="🌐 Все категории (сброс ссылки)", callback_data="mp_all_categories")],
        [InlineKeyboardButton(text="📅 Публ. от", callback_data="mp_set_pub_from"), InlineKeyboardButton(text="📅 Публ. до", callback_data="mp_set_pub_to")],
        [InlineKeyboardButton(text="👤 Рег. от", callback_data="mp_set_seller_from"), InlineKeyboardButton(text="👤 Рег. до", callback_data="mp_set_seller_to")],
        [InlineKeyboardButton(text="🚀 Запустить парс", callback_data="mp_run")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_main")],
    ])


async def render_menu(target):
    url = await get_setting("mp_base_url") or "🌐 все категории"
    p_from = await get_setting("mp_published_from") or "не задано"
    p_to = await get_setting("mp_published_to") or "не задано"
    s_from = await get_setting("mp_seller_from") or "не задано"
    s_to = await get_setting("mp_seller_to") or "не задано"
    text = (
        "🇳🇱 Парсер Marktplaats.nl\n\n"
        f"Ссылка: {url}\n"
        f"Дата публикации от: {p_from}\n"
        f"Дата публикации до: {p_to}\n"
        f"Дата регистрации продавца от: {s_from}\n"
        f"Дата регистрации продавца до: {s_to}\n\n"
        "Формат даты: YYYY-MM-DD\n"
        "💡 Если ссылка не задана — парсятся все топ-категории по очереди.\n"
        "⚠️ Дата регистрации у Marktplaats приблизительная (из «X jaar op Marktplaats»)."
    )
    if isinstance(target, CallbackQuery):
        await target.message.edit_text(text, reply_markup=keyboard())
    else:
        await target.answer(text, reply_markup=keyboard())


async def open_menu(callback: CallbackQuery):
    await callback.answer()
    await render_menu(callback)


async def ask_url(callback: CallbackQuery, state: FSMContext):
    await callback.answer(); await state.set_state(ParserMpStates.waiting_for_url)
    await callback.message.answer("Отправь ссылку. Пример: https://www.marktplaats.nl/l/computers-en-software/\nИли отправь «-» чтобы парсить все категории.")


async def set_all_categories(callback: CallbackQuery):
    await set_setting("mp_base_url", "")
    await callback.answer("Режим: все категории")
    await render_menu(callback)


async def ask_pub_from(callback: CallbackQuery, state: FSMContext):
    await callback.answer(); await state.set_state(ParserMpStates.waiting_for_published_from)
    await callback.message.answer("Дата публикации ОТ: YYYY-MM-DD или -")


async def ask_pub_to(callback: CallbackQuery, state: FSMContext):
    await callback.answer(); await state.set_state(ParserMpStates.waiting_for_published_to)
    await callback.message.answer("Дата публикации ДО: YYYY-MM-DD или -")


async def ask_seller_from(callback: CallbackQuery, state: FSMContext):
    await callback.answer(); await state.set_state(ParserMpStates.waiting_for_seller_from)
    await callback.message.answer("Дата регистрации продавца ОТ: YYYY-MM-DD или -")


async def ask_seller_to(callback: CallbackQuery, state: FSMContext):
    await callback.answer(); await state.set_state(ParserMpStates.waiting_for_seller_to)
    await callback.message.answer("Дата регистрации продавца ДО: YYYY-MM-DD или -")


async def save_value(message: Message, state: FSMContext, key: str, label: str):
    value = message.text.strip()
    if key != "mp_base_url" and value != "-" and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        await message.answer("❌ Только YYYY-MM-DD или -")
        return
    await set_setting(key, "" if value == "-" else value)
    await state.clear()
    await message.answer(f"✅ {label} сохранено")


async def save_url(message: Message, state: FSMContext): await save_value(message, state, "mp_base_url", "Ссылка")
async def save_pub_from(message: Message, state: FSMContext): await save_value(message, state, "mp_published_from", "Дата публикации от")
async def save_pub_to(message: Message, state: FSMContext): await save_value(message, state, "mp_published_to", "Дата публикации до")
async def save_seller_from(message: Message, state: FSMContext): await save_value(message, state, "mp_seller_from", "Дата регистрации продавца от")
async def save_seller_to(message: Message, state: FSMContext): await save_value(message, state, "mp_seller_to", "Дата регистрации продавца до")


def run_sync(url, p_from, p_to, s_from, s_to, progress_callback=None):
    parser = MarktplaatsParserFresh(url, max_items=100)
    return parser.run(p_from or None, p_to or None, s_from or None, s_to or None, progress_callback)


async def run_parser(callback: CallbackQuery):
    await callback.answer()
    url = await get_setting("mp_base_url") or ""  # пусто = все категории
    p_from = await get_setting("mp_published_from") or ""
    p_to = await get_setting("mp_published_to") or ""
    s_from = await get_setting("mp_seller_from") or ""
    s_to = await get_setting("mp_seller_to") or ""

    progress_message = await callback.message.edit_text("⏳ Идёт парс Marktplaats... 0/100")
    loop = asyncio.get_event_loop()
    queue: asyncio.Queue = asyncio.Queue()
    stop_marker = object()

    def progress_callback(found: int, total: int):
        loop.call_soon_threadsafe(queue.put_nowait, (found, total))

    async def worker():
        last = None
        while True:
            item = await queue.get()
            if item is stop_marker:
                break
            found, total = item
            if last == found:
                continue
            last = found
            try:
                await progress_message.edit_text(f"⏳ Идёт парс Marktplaats... {found}/{total}")
            except Exception:
                pass

    worker_task = asyncio.create_task(worker())
    try:
        result = await loop.run_in_executor(None, run_sync, url, p_from, p_to, s_from, s_to, progress_callback)
    except Exception as e:
        loop.call_soon_threadsafe(queue.put_nowait, stop_marker)
        await worker_task
        await callback.message.answer(f"❌ Ошибка парса: {e}")
        return

    loop.call_soon_threadsafe(queue.put_nowait, stop_marker)
    await worker_task
    await callback.message.answer(f"✅ Парс завершён\nНайдено: {result['count']}\nJSON: {result['json_path']}")
    try:
        await callback.message.answer_document(FSInputFile(result['json_path']))
    except Exception:
        pass

    try:
        txt_path, added_to_vbase, processed_count = process_parser_json(result['json_path'])
        await callback.message.answer(
            f"🗂 База V обновлена\nОбработано строк: {processed_count}\nДобавлено в Базу V: {added_to_vbase}"
        )
        await callback.message.answer_document(FSInputFile(txt_path), caption="Обработанные никнеймы и названия товаров")
    except Exception as e:
        await callback.message.answer(f"⚠️ TXT/База V не созданы: {e}")

    await render_menu(callback)


def register_handlers(dp: Dispatcher):
    dp.callback_query.register(open_menu, F.data == "menu_parser_mp")
    dp.callback_query.register(ask_url, F.data == "mp_set_url")
    dp.callback_query.register(set_all_categories, F.data == "mp_all_categories")
    dp.callback_query.register(ask_pub_from, F.data == "mp_set_pub_from")
    dp.callback_query.register(ask_pub_to, F.data == "mp_set_pub_to")
    dp.callback_query.register(ask_seller_from, F.data == "mp_set_seller_from")
    dp.callback_query.register(ask_seller_to, F.data == "mp_set_seller_to")
    dp.callback_query.register(run_parser, F.data == "mp_run")

    dp.message.register(save_url, StateFilter(ParserMpStates.waiting_for_url))
    dp.message.register(save_pub_from, StateFilter(ParserMpStates.waiting_for_published_from))
    dp.message.register(save_pub_to, StateFilter(ParserMpStates.waiting_for_published_to))
    dp.message.register(save_seller_from, StateFilter(ParserMpStates.waiting_for_seller_from))
    dp.message.register(save_seller_to, StateFilter(ParserMpStates.waiting_for_seller_to))
