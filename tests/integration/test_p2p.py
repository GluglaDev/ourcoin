import asyncio
import json
from collections.abc import Callable

import pytest

import ourcoin.p2p as p2p_module
from ourcoin.cli import CliError, run_network_node
from ourcoin.consensus import ATOMS_PER_OUR
from ourcoin.node import LocalNode
from ourcoin.p2p import (
    HandshakeError,
    LocalTestnetOnlyError,
    P2PNode,
    PeerLimits,
)
from ourcoin.p2p_protocol import (
    Hello,
    MessageType,
    encode_frame,
    encode_ping,
    read_frame,
)
from ourcoin.wallet import Wallet


async def _wait_until(
    predicate: Callable[[], bool],
    *,
    timeout: float = 5.0,
) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("condition was not reached before timeout")
        await asyncio.sleep(0.01)


def test_local_peers_propagate_transactions_blocks_and_sync_after_restart(tmp_path) -> None:
    async def scenario() -> tuple[bytes, bytes, bytes, int]:
        alice = Wallet.create("alice")
        bob = Wallet.create("bob")
        node_a = LocalNode.open_persistent(tmp_path / "a")
        node_b = LocalNode.open_persistent(tmp_path / "b")
        node_c = LocalNode.open_persistent(tmp_path / "c")
        peer_a = P2PNode(node_a, port=0, node_id=b"a" * 16)
        peer_b = P2PNode(node_b, port=0, node_id=b"b" * 16)
        peer_c = P2PNode(node_c, port=0, node_id=b"c" * 16)
        try:
            await peer_a.start()
            await peer_b.start()
            await peer_c.start()
            await peer_b.connect("127.0.0.1", peer_a.port)
            await _wait_until(lambda: len(peer_a.peers) == len(peer_b.peers) == 1)

            first = await peer_a.mine_and_broadcast(alice.address)
            await _wait_until(lambda: node_b.chain.tip_hash == first.block.block_hash)

            transaction = node_a.send(
                alice,
                recipient_address=bob.address,
                amount_atoms=3 * ATOMS_PER_OUR,
                fee_atoms=7,
            )
            await peer_a.broadcast_transaction(transaction)
            await _wait_until(lambda: node_b.mempool.contains(transaction.txid))

            second = await peer_b.mine_and_broadcast(bob.address)
            await _wait_until(lambda: node_a.chain.tip_hash == second.block.block_hash)
            assert not node_a.mempool.contains(transaction.txid)
            assert node_a.chain.state.snapshot() == node_b.chain.state.snapshot()

            await peer_c.connect("127.0.0.1", peer_a.port)
            await _wait_until(lambda: node_c.chain.tip_hash == node_a.chain.tip_hash)
            assert node_c.chain.state.snapshot() == node_a.chain.state.snapshot()
            assert node_c.chain.total_supply_atoms == node_a.chain.total_supply_atoms
            return (
                node_a.chain.tip_hash,
                transaction.txid,
                alice.address.encode("ascii"),
                node_a.chain.total_supply_atoms,
            )
        finally:
            await asyncio.gather(peer_a.close(), peer_b.close(), peer_c.close())
            node_a.close()
            node_b.close()
            node_c.close()

    tip_hash, transaction_id, alice_address, supply = asyncio.run(scenario())

    with LocalNode.open_persistent(tmp_path / "c", create=False) as restarted:
        assert restarted.chain.tip_hash == tip_hash
        assert restarted.chain.state.contains_transaction(transaction_id)
        assert restarted.account(alice_address.decode("ascii")).nonce == 1
        assert restarted.chain.total_supply_atoms == supply


def test_self_connection_is_rejected(tmp_path) -> None:
    async def scenario() -> None:
        local = LocalNode.open_persistent(tmp_path)
        peer = P2PNode(local, port=0, node_id=b"a" * 16)
        try:
            await peer.start()
            with pytest.raises(HandshakeError, match="rejected"):
                await peer.connect("127.0.0.1", peer.port)
            assert not peer.peers
        finally:
            await peer.close()
            local.close()

    asyncio.run(scenario())


