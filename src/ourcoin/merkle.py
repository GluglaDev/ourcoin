"""Domain-separated SHA-256 Merkle roots."""

from collections.abc import Sequence

from ourcoin.crypto import sha256_digest

HASH_LENGTH = 32
EMPTY_MERKLE_DOMAIN = b"\x00"
MERKLE_NODE_DOMAIN = b"\x01"


class MerkleError(ValueError):
    """Raised when a Merkle leaf is not a SHA-256-sized value."""


def merkle_root(leaves: Sequence[bytes]) -> bytes:
    """Return a deterministic root, duplicating an odd final node per level."""

    if not leaves:
        return sha256_digest(EMPTY_MERKLE_DOMAIN)
    if any(type(leaf) is not bytes or len(leaf) != HASH_LENGTH for leaf in leaves):
        raise MerkleError("every Merkle leaf must be exactly 32 bytes")

    level = list(leaves)
    while len(level) > 1:
        if len(level) % 2:
            level.append(level[-1])
        level = [
            sha256_digest(MERKLE_NODE_DOMAIN + level[index] + level[index + 1])
            for index in range(0, len(level), 2)
        ]
    return level[0]
