# pipeline.py
"""
Ядро авто-пайплайна: переиспользуемые блоки preflight / validation / mailing,
которые крутит оркестратор из handlers/auto_pipeline.py.

Всё, что здесь блокирующее (парсер, валидатор, отправка), запускается через
run_in_executor — event loop aiogram не блокируется.
"""

import asyncio
import json
import logging
import os
import random
import tempfile
import time
from typing import Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Глобальный лок на ОТПРАВКУ: и авто-пайплайн, и обработка присланных файлов
# шлют письма по очереди, чтобы не пробить лимит 20/3мин на аккаунт при пересечении.
MAILING_LOCK = asyncio.Lock()

from config import (
    VALIDATOR_DOMAINS,
    VALIDATOR_THREADS,
    VALIDATOR_TIMEOUT,
    VALIDATOR_PROXY,
    VALIDATOR_PROXIES,
    VALIDATOR_BATCH_SIZE,
    VALIDATOR_MAX_REQUEUE,
    VALIDATOR_FROM_EMAIL,
    VALIDATOR_HELO_HOST,
    MAILING_THREADS,
    MAILING_COOLDOWN,
    MAILING_PROXY,
    MAILING_BATCH_DELAY,
    MAILING_ACCOUNT_RESERVE,
    MAILING_RETRIES,
    MAILING_RETRY_DELAY,
    MAILING_DAILY_LIMIT,
    MAILING_COOLDOWN_JITTER,
    MAILING_PROXIES,
    MAILING_BURST_SIZE,
    MAILING_SESSIONS_PER_ACCOUNT,
    MAILING_MAX_WORKERS,
    MAILING_RATE_COUNT,
    MAILING_RATE_WINDOW,
    AUTO_PREFLIGHT_LOGIN_CHECK,
    AUTO_PARSE_MAX_ITEMS,
)
from database import (
    get_setting,
    get_presets,
    get_accounts,
    count_active_accounts,
    get_sendable_accounts,
    get_account_daily_sent,
    increment_sent_count,
    set_account_cooldown,
    log_mailing_error,
    log_outgoing_email,
    filter_unsent_emails,
)
from mailer import check_smtp_login, check_proxy, send_email_sync, send_batch_sync
from utils.email_validator import BatchEmailValidator
from kleinanzeigen_parser_fresh import KleinanzeigenParserFresh
from marktplaats_parser_fresh import MarktplaatsParserFresh
from parser_result_processor import process_parser_json
from namegen import random_female_name

# --- Присланные JSON-файлы стороннего парсера ---
# Обрабатываются ВНЕ ОЧЕРЕДИ: каждый файл запускается своим таском сразу,
# а авто-пайплайн уступает им приоритет (пауза перед своей рассылкой).
_active_file_jobs = 0

# Реестр статистики активных авто-процессов: chat_id -> stats dict.
# Обработка присланных файлов дописывает результаты СЮДА, чтобы они попадали
# в общий счётчик закреплённого сообщения.
_session_stats: Dict[int, dict] = {}


def register_session_stats(chat_id: int, stats: dict):
    _session_stats[chat_id] = stats


def unregister_session_stats(chat_id: int):
    _session_stats.pop(chat_id, None)


def _bump_session_stats(chat_id: int, **deltas):
    st = _session_stats.get(chat_id)
    if not st:
        return
    for key, val in deltas.items():
        st[key] = st.get(key, 0) + val


def file_jobs_active() -> bool:
    return _active_file_jobs > 0


async def process_leads_now(bot, chat_id: int, leads: List[Tuple[str, str]], source: str = "файл"):
    """Обрабатывает лиды из файла НЕМЕДЛЕННО (валидация → рассылка), с приоритетом."""
    global _active_file_jobs
    _active_file_jobs += 1
    try:
        await _process_job(bot, {"chat_id": chat_id, "leads": leads, "source": source})
    except Exception:
        logger.exception("Обработка присланного файла упала")
    finally:
        _active_file_jobs -= 1


def _external_to_internal_items(data) -> list:
    """Маппинг стороннего JSON (void-parser и т.п.) в нашу схему items."""
    if isinstance(data, dict):
        items = data.get("items", [])
    elif isinstance(data, list):
        items = data
    else:
        items = []
    internal = []
    for it in items:
        if not isinstance(it, dict):
            continue
        internal.append({
            "seller_nickname": it.get("item_person_name") or it.get("seller_nickname") or "",
            "title": it.get("item_title") or it.get("title") or "",
            "price": it.get("item_price") or it.get("price") or "",
            "photo_url": it.get("item_photo") or it.get("photo_url") or "",
            "listing_url": it.get("item_link") or it.get("listing_url") or "",
        })
    return internal


