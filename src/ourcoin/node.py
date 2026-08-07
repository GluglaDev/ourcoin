"""Single-process M5 node that joins the chain, mempool, wallet and miner."""

from dataclasses import dataclass

from ourcoin.account import Account
from ourcoin.block import Block
from ourcoin.chain import TARGET_BLOCK_SECONDS, Chain, ChainUpdate
from ourcoin.config import TESTNET, NetworkConfig
from ourcoin.consensus import MAX_BLOCK_TRANSACTIONS
from ourcoin.encoding import U64_MAX
from ourcoin.mempool import Mempool, MempoolError
from ourcoin.miner import mine_block
from ourcoin.transaction import Transaction
from ourcoin.wallet import Wallet, WalletError

DEFAULT_TRANSACTION_LIFETIME_BLOCKS = 100


class NodeError(ValueError):
    """Raised when a local node operation cannot be completed."""


@dataclass(frozen=True, slots=True)
class MiningResult:
    block: Block
    update: ChainUpdate


class LocalNode:
    """An in-memory node for deterministic local workflows and integration tests."""

    def __init__(self, *, network: NetworkConfig = TESTNET) -> None:
        self.chain = Chain(network=network)
        self.mempool = Mempool()

    @property
    def network(self) -> NetworkConfig:
        return self.chain.network

    def account(self, address: str) -> Account:
        return self.chain.state.get_account(address)

    def submit_transaction(self, transaction: Transaction) -> bytes:
        try:
            return self.mempool.add(
                transaction,
                self.chain.state,
                execution_height=self.chain.height + 1,
            )
        except MempoolError as error:
            raise NodeError(str(error)) from error

    def send(
        self,
        wallet: Wallet,
        *,
        recipient_address: str,
        amount_atoms: int,
        fee_atoms: int,
        valid_for_blocks: int = DEFAULT_TRANSACTION_LIFETIME_BLOCKS,
    ) -> Transaction:
        if not isinstance(wallet, Wallet):
            raise NodeError("sender must be a Wallet")
        if wallet.network != self.network:
            raise NodeError("wallet belongs to another network")
        if type(valid_for_blocks) is not int or valid_for_blocks < 0:
            raise NodeError("transaction lifetime must be a non-negative integer")
        execution_height = self.chain.height + 1
        valid_until_height = execution_height + valid_for_blocks
        if valid_until_height > U64_MAX:
            raise NodeError("transaction lifetime exceeds the height range")
        nonce = self.mempool.next_nonce(wallet.address, self.chain.state)
        try:
            transaction = wallet.create_transaction(
                recipient_address=recipient_address,
                amount_atoms=amount_atoms,
                fee_atoms=fee_atoms,
                nonce=nonce,
                valid_until_height=valid_until_height,
            )
        except (ValueError, WalletError) as error:
            raise NodeError(str(error)) from error
        self.submit_transaction(transaction)
        return transaction

    def submit_block(self, block: Block) -> ChainUpdate:
        """Add a block and reconcile pending transactions after an active-tip change."""

        update = self.chain.add_block(block)
        if not update.became_active:
            return update

        connected_transactions = tuple(
            transaction
            for block_hash in update.connected
            for transaction in self.chain.get_block(block_hash).transactions
        )
        disconnected_transactions = tuple(
            transaction
            for block_hash in reversed(update.disconnected)
            for transaction in self.chain.get_block(block_hash).transactions
        )
        self.mempool.remove_confirmed(connected_transactions)
        active_state = self.chain.state
        execution_height = self.chain.height + 1
        self.mempool.revalidate(active_state, execution_height=execution_height)

        for transaction in disconnected_transactions:
            if active_state.contains_transaction(transaction.txid):
                continue
            if self.mempool.contains(transaction.txid):
                continue
            try:
                self.mempool.add(
                    transaction,
                    active_state,
                    execution_height=execution_height,
                )
            except MempoolError:
                continue
        return update

    def mine(
        self,
        miner_address: str,
        *,
        timestamp: int | None = None,
        max_transactions: int = MAX_BLOCK_TRANSACTIONS,
        max_attempts: int = 1_000_000,
    ) -> MiningResult:
        if (
            type(max_transactions) is not int
            or not 0 <= max_transactions <= MAX_BLOCK_TRANSACTIONS
        ):
            raise NodeError("block transaction limit is outside the allowed range")
        selected = self.mempool.select(
            self.chain.state,
            execution_height=self.chain.height + 1,
            max_count=max_transactions,
        )
        candidate_timestamp = (
            self.chain.tip.header.timestamp + TARGET_BLOCK_SECONDS
            if timestamp is None
            else timestamp
        )
        candidate = self.chain.build_candidate(
            miner_address=miner_address,
            transactions=selected,
            timestamp=candidate_timestamp,
        )
        block = mine_block(candidate, max_attempts=max_attempts)
        return MiningResult(block=block, update=self.submit_block(block))


__all__ = ["DEFAULT_TRANSACTION_LIFETIME_BLOCKS", "LocalNode", "MiningResult", "NodeError"]
