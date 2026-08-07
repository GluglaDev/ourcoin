"""Intrinsic transaction validation and canonical serialization tests."""

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from ourcoin.address import address_from_public_key
from ourcoin.config import TESTNET, NetworkConfig
from ourcoin.crypto import generate_private_key, public_key_from_private
from ourcoin.encoding import U64_MAX
from ourcoin.transaction import (
    TRANSACTION_VERSION,
    Transaction,
    TransactionError,
    create_signed_transaction,
    validate_transaction,
)

VECTOR = Path(__file__).parents[1] / "vectors" / "transaction.json"


def _load_vector() -> dict[str, Any]:
    with VECTOR.open(encoding="utf-8") as handle:
        return json.load(handle)


def _recipient_address() -> str:
    private_key = generate_private_key()
    return address_from_public_key(public_key_from_private(private_key))


def _valid_transaction(*, nonce: int = 0, amount: int = 25, fee: int = 2) -> Transaction:
    return create_signed_transaction(
        generate_private_key(),
        recipient_address=_recipient_address(),
        amount_atoms=amount,
        fee_atoms=fee,
        nonce=nonce,
        valid_until_height=100,
    )


def test_signed_transaction_round_trip_and_txid() -> None:
    transaction = _valid_transaction()
    decoded = Transaction.from_bytes(transaction.to_bytes())

    assert decoded == transaction
    assert decoded.txid == transaction.txid
    assert len(transaction.txid) == 32
    validate_transaction(decoded, current_height=100)


def test_public_canonical_transaction_vector() -> None:
    vector = _load_vector()
    transaction = Transaction(
        version=vector["version"],
        chain_id=vector["chain_id"],
        sender_public_key=bytes.fromhex(vector["sender_public_key_hex"]),
        sender_address=vector["sender_address"],
        recipient_address=vector["recipient_address"],
        amount_atoms=vector["amount_atoms"],
        fee_atoms=vector["fee_atoms"],
        nonce=vector["nonce"],
        valid_until_height=vector["valid_until_height"],
        signature=bytes.fromhex(vector["signature_hex"]),
    )

    assert transaction.to_bytes().hex() == vector["encoded_hex"]
    assert transaction.txid_hex == vector["txid_hex"]
    assert Transaction.from_bytes(bytes.fromhex(vector["encoded_hex"])) == transaction
    validate_transaction(transaction, current_height=vector["valid_until_height"])


def test_changed_signed_field_invalidates_signature() -> None:
    transaction = _valid_transaction()
    changed = replace(transaction, amount_atoms=transaction.amount_atoms + 1)

    with pytest.raises(TransactionError, match="signature is invalid"):
        validate_transaction(changed, current_height=1)


def test_sender_must_match_public_key() -> None:
    transaction = _valid_transaction()
    changed = replace(transaction, sender_address=_recipient_address())

    with pytest.raises(TransactionError, match="does not match"):
        validate_transaction(changed, current_height=1)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"version": TRANSACTION_VERSION + 1}, "version"),
        ({"chain_id": "not-our-network"}, "another network"),
        ({"amount_atoms": 0}, "positive"),
        ({"amount_atoms": -1}, "unsigned 64-bit"),
        ({"fee_atoms": -1}, "unsigned 64-bit"),
        ({"amount_atoms": U64_MAX, "fee_atoms": 1}, "exceeds uint64"),
        ({"nonce": -1}, "unsigned 64-bit"),
        ({"signature": b"short"}, "64 bytes"),
    ],
)
def test_intrinsic_fields_are_strict(changes: dict[str, object], message: str) -> None:
    transaction = replace(_valid_transaction(), **changes)

    with pytest.raises(TransactionError, match=message):
        validate_transaction(transaction, current_height=1)


def test_expired_transaction_is_rejected() -> None:
    transaction = replace(_valid_transaction(), valid_until_height=9)

    with pytest.raises(TransactionError, match="expired"):
        validate_transaction(transaction, current_height=10)


def test_recipient_address_must_belong_to_network() -> None:
    other_network = NetworkConfig(
        name="other-test",
        chain_id="other-test-v1",
        address_hrp="xour",
        address_version=1,
    )
    private_key = generate_private_key()
    other_address = address_from_public_key(
        public_key_from_private(generate_private_key()),
        other_network,
    )

    with pytest.raises(TransactionError, match="invalid network address"):
        create_signed_transaction(
            private_key,
            recipient_address=other_address,
            amount_atoms=1,
            fee_atoms=0,
            nonce=0,
            valid_until_height=10,
            network=TESTNET,
        )


def test_decoder_rejects_truncated_trailing_and_wrong_domain_data() -> None:
    encoded = _valid_transaction().to_bytes()

    with pytest.raises(TransactionError, match="canonical"):
        Transaction.from_bytes(encoded[:-1])
    with pytest.raises(TransactionError, match="canonical"):
        Transaction.from_bytes(encoded + b"trailing")
    with pytest.raises(TransactionError, match="domain"):
        Transaction.from_bytes(b"\x00\x00\x00\x03bad")
