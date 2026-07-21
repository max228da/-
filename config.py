# config.py
import os

BOT_TOKEN = "8632876728:AAGQNyuty1BbmrFb0YF2jJX3pRu4IMY5HIY"

DB_PATH = "data/mailer.db"
TEMP_DIR = "data/temp"
VALID_EMAILS_FILE = "data/valid_emails.txt"
NICKNAMES_FILE = "data/nicknames.txt"

SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 587
SMTP_TIMEOUT = 30

# ---- Validator ----
VALIDATOR_THREADS = 10
VALIDATOR_TIMEOUT = 10           # per-socket read/connect timeout (было 20)
VALIDATOR_DOMAINS = ["gmail.com"]
VALIDATOR_BATCH_SIZE = 30        # RCPT TO per one SMTP session before reconnect
VALIDATOR_MAX_REQUEUE = 2        # retry attempts for transient (4xx) errors
VALIDATOR_FROM_EMAIL = "verify@example.com"  # MAIL FROM used for RCPT probes
VALIDATOR_HELO_HOST = "mail.example.com"     # EHLO/HELO hostname

TELEGRAM_PROXY = None

# ВАЖНО про прокси:
# Проверено — lomaproxy и mobile.notproxy.one режут SMTP-порты (25/465/587):
# SOCKS отвечает "Connection not allowed by ruleset". Резидентные/мобильные прокси
# почти всегда так делают, чтобы через них не слали спам.
# Прямое подключение с этой машины: 25/465/587/993 — все ОТКРЫТЫ и Gmail отвечает.
# Поэтому: ПАРСЕРЫ идут через прокси (им нужен только 443), а
# ВАЛИДАЦИЯ и РАССЫЛКА — напрямую.

# Пул прокси для парсеров. СЕЙЧАС ПУСТ (парсинг напрямую — быстро).
# Прокси lomaproxy на 20.07.2026 не отвечали (0/10, auth/timeout) и только
# тормозили парсинг. Впиши рабочие прокси сюда — код подхватит их автоматически.
PARSER_PROXIES = []

# Старый пул lomaproxy (не рабочий) — оставлен на случай восстановления:
_PARSER_PROXIES_LOMA = [
    "socks5://ZeyxYzl8mC_session-iJ8HY0AU:dNhcXIIyk4@proxy.lomaproxy.com:48175",
    "socks5://ZeyxYzl8mC_session-iC6McyoB:dNhcXIIyk4@proxy.lomaproxy.com:48175",
    "socks5://ZeyxYzl8mC_session-JrphjKBa:dNhcXIIyk4@proxy.lomaproxy.com:48175",
    "socks5://ZeyxYzl8mC_session-sqxM8NC9:dNhcXIIyk4@proxy.lomaproxy.com:48175",
    "socks5://ZeyxYzl8mC_session-w1yWaapg:dNhcXIIyk4@proxy.lomaproxy.com:48175",
    "socks5://ZeyxYzl8mC_session-FYmOmbCn:dNhcXIIyk4@proxy.lomaproxy.com:48175",
    "socks5://ZeyxYzl8mC_session-TZFEldlC:dNhcXIIyk4@proxy.lomaproxy.com:48175",
    "socks5://ZeyxYzl8mC_session-FMxwvFCq:dNhcXIIyk4@proxy.lomaproxy.com:48175",
    "socks5://ZeyxYzl8mC_session-yp1D5Cu8:dNhcXIIyk4@proxy.lomaproxy.com:48175",
    "socks5://ZeyxYzl8mC_session-lbhASQPB:dNhcXIIyk4@proxy.lomaproxy.com:48175",
]

# Валидатор — НАПРЯМУЮ (прокси не пускают на порт 25)
VALIDATOR_PROXY = None
VALIDATOR_PROXIES = []

# ---- Mailing ----
# Рассылка — НАПРЯМУЮ (прокси не пускают на порт 465).
MAILING_PROXY = None
MAILING_PROXIES = []
MAILING_THREADS = 3
# --- Темп отправки: не больше MAILING_RATE_COUNT писем за MAILING_RATE_WINDOW секунд
#     НА КАЖДЫЙ аккаунт. Окно считается от НАЧАЛА пачки, а не от конца,
#     поэтому время самой отправки входит в окно.
#     10 аккаунтов x 20/3мин  ->  ~67 писем/мин суммарно.
MAILING_RATE_COUNT = 20          # писем
MAILING_RATE_WINDOW = 180        # за сколько секунд (3 минуты)

MAILING_COOLDOWN = 0             # не используется (темп задаёт rate-лимит выше)
MAILING_COOLDOWN_JITTER_UNUSED = 0
MAILING_BURST_SIZE = 20          # писем в одной SMTP-сессии (один логин на пачку)
MAILING_BATCH_DELAY = 0
# При лимите 20/3мин параллелить один аккаунт незачем: одна сессия отправляет
# 20 писем за ~42с и дальше ждёт. Одна сессия = мягче для Gmail.
# (замеры на живом Gmail: 1 сессия -> 28/мин, 3 -> 60/мин, 6 -> 81/мин,
#  12 -> 21/мин + ошибки 421, т.е. Gmail троттлит аккаунт)
MAILING_SESSIONS_PER_ACCOUNT = 1
MAILING_MAX_WORKERS = 30         # общий потолок сессий по всем аккаунтам
MAILING_ACCOUNT_RESERVE = 120    # временная бронь аккаунта на время отправки (защита от гонки)
MAILING_RETRIES = 2              # ретраи внутри send_email_sync для transient ошибок
MAILING_RETRY_DELAY = 3          # базовая задержка между ретраями (сек), с backoff *attempt
MAILING_REQUEUE_MAX = 1          # сколько раз воркер может пере-поставить письмо в очередь

