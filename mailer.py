# mailer.py
import smtplib
import ssl
import socket
import time
from email.message import EmailMessage
from email.utils import make_msgid, formatdate
from typing import Optional, Union, Dict, Tuple

from python_socks.sync import Proxy

SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 465

# Классы ошибок, которые считаем временными (стоит ретраить)
_TRANSIENT_EXCEPTIONS = (
    smtplib.SMTPServerDisconnected,
    smtplib.SMTPConnectError,
    ConnectionError,
    ConnectionResetError,
    ConnectionAbortedError,
    TimeoutError,
    socket.timeout,
)

# Постоянные — не ретраить
_PERMANENT_EXCEPTIONS = (
    smtplib.SMTPAuthenticationError,
    smtplib.SMTPRecipientsRefused,
    smtplib.SMTPSenderRefused,
    smtplib.SMTPDataError,  # обычно постоянная, но при 4xx можем словить исключение — проверяем код ниже
    smtplib.SMTPNotSupportedError,
)


class SocksSMTP_SSL(smtplib.SMTP_SSL):
    def __init__(
        self,
        host: str,
        port: int,
        *,
        proxy_config: Optional[Union[Dict, str]] = None,
        timeout: int = 30,
        context: Optional[ssl.SSLContext] = None,
    ):
        self.proxy_config = proxy_config
        super().__init__(host=host, port=port, timeout=timeout, context=context)

    def _get_socket(self, host, port, timeout):
        if self.proxy_config:
            if isinstance(self.proxy_config, dict):
                proxy_host = self.proxy_config["host"]
                proxy_port = self.proxy_config["port"]
                proxy_user = self.proxy_config.get("username")
                proxy_pass = self.proxy_config.get("password")
                if proxy_user and proxy_pass:
                    proxy_url = f"socks5://{proxy_user}:{proxy_pass}@{proxy_host}:{proxy_port}"
                else:
                    proxy_url = f"socks5://{proxy_host}:{proxy_port}"
            else:
                proxy_url = self.proxy_config

            proxy = Proxy.from_url(proxy_url, rdns=True)
            raw_sock = proxy.connect(dest_host=host, dest_port=port, timeout=timeout)
        else:
            raw_sock = socket.create_connection((host, port), timeout=timeout)

        # Явный таймаут — python_socks не всегда его наследует
        raw_sock.settimeout(timeout)
        return self.context.wrap_socket(raw_sock, server_hostname=host)


def _open_smtp(
    proxy_config: Optional[Union[Dict, str]] = None,
    timeout: int = 30,
    debug: bool = False,
):
    context = ssl.create_default_context()
    server = SocksSMTP_SSL(
        SMTP_SERVER,
        SMTP_PORT,
        proxy_config=proxy_config,
        timeout=timeout,
        context=context,
    )
    if debug:
        server.set_debuglevel(1)
    return server


def check_proxy(proxy_config: Union[Dict, str], timeout: int = 15):
    try:
        if isinstance(proxy_config, dict):
            proxy_host = proxy_config["host"]
            proxy_port = int(proxy_config["port"])
            proxy_user = proxy_config.get("username")
            proxy_pass = proxy_config.get("password")
            if proxy_user and proxy_pass:
                proxy_url = f"socks5://{proxy_user}:{proxy_pass}@{proxy_host}:{proxy_port}"
            else:
                proxy_url = f"socks5://{proxy_host}:{proxy_port}"
        else:
            proxy_url = proxy_config

        proxy = Proxy.from_url(proxy_url, rdns=True)
        raw_sock = proxy.connect(dest_host="api.ipify.org", dest_port=443, timeout=timeout)
        raw_sock.settimeout(timeout)
        context = ssl.create_default_context()
        with context.wrap_socket(raw_sock, server_hostname="api.ipify.org") as sock:
            request = (
                "GET /?format=text HTTP/1.1\r\n"
                "Host: api.ipify.org\r\n"
                "Connection: close\r\n"
                "User-Agent: tg-mailer-bot\r\n\r\n"
            )
            sock.sendall(request.encode("utf-8"))
            response = b""
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                response += chunk

        if b"\r\n\r\n" not in response:
            return False, "Некорректный HTTP-ответ"

        headers_raw, body_raw = response.split(b"\r\n\r\n", 1)
        status_line = headers_raw.split(b"\r\n", 1)[0].decode("utf-8", errors="replace")
        if " 200 " not in f" {status_line} ":
            return False, f"Сервис проверки IP вернул: {status_line}"

        body = body_raw.decode("utf-8", errors="replace").strip()
        if not body:
            return False, "Пустой ответ от сервиса проверки IP"

        return True, body
    except Exception as e:
        return False, str(e)


