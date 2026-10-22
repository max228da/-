"""
Хендлер модуля laendleanzeiger: мониторинг новых объявлений, уведомления в TG,
черновики сообщений продавцам (консигнация) и воронка сделок.

Границы (осознанно):
- один твой аккаунт, ручной темп;
- отправку сообщений через мессенджер сайта НЕ автоматизируем — бот доводит до
  готового текста и кнопки «Открыть», пишет пользователь сам;
- телефоны продавцов не парсим/не расшифровываем.
"""
import asyncio
import html
import logging
import time

from aiogram import Dispatcher, F
from aiogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, Message,
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.filters import StateFilter

from database import get_setting, set_setting
from laendle_parser import LaendleParser
import laendle_db as ldb
import laendle_drafts as drafts

logger = logging.getLogger(__name__)

# ---- дефолты настроек ----
DEFAULT_INTERVAL = 600          # сек между авто-проверками (10 мин)
DEFAULT_MAX_PAGES = 2           # страниц на наблюдение за проход
ENRICH_LIMIT = 20               # сколько новых объявлений обогащать продавцом за проход
ENRICH_DELAY = 1.0              # пауза между обогащениями (по-человечески)


class LaeStates(StatesGroup):
    add_category = State()
    add_search = State()
    set_shop = State()
    set_interval = State()
    note = State()


# ---------------- настройки (хелперы) ----------------
async def _get(key, default=None):
    v = await get_setting(key)
    return default if v is None else v


async def _flag(key, default="0"):
    return (await _get(key, default)) == "1"


# ---------------- меню ----------------
async def render_menu(target):
    st = await ldb.laendle_stats()
    shop = await _get("lae_shop_name", "— не задано —")
    auto = "🟢 вкл" if await _flag("lae_auto") else "🔴 выкл"
    interval = await _get("lae_interval", str(DEFAULT_INTERVAL))
    ai = "🟢" if await _flag("lae_use_ai") else "🔴"
    prio = "🟢" if await _flag("lae_priority_only") else "🔴"
    text = (
        "🇦🇹 <b>Láendle Watcher</b> — laendleanzeiger.at\n\n"
        f"🏬 Магазин: <b>{html.escape(shop)}</b>\n"
        f"👁 Активных наблюдений: <b>{st['active_watches']}</b>\n"
        f"📦 Объявлений в базе: <b>{st['total_listings']}</b> (новых: {st['new_listings']})\n"
        f"🤖 Авто-монитор: <b>{auto}</b> (каждые {int(interval)//60} мин)\n"
        f"✍️ ИИ-черновики: {ai}   ⭐ Только приоритетные: {prio}\n\n"
        "Выбери действие:"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔎 Сканировать сейчас", callback_data="lae_scan")],
        [InlineKeyboardButton(text="👁 Наблюдения", callback_data="lae_watches"),
         InlineKeyboardButton(text="📊 Воронка", callback_data="lae_funnel")],
        [InlineKeyboardButton(text=f"🤖 Авто-монитор: {auto}", callback_data="lae_toggle_auto")],
        [InlineKeyboardButton(text=f"✍️ ИИ-черновики {ai}", callback_data="lae_toggle_ai"),
         InlineKeyboardButton(text=f"⭐ Приоритет {prio}", callback_data="lae_toggle_prio")],
        [InlineKeyboardButton(text="🏬 Имя магазина", callback_data="lae_set_shop"),
         InlineKeyboardButton(text="⏱ Интервал", callback_data="lae_set_interval")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_main")],
    ])
    if isinstance(target, CallbackQuery):
        try:
            await target.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
        except Exception:
            await target.message.answer(text, reply_markup=kb, parse_mode="HTML")
    else:
        await target.answer(text, reply_markup=kb, parse_mode="HTML")


async def open_menu(callback: CallbackQuery):
    await callback.answer()
    # запоминаем чат для уведомлений авто-монитора
    await set_setting("lae_chat_id", str(callback.message.chat.id))
    await render_menu(callback)


