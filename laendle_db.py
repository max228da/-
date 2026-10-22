"""
Слой БД для модуля laendleanzeiger: наблюдения (watches), найденные объявления
и воронка сделок (consignment: написал → согласился → на витрине → продано).

Использует тот же файл DB_PATH, что и основной бот, но свои таблицы с префиксом
laendle_. Инициализация вызывается из bot.on_startup().
"""
import time
from typing import List, Dict, Optional

import aiosqlite

from config import DB_PATH

# Статусы воронки сделки (consignment-модель)
DEAL_STATUSES = [
    ("new",       "🆕 Новое"),
    ("drafted",   "✍️ Черновик готов"),
    ("contacted", "📨 Написал продавцу"),
    ("replied",   "💬 Ответил"),
    ("agreed",    "🤝 Согласился"),
    ("listed",    "🏬 На витрине"),
    ("sold",      "✅ Продано"),
    ("declined",  "❌ Отказ"),
    ("skip",      "🚫 Пропуск"),
]
DEAL_STATUS_LABELS = dict(DEAL_STATUSES)


async def init_laendle_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA busy_timeout=5000;")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS laendle_watches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT DEFAULT 'category',   -- category | search | url
                value TEXT,
                label TEXT,
                enabled INTEGER DEFAULT 1,
                created_at INTEGER DEFAULT (strftime('%s','now')),
                UNIQUE(kind, value)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS laendle_listings (
                ad_id TEXT PRIMARY KEY,
                title TEXT,
                listing_url TEXT,
                price_display TEXT,
                price_value REAL,
                is_free INTEGER DEFAULT 0,
                location TEXT,
                date_raw TEXT,
                photo_url TEXT,
                image_count INTEGER DEFAULT 0,
                is_premium INTEGER DEFAULT 0,
                has_phone INTEGER DEFAULT 0,
                description TEXT,
                category TEXT,
                seller_name TEXT,
                seller_profile_url TEXT,
                priority_score INTEGER DEFAULT 0,
                deal_status TEXT DEFAULT 'new',
                deal_note TEXT,
                draft_text TEXT,
                found_at INTEGER DEFAULT (strftime('%s','now')),
                notified INTEGER DEFAULT 0,
                updated_at INTEGER DEFAULT (strftime('%s','now'))
            )
        """)
        await db.commit()


# ---------------- watches ----------------
async def add_watch(kind: str, value: str, label: str = None) -> bool:
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(
                "INSERT OR IGNORE INTO laendle_watches (kind, value, label) VALUES (?, ?, ?)",
                (kind, value.strip(), (label or value).strip()),
            )
            await db.commit()
            return cur.rowcount > 0
    except Exception:
        return False


async def list_watches(only_enabled: bool = False) -> List[Dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        q = "SELECT * FROM laendle_watches"
        if only_enabled:
            q += " WHERE enabled = 1"
        q += " ORDER BY id"
        cur = await db.execute(q)
        return [dict(r) for r in await cur.fetchall()]


async def toggle_watch(watch_id: int) -> Optional[int]:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE laendle_watches SET enabled = 1 - enabled WHERE id = ?", (watch_id,))
        await db.commit()
        cur = await db.execute("SELECT enabled FROM laendle_watches WHERE id = ?", (watch_id,))
        row = await cur.fetchone()
        return row[0] if row else None


async def delete_watch(watch_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("DELETE FROM laendle_watches WHERE id = ?", (watch_id,))
        await db.commit()
        return cur.rowcount > 0


# ---------------- listings ----------------
def _score(item: dict) -> int:
    """Простой приоритет: продавец с верифиц. телефоном и живым объявлением — выше."""
    s = 0
    if item.get("has_validated_phone"):
        s += 2            # доступен напрямую (звонок) — не жрёт лимит мессенджера
    if (item.get("image_count") or 0) >= 3:
        s += 1            # нормально оформленное объявление
    if not item.get("is_free"):
        s += 1
    if item.get("is_premium"):
        s += 1            # платное размещение — чаще коммерческий продавец
    return s


async def upsert_listing(item: dict) -> bool:
    """
    Вставляет объявление, если его ещё нет. Возвращает True, если оно НОВОЕ.
    Виденные не трогаем (чтобы не перетирать статус сделки/заметки).
    """
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """
            INSERT OR IGNORE INTO laendle_listings
              (ad_id, title, listing_url, price_display, price_value, is_free,
               location, date_raw, photo_url, image_count, is_premium, has_phone,
               description, category, seller_name, seller_profile_url, priority_score)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                item["ad_id"], item.get("title"), item.get("listing_url"),
                item.get("price_display"), item.get("price_value"),
                1 if item.get("is_free") else 0,
                item.get("location"), item.get("date_raw"), item.get("photo_url"),
                item.get("image_count") or 0,
                1 if item.get("is_premium") else 0,
                1 if item.get("has_validated_phone") else 0,
                item.get("description"), item.get("category"),
                item.get("seller_name"), item.get("seller_profile_url"),
                _score(item),
            ),
        )
        await db.commit()
        return cur.rowcount > 0


async def get_listing(ad_id: str) -> Optional[Dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM laendle_listings WHERE ad_id = ?", (ad_id,))
        row = await cur.fetchone()
        return dict(row) if row else None


async def update_listing_fields(ad_id: str, **fields):
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields)
    vals = list(fields.values()) + [int(time.time()), ad_id]
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            f"UPDATE laendle_listings SET {cols}, updated_at = ? WHERE ad_id = ?", vals)
        await db.commit()


async def set_deal_status(ad_id: str, status: str):
    await update_listing_fields(ad_id, deal_status=status)


async def set_draft(ad_id: str, draft_text: str):
    await update_listing_fields(ad_id, draft_text=draft_text)


async def set_note(ad_id: str, note: str):
    await update_listing_fields(ad_id, deal_note=note)


async def mark_notified(ad_id: str):
    await update_listing_fields(ad_id, notified=1)


async def list_by_status(status: str, limit: int = 50) -> List[Dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM laendle_listings WHERE deal_status = ? "
            "ORDER BY priority_score DESC, found_at DESC LIMIT ?",
            (status, limit))
        return [dict(r) for r in await cur.fetchall()]


async def funnel_counts() -> Dict[str, int]:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT deal_status, COUNT(*) FROM laendle_listings GROUP BY deal_status")
        rows = await cur.fetchall()
        return {r[0]: r[1] for r in rows}


async def laendle_stats() -> Dict:
    async with aiosqlite.connect(DB_PATH) as db:
        out = {}
        cur = await db.execute("SELECT COUNT(*) FROM laendle_listings")
        out["total_listings"] = (await cur.fetchone())[0]
        cur = await db.execute("SELECT COUNT(*) FROM laendle_watches WHERE enabled = 1")
        out["active_watches"] = (await cur.fetchone())[0]
        cur = await db.execute(
            "SELECT COUNT(*) FROM laendle_listings WHERE deal_status = 'new'")
        out["new_listings"] = (await cur.fetchone())[0]
        return out
