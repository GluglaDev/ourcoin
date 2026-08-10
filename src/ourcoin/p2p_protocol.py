"""Bounded canonical wire messages for the OurCoin testnet P2P protocol."""

import asyncio
import struct
from dataclasses import dataclass
from enum import IntEnum

from ourcoin.block import Block, BlockEncodingError, BlockHeader, RewardTransaction
from ourcoin.crypto import sha256_digest
from ourcoin.encoding import (
    U64_MAX,
    CanonicalReader,
    EncodingError,
    encode_bytes,
    encode_sequence,
    encode_text,
    encode_u16,
    encode_u64,
)
from ourcoin.transaction import Transaction, TransactionError

NETWORK_MAGIC = b"OURP"
PROTOCOL_VERSION = 1
FRAME_PREFIX = struct.Struct(">4sHBI4s")
MAX_FRAME_PAYLOAD = 8 * 1024 * 1024
MAX_BLOCK_TRANSACTIONS_WIRE = 10_000
MAX_TRANSACTION_BYTES = 4_096
MAX_BLOCK_LOCATOR_HASHES = 64
MAX_CHAIN_ID_BYTES = 64
NODE_ID_LENGTH = 16
HASH_LENGTH = 32
WORK_LENGTH = 40

HELLO_DOMAIN = b"OURCOIN:P2P:HELLO:V1"
LOCATOR_DOMAIN = b"OURCOIN:P2P:LOCATOR:V1"
SUMMARY_DOMAIN = b"OURCOIN:P2P:SUMMARY:V1"
BLOCK_BODY_DOMAIN = b"OURCOIN:P2P:BLOCK:V1"
REJECT_DOMAIN = b"OURCOIN:P2P:REJECT:V1"


class ProtocolError(ValueError):
    """Raised when untrusted wire data violates the framed protocol."""


class UnsupportedProtocolVersionError(ProtocolError):
    """Raised when a peer uses an unknown wire protocol version."""


class PeerDisconnectedError(ProtocolError):
    """Raised when a peer closes before a complete frame arrives."""


class PeerFrameTimeoutError(ProtocolError):
    """Raised when a peer does not complete a frame before its deadline."""


class MessageType(IntEnum):
    HELLO = 1
    GET_BLOCKS = 2
    BLOCK = 3
    TRANSACTION = 4
    SYNC_COMPLETE = 5
    PING = 6
    PONG = 7
    REJECT = 8


@dataclass(frozen=True, slots=True)
class Frame:
    message_type: MessageType
    payload: bytes


def _encode_work(work: int) -> bytes:
    if type(work) is not int or work <= 0 or work.bit_length() > WORK_LENGTH * 8:
        raise ProtocolError("cumulative work is outside the wire range")
    return work.to_bytes(WORK_LENGTH, "big")


def _decode_work(value: bytes) -> int:
    if len(value) != WORK_LENGTH:
        raise ProtocolError("cumulative work must be exactly 40 bytes")
    work = int.from_bytes(value, "big")
    if work <= 0:
        raise ProtocolError("cumulative work must be positive")
    return work


def _exact_bytes(value: bytes, length: int, name: str) -> bytes:
    if type(value) is not bytes or len(value) != length:
        raise ProtocolError(f"{name} must be exactly {length} bytes")
    return value


def encode_frame(
    message_type: MessageType,
    payload: bytes,
    *,
    protocol_version: int = PROTOCOL_VERSION,
) -> bytes:
    if not isinstance(message_type, MessageType):
        raise ProtocolError("unknown message type")
    if type(payload) is not bytes:
        raise ProtocolError("frame payload must be bytes")
    if len(payload) > MAX_FRAME_PAYLOAD:
        raise ProtocolError("frame payload exceeds the resource limit")
    if type(protocol_version) is not int or not 0 <= protocol_version <= 0xFFFF:
        raise ProtocolError("protocol version must be an unsigned 16-bit integer")
    checksum = sha256_digest(payload)[:4]
    return FRAME_PREFIX.pack(
        NETWORK_MAGIC,
        protocol_version,
        int(message_type),
        len(payload),
        checksum,
    ) + payload


def decode_frame(encoded: bytes) -> Frame:
    if type(encoded) is not bytes or len(encoded) < FRAME_PREFIX.size:
        raise ProtocolError("frame is truncated")
    magic, version, raw_type, payload_length, checksum = FRAME_PREFIX.unpack(
        encoded[: FRAME_PREFIX.size]
    )
    if magic != NETWORK_MAGIC:
        raise ProtocolError("frame uses another network magic")
    if version != PROTOCOL_VERSION:
        raise UnsupportedProtocolVersionError(
            f"unsupported P2P protocol version {version}"
        )
    if payload_length > MAX_FRAME_PAYLOAD:
        raise ProtocolError("frame payload exceeds the resource limit")
    payload = encoded[FRAME_PREFIX.size :]
    if len(payload) != payload_length:
        raise ProtocolError("frame payload length does not match its prefix")
    if sha256_digest(payload)[:4] != checksum:
        raise ProtocolError("frame checksum is invalid")
    try:
        message_type = MessageType(raw_type)
    except ValueError as error:
        raise ProtocolError("frame contains an unknown message type") from error
    return Frame(message_type=message_type, payload=payload)


