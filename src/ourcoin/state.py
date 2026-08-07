"""Atomic and deterministic account-state transitions."""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from ourcoin.account import Account
from ourcoin.address import AddressError, decode_address
from ourcoin.config import TESTNET, NetworkConfig
from ourcoin.crypto import sha256_digest
from ourcoin.encoding import U64_MAX, encode_bytes, encode_text, encode_u64
from ourcoin.merkle import merkle_root
from ourcoin.transaction import Transaction, TransactionError, validate_transaction

TXID_LENGTH = 32
STATE_LEAF_DOMAIN = b"OURCOIN:STATE:ACCOUNT:V1"


class StateError(ValueError):
    """Raised when account state or a state-dependent transaction rule is invalid."""


@dataclass(frozen=True, slots=True)
class StateSnapshot:
    """Comparable immutable view used for rollback checks and tests."""

    accounts: tuple[tuple[str, Account], ...]
    confirmed_txids: frozenset[bytes]


class AccountState:
    """A mutable state container that commits only complete valid transitions."""

    def __init__(
        self,
        accounts: Mapping[str, Account] | None = None,
        confirmed_txids: Iterable[bytes] = (),
        *,
        network: NetworkConfig = TESTNET,
    ) -> None:
        self.network = network
        self._accounts = dict(accounts or {})
        self._confirmed_txids = set(confirmed_txids)
        self._validate_initial_state()

    def _validate_initial_state(self) -> None:
        for address, account in self._accounts.items():
            if not isinstance(account, Account):
                raise StateError("state values must be Account instances")
            try:
                decode_address(address, self.network)
            except AddressError as error:
                raise StateError("state contains an invalid network address") from error
        for txid in self._confirmed_txids:
            if type(txid) is not bytes or len(txid) != TXID_LENGTH:
                raise StateError("confirmed transaction IDs must be 32 bytes")

    def snapshot(self) -> StateSnapshot:
        return StateSnapshot(
            accounts=tuple(sorted(self._accounts.items())),
            confirmed_txids=frozenset(self._confirmed_txids),
        )

    def copy(self) -> "AccountState":
        return AccountState(
            self._accounts,
            self._confirmed_txids,
            network=self.network,
        )

    @classmethod
    def from_snapshot(
        cls,
        snapshot: StateSnapshot,
        *,
        network: NetworkConfig = TESTNET,
    ) -> "AccountState":
        if not isinstance(snapshot, StateSnapshot):
            raise StateError("value is not a StateSnapshot")
        return cls(
            dict(snapshot.accounts),
            snapshot.confirmed_txids,
            network=network,
        )

    def state_root(self) -> bytes:
        """Commit to sorted addresses and their balances and nonces."""

        leaves = []
        for address, account in sorted(self._accounts.items()):
            encoded = b"".join(
                (
                    encode_bytes(STATE_LEAF_DOMAIN),
                    encode_text(address),
                    encode_u64(account.balance_atoms),
                    encode_u64(account.nonce),
                )
            )
            leaves.append(sha256_digest(encoded))
        return merkle_root(leaves)

    def get_account(self, address: str) -> Account:
        try:
            decode_address(address, self.network)
        except AddressError as error:
            raise StateError("account lookup uses an invalid network address") from error
        return self._accounts.get(address, Account())

    def contains_transaction(self, txid: bytes) -> bool:
        return txid in self._confirmed_txids

    def credit_reward(self, address: str, amount_atoms: int) -> None:
        """Credit a consensus-authorized reward on a working state copy."""

        try:
            decode_address(address, self.network)
        except AddressError as error:
            raise StateError("reward uses an invalid network address") from error
        if type(amount_atoms) is not int or not 0 <= amount_atoms <= U64_MAX:
            raise StateError("reward amount must be an unsigned 64-bit integer")
        if amount_atoms == 0:
            return
        account = self._accounts.get(address, Account())
        balance = account.balance_atoms + amount_atoms
        if balance > U64_MAX:
            raise StateError("reward would overflow the account balance")
        self._accounts[address] = Account(balance_atoms=balance, nonce=account.nonce)

    def validate(self, transaction: Transaction, *, current_height: int) -> None:
        try:
            validate_transaction(
                transaction,
                current_height=current_height,
                network=self.network,
            )
        except TransactionError as error:
            raise StateError(str(error)) from error

        txid = transaction.txid
        if txid in self._confirmed_txids:
            raise StateError("transaction has already been confirmed")
        sender = self._accounts.get(transaction.sender_address, Account())
        if transaction.nonce != sender.nonce:
            raise StateError("transaction nonce is not the account's exact next nonce")
        if sender.nonce == U64_MAX:
            raise StateError("account nonce is exhausted")
        if sender.balance_atoms < transaction.amount_atoms + transaction.fee_atoms:
            raise StateError("account balance does not cover amount plus fee")

    def _apply_validated(self, transaction: Transaction) -> int:
        sender = self._accounts.get(transaction.sender_address, Account())
        recipient = self._accounts.get(transaction.recipient_address, Account())

        if transaction.sender_address == transaction.recipient_address:
            self._accounts[transaction.sender_address] = Account(
                balance_atoms=sender.balance_atoms - transaction.fee_atoms,
                nonce=sender.nonce + 1,
            )
        else:
            self._accounts[transaction.sender_address] = Account(
                balance_atoms=(
                    sender.balance_atoms - transaction.amount_atoms - transaction.fee_atoms
                ),
                nonce=sender.nonce + 1,
            )
            recipient_balance = recipient.balance_atoms + transaction.amount_atoms
            if recipient_balance > U64_MAX:
                raise StateError("recipient balance exceeds uint64")
            self._accounts[transaction.recipient_address] = Account(
                balance_atoms=recipient_balance,
                nonce=recipient.nonce,
            )
        self._confirmed_txids.add(transaction.txid)
        return transaction.fee_atoms

    def apply_transaction(self, transaction: Transaction, *, current_height: int) -> int:
        return self.apply_transactions((transaction,), current_height=current_height)

    def apply_transactions(
        self,
        transactions: Sequence[Transaction],
        *,
        current_height: int,
    ) -> int:
        """Apply a batch to a copy and commit only if every transaction succeeds."""

        if type(current_height) is not int or not 0 <= current_height <= U64_MAX:
            raise StateError("current height must be an unsigned 64-bit integer")

        working = AccountState(
            self._accounts,
            self._confirmed_txids,
            network=self.network,
        )
        total_fees = 0
        for transaction in transactions:
            working.validate(transaction, current_height=current_height)
            total_fees += working._apply_validated(transaction)
            if total_fees > U64_MAX:
                raise StateError("total transaction fees exceed uint64")

        self._accounts = working._accounts
        self._confirmed_txids = working._confirmed_txids
        return total_fees