# ---------------- наблюдения ----------------
async def render_watches(callback: CallbackQuery):
    watches = await ldb.list_watches()
    lines = ["👁 <b>Наблюдения</b> (что мониторим):\n"]
    rows = []
    if not watches:
        lines.append("Пока пусто. Добавь категорию или поисковый запрос.")
    for w in watches:
        mark = "🟢" if w["enabled"] else "⚪️"
        kind = {"category": "кат.", "search": "поиск", "url": "url"}.get(w["kind"], w["kind"])
        lines.append(f"{mark} <b>{html.escape(w['label'])}</b> <i>({kind})</i>")
        rows.append([
            InlineKeyboardButton(text=f"{mark} {w['label'][:20]}", callback_data=f"lae_wtg:{w['id']}"),
            InlineKeyboardButton(text="🗑", callback_data=f"lae_wdel:{w['id']}"),
        ])
    rows.append([
        InlineKeyboardButton(text="➕ Категория", callback_data="lae_wadd_cat"),
        InlineKeyboardButton(text="➕ Поиск", callback_data="lae_wadd_search"),
    ])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="menu_parser_laendle")])
    await callback.message.edit_text(
        "\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=rows), parse_mode="HTML")


async def watches_menu(callback: CallbackQuery):
    await callback.answer()
    await render_watches(callback)


async def watch_toggle(callback: CallbackQuery):
    wid = int(callback.data.split(":")[1])
    await ldb.toggle_watch(wid)
    await callback.answer("Переключено")
    await render_watches(callback)


async def watch_delete(callback: CallbackQuery):
    wid = int(callback.data.split(":")[1])
    await ldb.delete_watch(wid)
    await callback.answer("Удалено")
    await render_watches(callback)


async def ask_add_cat(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.set_state(LaeStates.add_category)
    await callback.message.answer(
        "Пришли категорию. Примеры:\n"
        "<code>elektronik</code>\n"
        "<code>elektronik/computer</code>\n"
        "<code>auto-motorrad</code>\n"
        "или полный URL листинга с laendleanzeiger.at",
        parse_mode="HTML")


async def ask_add_search(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.set_state(LaeStates.add_search)
    await callback.message.answer(
        "Пришли поисковый запрос (как ищешь на сайте). Примеры:\n"
        "<code>iphone</code>, <code>dyson</code>, <code>macbook pro</code>",
        parse_mode="HTML")


async def save_add_cat(message: Message, state: FSMContext):
    val = message.text.strip()
    kind = "url" if val.startswith("http") else "category"
    ok = await ldb.add_watch(kind, val, label=val.split("/")[-1].strip("/ ") or val)
    await state.clear()
    await message.answer("✅ Добавлено" if ok else "⚠️ Уже есть такое наблюдение")


async def save_add_search(message: Message, state: FSMContext):
    val = message.text.strip()
    ok = await ldb.add_watch("search", val, label=f"🔎 {val}")
    await state.clear()
    await message.answer("✅ Добавлено" if ok else "⚠️ Уже есть такое наблюдение")


# ---------------- настройки: магазин / интервал / тумблеры ----------------
async def ask_shop(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.set_state(LaeStates.set_shop)
    await callback.message.answer("Пришли имя магазина (как представляться продавцам):")


async def save_shop(message: Message, state: FSMContext):
    await set_setting("lae_shop_name", message.text.strip()[:80])
    await state.clear()
    await message.answer("✅ Имя магазина сохранено")


async def ask_interval(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.set_state(LaeStates.set_interval)
    await callback.message.answer("Интервал авто-проверки в минутах (например 10):")


async def save_interval(message: Message, state: FSMContext):
    try:
        minutes = max(2, min(int(message.text.strip()), 720))
    except ValueError:
        await message.answer("❌ Нужно число минут")
        return
    await set_setting("lae_interval", str(minutes * 60))
    await state.clear()
    await message.answer(f"✅ Интервал: каждые {minutes} мин")


async def toggle_auto(callback: CallbackQuery):
    new = "0" if await _flag("lae_auto") else "1"
    await set_setting("lae_auto", new)
    await set_setting("lae_chat_id", str(callback.message.chat.id))
    await callback.answer("Авто-монитор " + ("включён" if new == "1" else "выключен"))
    await render_menu(callback)


async def toggle_ai(callback: CallbackQuery):
    new = "0" if await _flag("lae_use_ai") else "1"
    await set_setting("lae_use_ai", new)
    await callback.answer()
    await render_menu(callback)


async def toggle_prio(callback: CallbackQuery):
    new = "0" if await _flag("lae_priority_only") else "1"
    await set_setting("lae_priority_only", new)
    await callback.answer()
    await render_menu(callback)


# ---------------- уведомление-карточка ----------------
def _card_caption(it: dict) -> str:
    badges = []
    if it.get("is_premium"):
        badges.append("★ Premium")
    if it.get("has_phone"):
        badges.append("📞 верифиц. номер")
    if (it.get("priority_score") or 0) >= 3:
        badges.append("⭐ приоритет")
    badge = ("  ·  ".join(badges)) if badges else ""
    seller = f"\n👤 {html.escape(it['seller_name'])}" if it.get("seller_name") else ""
    cap = (
        f"<b>{html.escape(it.get('title') or '')}</b>\n"
        f"💶 {html.escape(it.get('price_display') or '—')}"
        + (f"  (–{html.escape(it['price_old'])})" if it.get("price_old") else "")
        + f"\n📍 {html.escape(it.get('location') or '')}  ·  🗓 {html.escape(it.get('date_raw') or '')}"
        f"\n🏷 {html.escape(it.get('category') or '')}"
        + seller
        + (f"\n{badge}" if badge else "")
    )
    return cap[:1000]


def _card_kb(ad_id: str, url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔗 Открыть", url=url),
         InlineKeyboardButton(text="✍️ Черновик", callback_data=f"lae_draft:{ad_id}")],
        [InlineKeyboardButton(text="📂 Статус сделки", callback_data=f"lae_deal:{ad_id}")],
    ])


async def _send_card(bot, chat_id, it: dict):
    cap = _card_caption(it)
    kb = _card_kb(it["ad_id"], it.get("listing_url") or "https://www.laendleanzeiger.at")
    photo = it.get("photo_url")
    try:
        if photo:
            await bot.send_photo(chat_id, photo=photo, caption=cap, reply_markup=kb, parse_mode="HTML")
        else:
            await bot.send_message(chat_id, cap, reply_markup=kb, parse_mode="HTML",
                                   disable_web_page_preview=True)
    except Exception as e:
        logger.warning("laendle card send failed: %s", e)
        try:
            await bot.send_message(chat_id, cap + f"\n\n🔗 {it.get('listing_url')}",
                                   reply_markup=kb, parse_mode="HTML")
        except Exception:
            pass


# ---------------- ядро сканирования ----------------
async def run_scan(bot, chat_id, progress_msg=None) -> dict:
    """Сканирует включённые наблюдения, кладёт новые в БД, шлёт карточки. Возвращает сводку."""
    watches = await ldb.list_watches(only_enabled=True)
    if not watches:
        return {"new": 0, "total": 0, "no_watches": True}

    max_pages = int(await _get("lae_max_pages", str(DEFAULT_MAX_PAGES)))
    priority_only = await _flag("lae_priority_only")

    parser = LaendleParser(max_pages=max_pages)
    loop = asyncio.get_event_loop()
    watch_dicts = [{"value": w["value"], "label": w["label"], "kind": w["kind"]} for w in watches]

    def _do():
        return parser.scan(watch_dicts, max_items_per_watch=60)

    items = await loop.run_in_executor(None, _do)

    # отбираем новые (upsert возвращает True для новых)
    new_items = []
    for it in items:
        if await ldb.upsert_listing(it.to_dict()):
            new_items.append(it)

    # обогащаем продавцом первые N новых (по-человечески, с паузой)
    to_enrich = new_items[:ENRICH_LIMIT]
    for it in to_enrich:
        enriched = await loop.run_in_executor(None, parser.enrich, it)
        await ldb.update_listing_fields(
            it.ad_id,
            seller_name=enriched.seller_name,
            seller_profile_url=enriched.seller_profile_url,
            has_phone=1 if enriched.has_validated_phone else 0,
        )
        await asyncio.sleep(ENRICH_DELAY)

    # шлём карточки (с учётом фильтра приоритета)
    sent = 0
    for it in new_items:
        row = await ldb.get_listing(it.ad_id)
        if priority_only and (row.get("priority_score") or 0) < 3:
            await ldb.mark_notified(it.ad_id)
            continue
        await _send_card(bot, chat_id, row)
        await ldb.mark_notified(it.ad_id)
        sent += 1
        await asyncio.sleep(0.3)   # мягко к Telegram rate-limit

    return {"new": len(new_items), "sent": sent, "total": len(items)}


async def scan_now(callback: CallbackQuery):
    await callback.answer()
    chat_id = callback.message.chat.id
    await set_setting("lae_chat_id", str(chat_id))
    msg = await callback.message.answer("🔎 Сканирую laendleanzeiger…")
    try:
        res = await run_scan(callback.bot, chat_id, progress_msg=msg)
    except Exception as e:
        logger.exception("laendle scan error")
        await msg.edit_text(f"❌ Ошибка сканирования: {e}")
        return
    if res.get("no_watches"):
        await msg.edit_text("⚠️ Нет активных наблюдений. Добавь категорию или поиск в «Наблюдения».")
        return
    await msg.edit_text(
        f"✅ Готово.\nПросмотрено: {res['total']}\nНовых: {res['new']}\nОтправлено карточек: {res.get('sent', 0)}")


# ---------------- черновик ----------------
async def make_draft_cb(callback: CallbackQuery):
    ad_id = callback.data.split(":", 1)[1]
    await callback.answer("Готовлю черновик…")
    row = await ldb.get_listing(ad_id)
    if not row:
        await callback.message.answer("⚠️ Объявление не найдено в базе.")
        return
    shop = await _get("lae_shop_name", "")
    use_ai = await _flag("lae_use_ai")
    api_key = await _get("lae_api_key", None)
    loop = asyncio.get_event_loop()
    text = await loop.run_in_executor(
        None, lambda: drafts.make_draft(row, shop, use_ai=use_ai, api_key=api_key))
    await ldb.set_draft(ad_id, text)
    if row.get("deal_status") == "new":
        await ldb.set_deal_status(ad_id, "drafted")
    await callback.message.answer(
        "✍️ <b>Черновик для продавца</b> (скопируй и отправь сам в мессенджере сайта "
        "со своего аккаунта — автоотправку мы не делаем):\n\n"
        f"<pre>{html.escape(text)}</pre>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔗 Открыть объявление", url=row.get("listing_url"))],
            [InlineKeyboardButton(text="📨 Отметить «написал»",
                                  callback_data=f"lae_sst:contacted:{ad_id}")],
        ]))


