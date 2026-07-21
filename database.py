# database.py
import os
import aiosqlite
from typing import List, Dict, Optional
from config import DB_PATH

# База товаров парсера (v_base.db в корне проекта — тот же файл, что у parser_result_processor)
VBASE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "v_base.db")


async def get_product_by_email(email: str) -> Optional[Dict]:
    """
    Находит карточку товара по почте отправителя.
    localpart почты (ник@gmail.com) = нормализованный ник = ключ в products.
    """
    if not email or "@" not in email:
        return None
    nick = email.split("@", 1)[0].strip().lower()
    try:
        async with aiosqlite.connect(VBASE_PATH) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM products WHERE nickname = ?", (nick,))
            row = await cursor.fetchone()
            return dict(row) if row else None
    except Exception:
        return None  # таблицы ещё нет (парс не запускался) и т.п.


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        # WAL режим: параллельные соединения не блокируют друг друга на чтении/записи.
        # Без него параллельная рассылка + inbox_listener часто ловят "database is locked".
        await db.execute("PRAGMA journal_mode=WAL;")
        await db.execute("PRAGMA busy_timeout=5000;")
        await db.execute("PRAGMA synchronous=NORMAL;")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT UNIQUE,
                app_password TEXT,
                from_name TEXT,
                proxy_id INTEGER,
                status TEXT DEFAULT 'active',
                cooldown_until INTEGER DEFAULT 0,
                sent_count INTEGER DEFAULT 0,
                last_used INTEGER DEFAULT 0,
                last_check INTEGER DEFAULT 0,
                check_status TEXT,
                FOREIGN KEY(proxy_id) REFERENCES proxies(id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS proxies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                host TEXT,
                port INTEGER,
                username TEXT,
                password TEXT,
                protocol TEXT DEFAULT 'socks5',
                is_active INTEGER DEFAULT 1,
                last_check INTEGER DEFAULT 0,
                external_ip TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS presets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT,
                subject TEXT,
                body TEXT,
                created_at INTEGER DEFAULT (strftime('%s','now'))
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS recipients (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT UNIQUE,
                status TEXT DEFAULT 'active',
                added_at INTEGER DEFAULT (strftime('%s','now'))
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS mailing_stats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER,
                recipient TEXT,
                preset_id INTEGER,
                status TEXT,
                error TEXT,
                timestamp INTEGER DEFAULT (strftime('%s','now'))
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS outgoing_emails (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER,
                to_email TEXT,
                subject TEXT,
                body TEXT,
                message_id TEXT UNIQUE,
                created_at INTEGER DEFAULT (strftime('%s','now'))
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS incoming_emails (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER,
                from_email TEXT,
                to_email TEXT,
                subject TEXT,
                body TEXT,
                message_id TEXT UNIQUE,
                in_reply_to TEXT,
                references_header TEXT,
                replied INTEGER DEFAULT 0,
                received_at INTEGER DEFAULT (strftime('%s','now'))
            )
        """)
        # Миграция: суточный счётчик отправки на аккаунт (для дневного лимита)
        for col, ddl in [
            ("daily_sent", "ALTER TABLE accounts ADD COLUMN daily_sent INTEGER DEFAULT 0"),
            ("daily_date", "ALTER TABLE accounts ADD COLUMN daily_date TEXT DEFAULT ''"),
            ("created_day", "ALTER TABLE accounts ADD COLUMN created_day TEXT DEFAULT ''"),
        ]:
            try:
                await db.execute(ddl)
            except Exception:
                pass  # колонка уже существует
        await db.commit()


async def add_account(email: str, app_password: str, from_name: str = None) -> bool:
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "INSERT OR IGNORE INTO accounts (email, app_password, from_name) VALUES (?, ?, ?)",
                (email, app_password, from_name or "Default"),
            )
            await db.commit()
            return cursor.rowcount > 0
    except Exception:
        return False


async def get_accounts(limit: int = 100) -> List[Dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM accounts ORDER BY id DESC LIMIT ?", (limit,))
        return await cursor.fetchall()


async def get_account_by_id(account_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT a.*, p.host, p.port, p.username as proxy_user, p.password as proxy_pass
            FROM accounts a
            LEFT JOIN proxies p ON a.proxy_id = p.id
            WHERE a.id = ?
            """,
            (account_id,),
        )
        return await cursor.fetchone()


async def delete_account(email: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("DELETE FROM accounts WHERE email = ?", (email,))
        await db.commit()
        return cursor.rowcount > 0


async def clear_accounts():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM accounts")
        await db.execute("DELETE FROM outgoing_emails")
        await db.execute("DELETE FROM incoming_emails")
        await db.execute("DELETE FROM settings WHERE key LIKE 'imap_last_uid_%'")
        await db.commit()


async def update_account_check(email: str, success: bool, error_msg: str = None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE accounts SET last_check = strftime('%s','now'), check_status = ? WHERE email = ?",
            ("OK" if success else f"ERROR: {error_msg}" if error_msg else "ERROR", email),
        )
        await db.commit()


async def get_available_account():
    """Read-only lookup — оставлено для обратной совместимости.
    Для рассылки используйте claim_available_account(): она атомарно резервирует аккаунт.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT a.*, p.host, p.port, p.username as proxy_user, p.password as proxy_pass
            FROM accounts a
            LEFT JOIN proxies p ON a.proxy_id = p.id
            WHERE a.status='active' AND a.cooldown_until < strftime('%s','now')
            ORDER BY a.sent_count ASC LIMIT 1
            """
        )
        return await cursor.fetchone()


async def claim_available_account(reserve_seconds: int = 120, daily_limit: int = None):
    """
    Атомарно захватывает свободный аккаунт: одним UPDATE ... RETURNING выставляет
    временный cooldown, чтобы конкурирующие воркеры не могли выбрать тот же аккаунт.

    daily_limit: если задан — аккаунты, уже отправившие >= лимита за сегодня,
    не выбираются (защита от бана Gmail).

    После успешной отправки вызовите set_account_cooldown() с реальным cooldown.
    """
    daily_clause = ""
    params = [reserve_seconds]
    if daily_limit is not None:
        daily_clause = "AND NOT (daily_date = date('now','localtime') AND daily_sent >= ?)"
        params.append(daily_limit)

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            f"""
            UPDATE accounts
            SET cooldown_until = strftime('%s','now') + ?,
                last_used = strftime('%s','now')
            WHERE id = (
                SELECT id FROM accounts
                WHERE status='active' AND cooldown_until < strftime('%s','now')
                {daily_clause}
                ORDER BY sent_count ASC, last_used ASC
                LIMIT 1
            )
            RETURNING id, email, app_password, from_name, proxy_id,
                      status, cooldown_until, sent_count, last_used,
                      last_check, check_status
            """,
            params,
        )
        row = await cursor.fetchone()
        await db.commit()
        if row is None:
            return None

        result = dict(row)
        proxy_id = result.get("proxy_id")
        if proxy_id:
            cursor = await db.execute(
                "SELECT host, port, username, password FROM proxies WHERE id = ?",
                (proxy_id,),
            )
            proxy_row = await cursor.fetchone()
            if proxy_row:
                result["host"] = proxy_row["host"]
                result["port"] = proxy_row["port"]
                result["proxy_user"] = proxy_row["username"]
                result["proxy_pass"] = proxy_row["password"]
            else:
                result["host"] = None
                result["port"] = None
                result["proxy_user"] = None
                result["proxy_pass"] = None
        else:
            result["host"] = None
            result["port"] = None
            result["proxy_user"] = None
            result["proxy_pass"] = None
        return result


async def get_sendable_accounts(daily_limit: int = None) -> List[Dict]:
    """
    Активные аккаунты, не исчерпавшие суточный лимит.
    Используется рассылкой: на каждый аккаунт поднимается несколько параллельных
    SMTP-сессий (Gmail это допускает), поэтому эксклюзивный claim здесь не нужен.
    Возвращает список dict с данными прокси аккаунта (если привязан).
    """
    where = "WHERE a.status='active'"
    params = []
    if daily_limit is not None:
        where += " AND NOT (a.daily_date = date('now','localtime') AND a.daily_sent >= ?)"
        params.append(daily_limit)

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            f"""
            SELECT a.*, p.host, p.port, p.username as proxy_user, p.password as proxy_pass
            FROM accounts a
            LEFT JOIN proxies p ON a.proxy_id = p.id
            {where}
            ORDER BY a.sent_count ASC
            """,
            params,
        )
        return [dict(r) for r in await cursor.fetchall()]


