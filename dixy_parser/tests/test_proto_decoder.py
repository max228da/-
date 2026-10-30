# -*- coding: utf-8 -*-
"""Smoke-test декодера protobuf на синтетически собранном сообщении.

Собираем вручную байты для:
  OffersResponse {
    offers = [
      Offer { id="o1", name="Молоко", price_old={value=100.0}, price_new={value=25.0}, discount=75.0 },
    ],
    total = 1,
  }

и проверяем, что decode_offers_response это правильно разобрал.
"""
from __future__ import annotations

import struct
import unittest

from dixy_parser.proto_decoder import decode_offers_response


def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        if n < 0x80:
            out.append(n)
            return bytes(out)
        out.append((n & 0x7F) | 0x80)
        n >>= 7


def _tag(field_num: int, wire_type: int) -> bytes:
    return _varint((field_num << 3) | wire_type)


def _ld(field_num: int, data: bytes) -> bytes:
    return _tag(field_num, 2) + _varint(len(data)) + data


def _str_field(field_num: int, s: str) -> bytes:
    return _ld(field_num, s.encode("utf-8"))


def _f32_field(field_num: int, value: float) -> bytes:
    return _tag(field_num, 5) + struct.pack("<f", value)


def _f64_field(field_num: int, value: float) -> bytes:
    return _tag(field_num, 1) + struct.pack("<d", value)


def _varint_field(field_num: int, value: int) -> bytes:
    return _tag(field_num, 0) + _varint(value)


def _price_msg(value: float, currency: str = "RUB") -> bytes:
    return _f64_field(1, value) + _str_field(2, currency)


def _offer_msg(oid: str, name: str, po: float, pn: float, disc: float) -> bytes:
    return (
        _str_field(1, oid)
        + _str_field(2, name)
        + _ld(4, _price_msg(po))
        + _ld(5, _price_msg(pn))
        + _f32_field(6, disc)
    )


def _response_msg(offers: list[bytes], total: int) -> bytes:
    out = b""
    for o in offers:
        out += _ld(1, o)
    out += _varint_field(3, total)
    return out


class TestProtoDecoder(unittest.TestCase):
    def test_simple(self):
        blob = _response_msg(
            [_offer_msg("o1", "Молоко", 100.0, 25.0, 75.0)],
            total=1,
        )
        parsed = decode_offers_response(blob)
        self.assertEqual(parsed["total"], 1)
        self.assertEqual(len(parsed["offers"]), 1)

        o = parsed["offers"][0]
        self.assertEqual(o["id"], "o1")
        self.assertEqual(o["name"], "Молоко")
        self.assertAlmostEqual(o["price_old"]["value"], 100.0)
        self.assertAlmostEqual(o["price_new"]["value"], 25.0)
        self.assertAlmostEqual(o["discount"], 75.0, places=3)
        self.assertEqual(o["price_old"]["currency"], "RUB")

    def test_multiple_offers_and_shop_ids(self):
        offer = (
            _str_field(1, "o2")
            + _str_field(2, "Хлеб")
            + _str_field(13, "shop-A")   # shop_ids = repeated string, field 13
            + _str_field(13, "shop-B")
            + _str_field(13, "shop-C")
        )
        blob = _response_msg([offer], total=1)
        parsed = decode_offers_response(blob)
        o = parsed["offers"][0]
        self.assertEqual(o["id"], "o2")
        self.assertEqual(o["shop_ids"], ["shop-A", "shop-B", "shop-C"])


if __name__ == "__main__":
    unittest.main()
