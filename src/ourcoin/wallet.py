"""In-memory Ed25519 wallet with authenticated encrypted file storage."""

import json
import os
import re
import tempfile
from base64 import b64decode, b64encode
from binascii import Error as Base64Error
from pathlib import Path
from typing import Any
from unicodedata import normalize

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from ourcoin.address import address_from_public_key
from ourcoin.config import TESTNET, NetworkConfig
from ourcoin.crypto import (
    PRIVATE_KEY_LENGTH,
    generate_private_key,
    public_key_from_private,
)
from ourcoin.encoding import encode_bytes, encode_text
from ourcoin.transaction import Transaction, create_signed_transaction

WALLET_FORMAT = "ourcoin-wallet-v1"
WALLET_AAD_DOMAIN = b"OURCOIN:WALLET:V1"
MAX_WALLET_FILE_BYTES = 65_536
SCRYPT_N = 1 << 14
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_SALT_LENGTH = 16
AES_KEY_LENGTH = 32
AES_NONCE_LENGTH = 12
WALLET_NAME_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")


class WalletError(ValueError):
    """Raised when wallet input or encrypted storage is invalid."""


def _validate_name(name: str) -> None:
    if type(name) is not str or WALLET_NAME_PATTERN.fullmatch(name) is None:
        raise WalletError("wallet name must use 1-64 ASCII letters, digits, '_' or '-'")


def _password_bytes(password: str) -> bytes:
    if type(password) is not str or not password:
        raise WalletError("wallet password must not be empty")
    return normalize("NFC", password).encode("utf-8")


def _derive_key(password: str, salt: bytes) -> bytes:
    return Scrypt(
        salt=salt,
        length=AES_KEY_LENGTH,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
    ).derive(_password_bytes(password))


def _wallet_aad(name: str, chain_id: str, address: str, public_key: bytes) -> bytes:
    return b"".join(
        (
            encode_bytes(WALLET_AAD_DOMAIN),
            encode_text(name),
            encode_text(chain_id),
            encode_text(address),
            encode_bytes(public_key),
        )
    )


def _decode_base64(value: Any, name: str, expected_length: int | None = None) -> bytes:
    if type(value) is not str or not value.isascii():
        raise WalletError(f"{name} must be Base64 ASCII text")
    try:
        decoded = b64decode(value, validate=True)
    except (Base64Error, ValueError) as error:
        raise WalletError(f"{name} is not valid Base64") from error
    if expected_length is not None and len(decoded) != expected_length:
        raise WalletError(f"{name} has an invalid length")
    return decoded


