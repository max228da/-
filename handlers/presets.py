import os
from aiogram import types, Dispatcher, F, Bot
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.filters import StateFilter
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from config import TEMP_DIR
from database import add_preset, get_presets, delete_preset


class PresetStates(StatesGroup):
    waiting_for_input = State()
    waiting_for_delete_id = State()


async def presets_menu(callback: CallbackQuery):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📤 Добавить пресеты", callback_data="preset_upload")],
        [InlineKeyboardButton(text="📋 Список пресетов", callback_data="preset_list")],
        [InlineKeyboardButton(text="🗑 Удалить пресет", callback_data="preset_delete")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_main")],
    ])
    await callback.message.edit_text("📝 Управление шаблонами писем", reply_markup=keyboard)
    await callback.answer()


async def ask_for_preset_input(callback: CallbackQuery, state: FSMContext):
    await state.set_state(PresetStates.waiting_for_input)
    await callback.message.answer(
        "📄 Отправьте .txt файл или просто текст в чат.\n"
        "Каждая новая строка = новый пресет.\n"
        "Можно использовать формат: Тема|||Текст письма"
    )
    await callback.answer()


async def process_preset_input(message: types.Message, state: FSMContext, bot: Bot):
    lines = []
    if message.document and message.document.file_name and message.document.file_name.endswith('.txt'):
        file_path = os.path.join(TEMP_DIR, f"presets_{message.from_user.id}.txt")
        await bot.download(message.document, destination=file_path)
        with open(file_path, 'r', encoding='utf-8') as f:
            lines = [line.strip() for line in f if line.strip()]
    elif message.text:
        lines = [line.strip() for line in message.text.splitlines() if line.strip()]
    else:
        await message.reply("❌ Отправьте .txt файл или текст")
        return

    count = 0
    for line in lines:
        if '|||' in line:
            subject, body = line.split('|||', 1)
        else:
            subject, body = "Без темы", line
        if await add_preset(subject.strip(), body.strip()):
            count += 1
    await message.reply(f"✅ Добавлено пресетов: {count}")
    await state.clear()


async def list_presets(callback: CallbackQuery):
    presets = await get_presets()
    if not presets:
        text = "Нет пресетов"
    else:
        text = "📝 Список пресетов:\n\n"
        for p in presets[:20]:
            text += f"ID {p['id']}: {p['subject']}\n"
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="menu_presets")]])
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()


async def ask_for_preset_delete(callback: CallbackQuery, state: FSMContext):
    presets = await get_presets()
    if not presets:
        await callback.answer("Нет пресетов")
        return
    text = "Введите ID пресета для удаления:\n"
    for p in presets[:10]:
        text += f"ID {p['id']}: {p['subject']}\n"
    await callback.message.answer(text)
    await state.set_state(PresetStates.waiting_for_delete_id)
    await callback.answer()


async def process_preset_delete(message: types.Message, state: FSMContext):
    try:
        pid = int(message.text.strip())
        await delete_preset(pid)
        await message.reply("✅ Удалено")
    except Exception:
        await message.reply("❌ Ошибка")
    await state.clear()


def register_handlers(dp: Dispatcher):
    dp.callback_query.register(presets_menu, F.data == "menu_presets")
    dp.callback_query.register(ask_for_preset_input, F.data == "preset_upload")
    dp.callback_query.register(list_presets, F.data == "preset_list")
    dp.callback_query.register(ask_for_preset_delete, F.data == "preset_delete")
    dp.message.register(process_preset_input, StateFilter(PresetStates.waiting_for_input))
    dp.message.register(process_preset_delete, StateFilter(PresetStates.waiting_for_delete_id))
