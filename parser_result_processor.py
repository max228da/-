import json
import re
import sqlite3
from pathlib import Path
from datetime import datetime

BASE_DIR = Path(__file__).resolve().parent
VBASE_DB = BASE_DIR / "v_base.db"


def _get_conn():
    conn = sqlite3.connect(VBASE_DB)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS v_base (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nickname TEXT NOT NULL,
            title TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(nickname, title)
        )
        """
    )
    # Товары продавца (для кнопки "Товар" при ответе): ключ = нормализованный ник,
    # который совпадает с localpart почты (ник@gmail.com).
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS products (
            nickname TEXT PRIMARY KEY,
            title TEXT,
            price TEXT,
            photo_url TEXT,
            listing_url TEXT,
            created_at TEXT
        )
        """
    )
    conn.commit()
    return conn


def get_vbase_count() -> int:
    conn = _get_conn()
    try:
        cur = conn.execute("SELECT COUNT(*) FROM v_base")
        row = cur.fetchone()
        return int(row[0] or 0)
    finally:
        conn.close()


def normalize_nickname(value: str) -> str:
    value = (value or "").strip().lower()
    raw_tokens = [x for x in re.split(r"\s+", value) if x.strip()]

    cleaned = []
    i = 0
    while i < len(raw_tokens):
        token = raw_tokens[i].strip()
        token = re.sub(r"\.+$", "", token)

        if token == "-" and cleaned and i + 1 < len(raw_tokens):
            next_token = raw_tokens[i + 1].strip()
            next_token = re.sub(r"\.+$", "", next_token)
            next_token = re.sub(r"[^a-z0-9._-]", "", next_token).strip(".")
            if next_token:
                cleaned[-1] = f"{cleaned[-1]}-{next_token}"
            i += 2
            continue

        token = re.sub(r"[^a-z0-9._-]", "", token).strip(".")
        if token:
            cleaned.append(token)
        i += 1

    normalized = ".".join(cleaned)
    normalized = re.sub(r"\.{2,}", ".", normalized).strip(".")
    return normalized


# --- Фильтр качества ников (для валидации/рассылки) ---
MIN_NICK_LEN = 4  # ники короче отбрасываем

# Точные ники, которые не валидируем (служебные / generic / названные пользователем).
# Список легко расширять. Учти: реальные имена вроде 'mark' блокируются тоже.
NICK_BLACKLIST = {
    "user", "users", "admin", "administrator", "info", "test", "testuser", "demo",
    "shop", "store", "sales", "support", "contact", "mail", "email", "noreply",
    "private", "privat", "prive", "verkoper", "koper", "gebruiker", "seller",
    "buyer", "member", "guest", "account", "default", "anoniem", "onbekend",
    "marktplaats", "kleinanzeigen", "mark",
}
# Подстроки — если встречаются в нике, отбраковываем.
NICK_BLACKLIST_SUBSTR = ["nutzer", "priva", "privat"]


def _nick_ok(nickname: str) -> bool:
    if not nickname or len(nickname) < MIN_NICK_LEN:
        return False
    if nickname in NICK_BLACKLIST:
        return False
    if any(sub in nickname for sub in NICK_BLACKLIST_SUBSTR):
        return False
    return True


def _iter_processed_rows(items):
    seen_nicks = set()  # ДЕДУП ПО НИКУ: один продавец = один лид
    for item in items:
        nickname = normalize_nickname(item.get("seller_nickname", ""))
        title = (item.get("title") or "").strip()

        if not title or not _nick_ok(nickname):
            continue
        if nickname in seen_nicks:
            continue
        seen_nicks.add(nickname)
        yield nickname, title


def save_rows_to_vbase(rows) -> int:
    conn = _get_conn()
    added = 0
    try:
        for nickname, title in rows:
            cur = conn.execute(
                "INSERT OR IGNORE INTO v_base (nickname, title, created_at) VALUES (?, ?, ?)",
                (nickname, title, datetime.now().isoformat(timespec="seconds")),
            )
            if cur.rowcount:
                added += 1
        conn.commit()
        return added
    finally:
        conn.close()


def _build_products(items):
    """Полные карточки товара, ключ = нормализованный ник (как в почте)."""
    out = {}
    for item in items:
        nickname = normalize_nickname(item.get("seller_nickname", ""))
        title = (item.get("title") or "").strip()
        if not title or not _nick_ok(nickname):
            continue
        if nickname in out:
            continue  # один товар на ник (первый в этом парсе)
        out[nickname] = {
            "nickname": nickname,
            "title": title,
            "price": (item.get("price") or "").strip(),
            "photo_url": (item.get("photo_url") or "").strip(),
            "listing_url": (item.get("listing_url") or "").strip(),
        }
    return list(out.values())


def save_products(products) -> int:
    conn = _get_conn()
    saved = 0
    try:
        for p in products:
            conn.execute(
                """
                INSERT OR REPLACE INTO products
                (nickname, title, price, photo_url, listing_url, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (p["nickname"], p["title"], p["price"], p["photo_url"],
                 p["listing_url"], datetime.now().isoformat(timespec="seconds")),
            )
            saved += 1
        conn.commit()
        return saved
    finally:
        conn.close()


def process_parser_json(json_path: str):
    path = Path(json_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    items = data.get("items", [])

    rows = list(_iter_processed_rows(items))
    added = save_rows_to_vbase(rows)
    # сохраняем карточки товаров (фото/цена/ссылка) для кнопки "Товар"
    try:
        save_products(_build_products(items))
    except Exception:
        pass

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = path.with_name(f"kleinanzeigen_processed_{ts}.txt")
    out_path.write_text("\n".join(f"{nickname}|{title}" for nickname, title in rows), encoding="utf-8")
    return str(out_path), added, len(rows)
