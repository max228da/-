# utils/email_validator.py
"""
Batched SMTP-based email validator.

Ключевая идея: одно SMTP-соединение обслуживает N адресов через RCPT TO / RSET,
вместо полного handshake на каждый email. Даёт 5-20x ускорение по сравнению
с одиночными сессиями.

Также:
- MX кэшируется per-domain (для gmail.com — один резолв на всю пачку).
- socket.settimeout() выставляется явно после proxy.connect() — иначе через SOCKS5
  таймаут может не наследоваться и getreply() виснет минутами.
- 4xx (transient) адреса пере-ставятся в очередь; сессия закрывается и переоткрывается,
  чтобы не жечь один и тот же IP под троттлингом.
- Опциональная ротация прокси между воркерами — снимает per-IP лимит Gmail.
"""

import smtplib
import socket
import threading
import time
from queue import Queue, Empty
from typing import List, Tuple, Callable, Optional

import dns.resolver
from python_socks.sync import Proxy


class BatchEmailValidator:
    def __init__(
        self,
        domains: List[str],
        threads: int = 3,
        timeout: int = 10,
        proxy_url: Optional[str] = None,
        proxies: Optional[List[str]] = None,
        from_email: str = "verify@example.com",
        helo_host: str = "mail.example.com",
        batch_size: int = 30,
        max_requeue: int = 2,
    ):
        self.domains = domains
        self.threads = threads
        self.timeout = timeout
        self.batch_size = batch_size
        self.max_requeue = max_requeue
        self.from_email = from_email
        self.helo_host = helo_host
        self.progress_callback: Optional[Callable] = None

        if proxies:
            self.proxies: List[Optional[str]] = list(proxies)
        elif proxy_url:
            self.proxies = [proxy_url]
        else:
            self.proxies = [None]

        self.resolver = dns.resolver.Resolver(configure=False)
        self.resolver.nameservers = ["8.8.8.8", "1.1.1.1"]
        self.resolver.timeout = self.timeout
        self.resolver.lifetime = self.timeout

        self._mx_cache: dict = {}
        self._mx_lock = threading.Lock()

    # ---------- Public API ----------

    def set_progress_callback(self, callback: Callable):
        self.progress_callback = callback

    def validate_nicknames(self, nicknames: List[str]) -> List[str]:
        emails = [f"{nick}@{domain}" for nick in nicknames for domain in self.domains]
        total = len(emails)

        queue: Queue = Queue()
        for email in emails:
            queue.put(email)

        valid: List[str] = []
        lock = threading.Lock()
        counter = {"done": 0}
        requeue_counts: dict = {}

        threads = []
        for i in range(self.threads):
            proxy_url = self.proxies[i % len(self.proxies)]
            t = threading.Thread(
                target=self._worker,
                args=(queue, valid, lock, total, proxy_url, requeue_counts, counter),
                daemon=True,
            )
            t.start()
            threads.append(t)

        for t in threads:
            t.join()

        return valid

    # ---------- MX ----------

    def _get_mx(self, domain: str) -> Tuple[Optional[str], Optional[str]]:
        with self._mx_lock:
            cached = self._mx_cache.get(domain)
            if cached:
                return cached, None

        try:
            answers = self.resolver.resolve(domain, "MX")
            mx_records = sorted(
                [(r.preference, str(r.exchange).rstrip(".")) for r in answers],
                key=lambda x: x[0],
            )
            if not mx_records:
                return None, "No MX records"
            mx = mx_records[0][1]
            with self._mx_lock:
                self._mx_cache[domain] = mx
            return mx, None
        except dns.resolver.NoAnswer:
            return None, "No MX records"
        except dns.resolver.NXDOMAIN:
            return None, "NXDOMAIN"
        except dns.resolver.Timeout:
            return None, "DNS timeout"
        except Exception as e:
            return None, f"DNS error: {str(e)[:50]}"

    # ---------- SMTP session ----------

    def _connect(self, mx_host: str, proxy_url: Optional[str], port: int = 25) -> Optional[smtplib.SMTP]:
        """
        Открывает SMTP-сессию, делает EHLO/HELO + MAIL FROM.
        Возвращает готовую к RCPT TO сессию или None, если не удалось.
        """
        try:
            if proxy_url:
                proxy = Proxy.from_url(proxy_url, rdns=True)
                sock = proxy.connect(dest_host=mx_host, dest_port=port, timeout=self.timeout)
            else:
                sock = socket.create_connection((mx_host, port), timeout=self.timeout)

            # ВАЖНО: явно проставляем таймаут — python_socks его не наследует
            sock.settimeout(self.timeout)

            smtp = smtplib.SMTP(timeout=self.timeout)
            smtp.sock = sock
            smtp.file = sock.makefile("rb")

            code, _ = smtp.getreply()
            if code != 220:
                self._hard_close(smtp)
                return None

            # EHLO с fallback на HELO
            smtp.putcmd("EHLO", self.helo_host)
            code, _ = smtp.getreply()
            if code != 250:
                smtp.putcmd("HELO", self.helo_host)
                code, _ = smtp.getreply()
                if code != 250:
                    self._hard_close(smtp)
                    return None

            smtp.putcmd("MAIL FROM:", f"<{self.from_email}>")
            code, _ = smtp.getreply()
            if code != 250:
                self._hard_close(smtp)
                return None

            return smtp
        except Exception:
            return None

    def _rset(self, smtp: smtplib.SMTP) -> bool:
        try:
            smtp.putcmd("RSET")
            code, _ = smtp.getreply()
            if code != 250:
                return False
            # После RSET нужно снова MAIL FROM
            smtp.putcmd("MAIL FROM:", f"<{self.from_email}>")
            code, _ = smtp.getreply()
            return code == 250
        except Exception:
            return False

    def _hard_close(self, smtp: Optional[smtplib.SMTP]):
        if not smtp:
            return
        try:
            smtp.close()
        except Exception:
            pass

    def _quit(self, smtp: Optional[smtplib.SMTP]):
        if not smtp:
            return
        try:
            smtp.putcmd("QUIT")
            smtp.getreply()
        except Exception:
            pass
        self._hard_close(smtp)

    def _check_on_session(self, smtp: smtplib.SMTP, email: str) -> Tuple[str, str]:
        """
        Возвращает (status, info), где status ∈ {'valid', 'invalid', 'transient', 'session_dead'}.
        """
        try:
            smtp.putcmd("RCPT TO:", f"<{email}>")
            code, msg = smtp.getreply()
            msg_text = msg.decode(errors="ignore") if isinstance(msg, bytes) else str(msg)

            if code in (250, 251):
                ok = self._rset(smtp)
                return ("valid" if ok else "valid", f"OK {code}")  # valid независимо
            if code in (550, 551, 552, 553, 554):
                # 5xx постоянные — адрес не существует / отказ
                if "5.1.1" in msg_text:
                    info = f"NOT EXISTS ({code} 5.1.1)"
                else:
                    info = f"REJECTED {code}: {msg_text[:60]}"
                # После 5xx сессия обычно живёт, но RSET на всякий случай
                self._rset(smtp)
                return "invalid", info
            if 400 <= code < 500:
                # 4xx — временная (rate limit, greylist, etc). Сессия скорее всего мертва.
                return "transient", f"TRANSIENT {code}: {msg_text[:60]}"
            # Всё остальное трактуем как невалидное, но пробуем восстановить сессию
            self._rset(smtp)
            return "invalid", f"CODE {code}: {msg_text[:60]}"
        except (socket.timeout, ConnectionError, smtplib.SMTPServerDisconnected, OSError) as e:
            return "session_dead", f"conn: {str(e)[:60]}"
        except Exception as e:
            return "session_dead", f"err: {str(e)[:60]}"

    # ---------- Worker ----------

    def _worker(
        self,
        queue: Queue,
        valid: List[str],
        lock: threading.Lock,
        total: int,
        proxy_url: Optional[str],
        requeue_counts: dict,
        counter: dict,
    ):
        smtp: Optional[smtplib.SMTP] = None
        current_mx: Optional[str] = None
        checks_on_session = 0

        while True:
            try:
                email = queue.get_nowait()
            except Empty:
                break

            # Валидность формата
            if "@" not in email:
                self._report(lock, counter, total, email, "✗ Invalid format")
                queue.task_done()
                continue

            domain = email.split("@", 1)[1]
            mx, dns_err = self._get_mx(domain)
            if not mx:
                self._report(lock, counter, total, email, f"✗ {dns_err}")
                queue.task_done()
                continue

            # Если сменился MX или пора обновить сессию — переоткрываем
            need_reconnect = (
                smtp is None
                or current_mx != mx
                or checks_on_session >= self.batch_size
            )
            if need_reconnect:
                self._quit(smtp)
                smtp = None
                current_mx = mx
                checks_on_session = 0
                for attempt in range(3):
                    smtp = self._connect(mx, proxy_url)
                    if smtp:
                        break
                    time.sleep(1.0 * (attempt + 1))

            if smtp is None:
                # Не смогли подключиться — requeue или fail
                if self._requeue_or_fail(email, queue, requeue_counts, lock, counter, total, "✗ Cannot connect"):
                    time.sleep(2.0)
                queue.task_done()
                continue

            status, info = self._check_on_session(smtp, email)
            checks_on_session += 1

            if status == "valid":
                with lock:
                    valid.append(email)
                    counter["done"] += 1
                    if self.progress_callback:
                        self.progress_callback(counter["done"], total, email, f"✓ {info}")
            elif status == "invalid":
                self._report(lock, counter, total, email, f"✗ {info}")
            elif status == "transient":
                # Сессия скорее всего протухла — закрываем и requeue письмо
                self._hard_close(smtp)
                smtp = None
                current_mx = None
                self._requeue_or_fail(email, queue, requeue_counts, lock, counter, total, f"✗ {info}")
                time.sleep(2.0)
            else:  # session_dead
                self._hard_close(smtp)
                smtp = None
                current_mx = None
                self._requeue_or_fail(email, queue, requeue_counts, lock, counter, total, f"✗ {info}")

            queue.task_done()

        self._quit(smtp)

    def _requeue_or_fail(
        self,
        email: str,
        queue: Queue,
        requeue_counts: dict,
        lock: threading.Lock,
        counter: dict,
        total: int,
        fail_status: str,
    ) -> bool:
        """
        Возвращает True если поставили обратно в очередь; False если исчерпан лимит и записали в fail.
        """
        with lock:
            rc = requeue_counts.get(email, 0) + 1
            requeue_counts[email] = rc
        if rc <= self.max_requeue:
            queue.put(email)
            return True
        self._report(lock, counter, total, email, fail_status)
        return False

    def _report(self, lock: threading.Lock, counter: dict, total: int, email: str, status: str):
        with lock:
            counter["done"] += 1
            if self.progress_callback:
                self.progress_callback(counter["done"], total, email, status)


# Обратная совместимость с существующим импортом
EmailValidator = BatchEmailValidator
