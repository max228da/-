# namegen.py
"""
Генератор случайных европейских женских имён (имя + фамилия).
Используется как имя отправителя (From) для рассылки.
Смесь немецких / нидерландских / общеевропейских имён — под площадки DE/NL.
"""

import random

FEMALE_FIRST_NAMES = [
    # German
    "Anna", "Lena", "Sophie", "Marie", "Laura", "Julia", "Lea", "Emma", "Hannah",
    "Sarah", "Lisa", "Katharina", "Johanna", "Charlotte", "Clara", "Amelie",
    "Franziska", "Melanie", "Nadine", "Sabine", "Claudia", "Petra", "Andrea",
    "Nicole", "Stefanie", "Christina", "Vanessa", "Jasmin", "Carina", "Theresa",
    # Dutch
    "Sanne", "Iris", "Femke", "Anouk", "Lotte", "Fleur", "Sophie", "Julia",
    "Eva", "Noa", "Sara", "Isa", "Roos", "Maud", "Lieke", "Bo", "Tess", "Nienke",
    "Marije", "Esther", "Ingrid", "Willemijn", "Daphne", "Marloes", "Sanneke",
    # French / broader European
    "Camille", "Chloé", "Manon", "Léa", "Emma", "Louise", "Alice", "Juliette",
    "Elodie", "Céline", "Aurélie", "Nathalie", "Isabelle", "Sylvie", "Sofia",
    "Elena", "Martina", "Valentina", "Giulia", "Francesca", "Alena", "Ewa",
]

FEMALE_LAST_NAMES = [
    # German
    "Müller", "Schmidt", "Schneider", "Fischer", "Weber", "Meyer", "Wagner",
    "Becker", "Hoffmann", "Schäfer", "Koch", "Bauer", "Richter", "Klein",
    "Wolf", "Schröder", "Neumann", "Braun", "Zimmermann", "Krüger", "Hartmann",
    # Dutch
    "de Jong", "Jansen", "de Vries", "van den Berg", "van Dijk", "Bakker",
    "Janssen", "Visser", "Smit", "Meijer", "de Boer", "Mulder", "de Groot",
    "Bos", "Vos", "Peters", "Hendriks", "van Leeuwen", "Dekker", "Brouwer",
    # French / broader
    "Martin", "Bernard", "Dubois", "Laurent", "Moreau", "Girard", "Rousseau",
    "Fontaine", "Rossi", "Ferrari", "Romano", "Kowalski", "Novak", "Andersson",
]


def random_female_name() -> str:
    """Возвращает случайное 'Имя Фамилия'."""
    return f"{random.choice(FEMALE_FIRST_NAMES)} {random.choice(FEMALE_LAST_NAMES)}"


if __name__ == "__main__":
    for _ in range(10):
        print(random_female_name())
