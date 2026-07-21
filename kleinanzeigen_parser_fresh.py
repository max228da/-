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
        KLEINANZEIGEN_PROXY, PARSER_PROXIES,
        KA_ALL_CATEGORIES, PARSE_ALL_PER_CATEGORY, PARSE_ALL_MAX_TOTAL,
        PARSER_CONCURRENCY,
    )
except Exception:
    KLEINANZEIGEN_PROXY = None
    PARSER_PROXIES = []
    KA_ALL_CATEGORIES = []
    PARSE_ALL_PER_CATEGORY = 8
    PARSE_ALL_MAX_TOTAL = 120
    PARSER_CONCURRENCY = 8

BASE_URL = "https://www.kleinanzeigen.de"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/135.0.0.0 Safari/537.36"
)
REQUEST_TIMEOUT = 30
SEEN_FILENAME = "kleinanzeigen_seen_v2.json"


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


class KleinanzeigenParserFresh:
    def __init__(self, base_url: str, max_items: int = 100, workdir: Optional[Path] = None):
        self.base_url = base_url.strip()
        self.max_items = min(max(max_items, 1), 100)
        self.workdir = Path(workdir or Path(__file__).resolve().parent)
        self.seen_path = self.workdir / SEEN_FILENAME
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "de-DE,de;q=0.9,en;q=0.8"})
        # Прокси: явное переопределение, иначе случайный из пула парсеров
        proxy = KLEINANZEIGEN_PROXY or (random.choice(PARSER_PROXIES) if PARSER_PROXIES else None)
        if proxy:
            proxy_url = proxy.replace("socks5://", "socks5h://")
            self.session.proxies.update({"http": proxy_url, "https": proxy_url})
        self.proxy_url = proxy
        self.seen = self._load_seen()
        self.run_seen_ads = set()
        self.run_seen_sellers = set()

    def _load_seen(self) -> dict:
        if self.seen_path.exists():
            try:
                return json.loads(self.seen_path.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {"ad_ids": [], "seller_keys": []}

    def _save_seen(self):
        self.seen_path.write_text(json.dumps(self.seen, ensure_ascii=False, indent=2), encoding="utf-8")

    def _get(self, url: str) -> str:
        resp = self.session.get(url, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        return resp.text

    def _make_session(self, direct: bool = False) -> requests.Session:
        """Сессия со своим (случайным) прокси. direct=True → без прокси (фолбэк)."""
        s = requests.Session()
        s.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "de-DE,de;q=0.9,en;q=0.8"})
        if not direct:
            proxy = KLEINANZEIGEN_PROXY or (random.choice(PARSER_PROXIES) if PARSER_PROXIES else None)
            if proxy:
                pu = proxy.replace("socks5://", "socks5h://")
                s.proxies.update({"http": pu, "https": pu})
        return s

    def _get_retry(self, url: str, tries: int = 3) -> Optional[str]:
        """GET с ретраями. Последняя попытка — напрямую (если прокси мертвы)."""
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
        """Загружает и парсит объявление в своей сессии (для пула потоков)."""
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
        if re.search(r"seite:\d+", base_url):
            return re.sub(r"seite:\d+", f"seite:{page}", base_url)
        # URL категории заканчивается на /cNNN → вставляем /seite:N/ перед кодом
        m = re.search(r"/(c\d+)/?$", base_url)
        if m:
            return base_url[:m.start()] + f"/seite:{page}/{m.group(1)}"
        if base_url.endswith("/"):
            return base_url + f"s-seite:{page}"
        return base_url + f"/s-seite:{page}"

    @staticmethod
    def _extract_ad_id(url: str) -> Optional[str]:
        m = re.search(r"/(\d+)-\d+-\d+/?$", url)
        return m.group(1) if m else None

    @staticmethod
    def _clean(text: str) -> str:
        return re.sub(r"\s+", " ", text or "").strip()

    @staticmethod
    def _to_iso_date(raw: Optional[str]) -> Optional[str]:
        if not raw:
            return None
        raw = raw.strip()
        today = datetime.now().date()
        if raw.lower().startswith("heute"):
            return today.isoformat()
        if raw.lower().startswith("gestern"):
            return (today - timedelta(days=1)).isoformat()
        m = re.search(r"(\d{2}\.\d{2}\.\d{4})", raw)
        if m:
            return datetime.strptime(m.group(1), "%d.%m.%Y").date().isoformat()
        m = re.search(r"(\d{4}-\d{2}-\d{2})", raw)
        if m:
            return datetime.strptime(m.group(1), "%Y-%m-%d").date().isoformat()
        return None

    def _extract_listing_urls(self, html: str) -> list[str]:
        soup = BeautifulSoup(html, "html.parser")
        out = []
        seen = set()
        for a in soup.find_all("a", href=True):
            href = a.get("href") or ""
            if "/s-anzeige/" not in href:
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
        return lines[0] if lines else ""

    def _extract_price(self, soup: BeautifulSoup, lines: list[str], title: str) -> str:
        # 1) канонический элемент цены Kleinanzeigen (самый надёжный)
        box = soup.find(id="viewad-price")
        if box:
            txt = self._clean(box.get_text(" ", strip=True))
            if txt:
                return txt
        # 2) микроразметка цены
        meta = soup.find("meta", attrs={"itemprop": "price"})
        if meta and meta.get("content"):
            val = self._clean(meta["content"])
            if val:
                # добавим € для читаемости
                return f"{val} €" if "€" not in val else val
        # 3) старый эвристический fallback
        h2 = soup.find("h2")
        if h2:
            txt = self._clean(h2.get_text(" ", strip=True))
            if "€" in txt or txt.lower() in {"vb", "zu verschenken"}:
                return txt
        found_title = False
        for line in lines[:60]:
            if line == title:
                found_title = True
                continue
            if found_title and ("€" in line or line.lower() in {"vb", "zu verschenken"}):
                return line
        return ""

    def _extract_photo_url(self, soup: BeautifulSoup) -> Optional[str]:
        meta = soup.find("meta", attrs={"property": "og:image"})
        if meta and meta.get("content"):
            return meta["content"].strip()
        return None

    def _extract_published_raw(self, lines: list[str]) -> Optional[str]:
        for line in lines[:80]:
            if line.lower().startswith(("heute", "gestern")):
                return line
            if re.fullmatch(r"\d{2}\.\d{2}\.\d{4}", line):
                return line
        return None

    def _extract_registered_raw(self, lines: list[str]) -> Optional[str]:
        for line in lines:
            m = re.search(r"Aktiv seit\s+(\d{2}\.\d{2}\.\d{4})", line, flags=re.I)
            if m:
                return m.group(1)
        return None

    def _extract_seller_identity(self, soup: BeautifulSoup, lines: list[str], title: str):
        blacklist = {
            "privater nutzer", "gewerblicher nutzer", "top zufriedenheit",
            "sehr freundlich", "sehr zuverlässig", "freundlich", "zuverlässig",
            "besonders freundlich", "besonders zuverlässig",
            "alle anzeigen dieses anbieters", "nachricht schreiben",
        }

        candidate_links = []

        # 1) точный сценарий со span.userprofile-vip > a
        for span in soup.find_all("span", class_=lambda x: x and "userprofile-vip" in " ".join(x) if isinstance(x, list) else (x and "userprofile-vip" in x)):
            a = span.find("a", href=True)
            if a:
                candidate_links.append(a)

        # 2) общий сценарий: ссылки на список объявлений / userId
        for a in soup.find_all("a", href=True):
            href = a.get("href") or ""
            if "bestandsliste" in href or "userId=" in href:
                candidate_links.append(a)

        seen = set()
        for a in candidate_links:
            href = a.get("href") or ""
            key = (href, a.get_text(" ", strip=True))
            if key in seen:
                continue
            seen.add(key)
            nickname = self._clean(a.get_text(" ", strip=True))
            lower = nickname.lower()
            if not nickname or nickname == title or len(nickname) > 60:
                continue
            if lower in blacklist or "aktiv seit" in lower or "anzeigen online" in lower:
                continue
            m = re.search(r"userId=(\d+)", href)
            seller_key = m.group(1) if m else href.strip()
            return nickname, seller_key, urljoin(BASE_URL, href)

        # fallback: ник возле Aktiv seit
        idx = None
        for i, line in enumerate(lines):
            if "Aktiv seit" in line:
                idx = i
                break
        if idx is not None:
            for candidate in reversed(lines[max(0, idx - 6):idx]):
                c = candidate.strip()
                cl = c.lower()
                if not c or c == title or len(c) > 60:
                    continue
                if cl in blacklist or "aktiv seit" in cl or "anzeigen online" in cl:
                    continue
                return c, c.lower(), None

        return "", "", None

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
        seller_registered_date = self._to_iso_date(seller_registered_raw)
        seller_nickname, seller_key, seller_profile_url = self._extract_seller_identity(soup, lines, title)
        seller_key = seller_key or (seller_nickname or "unknown").strip().lower()
        return AdItem(
            ad_id=ad_id,
            seller_key=seller_key,
            seller_nickname=seller_nickname,
            seller_profile_url=seller_profile_url,
            seller_registered_raw=seller_registered_raw,
            seller_registered_date=seller_registered_date,
            published_raw=self._extract_published_raw(lines),
            published_date=self._to_iso_date(self._extract_published_raw(lines)),
            title=title,
            price=self._extract_price(soup, lines, title),
            listing_url=url,
            photo_url=self._extract_photo_url(soup),
        )

    def run(
        self,
        published_from: Optional[str],
        published_to: Optional[str],
        seller_registered_from: Optional[str],
        seller_registered_to: Optional[str],
        progress_callback: Optional[Callable[[int, int], None]] = None,
    ) -> dict:
        # validate settings once
        for value in [published_from, published_to, seller_registered_from, seller_registered_to]:
            if value:
                datetime.strptime(value, "%Y-%m-%d")

        # режим "все категории": пустая ссылка → обходим встроенный список
        base = (self.base_url or "").strip().lower()
        if not base or base in ("all", "все", "all_categories", "*"):
            seeds, all_mode = list(KA_ALL_CATEGORIES), True
        else:
            seeds, all_mode = [self.base_url], False
        max_total = PARSE_ALL_MAX_TOTAL if all_mode else self.max_items
        per_category = PARSE_ALL_PER_CATEGORY if all_mode else max_total

        results: list[AdItem] = []
        seen_ads = set(self.seen.get("ad_ids", []))
        seen_sellers = set(self.seen.get("seller_keys", []))

        if progress_callback:
            progress_callback(0, max_total)

        def _passes(item: "AdItem") -> bool:
            if published_from and (not item.published_date or item.published_date < published_from):
                return False
            if published_to and (not item.published_date or item.published_date > published_to):
                return False
            if seller_registered_from and (not item.seller_registered_date or item.seller_registered_date < seller_registered_from):
                return False
            if seller_registered_to and (not item.seller_registered_date or item.seller_registered_date > seller_registered_to):
                return False
            return True

        for seed in seeds:
            if len(results) >= max_total:
                break
            cat_count = 0
            for page in range(1, 13):
                if cat_count >= per_category or len(results) >= max_total:
                    break
                page_url = self._page_url(seed, page)
                page_html = self._get_retry(page_url)
                if not page_html:
                    continue
                urls = self._extract_listing_urls(page_html)

                new_urls = []
                for url in urls:
                    ad_id = self._extract_ad_id(url)
                    if not ad_id or ad_id in seen_ads or ad_id in self.run_seen_ads:
                        continue
                    new_urls.append(url)
                if not new_urls:
                    continue

                # ПАРАЛЛЕЛЬНАЯ загрузка+парсинг объявлений страницы
                with ThreadPoolExecutor(max_workers=PARSER_CONCURRENCY) as ex:
                    parsed = list(ex.map(self._fetch_and_parse, new_urls))

                accepted_before = cat_count
                for item in parsed:
                    if cat_count >= per_category or len(results) >= max_total:
                        break
                    if not item or item.ad_id in self.run_seen_ads:
                        continue
                    if not _passes(item):
                        continue
                    if item.seller_key in seen_sellers or item.seller_key in self.run_seen_sellers:
                        continue

                    results.append(item)
                    self.run_seen_ads.add(item.ad_id)
                    self.run_seen_sellers.add(item.seller_key)
                    cat_count += 1
                    if progress_callback:
                        progress_callback(len(results), max_total)

                # страница не дала ничего нового (виденные/отфильтрованы/403) — выходим
                if cat_count == accepted_before:
                    break

        self.seen["ad_ids"] = sorted(seen_ads | self.run_seen_ads)
        self.seen["seller_keys"] = sorted(seen_sellers | self.run_seen_sellers)
        self._save_seen()

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_path = self.workdir / f"kleinanzeigen_parse_v2_{ts}.json"
        payload = {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "count": len(results),
            "applied_filters": {
                "published_from": published_from,
                "published_to": published_to,
                "seller_registered_from": seller_registered_from,
                "seller_registered_to": seller_registered_to,
            },
            "items": [asdict(x) for x in results],
        }
        out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"count": len(results), "json_path": str(out_path), "seen_path": str(self.seen_path)}
