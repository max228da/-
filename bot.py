import asyncio
import logging
import sys
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery,
    ReplyKeyboardMarkup, KeyboardButton, BotCommand, MenuButtonCommands,
)
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.client.session.aiohttp import AiohttpSession

from config import BOT_TOKEN, TELEGRAM_PROXY
from database import init_db, get_stats, set_setting
from inbox_listener import start_inbox_listener
from parser_result_processor import get_vbase_count

logging.basicConfig(level=logging.INFO, stream=sys.stdout)
logger = logging.getLogger(__name__)

if TELEGRAM_PROXY:
    session = AiohttpSession(proxy=TELEGRAM_PROXY)
    bot = Bot(token=BOT_TOKEN, session=session)
else:
    bot = Bot(token=BOT_TOKEN)

dp = Dispatcher(storage=MemoryStorage())


def _main_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📧 Почты", callback_data="menu_accounts"),
         InlineKeyboardButton(text="🌐 Прокси", callback_data="menu_proxies")],
        [InlineKeyboardButton(text="📝 Пресеты", callback_data="menu_presets"),
         InlineKeyboardButton(text="🔍 Валидатор", callback_data="menu_validator")],
        [InlineKeyboardButton(text="🚀 Рассылка", callback_data="menu_mailing"),
         InlineKeyboardButton(text="📊 Статистика", callback_data="menu_stats")],
        [InlineKeyboardButton(text="🛒 Парсер KA", callback_data="menu_parser_ka"),
         InlineKeyboardButton(text="🇳🇱 Парсер MP", callback_data="menu_parser_mp")],
        [InlineKeyboardButton(text="🇦🇹 Láendle Watcher", callback_data="menu_parser_laendle")],
        [InlineKeyboardButton(text="⚙️ Настройки", callback_data="menu_settings")],
        [InlineKeyboardButton(text="🤖 АВТО-ЗАПУСК (всё сразу)", callback_data="menu_auto")],
    ])


def _reply_keyboard():
    # Постоянная нижняя клавиатура — не исчезает при прокрутке (в отличие от inline).
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🏠 Меню"), KeyboardButton(text="📊 Статистика")],
            [KeyboardButton(text="🤖 Авто-запуск")],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


async def _send_main_menu(message: types.Message):
    await message.answer(
        "🤖 *QUANTUM Mailer Bot*\n\nВыберите действие из меню:",
        reply_markup=_main_keyboard(),
        parse_mode="Markdown",
    )


def _stats_text(stats, vbase_count):
    text = "📊 *Статистика*\n\n"
    text += f"📧 Аккаунтов в работе: {stats['active_accounts']}\n"
    text += f"⏳ В cooldown: {stats['cooldown_accounts']}\n"
    text += f"📋 Всего получателей: {stats['total_recipients']}\n"
    text += f"📨 Отправлено сообщений: {stats['total_sent']}\n"
    text += f"📥 Входящих писем: {stats.get('total_incoming', 0)}\n"
    text += f"🌐 Загружено прокси: {stats['total_proxies']}\n"
    text += f"📝 Загружено пресетов: {stats['total_presets']}\n"
    text += f"🗂 База V: {vbase_count}\n"
    return text


@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await set_setting("inbox_chat_id", str(message.chat.id))
    # 1) закрепляем нижнюю клавиатуру (постоянные кнопки снизу)
    await message.answer(
        "🤖 *QUANTUM Mailer Bot*\n\n"
        "Кнопки закреплены снизу 👇 — они не пропадут.\n"
        "Этот чат привязан для уведомлений о входящих письмах.",
        reply_markup=_reply_keyboard(),
        parse_mode="Markdown",
    )
    # 2) показываем основное inline-меню
    await _send_main_menu(message)


# --- нижние постоянные кнопки ---
@dp.message(F.text == "🏠 Меню")
async def kb_menu(message: types.Message, state: FSMContext):
    await state.clear()
    await _send_main_menu(message)


@dp.message(F.text == "📊 Статистика")
async def kb_stats(message: types.Message, state: FSMContext):
    await state.clear()
    stats = await get_stats()
    await message.answer(
        _stats_text(stats, get_vbase_count()),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Меню", callback_data="back_to_main")]
        ]),
        parse_mode="Markdown",
    )


@dp.message(F.text == "🤖 Авто-запуск")
async def kb_auto(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "🤖 Авто-пайплайн:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🤖 Открыть авто-запуск", callback_data="menu_auto")]
        ]),
    )


@dp.callback_query(F.data == "noop")
async def noop_callback(callback: CallbackQuery):
    await callback.answer()


@dp.callback_query(lambda c: c.data == "back_to_main")
async def back_to_main(callback: CallbackQuery):
    await callback.answer()
    await callback.message.edit_text(
        "🤖 *QUANTUM Mailer Bot*\n\nВыберите действие из меню:",
        reply_markup=_main_keyboard(),
        parse_mode="Markdown",
    )


@dp.callback_query(lambda c: c.data == "menu_stats")
async def show_stats(callback: CallbackQuery):
    await callback.answer()
    stats = await get_stats()
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_main")]])
    await callback.message.edit_text(_stats_text(stats, get_vbase_count()), reply_markup=keyboard, parse_mode="Markdown")


async def on_startup():
    await init_db()
    from laendle_db import init_laendle_db
    await init_laendle_db()
    logger.info("Database initialized")
    # /start в меню команд (слева от строки ввода) + кнопка-меню показывает команды
    try:
        await bot.set_my_commands([
            BotCommand(command="start", description="🏠 Открыть меню"),
        ])
        await bot.set_chat_menu_button(menu_button=MenuButtonCommands())
    except Exception as e:
        logger.warning("Не удалось установить команды бота: %s", e)
    logger.info("Bot started!")


async def main():
    from handlers.validator import register_handlers as register_validator
    from handlers.accounts import register_handlers as register_accounts
    from handlers.proxies import register_handlers as register_proxies
    from handlers.presets import register_handlers as register_presets
    from handlers.mailing import register_handlers as register_mailing
    from handlers.inbox import register_handlers as register_inbox
    from handlers.settings import register_handlers as register_settings
    from handlers.parser_ka_fresh import register_handlers as register_parser_ka
    from handlers.parser_mp import register_handlers as register_parser_mp
    from handlers.auto_pipeline import register_handlers as register_auto
    from handlers.external_json import register_handlers as register_external
    from handlers.parser_laendle import register_handlers as register_parser_laendle, start_laendle_monitor

    register_validator(dp)
    register_accounts(dp)
    register_proxies(dp)
    register_presets(dp)
    register_mailing(dp)
    register_inbox(dp)
    register_settings(dp)
    register_parser_ka(dp)
    register_parser_mp(dp)
    register_auto(dp)
    register_external(dp)
    register_parser_laendle(dp)

    await on_startup()
    asyncio.create_task(start_inbox_listener(bot))
    asyncio.create_task(start_laendle_monitor(bot))
    await dp.start_polling(bot, drop_pending_updates=True)


if __name__ == '__main__':
    asyncio.run(main())
