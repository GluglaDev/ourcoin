"""Canonical block, header and reward-transaction structures."""

from dataclasses import dataclass, replace

from ourcoin.crypto import sha256_digest
from ourcoin.encoding import (
    CanonicalReader,
    EncodingError,
    encode_bytes,
    encode_text,
    encode_u16,
    encode_u64,
)
from ourcoin.merkle import merkle_root
from ourcoin.transaction import Transaction

BLOCK_HEADER_DOMAIN = b"OURCOIN:BLOCK:HEADER:V1"
REWARD_TRANSACTION_DOMAIN = b"OURCOIN:REWARD:V1"
BLOCK_VERSION = 1
REWARD_TRANSACTION_VERSION = 1
HASH_LENGTH = 32
TARGET_LENGTH = 32
MAX_TARGET = (1 << 256) - 1
MAX_CHAIN_ID_BYTES = 64
MAX_ADDRESS_BYTES = 128


class BlockEncodingError(ValueError):
    """Raised when a block field has no canonical representation."""


def _exact_hash(value: bytes, name: str) -> bytes:
    if type(value) is not bytes or len(value) != HASH_LENGTH:
        raise BlockEncodingError(f"{name} must be exactly 32 bytes")
    return value


def encode_target(target: int) -> bytes:
    if type(target) is not int or not 0 < target <= MAX_TARGET:
        raise BlockEncodingError("difficulty target must be between 1 and 2^256 - 1")
    return target.to_bytes(TARGET_LENGTH, "big")


@dataclass(frozen=True, slots=True)
class RewardTransaction:
    version: int
    chain_id: str
    height: int
    miner_address: str
    amount_atoms: int

    def to_bytes(self) -> bytes:
        return b"".join(
            (
                encode_bytes(REWARD_TRANSACTION_DOMAIN),
                encode_u16(self.version),
                encode_text(self.chain_id),
                encode_u64(self.height),
                encode_text(self.miner_address),
                encode_u64(self.amount_atoms),
            )
        )

    @classmethod
    def from_bytes(cls, encoded: bytes) -> "RewardTransaction":
        """Decode one reward transaction from its exact canonical bytes."""

        try:
            reader = CanonicalReader(encoded)
            domain = reader.read_bytes(max_length=len(REWARD_TRANSACTION_DOMAIN))
            if domain != REWARD_TRANSACTION_DOMAIN:
                raise BlockEncodingError("unknown reward transaction domain")
            reward = cls(
                version=reader.read_u16(),
                chain_id=reader.read_text(max_length=MAX_CHAIN_ID_BYTES),
                height=reader.read_u64(),
                miner_address=reader.read_text(max_length=MAX_ADDRESS_BYTES),
                amount_atoms=reader.read_u64(),
            )
            reader.ensure_finished()
        except EncodingError as error:
            raise BlockEncodingError("invalid canonical reward transaction encoding") from error
        return reward

    @property
    def txid(self) -> bytes:
        return sha256_digest(self.to_bytes())


@dataclass(frozen=True, slots=True)
class BlockHeader:
    version: int
    chain_id: str
    height: int
    previous_block_hash: bytes
    transactions_root: bytes
    state_root: bytes
    timestamp: int
    difficulty_target: int
    nonce: int
    miner_address: str

    def to_bytes(self) -> bytes:
        return b"".join(
            (
                encode_bytes(BLOCK_HEADER_DOMAIN),
                encode_u16(self.version),
                encode_text(self.chain_id),
                encode_u64(self.height),
                encode_bytes(_exact_hash(self.previous_block_hash, "previous block hash")),
                encode_bytes(_exact_hash(self.transactions_root, "transactions root")),
                encode_bytes(_exact_hash(self.state_root, "state root")),
                encode_u64(self.timestamp),
                encode_bytes(encode_target(self.difficulty_target)),
                encode_u64(self.nonce),
                encode_text(self.miner_address),
            )
        )

    @classmethod
    def from_bytes(cls, encoded: bytes) -> "BlockHeader":
        """Decode one header from its exact canonical bytes."""

        try:
            reader = CanonicalReader(encoded)
            domain = reader.read_bytes(max_length=len(BLOCK_HEADER_DOMAIN))
            if domain != BLOCK_HEADER_DOMAIN:
                raise BlockEncodingError("unknown block header domain")
            version = reader.read_u16()
            chain_id = reader.read_text(max_length=MAX_CHAIN_ID_BYTES)
            height = reader.read_u64()
            previous_block_hash = reader.read_bytes(max_length=HASH_LENGTH)
            transactions_root_value = reader.read_bytes(max_length=HASH_LENGTH)
            state_root = reader.read_bytes(max_length=HASH_LENGTH)
            timestamp = reader.read_u64()
            target_bytes = reader.read_bytes(max_length=TARGET_LENGTH)
            header = cls(
                version=version,
                chain_id=chain_id,
                height=height,
                previous_block_hash=previous_block_hash,
                transactions_root=transactions_root_value,
                state_root=state_root,
                timestamp=timestamp,
                difficulty_target=int.from_bytes(target_bytes, "big"),
                nonce=reader.read_u64(),
                miner_address=reader.read_text(max_length=MAX_ADDRESS_BYTES),
            )
            reader.ensure_finished()
            _exact_hash(header.previous_block_hash, "previous block hash")
            _exact_hash(header.transactions_root, "transactions root")
            _exact_hash(header.state_root, "state root")
            if len(target_bytes) != TARGET_LENGTH:
                raise BlockEncodingError("difficulty target must be exactly 32 bytes")
            encode_target(header.difficulty_target)
        except EncodingError as error:
            raise BlockEncodingError("invalid canonical block header encoding") from error
        return header

    @property
    def block_hash(self) -> bytes:
        return sha256_digest(self.to_bytes())

    @property
    def block_hash_hex(self) -> str:
        return self.block_hash.hex()


@dataclass(frozen=True, slots=True)
class Block:
    header: BlockHeader
    reward_transaction: RewardTransaction
    transactions: tuple[Transaction, ...]

    @property
    def block_hash(self) -> bytes:
        return self.header.block_hash

    @property
    def block_hash_hex(self) -> str:
        return self.header.block_hash_hex

    def with_nonce(self, nonce: int) -> "Block":
        return replace(self, header=replace(self.header, nonce=nonce))


def transactions_root(
    reward_transaction: RewardTransaction,
    transactions: tuple[Transaction, ...],
) -> bytes:
    if not isinstance(reward_transaction, RewardTransaction):
        raise BlockEncodingError("block reward must be a RewardTransaction")
    if type(transactions) is not tuple or any(
        not isinstance(transaction, Transaction) for transaction in transactions
    ):
        raise BlockEncodingError("block transactions must be a tuple of Transaction values")
    transaction_ids = (reward_transaction.txid, *(tx.txid for tx in transactions))
    return merkle_root(transaction_ids)


def hash_meets_target(block_hash: bytes, target: int) -> bool:
    if type(block_hash) is not bytes or len(block_hash) != HASH_LENGTH:
        return False
    try:
        encode_target(target)
    except BlockEncodingError:
        return False
    return int.from_bytes(block_hash, "big") <= target
