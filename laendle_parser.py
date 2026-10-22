"""
Парсер laendleanzeiger.at (региональная барахолка, Форарльберг, AT).

Отдаёт новые объявления по заданным «наблюдениям» (watch) — категория или
поисковый запрос. Разметка сайта серверная, антибота на листинге нет, поэтому
берём обычным requests + BeautifulSoup (как kleinanzeigen_parser_fresh).

ВАЖНО по этике/ToS:
- телефон продавца сайт отдаёт КАРТИНКОЙ (/DetailAd/PhoneNumberImages) специально
  против парсинга — мы его НЕ достаём и НЕ расшифровываем. Детектим только факт
  «есть верифицированный номер» как сигнал приоритета (продавец доступен напрямую).
- email-ов на сайте нет вообще, контакт — через залогиненный мессенджер.
  Отправку сообщений НЕ автоматизируем: софт доводит до готового черновика и
  ссылки, пишет пользователь сам со своего аккаунта.
"""
import json
import random
import re
import time
from dataclasses import dataclass, field, asdict
from typing import Callable, List, Optional
from urllib.parse import quote_plus, urljoin

import requests
from bs4 import BeautifulSoup

try:
    from config import PARSER_PROXIES
except Exception:
    PARSER_PROXIES = []

BASE_URL = "https://www.laendleanzeiger.at"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/135.0.0.0 Safari/537.36"
)
REQUEST_TIMEOUT = 30
# Пауза между запросами страниц — по-человечески, не долбить сайт.
PAGE_DELAY_SEC = 1.5


@dataclass
class LaendleItem:
    ad_id: str                       # hex-id из URL (он же атрибут location у карточки)
    title: str
    listing_url: str
    price_display: str               # как показать: "750 EUR", "Zu verschenken", "—"
    price_value: Optional[float]     # число в EUR, если удалось распарсить
    price_old: Optional[str]
    is_free: bool
    location: str                    # "Eichenberg, Vorarlberg"
    date_raw: str                    # "heute 04:05" / "gestern 07:41" / "16 Oktober"
    photo_url: Optional[str]
    image_count: int
    is_premium: bool                 # платное/топовое размещение
    has_validated_phone: bool        # у продавца верифицированный номер (сигнал приоритета)
    description: str                 # короткое описание из карточки
    category: str                    # метка наблюдения, по которому нашли
    # --- обогащение со страницы объявления (по требованию) ---
    seller_name: Optional[str] = None
    seller_profile_url: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


_PRICE_NUM_RE = re.compile(r"[\d\s.,]+")


def _parse_price(text: str):
    """'750.0 EUR' / '1 150 EUR' / 'Zu verschenken' -> (display, value, is_free)."""
    t = (text or "").strip()
    if not t:
        return "—", None, False
    low = t.lower()
    if "verschenk" in low or low in ("0", "0.0", "0,0", "0 eur", "0.0 eur"):
        return "Zu verschenken", 0.0, True
    m = _PRICE_NUM_RE.search(t)
    value = None
    if m:
        # Сайт рендерит цены в US-формате: "." — десятичный разделитель ("750.0 EUR",
        # в JSON-LD "750.00"), тысячи — пробел ("1 150 EUR"). Запятую, если вдруг
        # встретится, трактуем как десятичную (нем. формат).
        raw = m.group(0).strip().replace(" ", "")
        if "," in raw and "." in raw:
            raw = raw.replace(",", "")        # "," — тысячи, "." — дробная
        elif "," in raw:
            raw = raw.replace(",", ".")       # "," — дробная
        try:
            value = float(raw)
        except ValueError:
            value = None
    if value is not None:
        disp = f"{int(value)} EUR" if value == int(value) else f"{value:.2f} EUR"
        return disp, value, value == 0.0
    return t, None, False


