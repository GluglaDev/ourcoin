"""Tests for canonical encoding primitives and vectors."""

import json
from pathlib import Path
from typing import Any

import pytest

from ourcoin.encoding import (
    CanonicalReader,
    EncodingError,
    encode_bytes,
    encode_sequence,
    encode_text,
    encode_u8,
    encode_u16,
    encode_u32,
    encode_u64,
)

VECTORS = Path(__file__).parents[1] / "vectors" / "encoding.json"
ENCODERS = {"u8": encode_u8, "u16": encode_u16, "u32": encode_u32, "u64": encode_u64}


def _load_vectors() -> dict[str, Any]:
    with VECTORS.open(encoding="utf-8") as handle:
        return json.load(handle)


def test_unsigned_vectors() -> None:
    for vector in _load_vectors()["unsigned"]:
        encoded = ENCODERS[vector["kind"]](vector["value"])
        assert encoded.hex() == vector["hex"]


def test_bytes_text_and_sequence_vectors() -> None:
    vectors = _load_vectors()
    for vector in vectors["bytes"]:
        assert encode_bytes(bytes.fromhex(vector["value_hex"])).hex() == vector["hex"]
    for vector in vectors["text"]:
        assert encode_text(vector["value"]).hex() == vector["hex"]
    sequence = vectors["sequence"]
    values = [bytes.fromhex(value) for value in sequence["values_hex"]]
    assert encode_sequence(values).hex() == sequence["hex"]


def test_reader_round_trip() -> None:
    encoded = encode_u8(7) + encode_u16(500) + encode_u32(100_000) + encode_u64(2**63)
    encoded += encode_text("OurCoin") + encode_sequence([b"a", b"bc"])
    reader = CanonicalReader(encoded)

    assert reader.read_u8() == 7
    assert reader.read_u16() == 500
    assert reader.read_u32() == 100_000
    assert reader.read_u64() == 2**63
    assert reader.read_text(max_length=32) == "OurCoin"
    assert reader.read_sequence(max_items=2, max_item_length=2) == (b"a", b"bc")
    reader.ensure_finished()


@pytest.mark.parametrize("value", [-1, 256, True, 1.5])
def test_u8_rejects_noncanonical_values(value: object) -> None:
    with pytest.raises(EncodingError):
        encode_u8(value)  # type: ignore[arg-type]


def test_reader_rejects_truncation_limits_and_trailing_data() -> None:
    with pytest.raises(EncodingError, match="truncated"):
        CanonicalReader(b"\x00\x00").read_u32()
    with pytest.raises(EncodingError, match="allowed length"):
        CanonicalReader(encode_bytes(b"toolong")).read_bytes(max_length=3)

    reader = CanonicalReader(encode_u8(1) + b"extra")
    assert reader.read_u8() == 1
    with pytest.raises(EncodingError, match="trailing"):
        reader.ensure_finished()


def test_reader_rejects_invalid_utf8_and_non_nfc_text() -> None:
    with pytest.raises(EncodingError, match="UTF-8"):
        CanonicalReader(encode_bytes(b"\xff")).read_text()

    decomposed = "e\u0301".encode()
    with pytest.raises(EncodingError, match="NFC"):
        CanonicalReader(encode_bytes(decomposed)).read_text()
