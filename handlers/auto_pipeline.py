# handlers/auto_pipeline.py
"""
Авто-пайплайн одной кнопкой: парс → валидация → рассылка в бесконечном цикле,
пока пользователь не нажмёт «Стоп». Живая статистика в закреплённом сообщении.
"""

import asyncio
import time
import logging
from typing import Dict, Optional

from aiogram import Dispatcher, F, Bot
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery

from config import (
    AUTO_CYCLE_DELAY,
    AUTO_EMPTY_CYCLE_DELAY,
    AUTO_STATUS_REFRESH,
)
from database import get_setting, set_setting, get_presets
from pipeline import (
    preflight_checks,
    run_parse_cycle,
    run_validation_batch,
    run_mailing_batch,
    dedupe_recipients,
    resolve_sender,
    get_auto_platform,
    file_jobs_active,
    register_session_stats,
    unregister_session_stats,
)

logger = logging.getLogger(__name__)

# Состояние авто-процесса на каждый чат
# chat_id -> {task, stop_event, stats, status_msg_id, started_at}
_RUNS: Dict[int, dict] = {}


def _fmt_duration(seconds: float) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}ч {m}м {s}с"
    if m:
        return f"{m}м {s}с"
    return f"{s}с"


def _menu_keyboard(running: bool, platform: str = "kleinanzeigen") -> InlineKeyboardMarkup:
    if running:
        rows = [
            [InlineKeyboardButton(text="⏹ ОСТАНОВИТЬ", callback_data="auto_stop")],
            [InlineKeyboardButton(text="🔄 Обновить статус", callback_data="auto_refresh")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_main")],
        ]
    else:
        plat_label = "🇳🇱 Marktplaats" if platform == "marktplaats" else "🇩🇪 Kleinanzeigen"
        rows = [
            [InlineKeyboardButton(text=f"Площадка: {plat_label}", callback_data="auto_toggle_platform")],
            [InlineKeyboardButton(text="🚀 ЗАПУСТИТЬ ВСЁ", callback_data="auto_start")],
            [InlineKeyboardButton(text="🩺 Проверить компоненты", callback_data="auto_preflight")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_main")],
        ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _new_stats() -> dict:
    return {
        "state": "инициализация",
        "cycles": 0,
        "parsed_total": 0,
        "validated_total": 0,
        "valid_found_total": 0,
        "sent_total": 0,
        "failed_total": 0,
        "cur_valid_done": 0,
        "cur_valid_total": 0,
        "cur_valid_found": 0,
        "last_cycle_new_leads": 0,
        "last_error": "",
        "recent_errors": [],
    }


def _render_status(stats: dict, started_at: float, running: bool) -> str:
    uptime = _fmt_duration(time.time() - started_at)
    head = "🤖 *АВТО-ПАЙПЛАЙН*"
    head += " — 🟢 работает" if running else " — 🔴 остановлен"

    lines = [
        head,
        "",
        f"⏱ Аптайм: {uptime}",
        f"🔁 Циклов пройдено: {stats['cycles']}",
        f"📍 Сейчас: *{stats['state']}*",
        "",
        "📈 *Всего за сессию:*",
        f"  🛒 Спарсено лидов: {stats['parsed_total']}",
        f"  🔍 Проверено почт: {stats['validated_total']}",
        f"  ✅ Валидных найдено: {stats['valid_found_total']}",
        f"  📨 Отправлено писем: {stats['sent_total']}",
        f"  ❌ Ошибок отправки: {stats['failed_total']}",
    ]

    if stats["state"].startswith("валид") and stats["cur_valid_total"]:
        pct = stats["cur_valid_done"] / stats["cur_valid_total"] * 100
        lines += [
            "",
            f"🔍 Текущая валидация: {stats['cur_valid_done']}/{stats['cur_valid_total']} "
            f"({pct:.0f}%), найдено {stats['cur_valid_found']}",
        ]

    if stats["recent_errors"]:
        lines += ["", "⚠️ *Последние ошибки:*"]
        for e in stats["recent_errors"][-3:]:
            lines.append(f"  • {e}")

    return "\n".join(lines)


async def auto_menu(callback: CallbackQuery):
    await callback.answer()
    chat_id = callback.from_user.id
    running = chat_id in _RUNS and not _RUNS[chat_id]["task"].done()
    platform = await get_auto_platform()

    if running:
        stats = _RUNS[chat_id]["stats"]
        started_at = _RUNS[chat_id]["started_at"]
        text = _render_status(stats, started_at, True)
    else:
        plat_label = "Marktplaats.nl" if platform == "marktplaats" else "Kleinanzeigen"
        text = (
            "🤖 *АВТО-ПАЙПЛАЙН*\n\n"
            "Одна кнопка запускает полный цикл:\n"
            f"1️⃣ Парсинг {plat_label}\n"
            "2️⃣ Валидация найденных почт\n"
            "3️⃣ Рассылка по валидным (рандомное женское имя)\n\n"
            "Цикл повторяется, пока не нажмёшь «Остановить».\n"
            "Перед стартом рекомендуется «Проверить компоненты»."
        )
    await callback.message.edit_text(text, reply_markup=_menu_keyboard(running, platform), parse_mode="Markdown")


async def auto_toggle_platform(callback: CallbackQuery):
    chat_id = callback.from_user.id
    if chat_id in _RUNS and not _RUNS[chat_id]["task"].done():
        await callback.answer("Нельзя менять площадку во время работы")
        return
    current = await get_auto_platform()
    new_platform = "marktplaats" if current != "marktplaats" else "kleinanzeigen"
    await set_setting("auto_platform", new_platform)
    await callback.answer("Marktplaats.nl" if new_platform == "marktplaats" else "Kleinanzeigen")
    await auto_menu(callback)


async def auto_preflight(callback: CallbackQuery):
    await callback.answer("Проверяю компоненты...")
    await callback.message.edit_text("🩺 Проверка компонентов... это может занять до минуты.")
    all_ok, checks = await preflight_checks()
    platform = await get_auto_platform()

    lines = ["🩺 *Проверка компонентов*", ""]
    for c in checks:
        icon = "✅" if c["ok"] else ("❌" if c["critical"] else "⚠️")
        lines.append(f"{icon} {c['name']}: {c['detail']}")
    lines.append("")
    lines.append("✅ Всё готово к запуску!" if all_ok else "❌ Есть критические проблемы — исправь перед запуском.")

    await callback.message.edit_text(
        "\n".join(lines),
        reply_markup=_menu_keyboard(False, platform),
        parse_mode="Markdown",
    )


async def auto_refresh(callback: CallbackQuery):
    chat_id = callback.from_user.id
    running = chat_id in _RUNS and not _RUNS[chat_id]["task"].done()
    if not running:
        await callback.answer("Процесс не запущен")
        await auto_menu(callback)
        return
    await callback.answer("Обновлено")
    stats = _RUNS[chat_id]["stats"]
    started_at = _RUNS[chat_id]["started_at"]
    try:
        await callback.message.edit_text(
            _render_status(stats, started_at, True),
            reply_markup=_menu_keyboard(True),
            parse_mode="Markdown",
        )
    except Exception:
        pass


async def auto_start(callback: CallbackQuery, bot: Bot):
    chat_id = callback.from_user.id

    if chat_id in _RUNS and not _RUNS[chat_id]["task"].done():
        await callback.answer("Уже запущено")
        return

    await callback.answer("Проверяю компоненты перед стартом...")
    await callback.message.edit_text("🩺 Preflight-проверка перед запуском...")
    all_ok, checks = await preflight_checks()
    if not all_ok:
        platform = await get_auto_platform()
        lines = ["❌ *Запуск отменён — проверка не пройдена:*", ""]
        for c in checks:
            if c["critical"] and not c["ok"]:
                lines.append(f"❌ {c['name']}: {c['detail']}")
        await callback.message.edit_text(
            "\n".join(lines),
            reply_markup=_menu_keyboard(False, platform),
            parse_mode="Markdown",
        )
        return

    stop_event = asyncio.Event()
    stats = _new_stats()
    started_at = time.time()

    # Закреплённое сообщение статуса
    status_msg = await bot.send_message(
        chat_id,
        _render_status(stats, started_at, True),
        parse_mode="Markdown",
    )
    try:
        await bot.pin_chat_message(chat_id, status_msg.message_id, disable_notification=True)
    except Exception as e:
        logger.warning("Не удалось закрепить сообщение: %s", e)

    # регистрируем статистику, чтобы обработка присланных файлов писала в тот же счётчик
    register_session_stats(chat_id, stats)

    task = asyncio.create_task(
        _run_loop(bot, chat_id, stop_event, stats, status_msg.message_id, started_at)
    )
    _RUNS[chat_id] = {
        "task": task,
        "stop_event": stop_event,
        "stats": stats,
        "status_msg_id": status_msg.message_id,
        "started_at": started_at,
    }

    await callback.message.edit_text(
        "🟢 Авто-пайплайн запущен!\nСтатистика — в закреплённом сообщении сверху.",
        reply_markup=_menu_keyboard(True),
    )


async def auto_stop(callback: CallbackQuery, bot: Bot):
    chat_id = callback.from_user.id
    await callback.answer("Останавливаю...")

    run = _RUNS.get(chat_id)
    if not run or run["task"].done():
        await callback.message.edit_text("Процесс не запущен.", reply_markup=_menu_keyboard(False))
        return

    run["stop_event"].set()
    try:
        await asyncio.wait_for(run["task"], timeout=30)
    except asyncio.TimeoutError:
        run["task"].cancel()
    except Exception:
        pass

    await callback.message.edit_text(
        "🔴 Авто-пайплайн остановлен.\nИтоги — в закреплённом сообщении.",
        reply_markup=_menu_keyboard(False),
    )


async def _update_status(bot: Bot, chat_id: int, msg_id: int, stats: dict, started_at: float, running: bool):
    try:
        await bot.edit_message_text(
            _render_status(stats, started_at, running),
            chat_id=chat_id,
            message_id=msg_id,
            parse_mode="Markdown",
        )
    except Exception:
        pass  # "message is not modified" / rate limit — игнор


async def _run_loop(bot: Bot, chat_id: int, stop_event: asyncio.Event, stats: dict, msg_id: int, started_at: float):
    """Главный цикл: парс → валидация → рассылка, пока не stop_event."""
    # Фоновый апдейтер статуса
    async def status_ticker():
        while not stop_event.is_set():
            await _update_status(bot, chat_id, msg_id, stats, started_at, True)
            await asyncio.sleep(AUTO_STATUS_REFRESH)

    ticker = asyncio.create_task(status_ticker())

    def push_error(msg: str):
        stats["last_error"] = msg
        stats["recent_errors"].append(msg[:120])
        if len(stats["recent_errors"]) > 10:
            stats["recent_errors"] = stats["recent_errors"][-10:]

    try:
        while not stop_event.is_set():
            stats["cycles"] += 1
            new_leads = 0

            # --- 1. Парсинг ---
            stats["state"] = "парсинг"
            stats["cur_parse_found"] = 0

            def parse_progress(found, total):
                stats["cur_parse_found"] = found
                stats["state"] = f"парсинг: найдено {found}"

            try:
                found, rows = await run_parse_cycle(parse_progress)
                stats["parsed_total"] += len(rows)
                new_leads = len(rows)
                stats["last_cycle_new_leads"] = new_leads
            except Exception as e:
                push_error(f"парсер: {e}")
                rows = []

            if stop_event.is_set():
                break

            # Нет новых лидов — ждём и повторяем
            if not rows:
                stats["state"] = f"ожидание (нет новых лидов), пауза {AUTO_EMPTY_CYCLE_DELAY}с"
                await _sleep_interruptible(AUTO_EMPTY_CYCLE_DELAY, stop_event)
                continue

            # --- 2. Валидация ---
            stats["state"] = "валидация"
            nick_to_title: Dict[str, str] = {}
            for nick, title in rows:
                if nick not in nick_to_title:
                    nick_to_title[nick] = title

            stats["cur_valid_done"] = 0
            stats["cur_valid_total"] = len(nick_to_title)
            stats["cur_valid_found"] = 0

            def valid_progress(done, total, found):
                stats["cur_valid_done"] = done
                stats["cur_valid_total"] = total
                stats["cur_valid_found"] = found

            try:
                valid_pairs = await run_validation_batch(nick_to_title, stop_event, valid_progress)
            except Exception as e:
                push_error(f"валидатор: {e}")
                valid_pairs = []

            stats["validated_total"] += len(nick_to_title)
            stats["valid_found_total"] += len(valid_pairs)

            if stop_event.is_set():
                break

            # --- 3. Рассылка ---
            valid_pairs = await dedupe_recipients(valid_pairs)
            if not valid_pairs:
                stats["state"] = f"нет новых валидных, пауза {AUTO_CYCLE_DELAY}с"
                await _sleep_interruptible(AUTO_CYCLE_DELAY, stop_event)
                continue

            # Уступаем приоритет присланным файлам: если файл обрабатывается —
            # ждём, не начинаем свою рассылку (файл идёт вне очереди).
            if file_jobs_active():
                stats["state"] = "пауза: обрабатываю присланный файл (приоритет)"
                while file_jobs_active() and not stop_event.is_set():
                    await asyncio.sleep(1)
                if stop_event.is_set():
                    break

            stats["state"] = f"рассылка ({len(valid_pairs)} шт.)"
            presets = await get_presets()
            random_sender, fixed_name = await resolve_sender(
                await get_setting("global_from_name") or "Mailer"
            )

            def on_result(success, email, error):
                if success:
                    stats["sent_total"] += 1
                else:
                    stats["failed_total"] += 1
                    if error:
                        push_error(f"{email}: {error[:80]}")

            try:
                await run_mailing_batch(
                    valid_pairs, presets, fixed_name, stop_event, on_result, random_sender
                )
            except Exception as e:
                push_error(f"рассылка: {e}")

            if stop_event.is_set():
                break

            # --- Пауза перед следующим циклом ---
            stats["state"] = f"пауза {AUTO_CYCLE_DELAY}с перед новым циклом"
            await _sleep_interruptible(AUTO_CYCLE_DELAY, stop_event)

    finally:
        stats["state"] = "остановлен"
        unregister_session_stats(chat_id)
        ticker.cancel()
        await _update_status(bot, chat_id, msg_id, stats, started_at, False)


async def _sleep_interruptible(seconds: float, stop_event: asyncio.Event):
    """Спит, но просыпается сразу, если выставлен stop_event."""
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        pass


def register_handlers(dp: Dispatcher):
    dp.callback_query.register(auto_menu, F.data == "menu_auto")
    dp.callback_query.register(auto_toggle_platform, F.data == "auto_toggle_platform")
    dp.callback_query.register(auto_preflight, F.data == "auto_preflight")
    dp.callback_query.register(auto_start, F.data == "auto_start")
    dp.callback_query.register(auto_stop, F.data == "auto_stop")
    dp.callback_query.register(auto_refresh, F.data == "auto_refresh")
