"""M3 block emission, construction and atomic validation rules."""

from dataclasses import dataclass

from ourcoin.address import AddressError, decode_address
from ourcoin.block import (
    BLOCK_VERSION,
    MAX_TARGET,
    REWARD_TRANSACTION_VERSION,
    Block,
    BlockEncodingError,
    BlockHeader,
    RewardTransaction,
    hash_meets_target,
    transactions_root,
)
from ourcoin.config import TESTNET, NetworkConfig
from ourcoin.encoding import U64_MAX
from ourcoin.state import AccountState, StateError
from ourcoin.transaction import Transaction

ATOMS_PER_OUR = 100_000_000
MAX_SUPPLY_ATOMS = 100_000_000 * ATOMS_PER_OUR
INITIAL_REWARD_ATOMS = 40 * ATOMS_PER_OUR
REWARD_INTERVAL = 500_000
REWARD_NUMERATOR = 4
REWARD_DENOMINATOR = 5
MAX_BLOCK_TRANSACTIONS = 10_000
TESTNET_INITIAL_TARGET = (1 << 252) - 1
TESTNET_GENESIS_TIMESTAMP = 1_786_060_800
TESTNET_GENESIS_NONCE = 10
TESTNET_GENESIS_MINER_ADDRESS = "tour1n4q74mo7ufkkeylcnp4fibdp2itrw67njmkxszhk"


class BlockError(ValueError):
    """Raised when a candidate violates an M3 consensus rule."""


@dataclass(frozen=True, slots=True)
class BlockExecution:
    state: AccountState
    total_supply_atoms: int
    subsidy_atoms: int
    fees_atoms: int


def _validate_supply(total_supply_atoms: int) -> None:
    if (
        type(total_supply_atoms) is not int
        or not 0 <= total_supply_atoms <= MAX_SUPPLY_ATOMS
    ):
        raise BlockError("total supply is outside the consensus range")


def block_subsidy(height: int, total_supply_atoms: int) -> int:
    """Return the scheduled subsidy capped by the remaining maximum supply."""

    if type(height) is not int or not 0 <= height <= U64_MAX:
        raise BlockError("block height must be an unsigned 64-bit integer")
    _validate_supply(total_supply_atoms)
    if height == 0 or total_supply_atoms == MAX_SUPPLY_ATOMS:
        return 0

    era = height // REWARD_INTERVAL
    reward = INITIAL_REWARD_ATOMS
    elapsed_eras = 0
    while elapsed_eras < era and reward > 0:
        reward = reward * REWARD_NUMERATOR // REWARD_DENOMINATOR
        elapsed_eras += 1
    return min(reward, MAX_SUPPLY_ATOMS - total_supply_atoms)


def genesis_block(network: NetworkConfig = TESTNET) -> Block:
    if network != TESTNET:
        raise BlockError("M3 defines only the testnet genesis block")
    empty_state = AccountState(network=network)
    reward = RewardTransaction(
        version=REWARD_TRANSACTION_VERSION,
        chain_id=network.chain_id,
        height=0,
        miner_address=TESTNET_GENESIS_MINER_ADDRESS,
        amount_atoms=0,
    )
    header = BlockHeader(
        version=BLOCK_VERSION,
        chain_id=network.chain_id,
        height=0,
        previous_block_hash=b"\x00" * 32,
        transactions_root=transactions_root(reward, ()),
        state_root=empty_state.state_root(),
        timestamp=TESTNET_GENESIS_TIMESTAMP,
        difficulty_target=TESTNET_INITIAL_TARGET,
        nonce=TESTNET_GENESIS_NONCE,
        miner_address=TESTNET_GENESIS_MINER_ADDRESS,
    )
    return Block(header=header, reward_transaction=reward, transactions=())


def build_block_candidate(
    parent: Block,
    state: AccountState,
    total_supply_atoms: int,
    *,
    miner_address: str,
    transactions: tuple[Transaction, ...] = (),
    timestamp: int,
    difficulty_target: int | None = None,
) -> Block:
    """Build a valid state-root candidate; Proof of Work is added separately."""

    _validate_supply(total_supply_atoms)
    if not isinstance(parent, Block):
        raise BlockError("parent value is not a Block")
    if not isinstance(state, AccountState):
        raise BlockError("state value is not an AccountState")
    if parent.header.height == U64_MAX:
        raise BlockError("parent height is exhausted")
    height = parent.header.height + 1
    if type(timestamp) is not int or not 0 <= timestamp <= U64_MAX:
        raise BlockError("block timestamp must be an unsigned 64-bit integer")
    if timestamp <= parent.header.timestamp:
        raise BlockError("block timestamp must be greater than its parent's timestamp")
    if type(transactions) is not tuple:
        raise BlockError("block transactions must be an immutable tuple")
    if len(transactions) > MAX_BLOCK_TRANSACTIONS:
        raise BlockError("block contains too many transactions")
    target = parent.header.difficulty_target if difficulty_target is None else difficulty_target
    if type(target) is not int or not 0 < target <= MAX_TARGET:
        raise BlockError("candidate difficulty target is outside the allowed range")
    try:
        decode_address(miner_address, state.network)
    except AddressError as error:
        raise BlockError("miner address is invalid for this network") from error

    working = state.copy()
    try:
        fees = working.apply_transactions(transactions, current_height=height)
        subsidy = block_subsidy(height, total_supply_atoms)
        reward_amount = subsidy + fees
        if reward_amount > U64_MAX:
            raise BlockError("block reward plus fees exceed uint64")
        reward = RewardTransaction(
            version=REWARD_TRANSACTION_VERSION,
            chain_id=state.network.chain_id,
            height=height,
            miner_address=miner_address,
            amount_atoms=reward_amount,
        )
        working.credit_reward(miner_address, reward_amount)
    except StateError as error:
        raise BlockError(str(error)) from error

    header = BlockHeader(
        version=BLOCK_VERSION,
        chain_id=state.network.chain_id,
        height=height,
        previous_block_hash=parent.block_hash,
        transactions_root=transactions_root(reward, transactions),
        state_root=working.state_root(),
        timestamp=timestamp,
        difficulty_target=target,
        nonce=0,
        miner_address=miner_address,
    )
    return Block(header=header, reward_transaction=reward, transactions=transactions)


