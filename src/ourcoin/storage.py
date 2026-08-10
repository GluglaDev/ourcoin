"""Transactional SQLite persistence for validated OurCoin chain data."""

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from ourcoin.account import Account
from ourcoin.block import (
    Block,
    BlockEncodingError,
    BlockHeader,
    RewardTransaction,
    transactions_root,
)
from ourcoin.chain import BlockRecord, Chain, ChainError, ChainUpdate
from ourcoin.config import TESTNET, NetworkConfig
from ourcoin.encoding import U64_MAX, CanonicalReader, EncodingError, encode_u64
from ourcoin.transaction import Transaction, TransactionError

SCHEMA_VERSION = 1
DATABASE_FILENAME = "blockchain.sqlite3"
CUMULATIVE_WORK_BYTES = 40
SQLITE_BUSY_TIMEOUT_MS = 5_000
SAFE_CHAIN_ID = re.compile(r"[A-Za-z0-9._-]{1,64}\Z")


class StorageError(RuntimeError):
    """Base class for persistent-chain failures."""


class StorageNotFoundError(StorageError):
    """Raised when an existing database was requested but does not exist."""


class StorageClosedError(StorageError):
    """Raised when a closed storage object is used."""


class StorageIntegrityError(StorageError):
    """Raised when persisted bytes or derived state are inconsistent."""


class UnsupportedSchemaVersionError(StorageError):
    """Raised when the database schema is not supported by this release."""


class NetworkMismatchError(StorageError):
    """Raised when database identity differs from the requested chain."""


class UnsupportedNetworkError(StorageError):
    """Raised when M6 is asked to run a network other than the defined testnet."""


class BlockNotFoundError(StorageError):
    """Raised when a requested persisted block does not exist."""


@dataclass(frozen=True, slots=True)
class StorageInfo:
    schema_version: int
    chain_id: str
    genesis_hash: bytes
    database_path: Path
    height: int
    tip_hash: bytes
    cumulative_work: int
    issued_supply_atoms: int
    block_count: int
    account_count: int


@dataclass(frozen=True, slots=True)
class StorageValidationReport:
    valid: bool
    block_count: int
    account_count: int
    height: int
    tip_hash: bytes


@dataclass(frozen=True, slots=True)
class StorageReindexReport:
    block_count: int
    account_count: int
    height: int
    tip_hash: bytes


def database_path_for_identity(
    base_data_dir: str | Path,
    chain_id: str,
    genesis_hash: bytes,
) -> Path:
    """Resolve an identity-isolated database path without activating a network."""

    if type(chain_id) is not str or SAFE_CHAIN_ID.fullmatch(chain_id) is None:
        raise StorageError("chain_id is not safe for a data-directory name")
    if type(genesis_hash) is not bytes or len(genesis_hash) != 32:
        raise StorageError("genesis hash must be exactly 32 bytes")
    return Path(base_data_dir) / chain_id / genesis_hash.hex() / DATABASE_FILENAME


def _require_supported_network(network: NetworkConfig) -> None:
    if network != TESTNET:
        raise UnsupportedNetworkError("M6 supports only the defined OurCoin testnet")


def _encode_work(value: int) -> bytes:
    if type(value) is not int or value <= 0 or value.bit_length() > CUMULATIVE_WORK_BYTES * 8:
        raise StorageIntegrityError("cumulative work is outside the storage range")
    return value.to_bytes(CUMULATIVE_WORK_BYTES, "big")


def _decode_u64(value: object, field_name: str) -> int:
    if type(value) is not bytes or len(value) != 8:
        raise StorageIntegrityError(f"{field_name} is not a canonical u64 BLOB")
    try:
        reader = CanonicalReader(value)
        decoded = reader.read_u64()
        reader.ensure_finished()
    except EncodingError as error:
        raise StorageIntegrityError(f"{field_name} is not a canonical u64 BLOB") from error
    return decoded


def _decode_work(value: object) -> int:
    if type(value) is not bytes or len(value) != CUMULATIVE_WORK_BYTES:
        raise StorageIntegrityError("cumulative work is not a 40-byte BLOB")
    decoded = int.from_bytes(value, "big")
    if decoded <= 0:
        raise StorageIntegrityError("cumulative work must be positive")
    return decoded