# ---- Анти-спам / защита аккаунтов ----
MAILING_DAILY_LIMIT = 480        # макс писем с ОДНОГО аккаунта в сутки (у Gmail потолок ~500)
MAILING_COOLDOWN_JITTER = 0      # не используется (темп задаёт rate-лимит)
MAILING_WARMUP_ENABLED = False   # плавный разогон новых аккаунтов (см. warmup_daily_cap)
MAILING_WARMUP_START = 20        # старт: писем/сутки в первый день аккаунта
MAILING_WARMUP_STEP = 20         # +N к лимиту каждый следующий день

# ---- Inbox ----
IMAP_SERVER = "imap.gmail.com"
IMAP_PORT = 993
INBOX_POLL_INTERVAL = 5
INBOX_FETCH_LIMIT = 20

# ---- Прокси парсеров ----
# Оба парсера берут случайный прокси из PARSER_PROXIES (только 443 — у них работает).
# None-переопределение: поставь строку socks5://... чтобы форсировать один прокси,
# или [] в PARSER_PROXIES чтобы парсить напрямую.
MARKTPLAATS_PROXY = None
KLEINANZEIGEN_PROXY = None

# ---- Marktplaats через внутренний search API (быстро + большой объём) ----
# l1-категории (id) — парсер идёт по ним, сортировка по свежести, пагинация по offset.
MP_L1_CATEGORIES = [322, 31, 820, 504, 621, 1776, 445, 91, 565, 784, 1099, 728, 1847, 395]
MP_API_LIMIT = 30            # объявлений за один запрос API
MP_API_PER_CATEGORY = 40     # новых лидов брать с одной категории за цикл
MP_API_MAX_TOTAL = 400       # общий потолок за цикл
MP_API_MAX_OFFSET = 300      # как глубоко листать категорию в поисках новых

# ---- Режим "все категории" ----
# Если ссылка парсера пустая — парсер обходит эти топ-категории по очереди.
PARSE_ALL_PER_CATEGORY = 8      # сколько новых объявлений брать с одной категории за проход
PARSE_ALL_MAX_TOTAL = 120       # общий лимит объявлений за один проход в режиме "все категории"

# Параллельная загрузка объявлений (главный ускоритель парсинга).
# Каждый поток берёт свой прокси из пула → и параллелизм, и распределение по IP.
PARSER_CONCURRENCY = 8          # одновременных загрузок страниц объявлений

KA_ALL_CATEGORIES = [
    "https://www.kleinanzeigen.de/s-multimedia-elektronik/c161",
    "https://www.kleinanzeigen.de/s-haus-garten/c80",
    "https://www.kleinanzeigen.de/s-mode-beauty/c153",
    "https://www.kleinanzeigen.de/s-familie-kind-baby/c17",
    "https://www.kleinanzeigen.de/s-freizeit-nachbarschaft/c185",
    "https://www.kleinanzeigen.de/s-musik-film-buecher/c73",
    "https://www.kleinanzeigen.de/s-auto-rad-boot/c210",
    "https://www.kleinanzeigen.de/s-haustiere/c130",
    "https://www.kleinanzeigen.de/s-eintrittskarten-tickets/c231",
    "https://www.kleinanzeigen.de/s-zu-verschenken-tauschen/c272",
]

MP_ALL_CATEGORIES = [
    "https://www.marktplaats.nl/l/computers-en-software/",
    "https://www.marktplaats.nl/l/audio-tv-en-foto/",
    "https://www.marktplaats.nl/l/telecommunicatie/",
    "https://www.marktplaats.nl/l/huis-en-inrichting/",
    "https://www.marktplaats.nl/l/kleding-dames/",
    "https://www.marktplaats.nl/l/kleding-heren/",
    "https://www.marktplaats.nl/l/fietsen-en-brommers/",
    "https://www.marktplaats.nl/l/auto-s/",
    "https://www.marktplaats.nl/l/kinderen-en-baby-s/",
    "https://www.marktplaats.nl/l/sport-en-fitness/",
    "https://www.marktplaats.nl/l/hobby-en-vrije-tijd/",
    "https://www.marktplaats.nl/l/muziek-en-instrumenten/",
    "https://www.marktplaats.nl/l/tuin-en-terras/",
    "https://www.marktplaats.nl/l/dieren-en-toebehoren/",
    "https://www.marktplaats.nl/l/games-en-spelcomputers/",
]

# ---- Auto pipeline (парс → валидация → рассылка в цикле) ----
AUTO_CYCLE_DELAY = 60            # пауза между циклами (сек), чтобы не долбить Kleinanzeigen
AUTO_EMPTY_CYCLE_DELAY = 120     # пауза, если цикл не дал новых лидов
AUTO_STATUS_REFRESH = 8          # как часто обновлять закреплённое сообщение статуса (сек)
AUTO_PREFLIGHT_LOGIN_CHECK = True  # проверять SMTP-логин аккаунтов перед стартом
AUTO_PARSE_MAX_ITEMS = 100       # максимум объявлений за один парс-проход

os.makedirs("data", exist_ok=True)
os.makedirs("data/temp", exist_ok=True)
