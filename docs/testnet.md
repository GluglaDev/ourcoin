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

## M6 data directory

The default testnet database is:

```text
data/ourcoin-testnet-v1/0250f3a3c3d1c40da9d3aca119c0e44bbae7751e83fe37722ecfa0d5eeaa84f3/blockchain.sqlite3
```

The directory includes both network identity fields to prevent accidental reuse by another
genesis. M6 refuses all other runtime networks with `UnsupportedNetworkError`; it does not
define or activate mainnet parameters. Use `--data-dir PATH` to choose another base directory.

## M7 local peer network

The default peer endpoint is `127.0.0.1:19733`. Port `0` may be used by tests to request an
ephemeral operating-system port. Runtime listening and dialing are restricted to the loopback
names and addresses `127.0.0.1`, `::1` and `localhost`.

Peers must present `ourcoin-testnet-v1` and the exact genesis hash above in their first framed
message. M7 intentionally has no seed list, peer discovery, DNS bootstrap, NAT traversal, TLS,
mainnet identity or public internet deployment. A devnet identity is not activated.
