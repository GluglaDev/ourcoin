"""Network configuration values that are safe to import from consensus code."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class NetworkConfig:
    """Immutable identifiers for one isolated OurCoin network."""

    name: str
    chain_id: str
    address_hrp: str
    address_version: int

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("network name must not be empty")
        if not self.chain_id or not self.chain_id.isascii():
            raise ValueError("chain_id must be non-empty ASCII")
        if (
            not self.address_hrp
            or not self.address_hrp.isascii()
            or not self.address_hrp.isalnum()
            or self.address_hrp != self.address_hrp.lower()
            or not self.address_hrp[0].isalpha()
        ):
            raise ValueError("address_hrp must be lowercase ASCII letters and digits")
        if type(self.address_version) is not int or not 0 <= self.address_version <= 0xFF:
            raise ValueError("address_version must be an unsigned byte")


TESTNET = NetworkConfig(
    name="testnet",
    chain_id="ourcoin-testnet-v1",
    address_hrp="tour",
    address_version=0x6F,
)
