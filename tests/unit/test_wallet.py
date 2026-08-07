import json

import pytest

from ourcoin.transaction import validate_transaction
from ourcoin.wallet import Wallet, WalletError


def test_wallet_creates_valid_signed_transaction() -> None:
    alice = Wallet.create("alice")
    bob = Wallet.create("bob")

    transaction = alice.create_transaction(
        recipient_address=bob.address,
        amount_atoms=100,
        fee_atoms=2,
        nonce=0,
        valid_until_height=10,
    )

    validate_transaction(transaction, current_height=1)
    assert transaction.sender_address == alice.address
    assert alice.address not in repr(alice).replace(alice.address, "")


def test_encrypted_wallet_round_trip_contains_no_plaintext_private_key(tmp_path) -> None:
    path = tmp_path / "alice.wallet.json"
    wallet = Wallet.create("alice")
    wallet.save_encrypted(path, "correct horse battery staple")

    document = json.loads(path.read_text(encoding="utf-8"))
    loaded = Wallet.load_encrypted(path, "correct horse battery staple")

    assert loaded.name == wallet.name
    assert loaded.address == wallet.address
    assert loaded.public_key == wallet.public_key
    transaction = loaded.create_transaction(
        recipient_address=Wallet.create("recipient").address,
        amount_atoms=1,
        fee_atoms=0,
        nonce=0,
        valid_until_height=1,
    )
    validate_transaction(transaction, current_height=1)
    assert "private_key" not in path.read_text(encoding="utf-8")
    assert document["cipher"]["name"] == "aes-256-gcm"
    assert document["kdf"]["name"] == "scrypt"


def test_wallet_rejects_wrong_password_and_authenticated_metadata_change(tmp_path) -> None:
    path = tmp_path / "alice.wallet.json"
    Wallet.create("alice").save_encrypted(path, "secret")

    with pytest.raises(WalletError, match="password is invalid|file was modified"):
        Wallet.load_encrypted(path, "wrong")

    document = json.loads(path.read_text(encoding="utf-8"))
    document["address"] = Wallet.create("other").address
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(WalletError, match="password is invalid|file was modified"):
        Wallet.load_encrypted(path, "secret")


def test_wallet_does_not_overwrite_existing_file_by_default(tmp_path) -> None:
    path = tmp_path / "wallet.json"
    wallet = Wallet.create("alice")
    wallet.save_encrypted(path, "secret")

    with pytest.raises(WalletError, match="already exists"):
        wallet.save_encrypted(path, "secret")


@pytest.mark.parametrize("name", ["", "space name", "żółw", "a" * 65])
def test_wallet_rejects_unsafe_names(name: str) -> None:
    with pytest.raises(WalletError, match="wallet name"):
        Wallet.create(name)
