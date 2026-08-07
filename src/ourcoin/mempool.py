"""Validated pending transactions and fee-prioritized candidate selection."""

from collections import defaultdict

from ourcoin.encoding import U64_MAX
from ourcoin.state import AccountState, StateError
from ourcoin.transaction import Transaction, TransactionError, validate_transaction

MAX_MEMPOOL_TRANSACTIONS = 50_000
MAX_PENDING_PER_SENDER = 64


class MempoolError(ValueError):
    """Raised when a transaction cannot enter the pending pool."""


class Mempool:
    """A nonce-aware in-memory pool; persistence and peer relay arrive later."""

    def __init__(self) -> None:
        self._transactions: dict[bytes, Transaction] = {}
        self._by_sender_nonce: dict[tuple[str, int], bytes] = {}

    def __len__(self) -> int:
        return len(self._transactions)

    def contains(self, txid: bytes) -> bool:
        return txid in self._transactions

    def transactions(self) -> tuple[Transaction, ...]:
        return tuple(
            sorted(
                self._transactions.values(),
                key=lambda transaction: (
                    transaction.sender_address,
                    transaction.nonce,
                    transaction.txid,
                ),
            )
        )

    def _sender_transactions(self, sender_address: str) -> list[Transaction]:
        return sorted(
            (
                transaction
                for transaction in self._transactions.values()
                if transaction.sender_address == sender_address
            ),
            key=lambda transaction: transaction.nonce,
        )

    def next_nonce(self, sender_address: str, state: AccountState) -> int:
        account = state.get_account(sender_address)
        if account.nonce == U64_MAX:
            raise MempoolError("account nonce is exhausted")
        pending = self._sender_transactions(sender_address)
        expected = account.nonce
        for transaction in pending:
            if transaction.nonce != expected:
                break
            if expected == U64_MAX:
                raise MempoolError("account nonce is exhausted")
            expected += 1
        return expected

    def add(
        self,
        transaction: Transaction,
        state: AccountState,
        *,
        execution_height: int,
    ) -> bytes:
        if len(self._transactions) >= MAX_MEMPOOL_TRANSACTIONS:
            raise MempoolError("mempool transaction limit has been reached")
        try:
            validate_transaction(
                transaction,
                current_height=execution_height,
                network=state.network,
            )
        except TransactionError as error:
            raise MempoolError(str(error)) from error

        txid = transaction.txid
        if txid in self._transactions:
            raise MempoolError("transaction is already in the mempool")
        if state.contains_transaction(txid):
            raise MempoolError("transaction has already been confirmed")
        conflict_key = (transaction.sender_address, transaction.nonce)
        if conflict_key in self._by_sender_nonce:
            raise MempoolError("another transaction already uses this sender nonce")

        sender_pending = self._sender_transactions(transaction.sender_address)
        if len(sender_pending) >= MAX_PENDING_PER_SENDER:
            raise MempoolError("sender pending transaction limit has been reached")
        account = state.get_account(transaction.sender_address)
        if account.nonce == U64_MAX:
            raise MempoolError("account nonce is exhausted")
        expected_nonce = account.nonce + len(sender_pending)
        if expected_nonce > U64_MAX:
            raise MempoolError("account nonce is exhausted")
        if transaction.nonce != expected_nonce:
            raise MempoolError("transaction nonce would create a pending gap")
        reserved_atoms = sum(
            pending.amount_atoms + pending.fee_atoms for pending in sender_pending
        )
        pending_total = reserved_atoms + transaction.amount_atoms + transaction.fee_atoms
        if pending_total > account.balance_atoms:
            raise MempoolError("pending transactions exceed the confirmed account balance")

        self._transactions[txid] = transaction
        self._by_sender_nonce[conflict_key] = txid
        return txid

    def remove(self, txid: bytes) -> Transaction | None:
        transaction = self._transactions.pop(txid, None)
        if transaction is not None:
            self._by_sender_nonce.pop((transaction.sender_address, transaction.nonce), None)
        return transaction

    def remove_confirmed(self, transactions: tuple[Transaction, ...]) -> None:
        for transaction in transactions:
            self.remove(transaction.txid)

    def clear(self) -> None:
        self._transactions.clear()
        self._by_sender_nonce.clear()

    def revalidate(
        self,
        state: AccountState,
        *,
        execution_height: int,
    ) -> tuple[Transaction, ...]:
        previous = self.transactions()
        self.clear()
        removed: list[Transaction] = []
        for transaction in previous:
            try:
                self.add(transaction, state, execution_height=execution_height)
            except MempoolError:
                removed.append(transaction)
        return tuple(removed)

    def select(
        self,
        state: AccountState,
        *,
        execution_height: int,
        max_count: int,
    ) -> tuple[Transaction, ...]:
        """Select highest-fee eligible heads while applying them to a working copy."""

        if type(max_count) is not int or max_count < 0:
            raise MempoolError("max_count must be a non-negative integer")
        if max_count == 0:
            return ()

        grouped: dict[str, dict[int, Transaction]] = defaultdict(dict)
        for transaction in self._transactions.values():
            grouped[transaction.sender_address][transaction.nonce] = transaction

        working = state.copy()
        selected: list[Transaction] = []
        blocked_senders: set[str] = set()
        while len(selected) < max_count:
            eligible: list[Transaction] = []
            for sender_address, by_nonce in grouped.items():
                if sender_address in blocked_senders:
                    continue
                nonce = working.get_account(sender_address).nonce
                candidate = by_nonce.get(nonce)
                if candidate is not None:
                    eligible.append(candidate)
            if not eligible:
                break
            eligible.sort(key=lambda transaction: (-transaction.fee_atoms, transaction.txid))

            applied = False
            for transaction in eligible:
                try:
                    working.apply_transaction(transaction, current_height=execution_height)
                except StateError:
                    blocked_senders.add(transaction.sender_address)
                    continue
                selected.append(transaction)
                applied = True
                break
            if not applied:
                break
        return tuple(selected)
