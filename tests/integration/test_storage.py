import sqlite3

import pytest

from ourcoin.block import hash_meets_target
from ourcoin.chain import ChainError
from ourcoin.config import NetworkConfig
from ourcoin.consensus import ATOMS_PER_OUR, genesis_block
from ourcoin.encoding import encode_u64
from ourcoin.miner import mine_block
from ourcoin.node import LocalNode
from ourcoin.storage import (
    BlockNotFoundError,
    NetworkMismatchError,
    SQLiteChainStorage,
    StorageClosedError,
    StorageError,
    StorageIntegrityError,
    UnsupportedNetworkError,
    UnsupportedSchemaVersionError,
    database_path_for_identity,
)
from ourcoin.wallet import Wallet


def test_block_balance_nonce_supply_and_tip_survive_restart(tmp_path) -> None:
    alice = Wallet.create("alice")
    bob = Wallet.create("bob")
    with LocalNode.open_persistent(tmp_path) as node:
        first = node.mine(alice.address).block
        transaction = node.send(
            alice,
            recipient_address=bob.address,
            amount_atoms=12 * ATOMS_PER_OUR,
            fee_atoms=3,
        )
        second = node.mine(bob.address).block
        expected_state = node.chain.state.snapshot()
        expected_tip = node.chain.tip_hash
        expected_supply = node.chain.total_supply_atoms

    with SQLiteChainStorage.open(tmp_path, create=False) as storage:
        assert storage.get_block_by_hash(first.block_hash) == first
        assert storage.get_canonical_block_by_height(2) == second
        assert storage.get_tip_record().block_hash == expected_tip
        assert storage.info().issued_supply_atoms == expected_supply

    with LocalNode.open_persistent(tmp_path, create=False) as restarted:
        assert restarted.chain.tip_hash == expected_tip
        assert restarted.chain.state.snapshot() == expected_state
        assert restarted.chain.total_supply_atoms == expected_supply
        assert restarted.account(alice.address).nonce == 1
        assert restarted.chain.state.contains_transaction(transaction.txid)


def test_equal_work_fork_choice_and_side_branch_survive_restart(tmp_path) -> None:
    main_miner = Wallet.create("main")
    side_miner = Wallet.create("side")
    with LocalNode.open_persistent(tmp_path) as node:
        genesis_hash = node.chain.tip_hash
        genesis_timestamp = node.chain.tip.header.timestamp
        main = node.mine(main_miner.address).block
        side = mine_block(
            node.chain.build_candidate(
                parent_hash=genesis_hash,
                miner_address=side_miner.address,
                timestamp=genesis_timestamp + 61,
            )
        )
        update = node.submit_block(side)
        assert not update.became_active
        assert node.chain.tip_hash == main.block_hash

    with LocalNode.open_persistent(tmp_path, create=False) as restarted:
        assert restarted.chain.tip_hash == main.block_hash
        assert restarted.chain.contains(side.block_hash)
        side_two = mine_block(
            restarted.chain.build_candidate(
                parent_hash=side.block_hash,
                miner_address=side_miner.address,
                timestamp=side.header.timestamp + 60,
            )
        )
        assert restarted.submit_block(side_two).reorganized

    with LocalNode.open_persistent(tmp_path, create=False) as final:
        assert final.chain.tip_hash == side_two.block_hash
        assert final.account(side_miner.address).balance_atoms == 80 * ATOMS_PER_OUR


def test_sqlite_failure_rolls_back_database_and_in_memory_chain(tmp_path) -> None:
    miner = Wallet.create("miner")
    storage = SQLiteChainStorage.open(tmp_path)
    node = LocalNode(storage=storage)
    storage._connection.executescript(
        """
        CREATE TEMP TRIGGER simulate_account_failure
        BEFORE INSERT ON accounts
        BEGIN
            SELECT RAISE(ABORT, 'simulated write failure');
        END;
        """
    )
    original_tip = node.chain.tip_hash

    with pytest.raises(StorageError, match="atomically persist"):
        node.mine(miner.address)

    assert node.chain.tip_hash == original_tip
    assert node.chain.height == 0
    assert storage.info().block_count == 1
    assert storage.load_chain().tip_hash == original_tip
    node.close()


def test_invalid_block_never_changes_database(tmp_path) -> None:
    miner = Wallet.create("miner")
    with LocalNode.open_persistent(tmp_path) as node:
        original_tip = node.chain.tip_hash
        candidate = node.chain.build_candidate(
            miner_address=miner.address,
            timestamp=node.chain.tip.header.timestamp + 60,
        )
        invalid = candidate.with_nonce(0)
        while hash_meets_target(invalid.block_hash, invalid.header.difficulty_target):
            invalid = candidate.with_nonce(invalid.header.nonce + 1)

        with pytest.raises(ChainError, match="Proof of Work"):
            node.submit_block(invalid)

        assert node.chain.tip_hash == original_tip

    with SQLiteChainStorage.open(tmp_path, create=False) as storage:
        assert storage.info().block_count == 1
        with pytest.raises(BlockNotFoundError):
            storage.get_block_by_hash(invalid.block_hash)


def test_repeated_identical_storage_write_is_idempotent(tmp_path) -> None:
    miner = Wallet.create("miner")
    with SQLiteChainStorage.open(tmp_path) as storage:
        chain = storage.load_chain()
        candidate_chain = chain.copy()
        block = mine_block(
            candidate_chain.build_candidate(
                miner_address=miner.address,
                timestamp=candidate_chain.tip.header.timestamp + 60,
            )
        )
        update = candidate_chain.add_block(block)

        assert storage.persist_chain_update(candidate_chain, update)
        assert not storage.persist_chain_update(candidate_chain, update)
        assert storage.info().block_count == 2
        assert storage.load_chain().tip_hash == block.block_hash