# ---------------- воронка сделок ----------------
async def funnel(callback: CallbackQuery):
    await callback.answer()
    counts = await ldb.funnel_counts()
    lines = ["📊 <b>Воронка сделок</b>\n"]
    rows = []
    for key, label in ldb.DEAL_STATUSES:
        n = counts.get(key, 0)
        lines.append(f"{label}: <b>{n}</b>")
        if n:
            rows.append([InlineKeyboardButton(text=f"{label} ({n})",
                                              callback_data=f"lae_list:{key}")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="menu_parser_laendle")])
    await callback.message.edit_text("\n".join(lines),
                                     reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
                                     parse_mode="HTML")


async def list_status(callback: CallbackQuery):
    status = callback.data.split(":", 1)[1]
    await callback.answer()
    items = await ldb.list_by_status(status, limit=20)
    label = ldb.DEAL_STATUS_LABELS.get(status, status)
    if not items:
        await callback.message.answer(f"{label}: пусто")
        return
    await callback.message.answer(f"{label} — {len(items)} шт:")
    for it in items:
        await _send_card(callback.bot, callback.message.chat.id, it)
        await asyncio.sleep(0.2)


async def deal_menu(callback: CallbackQuery):
    ad_id = callback.data.split(":", 1)[1]
    await callback.answer()
    row = await ldb.get_listing(ad_id)
    if not row:
        await callback.message.answer("⚠️ Не найдено.")
        return
    cur = ldb.DEAL_STATUS_LABELS.get(row.get("deal_status"), row.get("deal_status"))
    rows = []
    line = []
    for key, lbl in ldb.DEAL_STATUSES:
        line.append(InlineKeyboardButton(text=lbl, callback_data=f"lae_sst:{key}:{ad_id}"))
        if len(line) == 2:
            rows.append(line); line = []
    if line:
        rows.append(line)
    await callback.message.answer(
        f"📂 <b>{html.escape(row.get('title') or '')}</b>\nТекущий статус: <b>{cur}</b>\n\nВыбери новый статус:",
        parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


async def set_status_cb(callback: CallbackQuery):
    _, status, ad_id = callback.data.split(":", 2)
    await ldb.set_deal_status(ad_id, status)
    await callback.answer("Статус: " + ldb.DEAL_STATUS_LABELS.get(status, status))


# ---------------- фоновый монитор ----------------
async def start_laendle_monitor(bot):
    """Запускается из bot.main(). Крутит авто-сканы, когда включён авто-монитор."""
    await asyncio.sleep(10)  # дать боту стартовать
    while True:
        try:
            if await _flag("lae_auto"):
                chat_id = await _get("lae_chat_id")
                watches = await ldb.list_watches(only_enabled=True)
                if chat_id and watches:
                    res = await run_scan(bot, int(chat_id))
                    if res.get("sent"):
                        logger.info("laendle auto: %s new card(s)", res["sent"])
            interval = int(await _get("lae_interval", str(DEFAULT_INTERVAL)))
        except Exception:
            logger.exception("laendle monitor loop error")
            interval = DEFAULT_INTERVAL
        await asyncio.sleep(max(120, interval))


# ---------------- регистрация ----------------
def register_handlers(dp: Dispatcher):
    dp.callback_query.register(open_menu, F.data == "menu_parser_laendle")
    dp.callback_query.register(scan_now, F.data == "lae_scan")
    dp.callback_query.register(watches_menu, F.data == "lae_watches")
    dp.callback_query.register(funnel, F.data == "lae_funnel")
    dp.callback_query.register(toggle_auto, F.data == "lae_toggle_auto")
    dp.callback_query.register(toggle_ai, F.data == "lae_toggle_ai")
    dp.callback_query.register(toggle_prio, F.data == "lae_toggle_prio")
    dp.callback_query.register(ask_shop, F.data == "lae_set_shop")
    dp.callback_query.register(ask_interval, F.data == "lae_set_interval")
    dp.callback_query.register(ask_add_cat, F.data == "lae_wadd_cat")
    dp.callback_query.register(ask_add_search, F.data == "lae_wadd_search")
    dp.callback_query.register(watch_toggle, F.data.startswith("lae_wtg:"))
    dp.callback_query.register(watch_delete, F.data.startswith("lae_wdel:"))
    dp.callback_query.register(make_draft_cb, F.data.startswith("lae_draft:"))
    dp.callback_query.register(deal_menu, F.data.startswith("lae_deal:"))
    dp.callback_query.register(list_status, F.data.startswith("lae_list:"))
    dp.callback_query.register(set_status_cb, F.data.startswith("lae_sst:"))

    dp.message.register(save_add_cat, StateFilter(LaeStates.add_category))
    dp.message.register(save_add_search, StateFilter(LaeStates.add_search))
    dp.message.register(save_shop, StateFilter(LaeStates.set_shop))
    dp.message.register(save_interval, StateFilter(LaeStates.set_interval))
