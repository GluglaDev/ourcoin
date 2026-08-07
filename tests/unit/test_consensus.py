"""M3 emission, block validation and atomic execution tests."""

from dataclasses import replace

import pytest

from ourcoin.account import Account
from ourcoin.address import address_from_public_key
from ourcoin.block import Block, hash_meets_target, transactions_root
from ourcoin.consensus import (
    ATOMS_PER_OUR,
    INITIAL_REWARD_ATOMS,
    MAX_BLOCK_TRANSACTIONS,
    MAX_SUPPLY_ATOMS,
    BlockError,
    block_subsidy,
    build_block_candidate,
    genesis_block,
    validate_and_execute_block,
)
from ourcoin.crypto import generate_private_key, public_key_from_private
from ourcoin.encoding import U64_MAX
from ourcoin.miner import mine_block
from ourcoin.state import AccountState
from ourcoin.transaction import Transaction, create_signed_transaction


def _key_and_address() -> tuple[bytes, str]:
    private_key = generate_private_key()
    return private_key, address_from_public_key(public_key_from_private(private_key))


def _mined_child(
    parent: Block,
    state: AccountState,
    supply: int,
    miner_address: str,
    *,
    transactions: tuple[Transaction, ...] = (),
) -> Block:
    candidate = build_block_candidate(
        parent,
        state,
        supply,
        miner_address=miner_address,
        transactions=transactions,
        timestamp=parent.header.timestamp + 60,
    )
    return mine_block(candidate)


@pytest.mark.parametrize(
    ("height", "expected"),
    [
        (0, 0),
        (1, 40 * ATOMS_PER_OUR),
        (499_999, 40 * ATOMS_PER_OUR),
        (500_000, 32 * ATOMS_PER_OUR),
        (999_999, 32 * ATOMS_PER_OUR),
        (1_000_000, 2_560_000_000),
    ],
)
def test_reward_boundaries(height: int, expected: int) -> None:
    assert block_subsidy(height, 0) == expected


def test_reward_is_capped_and_eventually_reaches_zero() -> None:
    assert block_subsidy(1, MAX_SUPPLY_ATOMS - 7) == 7
    assert block_subsidy(1, MAX_SUPPLY_ATOMS) == 0
    assert block_subsidy(U64_MAX, 0) == 0


def test_genesis_validation_keeps_empty_state_and_supply() -> None:
    state = AccountState()
    before = state.snapshot()

    execution = validate_and_execute_block(genesis_block(), None, state, 0)

    assert execution.state.snapshot() == before
    assert state.snapshot() == before
    assert execution.total_supply_atoms == 0
    assert execution.subsidy_atoms == 0


def test_first_mined_block_credits_subsidy_without_mutating_input() -> None:
    _private_key, miner = _key_and_address()
    state = AccountState()
    before = state.snapshot()
    block = _mined_child(genesis_block(), state, 0, miner)

    execution = validate_and_execute_block(block, genesis_block(), state, 0)

    assert state.snapshot() == before
    assert execution.total_supply_atoms == INITIAL_REWARD_ATOMS
    assert execution.fees_atoms == 0
    assert execution.state.get_account(miner) == Account(INITIAL_REWARD_ATOMS, 0)


def test_transaction_fee_and_next_subsidy_are_paid_to_miner() -> None:
    sender_key, sender = _key_and_address()
    state = AccountState()
    first = _mined_child(genesis_block(), state, 0, sender)
    first_execution = validate_and_execute_block(first, genesis_block(), state, 0)
    recipient_key, recipient = _key_and_address()
    del recipient_key
    _second_miner_key, second_miner = _key_and_address()
    transaction = create_signed_transaction(
        sender_key,
        recipient_address=recipient,
        amount_atoms=10 * ATOMS_PER_OUR,
        fee_atoms=3,
        nonce=0,
        valid_until_height=100,
    )
    second = _mined_child(
        first,
        first_execution.state,
        first_execution.total_supply_atoms,
        second_miner,
        transactions=(transaction,),
    )

    execution = validate_and_execute_block(
        second,
        first,
        first_execution.state,
        first_execution.total_supply_atoms,
    )

    assert execution.fees_atoms == 3
    assert execution.subsidy_atoms == INITIAL_REWARD_ATOMS
    assert execution.total_supply_atoms == 2 * INITIAL_REWARD_ATOMS
    assert execution.state.get_account(sender) == Account(3_000_000_000 - 3, 1)
    assert execution.state.get_account(recipient) == Account(10 * ATOMS_PER_OUR, 0)
    assert execution.state.get_account(second_miner) == Account(INITIAL_REWARD_ATOMS + 3, 0)


