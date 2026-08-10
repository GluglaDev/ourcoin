"""Single-process node joining chain, optional SQLite storage, mempool and miner."""

from dataclasses import dataclass
from pathlib import Path

from ourcoin.account import Account
from ourcoin.block import Block
from ourcoin.chain import TARGET_BLOCK_SECONDS, Chain, ChainUpdate
from ourcoin.config import TESTNET, NetworkConfig
from ourcoin.consensus import MAX_BLOCK_TRANSACTIONS
from ourcoin.encoding import U64_MAX
from ourcoin.mempool import Mempool, MempoolError
from ourcoin.miner import mine_block
from ourcoin.storage import SQLiteChainStorage
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
    """A local node with optional transactional SQLite persistence."""

    def __init__(
        self,
        *,
        network: NetworkConfig = TESTNET,
        storage: SQLiteChainStorage | None = None,
    ) -> None:
        if storage is not None and storage.network != network:
            raise NodeError("storage belongs to another network")
        self._storage = storage
        self.chain = storage.load_chain() if storage is not None else Chain(network=network)
        self.mempool = Mempool()

    @classmethod
    def open_persistent(
        cls,
        base_data_dir: str | Path = Path("data"),
        *,
        network: NetworkConfig = TESTNET,
        create: bool = True,
    ) -> "LocalNode":
        storage = SQLiteChainStorage.open(base_data_dir, network=network, create=create)
        try:
            return cls(network=network, storage=storage)
        except Exception:
            storage.close()
            raise

    @property
    def network(self) -> NetworkConfig:
        return self.chain.network

    @property
    def is_persistent(self) -> bool:
        return self._storage is not None

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

        if self._storage is None:
            update = self.chain.add_block(block)
        else:
            candidate_chain = self.chain.copy()
            update = candidate_chain.add_block(block)
            self._storage.persist_chain_update(candidate_chain, update)
            self.chain = candidate_chain
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

    def close(self) -> None:
        if self._storage is not None:
            self._storage.close()

    def __enter__(self) -> "LocalNode":
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        self.close()


__all__ = ["DEFAULT_TRANSACTION_LIFETIME_BLOCKS", "LocalNode", "MiningResult", "NodeError"]