async def read_frame(
    reader: asyncio.StreamReader,
    *,
    timeout: float,
) -> Frame:
    try:
        prefix = await asyncio.wait_for(reader.readexactly(FRAME_PREFIX.size), timeout)
        magic, version, raw_type, payload_length, checksum = FRAME_PREFIX.unpack(prefix)
        if magic != NETWORK_MAGIC:
            raise ProtocolError("frame uses another network magic")
        if version != PROTOCOL_VERSION:
            raise UnsupportedProtocolVersionError(
                f"unsupported P2P protocol version {version}"
            )
        if payload_length > MAX_FRAME_PAYLOAD:
            raise ProtocolError("frame payload exceeds the resource limit")
        payload = await asyncio.wait_for(reader.readexactly(payload_length), timeout)
    except asyncio.IncompleteReadError as error:
        raise PeerDisconnectedError("peer disconnected during a frame") from error
    except TimeoutError as error:
        raise PeerFrameTimeoutError("peer frame timed out") from error
    if sha256_digest(payload)[:4] != checksum:
        raise ProtocolError("frame checksum is invalid")
    try:
        message_type = MessageType(raw_type)
    except ValueError as error:
        raise ProtocolError("frame contains an unknown message type") from error
    return Frame(message_type=message_type, payload=payload)


@dataclass(frozen=True, slots=True)
class Hello:
    node_id: bytes
    chain_id: str
    genesis_hash: bytes
    tip_hash: bytes
    height: int
    cumulative_work: int
    listen_port: int

    def to_bytes(self) -> bytes:
        if len(self.chain_id.encode("utf-8")) > MAX_CHAIN_ID_BYTES:
            raise ProtocolError("chain ID exceeds the wire resource limit")
        return b"".join(
            (
                encode_bytes(HELLO_DOMAIN),
                encode_bytes(_exact_bytes(self.node_id, NODE_ID_LENGTH, "node ID")),
                encode_text(self.chain_id),
                encode_bytes(_exact_bytes(self.genesis_hash, HASH_LENGTH, "genesis hash")),
                encode_bytes(_exact_bytes(self.tip_hash, HASH_LENGTH, "tip hash")),
                encode_u64(self.height),
                encode_bytes(_encode_work(self.cumulative_work)),
                encode_u16(self.listen_port),
            )
        )

    @classmethod
    def from_bytes(cls, encoded: bytes) -> "Hello":
        try:
            reader = CanonicalReader(encoded)
            if reader.read_bytes(max_length=len(HELLO_DOMAIN)) != HELLO_DOMAIN:
                raise ProtocolError("unknown hello domain")
            hello = cls(
                node_id=reader.read_bytes(max_length=NODE_ID_LENGTH),
                chain_id=reader.read_text(max_length=MAX_CHAIN_ID_BYTES),
                genesis_hash=reader.read_bytes(max_length=HASH_LENGTH),
                tip_hash=reader.read_bytes(max_length=HASH_LENGTH),
                height=reader.read_u64(),
                cumulative_work=_decode_work(
                    reader.read_bytes(max_length=WORK_LENGTH)
                ),
                listen_port=reader.read_u16(),
            )
            reader.ensure_finished()
            _exact_bytes(hello.node_id, NODE_ID_LENGTH, "node ID")
            _exact_bytes(hello.genesis_hash, HASH_LENGTH, "genesis hash")
            _exact_bytes(hello.tip_hash, HASH_LENGTH, "tip hash")
        except EncodingError as error:
            raise ProtocolError("invalid canonical hello payload") from error
        return hello


@dataclass(frozen=True, slots=True)
class BlockLocator:
    hashes: tuple[bytes, ...]

    def to_bytes(self) -> bytes:
        if not 0 < len(self.hashes) <= MAX_BLOCK_LOCATOR_HASHES:
            raise ProtocolError("block locator has an invalid item count")
        return encode_bytes(LOCATOR_DOMAIN) + encode_sequence(
            tuple(_exact_bytes(value, HASH_LENGTH, "locator hash") for value in self.hashes)
        )

    @classmethod
    def from_bytes(cls, encoded: bytes) -> "BlockLocator":
        try:
            reader = CanonicalReader(encoded)
            if reader.read_bytes(max_length=len(LOCATOR_DOMAIN)) != LOCATOR_DOMAIN:
                raise ProtocolError("unknown block locator domain")
            hashes = reader.read_sequence(
                max_items=MAX_BLOCK_LOCATOR_HASHES,
                max_item_length=HASH_LENGTH,
            )
            reader.ensure_finished()
        except EncodingError as error:
            raise ProtocolError("invalid canonical block locator") from error
        if not hashes:
            raise ProtocolError("block locator must not be empty")
        for value in hashes:
            _exact_bytes(value, HASH_LENGTH, "locator hash")
        return cls(hashes=hashes)


