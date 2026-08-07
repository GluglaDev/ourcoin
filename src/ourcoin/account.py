"""Immutable account values used by deterministic state transitions."""

from dataclasses import dataclass

from ourcoin.encoding import U64_MAX


class AccountError(ValueError):
    """Raised when an account cannot be represented by consensus values."""


@dataclass(frozen=True, slots=True)
class Account:
    """An account balance and its exact next transaction nonce."""

    balance_atoms: int = 0
    nonce: int = 0

    def __post_init__(self) -> None:
        if type(self.balance_atoms) is not int or not 0 <= self.balance_atoms <= U64_MAX:
            raise AccountError("account balance must be an unsigned 64-bit integer")
        if type(self.nonce) is not int or not 0 <= self.nonce <= U64_MAX:
            raise AccountError("account nonce must be an unsigned 64-bit integer")
