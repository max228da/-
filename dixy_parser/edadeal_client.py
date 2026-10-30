# -*- coding: utf-8 -*-
"""HTTP-клиент Edadeal: куки Яндекса + прокси + ретраи + авто-формат.

Клиент пытается работать с двумя форматами ответа:
  1. JSON (новый API v3) — предпочтительный, парсится напрямую;
  2. Protobuf (legacy /web/search/offers) — fallback, декодируется
     через google.protobuf (если установлен) либо через protobuf-decoder
     "сырое" представление, из которого достаём нужные поля по номерам.
"""
from __future__ import annotations

import http.cookiejar
import json
import logging
import random
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Iterator

import requests

from . import config as cfg

log = logging.getLogger("dixy_parser.edadeal")


@dataclass
class RawOffer:
    """Нормализованная карточка оффера, независимо от формата ответа."""
    offer_id: str
    name: str
    description: str = ""
    price_old: float | None = None
    price_new: float | None = None
    discount_percent: float | None = None
    currency: str = "RUB"
    image_url: str = ""
    start_date: str = ""
    end_date: str = ""
    retailer_slug: str = ""
    segment_slug: str = ""
    segment_name: str = ""
    shop_ids: list[str] = field(default_factory=list)
    quantity_value: float | None = None
    quantity_unit: str = ""
    url: str = ""
    source_format: str = ""   # "json" | "protobuf" — для диагностики

    def computed_discount(self) -> float | None:
        """% скидки: берём явно указанный, иначе считаем из цен."""
        if self.discount_percent and self.discount_percent > 0:
            return float(self.discount_percent)
        if self.price_old and self.price_new and self.price_old > 0:
            raw = (1.0 - self.price_new / self.price_old) * 100.0
            return round(raw, 2) if raw > 0 else None
        return None


