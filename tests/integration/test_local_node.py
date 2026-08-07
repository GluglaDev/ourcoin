import pytest

from ourcoin.cli import CliError, demo_summary, format_atoms, parse_our_amount
from ourcoin.consensus import ATOMS_PER_OUR
from ourcoin.miner import mine_block
from ourcoin.node import LocalNode
from ourcoin.wallet import Wallet


def test_reward_send_mine_and_balances_work_end_to_end() -> None:
    node = LocalNode()
    alice = Wallet.create("alice")
    bob = Wallet.create("bob")
    node.mine(alice.address)

    transaction = node.send(
        alice,
        recipient_address=bob.address,
        amount_atoms=12 * ATOMS_PER_OUR + ATOMS_PER_OUR // 2,
        fee_atoms=ATOMS_PER_OUR // 100,
    )
    assert node.mempool.contains(transaction.txid)
    result = node.mine(bob.address)

    assert result.update.became_active
    assert node.chain.height == 2
    assert len(node.mempool) == 0
    assert node.account(alice.address).balance_atoms == 27 * ATOMS_PER_OUR + 49_000_000
    assert node.account(alice.address).nonce == 1
    assert node.account(bob.address).balance_atoms == 52 * ATOMS_PER_OUR + 51_000_000
    assert node.chain.total_supply_atoms == 80 * ATOMS_PER_OUR


def test_reorganization_returns_still_valid_disconnected_transaction_to_mempool() -> None:
    node = LocalNode()
    alice = Wallet.create("alice")
    bob = Wallet.create("bob")
    charlie = Wallet.create("charlie")
    genesis_hash = node.chain.tip_hash
    genesis_timestamp = node.chain.tip.header.timestamp

    node.mine(alice.address)
    transaction = node.send(
        alice,
        recipient_address=bob.address,
        amount_atoms=ATOMS_PER_OUR,
        fee_atoms=1,
    )
    node.mine(charlie.address)
    assert node.chain.state.contains_transaction(transaction.txid)

    side_one = mine_block(
        node.chain.build_candidate(
            miner_address=alice.address,
            timestamp=genesis_timestamp + 61,
            parent_hash=genesis_hash,
        )
    )
    node.submit_block(side_one)
    side_two = mine_block(
        node.chain.build_candidate(
            miner_address=charlie.address,
            timestamp=genesis_timestamp + 122,
            parent_hash=side_one.block_hash,
        )
    )
    node.submit_block(side_two)
    side_three = mine_block(
        node.chain.build_candidate(
            miner_address=charlie.address,
            timestamp=genesis_timestamp + 183,
            parent_hash=side_two.block_hash,
        )
    )
    update = node.submit_block(side_three)

    assert update.reorganized
    assert not node.chain.state.contains_transaction(transaction.txid)
    assert node.mempool.contains(transaction.txid)


def test_fixed_point_amounts_and_public_demo() -> None:
    assert parse_our_amount("12.50000000") == 1_250_000_000
    assert format_atoms(1_250_000_000) == "12.50000000"

    summary = demo_summary()

    assert summary["height"] == 2
    assert summary["mempool_size"] == 0
    assert summary["total_supply_our"] == "80.00000000"
    assert summary["alice"]["balance_our"] == "27.49000000"
    assert summary["bob"]["balance_our"] == "52.51000000"


@pytest.mark.parametrize("value", ["1e2", "-1", "+1", " 1", "1.000000001", "1."])
def test_fixed_point_parser_rejects_ambiguous_amounts(value: str) -> None:
    with pytest.raises(CliError):
        parse_our_amount(value)