async def get_account_daily_sent(account_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            SELECT CASE WHEN daily_date = date('now','localtime') THEN daily_sent ELSE 0 END
            FROM accounts WHERE id = ?
            """,
            (account_id,),
        )
        row = await cursor.fetchone()
        return row[0] if row else 0


async def set_account_cooldown(account_id: int, seconds: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE accounts SET cooldown_until = strftime('%s','now') + ? WHERE id = ?",
            (seconds, account_id),
        )
        await db.commit()


async def increment_sent_count(account_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        # daily_sent сбрасывается автоматически при смене даты
        await db.execute(
            """
            UPDATE accounts SET
                sent_count = sent_count + 1,
                daily_sent = CASE WHEN daily_date = date('now','localtime') THEN daily_sent + 1 ELSE 1 END,
                daily_date = date('now','localtime'),
                last_used = strftime('%s','now')
            WHERE id = ?
            """,
            (account_id,),
        )
        await db.commit()


async def add_proxy(host: str, port: int, username: str = None, password: str = None) -> bool:
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "INSERT OR IGNORE INTO proxies (host, port, username, password) VALUES (?, ?, ?, ?)",
                (host, port, username, password),
            )
            await db.commit()
            return cursor.rowcount > 0
    except Exception:
        return False


async def update_proxy_check(proxy_id: int, success: bool, external_ip: str = None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE proxies SET last_check = strftime('%s','now'), is_active = ?, external_ip = ? WHERE id = ?",
            (1 if success else 0, external_ip, proxy_id),
        )
        await db.commit()


async def get_proxies(only_active: bool = True) -> List[Dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        query = "SELECT * FROM proxies"
        if only_active:
            query += " WHERE is_active = 1"
        query += " ORDER BY id DESC"
        cursor = await db.execute(query)
        return await cursor.fetchall()


async def delete_proxy(proxy_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM proxies WHERE id = ?", (proxy_id,))
        await db.commit()


async def add_preset(subject: str, body: str, name: str = None) -> bool:
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "INSERT INTO presets (name, subject, body) VALUES (?, ?, ?)",
                (name or f"Preset {subject[:20]}", subject, body),
            )
            await db.commit()
            return cursor.rowcount > 0
    except Exception:
        return False


async def get_presets() -> List[Dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM presets ORDER BY id DESC")
        return await cursor.fetchall()


async def delete_preset(preset_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM presets WHERE id = ?", (preset_id,))
        await db.commit()


async def add_recipients_batch(emails: List[str]) -> int:
    count = 0
    async with aiosqlite.connect(DB_PATH) as db:
        for email in emails:
            try:
                cursor = await db.execute("INSERT OR IGNORE INTO recipients (email) VALUES (?)", (email,))
                if cursor.rowcount > 0:
                    count += 1
            except Exception:
                pass
        await db.commit()
    return count


async def clear_recipients():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM recipients")
        await db.commit()


async def get_recipients() -> List[str]:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT email FROM recipients WHERE status = 'active'")
        rows = await cursor.fetchall()
        return [row[0] for row in rows]


async def get_setting(key: str) -> Optional[str]:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT value FROM settings WHERE key = ?", (key,))
        row = await cursor.fetchone()
        return row[0] if row else None


async def set_setting(key: str, value: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
        await db.commit()


async def get_stats() -> Dict:
    async with aiosqlite.connect(DB_PATH) as db:
        stats = {}
        cursor = await db.execute("SELECT COUNT(*) FROM accounts")
        stats['total_accounts'] = (await cursor.fetchone())[0]
        cursor = await db.execute("SELECT COUNT(*) FROM accounts WHERE status = 'active'")
        stats['active_accounts'] = (await cursor.fetchone())[0]
        cursor = await db.execute("SELECT COUNT(*) FROM accounts WHERE cooldown_until > strftime('%s','now')")
        stats['cooldown_accounts'] = (await cursor.fetchone())[0]
        cursor = await db.execute("SELECT COUNT(*) FROM recipients")
        stats['total_recipients'] = (await cursor.fetchone())[0]
        cursor = await db.execute("SELECT SUM(sent_count) FROM accounts")
        stats['total_sent'] = (await cursor.fetchone())[0] or 0
        cursor = await db.execute("SELECT COUNT(*) FROM proxies")
        stats['total_proxies'] = (await cursor.fetchone())[0]
        cursor = await db.execute("SELECT COUNT(*) FROM presets")
        stats['total_presets'] = (await cursor.fetchone())[0]
        cursor = await db.execute("SELECT COUNT(*) FROM incoming_emails")
        stats['total_incoming'] = (await cursor.fetchone())[0]
        return stats


async def log_mailing_error(account_id: int, recipient: str, preset_id: int, error: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO mailing_stats (account_id, recipient, preset_id, status, error) VALUES (?, ?, ?, 'error', ?)",
            (account_id, recipient, preset_id, error),
        )
        await db.commit()


async def log_outgoing_email(account_id: int, to_email: str, subject: str, body: str, message_id: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO outgoing_emails (account_id, to_email, subject, body, message_id) VALUES (?, ?, ?, ?, ?)",
            (account_id, to_email, subject, body, message_id),
        )
        await db.commit()


async def filter_unsent_emails(emails: List[str]) -> List[str]:
    """
    Возвращает подсписок emails, на которые мы ещё НЕ отправляли письма
    (нет записи в outgoing_emails). Используется авто-пайплайном, чтобы не слать
    повторно одним и тем же получателям между циклами.
    """
    if not emails:
        return []
    async with aiosqlite.connect(DB_PATH) as db:
        placeholders = ",".join("?" for _ in emails)
        cursor = await db.execute(
            f"SELECT to_email FROM outgoing_emails WHERE to_email IN ({placeholders})",
            emails,
        )
        already = {row[0] for row in await cursor.fetchall()}
    # сохраняем порядок и убираем дубликаты внутри входного списка
    seen = set()
    result = []
    for e in emails:
        if e in already or e in seen:
            continue
        seen.add(e)
        result.append(e)
    return result


async def count_active_accounts() -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT COUNT(*) FROM accounts WHERE status='active'")
        return (await cursor.fetchone())[0]


async def incoming_email_exists(message_id: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT 1 FROM incoming_emails WHERE message_id = ?", (message_id,))
        return await cursor.fetchone() is not None


async def add_incoming_email(
    account_id: int,
    from_email: str,
    to_email: str,
    subject: str,
    body: str,
    message_id: str,
    in_reply_to: str = None,
    references_header: str = None,
) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT OR IGNORE INTO incoming_emails
            (account_id, from_email, to_email, subject, body, message_id, in_reply_to, references_header)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (account_id, from_email, to_email, subject, body, message_id, in_reply_to, references_header),
        )
        await db.commit()
        cursor = await db.execute("SELECT id FROM incoming_emails WHERE message_id = ?", (message_id,))
        row = await cursor.fetchone()
        return row[0]


async def get_incoming_email(incoming_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM incoming_emails WHERE id = ?", (incoming_id,))
        return await cursor.fetchone()


async def mark_incoming_replied(incoming_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE incoming_emails SET replied = 1 WHERE id = ?", (incoming_id,))
        await db.commit()