def test_invalid_previous_hash_is_rejected_without_mutation() -> None:
    _key, miner = _key_and_address()
    state = AccountState()
    valid = _mined_child(genesis_block(), state, 0, miner)
    invalid = replace(
        valid,
        header=replace(valid.header, previous_block_hash=b"\x00" * 32, nonce=0),
    )
    invalid = mine_block(invalid)
    before = state.snapshot()

    with pytest.raises(BlockError, match="previous block hash"):
        validate_and_execute_block(invalid, genesis_block(), state, 0)

    assert state.snapshot() == before


def test_false_reward_is_rejected_without_mutation() -> None:
    _key, miner = _key_and_address()
    state = AccountState()
    valid = _mined_child(genesis_block(), state, 0, miner)
    false_reward = replace(
        valid.reward_transaction,
        amount_atoms=valid.reward_transaction.amount_atoms + 1,
    )
    invalid = replace(
        valid,
        reward_transaction=false_reward,
        header=replace(
            valid.header,
            transactions_root=transactions_root(false_reward, valid.transactions),
            nonce=0,
        ),
    )
    invalid = mine_block(invalid)
    before = state.snapshot()

    with pytest.raises(BlockError, match="subsidy plus fees"):
        validate_and_execute_block(invalid, genesis_block(), state, 0)

    assert state.snapshot() == before


@pytest.mark.parametrize(
    ("field", "message"),
    [("transactions_root", "transactions root"), ("state_root", "state root")],
)
def test_tampered_roots_are_rejected(field: str, message: str) -> None:
    _key, miner = _key_and_address()
    state = AccountState()
    valid = _mined_child(genesis_block(), state, 0, miner)
    invalid_header = replace(valid.header, **{field: b"\xff" * 32}, nonce=0)
    invalid = mine_block(replace(valid, header=invalid_header))

    with pytest.raises(BlockError, match=message):
        validate_and_execute_block(invalid, genesis_block(), state, 0)


def test_insufficient_proof_of_work_is_rejected() -> None:
    _key, miner = _key_and_address()
    state = AccountState()
    candidate = build_block_candidate(
        genesis_block(),
        state,
        0,
        miner_address=miner,
        timestamp=genesis_block().header.timestamp + 60,
    )
    nonce = 0
    invalid = candidate.with_nonce(nonce)
    while hash_meets_target(invalid.block_hash, invalid.header.difficulty_target):
        nonce += 1
        invalid = candidate.with_nonce(nonce)

    with pytest.raises(BlockError, match="Proof of Work"):
        validate_and_execute_block(invalid, genesis_block(), state, 0)


def test_invalid_transaction_in_block_does_not_mutate_input_state() -> None:
    sender_key, sender = _key_and_address()
    state = AccountState()
    first = _mined_child(genesis_block(), state, 0, sender)
    execution = validate_and_execute_block(first, genesis_block(), state, 0)
    _recipient_key, recipient = _key_and_address()
    transaction = create_signed_transaction(
        sender_key,
        recipient_address=recipient,
        amount_atoms=1,
        fee_atoms=0,
        nonce=0,
        valid_until_height=100,
    )
    valid = _mined_child(
        first,
        execution.state,
        execution.total_supply_atoms,
        sender,
        transactions=(transaction,),
    )
    invalid_transaction = replace(transaction, signature=b"\x00" * 64)
    invalid = replace(
        valid,
        transactions=(invalid_transaction,),
        header=replace(
            valid.header,
            transactions_root=transactions_root(
                valid.reward_transaction,
                (invalid_transaction,),
            ),
            nonce=0,
        ),
    )
    invalid = mine_block(invalid)
    before = execution.state.snapshot()

    with pytest.raises(BlockError, match="signature is invalid"):
        validate_and_execute_block(
            invalid,
            first,
            execution.state,
            execution.total_supply_atoms,
        )

    assert execution.state.snapshot() == before


def test_candidate_rejects_excess_transaction_count() -> None:
    sender_key, sender = _key_and_address()
    _recipient_key, recipient = _key_and_address()
    transaction = create_signed_transaction(
        sender_key,
        recipient_address=recipient,
        amount_atoms=1,
        fee_atoms=0,
        nonce=0,
        valid_until_height=100,
    )

    with pytest.raises(BlockError, match="too many"):
        build_block_candidate(
            genesis_block(),
            AccountState({sender: Account(1, 0)}),
            1,
            miner_address=sender,
            transactions=(transaction,) * (MAX_BLOCK_TRANSACTIONS + 1),
            timestamp=genesis_block().header.timestamp + 60,
        )
