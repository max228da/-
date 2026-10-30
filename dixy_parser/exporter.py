# -*- coding: utf-8 -*-
"""Экспорт офферов в JSON и CSV."""
from __future__ import annotations

import csv
import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Iterable

from .edadeal_client import RawOffer


def _timestamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def to_json(offers: Iterable[RawOffer], out_dir: Path, label: str = "dixy") -> Path:
    path = out_dir / f"{label}_{_timestamp()}.json"
    data = [asdict(o) for o in offers]
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def to_csv(offers: Iterable[RawOffer], out_dir: Path, label: str = "dixy") -> Path:
    path = out_dir / f"{label}_{_timestamp()}.csv"
    offers = list(offers)
    fields = [
        "offer_id", "name", "segment_slug", "segment_name",
        "price_old", "price_new", "discount_percent", "currency",
        "quantity_value", "quantity_unit",
        "start_date", "end_date", "image_url", "url",
        "shop_ids_count", "retailer_slug", "source_format",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for o in offers:
            row = asdict(o)
            row["shop_ids_count"] = len(o.shop_ids)
            row.pop("shop_ids", None)
            row.pop("description", None)
            w.writerow({k: row.get(k, "") for k in fields})
    return path
