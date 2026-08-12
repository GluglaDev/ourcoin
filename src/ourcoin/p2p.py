"""Async localhost-only peer network for the OurCoin M7 testnet."""

import asyncio
import contextlib
import os
import time
from collections import deque
from dataclasses import dataclass, replace
from ipaddress import IPv6Address, ip_address
from math import isfinite

from ourcoin.block import Block
from ourcoin.chain import ChainError
from ourcoin.node import LocalNode, MiningResult, NodeError
from ourcoin.p2p_protocol import (
    FRAME_PREFIX,
    BlockLocator,
    ChainSummary,
    Frame,
    Hello,
    MessageType,
    PeerDisconnectedError,
    PeerFrameTimeoutError,
    ProtocolError,
    Reject,
    UnsupportedProtocolVersionError,
    decode_block,
    decode_ping,
    encode_block,
    encode_frame,
    encode_ping,
    read_frame,
)
from ourcoin.storage import StorageError
from ourcoin.transaction import Transaction, TransactionError

DEFAULT_TESTNET_PORT = 19_733
DEFAULT_LISTEN_HOST = "127.0.0.1"
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
MAX_BLOCKS_PER_RESPONSE = 128
HANDSHAKE_TIMEOUT_SECONDS = 5.0
IDLE_TIMEOUT_SECONDS = 60.0
SCORE_DISCONNECT_THRESHOLD = 100
BAN_SECONDS = 60.0
WRITE_TIMEOUT_SECONDS = 5.0
MAX_PENDING_SENDS_PER_PEER = 16


class P2PError(RuntimeError):
    """Base class for local testnet networking errors."""


class LocalTestnetOnlyError(P2PError):
    """Raised when M7 is asked to expose or dial a non-loopback address."""


class PeerLimitError(P2PError):
    """Raised when a configured peer resource limit is reached."""


class HandshakeError(P2PError):
    """Raised when an outbound peer does not complete a valid handshake."""


