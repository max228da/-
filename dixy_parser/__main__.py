# -*- coding: utf-8 -*-
"""CLI: python -m dixy_parser  [--min-discount 70] [--format json|csv|both]."""
from __future__ import annotations

import argparse
import logging
import sys

from . import config as cfg
from .edadeal_client import EdadealClient, AuthRequired
from .filter import by_min_discount
from .exporter import to_json, to_csv


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="dixy_parser",
        description="Парсер акций Дикси со скидкой ≥ X% через Edadeal.",
    )
    ap.add_argument("--min-discount", type=float, default=cfg.MIN_DISCOUNT_PERCENT,
                    help=f"Минимальный %% скидки (по умолчанию {cfg.MIN_DISCOUNT_PERCENT}).")
    ap.add_argument("--locality", default=cfg.LOCALITY,
                    help=f"Слуг города (по умолчанию {cfg.LOCALITY}).")
    ap.add_argument("--format", choices=["json", "csv", "both"], default="both")
    ap.add_argument("--proxy", default=cfg.PROXY,
                    help="HTTP/SOCKS прокси (например http://user:pass@host:port).")
    ap.add_argument("--cookies-file", default=str(cfg.COOKIES_FILE),
                    help="Netscape cookies.txt с куки Yandex-аккаунта.")
    ap.add_argument("--verbose", "-v", action="count", default=0)
    args = ap.parse_args()

    level = logging.DEBUG if args.verbose >= 2 else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    client = EdadealClient(
        locality=args.locality,
        proxy=args.proxy,
        cookies_file=args.cookies_file,
    )

    try:
        all_offers = list(client.iter_all())
    except AuthRequired as e:
        print(f"\n[!] Авторизация не удалась: {e}", file=sys.stderr)
        print("    Проверь что yandex_cookies.txt свежий (Session_id не "
              "старше суток) и что IP прокси российский.", file=sys.stderr)
        return 2

    print(f"[+] Получено офферов (всего): {len(all_offers)}")

    hits = by_min_discount(all_offers, args.min_discount)
    print(f"[+] Из них со скидкой ≥ {args.min_discount}%: {len(hits)}")

    if args.format in ("json", "both"):
        p = to_json(hits, cfg.OUTPUT_DIR, label=f"dixy_discount{int(args.min_discount)}")
        print(f"[+] JSON: {p}")
    if args.format in ("csv", "both"):
        p = to_csv(hits, cfg.OUTPUT_DIR, label=f"dixy_discount{int(args.min_discount)}")
        print(f"[+] CSV:  {p}")

    return 0 if hits else 1


if __name__ == "__main__":
    raise SystemExit(main())
