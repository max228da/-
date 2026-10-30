# dixy_parser

Парсер акций сети **Дикси** по магазинам Москвы со скидкой ≥ 70% (порог
настраивается). Источник данных — агрегатор **Edadeal** (edadeal.ru).

## Почему через Edadeal, а не напрямую с dixy.ru

`dixy.ru` — сайт на Bitrix, но каталога товаров с ценами **там нет**.
В `sitemap.xml` всего 7 корпоративных страниц (главная, клуб, контакты,
«Многору», легал), товары с ценами живут только в мобильном приложении
через закрытый API, который требует SMS-авторизации и привязки к магазину
по геопозиции.

Edadeal — агрегатор акций всех ритейлеров РФ, включая Дикси. Содержит
название, старую/новую цену, % скидки, срок действия, список магазинов,
категорию, картинку. То есть ровно то что нужно для задачи.

## Установка

```bash
pip install requests
# опционально для SOCKS-прокси:
pip install "requests[socks]"
```

## Подготовка — ОБЯЗАТЕЛЬНО

### 1. Российский прокси

Edadeal геоблокирован вне РФ. Плюс Яндекс выкинет капчу при входе с
иностранного IP. Нужен **резидентный российский IP**.

```bash
export EDADEAL_PROXY="socks5://login:pass@ru-proxy.example.com:1080"
# или http://login:pass@host:port
```

### 2. Куки Яндекс-аккаунта

Edadeal полностью за Yandex SSO. Нужны куки залогиненного Яндекс-аккаунта.

**Как снять:**

1. В браузере (Chrome/Firefox) с **российским IP** открой https://edadeal.ru/
2. Залогинься через Яндекс
3. Открой `https://edadeal.ru/moskva/retailers/dixy` — должна показаться
   страница Дикси с товарами
4. Экспортируй куки в формате Netscape `cookies.txt`:
   - Chrome: расширение **«Get cookies.txt LOCALLY»** → экспорт для домена
     `edadeal.ru` **и** `yandex.ru`/`passport.yandex.ru`
   - Firefox: расширение **«cookies.txt»**
5. Сохрани файл как `dixy_parser/yandex_cookies.txt`

**Важно:** куки обновлять ~раз в неделю (`Session_id` живёт ~7 дней).
Если видишь `AuthRequired: Redirect to SSO` — куки протухли, снимай заново.

### 3. User-Agent

В `config.py` поле `USER_AGENT` должно совпадать с UA браузера, из которого
сняли куки. Иначе Яндекс обнаружит расхождение fingerprint и выкинет из
сессии.

## Использование

```bash
# Все акции Дикси в Москве со скидкой ≥ 70%, в JSON и CSV
python -m dixy_parser

# Другой порог
python -m dixy_parser --min-discount 50

# Другой город
python -m dixy_parser --locality sankt-peterburg

# Только CSV
python -m dixy_parser --format csv

# С прокси из командной строки
python -m dixy_parser --proxy "http://user:pass@ru-proxy:8080"
```

Результаты пишутся в `dixy_parser/out/`:
- `dixy_discount70_YYYYMMDD_HHMMSS.json` — полный дамп всех найденных офферов
- `dixy_discount70_YYYYMMDD_HHMMSS.csv` — таблица для Excel/Google Sheets

## Что внутри

| Файл | Что делает |
|---|---|
| `edadeal_client.py` | HTTP-клиент с куки + прокси + ретраями. Пробует сначала JSON API v3, при отсутствии падает в legacy /web/search/offers (protobuf) |
| `proto_decoder.py` | Декодер wire-формата protobuf без зависимостей — удобно если Edadeal изменит нумерацию полей, карта правится в одном месте |
| `schema.proto` | Канонические номера полей (reference для `proto_decoder.py`) |
| `filter.py` | Фильтр по минимальному % скидки, с автодосчётом из old/new price |
| `exporter.py` | JSON + CSV экспортёры |
| `config.py` | Все настройки |
| `__main__.py` | CLI |

## Известные ограничения

1. **API Edadeal может меняться.** На момент октября 2026 известно что:
   - `/web/search/offers` (protobuf) → работал в 2022 через `squark.edadeal.ru`,
     этот поддомен сейчас отдаёт сертификат на `zapravki.edadeal.ru` —
     возможно переехал. Клиент пробует оба эндпоинта.
   - `/api/v3/retailers/{slug}/offers` — JSON, точный маршрут проверяется
     на первом запросе из двух кандидатов.
2. **Слуг ритейлера.** В разное время было `dixy` и `diksi`. Клиент
   пробует оба подряд.
3. **Пагинация.** Клиент идёт по страницам пока сервер не скажет «больше
   нет», с жёстким потолком `MAX_PAGES_PER_SEGMENT` против зацикливания.
4. **Магазины.** Edadeal возвращает `shop_ids` для оффера, но не их полный
   список по городу. Если нужна привязка «акция → адреса магазинов»,
   придётся отдельно тянуть `/api/v3/shops?retailer=dixy&locality=moskva`
   (TODO, не реализовано).

## Правовая часть

Edadeal — публичный сервис Яндекса, акции ритейлеров там выложены
открыто для просмотра. Автоматический сбор для личного мониторинга цен
— ровно та нагрузка, под которую сервис и предназначен (их мобильное
приложение делает то же самое). Соблюдай паузы между запросами
(`REQUEST_DELAY_SEC`) и не хами, чтобы не словить rate-limit.

## Траблшутинг

**`AuthRequired: Redirect to SSO`** — куки протухли или не загружены.
Пересними `yandex_cookies.txt` (см. выше).

**`JsonModeNotAvailable` + `All retries failed`** — оба endpoint-а недоступны.
Проверь:
1. Прокси работает: `curl --proxy $EDADEAL_PROXY https://edadeal.ru/`
2. С этим IP ты можешь залогиниться в браузере на edadeal.ru
3. UA в `config.py` совпадает с браузерным

**`HTTP 429 Too Many Requests`** — слишком быстро. Клиент сам тормозит
на экспоненциальном backoff, но если повторяется — увеличь
`REQUEST_DELAY_SEC` в `config.py` (например `(2.0, 5.0)`).

**Пустой результат при залогиненной сессии** — возможно изменились
имена полей в JSON. Запусти с `-vv`, посмотри сырой ответ, обнови
`_parse_json_offers` / `_json_to_raw` в `edadeal_client.py`.
