"""Tests for network-specific address encoding."""

import json
from pathlib import Path
from typing import Any

import pytest

from ourcoin.address import AddressError, address_from_public_key, decode_address, is_valid_address
from ourcoin.config import TESTNET, NetworkConfig

VECTOR = Path(__file__).parents[1] / "vectors" / "address.json"


def _load_vector() -> dict[str, Any]:
    with VECTOR.open(encoding="utf-8") as handle:
        return json.load(handle)


def test_testnet_address_vector() -> None:
    vector = _load_vector()
    public_key = bytes.fromhex(vector["public_key_hex"])

    address = address_from_public_key(public_key)

    assert TESTNET.chain_id == vector["network"]
    assert address == vector["address"]
    assert decode_address(address).hex() == vector["public_key_hash_hex"]
    assert is_valid_address(address)


def test_address_rejects_bad_checksum_and_case() -> None:
    address = _load_vector()["address"]
    replacement = "a" if address[-1] != "a" else "b"

    with pytest.raises(AddressError, match="checksum"):
        decode_address(address[:-1] + replacement)
    assert not is_valid_address(address.upper())


def test_address_rejects_another_network() -> None:
    address = _load_vector()["address"]
    other_network = NetworkConfig(
        name="isolated-test",
        chain_id="isolated-test-v1",
        address_hrp="xour",
        address_version=1,
    )

    with pytest.raises(AddressError, match="another network"):
        decode_address(address, other_network)


def test_address_rejects_wrong_version_with_same_prefix() -> None:
    address = _load_vector()["address"]
    wrong_version = NetworkConfig(
        name="wrong-version-test",
        chain_id="wrong-version-test-v1",
        address_hrp=TESTNET.address_hrp,
        address_version=1,
    )

    with pytest.raises(AddressError, match="version"):
        decode_address(address, wrong_version)
