"""M4 branch selection, reorganizations and difficulty adjustment tests."""

from dataclasses import replace

import pytest

from ourcoin.account import Account
from ourcoin.address import address_from_public_key
from ourcoin.block import hash_meets_target
from ourcoin.chain import (
    DIFFICULTY_ADJUSTMENT_INTERVAL,
    Chain,
    ChainError,
    work_for_target,
)
from ourcoin.consensus import (
    ATOMS_PER_OUR,
    INITIAL_REWARD_ATOMS,
    TESTNET_INITIAL_TARGET,
    build_block_candidate,
)
from ourcoin.crypto import generate_private_key, public_key_from_private
from ourcoin.miner import mine_block
from ourcoin.transaction import Transaction, create_signed_transaction


def _key_and_address() -> tuple[bytes, str]:
    private_key = generate_private_key()
    return private_key, address_from_public_key(public_key_from_private(private_key))


def _mine_on(
    chain: Chain,
    parent_hash: bytes,
    miner_address: str,
    *,
    seconds_after_parent: int = 60,
    transactions: tuple[Transaction, ...] = (),
):
    parent = chain.get_block(parent_hash)
    candidate = chain.build_candidate(
        parent_hash=parent_hash,
        miner_address=miner_address,
        transactions=transactions,
        timestamp=parent.header.timestamp + seconds_after_parent,
    )
    return mine_block(candidate)


def _extend_branch(
    chain: Chain,
    parent_hash: bytes,
    miner_address: str,
    count: int,
    *,
    seconds_per_block: int,
) -> bytes:
    tip_hash = parent_hash
    for _ in range(count):
        block = _mine_on(
            chain,
            tip_hash,
            miner_address,
            seconds_after_parent=seconds_per_block,
        )
        chain.add_block(block)
        tip_hash = block.block_hash
    return tip_hash


def test_chain_starts_at_valid_genesis() -> None:
    chain = Chain()

    assert chain.height == 0
    assert chain.tip.header.height == 0
    assert chain.total_supply_atoms == 0
    assert chain.state.snapshot().accounts == ()
    assert len(chain.active_chain()) == 1
    assert chain.cumulative_work == work_for_target(TESTNET_INITIAL_TARGET)


def test_extension_becomes_active_without_reorganization() -> None:
    chain = Chain()
    _key, miner = _key_and_address()
    genesis_hash = chain.tip_hash
    block = _mine_on(chain, genesis_hash, miner)

    update = chain.add_block(block)

    assert update.became_active
    assert not update.reorganized
    assert update.common_ancestor_hash == genesis_hash
    assert update.disconnected == ()
    assert update.connected == (block.block_hash,)
    assert chain.tip_hash == block.block_hash


def test_longer_equal_difficulty_fork_reorganizes_balances_atomically() -> None:
    chain = Chain()
    genesis_hash = chain.tip_hash
    alice_key, alice = _key_and_address()
    _bob_key, bob = _key_and_address()
    _carol_key, carol = _key_and_address()

    main_1 = _mine_on(chain, genesis_hash, alice)
    chain.add_block(main_1)
    transfer = create_signed_transaction(
        alice_key,
        recipient_address=bob,
        amount_atoms=10 * ATOMS_PER_OUR,
        fee_atoms=5,
        nonce=0,
        valid_until_height=100,
    )
    main_2 = _mine_on(chain, main_1.block_hash, alice, transactions=(transfer,))
    chain.add_block(main_2)
    main_state = chain.state.snapshot()

    fork_1 = _mine_on(chain, genesis_hash, carol, seconds_after_parent=61)
    fork_1_update = chain.add_block(fork_1)
    assert not fork_1_update.became_active
    fork_2 = _mine_on(chain, fork_1.block_hash, carol)
    fork_2_update = chain.add_block(fork_2)
    assert not fork_2_update.became_active
    assert chain.tip_hash == main_2.block_hash
    assert chain.state.snapshot() == main_state

    fork_3 = _mine_on(chain, fork_2.block_hash, carol)
    update = chain.add_block(fork_3)

    assert update.became_active
    assert update.reorganized
    assert update.common_ancestor_hash == genesis_hash
    assert update.disconnected == (main_2.block_hash, main_1.block_hash)
    assert update.connected == (fork_1.block_hash, fork_2.block_hash, fork_3.block_hash)
    assert chain.state.get_account(carol) == Account(3 * INITIAL_REWARD_ATOMS, 0)
    assert chain.state.get_account(alice) == Account()
    assert chain.state.get_account(bob) == Account()

    main_3 = _mine_on(chain, main_2.block_hash, alice)
    main_3_update = chain.add_block(main_3)
    assert not main_3_update.became_active
    main_4 = _mine_on(chain, main_3.block_hash, alice)
    reverse_update = chain.add_block(main_4)

    assert reverse_update.reorganized
    assert reverse_update.common_ancestor_hash == genesis_hash
    assert chain.tip_hash == main_4.block_hash
    assert chain.state.get_account(alice) == Account(150 * ATOMS_PER_OUR, 1)
    assert chain.state.get_account(bob) == Account(10 * ATOMS_PER_OUR, 0)
    assert chain.state.get_account(carol) == Account()


