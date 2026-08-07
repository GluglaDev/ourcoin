"""State-dependent account and transaction tests."""

from dataclasses import replace

import pytest

from ourcoin.account import Account, AccountError
from ourcoin.address import address_from_public_key
from ourcoin.crypto import generate_private_key, public_key_from_private
from ourcoin.encoding import U64_MAX
from ourcoin.state import AccountState, StateError
from ourcoin.transaction import Transaction, create_signed_transaction


def _new_address() -> str:
    return address_from_public_key(public_key_from_private(generate_private_key()))


def _signed_transfer(
    private_key: bytes,
    recipient: str,
    *,
    amount: int = 100,
    fee: int = 3,
    nonce: int = 0,
) -> Transaction:
    return create_signed_transaction(
        private_key,
        recipient_address=recipient,
        amount_atoms=amount,
        fee_atoms=fee,
        nonce=nonce,
        valid_until_height=100,
    )


def _funded_sender(balance: int = 1_000, nonce: int = 0) -> tuple[bytes, str, AccountState]:
    private_key = generate_private_key()
    address = address_from_public_key(public_key_from_private(private_key))
    state = AccountState({address: Account(balance_atoms=balance, nonce=nonce)})
    return private_key, address, state


def test_transfer_updates_balances_nonce_and_replay_index() -> None:
    private_key, sender, state = _funded_sender()
    recipient = _new_address()
    transaction = _signed_transfer(private_key, recipient)

    fee = state.apply_transaction(transaction, current_height=1)

    assert fee == 3
    assert state.get_account(sender) == Account(balance_atoms=897, nonce=1)
    assert state.get_account(recipient) == Account(balance_atoms=100, nonce=0)
    assert state.contains_transaction(transaction.txid)


def test_replay_is_rejected_without_mutation() -> None:
    private_key, _sender, state = _funded_sender()
    transaction = _signed_transfer(private_key, _new_address())
    state.apply_transaction(transaction, current_height=1)
    before = state.snapshot()

    with pytest.raises(StateError, match="already been confirmed"):
        state.apply_transaction(transaction, current_height=1)

    assert state.snapshot() == before


@pytest.mark.parametrize("nonce", [1, 2])
def test_repeated_or_skipped_nonce_is_rejected(nonce: int) -> None:
    private_key, _sender, state = _funded_sender(nonce=0)
    transaction = _signed_transfer(private_key, _new_address(), nonce=nonce)
    before = state.snapshot()

    with pytest.raises(StateError, match="exact next nonce"):
        state.apply_transaction(transaction, current_height=1)

    assert state.snapshot() == before


def test_insufficient_balance_is_rejected_without_mutation() -> None:
    private_key, _sender, state = _funded_sender(balance=102)
    transaction = _signed_transfer(private_key, _new_address(), amount=100, fee=3)
    before = state.snapshot()

    with pytest.raises(StateError, match="does not cover"):
        state.apply_transaction(transaction, current_height=1)

    assert state.snapshot() == before


def test_batch_is_atomic_when_later_transaction_fails() -> None:
    private_key, _sender, state = _funded_sender()
    recipient = _new_address()
    first = _signed_transfer(private_key, recipient, nonce=0)
    skipped_nonce = _signed_transfer(private_key, recipient, nonce=2)
    before = state.snapshot()

    with pytest.raises(StateError, match="exact next nonce"):
        state.apply_transactions((first, skipped_nonce), current_height=1)

    assert state.snapshot() == before


def test_valid_batch_uses_updated_working_nonce() -> None:
    private_key, sender, state = _funded_sender()
    recipient = _new_address()
    first = _signed_transfer(private_key, recipient, amount=100, fee=3, nonce=0)
    second = _signed_transfer(private_key, recipient, amount=50, fee=2, nonce=1)

    total_fees = state.apply_transactions((first, second), current_height=1)

    assert total_fees == 5
    assert state.get_account(sender) == Account(balance_atoms=845, nonce=2)
    assert state.get_account(recipient) == Account(balance_atoms=150, nonce=0)


def test_transfer_to_self_only_burns_fee_and_increments_nonce() -> None:
    private_key, sender, state = _funded_sender()
    transaction = _signed_transfer(private_key, sender, amount=200, fee=4)

    state.apply_transaction(transaction, current_height=1)

    assert state.get_account(sender) == Account(balance_atoms=996, nonce=1)


def test_invalid_signature_does_not_mutate_state() -> None:
    private_key, _sender, state = _funded_sender()
    transaction = _signed_transfer(private_key, _new_address())
    invalid = replace(transaction, signature=b"\x00" * 64)
    before = state.snapshot()

    with pytest.raises(StateError, match="signature is invalid"):
        state.apply_transaction(invalid, current_height=1)

    assert state.snapshot() == before


def test_nonce_exhaustion_is_rejected() -> None:
    private_key, _sender, state = _funded_sender(nonce=U64_MAX)
    transaction = _signed_transfer(private_key, _new_address(), nonce=U64_MAX)

    with pytest.raises(StateError, match="exhausted"):
        state.apply_transaction(transaction, current_height=1)


@pytest.mark.parametrize(
    ("balance", "nonce"),
    [(-1, 0), (U64_MAX + 1, 0), (0, -1), (0, U64_MAX + 1)],
)
def test_account_rejects_out_of_range_values(balance: int, nonce: int) -> None:
    with pytest.raises(AccountError):
        Account(balance_atoms=balance, nonce=nonce)


def test_state_rejects_malformed_confirmed_txid() -> None:
    with pytest.raises(StateError, match="32 bytes"):
        AccountState(confirmed_txids=(b"short",))


def test_empty_batch_still_rejects_invalid_height() -> None:
    state = AccountState()

    with pytest.raises(StateError, match="current height"):
        state.apply_transactions((), current_height=-1)


def test_batch_rejects_total_fee_overflow_without_mutation() -> None:
    first_key = generate_private_key()
    second_key = generate_private_key()
    first_sender = address_from_public_key(public_key_from_private(first_key))
    second_sender = address_from_public_key(public_key_from_private(second_key))
    state = AccountState(
        {
            first_sender: Account(balance_atoms=U64_MAX),
            second_sender: Account(balance_atoms=U64_MAX),
        }
    )
    first = _signed_transfer(first_key, _new_address(), amount=1, fee=U64_MAX - 1)
    second = _signed_transfer(second_key, _new_address(), amount=1, fee=U64_MAX - 1)
    before = state.snapshot()

    with pytest.raises(StateError, match="total transaction fees"):
        state.apply_transactions((first, second), current_height=1)

    assert state.snapshot() == before


def test_state_root_is_order_independent_and_commits_to_nonce() -> None:
    first = _new_address()
    second = _new_address()
    first_state = AccountState({first: Account(10, 0), second: Account(20, 1)})
    second_state = AccountState({second: Account(20, 1), first: Account(10, 0)})
    changed_nonce = AccountState({first: Account(10, 1), second: Account(20, 1)})

    assert first_state.state_root() == second_state.state_root()
    assert first_state.state_root() != changed_nonce.state_root()
    assert AccountState().state_root().hex() == (
        "6e340b9cffb37a989ca544e6bb780a2c78901d3fb33738768511a30617afa01d"
    )
