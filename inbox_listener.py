import asyncio
import html
import imaplib
import logging
import re
import socket
import ssl
from email import message_from_bytes
from email.header import decode_header, make_header
from email.utils import parseaddr
from html import unescape
from typing import Optional

from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from python_socks.sync import Proxy

from config import IMAP_SERVER, IMAP_PORT, INBOX_POLL_INTERVAL, TELEGRAM_PROXY
from database import (
    get_accounts,
    get_account_by_id,
    get_setting,
    incoming_email_exists,
    add_incoming_email,
)

logger = logging.getLogger(__name__)
RECENT_UID_WINDOW = 120


class ProxyIMAP4_SSL(imaplib.IMAP4_SSL):
    def __init__(self, host: str, port: int, proxy_config: Optional[dict] = None, timeout: int = 30):
        self._proxy_config = proxy_config
        self.ssl_context = ssl.create_default_context()
        super().__init__(host=host, port=port, ssl_context=self.ssl_context, timeout=timeout)

    def _create_socket(self, timeout):
        if self._proxy_config:
            proxy_host = self._proxy_config["host"]
            proxy_port = int(self._proxy_config["port"])
            proxy_user = self._proxy_config.get("username")
            proxy_pass = self._proxy_config.get("password")

            if proxy_user and proxy_pass:
                proxy_url = f"socks5://{proxy_user}:{proxy_pass}@{proxy_host}:{proxy_port}"
            else:
                proxy_url = f"socks5://{proxy_host}:{proxy_port}"

            raw_sock = Proxy.from_url(proxy_url).connect(dest_host=self.host, dest_port=self.port, timeout=timeout)
        else:
            raw_sock = socket.create_connection((self.host, self.port), timeout=timeout)
        return self.ssl_context.wrap_socket(raw_sock, server_hostname=self.host)