def check_smtp_login(
    email: str,
    app_password: str,
    proxy_config: Optional[Union[Dict, str]] = None,
    debug: bool = False,
):
    try:
        with _open_smtp(proxy_config=proxy_config, debug=debug) as server:
            server.login(email, app_password)
            return True, None
    except Exception as e:
        return False, str(e)


def _is_transient_smtp_code(code: int) -> bool:
    return 400 <= code < 500


def _classify_error(e: Exception) -> Tuple[bool, str]:
    """
    Возвращает (is_transient, err_message).
    """
    if isinstance(e, smtplib.SMTPResponseException):
        code = getattr(e, "smtp_code", 0) or 0
        text = getattr(e, "smtp_error", b"")
        if isinstance(text, bytes):
            text = text.decode(errors="ignore")
        transient = _is_transient_smtp_code(code)
        return transient, f"SMTP {code}: {text[:120]}"
    if isinstance(e, _PERMANENT_EXCEPTIONS):
        return False, f"{type(e).__name__}: {str(e)[:120]}"
    if isinstance(e, _TRANSIENT_EXCEPTIONS):
        return True, f"{type(e).__name__}: {str(e)[:120]}"
    if isinstance(e, OSError):
        return True, f"OSError: {str(e)[:120]}"
    return False, f"{type(e).__name__}: {str(e)[:120]}"


def _send_with_retry(
    account_email: str,
    app_password: str,
    msg: EmailMessage,
    proxy_config: Optional[Union[Dict, str]],
    retries: int,
    retry_delay: float,
    timeout: int,
    debug: bool,
) -> Tuple[bool, str]:
    last_err = "unknown"
    for attempt in range(retries + 1):
        try:
            with _open_smtp(proxy_config=proxy_config, timeout=timeout, debug=debug) as server:
                server.login(account_email, app_password)
                server.send_message(msg)
            return True, msg["Message-ID"]
        except Exception as e:
            transient, err_msg = _classify_error(e)
            last_err = err_msg
            if transient and attempt < retries:
                # Экспоненциальный backoff: base * (attempt+1)
                time.sleep(retry_delay * (attempt + 1))
                continue
            return False, last_err
    return False, last_err


def _build_message(from_name: str, account_email: str, to_email: str, subject: str, body: str) -> EmailMessage:
    msg = EmailMessage()
    msg["Message-ID"] = make_msgid(domain="gmail.com")
    msg["Date"] = formatdate(localtime=True)
    msg["Subject"] = subject
    msg["From"] = f"{from_name} <{account_email}>" if from_name else account_email
    msg["To"] = to_email
    msg["Reply-To"] = account_email
    msg.set_content(body)
    return msg


