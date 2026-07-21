import json
import re
import time
import random
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

try:
    from config import (
        MARKTPLAATS_PROXY, PARSER_PROXIES,
        MP_ALL_CATEGORIES, PARSE_ALL_PER_CATEGORY, PARSE_ALL_MAX_TOTAL,
        PARSER_CONCURRENCY,
        MP_L1_CATEGORIES, MP_API_LIMIT, MP_API_PER_CATEGORY, MP_API_MAX_TOTAL, MP_API_MAX_OFFSET,
    )
except Exception:
    MARKTPLAATS_PROXY = None
    PARSER_PROXIES = []
    MP_ALL_CATEGORIES = []
    PARSE_ALL_PER_CATEGORY = 8
    PARSE_ALL_MAX_TOTAL = 120
    PARSER_CONCURRENCY = 8
    MP_L1_CATEGORIES = [322, 31, 820, 504, 621, 1776, 445, 91, 565, 784, 1099, 728, 1847, 395]
    MP_API_LIMIT = 30
    MP_API_PER_CATEGORY = 40
    MP_API_MAX_TOTAL = 400
    MP_API_MAX_OFFSET = 300

BASE_URL = "https://www.marktplaats.nl"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/135.0.0.0 Safari/537.36"
)
REQUEST_TIMEOUT = 30
SEEN_FILENAME = "marktplaats_seen_v2.json"

# Нидерландские сокращения месяцев (+ 'mar' как альт. написание марта)
NL_MONTHS = {
    "jan": 1, "feb": 2, "mrt": 3, "mar": 3, "maart": 3, "apr": 4, "mei": 5,
    "jun": 6, "jul": 7, "aug": 8, "sep": 9, "sept": 9, "okt": 10, "nov": 11, "dec": 12,
}
# Регэксп даты, требующий реального названия месяца (без ложных срабатываний)
MONTH_ALT = "|".join(sorted(NL_MONTHS.keys(), key=len, reverse=True))
DATE_RE = rf"\d{{1,2}}\s+(?:{MONTH_ALT})\.?\s*'?\d{{2,4}}"


@dataclass
class AdItem:
    ad_id: str
    seller_key: str
    seller_nickname: str
    seller_profile_url: Optional[str]
    seller_registered_raw: Optional[str]
    seller_registered_date: Optional[str]
    published_raw: Optional[str]
    published_date: Optional[str]
    title: str
    price: str
    listing_url: str
    photo_url: Optional[str]


