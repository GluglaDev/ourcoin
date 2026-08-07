"""Canonical, signed OurCoin account transactions."""

from dataclasses import dataclass, replace

from ourcoin.address import AddressError, address_from_public_key, decode_address
from ourcoin.config import TESTNET, NetworkConfig
from ourcoin.crypto import (
    PUBLIC_KEY_LENGTH,
    SIGNATURE_LENGTH,
    CryptoError,
    public_key_from_private,
    sha256_digest,
    sign,
    verify_signature,
)
from ourcoin.encoding import (
    U64_MAX,
    CanonicalReader,
    EncodingError,
    encode_bytes,
    encode_text,
    encode_u16,
    encode_u64,
)

TRANSACTION_DOMAIN = b"OURCOIN:TRANSACTION:V1"
TRANSACTION_VERSION = 1
MAX_CHAIN_ID_BYTES = 64
MAX_ADDRESS_BYTES = 128


class TransactionError(ValueError):
    """Raised when a transaction is malformed or fails intrinsic validation."""


@dataclass(frozen=True, slots=True)
class Transaction:
    """An immutable signed transfer between two account addresses."""

    version: int
    chain_id: str
    sender_public_key: bytes
    sender_address: str
    recipient_address: str
    amount_atoms: int
    fee_atoms: int
    nonce: int
    valid_until_height: int
    signature: bytes

    def signing_bytes(self) -> bytes:
        """Return the exact domain-separated bytes covered by the signature."""

        return b"".join(
            (
                encode_bytes(TRANSACTION_DOMAIN),
                encode_u16(self.version),
                encode_text(self.chain_id),
                encode_bytes(self.sender_public_key),
                encode_text(self.sender_address),
                encode_text(self.recipient_address),
                encode_u64(self.amount_atoms),
                encode_u64(self.fee_atoms),
                encode_u64(self.nonce),
                encode_u64(self.valid_until_height),
            )
        )

    def to_bytes(self) -> bytes:
        return self.signing_bytes() + encode_bytes(self.signature)

    @property
    def txid(self) -> bytes:
        return sha256_digest(self.to_bytes())

    @property
    def txid_hex(self) -> str:
        return self.txid.hex()

    @classmethod
    def from_bytes(cls, encoded: bytes) -> "Transaction":
        """Decode one canonical transaction without applying state-dependent rules."""

        try:
            reader = CanonicalReader(encoded)
            domain = reader.read_bytes(max_length=len(TRANSACTION_DOMAIN))
            if domain != TRANSACTION_DOMAIN:
                raise TransactionError("unknown transaction domain")
            transaction = cls(
                version=reader.read_u16(),
                chain_id=reader.read_text(max_length=MAX_CHAIN_ID_BYTES),
                sender_public_key=reader.read_bytes(max_length=PUBLIC_KEY_LENGTH),
                sender_address=reader.read_text(max_length=MAX_ADDRESS_BYTES),
                recipient_address=reader.read_text(max_length=MAX_ADDRESS_BYTES),
                amount_atoms=reader.read_u64(),
                fee_atoms=reader.read_u64(),
                nonce=reader.read_u64(),
                valid_until_height=reader.read_u64(),
                signature=reader.read_bytes(max_length=SIGNATURE_LENGTH),
            )
            reader.ensure_finished()
        except EncodingError as error:
            raise TransactionError("invalid canonical transaction encoding") from error
        return transaction


def _validate_u64(value: int, field_name: str) -> None:
    if type(value) is not int or not 0 <= value <= U64_MAX:
        raise TransactionError(f"{field_name} must be an unsigned 64-bit integer")


def validate_transaction(
    transaction: Transaction,
    *,
    current_height: int,
    network: NetworkConfig = TESTNET,
) -> None:
    """Validate canonical fields, network binding, addresses and signature."""

    if not isinstance(transaction, Transaction):
        raise TransactionError("value is not a Transaction")
    _validate_u64(current_height, "current height")
    if type(transaction.version) is not int or transaction.version != TRANSACTION_VERSION:
        raise TransactionError("unsupported transaction version")
    if transaction.chain_id != network.chain_id:
        raise TransactionError("transaction belongs to another network")
    if (
        type(transaction.sender_public_key) is not bytes
        or len(transaction.sender_public_key) != PUBLIC_KEY_LENGTH
    ):
        raise TransactionError("sender public key must be exactly 32 bytes")
    if type(transaction.signature) is not bytes or len(transaction.signature) != SIGNATURE_LENGTH:
        raise TransactionError("transaction signature must be exactly 64 bytes")

    _validate_u64(transaction.amount_atoms, "amount")
    _validate_u64(transaction.fee_atoms, "fee")
    _validate_u64(transaction.nonce, "nonce")
    _validate_u64(transaction.valid_until_height, "valid-until height")
    if transaction.amount_atoms == 0:
        raise TransactionError("transaction amount must be positive")
    if transaction.amount_atoms + transaction.fee_atoms > U64_MAX:
        raise TransactionError("transaction amount plus fee exceeds uint64")
    if current_height > transaction.valid_until_height:
        raise TransactionError("transaction has expired")

    try:
        decode_address(transaction.sender_address, network)
        decode_address(transaction.recipient_address, network)
        expected_sender = address_from_public_key(transaction.sender_public_key, network)
    except (AddressError, CryptoError) as error:
        raise TransactionError("transaction contains an invalid network address") from error
    if transaction.sender_address != expected_sender:
        raise TransactionError("sender address does not match the public key")

    try:
        signing_bytes = transaction.signing_bytes()
        transaction.to_bytes()
    except EncodingError as error:
        raise TransactionError("transaction fields are not canonically encodable") from error
    if not verify_signature(transaction.sender_public_key, signing_bytes, transaction.signature):
        raise TransactionError("transaction signature is invalid")


def create_signed_transaction(
    private_key_bytes: bytes,
    *,
    recipient_address: str,
    amount_atoms: int,
    fee_atoms: int,
    nonce: int,
    valid_until_height: int,
    network: NetworkConfig = TESTNET,
) -> Transaction:
    """Create a transaction while keeping private material outside the result."""

    try:
        sender_public_key = public_key_from_private(private_key_bytes)
        sender_address = address_from_public_key(sender_public_key, network)
        unsigned = Transaction(
            version=TRANSACTION_VERSION,
            chain_id=network.chain_id,
            sender_public_key=sender_public_key,
            sender_address=sender_address,
            recipient_address=recipient_address,
            amount_atoms=amount_atoms,
            fee_atoms=fee_atoms,
            nonce=nonce,
            valid_until_height=valid_until_height,
            signature=b"",
        )
        transaction = replace(unsigned, signature=sign(private_key_bytes, unsigned.signing_bytes()))
    except (CryptoError, EncodingError) as error:
        raise TransactionError("could not create a canonical signed transaction") from error
    validate_transaction(transaction, current_height=0, network=network)
    return transaction
