"""Merkle root behavior used by transactions and account state."""

import pytest

from ourcoin.crypto import sha256_digest
from ourcoin.merkle import MerkleError, merkle_root


def test_empty_and_single_leaf_roots() -> None:
    leaf = sha256_digest(b"leaf")

    assert merkle_root(()) == sha256_digest(b"\x00")
    assert merkle_root((leaf,)) == leaf


def test_odd_leaf_is_duplicated() -> None:
    first = sha256_digest(b"first")
    second = sha256_digest(b"second")
    third = sha256_digest(b"third")
    left = sha256_digest(b"\x01" + first + second)
    right = sha256_digest(b"\x01" + third + third)

    assert merkle_root((first, second, third)) == sha256_digest(b"\x01" + left + right)


def test_invalid_leaf_length_is_rejected() -> None:
    with pytest.raises(MerkleError, match="32 bytes"):
        merkle_root((b"short",))