def _is_loopback_address(value: str) -> bool:
    """Return whether a resolved numeric socket address is loopback-only."""

    without_scope = value.partition("%")[0]
    try:
        address = ip_address(without_scope)
    except ValueError:
        return False
    if isinstance(address, IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped.is_loopback
    return address.is_loopback


async def _close_stream_writer(
    writer: asyncio.StreamWriter,
    *,
    timeout: float,
) -> None:
    writer.close()
    with contextlib.suppress(ConnectionError, OSError, TimeoutError):
        await asyncio.wait_for(writer.wait_closed(), timeout)


@dataclass(frozen=True, slots=True)
class PeerLimits:
    max_peers: int = 32
    max_peers_per_ip: int = 4
    max_messages_per_minute: int = 600
    max_bytes_per_minute: int = 32 * 1024 * 1024
    max_pending_sends_per_peer: int = MAX_PENDING_SENDS_PER_PEER
    score_disconnect_threshold: int = SCORE_DISCONNECT_THRESHOLD
    write_timeout_seconds: float = WRITE_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        values = (
            self.max_peers,
            self.max_peers_per_ip,
            self.max_messages_per_minute,
            self.max_bytes_per_minute,
            self.max_pending_sends_per_peer,
            self.score_disconnect_threshold,
        )
        if any(type(value) is not int or value <= 0 for value in values):
            raise ValueError("peer limits must be positive integers")
        if (
            type(self.write_timeout_seconds) not in {int, float}
            or self.write_timeout_seconds <= 0
            or not isfinite(self.write_timeout_seconds)
        ):
            raise ValueError("peer write timeout must be positive and finite")


@dataclass(frozen=True, slots=True)
class PeerInfo:
    node_id: str
    host: str
    port: int
    height: int
    tip_hash: str
    cumulative_work: int
    score: int
    outbound: bool


class _RateLimiter:
    def __init__(self, limits: PeerLimits) -> None:
        self._limits = limits
        self._events: deque[tuple[float, int]] = deque()
        self._bytes = 0

    def allow(self, size: int, *, now: float | None = None) -> bool:
        current = time.monotonic() if now is None else now
        cutoff = current - 60.0
        while self._events and self._events[0][0] <= cutoff:
            _timestamp, previous_size = self._events.popleft()
            self._bytes -= previous_size
        if len(self._events) + 1 > self._limits.max_messages_per_minute:
            return False
        if self._bytes + size > self._limits.max_bytes_per_minute:
            return False
        self._events.append((current, size))
        self._bytes += size
        return True


class _PeerConnection:
    def __init__(
        self,
        owner: "P2PNode",
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        host: str,
        port: int,
        outbound: bool,
    ) -> None:
        self.owner = owner
        self.reader = reader
        self.writer = writer
        self.host = host
        self.port = port
        self.outbound = outbound
        self.hello: Hello | None = None
        self.score = 0
        self.closed = False
        self.handshake_complete = asyncio.Event()
        self._send_lock = asyncio.Lock()
        self._rate_limiter = _RateLimiter(owner.limits)
        self._task: asyncio.Task[None] | None = None
        self._pending_ping: int | None = None
        self._pending_sends = 0
        self._block_request_outstanding = False
        self._accepted_blocks_in_request = 0
        self._block_request_start_work = 0

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def send(self, message_type: MessageType, payload: bytes) -> None:
        if self.closed:
            raise PeerDisconnectedError("peer connection is closed")
        encoded = encode_frame(message_type, payload)
        if self._pending_sends >= self.owner.limits.max_pending_sends_per_peer:
            raise PeerDisconnectedError("peer outbound queue reached its resource limit")
        self._pending_sends += 1
        try:
            try:
                async with asyncio.timeout(self.owner.limits.write_timeout_seconds):
                    async with self._send_lock:
                        if self.closed:
                            raise PeerDisconnectedError("peer connection is closed")
                        self.writer.write(encoded)
                        await self.writer.drain()
            except TimeoutError as error:
                raise PeerDisconnectedError("peer write timed out") from error
        finally:
            self._pending_sends -= 1

    async def reject(self, code: int, reason: str) -> None:
        safe_reason = reason[:256]
        with contextlib.suppress(PeerDisconnectedError, ConnectionError):
            await self.send(MessageType.REJECT, Reject(code, safe_reason).to_bytes())

    def penalize(self, points: int) -> None:
        self.score += points
        host_score = self.owner._host_scores.get(self.host, 0) + points
        self.owner._host_scores[self.host] = host_score
        if self.hello is not None:
            self.owner._scores[self.hello.node_id] = self.score
        if (
            self.score >= self.owner.limits.score_disconnect_threshold
            or host_score >= self.owner.limits.score_disconnect_threshold
        ):
            self.owner._banned_until[self.host] = time.monotonic() + BAN_SECONDS
            raise ProtocolError("peer score reached the disconnect threshold")

    async def _run(self) -> None:
        try:
            await self.send(MessageType.HELLO, self.owner._hello().to_bytes())
            first = await read_frame(self.reader, timeout=HANDSHAKE_TIMEOUT_SECONDS)
            if first.message_type is not MessageType.HELLO:
                raise ProtocolError("first peer message must be HELLO")
            hello = Hello.from_bytes(first.payload)
            self.owner._complete_handshake(self, hello)
            self.hello = hello
            self.score = self.owner._scores.get(hello.node_id, 0)
            self.handshake_complete.set()
            if hello.cumulative_work > self.owner.local_node.chain.cumulative_work:
                await self.owner._request_blocks(self)

            while not self.closed:
                try:
                    frame = await read_frame(self.reader, timeout=IDLE_TIMEOUT_SECONDS)
                except PeerFrameTimeoutError as error:
                    if self._pending_ping is not None:
                        raise PeerDisconnectedError(
                            "peer did not answer the keepalive ping"
                        ) from error
                    self._pending_ping = int.from_bytes(os.urandom(8), "big")
                    await self.send(MessageType.PING, encode_ping(self._pending_ping))
                    continue
                frame_size = FRAME_PREFIX.size + len(frame.payload)
                if not self._rate_limiter.allow(frame_size):
                    self.penalize(self.owner.limits.score_disconnect_threshold)
                await self.owner._handle_frame(self, frame)
        except PeerDisconnectedError:
            pass
        except UnsupportedProtocolVersionError:
            with contextlib.suppress(ProtocolError):
                self.penalize(self.owner.limits.score_disconnect_threshold)
        except PeerFrameTimeoutError:
            with contextlib.suppress(ProtocolError):
                self.penalize(20)
        except (ProtocolError, TransactionError):
            with contextlib.suppress(ProtocolError):
                self.penalize(20)
        except (ConnectionError, OSError):
            pass
        except StorageError:
            # A local durability failure is not evidence of peer misconduct.
            pass
        finally:
            self.handshake_complete.set()
            await self.close()

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.owner._remove_connection(self)
        await _close_stream_writer(
            self.writer,
            timeout=self.owner.limits.write_timeout_seconds,
        )

    def info(self) -> PeerInfo | None:
        if self.hello is None or self.closed:
            return None
        return PeerInfo(
            node_id=self.hello.node_id.hex(),
            host=self.host,
            port=self.hello.listen_port,
            height=self.hello.height,
            tip_hash=self.hello.tip_hash.hex(),
            cumulative_work=self.hello.cumulative_work,
            score=self.score,
            outbound=self.outbound,
        )

    def begin_block_request(self, *, local_work: int) -> bool:
        if self._block_request_outstanding:
            return False
        self._block_request_outstanding = True
        self._accepted_blocks_in_request = 0
        self._block_request_start_work = local_work
        return True

    def cancel_block_request(self) -> None:
        self._block_request_outstanding = False
        self._accepted_blocks_in_request = 0
        self._block_request_start_work = 0

    def note_accepted_block(self) -> None:
        if self._block_request_outstanding:
            self._accepted_blocks_in_request += 1

    def finish_block_request(self) -> tuple[int, int] | None:
        if not self._block_request_outstanding:
            return None
        accepted = self._accepted_blocks_in_request
        start_work = self._block_request_start_work
        self.cancel_block_request()
        return accepted, start_work

    def update_advertised_summary(self, summary: ChainSummary) -> None:
        if self.hello is None:
            raise ProtocolError("peer summary arrived before HELLO")
        if summary.cumulative_work < self.hello.cumulative_work:
            raise ProtocolError("peer cumulative work moved backwards")
        self.hello = replace(
            self.hello,
            tip_hash=summary.tip_hash,
            height=summary.height,
            cumulative_work=summary.cumulative_work,
        )


class P2PNode:
    """One localhost-only TCP peer service around an existing LocalNode."""

    def __init__(
        self,
        local_node: LocalNode,
        *,
        host: str = DEFAULT_LISTEN_HOST,
        port: int = DEFAULT_TESTNET_PORT,
        limits: PeerLimits | None = None,
        node_id: bytes | None = None,
    ) -> None:
        if host not in LOOPBACK_HOSTS:
            raise LocalTestnetOnlyError("M7 listens only on localhost addresses")
        if type(port) is not int or not 0 <= port <= 0xFFFF:
            raise P2PError("listen port must be an unsigned 16-bit integer")
        selected_node_id = os.urandom(16) if node_id is None else node_id
        if type(selected_node_id) is not bytes or len(selected_node_id) != 16:
            raise P2PError("node ID must be exactly 16 bytes")
        self.local_node = local_node
        self.host = host
        self.port = port
        self.node_id = selected_node_id
        self.limits = PeerLimits() if limits is None else limits
        self._server: asyncio.Server | None = None
        self._connections: set[_PeerConnection] = set()
        self._by_node_id: dict[bytes, _PeerConnection] = {}
        self._scores: dict[bytes, int] = {}
        self._host_scores: dict[str, int] = {}
        self._banned_until: dict[str, float] = {}

    @property
    def peers(self) -> tuple[PeerInfo, ...]:
        peer_values = (
            info
            for connection in self._connections
            if (info := connection.info()) is not None
        )
        return tuple(sorted(peer_values, key=lambda info: info.node_id))

    @property
    def is_running(self) -> bool:
        return self._server is not None

    async def start(self) -> None:
        if self._server is not None:
            raise P2PError("peer server is already running")
        self._server = await asyncio.start_server(self._accept, self.host, self.port)
        sockets = self._server.sockets or ()
        if not sockets:
            await self.close()
            raise P2PError("peer server did not create a listening socket")
        for sock in sockets:
            socket_name = sock.getsockname()
            if (
                not isinstance(socket_name, tuple)
                or not socket_name
                or not _is_loopback_address(str(socket_name[0]))
            ):
                await self.close()
                raise LocalTestnetOnlyError("resolved listen address is not loopback")
        socket_name = sockets[0].getsockname()
        self.port = int(socket_name[1])

    def _endpoint(self, writer: asyncio.StreamWriter) -> tuple[str, int]:
        peer = writer.get_extra_info("peername")
        if not isinstance(peer, tuple) or len(peer) < 2:
            raise P2PError("peer socket has no remote endpoint")
        return str(peer[0]), int(peer[1])

    def _host_connection_count(self, host: str) -> int:
        return sum(1 for connection in self._connections if connection.host == host)

    def _can_accept(self, host: str) -> bool:
        banned_until = self._banned_until.get(host, 0.0)
        current = time.monotonic()
        if banned_until and current >= banned_until:
            self._banned_until.pop(host, None)
            self._host_scores.pop(host, None)
            banned_until = 0.0
        return (
            current >= banned_until
            and len(self._connections) < self.limits.max_peers
            and self._host_connection_count(host) < self.limits.max_peers_per_ip
        )

    async def _accept(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            host, port = self._endpoint(writer)
        except P2PError:
            await _close_stream_writer(
                writer,
                timeout=self.limits.write_timeout_seconds,
            )
            return
        if not _is_loopback_address(host) or not self._can_accept(host):
            await _close_stream_writer(
                writer,
                timeout=self.limits.write_timeout_seconds,
            )
            return
        connection = _PeerConnection(
            self,
            reader,
            writer,
            host=host,
            port=port,
            outbound=False,
        )
        self._connections.add(connection)
        connection.start()

    async def connect(self, host: str, port: int) -> PeerInfo:
        if self._server is None:
            raise P2PError("start the peer server before connecting")
        if host not in LOOPBACK_HOSTS:
            raise LocalTestnetOnlyError("M7 dials only localhost addresses")
        if type(port) is not int or not 1 <= port <= 0xFFFF:
            raise P2PError("peer port must be between 1 and 65535")
        if not self._can_accept(host):
            raise PeerLimitError("peer limit or temporary ban prevents connection")
        reader, writer = await asyncio.open_connection(host, port)
        endpoint_host, endpoint_port = self._endpoint(writer)
        if not _is_loopback_address(endpoint_host):
            await _close_stream_writer(
                writer,
                timeout=self.limits.write_timeout_seconds,
            )
            raise LocalTestnetOnlyError("resolved peer address is not loopback")
        if not self._can_accept(endpoint_host):
            await _close_stream_writer(
                writer,
                timeout=self.limits.write_timeout_seconds,
            )
            raise PeerLimitError("resolved peer endpoint exceeds a connection limit")
        connection = _PeerConnection(
            self,
            reader,
            writer,
            host=endpoint_host,
            port=endpoint_port,
            outbound=True,
        )
        self._connections.add(connection)
        connection.start()
        try:
            await asyncio.wait_for(
                connection.handshake_complete.wait(),
                HANDSHAKE_TIMEOUT_SECONDS + 1,
            )
        except TimeoutError as error:
            await connection.close()
            raise HandshakeError("peer handshake timed out") from error
        info = connection.info()
        if info is None:
            raise HandshakeError("peer rejected or closed the handshake")
        return info

    def _hello(self) -> Hello:
        chain = self.local_node.chain
        genesis_hash = chain.active_chain()[0].block_hash
        return Hello(
            node_id=self.node_id,
            chain_id=chain.network.chain_id,
            genesis_hash=genesis_hash,
            tip_hash=chain.tip_hash,
            height=chain.height,
            cumulative_work=chain.cumulative_work,
            listen_port=self.port,
        )

    def _complete_handshake(self, connection: _PeerConnection, hello: Hello) -> None:
        local = self._hello()
        if hello.chain_id != local.chain_id:
            raise ProtocolError("peer belongs to another chain_id")
        if hello.genesis_hash != local.genesis_hash:
            raise ProtocolError("peer uses another genesis block")
        if hello.node_id == self.node_id:
            raise ProtocolError("self-connections are not allowed")
        existing = self._by_node_id.get(hello.node_id)
        if existing is not None and existing is not connection and not existing.closed:
            raise ProtocolError("duplicate peer node ID")
        self._by_node_id[hello.node_id] = connection

    def _remove_connection(self, connection: _PeerConnection) -> None:
        self._connections.discard(connection)
        if connection.hello is not None:
            existing = self._by_node_id.get(connection.hello.node_id)
            if existing is connection:
                self._by_node_id.pop(connection.hello.node_id, None)

    def _block_locator(self) -> BlockLocator:
        active = self.local_node.chain.active_chain()
        hashes: list[bytes] = []
        index = len(active) - 1
        step = 1
        while index > 0 and len(hashes) < 63:
            hashes.append(active[index].block_hash)
            if len(hashes) >= 10:
                step *= 2
            index = max(0, index - step)
        genesis_hash = active[0].block_hash
        if not hashes or hashes[-1] != genesis_hash:
            hashes.append(genesis_hash)
        return BlockLocator(tuple(hashes))

    async def _request_blocks(self, connection: _PeerConnection) -> bool:
        if not connection.begin_block_request(
            local_work=self.local_node.chain.cumulative_work
        ):
            return False
        try:
            await connection.send(MessageType.GET_BLOCKS, self._block_locator().to_bytes())
        except BaseException:
            connection.cancel_block_request()
            raise
        return True

    async def _send_blocks(self, connection: _PeerConnection, locator: BlockLocator) -> None:
        active = self.local_node.chain.active_chain()
        positions = {block.block_hash: index for index, block in enumerate(active)}
        common_index: int | None = None
        for block_hash in locator.hashes:
            if block_hash in positions:
                common_index = positions[block_hash]
                break
        if common_index is None:
            connection.penalize(10)
            await connection.reject(20, "block locator has no common testnet ancestor")
            return
        for block in active[common_index + 1 : common_index + 1 + MAX_BLOCKS_PER_RESPONSE]:
            await connection.send(MessageType.BLOCK, encode_block(block))
        summary = ChainSummary(
            tip_hash=self.local_node.chain.tip_hash,
            height=self.local_node.chain.height,
            cumulative_work=self.local_node.chain.cumulative_work,
        )
        await connection.send(MessageType.SYNC_COMPLETE, summary.to_bytes())

    async def _handle_block(self, connection: _PeerConnection, block: Block) -> None:
        if self.local_node.chain.contains(block.block_hash):
            return
        try:
            self.local_node.submit_block(block)
        except ChainError as error:
            message = str(error)
            if "parent is unknown" in message:
                await self._request_blocks(connection)
                return
            if "already known" in message:
                return
            connection.penalize(20)
            await connection.reject(30, "invalid block")
            return
        connection.note_accepted_block()
        await self._broadcast(MessageType.BLOCK, encode_block(block), exclude=connection)

    async def _handle_sync_complete(
        self,
        connection: _PeerConnection,
        summary: ChainSummary,
    ) -> None:
        if self.local_node.chain.contains(summary.tip_hash):
            record = self.local_node.chain.get_record(summary.tip_hash)
            if (
                record.height != summary.height
                or record.cumulative_work != summary.cumulative_work
            ):
                raise ProtocolError("peer summary contradicts a known block")
        connection.update_advertised_summary(summary)
        request_progress = connection.finish_block_request()
        local_work = self.local_node.chain.cumulative_work
        if summary.cumulative_work <= local_work:
            return
        if (
            request_progress is not None
            and request_progress[0] == 0
            and local_work <= request_progress[1]
        ):
            connection.penalize(20)
            await connection.reject(32, "block synchronization made no progress")
            raise PeerDisconnectedError("peer claimed higher work without progress")
        await self._request_blocks(connection)

    async def _handle_transaction(
        self,
        connection: _PeerConnection,
        transaction: Transaction,
    ) -> None:
        if self.local_node.mempool.contains(transaction.txid):
            return
        if self.local_node.chain.state.contains_transaction(transaction.txid):
            return
        try:
            self.local_node.submit_transaction(transaction)
        except NodeError as error:
            if "already" in str(error):
                return
            connection.penalize(10)
            await connection.reject(31, "invalid transaction")
            return
        await self._broadcast(
            MessageType.TRANSACTION,
            transaction.to_bytes(),
            exclude=connection,
        )

    async def _handle_frame(self, connection: _PeerConnection, frame: Frame) -> None:
        if frame.message_type is MessageType.HELLO:
            raise ProtocolError("HELLO may appear only once")
        if frame.message_type is MessageType.GET_BLOCKS:
            await self._send_blocks(connection, BlockLocator.from_bytes(frame.payload))
        elif frame.message_type is MessageType.BLOCK:
            await self._handle_block(connection, decode_block(frame.payload))
        elif frame.message_type is MessageType.TRANSACTION:
            await self._handle_transaction(
                connection,
                Transaction.from_bytes(frame.payload),
            )
        elif frame.message_type is MessageType.SYNC_COMPLETE:
            await self._handle_sync_complete(
                connection,
                ChainSummary.from_bytes(frame.payload),
            )
        elif frame.message_type is MessageType.PING:
            await connection.send(MessageType.PONG, encode_ping(decode_ping(frame.payload)))
        elif frame.message_type is MessageType.PONG:
            nonce = decode_ping(frame.payload)
            if connection._pending_ping != nonce:
                raise ProtocolError("peer returned an unsolicited or mismatched PONG")
            connection._pending_ping = None
        elif frame.message_type is MessageType.REJECT:
            Reject.from_bytes(frame.payload)

    async def _broadcast(
        self,
        message_type: MessageType,
        payload: bytes,
        *,
        exclude: _PeerConnection | None = None,
    ) -> None:
        targets = [
            connection
            for connection in self._connections
            if connection is not exclude and connection.hello is not None and not connection.closed
        ]
        results = await asyncio.gather(
            *(connection.send(message_type, payload) for connection in targets),
            return_exceptions=True,
        )
        for connection, result in zip(targets, results, strict=True):
            if isinstance(result, BaseException):
                await connection.close()

    async def broadcast_transaction(self, transaction: Transaction) -> None:
        await self._broadcast(MessageType.TRANSACTION, transaction.to_bytes())

    async def broadcast_block(self, block: Block) -> None:
        await self._broadcast(MessageType.BLOCK, encode_block(block))

    async def mine_and_broadcast(
        self,
        miner_address: str,
        *,
        timestamp: int | None = None,
        max_transactions: int = 10_000,
        max_attempts: int = 1_000_000,
    ) -> MiningResult:
        result = self.local_node.mine(
            miner_address,
            timestamp=timestamp,
            max_transactions=max_transactions,
            max_attempts=max_attempts,
        )
        await self.broadcast_block(result.block)
        return result

    async def close(self) -> None:
        server = self._server
        self._server = None
        if server is not None:
            server.close()
            await server.wait_closed()
        connections = tuple(self._connections)
        await asyncio.gather(
            *(connection.close() for connection in connections),
            return_exceptions=True,
        )
        current_task = asyncio.current_task()
        tasks = tuple(
            connection._task
            for connection in connections
            if connection._task is not None
            and connection._task is not current_task
            and not connection._task.done()
        )
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def __aenter__(self) -> "P2PNode":
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: object,
        exc_value: object,
        traceback: object,
    ) -> None:
        await self.close()


__all__ = [
    "DEFAULT_LISTEN_HOST",
    "DEFAULT_TESTNET_PORT",
    "HandshakeError",
    "LocalTestnetOnlyError",
    "P2PError",
    "P2PNode",
    "PeerInfo",
    "PeerLimitError",
    "PeerLimits",
]