def test_second_peer_from_same_ip_is_rejected(tmp_path) -> None:
    async def scenario() -> None:
        local_a = LocalNode.open_persistent(tmp_path / "a")
        local_b = LocalNode.open_persistent(tmp_path / "b")
        local_c = LocalNode.open_persistent(tmp_path / "c")
        peer_a = P2PNode(
            local_a,
            port=0,
            node_id=b"a" * 16,
            limits=PeerLimits(max_peers_per_ip=1),
        )
        peer_b = P2PNode(local_b, port=0, node_id=b"b" * 16)
        peer_c = P2PNode(local_c, port=0, node_id=b"c" * 16)
        try:
            await peer_a.start()
            await peer_b.start()
            await peer_c.start()
            await peer_b.connect("127.0.0.1", peer_a.port)
            with pytest.raises(HandshakeError, match="rejected"):
                await peer_c.connect("127.0.0.1", peer_a.port)
        finally:
            await asyncio.gather(peer_a.close(), peer_b.close(), peer_c.close())
            local_a.close()
            local_b.close()
            local_c.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("attack", ["unknown-version", "wrong-chain"])
def test_untrusted_handshake_is_disconnected(tmp_path, attack: str) -> None:
    async def scenario() -> None:
        local = LocalNode.open_persistent(tmp_path / attack)
        peer = P2PNode(local, port=0, node_id=b"s" * 16)
        await peer.start()
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", peer.port)
            server_hello = await read_frame(reader, timeout=1.0)
            assert server_hello.message_type is MessageType.HELLO
            if attack == "unknown-version":
                encoded = encode_frame(
                    MessageType.HELLO,
                    b"",
                    protocol_version=99,
                )
            else:
                hello = Hello.from_bytes(server_hello.payload)
                encoded = encode_frame(
                    MessageType.HELLO,
                    Hello(
                        node_id=b"x" * 16,
                        chain_id="another-chain",
                        genesis_hash=hello.genesis_hash,
                        tip_hash=hello.tip_hash,
                        height=hello.height,
                        cumulative_work=hello.cumulative_work,
                        listen_port=20_000,
                    ).to_bytes(),
                )
            writer.write(encoded)
            await writer.drain()
            assert await asyncio.wait_for(reader.read(1), 1.0) == b""
            writer.close()
            await writer.wait_closed()
            assert not peer.peers
        finally:
            await peer.close()
            local.close()

    asyncio.run(scenario())


def test_network_cli_runner_starts_and_closes_cleanly(tmp_path, capsys) -> None:
    async def scenario() -> None:
        stop_event = asyncio.Event()
        stop_event.set()
        await run_network_node(
            tmp_path,
            port=0,
            stop_event=stop_event,
        )

    asyncio.run(scenario())
    document = json.loads(capsys.readouterr().out)
    assert document["chain_id"] == "ourcoin-testnet-v1"
    assert document["height"] == 0
    assert document["host"] == "127.0.0.1"
    assert document["port"] > 0
    assert document["peers"] == []
    with LocalNode.open_persistent(tmp_path, create=False) as restarted:
        assert restarted.chain.height == 0


def test_network_cli_runner_rejects_non_local_peer(tmp_path) -> None:
    async def scenario() -> None:
        with pytest.raises(CliError, match="localhost"):
            await run_network_node(
                tmp_path,
                port=0,
                peer_endpoints=("192.0.2.10:19733",),
            )

    asyncio.run(scenario())