class LaendleParser:
    def __init__(self, max_pages: int = 2, use_proxy: bool = False):
        self.max_pages = max(1, min(max_pages, 20))
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept-Language": "de-AT,de;q=0.9,en;q=0.8",
        })
        proxy = (random.choice(PARSER_PROXIES) if (use_proxy and PARSER_PROXIES) else None)
        if proxy:
            purl = proxy.replace("socks5://", "socks5h://")
            self.session.proxies.update({"http": purl, "https": purl})

    # ---------- построение URL ----------
    def build_watch_url(self, watch_value: str, pag: int = 1,
                        kind: Optional[str] = None) -> str:
        """
        kind: "category" | "search" | "url" | None (эвристика).
        watch_value:
          - "url"      — полный URL берём как есть;
          - "category" — путь категории ("elektronik", "elektronik/computer")
                         или готовый "/anzeigen/...";
          - "search"   — поисковый запрос (?q=...).
        None — определяем сами: http→url, /anzeigen или наличие "/"→категория,
        одиночное слово/фраза→поиск.
        Всегда добавляем сортировку по свежим (sort=date) и страницу (pag).
        """
        wv = (watch_value or "").strip()
        if kind == "url" or (kind is None and wv.startswith("http")):
            base = wv
        elif kind == "search":
            base = f"{BASE_URL}/anzeigen/?q={quote_plus(wv)}"
        elif kind == "category":
            if wv.startswith("/anzeigen"):
                base = urljoin(BASE_URL, wv)
            else:
                base = f"{BASE_URL}/anzeigen/{wv.strip('/')}/"
        elif wv.startswith("/anzeigen") or "/" in wv:
            # эвристика: путь категории
            base = urljoin(BASE_URL, wv) if wv.startswith("/anzeigen") \
                else f"{BASE_URL}/anzeigen/{wv.strip('/')}/"
        else:
            # эвристика по умолчанию: поисковый запрос
            base = f"{BASE_URL}/anzeigen/?q={quote_plus(wv)}"
        sep = "&" if "?" in base else "?"
        parts = [base]
        parts_qs = []
        if "sort=" not in base:
            parts_qs.append("sort=date")
        if pag and pag > 1:
            parts_qs.append(f"pag={pag}")
        if parts_qs:
            return base + sep + "&".join(parts_qs)
        return base

    # ---------- сеть ----------
    def _get(self, url: str) -> Optional[str]:
        try:
            r = self.session.get(url, timeout=REQUEST_TIMEOUT)
            if r.status_code == 200:
                return r.text
        except requests.RequestException:
            return None
        return None

    # ---------- разбор карточек ----------
    def parse_cards(self, html: str, category_label: str) -> List[LaendleItem]:
        soup = BeautifulSoup(html, "html.parser")
        items: List[LaendleItem] = []
        for card in soup.select("div.article-item"):
            ad_id = (card.get("location") or card.get("data-articleid") or "").strip()
            title_a = card.select_one("h2.article-title a[href]")
            if not title_a:
                continue
            url = title_a.get("href", "").strip()
            if not url.startswith("http"):
                url = urljoin(BASE_URL, url)
            if not ad_id:
                m = re.search(r"/([0-9a-f]{20,})\.html", url)
                ad_id = m.group(1) if m else url
            title = title_a.get_text(" ", strip=True)

            price_el = card.select_one(".article-price .new-price") or card.select_one(".article-price")
            price_display, price_value, is_free = _parse_price(
                price_el.get_text(" ", strip=True) if price_el else ""
            )
            old_el = card.select_one(".article-price .old-price")
            price_old = old_el.get_text(" ", strip=True) if old_el else None

            loc_el = card.select_one("p.article-location span") or card.select_one("p.article-location")
            location = loc_el.get_text(" ", strip=True) if loc_el else ""
            date_el = card.select_one("p.article-date")
            date_raw = date_el.get_text(" ", strip=True) if date_el else ""
            desc_el = card.select_one("p.article-description")
            description = desc_el.get_text(" ", strip=True) if desc_el else ""

            img = card.select_one("div.art-img img[src]")
            photo_url = img.get("src") if img else None
            if photo_url and "no_img" in photo_url:
                photo_url = None
            cnt_el = card.select_one(".article-img-count-number")
            try:
                image_count = int(cnt_el.get_text(strip=True)) if cnt_el else 0
            except ValueError:
                image_count = 0

            is_premium = bool(card.select_one(".art-promoted"))
            has_phone = bool(card.select_one(".article-lbl-validated-phone"))

            items.append(LaendleItem(
                ad_id=ad_id, title=title, listing_url=url,
                price_display=price_display, price_value=price_value,
                price_old=price_old, is_free=is_free,
                location=location, date_raw=date_raw,
                photo_url=photo_url, image_count=image_count,
                is_premium=is_premium, has_validated_phone=has_phone,
                description=description, category=category_label,
            ))
        return items

    # ---------- обогащение со страницы объявления ----------
    def enrich(self, item: LaendleItem) -> LaendleItem:
        """Догружает продавца и профиль со страницы объявления. Тихо игнорит ошибки."""
        html = self._get(item.listing_url)
        if not html:
            return item
        soup = BeautifulSoup(html, "html.parser")
        name = soup.select_one("h2.user-profile-name")
        if name:
            item.seller_name = name.get_text(" ", strip=True)
        prof = soup.select_one("a.btn-profile-page[href]")
        if prof:
            href = prof.get("href", "")
            item.seller_profile_url = href if href.startswith("http") else urljoin(BASE_URL, href)
        if not item.has_validated_phone:
            item.has_validated_phone = bool(soup.select_one(".valid-phone"))
        return item

    # ---------- основной проход ----------
    def scan_watch(self, watch_value: str, category_label: str,
                   max_items: int = 60, kind: Optional[str] = None) -> List[LaendleItem]:
        """Обходит страницы одного наблюдения, возвращает объявления (с дедупом по ad_id)."""
        found: List[LaendleItem] = []
        seen_ids = set()
        for pag in range(1, self.max_pages + 1):
            url = self.build_watch_url(watch_value, pag, kind=kind)
            html = self._get(url)
            if not html:
                break
            cards = self.parse_cards(html, category_label)
            if not cards:
                break
            for it in cards:
                if it.ad_id in seen_ids:
                    continue
                seen_ids.add(it.ad_id)
                found.append(it)
                if len(found) >= max_items:
                    return found
            time.sleep(PAGE_DELAY_SEC)
        return found

    def scan(self, watches: List[dict], max_items_per_watch: int = 60,
             progress: Optional[Callable[[int], None]] = None) -> List[LaendleItem]:
        """
        watches: [{"value": "elektronik", "label": "Электроника", "kind": "category"}, ...]
        Возвращает все объявления по всем наблюдениям (дедуп по ad_id между ними).
        Фильтрацию «новое/виденное» делает слой БД.
        """
        all_items: List[LaendleItem] = []
        seen = set()
        for w in watches:
            items = self.scan_watch(w["value"], w.get("label") or w["value"],
                                    max_items=max_items_per_watch, kind=w.get("kind"))
            for it in items:
                if it.ad_id in seen:
                    continue
                seen.add(it.ad_id)
                all_items.append(it)
            if progress:
                progress(len(all_items))
        return all_items


if __name__ == "__main__":
    # быстрый ручной тест
    import sys
    p = LaendleParser(max_pages=1)
    q = sys.argv[1] if len(sys.argv) > 1 else "elektronik"
    res = p.scan_watch(q, q, max_items=10)
    print(f"watch={q!r} -> {len(res)} объявлений")
    for it in res[:10]:
        print(f"  [{'★' if it.is_premium else ' '}]"
              f"[{'📞' if it.has_validated_phone else '  '}] "
              f"{it.price_display:>14} | {it.title[:60]} | {it.location} | {it.date_raw}")
        print(f"       {it.listing_url}")
