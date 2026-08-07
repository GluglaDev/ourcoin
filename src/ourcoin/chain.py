"""M4 block tree, cumulative-work selection and atomic active-state switching."""

from dataclasses import dataclass

from ourcoin.block import MAX_TARGET, Block
from ourcoin.config import TESTNET, NetworkConfig
from ourcoin.consensus import (
    TESTNET_INITIAL_TARGET,
    BlockError,
    build_block_candidate,
    genesis_block,
    validate_and_execute_block,
)
from ourcoin.encoding import U64_MAX
from ourcoin.state import AccountState, StateSnapshot
from ourcoin.transaction import Transaction

TARGET_BLOCK_SECONDS = 60
DIFFICULTY_ADJUSTMENT_INTERVAL = 120
DIFFICULTY_MAX_ADJUSTMENT_FACTOR = 4
EXPECTED_ADJUSTMENT_TIMESPAN = (
    DIFFICULTY_ADJUSTMENT_INTERVAL - 1
) * TARGET_BLOCK_SECONDS


class ChainError(ValueError):
    """Raised when a block cannot be added to the validated block tree."""


@dataclass(frozen=True, slots=True)
class BlockRecord:
    block_hash: bytes
    parent_hash: bytes | None
    height: int
    cumulative_work: int
    total_supply_atoms: int


@dataclass(frozen=True, slots=True)
class ChainUpdate:
    added_block_hash: bytes
    active_tip_hash: bytes
    became_active: bool
    reorganized: bool
    common_ancestor_hash: bytes | None
    disconnected: tuple[bytes, ...]
    connected: tuple[bytes, ...]


@dataclass(frozen=True, slots=True)
class _ChainEntry:
    block: Block
    record: BlockRecord
    state_snapshot: StateSnapshot


def work_for_target(target: int) -> int:
    """Return the integer expected-work contribution for one valid header."""

    if type(target) is not int or not 0 < target <= MAX_TARGET:
        raise ChainError("work target must be between 1 and 2^256 - 1")
    return (1 << 256) // (target + 1)