def test_message_rate_limit_disconnects_flooding_peer(tmp_path) -> None:
    async def scenario() -> None:
        local = LocalNode.open_persistent(tmp_path)
        peer = P2PNode(
            local,
            port=0,
            node_id=b"s" * 16,
            limits=PeerLimits(max_messages_per_minute=1),
        )
        await peer.start()
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", peer.port)
            server_frame = await read_frame(reader, timeout=1.0)
            server_hello = Hello.from_bytes(server_frame.payload)
            client_hello = Hello(
                node_id=b"c" * 16,
                chain_id=server_hello.chain_id,
                genesis_hash=server_hello.genesis_hash,
                tip_hash=server_hello.tip_hash,
                height=server_hello.height,
                cumulative_work=server_hello.cumulative_work,
                listen_port=20_001,
            )
            writer.write(encode_frame(MessageType.HELLO, client_hello.to_bytes()))
            writer.write(encode_frame(MessageType.PING, encode_ping(1)))
            await writer.drain()
            pong = await read_frame(reader, timeout=1.0)
            assert pong.message_type is MessageType.PONG

            writer.write(encode_frame(MessageType.PING, encode_ping(2)))
            await writer.drain()
            assert await asyncio.wait_for(reader.read(1), 1.0) == b""
            writer.close()
            await writer.wait_closed()
        finally:
            await peer.close()
            local.close()

    asyncio.run(scenario())


def test_sync_requests_another_bounded_batch() -> None:
    async def scenario() -> None:
        miner = Wallet.create("batch-miner")
        source = LocalNode()
        destination = LocalNode()
        for _ in range(129):
            source.mine(miner.address)
        peer_source = P2PNode(source, port=0, node_id=b"s" * 16)
        peer_destination = P2PNode(destination, port=0, node_id=b"d" * 16)
        try:
            await peer_source.start()
            await peer_destination.start()
            await peer_destination.connect("127.0.0.1", peer_source.port)
            await _wait_until(
                lambda: destination.chain.tip_hash == source.chain.tip_hash,
                timeout=10.0,
            )
            assert destination.chain.height == 129
            assert destination.chain.state.snapshot() == source.chain.state.snapshot()
        finally:
            await asyncio.gather(peer_source.close(), peer_destination.close())

    asyncio.run(scenario())


def test_public_network_addresses_are_rejected_before_socket_use() -> None:
    node = LocalNode()
    with pytest.raises(LocalTestnetOnlyError, match="localhost"):
        P2PNode(node, host="0.0.0.0")

    async def scenario() -> None:
        peer = P2PNode(node, port=0)
        await peer.start()
        try:
            with pytest.raises(LocalTestnetOnlyError, match="localhost"):
                await peer.connect("192.0.2.1", 19_733)
        finally:
            await peer.close()

    asyncio.run(scenario())


def test_invalid_transaction_increases_peer_score_without_entering_mempool() -> None:
    async def scenario() -> None:
        local_a = LocalNode()
        local_b = LocalNode()
        peer_a = P2PNode(local_a, port=0, node_id=b"a" * 16)
        peer_b = P2PNode(local_b, port=0, node_id=b"b" * 16)
        empty_wallet = Wallet.create("empty")
        recipient = Wallet.create("recipient")
        transaction = empty_wallet.create_transaction(
            recipient_address=recipient.address,
            amount_atoms=1,
            fee_atoms=0,
            nonce=0,
            valid_until_height=100,
        )
        try:
            await peer_a.start()
            await peer_b.start()
            await peer_b.connect("127.0.0.1", peer_a.port)
            await _wait_until(lambda: len(peer_a.peers) == 1)
            await peer_b.broadcast_transaction(transaction)
            await _wait_until(lambda: peer_a.peers[0].score == 10)
            assert not local_a.mempool.contains(transaction.txid)
        finally:
            await asyncio.gather(peer_a.close(), peer_b.close())

    asyncio.run(scenario())


def test_idle_peers_exchange_keepalives(monkeypatch) -> None:
    monkeypatch.setattr(p2p_module, "IDLE_TIMEOUT_SECONDS", 0.02)

    async def scenario() -> None:
        local_a = LocalNode()
        local_b = LocalNode()
        peer_a = P2PNode(local_a, port=0, node_id=b"a" * 16)
        peer_b = P2PNode(local_b, port=0, node_id=b"b" * 16)
        try:
            await peer_a.start()
            await peer_b.start()
            await peer_b.connect("127.0.0.1", peer_a.port)
            await asyncio.sleep(0.12)
            assert len(peer_a.peers) == len(peer_b.peers) == 1
            assert peer_a.peers[0].score == peer_b.peers[0].score == 0
        finally:
            await asyncio.gather(peer_a.close(), peer_b.close())

    asyncio.run(scenario())