def process_external_json_sync(path: str) -> Tuple[List[Tuple[str, str]], int]:
    """
    Разбирает присланный JSON-файл: сохраняет товары/базу V (переиспользуя
    process_parser_json) и возвращает (leads=[(nick,title)], всего_в_файле).
    Блокирующая — вызывать через run_in_executor.
    """
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    internal = _external_to_internal_items(data)
    total = len(internal)

    tmp = tempfile.mktemp(suffix="_ext.json")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"items": internal}, f, ensure_ascii=False)
    try:
        txt_path, _added, _cnt = process_parser_json(tmp)
    finally:
        try:
            os.remove(tmp)
        except Exception:
            pass

    leads: List[Tuple[str, str]] = []
    try:
        with open(txt_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                if "|" in line:
                    nick, title = line.split("|", 1)
                    leads.append((nick.strip(), title.strip()))
                else:
                    leads.append((line, ""))
    except Exception:
        pass
    return leads, total


async def _process_job(bot, job):
    chat_id = job["chat_id"]
    leads = job["leads"]
    source = job.get("source", "файл")

    nick_to_title: Dict[str, str] = {}
    for nick, title in leads:
        if nick and nick not in nick_to_title:
            nick_to_title[nick] = title

    if not nick_to_title:
        await bot.send_message(chat_id, f"⚠️ {source}: подходящих продавцов не найдено")
        return

    # в общий счётчик активного авто-процесса: спарсено (лиды из файла)
    _bump_session_stats(chat_id, parsed_total=len(nick_to_title))

    await bot.send_message(chat_id, f"🔍 {source}: валидирую {len(nick_to_title)} почт...")
    stop = asyncio.Event()
    try:
        valid = await run_validation_batch(nick_to_title, stop)
    except Exception as e:
        await bot.send_message(chat_id, f"❌ {source}: ошибка валидации: {str(e)[:150]}")
        return

    valid = await dedupe_recipients(valid)
    # в общий счётчик: проверено почт + найдено валидных
    _bump_session_stats(chat_id, validated_total=len(nick_to_title), valid_found_total=len(valid))

    if not valid:
        await bot.send_message(chat_id, f"ℹ️ {source}: новых валидных почт нет (все уже проверены/отправлены)")
        return

    presets = await get_presets()
    if not presets:
        await bot.send_message(chat_id, f"❌ {source}: нет пресетов — рассылка невозможна")
        return

    random_sender, fixed_name = await resolve_sender(await get_setting("global_from_name") or "Mailer")
    await bot.send_message(chat_id, f"🚀 {source}: рассылка по {len(valid)} валидным почтам...")

    def on_result(success, email, error):
        pass

    sent, failed = await run_mailing_batch(valid, presets, fixed_name, stop, on_result, random_sender)
    # в общий счётчик: отправлено + ошибки
    _bump_session_stats(chat_id, sent_total=sent, failed_total=failed)
    await bot.send_message(
        chat_id,
        f"✅ {source} обработан: найдено валидных {len(valid)}, отправлено {sent}, ошибок {failed}",
    )


async def resolve_sender(from_name: str) -> "tuple[bool, str]":
    """
    Возвращает (random_mode, fixed_name).
    По умолчанию режим 'random' — на каждое письмо генерится женское имя.
    """
    mode = await get_setting("sender_mode") or "random"
    return (mode != "fixed", from_name)


# ------------------------------------------------------------------ PREFLIGHT

async def preflight_checks() -> Tuple[bool, List[Dict]]:
    """
    Проверяет все компоненты, нужные для запуска авто-цикла.
    Возвращает (all_ok, checks), где checks — список
    {name, ok, detail, critical}.
    """
    checks: List[Dict] = []
    loop = asyncio.get_event_loop()

    # 1. Ссылка парсера (активной площадки)
    platform = await get_auto_platform()
    keys = _platform_setting_keys(platform)
    base_url = await get_setting(keys["url"])
    platform_label = "Marktplaats" if platform == "marktplaats" else "Kleinanzeigen"
    checks.append({
        "name": f"Парсер [{platform_label}]",
        "ok": True,  # пустая ссылка = режим "все категории", тоже валидно
        "detail": base_url if base_url else "все категории (ссылка не задана)",
        "critical": False,
    })

    # 2. Пресеты
    presets = await get_presets()
    checks.append({
        "name": "Пресеты писем",
        "ok": len(presets) > 0,
        "detail": f"{len(presets)} шт." if presets else "нет пресетов (меню 📝 Пресеты)",
        "critical": True,
    })

    # 3. Имя отправителя
    from_name = await get_setting("global_from_name")
    checks.append({
        "name": "Имя отправителя",
        "ok": True,  # не критично, есть дефолт
        "detail": from_name or "не задано, будет 'Mailer'",
        "critical": False,
    })

    # 4. Аккаунты
    active_count = await count_active_accounts()
    checks.append({
        "name": "Активные аккаунты",
        "ok": active_count > 0,
        "detail": f"{active_count} шт." if active_count else "нет аккаунтов (меню 📧 Почты)",
        "critical": True,
    })

    # 5. Прокси рассылки
    if MAILING_PROXY:
        ok, info = await loop.run_in_executor(None, check_proxy, MAILING_PROXY, 15)
        checks.append({
            "name": "Прокси рассылки",
            "ok": ok,
            "detail": f"IP: {info}" if ok else f"не отвечает: {info}",
            "critical": True,
        })
    else:
        checks.append({
            "name": "Прокси рассылки",
            "ok": True,
            "detail": "не используется (прямое подключение)",
            "critical": False,
        })

    # 6. SMTP-логин аккаунтов (опционально — самый медленный чек)
    if AUTO_PREFLIGHT_LOGIN_CHECK and active_count > 0:
        accounts = await get_accounts(100)
        active = [a for a in accounts if a["status"] == "active"]
        ok_logins = 0
        checked = 0
        proxy_cfg = MAILING_PROXY if MAILING_PROXY else None
        for a in active:
            checked += 1
            success, _ = await loop.run_in_executor(
                None, check_smtp_login, a["email"], a["app_password"], proxy_cfg, False
            )
            if success:
                ok_logins += 1
        checks.append({
            "name": "SMTP-логин аккаунтов",
            "ok": ok_logins > 0,
            "detail": f"работают {ok_logins}/{checked}",
            "critical": True,
        })

    all_ok = all(c["ok"] for c in checks if c["critical"])
    return all_ok, checks


# ------------------------------------------------------------------ PARSE

def _parse_sync(platform: str, base_url: str, p_from, p_to, s_from, s_to, progress_cb=None) -> dict:
    if platform == "marktplaats":
        parser = MarktplaatsParserFresh(base_url, max_items=AUTO_PARSE_MAX_ITEMS)
    else:
        parser = KleinanzeigenParserFresh(base_url, max_items=AUTO_PARSE_MAX_ITEMS)
    return parser.run(p_from or None, p_to or None, s_from or None, s_to or None, progress_cb)


async def get_auto_platform() -> str:
    return await get_setting("auto_platform") or "kleinanzeigen"


def _platform_setting_keys(platform: str) -> Dict[str, str]:
    prefix = "mp_" if platform == "marktplaats" else "ka2_"
    return {
        "url": f"{prefix}base_url",
        "pub_from": f"{prefix}published_from",
        "pub_to": f"{prefix}published_to",
        "seller_from": f"{prefix}seller_from",
        "seller_to": f"{prefix}seller_to",
    }


async def run_parse_cycle(progress_cb: Optional[Callable[[int, int], None]] = None) -> Tuple[int, List[Tuple[str, str]]]:
    """
    Один проход парсера (по выбранной площадке auto_platform).
    Возвращает (found_count, [(nickname, title), ...]).
    progress_cb(found, total) — вызывается по мере нахождения лидов (для живого статуса).
    Список ников — только НОВЫЕ (парсер сам фильтрует уже виденных продавцов).
    """
    loop = asyncio.get_event_loop()
    platform = await get_auto_platform()
    keys = _platform_setting_keys(platform)

    # пустой base_url → парсер сам перейдёт в режим "все категории"
    base_url = await get_setting(keys["url"]) or ""
    p_from = await get_setting(keys["pub_from"]) or ""
    p_to = await get_setting(keys["pub_to"]) or ""
    s_from = await get_setting(keys["seller_from"]) or ""
    s_to = await get_setting(keys["seller_to"]) or ""

    # прокидываем прогресс из потока парсера в event loop
    def _threadsafe_cb(found, total):
        if progress_cb:
            loop.call_soon_threadsafe(progress_cb, found, total)

    result = await loop.run_in_executor(
        None, _parse_sync, platform, base_url, p_from, p_to, s_from, s_to, _threadsafe_cb
    )

    # process_parser_json: пишет nickname|title в txt + кладёт в v_base
    txt_path, _added, _count = await loop.run_in_executor(
        None, process_parser_json, result["json_path"]
    )

    rows: List[Tuple[str, str]] = []
    try:
        with open(txt_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                if "|" in line:
                    nick, title = line.split("|", 1)
                    nick, title = nick.strip(), title.strip()
                else:
                    nick, title = line, ""
                if nick:
                    rows.append((nick, title))
    except Exception:
        pass

    return result.get("count", 0), rows


# ------------------------------------------------------------------ VALIDATE

async def run_validation_batch(
    nick_to_title: Dict[str, str],
    stop_event: asyncio.Event,
    progress_cb: Optional[Callable[[int, int, int], None]] = None,
) -> List[Tuple[str, str]]:
    """
    Валидирует ники → возвращает [(email, title), ...] только для валидных.
    progress_cb(done, total, valid_found) — опционально для живого статуса.
    """
    unique_nicks = list(nick_to_title.keys())
    if not unique_nicks:
        return []

    loop = asyncio.get_event_loop()
    total = len(unique_nicks) * len(VALIDATOR_DOMAINS)
    state = {"done": 0, "valid": 0}

    def _cb(current, total_, email, status):
        # вызывается из потоков валидатора
        state["done"] = current
        if status.startswith("✓"):
            state["valid"] += 1
        if progress_cb:
            loop.call_soon_threadsafe(progress_cb, current, total_, state["valid"])

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
    validator.set_progress_callback(_cb)

    valid_emails = await loop.run_in_executor(
        None, validator.validate_nicknames, unique_nicks
    )

    result: List[Tuple[str, str]] = []
    for email in valid_emails:
        nick = email.split("@", 1)[0]
        title = nick_to_title.get(nick, "")
        result.append((email, title))
    return result


# ------------------------------------------------------------------ MAIL

async def run_mailing_batch(
    recipients: List[Tuple[str, str]],
    presets: List[Dict],
    from_name: str,
    stop_event: asyncio.Event,
    on_result: Optional[Callable[[bool, str, str], None]] = None,
    random_sender: bool = True,
) -> Tuple[int, int]:
    """
    Обёртка: сериализует отправку глобальным MAILING_LOCK, чтобы авто-пайплайн и
    обработка присланных файлов не слали одновременно и не пробивали лимит.
    """
    if not recipients:
        return 0, 0
    async with MAILING_LOCK:
        return await _run_mailing_batch(
            recipients, presets, from_name, stop_event, on_result, random_sender
        )


async def _run_mailing_batch(
    recipients: List[Tuple[str, str]],
    presets: List[Dict],
    from_name: str,
    stop_event: asyncio.Event,
    on_result: Optional[Callable[[bool, str, str], None]] = None,
    random_sender: bool = True,
) -> Tuple[int, int]:
    """
    Рассылает по списку [(email, title)]. Раздаёт аккаунты воркерам, respect stop_event,
    rate-лимит 20/3мин на аккаунт. random_sender=True → случайное женское имя (From).
    Возвращает (sent, failed).
    """
    if not recipients:
        return 0, 0

    loop = asyncio.get_event_loop()
    queue: asyncio.Queue = asyncio.Queue()
    for email, title in recipients:
        await queue.put((email, title))

    counters = {"sent": 0, "failed": 0}
    lock = asyncio.Lock()
    preset_idx = {"i": 0}

    # Аккаунты раздаём воркерам заранее: на каждый аккаунт поднимаем
    # MAILING_SESSIONS_PER_ACCOUNT параллельных SMTP-сессий. Именно параллелизм
    # даёт скорость — Gmail принимает одно письмо ~2.1с, как ни оптимизируй.
    accounts = await get_sendable_accounts(MAILING_DAILY_LIMIT)
    if not accounts:
        if on_result:
            for email, _ in recipients:
                on_result(False, email, "нет доступных аккаунтов (суточный лимит?)")
        return 0, len(recipients)

    # Остаток суточного лимита на аккаунт — чтобы не пробить его параллельными сессиями
    remaining = {}
    for acc in accounts:
        used = await get_account_daily_sent(acc["id"])
        remaining[acc["id"]] = max(0, MAILING_DAILY_LIMIT - used) if MAILING_DAILY_LIMIT else 10**9

    async def worker(account, n_workers):
        acc_id = account["id"]
        while not stop_event.is_set():
            batch_start = time.monotonic()
            # Резервируем квоту под пачку, чтобы параллельные сессии не пробили лимит
            async with lock:
                # Пачка адаптивная: нельзя выгребать очередь одним воркером —
                # иначе остальные сессии простаивают и параллелизм теряется.
                fair_share = max(1, queue.qsize() // n_workers)
                allowance = min(MAILING_BURST_SIZE, fair_share, remaining.get(acc_id, 0))
                if allowance <= 0:
                    return
                batch = []
                for _ in range(allowance):
                    try:
                        batch.append(queue.get_nowait())
                    except asyncio.QueueEmpty:
                        break
                if not batch:
                    return
                remaining[acc_id] -= len(batch)

                # Готовим письма: пресеты чередуются, имя отправителя — своё на каждое
                items, meta = [], []
                for email, title in batch:
                    preset = presets[preset_idx["i"] % len(presets)]
                    preset_idx["i"] += 1
                    subject = title if title else preset["subject"]
                    body = preset["body"].replace("OFFER", title) if title else preset["body"]
                    sender = random_female_name() if random_sender else from_name
                    items.append((email, subject, body, sender))
                    meta.append((email, subject, body, preset["id"]))

            proxy_config = None
            if account["host"]:
                proxy_config = {
                    "host": account["host"],
                    "port": account["port"],
                    "username": account["proxy_user"],
                    "password": account["proxy_pass"],
                }
            elif MAILING_PROXIES:
                proxy_config = random.choice(MAILING_PROXIES)
            elif MAILING_PROXY:
                proxy_config = MAILING_PROXY

            results = await loop.run_in_executor(
                None,
                lambda it=items: send_batch_sync(
                    account["email"],
                    account["app_password"],
                    it,
                    proxy_config,
                    30,
                    MAILING_RETRY_DELAY,
                ),
            )

            by_email = {r[0]: (r[1], r[2]) for r in results}
            failed_in_batch = 0
            for email, subject, body, preset_id in meta:
                ok, res = by_email.get(email, (False, "нет результата"))
                if ok:
                    await increment_sent_count(acc_id)
                    await log_outgoing_email(acc_id, email, subject, body, res)
                    async with lock:
                        counters["sent"] += 1
                    if on_result:
                        on_result(True, email, "")
                else:
                    failed_in_batch += 1
                    await log_mailing_error(acc_id, email, preset_id, res)
                    async with lock:
                        counters["failed"] += 1
                    if on_result:
                        on_result(False, email, res)

            # неотправленные возвращаем в квоту
            if failed_in_batch:
                async with lock:
                    remaining[acc_id] += failed_in_batch

            for _ in batch:
                queue.task_done()

            # Rate-лимит: не больше MAILING_RATE_COUNT писем за MAILING_RATE_WINDOW сек.
            # Окно отсчитываем от НАЧАЛА пачки — время самой отправки входит в него,
            # иначе темп получился бы медленнее заданного.
            if MAILING_RATE_COUNT > 0 and MAILING_RATE_WINDOW > 0 and not stop_event.is_set():
                quota_share = len(batch) / MAILING_RATE_COUNT
                wait = quota_share * MAILING_RATE_WINDOW - (time.monotonic() - batch_start)
                if wait > 0:
                    # cooldown в БД — чтобы статистика показывала аккаунт занятым
                    await set_account_cooldown(acc_id, int(wait))
                    try:
                        await asyncio.wait_for(stop_event.wait(), timeout=wait)
                    except asyncio.TimeoutError:
                        pass

    # Раздаём сессии по кругу, а не подряд: иначе при упоре в MAILING_MAX_WORKERS
    # первые аккаунты забрали бы все сессии, а хвост списка простаивал.
    n_workers = min(len(accounts) * MAILING_SESSIONS_PER_ACCOUNT, MAILING_MAX_WORKERS)
    tasks = [
        asyncio.create_task(worker(accounts[i % len(accounts)], n_workers))
        for i in range(n_workers)
    ]

    await asyncio.gather(*tasks, return_exceptions=True)
    return counters["sent"], counters["failed"]


async def dedupe_recipients(recipients: List[Tuple[str, str]]) -> List[Tuple[str, str]]:
    """Убирает получателей, которым уже слали (есть в outgoing_emails)."""
    if not recipients:
        return []
    emails = [e for e, _ in recipients]
    unsent = set(await filter_unsent_emails(emails))
    return [(e, t) for e, t in recipients if e in unsent]
