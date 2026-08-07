"""Checksummed, network-specific OurCoin address encoding."""

from base64 import b32decode, b32encode
from binascii import Error as Base64Error
from hmac import compare_digest

from ourcoin.config import TESTNET, NetworkConfig
from ourcoin.crypto import PUBLIC_KEY_HASH_LENGTH, double_sha256, public_key_hash

ADDRESS_CHECKSUM_LENGTH = 4
ADDRESS_SEPARATOR = "1"
ADDRESS_DATA_LENGTH = 1 + PUBLIC_KEY_HASH_LENGTH + ADDRESS_CHECKSUM_LENGTH
ADDRESS_TOKEN_LENGTH = 40


class AddressError(ValueError):
    """Raised when an address is malformed or belongs to another network."""


def _checksum(payload: bytes) -> bytes:
    return double_sha256(payload)[:ADDRESS_CHECKSUM_LENGTH]


def address_from_public_key(
    public_key_bytes: bytes,
    network: NetworkConfig = TESTNET,
) -> str:
    payload = bytes((network.address_version,)) + public_key_hash(public_key_bytes)
    token = b32encode(payload + _checksum(payload)).decode("ascii").lower()
    return f"{network.address_hrp}{ADDRESS_SEPARATOR}{token}"


def decode_address(address: str, network: NetworkConfig = TESTNET) -> bytes:
    """Validate an address and return its 20-byte public-key hash."""

    if type(address) is not str or not address.isascii() or address != address.lower():
        raise AddressError("address must be lowercase ASCII text")

    prefix = f"{network.address_hrp}{ADDRESS_SEPARATOR}"
    if not address.startswith(prefix):
        raise AddressError("address belongs to another network or has an invalid prefix")

    token = address[len(prefix) :]
    if len(token) != ADDRESS_TOKEN_LENGTH:
        raise AddressError("address payload has an invalid length")

    try:
        decoded = b32decode(token.upper(), casefold=False)
    except Base64Error as error:
        raise AddressError("address payload is not valid Base32") from error
    if len(decoded) != ADDRESS_DATA_LENGTH:
        raise AddressError("address payload has an invalid decoded length")

    payload = decoded[:-ADDRESS_CHECKSUM_LENGTH]
    checksum = decoded[-ADDRESS_CHECKSUM_LENGTH:]
    if payload[0] != network.address_version:
        raise AddressError("address version does not match the selected network")
    if not compare_digest(checksum, _checksum(payload)):
        raise AddressError("address checksum does not match")
    return payload[1:]


def is_valid_address(address: str, network: NetworkConfig = TESTNET) -> bool:
    try:
        decode_address(address, network)
    except AddressError:
        return False
    return True