def _exact_blob(value: object, length: int, field_name: str) -> bytes:
    if type(value) is not bytes or len(value) != length:
        raise StorageIntegrityError(f"{field_name} must be exactly {length} bytes")
    return value


class SQLiteChainStorage:
    """Own one SQLite connection for one identity-bound validated blockchain."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        database_path: Path,
        network: NetworkConfig,
        genesis_hash: bytes,
    ) -> None:
        self._connection = connection
        self.database_path = database_path
        self.network = network
        self.genesis_hash = genesis_hash
        self._closed = False

    @classmethod
    def open(
        cls,
        base_data_dir: str | Path,
        *,
        network: NetworkConfig = TESTNET,
        create: bool = True,
    ) -> "SQLiteChainStorage":
        _require_supported_network(network)
        genesis_hash = Chain(network=network).tip_hash
        path = database_path_for_identity(base_data_dir, network.chain_id, genesis_hash)
        existed = path.exists()
        if not existed and not create:
            raise StorageNotFoundError("blockchain database does not exist")
        if create:
            path.parent.mkdir(parents=True, exist_ok=True)

        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(path, isolation_level=None)
            connection.row_factory = sqlite3.Row
            cls._configure_connection(connection)
            storage = cls(connection, path, network, genesis_hash)
            storage._open_or_initialize(existed=existed, create=create)
            return storage
        except sqlite3.Error as error:
            if connection is not None:
                connection.close()
            raise StorageError("SQLite blockchain database could not be opened") from error
        except Exception:
            if connection is not None:
                connection.close()
            raise

    @staticmethod
    def _configure_connection(connection: sqlite3.Connection) -> None:
        connection.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")
        connection.execute("PRAGMA foreign_keys = ON")
        foreign_keys = connection.execute("PRAGMA foreign_keys").fetchone()
        if foreign_keys is None or foreign_keys[0] != 1:
            raise StorageError("SQLite foreign key enforcement could not be enabled")
        journal = connection.execute("PRAGMA journal_mode = WAL").fetchone()
        if journal is None or str(journal[0]).lower() != "wal":
            raise StorageError("SQLite WAL journal mode could not be enabled")
        connection.execute("PRAGMA synchronous = FULL")
        connection.execute("PRAGMA trusted_schema = OFF")

    def _ensure_open(self) -> None:
        if self._closed:
            raise StorageClosedError("blockchain storage is closed")

    def _open_or_initialize(self, *, existed: bool, create: bool) -> None:
        user_version = int(self._connection.execute("PRAGMA user_version").fetchone()[0])
        tables = {
            str(row[0])
            for row in self._connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        if user_version == 0 and not tables:
            if existed and not create:
                raise StorageIntegrityError("existing database has no OurCoin schema")
            self._initialize_schema()
            return
        if user_version != SCHEMA_VERSION:
            raise UnsupportedSchemaVersionError(
                f"unsupported database schema version {user_version}"
            )
        self._validate_schema_tables(tables)
        self._validate_identity()

    @staticmethod
    def _required_tables() -> frozenset[str]:
        return frozenset(
            {
                "accounts",
                "block_transactions",
                "blocks",
                "canonical_chain",
                "chain_metadata",
                "confirmed_transactions",
            }
        )

    def _validate_schema_tables(self, tables: set[str]) -> None:
        missing = self._required_tables() - tables
        if missing:
            raise StorageIntegrityError(
                f"database schema is missing tables: {', '.join(sorted(missing))}"
            )

    def _initialize_schema(self) -> None:
        chain = Chain(network=self.network)
        try:
            self._connection.executescript(
                """
                BEGIN IMMEDIATE;

                CREATE TABLE blocks (
                    acceptance_order INTEGER PRIMARY KEY AUTOINCREMENT,
                    block_hash BLOB NOT NULL UNIQUE
                        CHECK(typeof(block_hash) = 'blob' AND length(block_hash) = 32),
                    parent_hash BLOB NULL
                        REFERENCES blocks(block_hash),
                    height BLOB NOT NULL
                        CHECK(typeof(height) = 'blob' AND length(height) = 8),
                    cumulative_work BLOB NOT NULL
                        CHECK(typeof(cumulative_work) = 'blob' AND length(cumulative_work) = 40),
                    issued_supply_atoms BLOB NOT NULL
                        CHECK(typeof(issued_supply_atoms) = 'blob'
                              AND length(issued_supply_atoms) = 8),
                    header_bytes BLOB NOT NULL CHECK(typeof(header_bytes) = 'blob'),
                    reward_bytes BLOB NOT NULL CHECK(typeof(reward_bytes) = 'blob')
                );

                CREATE TABLE block_transactions (
                    block_hash BLOB NOT NULL REFERENCES blocks(block_hash) ON DELETE CASCADE,
                    position INTEGER NOT NULL CHECK(position >= 0),
                    txid BLOB NOT NULL
                        CHECK(typeof(txid) = 'blob' AND length(txid) = 32),
                    transaction_bytes BLOB NOT NULL CHECK(typeof(transaction_bytes) = 'blob'),
                    PRIMARY KEY(block_hash, position)
                );

                CREATE TABLE canonical_chain (
                    height BLOB PRIMARY KEY
                        CHECK(typeof(height) = 'blob' AND length(height) = 8),
                    block_hash BLOB NOT NULL UNIQUE REFERENCES blocks(block_hash)
                );

                CREATE TABLE accounts (
                    address TEXT PRIMARY KEY CHECK(typeof(address) = 'text'),
                    balance_atoms BLOB NOT NULL
                        CHECK(typeof(balance_atoms) = 'blob' AND length(balance_atoms) = 8),
                    nonce BLOB NOT NULL
                        CHECK(typeof(nonce) = 'blob' AND length(nonce) = 8)
                );

                CREATE TABLE confirmed_transactions (
                    txid BLOB PRIMARY KEY
                        CHECK(typeof(txid) = 'blob' AND length(txid) = 32),
                    block_hash BLOB NOT NULL REFERENCES blocks(block_hash)
                );

                CREATE TABLE chain_metadata (
                    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                    schema_version INTEGER NOT NULL,
                    chain_id TEXT NOT NULL,
                    genesis_hash BLOB NOT NULL
                        CHECK(typeof(genesis_hash) = 'blob' AND length(genesis_hash) = 32),
                    canonical_tip_hash BLOB NOT NULL REFERENCES blocks(block_hash),
                    issued_supply_atoms BLOB NOT NULL
                        CHECK(typeof(issued_supply_atoms) = 'blob'
                              AND length(issued_supply_atoms) = 8)
                );
                """
            )
            self._insert_block(chain, chain.tip_hash)
            self._connection.execute(
                """
                INSERT INTO chain_metadata(
                    singleton, schema_version, chain_id, genesis_hash,
                    canonical_tip_hash, issued_supply_atoms
                ) VALUES(1, ?, ?, ?, ?, ?)
                """,
                (
                    SCHEMA_VERSION,
                    self.network.chain_id,
                    self.genesis_hash,
                    chain.tip_hash,
                    encode_u64(chain.total_supply_atoms),
                ),
            )
            self._replace_derived_state(chain)
            self._connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            self._connection.execute("COMMIT")
        except Exception:
            if self._connection.in_transaction:
                self._connection.execute("ROLLBACK")
            raise

    def _metadata_row(self) -> sqlite3.Row:
        self._ensure_open()
        rows = cast(
            list[sqlite3.Row],
            self._connection.execute("SELECT * FROM chain_metadata").fetchall(),
        )
        if len(rows) != 1:
            raise StorageIntegrityError("database must contain exactly one metadata row")
        return rows[0]

    def _validate_identity(self) -> None:
        row = self._metadata_row()
        if type(row["schema_version"]) is not int or row["schema_version"] != SCHEMA_VERSION:
            raise UnsupportedSchemaVersionError("metadata schema version is unsupported")
        if row["chain_id"] != self.network.chain_id:
            raise NetworkMismatchError("database chain_id does not match the selected network")
        stored_genesis = _exact_blob(row["genesis_hash"], 32, "metadata genesis hash")
        if stored_genesis != self.genesis_hash:
            raise NetworkMismatchError("database genesis hash does not match the selected network")

    def _insert_block(self, chain: Chain, block_hash: bytes) -> None:
        block = chain.get_block(block_hash)
        record = chain.get_record(block_hash)
        self._connection.execute(
            """
            INSERT INTO blocks(
                block_hash, parent_hash, height, cumulative_work,
                issued_supply_atoms, header_bytes, reward_bytes
            ) VALUES(?, ?, ?, ?, ?, ?, ?)
            """,
            (
                block_hash,
                record.parent_hash,
                encode_u64(record.height),
                _encode_work(record.cumulative_work),
                encode_u64(record.total_supply_atoms),
                block.header.to_bytes(),
                block.reward_transaction.to_bytes(),
            ),
        )
        for position, transaction in enumerate(block.transactions):
            self._connection.execute(
                """
                INSERT INTO block_transactions(
                    block_hash, position, txid, transaction_bytes
                ) VALUES(?, ?, ?, ?)
                """,
                (block_hash, position, transaction.txid, transaction.to_bytes()),
            )

    def _replace_derived_state(self, chain: Chain) -> None:
        self._connection.execute("DELETE FROM canonical_chain")
        self._connection.execute("DELETE FROM accounts")
        self._connection.execute("DELETE FROM confirmed_transactions")
        active_chain = chain.active_chain()
        for block in active_chain:
            self._connection.execute(
                "INSERT INTO canonical_chain(height, block_hash) VALUES(?, ?)",
                (encode_u64(block.header.height), block.block_hash),
            )
            for transaction in block.transactions:
                self._connection.execute(
                    "INSERT INTO confirmed_transactions(txid, block_hash) VALUES(?, ?)",
                    (transaction.txid, block.block_hash),
                )
        for address, account in chain.state.snapshot().accounts:
            self._connection.execute(
                "INSERT INTO accounts(address, balance_atoms, nonce) VALUES(?, ?, ?)",
                (address, encode_u64(account.balance_atoms), encode_u64(account.nonce)),
            )
        self._connection.execute(
            """
            UPDATE chain_metadata
            SET canonical_tip_hash = ?, issued_supply_atoms = ?
            WHERE singleton = 1
            """,
            (chain.tip_hash, encode_u64(chain.total_supply_atoms)),
        )

    def _decode_block_row(self, row: sqlite3.Row) -> Block:
        block_hash = _exact_blob(row["block_hash"], 32, "block hash")
        try:
            header = BlockHeader.from_bytes(row["header_bytes"])
            reward = RewardTransaction.from_bytes(row["reward_bytes"])
            transaction_rows = self._connection.execute(
                """
                SELECT position, txid, transaction_bytes
                FROM block_transactions
                WHERE block_hash = ?
                ORDER BY position
                """,
                (block_hash,),
            ).fetchall()
            transactions: list[Transaction] = []
            for expected_position, transaction_row in enumerate(transaction_rows):
                if transaction_row["position"] != expected_position:
                    raise StorageIntegrityError("block transaction positions are not contiguous")
                transaction = Transaction.from_bytes(transaction_row["transaction_bytes"])
                txid = _exact_blob(transaction_row["txid"], 32, "transaction ID")
                if transaction.txid != txid:
                    raise StorageIntegrityError("stored transaction ID does not match its bytes")
                transactions.append(transaction)
        except (BlockEncodingError, TransactionError, TypeError) as error:
            raise StorageIntegrityError("stored block contains invalid canonical bytes") from error
        block = Block(header=header, reward_transaction=reward, transactions=tuple(transactions))
        if block.block_hash != block_hash:
            raise StorageIntegrityError("stored block hash does not match its header")
        try:
            body_root = transactions_root(reward, block.transactions)
        except BlockEncodingError as error:
            raise StorageIntegrityError("stored block body is not canonically valid") from error
        if body_root != header.transactions_root:
            raise StorageIntegrityError("stored block body does not match its transaction root")
        return block

    def _block_row(self, block_hash: bytes) -> sqlite3.Row:
        exact_hash = _exact_blob(block_hash, 32, "requested block hash")
        row = cast(
            sqlite3.Row | None,
            self._connection.execute(
                "SELECT * FROM blocks WHERE block_hash = ?",
                (exact_hash,),
            ).fetchone(),
        )
        if row is None:
            raise BlockNotFoundError("block is not stored")
        return row

    def get_block_by_hash(self, block_hash: bytes) -> Block:
        self._ensure_open()
        return self._decode_block_row(self._block_row(block_hash))

    def get_canonical_block_by_height(self, height: int) -> Block:
        self._ensure_open()
        if type(height) is not int or not 0 <= height <= U64_MAX:
            raise StorageError("block height must be an unsigned 64-bit integer")
        row = self._connection.execute(
            """
            SELECT blocks.*
            FROM canonical_chain
            JOIN blocks USING(block_hash)
            WHERE canonical_chain.height = ?
            """,
            (encode_u64(height),),
        ).fetchone()
        if row is None:
            raise BlockNotFoundError("canonical block height is not stored")
        return self._decode_block_row(row)

    @staticmethod
    def _record_from_row(row: sqlite3.Row) -> BlockRecord:
        parent_value = row["parent_hash"]
        parent_hash = (
            None
            if parent_value is None
            else _exact_blob(parent_value, 32, "parent block hash")
        )
        return BlockRecord(
            block_hash=_exact_blob(row["block_hash"], 32, "block hash"),
            parent_hash=parent_hash,
            height=_decode_u64(row["height"], "block height"),
            cumulative_work=_decode_work(row["cumulative_work"]),
            total_supply_atoms=_decode_u64(
                row["issued_supply_atoms"], "block issued supply"
            ),
        )

    def get_tip_record(self) -> BlockRecord:
        row = self._metadata_row()
        tip_hash = _exact_blob(row["canonical_tip_hash"], 32, "canonical tip hash")
        return self._record_from_row(self._block_row(tip_hash))

    def _assert_stored_block_matches(self, chain: Chain, block_hash: bytes) -> None:
        row = self._block_row(block_hash)
        if self._decode_block_row(row) != chain.get_block(block_hash):
            raise StorageIntegrityError("duplicate block bytes do not match stored data")
        if self._record_from_row(row) != chain.get_record(block_hash):
            raise StorageIntegrityError("duplicate block metadata does not match stored data")

    @staticmethod
    def _expected_previous_tip(update: ChainUpdate) -> bytes:
        if not update.became_active:
            return update.active_tip_hash
        if update.disconnected:
            return update.disconnected[0]
        if update.common_ancestor_hash is None:
            raise StorageIntegrityError("active extension does not identify its previous tip")
        return update.common_ancestor_hash

    def persist_chain_update(self, chain: Chain, update: ChainUpdate) -> bool:
        """Atomically persist one already validated chain update."""

        self._ensure_open()
        if chain.network != self.network:
            raise NetworkMismatchError("validated chain belongs to another network")
        if not chain.contains(update.added_block_hash):
            raise StorageIntegrityError("chain update does not exist in the supplied chain")
        if chain.tip_hash != update.active_tip_hash:
            raise StorageIntegrityError("chain update active tip does not match the supplied chain")

        try:
            self._connection.execute("BEGIN IMMEDIATE")
            metadata = self._metadata_row()
            stored_tip = _exact_blob(
                metadata["canonical_tip_hash"], 32, "stored canonical tip hash"
            )
            existing = self._connection.execute(
                "SELECT 1 FROM blocks WHERE block_hash = ?",
                (update.added_block_hash,),
            ).fetchone()
            if existing is not None:
                self._assert_stored_block_matches(chain, update.added_block_hash)
                self._connection.execute("COMMIT")
                return False
            if stored_tip != self._expected_previous_tip(update):
                raise StorageIntegrityError("database canonical tip changed before persistence")

            self._insert_block(chain, update.added_block_hash)
            if update.became_active:
                self._replace_derived_state(chain)
            self._connection.execute("COMMIT")
            return True
        except Exception as error:
            if self._connection.in_transaction:
                self._connection.execute("ROLLBACK")
            if isinstance(error, StorageError):
                raise
            if isinstance(error, sqlite3.Error):
                raise StorageError(
                    "SQLite could not atomically persist the chain update"
                ) from error
            raise

    def _replay_blocks(self, *, check_derived_records: bool) -> Chain:
        rows = self._connection.execute(
            "SELECT * FROM blocks ORDER BY acceptance_order"
        ).fetchall()
        if not rows:
            raise StorageIntegrityError("database contains no genesis block")
        chain = Chain(network=self.network)
        for index, row in enumerate(rows):
            block = self._decode_block_row(row)
            stored_parent = row["parent_hash"]
            expected_parent = None if index == 0 else block.header.previous_block_hash
            if stored_parent != expected_parent:
                raise StorageIntegrityError("stored parent hash does not match the block header")
            stored_height = _decode_u64(row["height"], "block height")
            if stored_height != block.header.height:
                raise StorageIntegrityError("stored height does not match the block header")
            if index == 0:
                if block != chain.tip or block.block_hash != self.genesis_hash:
                    raise StorageIntegrityError(
                        "stored genesis does not match the selected network"
                    )
            else:
                try:
                    chain.add_block(block)
                except ChainError as error:
                    raise StorageIntegrityError("stored block replay failed validation") from error
            if check_derived_records and self._record_from_row(row) != chain.get_record(
                block.block_hash
            ):
                raise StorageIntegrityError("stored block record differs from replay")
        return chain

    def _stored_state(self) -> tuple[tuple[tuple[str, Account], ...], frozenset[bytes]]:
        try:
            accounts = tuple(
                (
                    str(row["address"]),
                    Account(
                        balance_atoms=_decode_u64(row["balance_atoms"], "account balance"),
                        nonce=_decode_u64(row["nonce"], "account nonce"),
                    ),
                )
                for row in self._connection.execute(
                    "SELECT address, balance_atoms, nonce FROM accounts ORDER BY address"
                )
            )
        except ValueError as error:
            raise StorageIntegrityError("stored account data is invalid") from error
        confirmed = frozenset(
            _exact_blob(row["txid"], 32, "confirmed transaction ID")
            for row in self._connection.execute("SELECT txid FROM confirmed_transactions")
        )
        return accounts, confirmed

    def _validate_sqlite_integrity(self) -> None:
        result = self._connection.execute("PRAGMA integrity_check").fetchall()
        if len(result) != 1 or result[0][0] != "ok":
            raise StorageIntegrityError("SQLite integrity_check failed")
        if self._connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise StorageIntegrityError("SQLite foreign key check failed")

    def _validate_derived_state(self, chain: Chain) -> None:
        metadata = self._metadata_row()
        tip_hash = _exact_blob(metadata["canonical_tip_hash"], 32, "canonical tip hash")
        if tip_hash != chain.tip_hash:
            raise StorageIntegrityError("stored canonical tip differs from replay")
        supply = _decode_u64(metadata["issued_supply_atoms"], "metadata issued supply")
        if supply != chain.total_supply_atoms:
            raise StorageIntegrityError("stored issued supply differs from replay")

        canonical_rows = self._connection.execute(
            "SELECT height, block_hash FROM canonical_chain ORDER BY height"
        ).fetchall()
        expected_chain = chain.active_chain()
        if len(canonical_rows) != len(expected_chain):
            raise StorageIntegrityError("canonical height index differs from replay")
        for row, block in zip(canonical_rows, expected_chain, strict=True):
            if (
                _decode_u64(row["height"], "canonical height") != block.header.height
                or _exact_blob(row["block_hash"], 32, "canonical block hash")
                != block.block_hash
            ):
                raise StorageIntegrityError("canonical height index differs from replay")
        stored_accounts, stored_confirmed_ids = self._stored_state()
        expected_snapshot = chain.state.snapshot()
        if (
            stored_accounts != expected_snapshot.accounts
            or stored_confirmed_ids != expected_snapshot.confirmed_txids
        ):
            raise StorageIntegrityError("stored account state differs from replay")
        stored_confirmed = {
            (
                _exact_blob(row["txid"], 32, "confirmed transaction ID"),
                _exact_blob(row["block_hash"], 32, "confirmed block hash"),
            )
            for row in self._connection.execute(
                "SELECT txid, block_hash FROM confirmed_transactions"
            )
        }
        expected_confirmed = {
            (transaction.txid, block.block_hash)
            for block in expected_chain
            for transaction in block.transactions
        }
        if stored_confirmed != expected_confirmed:
            raise StorageIntegrityError("stored confirmed transaction index differs from replay")

    def load_chain(self) -> Chain:
        self._ensure_open()
        self._validate_identity()
        self._validate_sqlite_integrity()
        chain = self._replay_blocks(check_derived_records=True)
        self._validate_derived_state(chain)
        return chain

    def info(self) -> StorageInfo:
        self._ensure_open()
        metadata = self._metadata_row()
        record = self.get_tip_record()
        block_count = int(self._connection.execute("SELECT COUNT(*) FROM blocks").fetchone()[0])
        account_count = int(
            self._connection.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
        )
        return StorageInfo(
            schema_version=SCHEMA_VERSION,
            chain_id=self.network.chain_id,
            genesis_hash=self.genesis_hash,
            database_path=self.database_path,
            height=record.height,
            tip_hash=record.block_hash,
            cumulative_work=record.cumulative_work,
            issued_supply_atoms=_decode_u64(
                metadata["issued_supply_atoms"], "metadata issued supply"
            ),
            block_count=block_count,
            account_count=account_count,
        )

    def validate(self) -> StorageValidationReport:
        chain = self.load_chain()
        info = self.info()
        return StorageValidationReport(
            valid=True,
            block_count=info.block_count,
            account_count=info.account_count,
            height=chain.height,
            tip_hash=chain.tip_hash,
        )

    def reindex(self) -> StorageReindexReport:
        self._ensure_open()
        integrity = self._connection.execute("PRAGMA integrity_check").fetchall()
        if len(integrity) != 1 or integrity[0][0] != "ok":
            raise StorageIntegrityError("SQLite integrity_check failed")
        chain = self._replay_blocks(check_derived_records=False)
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            block_rows = self._connection.execute("SELECT block_hash FROM blocks").fetchall()
            for row in block_rows:
                block_hash = _exact_blob(row["block_hash"], 32, "block hash")
                record = chain.get_record(block_hash)
                self._connection.execute(
                    """
                    UPDATE blocks
                    SET cumulative_work = ?, issued_supply_atoms = ?
                    WHERE block_hash = ?
                    """,
                    (
                        _encode_work(record.cumulative_work),
                        encode_u64(record.total_supply_atoms),
                        block_hash,
                    ),
                )
            self._replace_derived_state(chain)
            self._connection.execute("COMMIT")
        except Exception as error:
            if self._connection.in_transaction:
                self._connection.execute("ROLLBACK")
            if isinstance(error, sqlite3.Error):
                raise StorageError("SQLite could not atomically rebuild chain indexes") from error
            raise
        report = self.validate()
        return StorageReindexReport(
            block_count=report.block_count,
            account_count=report.account_count,
            height=report.height,
            tip_hash=report.tip_hash,
        )

    def close(self) -> None:
        if self._closed:
            return
        self._connection.close()
        self._closed = True

    def __enter__(self) -> "SQLiteChainStorage":
        self._ensure_open()
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        self.close()


__all__ = [
    "BlockNotFoundError",
    "NetworkMismatchError",
    "SCHEMA_VERSION",
    "SQLiteChainStorage",
    "StorageClosedError",
    "StorageError",
    "StorageInfo",
    "StorageIntegrityError",
    "StorageNotFoundError",
    "StorageReindexReport",
    "StorageValidationReport",
    "UnsupportedNetworkError",
    "UnsupportedSchemaVersionError",
    "database_path_for_identity",
]
