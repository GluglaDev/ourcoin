# Testnet

Development targets the isolated `ourcoin-testnet-v1` network. Mainnet is out of scope
until a separate design task freezes its genesis and protocol parameters.

## Genesis v1

- Timestamp: `1786060800`
- Initial target: `0x0fffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff`
- Nonce: `10`
- Block hash: `0250f3a3c3d1c40da9d3aca119c0e44bbae7751e83fe37722ecfa0d5eeaa84f3`
- Initial reward and supply: `0`

The complete canonical vector is `tests/vectors/genesis.json`. M4 keeps the target unchanged
between boundaries and recalculates it at heights divisible by 120 using the preceding 119
timestamp intervals. A single recalculation is limited to a fourfold change.