@pytest.mark.parametrize("field", ["chain_id", "genesis_hash"])
def test_database_rejects_another_chain_identity(tmp_path, field: str) -> None:
    with SQLiteChainStorage.open(tmp_path) as storage:
        path = storage.database_path
    connection = sqlite3.connect(path)
    if field == "chain_id":
        connection.execute("UPDATE chain_metadata SET chain_id = 'other-chain'")
    else:
        connection.execute("UPDATE chain_metadata SET genesis_hash = ?", (b"\xff" * 32,))
    connection.commit()
    connection.close()

    with pytest.raises(NetworkMismatchError):
        SQLiteChainStorage.open(tmp_path, create=False)


def test_undefined_mainnet_is_rejected_before_creating_data(tmp_path) -> None:
    mainnet = NetworkConfig(
        name="mainnet",
        chain_id="ourcoin-mainnet-v1",
        address_hrp="our",
        address_version=0,
    )

    with pytest.raises(UnsupportedNetworkError, match="only.*testnet"):
        SQLiteChainStorage.open(tmp_path, network=mainnet)
    assert not any(tmp_path.iterdir())


def test_testnet_and_fixture_devnet_paths_are_isolated(tmp_path) -> None:
    testnet_genesis = genesis_block().block_hash
    devnet_genesis = b"\x42" * 32
    testnet_path = database_path_for_identity(
        tmp_path, "ourcoin-testnet-v1", testnet_genesis
    )
    devnet_path = database_path_for_identity(
        tmp_path, "ourcoin-devnet-fixture-v1", devnet_genesis
    )

    assert testnet_path != devnet_path
    testnet_path.parent.mkdir(parents=True)
    devnet_path.parent.mkdir(parents=True)
    testnet_path.write_bytes(b"testnet")
    devnet_path.write_bytes(b"devnet")
    assert testnet_path.read_bytes() == b"testnet"
    assert devnet_path.read_bytes() == b"devnet"


def test_unsupported_schema_version_is_rejected(tmp_path) -> None:
    with SQLiteChainStorage.open(tmp_path) as storage:
        path = storage.database_path
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA user_version = 99")
    connection.close()

    with pytest.raises(UnsupportedSchemaVersionError, match="version 99"):
        SQLiteChainStorage.open(tmp_path, create=False)


def test_metadata_schema_version_must_match_runtime(tmp_path) -> None:
    with SQLiteChainStorage.open(tmp_path) as storage:
        path = storage.database_path
    connection = sqlite3.connect(path)
    connection.execute("UPDATE chain_metadata SET schema_version = 2")
    connection.commit()
    connection.close()

    with pytest.raises(UnsupportedSchemaVersionError, match="metadata"):
        SQLiteChainStorage.open(tmp_path, create=False)


def test_durability_and_foreign_key_pragmas_are_enabled(tmp_path) -> None:
    with SQLiteChainStorage.open(tmp_path) as storage:
        connection = storage._connection
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert connection.execute("PRAGMA synchronous").fetchone()[0] == 2
        assert connection.execute("PRAGMA trusted_schema").fetchone()[0] == 0


def test_reindex_rebuilds_identical_derived_state(tmp_path) -> None:
    miner = Wallet.create("miner")
    with LocalNode.open_persistent(tmp_path) as node:
        node.mine(miner.address)
        node.mine(miner.address)
        expected_snapshot = node.chain.state.snapshot()
        expected_tip = node.chain.tip_hash
        expected_supply = node.chain.total_supply_atoms

    with SQLiteChainStorage.open(tmp_path, create=False) as storage:
        storage._connection.execute("DELETE FROM accounts")
        storage._connection.execute("DELETE FROM canonical_chain")
        storage._connection.execute("DELETE FROM confirmed_transactions")
        storage._connection.execute(
            "UPDATE chain_metadata SET canonical_tip_hash = ?, issued_supply_atoms = ?",
            (genesis_block().block_hash, encode_u64(0)),
        )

        report = storage.reindex()

        assert report.tip_hash == expected_tip
        assert storage.load_chain().state.snapshot() == expected_snapshot
        assert storage.info().issued_supply_atoms == expected_supply


def test_corrupt_raw_block_aborts_reindex_without_derived_changes(tmp_path) -> None:
    miner = Wallet.create("miner")
    with LocalNode.open_persistent(tmp_path) as node:
        block = node.mine(miner.address).block
    with SQLiteChainStorage.open(tmp_path, create=False) as storage:
        before = storage.info()
        storage._connection.execute(
            "UPDATE blocks SET reward_bytes = ? WHERE block_hash = ?",
            (b"corrupt", block.block_hash),
        )

        with pytest.raises(StorageIntegrityError, match="canonical bytes"):
            storage.reindex()

        after = storage.info()
        assert after.tip_hash == before.tip_hash
        assert after.issued_supply_atoms == before.issued_supply_atoms
        assert after.block_count == before.block_count


def test_close_is_idempotent_and_closed_storage_rejects_operations(tmp_path) -> None:
    storage = SQLiteChainStorage.open(tmp_path)
    storage.close()
    storage.close()

    with pytest.raises(StorageClosedError):
        storage.info()
