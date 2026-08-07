"""Proof-of-Work search over an otherwise immutable block candidate."""

from ourcoin.block import Block, hash_meets_target
from ourcoin.encoding import U64_MAX


class MiningError(RuntimeError):
    """Raised when a bounded mining attempt cannot find a valid nonce."""


def mine_block(block: Block, *, max_attempts: int = 1_000_000) -> Block:
    if type(max_attempts) is not int or max_attempts <= 0:
        raise MiningError("max_attempts must be a positive integer")

    nonce = block.header.nonce
    for _ in range(max_attempts):
        if nonce > U64_MAX:
            break
        candidate = block.with_nonce(nonce)
        if hash_meets_target(candidate.block_hash, candidate.header.difficulty_target):
            return candidate
        nonce += 1
    raise MiningError("no valid nonce found within max_attempts")
