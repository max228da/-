# -*- coding: utf-8 -*-
"""Конфиг парсера Дикси через Edadeal."""
import os
from pathlib import Path

# ---------- Базовое ----------
BASE_URL = "https://edadeal.ru"
# Исторический search endpoint (возвращает protobuf). Если 404 — используем
# новый JSON endpoint через API v3 (попробуется автоматически).
SEARCH_URL_LEGACY = "https://squark.edadeal.ru/web/search/offers"
# Веб-API v3 (JSON). Точный путь уточняется парсером при первом запросе.
API_V3_BASE = "https://edadeal.ru/api/v3"

# Город в URL-слуге Edadeal (не ID).
LOCALITY = "moskva"
# Слуг ритейлера. Для Дикси в разные годы встречались "dixy" и "diksi" —
# парсер попробует оба.
RETAILER_SLUGS = ["dixy", "diksi"]

# ---------- Фильтры ----------
MIN_DISCOUNT_PERCENT = 70.0   # ≥ 70% — то что нужно пользователю
# Если ритейлер не указывает % явно, считаем из old_price / new_price:
#   discount = (1 - new/old) * 100
# и округляем вниз.

# ---------- Пагинация / Rate limit ----------
PAGE_SIZE = 60                # максимум, который Edadeal отдаёт за запрос
MAX_PAGES_PER_SEGMENT = 50    # защита от бесконечного цикла
REQUEST_DELAY_SEC = (0.4, 1.2)  # случайная пауза между запросами
REQUEST_TIMEOUT = 25
MAX_RETRIES = 4

# ---------- Прокси ----------
# ОБЯЗАТЕЛЬНО российский IP: edadeal.ru геоблокирован вне РФ, плюс Яндекс
# заподозрит сессию при входе с иностранного адреса и выкинет капчу.
# Форматы: "http://user:pass@host:port", "socks5://user:pass@host:port".
# Можно переопределить переменной окружения EDADEAL_PROXY.
PROXY = os.environ.get("EDADEAL_PROXY") or None

# ---------- Куки Яндекса ----------
# Куки авторизованного пользователя Edadeal (= Yandex ID).
# Снимаются из DevTools браузера после логина на edadeal.ru.
# Минимально нужны: Session_id, yandexuid, i, sessionid2 (всё с домена .yandex.ru)
#                  + Session, Session_id, _ym_uid (с .edadeal.ru).
# Файл формата Netscape cookies.txt — можно экспортировать расширениями
# "Get cookies.txt LOCALLY" (Chrome) или "cookies.txt" (Firefox).
COOKIES_FILE = Path(__file__).resolve().parent / "yandex_cookies.txt"

# Либо напрямую словарь (пути два — используется тот, где значения):
COOKIES_INLINE = {
    # "Session_id": "3:...",
    # "yandexuid":  "...",
    # "i":          "...",
    # "sessionid2": "...",
}

# ---------- User-Agent ----------
# Должен совпадать с UA браузера, из которого сняли куки — Яндекс
# сопоставляет fingerprint; расхождение UA + IP + Session_id = разлогин.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

# ---------- Выход ----------
OUTPUT_DIR = Path(__file__).resolve().parent / "out"
OUTPUT_DIR.mkdir(exist_ok=True)
