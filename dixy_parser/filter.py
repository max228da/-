# -*- coding: utf-8 -*-
"""Фильтр офферов по минимальной скидке."""
from __future__ import annotations
from typing import Iterable

from .edadeal_client import RawOffer


def by_min_discount(offers: Iterable[RawOffer], min_percent: float) -> list[RawOffer]:
    """Оставляет только офферы с реальным % скидки ≥ min_percent."""
    out: list[RawOffer] = []
    for o in offers:
        d = o.computed_discount()
        if d is not None and d >= min_percent:
            # нормализуем для экспорта
            o.discount_percent = d
            out.append(o)
    return out