class EdadealClient:
    def __init__(
        self,
        locality: str = cfg.LOCALITY,
        retailer_slugs: list[str] | None = None,
        proxy: str | None = cfg.PROXY,
        cookies_file: str | None = None,
        cookies_inline: dict | None = None,
        user_agent: str = cfg.USER_AGENT,
    ):
        self.locality = locality
        self.retailer_slugs = retailer_slugs or cfg.RETAILER_SLUGS
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": user_agent,
            "Accept": "application/json, application/x-protobuf, */*",
            "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.3",
            "Referer": f"https://edadeal.ru/{locality}/",
            "Origin": "https://edadeal.ru",
        })
        if proxy:
            self.session.proxies = {"http": proxy, "https": proxy}

        self._load_cookies(
            cookies_file if cookies_file is not None else cfg.COOKIES_FILE,
            cookies_inline if cookies_inline is not None else cfg.COOKIES_INLINE,
        )

        # Будет установлено после первого успешного запроса:
        #   "json" — API v3 жив, работаем с ним
        #   "protobuf" — только legacy /web/search/offers
        self._api_mode: str | None = None
        self._working_retailer: str | None = None

    # ---------- cookies ----------

    def _load_cookies(self, cookies_file, cookies_inline):
        loaded = 0
        if cookies_file:
            try:
                jar = http.cookiejar.MozillaCookieJar(str(cookies_file))
                jar.load(ignore_discard=True, ignore_expires=True)
                for c in jar:
                    self.session.cookies.set_cookie(c)
                    loaded += 1
                log.info("Loaded %d cookies from %s", loaded, cookies_file)
            except FileNotFoundError:
                log.warning("Cookies file not found: %s", cookies_file)
            except Exception as e:
                log.warning("Failed to load cookies file: %s", e)
        if cookies_inline:
            for k, v in cookies_inline.items():
                if not v:
                    continue
                # По умолчанию ставим на .edadeal.ru; для yandex-куки
                # придётся передавать через cookies-файл с правильным доменом.
                self.session.cookies.set(k, v, domain=".edadeal.ru")
                loaded += 1
        if loaded == 0:
            log.warning(
                "No cookies loaded. Edadeal requires Yandex SSO — "
                "requests will be redirected to passport.yandex.ru."
            )

    # ---------- низкоуровневый запрос с ретраями ----------

    def _request(self, method: str, url: str, **kwargs) -> requests.Response:
        kwargs.setdefault("timeout", cfg.REQUEST_TIMEOUT)
        last_exc: Exception | None = None
        for attempt in range(1, cfg.MAX_RETRIES + 1):
            try:
                resp = self.session.request(method, url, **kwargs)
                # 307 → SSO: куки протухли или их не было вообще
                if resp.status_code in (301, 302, 307, 308):
                    loc = resp.headers.get("Location", "")
                    if "passport.yandex" in loc or "sso." in loc:
                        raise AuthRequired(
                            f"Redirect to SSO ({loc[:120]}). "
                            "Yandex cookies missing or expired."
                        )
                if resp.status_code == 429:
                    sleep = min(2 ** attempt, 30)
                    log.warning("429 Too Many Requests — sleeping %ds", sleep)
                    time.sleep(sleep)
                    continue
                if resp.status_code >= 500 and attempt < cfg.MAX_RETRIES:
                    time.sleep(2 ** attempt)
                    continue
                resp.raise_for_status()
                return resp
            except AuthRequired:
                raise
            except (requests.RequestException,) as e:
                last_exc = e
                log.warning("Attempt %d failed: %s", attempt, e)
                time.sleep(1.5 * attempt)
        raise RuntimeError(f"All retries failed for {url}: {last_exc}")

    def _throttle(self):
        lo, hi = cfg.REQUEST_DELAY_SEC
        time.sleep(random.uniform(lo, hi))

    # ---------- высокоуровневые методы ----------

    def iter_offers(self, retailer_slug: str) -> Iterator[RawOffer]:
        """Итерация по всем акциям одного ритейлера для self.locality."""
        for page in range(1, cfg.MAX_PAGES_PER_SEGMENT + 1):
            batch, has_more = self._fetch_page(retailer_slug, page)
            if not batch:
                break
            for o in batch:
                yield o
            if not has_more:
                break
            self._throttle()

    def _fetch_page(self, retailer_slug: str, page: int) -> tuple[list[RawOffer], bool]:
        """Пробует JSON v3 → protobuf legacy. Запоминает рабочий режим."""
        # 1) JSON v3
        if self._api_mode in (None, "json"):
            try:
                offers, more = self._fetch_json(retailer_slug, page)
                self._api_mode = "json"
                return offers, more
            except (JsonModeNotAvailable, ValueError) as e:
                if self._api_mode == "json":
                    raise  # уже прибились — не падаем в proto
                log.info("JSON API v3 not available (%s), trying protobuf…", e)

        # 2) Protobuf legacy
        offers, more = self._fetch_protobuf(retailer_slug, page)
        self._api_mode = "protobuf"
        return offers, more

    def _fetch_json(self, retailer_slug: str, page: int) -> tuple[list[RawOffer], bool]:
        """Новый JSON API. Точный маршрут уточняется после первого ответа."""
        candidates = [
            f"{cfg.API_V3_BASE}/retailers/{retailer_slug}/offers",
            f"{cfg.API_V3_BASE}/offers",
        ]
        params = {
            "locality": self.locality,
            "retailer": retailer_slug,
            "page": page,
            "count": cfg.PAGE_SIZE,
        }
        headers = {"Accept": "application/json"}
        last_err: Exception | None = None
        for url in candidates:
            try:
                r = self._request("GET", url, params=params, headers=headers)
                ct = r.headers.get("Content-Type", "")
                if "json" not in ct:
                    last_err = JsonModeNotAvailable(
                        f"{url} returned {ct!r}, not JSON"
                    )
                    continue
                data = r.json()
                offers = _parse_json_offers(data, retailer_slug)
                has_more = _json_has_more(data, page)
                log.info(
                    "JSON page %d from %s: %d offers (more=%s)",
                    page, url.rsplit("/", 2)[-2:], len(offers), has_more,
                )
                return offers, has_more
            except AuthRequired:
                raise
            except (requests.HTTPError, ValueError, JsonModeNotAvailable) as e:
                last_err = e
                continue
        raise JsonModeNotAvailable(str(last_err) if last_err else "no JSON endpoint")

    def _fetch_protobuf(self, retailer_slug: str, page: int) -> tuple[list[RawOffer], bool]:
        """Legacy /web/search/offers → protobuf."""
        params = {
            "count": cfg.PAGE_SIZE,
            "locality": self.locality,
            "page": page,
            "retailer": retailer_slug,
        }
        headers = {"Accept": "application/x-protobuf"}
        r = self._request("GET", cfg.SEARCH_URL_LEGACY, params=params, headers=headers)
        from .proto_decoder import decode_offers_response
        resp = decode_offers_response(r.content)
        offers = [_proto_to_raw(x, retailer_slug) for x in resp.get("offers", [])]
        total = resp.get("total", 0)
        has_more = page * cfg.PAGE_SIZE < total
        log.info("Proto page %d: %d offers (total=%s)", page, len(offers), total)
        return offers, has_more

    # ---------- публичный API ----------

    def iter_all(self) -> Iterator[RawOffer]:
        """Пройти все слуги ритейлера, пока какой-то не отработает."""
        for slug in self.retailer_slugs:
            try:
                log.info("Trying retailer slug: %s", slug)
                any_yielded = False
                for off in self.iter_offers(slug):
                    any_yielded = True
                    self._working_retailer = slug
                    yield off
                if any_yielded:
                    return  # нашли рабочий слуг, другие не трогаем
            except AuthRequired:
                raise
            except Exception as e:
                log.warning("Slug %r failed: %s", slug, e)
                continue
        if not self._working_retailer:
            log.error("None of retailer slugs returned data: %s", self.retailer_slugs)


# ---------- исключения ----------

