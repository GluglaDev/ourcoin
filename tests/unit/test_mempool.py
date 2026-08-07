import pytest

from ourcoin.account import Account
from ourcoin.encoding import U64_MAX
from ourcoin.mempool import Mempool, MempoolError
from ourcoin.state import AccountState
from ourcoin.wallet import Wallet


def funded_state(*wallets: Wallet, balance_atoms: int = 1_000_000) -> AccountState:
    return AccountState(
        {wallet.address: Account(balance_atoms=balance_atoms) for wallet in wallets}
    )


def signed_transfer(
    sender: Wallet,
    recipient: Wallet,
    *,
    nonce: int = 0,
    amount_atoms: int = 100,
    fee_atoms: int = 1,
    valid_until_height: int = 100,
):
    return sender.create_transaction(
        recipient_address=recipient.address,
        amount_atoms=amount_atoms,
        fee_atoms=fee_atoms,
        nonce=nonce,
        valid_until_height=valid_until_height,
    )


def test_mempool_rejects_duplicate_nonce_conflict_and_gap() -> None:
    alice = Wallet.create("alice")
    bob = Wallet.create("bob")
    state = funded_state(alice)
    pool = Mempool()
    first = signed_transfer(alice, bob)

    assert pool.add(first, state, execution_height=1) == first.txid
    assert pool.next_nonce(alice.address, state) == 1
    with pytest.raises(MempoolError, match="already in"):
        pool.add(first, state, execution_height=1)

    conflict = signed_transfer(alice, bob, amount_atoms=101)
    with pytest.raises(MempoolError, match="sender nonce"):
        pool.add(conflict, state, execution_height=1)

    gap = signed_transfer(alice, bob, nonce=2)
    with pytest.raises(MempoolError, match="pending gap"):
        pool.add(gap, state, execution_height=1)


def test_mempool_reserves_confirmed_balance_across_pending_transactions() -> None:
    alice = Wallet.create("alice")
    bob = Wallet.create("bob")
    state = funded_state(alice, balance_atoms=150)
    pool = Mempool()
    pool.add(
        signed_transfer(alice, bob, amount_atoms=100, fee_atoms=1),
        state,
        execution_height=1,
    )

    overspend = signed_transfer(alice, bob, nonce=1, amount_atoms=49, fee_atoms=1)
    with pytest.raises(MempoolError, match="confirmed account balance"):
        pool.add(overspend, state, execution_height=1)


def test_selection_prioritizes_fees_without_skipping_sender_nonces() -> None:
    alice = Wallet.create("alice")
    bob = Wallet.create("bob")
    recipient = Wallet.create("recipient")
    state = funded_state(alice, bob)
    pool = Mempool()
    alice_first = signed_transfer(alice, recipient, fee_atoms=1)
    alice_second = signed_transfer(alice, recipient, nonce=1, fee_atoms=100)
    bob_first = signed_transfer(bob, recipient, fee_atoms=50)
    for transaction in (alice_first, alice_second, bob_first):
        pool.add(transaction, state, execution_height=1)

    selected = pool.select(state, execution_height=1, max_count=3)

    assert selected == (bob_first, alice_first, alice_second)


def test_revalidate_removes_expired_transactions() -> None:
    alice = Wallet.create("alice")
    bob = Wallet.create("bob")
    state = funded_state(alice)
    pool = Mempool()
    transaction = signed_transfer(alice, bob, valid_until_height=1)
    pool.add(transaction, state, execution_height=1)

    assert pool.revalidate(state, execution_height=2) == (transaction,)
    assert len(pool) == 0


def test_confirmed_transaction_cannot_enter_mempool() -> None:
    alice = Wallet.create("alice")
    bob = Wallet.create("bob")
    transaction = signed_transfer(alice, bob)
    state = funded_state(alice)
    state.apply_transaction(transaction, current_height=1)

    with pytest.raises(MempoolError, match="already been confirmed"):
        Mempool().add(transaction, state, execution_height=2)


def test_mempool_rejects_exhausted_account_nonce() -> None:
    alice = Wallet.create("alice")
    bob = Wallet.create("bob")
    state = AccountState(
        {alice.address: Account(balance_atoms=1_000, nonce=U64_MAX)}
    )
    transaction = signed_transfer(alice, bob, nonce=U64_MAX)
    pool = Mempool()

    with pytest.raises(MempoolError, match="nonce is exhausted"):
        pool.next_nonce(alice.address, state)
    with pytest.raises(MempoolError, match="nonce is exhausted"):
        pool.add(transaction, state, execution_height=1)