@dataclass(frozen=True, slots=True)
class ChainSummary:
    tip_hash: bytes
    height: int
    cumulative_work: int

    def to_bytes(self) -> bytes:
        return b"".join(
            (
                encode_bytes(SUMMARY_DOMAIN),
                encode_bytes(_exact_bytes(self.tip_hash, HASH_LENGTH, "tip hash")),
                encode_u64(self.height),
                encode_bytes(_encode_work(self.cumulative_work)),
            )
        )

    @classmethod
    def from_bytes(cls, encoded: bytes) -> "ChainSummary":
        try:
            reader = CanonicalReader(encoded)
            if reader.read_bytes(max_length=len(SUMMARY_DOMAIN)) != SUMMARY_DOMAIN:
                raise ProtocolError("unknown chain summary domain")
            summary = cls(
                tip_hash=reader.read_bytes(max_length=HASH_LENGTH),
                height=reader.read_u64(),
                cumulative_work=_decode_work(
                    reader.read_bytes(max_length=WORK_LENGTH)
                ),
            )
            reader.ensure_finished()
            _exact_bytes(summary.tip_hash, HASH_LENGTH, "tip hash")
        except EncodingError as error:
            raise ProtocolError("invalid canonical chain summary") from error
        return summary


def encode_block(block: Block) -> bytes:
    if not isinstance(block, Block):
        raise ProtocolError("network block value is not a Block")
    return b"".join(
        (
            encode_bytes(BLOCK_BODY_DOMAIN),
            encode_bytes(block.header.to_bytes()),
            encode_bytes(block.reward_transaction.to_bytes()),
            encode_sequence(tuple(transaction.to_bytes() for transaction in block.transactions)),
        )
    )


def decode_block(encoded: bytes) -> Block:
    try:
        reader = CanonicalReader(encoded)
        if reader.read_bytes(max_length=len(BLOCK_BODY_DOMAIN)) != BLOCK_BODY_DOMAIN:
            raise ProtocolError("unknown network block domain")
        header = BlockHeader.from_bytes(reader.read_bytes(max_length=4_096))
        reward = RewardTransaction.from_bytes(reader.read_bytes(max_length=2_048))
        transaction_values = reader.read_sequence(
            max_items=MAX_BLOCK_TRANSACTIONS_WIRE,
            max_item_length=MAX_TRANSACTION_BYTES,
        )
        reader.ensure_finished()
        transactions = tuple(
            Transaction.from_bytes(transaction_value)
            for transaction_value in transaction_values
        )
    except (BlockEncodingError, EncodingError, TransactionError) as error:
        raise ProtocolError("invalid canonical network block") from error
    return Block(header=header, reward_transaction=reward, transactions=transactions)


def encode_ping(nonce: int) -> bytes:
    if type(nonce) is not int or not 0 <= nonce <= U64_MAX:
        raise ProtocolError("ping nonce must be an unsigned 64-bit integer")
    return encode_u64(nonce)


def decode_ping(encoded: bytes) -> int:
    try:
        reader = CanonicalReader(encoded)
        nonce = reader.read_u64()
        reader.ensure_finished()
    except EncodingError as error:
        raise ProtocolError("invalid ping payload") from error
    return nonce


@dataclass(frozen=True, slots=True)
class Reject:
    code: int
    reason: str

    def to_bytes(self) -> bytes:
        if len(self.reason.encode("utf-8")) > 256:
            raise ProtocolError("reject reason exceeds the wire resource limit")
        return encode_bytes(REJECT_DOMAIN) + encode_u16(self.code) + encode_text(self.reason)

    @classmethod
    def from_bytes(cls, encoded: bytes) -> "Reject":
        try:
            reader = CanonicalReader(encoded)
            if reader.read_bytes(max_length=len(REJECT_DOMAIN)) != REJECT_DOMAIN:
                raise ProtocolError("unknown reject domain")
            rejection = cls(
                code=reader.read_u16(),
                reason=reader.read_text(max_length=256),
            )
            reader.ensure_finished()
        except EncodingError as error:
            raise ProtocolError("invalid reject payload") from error
        return rejection


__all__ = [
    "MAX_FRAME_PAYLOAD",
    "PROTOCOL_VERSION",
    "BlockLocator",
    "ChainSummary",
    "Frame",
    "Hello",
    "MessageType",
    "PeerDisconnectedError",
    "PeerFrameTimeoutError",
    "ProtocolError",
    "Reject",
    "UnsupportedProtocolVersionError",
    "decode_block",
    "decode_frame",
    "decode_ping",
    "encode_block",
    "encode_frame",
    "encode_ping",
    "read_frame",
]