class AuthRequired(Exception):
    """Edadeal редиректит на SSO — нужны валидные Yandex-куки."""

class JsonModeNotAvailable(Exception):
    """JSON API v3 недоступен — переключаемся на protobuf."""


# ---------- JSON helpers ----------

def _parse_json_offers(data: Any, retailer_slug: str) -> list[RawOffer]:
    """Пытается достать офферы из JSON нескольких известных форматов."""
    if not isinstance(data, dict):
        return []
    # Возможные поля-контейнеры массива офферов:
    for key in ("offers", "items", "data", "results"):
        arr = data.get(key)
        if isinstance(arr, list):
            return [_json_to_raw(x, retailer_slug) for x in arr if isinstance(x, dict)]
    # Иногда это сразу список:
    if isinstance(data.get("payload"), list):
        return [_json_to_raw(x, retailer_slug) for x in data["payload"] if isinstance(x, dict)]
    return []


def _json_has_more(data: Any, current_page: int) -> bool:
    if not isinstance(data, dict):
        return False
    page_info = data.get("page") or data.get("pagination") or data.get("meta") or {}
    if isinstance(page_info, dict):
        total_pages = page_info.get("total_pages") or page_info.get("totalPages")
        if isinstance(total_pages, int):
            return current_page < total_pages
        has_next = page_info.get("has_next") or page_info.get("hasNext")
        if isinstance(has_next, bool):
            return has_next
    total = data.get("total") or data.get("count")
    if isinstance(total, int):
        return current_page * cfg.PAGE_SIZE < total
    # Безопасный fallback — одна страница.
    return False


def _json_to_raw(x: dict, retailer_slug: str) -> RawOffer:
    def _num(v):
        if isinstance(v, (int, float)):
            return float(v)
        if isinstance(v, dict):
            for k in ("value", "amount", "price"):
                if k in v:
                    return _num(v[k])
        if isinstance(v, str):
            try:
                return float(v.replace(",", ".").replace(" ", ""))
            except ValueError:
                return None
        return None

    price_old = _num(x.get("priceOld") or x.get("price_old") or x.get("oldPrice") or x.get("priceBefore"))
    price_new = _num(x.get("priceNew") or x.get("price_new") or x.get("newPrice") or x.get("price"))
    discount = _num(x.get("discount") or x.get("discountPercent") or x.get("discount_percent"))

    seg = x.get("segment") or {}
    if isinstance(seg, str):
        segment_slug, segment_name = seg, ""
    else:
        segment_slug = seg.get("slug") or seg.get("code") or ""
        segment_name = seg.get("name") or ""

    qty = x.get("quantity") or {}
    if isinstance(qty, dict):
        q_val = _num(qty.get("value"))
        q_unit = qty.get("unit") or ""
    else:
        q_val, q_unit = None, ""

    return RawOffer(
        offer_id=str(x.get("id") or x.get("uuid") or x.get("offerId") or ""),
        name=str(x.get("name") or x.get("title") or ""),
        description=str(x.get("description") or ""),
        price_old=price_old,
        price_new=price_new,
        discount_percent=discount,
        currency="RUB",
        image_url=str(x.get("image") or x.get("imageUrl") or x.get("image_url") or ""),
        start_date=str(x.get("startDate") or x.get("start_date") or x.get("dateStart") or ""),
        end_date=str(x.get("endDate") or x.get("end_date") or x.get("dateEnd") or ""),
        retailer_slug=retailer_slug,
        segment_slug=segment_slug,
        segment_name=segment_name,
        shop_ids=[str(s) for s in (x.get("shopIds") or x.get("shop_ids") or [])],
        quantity_value=q_val,
        quantity_unit=q_unit,
        url=str(x.get("url") or x.get("link") or ""),
        source_format="json",
    )


def _proto_to_raw(x: dict, retailer_slug: str) -> RawOffer:
    po = x.get("price_old") or {}
    pn = x.get("price_new") or {}
    qty = x.get("quantity") or {}
    return RawOffer(
        offer_id=str(x.get("id", "")),
        name=str(x.get("name", "")),
        description=str(x.get("description", "")),
        price_old=po.get("value") if isinstance(po, dict) else None,
        price_new=pn.get("value") if isinstance(pn, dict) else None,
        discount_percent=x.get("discount"),
        currency=(po.get("currency") if isinstance(po, dict) else "") or "RUB",
        image_url=str(x.get("image_url", "")),
        start_date=str(x.get("start_date", "")),
        end_date=str(x.get("end_date", "")),
        retailer_slug=retailer_slug or str(x.get("retailer_slug", "")),
        segment_slug=str(x.get("segment", "")),
        segment_name=str(x.get("segment_name", "")),
        shop_ids=[str(s) for s in x.get("shop_ids", [])],
        quantity_value=qty.get("value") if isinstance(qty, dict) else None,
        quantity_unit=(qty.get("unit") if isinstance(qty, dict) else "") or "",
        url=str(x.get("url", "")),
        source_format="protobuf",
    )
