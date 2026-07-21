import asyncio
import html
from aiogram import Dispatcher, F, Bot
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.filters import StateFilter
from aiogram.types import CallbackQuery, Message, InlineKeyboardMarkup, InlineKeyboardButton

from database import (
    get_incoming_email,
    get_account_by_id,
    mark_incoming_replied,
    get_setting,
    log_outgoing_email,
    get_product_by_email,
)
from mailer import send_reply_sync
from quick_replies import QUICK_REPLIES, random_quick_reply


async def _send_reply_for(incoming_id: int, reply_text: str):
    """Отправляет reply_text как ответ на входящее письмо. Возвращает (ok, info)."""
    incoming = await get_incoming_email(incoming_id)
    if not incoming:
        return False, "Исходное письмо не найдено"

    account = await get_account_by_id(incoming['account_id'])
    if not account:
        return False, "Аккаунт отправителя не найден"

    global_from_name = await get_setting("global_from_name") or account['from_name'] or "Mailer"
    proxy_config = None
    if account['host']:
        proxy_config = {
            'host': account['host'],
            'port': account['port'],
            'username': account['proxy_user'],
            'password': account['proxy_pass'],
        }

    loop = asyncio.get_event_loop()
    success, result = await loop.run_in_executor(
        None,
        send_reply_sync,
        account['email'],
        account['app_password'],
        global_from_name,
        incoming['from_email'],
        incoming['subject'] or "Reply",
        reply_text,
        incoming['message_id'],
        incoming['references_header'],
        proxy_config,
    )
    if success:
        await log_outgoing_email(account['id'], incoming['from_email'], incoming['subject'] or 'Reply', reply_text, result)
        await mark_incoming_replied(incoming_id)
        return True, incoming['from_email']
    return False, result


def _quick_menu_keyboard(incoming_id: int) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text="🎲 Случайная", callback_data=f"inbox_quick:{incoming_id}:r")]]
    # номера 1..30 сеткой по 6 в ряд
    row = []
    for i in range(len(QUICK_REPLIES)):
        row.append(InlineKeyboardButton(text=str(i + 1), callback_data=f"inbox_quick:{incoming_id}:{i}"))
        if len(row) == 6:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def show_quick_replies(callback: CallbackQuery):
    await callback.answer()
    incoming_id = int(callback.data.split(':', 1)[1])
    incoming = await get_incoming_email(incoming_id)
    if not incoming:
        await callback.message.answer("❌ Письмо не найдено")
        return
    lines = ["⚡ *Быстрые ответы* — выбери номер или 🎲 Случайную:\n"]
    for i, phrase in enumerate(QUICK_REPLIES, 1):
        lines.append(f"{i}. {phrase}")
    text = "\n".join(lines)
    await callback.message.answer(text, reply_markup=_quick_menu_keyboard(incoming_id), parse_mode="Markdown")


async def send_quick_reply(callback: CallbackQuery):
    # формат: inbox_quick:{incoming_id}:{index|r}
    parts = callback.data.split(':')
    incoming_id = int(parts[1])
    sel = parts[2]
    if sel == "r":
        phrase = random_quick_reply()
    else:
        idx = int(sel)
        if idx < 0 or idx >= len(QUICK_REPLIES):
            await callback.answer("Неверный номер")
            return
        phrase = QUICK_REPLIES[idx]

    await callback.answer("Отправляю…")
    ok, info = await _send_reply_for(incoming_id, phrase)
    if ok:
        await callback.message.answer(f"✅ Быстрый ответ отправлен на {info}:\n\n«{phrase}»")
    else:
        await callback.message.answer(f"❌ Ошибка отправки: {info}")


async def show_product(callback: CallbackQuery):
    await callback.answer()
    incoming_id = int(callback.data.split(':', 1)[1])
    incoming = await get_incoming_email(incoming_id)
    if not incoming:
        await callback.message.answer("❌ Письмо не найдено")
        return

    product = await get_product_by_email(incoming['from_email'])
    if not product:
        await callback.message.answer(
            f"🛒 Товар не найден в базе парсера для {incoming['from_email']}.\n"
            "Возможно, продавца парсили до добавления этой функции."
        )
        return

    title = product.get('title') or 'Без названия'
    price = product.get('price') or 'цена не указана'
    listing_url = product.get('listing_url') or ''
    photo_url = product.get('photo_url') or ''

    caption = f"🛒 <b>{html.escape(title)}</b>\n💶 {html.escape(price)}"
    markup = None
    if listing_url:
        markup = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="🔗 Открыть объявление", url=listing_url)]]
        )

    # пробуем отправить фото по URL; если Telegram не смог его забрать — шлём текстом
    if photo_url:
        try:
            await callback.message.answer_photo(
                photo=photo_url, caption=caption, parse_mode="HTML", reply_markup=markup
            )
            return
        except Exception:
            caption += f"\n\n🖼 {html.escape(photo_url)}"
    await callback.message.answer(caption, parse_mode="HTML", reply_markup=markup)


class InboxReplyStates(StatesGroup):
    waiting_for_reply_text = State()


async def start_reply(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    incoming_id = int(callback.data.split(':', 1)[1])
    incoming = await get_incoming_email(incoming_id)
    if not incoming:
        await callback.message.answer("❌ Письмо не найдено в базе")
        return

    await state.set_state(InboxReplyStates.waiting_for_reply_text)
    await state.update_data(incoming_id=incoming_id)
    await callback.message.answer(
        f"✍️ Ответ на письмо\nКому: {incoming['from_email']}\nТема: {incoming['subject'] or 'Без темы'}\n\n"
        f"Отправьте текст ответа одним сообщением."
    )


async def send_reply_message(message: Message, state: FSMContext):
    if not message.text:
        await message.answer("❌ Отправьте текстовое сообщение")
        return

    data = await state.get_data()
    incoming_id = data.get('incoming_id')
    if not incoming_id:
        await state.clear()
        await message.answer("❌ Контекст ответа потерян")
        return

    ok, info = await _send_reply_for(incoming_id, message.text.strip())
    if ok:
        await message.answer(f"✅ Ответ отправлен на {info}")
    else:
        await message.answer(f"❌ Ошибка отправки ответа: {info}")
    await state.clear()


def register_handlers(dp: Dispatcher):
    dp.callback_query.register(start_reply, F.data.startswith("inbox_reply:"))
    dp.callback_query.register(show_product, F.data.startswith("inbox_product:"))
    dp.callback_query.register(show_quick_replies, F.data.startswith("inbox_quick_menu:"))
    dp.callback_query.register(send_quick_reply, F.data.startswith("inbox_quick:"))
    dp.message.register(send_reply_message, StateFilter(InboxReplyStates.waiting_for_reply_text))
