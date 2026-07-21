# quick_replies.py
"""
Готовые быстрые ответы (немецкий) — для кнопки "⚡ Быстрые ответы" при ответе
на входящее письмо. Список легко редактировать.
"""

import random

QUICK_REPLIES = [
    "Vielen Dank für Ihre Antwort, dann werde ich die Bestellung jetzt bezahlen.",
    "Danke für die Rückmeldung, ich werde den Kauf jetzt abschließen.",
    "Vielen Dank, dann bezahle ich den Artikel jetzt direkt.",
    "Danke für die schnelle Antwort, ich werde die Zahlung jetzt vornehmen.",
    "Alles klar, danke. Dann bezahle ich die Bestellung jetzt.",
    "Vielen Dank, ich werde den Betrag jetzt überweisen.",
    "Danke Ihnen, dann schließe ich die Bestellung jetzt ab.",
    "Vielen Dank für die Info, ich bezahle jetzt.",
    "Alles klar, dann werde ich den Kauf jetzt bezahlen.",
    "Danke für Ihre Nachricht, ich mache die Zahlung jetzt fertig.",
    "Vielen Dank, dann erledige ich die Bezahlung jetzt.",
    "Danke, ich werde die Bestellung jetzt bezahlen.",
    "Perfekt, danke. Dann zahle ich jetzt.",
    "Vielen Dank für die Rückmeldung, ich werde jetzt bezahlen.",
    "Alles klar, ich bezahle den Artikel jetzt.",
    "Danke für Ihre Antwort, dann mache ich die Zahlung jetzt.",
    "Vielen Dank, ich werde den Kauf jetzt abschließen.",
    "Danke, dann überweise ich den Betrag jetzt.",
    "Alles klar, vielen Dank. Ich bezahle jetzt.",
    "Danke für die Info, dann werde ich die Bestellung jetzt bezahlen.",
    "Vielen Dank, ich erledige die Zahlung jetzt.",
    "Danke, dann schließe ich den Kauf jetzt ab.",
    "Alles klar, dann mache ich die Bezahlung jetzt.",
    "Vielen Dank für die schnelle Rückmeldung, ich zahle jetzt.",
    "Danke Ihnen, ich werde die Zahlung jetzt durchführen.",
    "Perfekt, dann bezahle ich die Bestellung jetzt.",
    "Danke für die Antwort, ich werde jetzt bezahlen.",
    "Alles klar, ich mache die Bestellung jetzt fertig.",
    "Vielen Dank, dann zahle ich den Betrag jetzt.",
    "Danke, dann werde ich die Bezahlung jetzt abschließen.",
]


def random_quick_reply() -> str:
    return random.choice(QUICK_REPLIES)
