"""Tests for SHA-256 and Ed25519 wrappers."""

import json
from pathlib import Path
from typing import Any

import pytest

from ourcoin.crypto import (
    CryptoError,
    generate_private_key,
    public_key_from_private,
    sha256_digest,
    sign,
    verify_signature,
)

VECTOR = Path(__file__).parents[1] / "vectors" / "ed25519.json"


def _load_vector() -> dict[str, Any]:
    with VECTOR.open(encoding="utf-8") as handle:
        return json.load(handle)


def test_sha256_known_value() -> None:
    assert sha256_digest(b"abc").hex() == (
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    )


def test_rfc_8032_ed25519_vector() -> None:
    vector = _load_vector()
    public_key = bytes.fromhex(vector["public_key_hex"])
    message = bytes.fromhex(vector["message_hex"])
    signature = bytes.fromhex(vector["signature_hex"])

    assert verify_signature(public_key, message, signature)


def test_generated_key_can_sign_and_verify() -> None:
    private_key = generate_private_key()
    public_key = public_key_from_private(private_key)
    message = b"ourcoin-m1"
    signature = sign(private_key, message)

    assert len(private_key) == 32
    assert len(public_key) == 32
    assert len(signature) == 64
    assert verify_signature(public_key, message, signature)
    assert not verify_signature(public_key, message + b"!", signature)


@pytest.mark.parametrize("private_key", [b"", b"x" * 31, b"x" * 33])
def test_private_key_length_is_strict(private_key: bytes) -> None:
    with pytest.raises(CryptoError, match="32 bytes"):
        public_key_from_private(private_key)


def test_verifier_rejects_malformed_inputs() -> None:
    assert not verify_signature(b"short", b"message", b"x" * 64)
    assert not verify_signature(b"x" * 32, b"message", b"short")
    assert not verify_signature(b"x" * 32, b"message", b"x" * 64)
