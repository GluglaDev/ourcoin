"""Small cryptographic API for hashing and Ed25519 signatures."""

from hashlib import sha256

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

PRIVATE_KEY_LENGTH = 32
PUBLIC_KEY_LENGTH = 32
SIGNATURE_LENGTH = 64
PUBLIC_KEY_HASH_LENGTH = 20


class CryptoError(ValueError):
    """Raised when key material has an invalid representation."""


def _require_bytes(value: bytes, expected_length: int, name: str) -> None:
    if type(value) is not bytes or len(value) != expected_length:
        raise CryptoError(f"{name} must be exactly {expected_length} bytes")


def sha256_digest(data: bytes) -> bytes:
    if type(data) is not bytes:
        raise CryptoError("hash input must be bytes")
    return sha256(data).digest()


def double_sha256(data: bytes) -> bytes:
    return sha256_digest(sha256_digest(data))


def generate_private_key() -> bytes:
    """Generate and return a raw 32-byte Ed25519 private seed."""

    private_key = Ed25519PrivateKey.generate()
    return private_key.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )


def public_key_from_private(private_key_bytes: bytes) -> bytes:
    _require_bytes(private_key_bytes, PRIVATE_KEY_LENGTH, "private key")
    private_key = Ed25519PrivateKey.from_private_bytes(private_key_bytes)
    return private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )


def sign(private_key_bytes: bytes, message: bytes) -> bytes:
    _require_bytes(private_key_bytes, PRIVATE_KEY_LENGTH, "private key")
    if type(message) is not bytes:
        raise CryptoError("message must be bytes")
    private_key = Ed25519PrivateKey.from_private_bytes(private_key_bytes)
    return private_key.sign(message)


def verify_signature(public_key_bytes: bytes, message: bytes, signature: bytes) -> bool:
    """Return False for every malformed key, message or signature."""

    if (
        type(public_key_bytes) is not bytes
        or len(public_key_bytes) != PUBLIC_KEY_LENGTH
        or type(message) is not bytes
        or type(signature) is not bytes
        or len(signature) != SIGNATURE_LENGTH
    ):
        return False
    try:
        public_key = Ed25519PublicKey.from_public_bytes(public_key_bytes)
        public_key.verify(signature, message)
    except (InvalidSignature, ValueError):
        return False
    return True


def public_key_hash(public_key_bytes: bytes) -> bytes:
    _require_bytes(public_key_bytes, PUBLIC_KEY_LENGTH, "public key")
    return sha256_digest(public_key_bytes)[:PUBLIC_KEY_HASH_LENGTH]
