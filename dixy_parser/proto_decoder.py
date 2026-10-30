# -*- coding: utf-8 -*-
"""Декодер protobuf-ответа Edadeal без компиляции .proto.

Если endpoint /web/search/offers вернул application/x-protobuf,
декодируем сырые wire-пары по известной нумерации полей (см. schema.proto).
Это даёт достаточно гибкости — если Edadeal переставит номера полей,
обновляется только карта FIELD_MAP, без перекомпиляции proto.
"""
from __future__ import annotations

from typing import Any


# ---------- низкоуровневый wire-decoder (без зависимостей) ----------

def _read_varint(buf: bytes, pos: int) -> tuple[int, int]:
    shift = 0
    result = 0
    while True:
        if pos >= len(buf):
            raise ValueError("Truncated varint")
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, pos
        shift += 7
        if shift >= 64:
            raise ValueError("Varint too long")


def _read_field(buf: bytes, pos: int) -> tuple[int, int, Any, int]:
    tag, pos = _read_varint(buf, pos)
    field_num = tag >> 3
    wire_type = tag & 0x7
    if wire_type == 0:  # varint
        val, pos = _read_varint(buf, pos)
        return field_num, wire_type, val, pos
    if wire_type == 1:  # 64-bit
        val = int.from_bytes(buf[pos:pos + 8], "little")
        return field_num, wire_type, val, pos + 8
    if wire_type == 2:  # length-delimited
        ln, pos = _read_varint(buf, pos)
        data = buf[pos:pos + ln]
        return field_num, wire_type, data, pos + ln
    if wire_type == 5:  # 32-bit
        val = int.from_bytes(buf[pos:pos + 4], "little")
        return field_num, wire_type, val, pos + 4
    raise ValueError(f"Unsupported wire type {wire_type}")


def _parse_message(buf: bytes) -> dict[int, list[tuple[int, Any]]]:
    """Возвращает {field_num: [(wire_type, raw_value), …]}."""
    out: dict[int, list[tuple[int, Any]]] = {}
    pos = 0
    while pos < len(buf):
        fn, wt, val, pos = _read_field(buf, pos)
        out.setdefault(fn, []).append((wt, val))
    return out


def _as_str(raw: Any) -> str:
    if isinstance(raw, (bytes, bytearray)):
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return raw.decode("utf-8", errors="replace")
    return str(raw)


def _as_float32(raw: Any) -> float:
    if isinstance(raw, int):
        import struct
        return struct.unpack("<f", raw.to_bytes(4, "little"))[0]
    return float(raw)


def _as_float64(raw: Any) -> float:
    if isinstance(raw, int):
        import struct
        return struct.unpack("<d", raw.to_bytes(8, "little"))[0]
    return float(raw)


# ---------- маппинг из schema.proto ----------

def _parse_price(buf: bytes) -> dict:
    """message Price { double value=1; string currency=2; }"""
    m = _parse_message(buf)
    out: dict = {}
    if 1 in m:
        wt, raw = m[1][0]
        out["value"] = _as_float64(raw) if wt == 1 else float(raw)
    if 2 in m:
        out["currency"] = _as_str(m[2][0][1])
    return out


def _parse_quantity(buf: bytes) -> dict:
    """message Quantity { double value=1; string unit=2; }"""
    m = _parse_message(buf)
    out: dict = {}
    if 1 in m:
        wt, raw = m[1][0]
        out["value"] = _as_float64(raw) if wt == 1 else float(raw)
    if 2 in m:
        out["unit"] = _as_str(m[2][0][1])
    return out


def _parse_offer(buf: bytes) -> dict:
    m = _parse_message(buf)
    offer: dict = {}
    if 1 in m:  offer["id"]            = _as_str(m[1][0][1])
    if 2 in m:  offer["name"]          = _as_str(m[2][0][1])
    if 3 in m:  offer["description"]   = _as_str(m[3][0][1])
    if 4 in m:  offer["price_old"]     = _parse_price(m[4][0][1])
    if 5 in m:  offer["price_new"]     = _parse_price(m[5][0][1])
    if 6 in m:
        wt, raw = m[6][0]
        offer["discount"] = _as_float32(raw) if wt == 5 else float(raw)
    if 7 in m:  offer["image_url"]     = _as_str(m[7][0][1])
    if 8 in m:  offer["start_date"]    = _as_str(m[8][0][1])
    if 9 in m:  offer["end_date"]      = _as_str(m[9][0][1])
    if 10 in m: offer["retailer_slug"] = _as_str(m[10][0][1])
    if 11 in m: offer["segment"]       = _as_str(m[11][0][1])
    if 12 in m: offer["segment_name"]  = _as_str(m[12][0][1])
    if 13 in m: offer["shop_ids"]      = [_as_str(v[1]) for v in m[13]]
    if 14 in m: offer["url"]           = _as_str(m[14][0][1])
    if 15 in m: offer["quantity"]      = _parse_quantity(m[15][0][1])
    if 16 in m: offer["is_exclusive"]  = bool(m[16][0][1])
    return offer


def decode_offers_response(blob: bytes) -> dict:
    """message OffersResponse { Offer offers=1; PageInfo page=2; int32 total=3; }"""
    m = _parse_message(blob)
    offers = [_parse_offer(v[1]) for v in m.get(1, [])]
    total = int(m[3][0][1]) if 3 in m else len(offers)
    page_info = {}
    if 2 in m:
        pm = _parse_message(m[2][0][1])
        page_info = {
            "count":       int(pm[1][0][1]) if 1 in pm else None,
            "page":        int(pm[2][0][1]) if 2 in pm else None,
            "total_pages": int(pm[3][0][1]) if 3 in pm else None,
        }
    return {"offers": offers, "page": page_info, "total": total}
