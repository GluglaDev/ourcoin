"""Canonical genesis and Proof-of-Work structure tests."""

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from ourcoin.block import BlockEncodingError, hash_meets_target, transactions_root
from ourcoin.consensus import TESTNET_INITIAL_TARGET, genesis_block
from ourcoin.miner import MiningError, mine_block

VECTOR = Path(__file__).parents[1] / "vectors" / "genesis.json"


def _load_vector() -> dict[str, Any]:
    with VECTOR.open(encoding="utf-8") as handle:
        return json.load(handle)


def test_genesis_matches_public_vector() -> None:
    vector = _load_vector()
    block = genesis_block()

    assert block.header.chain_id == vector["chain_id"]
    assert block.header.height == vector["height"]
    assert block.header.timestamp == vector["timestamp"]
    assert block.header.difficulty_target.to_bytes(32, "big").hex() == vector[
        "difficulty_target_hex"
    ]
    assert block.header.nonce == vector["nonce"]
    assert block.header.miner_address == vector["miner_address"]
    assert block.header.previous_block_hash.hex() == vector["previous_block_hash_hex"]
    assert block.header.transactions_root.hex() == vector["transactions_root_hex"]
    assert block.header.state_root.hex() == vector["state_root_hex"]
    assert block.reward_transaction.to_bytes().hex() == vector["reward_transaction_hex"]
    assert block.reward_transaction.txid.hex() == vector["reward_txid_hex"]
    assert block.header.to_bytes().hex() == vector["header_hex"]
    assert block.block_hash_hex == vector["block_hash_hex"]
    assert hash_meets_target(block.block_hash, TESTNET_INITIAL_TARGET)


def test_miner_finds_first_valid_nonce_from_candidate() -> None:
    genesis = genesis_block()
    candidate = genesis.with_nonce(0)

    mined = mine_block(candidate, max_attempts=100)

    assert mined.header.nonce == genesis.header.nonce
    assert mined.block_hash == genesis.block_hash


def test_miner_respects_attempt_limit() -> None:
    candidate = genesis_block().with_nonce(0)

    with pytest.raises(MiningError, match="no valid nonce"):
        mine_block(candidate, max_attempts=1)


def test_invalid_hash_and_target_representations_are_rejected() -> None:
    assert not hash_meets_target(b"short", TESTNET_INITIAL_TARGET)
    assert not hash_meets_target(b"\x00" * 32, 0)
    with pytest.raises(BlockEncodingError, match="32 bytes"):
        replace(genesis_block().header, previous_block_hash=b"short").to_bytes()


def test_transaction_root_rejects_non_transaction_body_values() -> None:
    genesis = genesis_block()

    with pytest.raises(BlockEncodingError, match="tuple of Transaction"):
        transactions_root(genesis.reward_transaction, (object(),))  # type: ignore[arg-type]
