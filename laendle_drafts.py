"""
Генерация черновика сообщения продавцу на laendleanzeiger (нем. язык).

Модель — консигнация: вежливо спрашиваем разрешение выставить его товар в нашем
онлайн-магазине и продавать для него. НИКАКОЙ автоотправки: функция только
отдаёт текст, пользователь копирует и пишет сам со своего аккаунта.

Варианты формулировок нужны не для обхода антиспама (аккаунт один, темп ручной),
а чтобы сообщения звучали по-человечески под конкретный товар.

ИИ-черновик (Claude API) — опционально: включается, только если задан ключ в
config.CLAUDE_API_KEY или в настройке laendle_api_key. Иначе — шаблоны.
"""
import hashlib
from typing import Optional

# Шаблоны на немецком. {shop} — имя магазина, {title} — товар, {price} — цена.
TEMPLATES = [
    (
        "Guten Tag,\n\n"
        "ich betreibe den Online-Shop „{shop}“ und bin auf Ihr Angebot "
        "„{title}“ ({price}) gestoßen. Ich würde den Artikel gerne in meinem "
        "Shop listen und für Sie mitverkaufen – für Sie entsteht dabei kein "
        "Aufwand und kein Risiko.\n\n"
        "Hätten Sie grundsätzlich Interesse an so einer Zusammenarbeit? "
        "Dann bespreche ich gerne die Details mit Ihnen.\n\n"
        "Beste Grüße"
    ),
    (
        "Hallo,\n\n"
        "mir ist Ihr Inserat „{title}“ ({price}) aufgefallen. Ich führe einen "
        "kleinen Online-Shop ({shop}) und suche laufend gute Artikel, die ich "
        "für Verkäufer aus der Region mitanbieten kann.\n\n"
        "Wäre es für Sie in Ordnung, wenn ich Ihr Angebot bei mir im Shop "
        "aufnehme? Melde ich mich dann, sobald sich ein Käufer findet. "
        "Über eine kurze Rückmeldung freue ich mich.\n\n"
        "Freundliche Grüße"
    ),
    (
        "Grüß Sie,\n\n"
        "ich betreibe den Shop „{shop}“ und interessiere mich für Ihren Artikel "
        "„{title}“ ({price}). Ich würde ihn gerne zusätzlich über meinen "
        "Online-Shop anbieten und bei einem Verkauf alles mit Ihnen abwickeln.\n\n"
        "Dürfte ich Ihr Angebot dafür bei mir listen? Gerne kläre ich vorab alle "
        "offenen Fragen mit Ihnen.\n\n"
        "Mit besten Grüßen"
    ),
]

# Доп. короткие варианты для повторного контакта (когда нашёлся покупатель).
BUYER_FOUND_TEMPLATE = (
    "Hallo,\n\n"
    "für Ihren Artikel „{title}“ hat sich über meinen Shop „{shop}“ ein "
    "Interessent gefunden. Ist der Artikel noch verfügbar? Dann würde ich die "
    "nächsten Schritte gerne mit Ihnen abstimmen.\n\n"
    "Freundliche Grüße"
)


def _pick_template(ad_id: str) -> str:
    """Стабильный выбор варианта по ad_id — один товар всегда даёт один текст."""
    h = int(hashlib.sha1((ad_id or "x").encode()).hexdigest(), 16)
    return TEMPLATES[h % len(TEMPLATES)]


def template_draft(listing: dict, shop_name: str) -> str:
    return _pick_template(listing.get("ad_id", "")).format(
        shop=shop_name or "mein Online-Shop",
        title=(listing.get("title") or "").strip(),
        price=listing.get("price_display") or "VB",
    )


def buyer_found_draft(listing: dict, shop_name: str) -> str:
    return BUYER_FOUND_TEMPLATE.format(
        shop=shop_name or "mein Online-Shop",
        title=(listing.get("title") or "").strip(),
    )


def _get_api_key() -> Optional[str]:
    try:
        from config import CLAUDE_API_KEY
        if CLAUDE_API_KEY:
            return CLAUDE_API_KEY
    except Exception:
        pass
    return None


def ai_draft(listing: dict, shop_name: str, api_key: Optional[str] = None) -> Optional[str]:
    """
    ИИ-черновик через Claude API. Возвращает текст или None, если ИИ недоступен
    (нет ключа / нет SDK / ошибка сети) — тогда вызывающий код берёт шаблон.
    """
    key = api_key or _get_api_key()
    if not key:
        return None
    try:
        import anthropic
    except Exception:
        return None
    try:
        client = anthropic.Anthropic(api_key=key)
        prompt = (
            "Schreibe eine kurze, höfliche Nachricht auf Deutsch an einen privaten "
            "Verkäufer auf einer Kleinanzeigen-Plattform. Ich betreibe den Online-Shop "
            f"\"{shop_name or 'mein Online-Shop'}\" und möchte höflich fragen, ob ich "
            "seinen Artikel in meinem Shop listen und als Kommission (Consignment) "
            "mitverkaufen darf. Kein Druck, kein Werbe-Spam, natürlicher Ton, max 6 Sätze. "
            f"Artikel: \"{(listing.get('title') or '').strip()}\", Preis: "
            f"{listing.get('price_display') or 'VB'}. Gib nur den Nachrichtentext zurück."
        )
        msg = client.messages.create(
            model="claude-haiku-5-5",
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}],
        )
        parts = [b.text for b in msg.content if getattr(b, "type", None) == "text"]
        text = "\n".join(parts).strip()
        return text or None
    except Exception:
        return None


def make_draft(listing: dict, shop_name: str, use_ai: bool = False,
               api_key: Optional[str] = None) -> str:
    """Главная точка: ИИ если включён и доступен, иначе шаблон."""
    if use_ai:
        t = ai_draft(listing, shop_name, api_key=api_key)
        if t:
            return t
    return template_draft(listing, shop_name)