def send_batch_sync(
    account_email: str,
    app_password: str,
    items,
    proxy_config: Optional[Union[Dict, str]] = None,
    timeout: int = 30,
    retry_delay: float = 3.0,
    debug: bool = False,
):
    """
    Шлёт пачку писем через ОДНУ SMTP-сессию (один логин на всю пачку).
    Это главный ускоритель: коннект+логин стоит ~1с, а письмо в открытой сессии ~0.1-0.3с.

    items: список (to_email, subject, body, from_name)
    Возвращает список (to_email, success, msg_id_или_ошибка) в том же порядке.

    Если сессия падает посреди пачки — переоткрывает её и продолжает с текущего письма.
    """
    results = []
    server = None
    reconnects = 0

    def _open():
        s = _open_smtp(proxy_config=proxy_config, timeout=timeout, debug=debug)
        s.login(account_email, app_password)
        return s

    i = 0
    while i < len(items):
        to_email, subject, body, from_name = items[i]

        if server is None:
            try:
                server = _open()
            except Exception as e:
                transient, err = _classify_error(e)
                if transient and reconnects < 2:
                    reconnects += 1
                    time.sleep(retry_delay * reconnects)
                    continue
                # не смогли подключиться — валим остаток пачки
                for rest in items[i:]:
                    results.append((rest[0], False, err))
                return results

        try:
            msg = _build_message(from_name, account_email, to_email, subject, body)
            server.send_message(msg)
            results.append((to_email, True, msg["Message-ID"]))
            i += 1
        except Exception as e:
            transient, err = _classify_error(e)
            # сессия могла умереть — закрываем и пробуем переоткрыть
            try:
                server.close()
            except Exception:
                pass
            server = None
            if transient and reconnects < 2:
                reconnects += 1
                time.sleep(retry_delay * reconnects)
                continue  # то же письмо на новой сессии
            results.append((to_email, False, err))
            i += 1

    if server is not None:
        try:
            server.quit()
        except Exception:
            pass
    return results


def send_email_sync(
    account_email: str,
    app_password: str,
    from_name: str,
    to_email: str,
    subject: str,
    body: str,
    proxy_config: Optional[Union[Dict, str]] = None,
    debug: bool = False,
    retries: int = 2,
    retry_delay: float = 3.0,
    timeout: int = 30,
):
    msg = EmailMessage()
    # Message-ID с доменом gmail.com и заголовок Date — гигиена для антиспама:
    # письмо без Date или с «странным» Message-ID сразу подозрительно.
    msg["Message-ID"] = make_msgid(domain="gmail.com")
    msg["Date"] = formatdate(localtime=True)
    msg["Subject"] = subject
    msg["From"] = f"{from_name} <{account_email}>" if from_name else account_email
    msg["To"] = to_email
    msg["Reply-To"] = account_email
    msg.set_content(body)

    return _send_with_retry(
        account_email=account_email,
        app_password=app_password,
        msg=msg,
        proxy_config=proxy_config,
        retries=retries,
        retry_delay=retry_delay,
        timeout=timeout,
        debug=debug,
    )


def send_reply_sync(
    account_email: str,
    app_password: str,
    from_name: str,
    to_email: str,
    subject: str,
    body: str,
    reply_to_message_id: str,
    references: Optional[str] = None,
    proxy_config: Optional[Union[Dict, str]] = None,
    debug: bool = False,
    retries: int = 2,
    retry_delay: float = 3.0,
    timeout: int = 30,
):
    reply_subject = subject or "Reply"
    if not reply_subject.lower().startswith("re:"):
        reply_subject = f"Re: {reply_subject}"

    msg = EmailMessage()
    msg["Message-ID"] = make_msgid(domain="gmail.com")
    msg["Date"] = formatdate(localtime=True)
    msg["Subject"] = reply_subject
    msg["From"] = f"{from_name} <{account_email}>" if from_name else account_email
    msg["To"] = to_email
    msg["In-Reply-To"] = reply_to_message_id
    msg["References"] = (
        f"{references} {reply_to_message_id}".strip()
        if references
        else reply_to_message_id
    )
    msg.set_content(body)

    return _send_with_retry(
        account_email=account_email,
        app_password=app_password,
        msg=msg,
        proxy_config=proxy_config,
        retries=retries,
        retry_delay=retry_delay,
        timeout=timeout,
        debug=debug,
    )