class Wallet:
    """A wallet whose private bytes are intentionally not exposed as a property."""

    __slots__ = ("_private_key_bytes", "_network", "address", "name", "public_key")

    def __init__(
        self,
        name: str,
        private_key_bytes: bytes,
        *,
        network: NetworkConfig = TESTNET,
    ) -> None:
        _validate_name(name)
        if type(private_key_bytes) is not bytes or len(private_key_bytes) != PRIVATE_KEY_LENGTH:
            raise WalletError("private key must be exactly 32 bytes")
        self.name = name
        self._network = network
        self._private_key_bytes = private_key_bytes
        self.public_key = public_key_from_private(private_key_bytes)
        self.address = address_from_public_key(self.public_key, network)

    def __repr__(self) -> str:
        return f"Wallet(name={self.name!r}, address={self.address!r})"

    @classmethod
    def create(cls, name: str, *, network: NetworkConfig = TESTNET) -> "Wallet":
        return cls(name, generate_private_key(), network=network)

    @property
    def network(self) -> NetworkConfig:
        return self._network

    def create_transaction(
        self,
        *,
        recipient_address: str,
        amount_atoms: int,
        fee_atoms: int,
        nonce: int,
        valid_until_height: int,
    ) -> Transaction:
        return create_signed_transaction(
            self._private_key_bytes,
            recipient_address=recipient_address,
            amount_atoms=amount_atoms,
            fee_atoms=fee_atoms,
            nonce=nonce,
            valid_until_height=valid_until_height,
            network=self._network,
        )

    def save_encrypted(
        self,
        path: str | Path,
        password: str,
        *,
        overwrite: bool = False,
    ) -> Path:
        destination = Path(path)
        if destination.exists() and not overwrite:
            raise WalletError("wallet file already exists")
        destination.parent.mkdir(parents=True, exist_ok=True)

        salt = os.urandom(SCRYPT_SALT_LENGTH)
        nonce = os.urandom(AES_NONCE_LENGTH)
        aad = _wallet_aad(self.name, self._network.chain_id, self.address, self.public_key)
        ciphertext = AESGCM(_derive_key(password, salt)).encrypt(
            nonce,
            self._private_key_bytes,
            aad,
        )
        document = {
            "format": WALLET_FORMAT,
            "name": self.name,
            "chain_id": self._network.chain_id,
            "address": self.address,
            "public_key_hex": self.public_key.hex(),
            "kdf": {
                "name": "scrypt",
                "salt_base64": b64encode(salt).decode("ascii"),
                "n": SCRYPT_N,
                "r": SCRYPT_R,
                "p": SCRYPT_P,
            },
            "cipher": {
                "name": "aes-256-gcm",
                "nonce_base64": b64encode(nonce).decode("ascii"),
                "ciphertext_base64": b64encode(ciphertext).decode("ascii"),
            },
        }

        file_descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.name}.",
            suffix=".tmp",
            dir=destination.parent,
            text=True,
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(file_descriptor, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(document, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            if overwrite:
                os.replace(temporary, destination)
            else:
                try:
                    os.link(temporary, destination)
                except FileExistsError as error:
                    raise WalletError("wallet file already exists") from error
        finally:
            if temporary.exists():
                temporary.unlink()
        return destination

    @classmethod
    def load_encrypted(
        cls,
        path: str | Path,
        password: str,
        *,
        network: NetworkConfig = TESTNET,
    ) -> "Wallet":
        source = Path(path)
        try:
            with source.open("rb") as handle:
                encoded_document = handle.read(MAX_WALLET_FILE_BYTES + 1)
            if len(encoded_document) > MAX_WALLET_FILE_BYTES:
                raise WalletError("wallet file exceeds its size limit")
            raw_document = encoded_document.decode("utf-8")
            document = json.loads(raw_document)
        except WalletError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise WalletError("wallet file cannot be read as valid JSON") from error
        if not isinstance(document, dict):
            raise WalletError("wallet document must be a JSON object")

        try:
            wallet_format = document["format"]
            name = document["name"]
            chain_id = document["chain_id"]
            address = document["address"]
            public_key_hex = document["public_key_hex"]
            kdf = document["kdf"]
            cipher = document["cipher"]
        except KeyError as error:
            raise WalletError("wallet document is missing a required field") from error
        if wallet_format != WALLET_FORMAT:
            raise WalletError("unsupported wallet format")
        _validate_name(name)
        if chain_id != network.chain_id:
            raise WalletError("wallet belongs to another network")
        if type(address) is not str or type(public_key_hex) is not str:
            raise WalletError("wallet public metadata has invalid types")
        try:
            public_key = bytes.fromhex(public_key_hex)
        except ValueError as error:
            raise WalletError("wallet public key is not valid hexadecimal") from error
        if not isinstance(kdf, dict) or not isinstance(cipher, dict):
            raise WalletError("wallet cryptographic metadata must be objects")
        if (
            kdf.get("name") != "scrypt"
            or kdf.get("n") != SCRYPT_N
            or kdf.get("r") != SCRYPT_R
            or kdf.get("p") != SCRYPT_P
        ):
            raise WalletError("wallet uses unsupported KDF parameters")
        if cipher.get("name") != "aes-256-gcm":
            raise WalletError("wallet uses an unsupported cipher")

        salt = _decode_base64(kdf.get("salt_base64"), "wallet salt", SCRYPT_SALT_LENGTH)
        nonce = _decode_base64(
            cipher.get("nonce_base64"),
            "wallet cipher nonce",
            AES_NONCE_LENGTH,
        )
        ciphertext = _decode_base64(
            cipher.get("ciphertext_base64"),
            "wallet ciphertext",
        )
        aad = _wallet_aad(name, chain_id, address, public_key)
        try:
            private_key = AESGCM(_derive_key(password, salt)).decrypt(nonce, ciphertext, aad)
        except InvalidTag as error:
            raise WalletError("wallet password is invalid or the file was modified") from error
        wallet = cls(name, private_key, network=network)
        if wallet.address != address or wallet.public_key != public_key:
            raise WalletError("wallet public metadata does not match the private key")
        return wallet