def _validate_genesis(
    block: Block,
    state: AccountState,
    total_supply_atoms: int,
    network: NetworkConfig,
) -> BlockExecution:
    if state.snapshot().accounts or state.snapshot().confirmed_txids:
        raise BlockError("genesis requires an empty account state")
    if total_supply_atoms != 0:
        raise BlockError("genesis requires zero existing supply")
    if block != genesis_block(network):
        raise BlockError("genesis block does not match the testnet definition")
    if not hash_meets_target(block.block_hash, block.header.difficulty_target):
        raise BlockError("genesis Proof of Work is insufficient")
    return BlockExecution(state.copy(), 0, 0, 0)


def validate_and_execute_block(
    block: Block,
    parent: Block | None,
    state: AccountState,
    total_supply_atoms: int,
    *,
    network: NetworkConfig = TESTNET,
    expected_difficulty_target: int | None = None,
) -> BlockExecution:
    """Validate against a parent and return a new state without mutating the input."""

    _validate_supply(total_supply_atoms)
    if not isinstance(block, Block):
        raise BlockError("value is not a Block")
    if not isinstance(state, AccountState):
        raise BlockError("state value is not an AccountState")
    if parent is None:
        return _validate_genesis(block, state, total_supply_atoms, network)
    if not isinstance(parent, Block):
        raise BlockError("parent value is not a Block")
    if not isinstance(block.header, BlockHeader):
        raise BlockError("block header has an invalid type")
    if not isinstance(block.reward_transaction, RewardTransaction):
        raise BlockError("block reward has an invalid type")
    if type(block.transactions) is not tuple:
        raise BlockError("block transactions must be an immutable tuple")

    header = block.header
    if type(header.version) is not int or header.version != BLOCK_VERSION:
        raise BlockError("unsupported block version")
    if header.chain_id != network.chain_id or state.network != network:
        raise BlockError("block or state belongs to another network")
    if header.height != parent.header.height + 1:
        raise BlockError("block height does not follow its parent")
    if header.previous_block_hash != parent.block_hash:
        raise BlockError("previous block hash does not match the parent")
    if header.timestamp <= parent.header.timestamp:
        raise BlockError("block timestamp does not increase")
    required_target = (
        parent.header.difficulty_target
        if expected_difficulty_target is None
        else expected_difficulty_target
    )
    if type(required_target) is not int or not 0 < required_target <= MAX_TARGET:
        raise BlockError("expected difficulty target is outside the allowed range")
    if header.difficulty_target != required_target:
        raise BlockError("block difficulty target does not match the expected target")
    if not 0 < header.difficulty_target <= MAX_TARGET:
        raise BlockError("block difficulty target is outside the allowed range")
    if len(block.transactions) > MAX_BLOCK_TRANSACTIONS:
        raise BlockError("block contains too many transactions")
    try:
        decode_address(header.miner_address, network)
    except AddressError as error:
        raise BlockError("block miner address is invalid") from error
    try:
        expected_transactions_root = transactions_root(
            block.reward_transaction,
            block.transactions,
        )
    except (BlockEncodingError, ValueError) as error:
        raise BlockError("block transactions are not canonically encodable") from error
    if header.transactions_root != expected_transactions_root:
        raise BlockError("block transactions root does not match its body")
    try:
        proof_is_valid = hash_meets_target(block.block_hash, header.difficulty_target)
    except (BlockEncodingError, ValueError) as error:
        raise BlockError("block header is not canonically encodable") from error
    if not proof_is_valid:
        raise BlockError("block Proof of Work is insufficient")

    reward = block.reward_transaction
    if reward.version != REWARD_TRANSACTION_VERSION:
        raise BlockError("unsupported reward transaction version")
    if reward.chain_id != network.chain_id:
        raise BlockError("reward transaction belongs to another network")
    if reward.height != header.height:
        raise BlockError("reward transaction height does not match the block")
    if reward.miner_address != header.miner_address:
        raise BlockError("reward recipient does not match the block miner")

    working = state.copy()
    try:
        fees = working.apply_transactions(block.transactions, current_height=header.height)
        subsidy = block_subsidy(header.height, total_supply_atoms)
        expected_reward = subsidy + fees
        if expected_reward > U64_MAX:
            raise BlockError("block reward plus fees exceed uint64")
        if reward.amount_atoms != expected_reward:
            raise BlockError("reward amount is not the subsidy plus fees")
        working.credit_reward(reward.miner_address, reward.amount_atoms)
    except StateError as error:
        raise BlockError(str(error)) from error

    if header.state_root != working.state_root():
        raise BlockError("block state root does not match the executed state")
    return BlockExecution(
        state=working,
        total_supply_atoms=total_supply_atoms + subsidy,
        subsidy_atoms=subsidy,
        fees_atoms=fees,
    )
