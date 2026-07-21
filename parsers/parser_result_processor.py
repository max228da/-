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


def _iter_processed_rows(items):
    seen = set()
    for item in items:
        nickname = normalize_nickname(item.get("seller_nickname", ""))
        title = (item.get("title") or "").strip()

        if not nickname or not title:
            continue
        if any(block in nickname for block in ["nutzer", "priva", "privat"]):
            continue
        if len(nickname) < 5:
            continue

        key = (nickname, title.lower())
        if key in seen:
            continue
        seen.add(key)
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


def process_parser_json(json_path: str):
    path = Path(json_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    items = data.get("items", [])

    rows = list(_iter_processed_rows(items))
    added = save_rows_to_vbase(rows)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = path.with_name(f"kleinanzeigen_processed_{ts}.txt")
    out_path.write_text("\n".join(f"{nickname}|{title}" for nickname, title in rows), encoding="utf-8")
    return str(out_path), added, len(rows)
