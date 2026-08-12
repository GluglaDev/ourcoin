import asyncio

import pytest

from ourcoin.consensus import genesis_block
from ourcoin.p2p_protocol import (
    FRAME_PREFIX,
    MAX_FRAME_PAYLOAD,
    NETWORK_MAGIC,
    BlockLocator,
    ChainSummary,
    Hello,
    MessageType,
    PeerDisconnectedError,
    PeerFrameTimeoutError,
    ProtocolError,
    Reject,
    UnsupportedProtocolVersionError,
    decode_block,
    decode_frame,
    decode_ping,
    encode_block,
    encode_frame,
    encode_ping,
    read_frame,
)


def test_frame_round_trip_and_checksum() -> None:
    encoded = encode_frame(MessageType.PING, encode_ping(42))

    assert decode_frame(encoded).message_type is MessageType.PING
    assert decode_ping(decode_frame(encoded).payload) == 42

    damaged = encoded[:-1] + bytes((encoded[-1] ^ 1,))
    with pytest.raises(ProtocolError, match="checksum"):
        decode_frame(damaged)


@pytest.mark.parametrize(
    ("encoded", "message"),
    [
        (b"short", "truncated"),
        (
            FRAME_PREFIX.pack(b"NOPE", 1, int(MessageType.PING), 0, b"\xe3\xb0\xc4\x42"),
            "network magic",
        ),
        (encode_frame(MessageType.PING, b"") + b"trailing", "length"),
        (
            FRAME_PREFIX.pack(
                NETWORK_MAGIC,
                1,
                int(MessageType.PING),
                MAX_FRAME_PAYLOAD + 1,
                b"\x00" * 4,
            ),
            "resource limit",
        ),
    ],
)
def test_frame_rejects_invalid_bounds(encoded: bytes, message: str) -> None:
    with pytest.raises(ProtocolError, match=message):
        decode_frame(encoded)


def test_frame_rejects_unknown_protocol_version_and_message_type() -> None:
    with pytest.raises(UnsupportedProtocolVersionError, match="version 99"):
        decode_frame(encode_frame(MessageType.PING, b"", protocol_version=99))

    valid = bytearray(encode_frame(MessageType.PING, b""))
    valid[6] = 255
    with pytest.raises(ProtocolError, match="unknown message type"):
        decode_frame(bytes(valid))


def test_async_reader_rejects_disconnect_mid_frame() -> None:
    async def scenario() -> None:
        reader = asyncio.StreamReader()
        reader.feed_data(encode_frame(MessageType.PING, b"")[:-1])
        reader.feed_eof()
        with pytest.raises(PeerDisconnectedError, match="disconnected"):
            await read_frame(reader, timeout=0.1)

    asyncio.run(scenario())


def test_async_reader_has_a_distinct_frame_timeout() -> None:
    async def scenario() -> None:
        reader = asyncio.StreamReader()
        with pytest.raises(PeerFrameTimeoutError, match="timed out"):
            await read_frame(reader, timeout=0.001)

    asyncio.run(scenario())


def test_hello_locator_summary_and_reject_are_canonical() -> None:
    block = genesis_block()
    hello = Hello(
        node_id=b"n" * 16,
        chain_id="ourcoin-testnet-v1",
        genesis_hash=block.block_hash,
        tip_hash=block.block_hash,
        height=0,
        cumulative_work=2,
        listen_port=19_733,
    )
    locator = BlockLocator((block.block_hash,))
    summary = ChainSummary(block.block_hash, 0, 2)
    rejection = Reject(20, "no common ancestor")

    assert Hello.from_bytes(hello.to_bytes()) == hello
    assert BlockLocator.from_bytes(locator.to_bytes()) == locator
    assert ChainSummary.from_bytes(summary.to_bytes()) == summary
    assert Reject.from_bytes(rejection.to_bytes()) == rejection

    for parser, encoded in (
        (Hello.from_bytes, hello.to_bytes()),
        (BlockLocator.from_bytes, locator.to_bytes()),
        (ChainSummary.from_bytes, summary.to_bytes()),
        (Reject.from_bytes, rejection.to_bytes()),
    ):
        with pytest.raises(ProtocolError):
            parser(encoded + b"trailing")


def test_outgoing_text_payloads_enforce_wire_limits() -> None:
    block = genesis_block()
    oversized_hello = Hello(
        node_id=b"n" * 16,
        chain_id="x" * 65,
        genesis_hash=block.block_hash,
        tip_hash=block.block_hash,
        height=0,
        cumulative_work=1,
        listen_port=19_733,
    )
    with pytest.raises(ProtocolError, match="chain ID"):
        oversized_hello.to_bytes()
    with pytest.raises(ProtocolError, match="reject reason"):
        Reject(1, "x" * 257).to_bytes()


def test_block_transport_preserves_existing_canonical_encodings() -> None:
    block = genesis_block()

    decoded = decode_block(encode_block(block))

    assert decoded == block
    assert decoded.header.to_bytes() == block.header.to_bytes()
    assert decoded.reward_transaction.to_bytes() == block.reward_transaction.to_bytes()
    with pytest.raises(ProtocolError):
        decode_block(encode_block(block) + b"trailing")


@pytest.mark.parametrize("value", [-1, 1 << 64, True])
def test_ping_rejects_values_outside_u64(value: int) -> None:
    with pytest.raises(ProtocolError):
        encode_ping(value)