class MarktplaatsParserFresh:
    """
    Парсер marktplaats.nl — зеркало KleinanzeigenParserFresh.
    Тот же интерфейс run(), та же схема AdItem/JSON, тот же seen-механизм,
    чтобы downstream (process_parser_json → v_base → валидатор → рассылка)
    работал без изменений.
    """

    def __init__(self, base_url: str, max_items: int = 100, workdir: Optional[Path] = None):
        self.base_url = base_url.strip()
        self.max_items = min(max(max_items, 1), 100)
        self.workdir = Path(workdir or Path(__file__).resolve().parent)
        self.seen_path = self.workdir / SEEN_FILENAME
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept-Language": "nl-NL,nl;q=0.9,en;q=0.8",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        })
        # Прокси: явное переопределение, иначе случайный из пула парсеров
        proxy = MARKTPLAATS_PROXY or (random.choice(PARSER_PROXIES) if PARSER_PROXIES else None)
        if proxy:
            # requests требует socks5h для удалённого DNS
            proxy_url = proxy.replace("socks5://", "socks5h://")
            self.session.proxies.update({"http": proxy_url, "https": proxy_url})
        self.proxy_url = proxy
        self.seen = self._load_seen()
        self.run_seen_ads = set()
        self.run_seen_sellers = set()

    # ---------- seen ----------

    def _load_seen(self) -> dict:
        if self.seen_path.exists():
            try:
                return json.loads(self.seen_path.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {"ad_ids": [], "seller_keys": []}

    def _save_seen(self):
        self.seen_path.write_text(json.dumps(self.seen, ensure_ascii=False, indent=2), encoding="utf-8")

    # ---------- http ----------

    def _get(self, url: str) -> str:
        resp = self.session.get(url, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        return resp.text

    # ---------- search API (быстрый путь: всё из JSON, без загрузки страниц) ----------

    def _api_url(self, l1: int, offset: int, limit: int) -> str:
        return (f"{BASE_URL}/lrp/api/search?l1CategoryId={l1}"
                f"&limit={limit}&offset={offset}"
                f"&sortBy=SORT_INDEX&sortOrder=DECREASING&viewOptions=list-view")

    def _api_get(self, url: str, tries: int = 3):
        """GET JSON API с ретраями и фолбэком на прямое подключение."""
        for i in range(tries):
            last = (i == tries - 1)
            s = self._make_session(direct=last)
            s.headers["Accept"] = "application/json"
            try:
                resp = s.get(url, timeout=REQUEST_TIMEOUT if last else 12)
                if resp.status_code != 200:
                    continue
                return resp.json()
            except Exception:
                continue
            finally:
                try:
                    s.close()
                except Exception:
                    pass
        return None

    def _build_item_from_api(self, l: dict) -> Optional["AdItem"]:
        item_id = l.get("itemId")
        title = (l.get("title") or "").strip()
        if not item_id or not title:
            return None
        seller = ((l.get("sellerInformation") or {}).get("sellerName") or "").strip()

        pi = l.get("priceInfo") or {}
        cents = pi.get("priceCents")
        ptype = pi.get("priceType", "")
        if isinstance(cents, int) and cents > 0:
            price = f"€ {cents/100:.2f}".replace(".", ",")
        elif ptype == "FREE":
            price = "Gratis"
        elif ptype == "RESERVED":
            price = "Gereserveerd"
        else:
            price = ""

        photo = ""
        imgs = l.get("imageUrls") or []
        if imgs:
            photo = imgs[0]
            if photo.startswith("//"):
                photo = "https:" + photo
        elif l.get("pictures"):
            photo = (l["pictures"][0] or {}).get("url", "")

        vip = l.get("vipUrl") or ""
        listing_url = urljoin(BASE_URL, vip) if vip else ""
        date_raw = l.get("date") or ""

        return AdItem(
            ad_id=str(item_id),
            seller_key=(seller or "unknown").strip().lower(),
            seller_nickname=seller,
            seller_profile_url=None,
            seller_registered_raw=None,
            seller_registered_date=None,
            published_raw=date_raw,
            published_date=self._to_iso_date(date_raw),
            title=title,
            price=price,
            listing_url=listing_url,
            photo_url=photo,
        )

    def _make_session(self, direct: bool = False) -> requests.Session:
        """Сессия со своим (случайным) прокси. direct=True → без прокси (фолбэк)."""
        s = requests.Session()
        s.headers.update({
            "User-Agent": USER_AGENT,
            "Accept-Language": "nl-NL,nl;q=0.9,en;q=0.8",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        })
        if not direct:
            proxy = MARKTPLAATS_PROXY or (random.choice(PARSER_PROXIES) if PARSER_PROXIES else None)
            if proxy:
                pu = proxy.replace("socks5://", "socks5h://")
                s.proxies.update({"http": pu, "https": pu})
        return s

    def _get_retry(self, url: str, tries: int = 3) -> Optional[str]:
        """GET с ретраями. Последняя попытка — НАПРЯМУЮ (если прокси все мертвы).
        Прокси-попытки с коротким таймаутом, чтобы зависший прокси не тормозил."""
        for i in range(tries):
            last = (i == tries - 1)
            s = self._make_session(direct=last)
            try:
                resp = s.get(url, timeout=REQUEST_TIMEOUT if last else 12)
                resp.raise_for_status()
                return resp.text
            except Exception:
                continue
            finally:
                try:
                    s.close()
                except Exception:
                    pass
        return None

    def _fetch_and_parse(self, url: str, tries: int = 2) -> Optional["AdItem"]:
        """Загружает и парсит объявление. Последняя попытка — напрямую."""
        for i in range(tries):
            last = (i == tries - 1)
            s = self._make_session(direct=last)
            try:
                resp = s.get(url, timeout=REQUEST_TIMEOUT if last else 12)
                resp.raise_for_status()
                return self._parse_html(url, resp.text)
            except Exception:
                continue
            finally:
                try:
                    s.close()
                except Exception:
                    pass
        return None

    @staticmethod
    def _page_url(base_url: str, page: int) -> str:
        if page <= 1:
            return base_url
        # marktplaats пагинация: ?p=N (или &p=N если уже есть query)
        if re.search(r"[?&]p=\d+", base_url):
            return re.sub(r"([?&]p=)\d+", rf"\g<1>{page}", base_url)
        sep = "&" if "?" in base_url else "?"
        return f"{base_url}{sep}p={page}"

    @staticmethod
    def _extract_ad_id(url: str) -> Optional[str]:
        # /v/.../a1506778836-slug   или   /m/2420496518-slug
        m = re.search(r"/[vm]/(?:[^/]+/)*[am]?(\d{6,})", url)
        if m:
            return m.group(1)
        m = re.search(r"/m/(\d{6,})", url)
        return m.group(1) if m else None

    @staticmethod
    def _clean(text: str) -> str:
        return re.sub(r"\s+", " ", text or "").strip()

    # ---------- даты ----------

    @classmethod
    def _to_iso_date(cls, raw: Optional[str]) -> Optional[str]:
        if not raw:
            return None
        raw = raw.strip().lower()
        today = datetime.now().date()
        if "vandaag" in raw:
            return today.isoformat()
        if "eergisteren" in raw:
            return (today - timedelta(days=2)).isoformat()
        if "gisteren" in raw:
            return (today - timedelta(days=1)).isoformat()

        # "14 okt '24"  / "14 okt 2024" / "14 oktober 2024"
        m = re.search(r"(\d{1,2})\s+([a-z]{3,9})\.?\s*'?(\d{2,4})", raw)
        if m:
            day = int(m.group(1))
            mon_raw = m.group(2)
            month = (
                NL_MONTHS.get(mon_raw)
                or NL_MONTHS.get(mon_raw[:4])
                or NL_MONTHS.get(mon_raw[:3])
            )
            year = int(m.group(3))
            if year < 100:
                year += 2000
            if month:
                try:
                    return datetime(year, month, day).date().isoformat()
                except ValueError:
                    return None

        # ISO
        m = re.search(r"(\d{4}-\d{2}-\d{2})", raw)
        if m:
            return m.group(1)
        # dd-mm-yyyy
        m = re.search(r"(\d{2})-(\d{2})-(\d{4})", raw)
        if m:
            return f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
        return None

    @staticmethod
    def _tenure_to_iso(raw: Optional[str]) -> Optional[str]:
        """
        'X jaar op Marktplaats' / 'X maanden op Marktplaats' → приблизительная
        дата регистрации (ISO). Точной даты marktplaats на VIP не даёт.
        """
        if not raw:
            return None
        low = raw.lower()
        today = datetime.now().date()
        m = re.search(r"(\d+)\s*jaar", low)
        if m:
            years = int(m.group(1))
            return (today - timedelta(days=365 * years)).isoformat()
        m = re.search(r"(\d+)\s*maand", low)
        if m:
            months = int(m.group(1))
            return (today - timedelta(days=30 * months)).isoformat()
        m = re.search(r"(\d+)\s*we(?:ek|ken)", low)
        if m:
            weeks = int(m.group(1))
            return (today - timedelta(weeks=weeks)).isoformat()
        return None

    # ---------- extraction ----------

    def _extract_listing_urls(self, html: str) -> list[str]:
        soup = BeautifulSoup(html, "html.parser")
        out = []
        seen = set()
        for a in soup.find_all("a", href=True):
            href = a.get("href") or ""
            if "/v/" not in href and "/m/" not in href:
                continue
            full = urljoin(BASE_URL, href.split("?")[0])
            ad_id = self._extract_ad_id(full)
            if not ad_id or full in seen:
                continue
            seen.add(full)
            out.append(full)
        return out

    def _extract_lines(self, soup: BeautifulSoup) -> list[str]:
        return [self._clean(x) for x in soup.stripped_strings if self._clean(x)]

    def _extract_title(self, soup: BeautifulSoup, lines: list[str]) -> str:
        h1 = soup.find("h1")
        if h1:
            return self._clean(h1.get_text(" ", strip=True))
        meta = soup.find("meta", attrs={"property": "og:title"})
        if meta and meta.get("content"):
            return self._clean(meta["content"])
        return lines[0] if lines else ""

    def _extract_photo_url(self, soup: BeautifulSoup) -> Optional[str]:
        meta = soup.find("meta", attrs={"property": "og:image"})
        if meta and meta.get("content"):
            return meta["content"].strip()
        return None

    def _extract_price(self, soup: BeautifulSoup, lines: list[str]) -> str:
        meta = soup.find("meta", attrs={"property": "product:price:amount"})
        if meta and meta.get("content"):
            return f"€ {meta['content'].strip()}"
        for line in lines[:80]:
            if "€" in line:
                return line
            if line.lower() in {"gratis", "bieden", "n.o.t.k.", " term.o.t.k."}:
                return line
        return ""

    def _extract_seller(self, soup: BeautifulSoup, lines: list[str], title: str):
        """Возвращает (nickname, seller_key, profile_url)."""
        # 1) JSON-LD seller.name
        for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
            try:
                data = json.loads(script.string or "")
            except Exception:
                continue
            for node in (data if isinstance(data, list) else [data]):
                if not isinstance(node, dict):
                    continue
                seller = node.get("seller") or node.get("offers", {}).get("seller") if isinstance(node.get("offers"), dict) else node.get("seller")
                if isinstance(seller, dict) and seller.get("name"):
                    name = self._clean(seller["name"])
                    if name and name != title:
                        return name, name.lower(), None

        # 2) ссылка на профиль продавца /u/
        for a in soup.find_all("a", href=True):
            href = a.get("href") or ""
            if "/u/" in href:
                name = self._clean(a.get_text(" ", strip=True))
                if name and name != title and len(name) <= 60 and "advertentie" not in name.lower():
                    return name, href.strip(), urljoin(BASE_URL, href)

        # 3) fallback: имя рядом с 'op Marktplaats'
        for i, line in enumerate(lines):
            if "op marktplaats" in line.lower():
                for candidate in reversed(lines[max(0, i - 5):i]):
                    c = candidate.strip()
                    cl = c.lower()
                    if not c or c == title or len(c) > 60:
                        continue
                    if any(x in cl for x in ["marktplaats", "advertentie", "bekijk", "sinds", "reageer", "bod", "€"]):
                        continue
                    return c, cl, None
        return "", "", None

    def _extract_published_raw(self, lines: list[str]) -> Optional[str]:
        for i, line in enumerate(lines[:150]):
            low = line.lower()
            if low.startswith(("vandaag", "gisteren", "eergisteren")):
                return line
            if low.startswith("sinds"):
                # дата может быть в этой же или в следующих 1-2 строках
                combined = " ".join(lines[i:i + 3])
                cl = combined.lower()
                if re.search(DATE_RE, cl) or "vandaag" in cl or "gisteren" in cl:
                    return combined
                return line
            if re.search(DATE_RE, low):
                return line
        return None

    def _extract_registered_raw(self, lines: list[str]) -> Optional[str]:
        for line in lines:
            if "op marktplaats" in line.lower() and re.search(r"\d", line):
                return line
            if re.search(r"lid sinds", line, flags=re.I):
                return line
        return None

    def _parse_item(self, url: str) -> Optional[AdItem]:
        return self._parse_html(url, self._get(url))

    def _parse_html(self, url: str, html: str) -> Optional[AdItem]:
        soup = BeautifulSoup(html, "html.parser")
        lines = self._extract_lines(soup)
        title = self._extract_title(soup, lines)
        ad_id = self._extract_ad_id(url)
        if not ad_id or not title:
            return None

        seller_registered_raw = self._extract_registered_raw(lines)
        seller_registered_date = self._tenure_to_iso(seller_registered_raw) or self._to_iso_date(seller_registered_raw)
        seller_nickname, seller_key, seller_profile_url = self._extract_seller(soup, lines, title)
        seller_key = seller_key or (seller_nickname or "unknown").strip().lower()
        published_raw = self._extract_published_raw(lines)

        return AdItem(
            ad_id=ad_id,
            seller_key=seller_key,
            seller_nickname=seller_nickname,
            seller_profile_url=seller_profile_url,
            seller_registered_raw=seller_registered_raw,
            seller_registered_date=seller_registered_date,
            published_raw=published_raw,
            published_date=self._to_iso_date(published_raw),
            title=title,
            price=self._extract_price(soup, lines),
            listing_url=url,
            photo_url=self._extract_photo_url(soup),
        )

    def _seed_urls(self) -> tuple[list, bool]:
        """Возвращает (список стартовых URL, режим_все_категории)."""
        url = (self.base_url or "").strip().lower()
        if not url or url in ("all", "все", "all_categories", "*"):
            return list(MP_ALL_CATEGORIES), True
        return [self.base_url], False

    def run(
        self,
        published_from: Optional[str] = None,
        published_to: Optional[str] = None,
        seller_registered_from: Optional[str] = None,
        seller_registered_to: Optional[str] = None,
        progress_callback: Optional[Callable[[int, int], None]] = None,
    ) -> dict:
        """
        Быстрый парсинг через внутренний search API marktplaats.
        Фильтры по датам НЕ применяются (не нужны для этой площадки).
        Идёт по l1-категориям, сортировка по свежести, пагинация по offset —
        большой объём новых лидов за цикл, без загрузки страниц объявлений.
        """
        results: list[AdItem] = []
        seen_ads = set(self.seen.get("ad_ids", []))
        seen_sellers = set(self.seen.get("seller_keys", []))

        if progress_callback:
            progress_callback(0, MP_API_MAX_TOTAL)

        for l1 in MP_L1_CATEGORIES:
            if len(results) >= MP_API_MAX_TOTAL:
                break
            cat_count = 0
            offset = 0
            while cat_count < MP_API_PER_CATEGORY and offset < MP_API_MAX_OFFSET and len(results) < MP_API_MAX_TOTAL:
                data = self._api_get(self._api_url(l1, offset, MP_API_LIMIT))
                if not data:
                    break
                listings = data.get("listings") or []
                if not listings:
                    break
                for l in listings:
                    item = self._build_item_from_api(l)
                    if not item:
                        continue
                    if item.ad_id in seen_ads or item.ad_id in self.run_seen_ads:
                        continue
                    if item.seller_key in seen_sellers or item.seller_key in self.run_seen_sellers:
                        continue
                    results.append(item)
                    self.run_seen_ads.add(item.ad_id)
                    self.run_seen_sellers.add(item.seller_key)
                    cat_count += 1
                    if progress_callback:
                        progress_callback(len(results), MP_API_MAX_TOTAL)
                    if cat_count >= MP_API_PER_CATEGORY or len(results) >= MP_API_MAX_TOTAL:
                        break
                offset += MP_API_LIMIT
                time.sleep(0.05)

        self.seen["ad_ids"] = sorted(seen_ads | self.run_seen_ads)
        self.seen["seller_keys"] = sorted(seen_sellers | self.run_seen_sellers)
        self._save_seen()

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_path = self.workdir / f"marktplaats_parse_v2_{ts}.json"
        payload = {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "count": len(results),
            "items": [asdict(x) for x in results],
        }
        out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"count": len(results), "json_path": str(out_path), "seen_path": str(self.seen_path)}