def _decode_mime(value: str) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def _html_to_text(value: str) -> str:
    value = re.sub(r"<br\s*/?>", "\n", value, flags=re.I)
    value = re.sub(r"</p>", "\n", value, flags=re.I)
    value = re.sub(r"<[^>]+>", "", value)
    value = unescape(value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def _extract_body(msg) -> str:
    plain_parts = []
    html_parts = []

    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            disposition = str(part.get("Content-Disposition", ""))
            if "attachment" in disposition.lower():
                continue

            payload = part.get_payload(decode=True)
            if payload is None:
                continue

            charset = part.get_content_charset() or "utf-8"
            try:
                text = payload.decode(charset, errors="replace")
            except Exception:
                text = payload.decode("utf-8", errors="replace")

            if content_type == "text/plain":
                plain_parts.append(text)
            elif content_type == "text/html":
                html_parts.append(text)
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            try:
                text = payload.decode(charset, errors="replace")
            except Exception:
                text = payload.decode("utf-8", errors="replace")
            if msg.get_content_type() == "text/html":
                html_parts.append(text)
            else:
                plain_parts.append(text)

    if plain_parts:
        return "\n".join(p.strip() for p in plain_parts if p.strip()).strip()
    if html_parts:
        return _html_to_text("\n".join(html_parts))
    return ""


def _parse_global_proxy():
    if not TELEGRAM_PROXY:
        return None
    match = re.match(r"socks5://(?:([^:]+):([^@]+)@)?([^:]+):(\d+)", TELEGRAM_PROXY)
    if not match:
        return None
    username, password, host, port = match.groups()
    return {
        "host": host,
        "port": int(port),
        "username": username,
        "password": password,
    }


def _row_value(row, key, default=None):
    try:
        value = row[key]
        return default if value is None else value
    except Exception:
        return default


def _resolve_proxy_for_account(account_row) -> Optional[dict]:
    host = _row_value(account_row, "host")
    if host:
        return {
            "host": host,
            "port": int(_row_value(account_row, "port", 0)),
            "username": _row_value(account_row, "proxy_user"),
            "password": _row_value(account_row, "proxy_pass"),
        }
    return _parse_global_proxy()


def _extract_mailbox_name(raw_line: bytes | str) -> Optional[str]:
    line = raw_line.decode("utf-8", errors="replace") if isinstance(raw_line, bytes) else str(raw_line)
    matches = re.findall(r'"([^"]+)"', line)
    if matches:
        return matches[-1]
    parts = line.split(" ")
    if parts:
        return parts[-1].strip('"')
    return None


def _discover_mailboxes(imap: imaplib.IMAP4_SSL):
    folders = ["INBOX"]
    try:
        status, data = imap.list()
        if status != "OK" or not data:
            return folders
        found = []
        for item in data:
            name = _extract_mailbox_name(item)
            if not name:
                continue
            lower = name.lower()
            if lower == "inbox":
                found.append(name)
            elif "all mail" in lower or lower.endswith("/allmail") or lower.endswith("/all mail"):
                found.append(name)
        # keep order, dedupe
        ordered = []
        for name in ["INBOX", *found]:
            if name not in ordered:
                ordered.append(name)
        return ordered
    except Exception:
        return folders


def _open_imap_connection(proxy_config: Optional[dict]):
    return ProxyIMAP4_SSL(IMAP_SERVER, IMAP_PORT, proxy_config=proxy_config, timeout=30)


def _fetch_recent_messages_sync(account_row):
    email_addr = account_row["email"]
    app_password = account_row["app_password"]
    proxy_config = _resolve_proxy_for_account(account_row)
    messages = []
    seen_message_ids = set()

    imap = _open_imap_connection(proxy_config)
    try:
        imap.login(email_addr, app_password)
        folders = _discover_mailboxes(imap)

        for folder in folders:
            try:
                status, _ = imap.select(f'"{folder}"', readonly=True)
                if status != "OK":
                    continue
                status, data = imap.uid("search", None, "ALL")
                if status != "OK" or not data or not data[0]:
                    continue

                uid_list = [uid for uid in data[0].split() if uid]
                recent_uids = uid_list[-RECENT_UID_WINDOW:]

                for uid in recent_uids:
                    status, msg_data = imap.uid("fetch", uid, "(BODY.PEEK[])")
                    if status != "OK" or not msg_data:
                        continue

                    raw_email = None
                    for item in msg_data:
                        if isinstance(item, tuple) and len(item) > 1:
                            raw_email = item[1]
                            break
                    if not raw_email:
                        continue

                    msg = message_from_bytes(raw_email)
                    message_id = (msg.get("Message-ID") or "").strip()
                    if not message_id:
                        message_id = f"<{email_addr}-{folder}-{uid.decode() if isinstance(uid, bytes) else uid}@local>"
                    if message_id in seen_message_ids:
                        continue
                    seen_message_ids.add(message_id)

                    from_email = parseaddr(_decode_mime(msg.get("From", "")))[1] or _decode_mime(msg.get("From", ""))
                    if from_email and from_email.strip().lower() == email_addr.strip().lower():
                        continue
                    to_email = parseaddr(_decode_mime(msg.get("To", "")))[1] or email_addr
                    subject = _decode_mime(msg.get("Subject", "")) or "Без темы"
                    in_reply_to = (msg.get("In-Reply-To") or "").strip() or None
                    references_header = (msg.get("References") or "").strip() or None
                    body = _extract_body(msg) or "(пустое письмо)"

                    messages.append(
                        {
                            "from_email": from_email,
                            "to_email": to_email,
                            "subject": subject,
                            "message_id": message_id,
                            "in_reply_to": in_reply_to,
                            "references_header": references_header,
                            "body": body,
                        }
                    )
            except Exception:
                logger.exception("Inbox folder scan failed for %s in folder %s", email_addr, folder)
                continue
    finally:
        try:
            imap.close()
        except Exception:
            pass
        try:
            imap.logout()
        except Exception:
            pass

    return messages


async def _notify(bot: Bot, chat_id: int, incoming_id: int, mail: dict):
    body_preview = mail["body"].strip()
    if len(body_preview) > 2500:
        body_preview = body_preview[:2500] + "..."

    text = (
        "📩 <b>Новое письмо</b>\n\n"
        f"<b>ID:</b> #{incoming_id}\n"
        f"<b>From:</b> <code>{html.escape(mail['from_email'])}</code>\n"
        f"<b>To:</b> <code>{html.escape(mail['to_email'])}</code>\n"
        f"<b>Subject:</b> {html.escape(mail['subject'] or 'Без темы')}\n\n"
        f"<blockquote>{html.escape(body_preview)}</blockquote>"
    )
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="💬 Ответить", callback_data=f"inbox_reply:{incoming_id}"),
                InlineKeyboardButton(text="🛒 Товар", callback_data=f"inbox_product:{incoming_id}"),
            ],
            [InlineKeyboardButton(text="⚡ Быстрые ответы", callback_data=f"inbox_quick_menu:{incoming_id}")],
        ]
    )
    await bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=keyboard)


async def start_inbox_listener(bot: Bot):
    logger.info("Inbox listener started")
    while True:
        try:
            inbox_chat_id = await get_setting("inbox_chat_id")
            if not inbox_chat_id:
                await asyncio.sleep(INBOX_POLL_INTERVAL)
                continue

            base_accounts = await get_accounts(500)
            if not base_accounts:
                await asyncio.sleep(INBOX_POLL_INTERVAL)
                continue

            detailed_accounts = []
            for account in base_accounts:
                if account["status"] != "active":
                    continue
                detailed = await get_account_by_id(account["id"])
                if detailed:
                    detailed_accounts.append(detailed)

            tasks = [asyncio.to_thread(_fetch_recent_messages_sync, account) for account in detailed_accounts]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            for account, result in zip(detailed_accounts, results):
                if isinstance(result, Exception):
                    logger.exception("Inbox polling failed for %s", account["email"], exc_info=result)
                    continue

                for mail in result:
                    if await incoming_email_exists(mail["message_id"]):
                        continue
                    incoming_id = await add_incoming_email(
                        account_id=account["id"],
                        from_email=mail["from_email"],
                        to_email=mail["to_email"],
                        subject=mail["subject"],
                        body=mail["body"],
                        message_id=mail["message_id"],
                        in_reply_to=mail["in_reply_to"],
                        references_header=mail["references_header"],
                    )
                    await _notify(bot, int(inbox_chat_id), incoming_id, mail)
        except Exception:
            logger.exception("Inbox listener main loop failed")

        await asyncio.sleep(INBOX_POLL_INTERVAL)