class Chain:
    """Validated blocks on all known branches with one cumulative-work tip."""

    def __init__(self, *, network: NetworkConfig = TESTNET) -> None:
        if network != TESTNET:
            raise ChainError("M4 currently defines only the testnet chain")
        self._network = network
        genesis = genesis_block(network)
        execution = validate_and_execute_block(
            genesis,
            None,
            AccountState(network=network),
            0,
            network=network,
        )
        block_hash = genesis.block_hash
        record = BlockRecord(
            block_hash=block_hash,
            parent_hash=None,
            height=0,
            cumulative_work=work_for_target(genesis.header.difficulty_target),
            total_supply_atoms=execution.total_supply_atoms,
        )
        self._entries: dict[bytes, _ChainEntry] = {
            block_hash: _ChainEntry(genesis, record, execution.state.snapshot())
        }
        self._active_tip_hash = block_hash

    @property
    def network(self) -> NetworkConfig:
        return self._network

    @property
    def tip(self) -> Block:
        return self._entries[self._active_tip_hash].block

    @property
    def tip_hash(self) -> bytes:
        return self._active_tip_hash

    @property
    def height(self) -> int:
        return self._entries[self._active_tip_hash].record.height

    @property
    def cumulative_work(self) -> int:
        return self._entries[self._active_tip_hash].record.cumulative_work

    @property
    def total_supply_atoms(self) -> int:
        return self._entries[self._active_tip_hash].record.total_supply_atoms

    @property
    def state(self) -> AccountState:
        entry = self._entries[self._active_tip_hash]
        return AccountState.from_snapshot(entry.state_snapshot, network=self._network)

    def contains(self, block_hash: bytes) -> bool:
        return block_hash in self._entries

    def get_block(self, block_hash: bytes) -> Block:
        try:
            return self._entries[block_hash].block
        except (KeyError, TypeError) as error:
            raise ChainError("unknown block hash") from error

    def get_record(self, block_hash: bytes) -> BlockRecord:
        try:
            return self._entries[block_hash].record
        except (KeyError, TypeError) as error:
            raise ChainError("unknown block hash") from error

    def get_state(self, block_hash: bytes) -> AccountState:
        try:
            snapshot = self._entries[block_hash].state_snapshot
        except (KeyError, TypeError) as error:
            raise ChainError("unknown block hash") from error
        return AccountState.from_snapshot(snapshot, network=self._network)

    def _ancestor_at_height(self, block_hash: bytes, height: int) -> _ChainEntry:
        entry = self._entries[block_hash]
        if type(height) is not int or not 0 <= height <= entry.record.height:
            raise ChainError("ancestor height is outside the branch")
        while entry.record.height > height:
            parent_hash = entry.record.parent_hash
            if parent_hash is None:
                raise ChainError("branch ended before the requested ancestor")
            entry = self._entries[parent_hash]
        return entry

    def expected_target_for_child(self, parent_hash: bytes) -> int:
        try:
            parent = self._entries[parent_hash]
        except (KeyError, TypeError) as error:
            raise ChainError("unknown difficulty parent") from error
        if parent.record.height == U64_MAX:
            raise ChainError("parent height is exhausted")

        next_height = parent.record.height + 1
        if next_height % DIFFICULTY_ADJUSTMENT_INTERVAL != 0:
            return parent.block.header.difficulty_target

        window_start_height = next_height - DIFFICULTY_ADJUSTMENT_INTERVAL
        window_start = self._ancestor_at_height(parent_hash, window_start_height)
        actual_timespan = parent.block.header.timestamp - window_start.block.header.timestamp
        minimum_timespan = EXPECTED_ADJUSTMENT_TIMESPAN // DIFFICULTY_MAX_ADJUSTMENT_FACTOR
        maximum_timespan = EXPECTED_ADJUSTMENT_TIMESPAN * DIFFICULTY_MAX_ADJUSTMENT_FACTOR
        bounded_timespan = min(max(actual_timespan, minimum_timespan), maximum_timespan)
        adjusted = (
            parent.block.header.difficulty_target
            * bounded_timespan
            // EXPECTED_ADJUSTMENT_TIMESPAN
        )
        return min(max(adjusted, 1), MAX_TARGET)

    def build_candidate(
        self,
        *,
        miner_address: str,
        transactions: tuple[Transaction, ...] = (),
        timestamp: int,
        parent_hash: bytes | None = None,
    ) -> Block:
        selected_parent_hash = self._active_tip_hash if parent_hash is None else parent_hash
        try:
            parent = self._entries[selected_parent_hash]
        except (KeyError, TypeError) as error:
            raise ChainError("cannot build on an unknown parent") from error
        state = AccountState.from_snapshot(parent.state_snapshot, network=self._network)
        try:
            return build_block_candidate(
                parent.block,
                state,
                parent.record.total_supply_atoms,
                miner_address=miner_address,
                transactions=transactions,
                timestamp=timestamp,
                difficulty_target=self.expected_target_for_child(selected_parent_hash),
            )
        except BlockError as error:
            raise ChainError(str(error)) from error

    def _reorganization_paths(
        self,
        old_tip_hash: bytes,
        new_tip_hash: bytes,
    ) -> tuple[bytes, tuple[bytes, ...], tuple[bytes, ...]]:
        old_hash = old_tip_hash
        new_hash = new_tip_hash
        old_entry = self._entries[old_hash]
        new_entry = self._entries[new_hash]
        disconnected: list[bytes] = []
        connected_reverse: list[bytes] = []

        while old_entry.record.height > new_entry.record.height:
            disconnected.append(old_hash)
            parent_hash = old_entry.record.parent_hash
            if parent_hash is None:
                raise ChainError("old branch has no common ancestor")
            old_hash = parent_hash
            old_entry = self._entries[old_hash]
        while new_entry.record.height > old_entry.record.height:
            connected_reverse.append(new_hash)
            parent_hash = new_entry.record.parent_hash
            if parent_hash is None:
                raise ChainError("new branch has no common ancestor")
            new_hash = parent_hash
            new_entry = self._entries[new_hash]
        while old_hash != new_hash:
            disconnected.append(old_hash)
            connected_reverse.append(new_hash)
            old_parent = old_entry.record.parent_hash
            new_parent = new_entry.record.parent_hash
            if old_parent is None or new_parent is None:
                raise ChainError("branches have no common ancestor")
            old_hash = old_parent
            new_hash = new_parent
            old_entry = self._entries[old_hash]
            new_entry = self._entries[new_hash]

        return old_hash, tuple(disconnected), tuple(reversed(connected_reverse))

    def add_block(self, block: Block) -> ChainUpdate:
        if not isinstance(block, Block):
            raise ChainError("value is not a Block")
        try:
            block_hash = block.block_hash
        except (AttributeError, TypeError, ValueError) as error:
            raise ChainError("block header is not canonically encodable") from error
        if block_hash in self._entries:
            raise ChainError("block is already known")
        parent_hash = block.header.previous_block_hash
        try:
            parent = self._entries[parent_hash]
        except (KeyError, TypeError) as error:
            raise ChainError("block parent is unknown") from error

        parent_state = AccountState.from_snapshot(parent.state_snapshot, network=self._network)
        expected_target = self.expected_target_for_child(parent_hash)
        try:
            execution = validate_and_execute_block(
                block,
                parent.block,
                parent_state,
                parent.record.total_supply_atoms,
                network=self._network,
                expected_difficulty_target=expected_target,
            )
        except BlockError as error:
            raise ChainError(str(error)) from error

        record = BlockRecord(
            block_hash=block_hash,
            parent_hash=parent_hash,
            height=block.header.height,
            cumulative_work=(
                parent.record.cumulative_work + work_for_target(block.header.difficulty_target)
            ),
            total_supply_atoms=execution.total_supply_atoms,
        )
        entry = _ChainEntry(block, record, execution.state.snapshot())
        self._entries[block_hash] = entry

        old_tip_hash = self._active_tip_hash
        old_tip = self._entries[old_tip_hash]
        became_active = record.cumulative_work > old_tip.record.cumulative_work
        common_ancestor_hash: bytes | None = None
        disconnected: tuple[bytes, ...] = ()
        connected: tuple[bytes, ...] = ()
        reorganized = False
        if became_active:
            common_ancestor_hash, disconnected, connected = self._reorganization_paths(
                old_tip_hash,
                block_hash,
            )
            reorganized = bool(disconnected)

        if became_active:
            self._active_tip_hash = block_hash

        return ChainUpdate(
            added_block_hash=block_hash,
            active_tip_hash=self._active_tip_hash,
            became_active=became_active,
            reorganized=reorganized,
            common_ancestor_hash=common_ancestor_hash,
            disconnected=disconnected,
            connected=connected,
        )

    def active_chain(self) -> tuple[Block, ...]:
        blocks: list[Block] = []
        entry = self._entries[self._active_tip_hash]
        while True:
            blocks.append(entry.block)
            parent_hash = entry.record.parent_hash
            if parent_hash is None:
                break
            entry = self._entries[parent_hash]
        return tuple(reversed(blocks))


__all__ = [
    "DIFFICULTY_ADJUSTMENT_INTERVAL",
    "EXPECTED_ADJUSTMENT_TIMESPAN",
    "TARGET_BLOCK_SECONDS",
    "BlockRecord",
    "Chain",
    "ChainError",
    "ChainUpdate",
    "TESTNET_INITIAL_TARGET",
    "work_for_target",
]