def test_invalid_block_does_not_change_tip_or_enter_tree() -> None:
    chain = Chain()
    _key, miner = _key_and_address()
    original_tip = chain.tip_hash
    candidate = chain.build_candidate(
        miner_address=miner,
        timestamp=chain.tip.header.timestamp + 60,
    )
    nonce = 0
    invalid = candidate.with_nonce(nonce)
    while hash_meets_target(invalid.block_hash, invalid.header.difficulty_target):
        nonce += 1
        invalid = candidate.with_nonce(nonce)

    with pytest.raises(ChainError, match="Proof of Work"):
        chain.add_block(invalid)

    assert chain.tip_hash == original_tip
    assert not chain.contains(invalid.block_hash)


def test_unknown_parent_and_duplicate_are_rejected() -> None:
    chain = Chain()
    _key, miner = _key_and_address()
    block = _mine_on(chain, chain.tip_hash, miner)
    chain.add_block(block)

    with pytest.raises(ChainError, match="already known"):
        chain.add_block(block)

    orphan = replace(
        block,
        header=replace(block.header, previous_block_hash=b"\xff" * 32, nonce=0),
    )
    orphan = mine_block(orphan)
    with pytest.raises(ChainError, match="parent is unknown"):
        chain.add_block(orphan)


def test_shorter_branch_with_more_work_wins_after_adjustment() -> None:
    chain = Chain()
    genesis_hash = chain.tip_hash
    _slow_key, slow_miner = _key_and_address()
    _fast_key, fast_miner = _key_and_address()

    slow_119 = _extend_branch(
        chain,
        genesis_hash,
        slow_miner,
        DIFFICULTY_ADJUSTMENT_INTERVAL - 1,
        seconds_per_block=240,
    )
    assert chain.expected_target_for_child(slow_119) == TESTNET_INITIAL_TARGET * 4
    slow_121 = _extend_branch(
        chain,
        slow_119,
        slow_miner,
        2,
        seconds_per_block=240,
    )
    assert chain.height == 121

    fast_119 = _extend_branch(
        chain,
        genesis_hash,
        fast_miner,
        DIFFICULTY_ADJUSTMENT_INTERVAL - 1,
        seconds_per_block=15,
    )
    assert chain.expected_target_for_child(fast_119) == TESTNET_INITIAL_TARGET // 4
    fast_parent = chain.get_block(fast_119)
    wrong_target = build_block_candidate(
        fast_parent,
        chain.get_state(fast_119),
        chain.get_record(fast_119).total_supply_atoms,
        miner_address=fast_miner,
        timestamp=fast_parent.header.timestamp + 15,
    )
    wrong_target = mine_block(wrong_target)
    with pytest.raises(ChainError, match="expected target"):
        chain.add_block(wrong_target)
    assert not chain.contains(wrong_target.block_hash)

    fast_120_block = _mine_on(
        chain,
        fast_119,
        fast_miner,
        seconds_after_parent=15,
    )
    update = chain.add_block(fast_120_block)

    assert update.reorganized
    assert chain.tip_hash == fast_120_block.block_hash
    assert chain.height == 120
    assert chain.get_record(fast_120_block.block_hash).cumulative_work > chain.get_record(
        slow_121
    ).cumulative_work


@pytest.mark.parametrize("target", [0, -1, True, (1 << 256)])
def test_work_rejects_invalid_target(target: int) -> None:
    with pytest.raises(ChainError, match="work target"):
        work_for_target(target)
