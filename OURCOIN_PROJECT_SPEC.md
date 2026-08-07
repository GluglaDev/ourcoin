# OurCoin (OUR) — project specification v0.1

## 1. Vision

OurCoin is an independent cryptocurrency with its own blockchain, nodes, wallets and
peer-to-peer network. It is neither a token on another network nor a copy of Bitcoin's
code. The first goal is a local testnet. A separate public network may be designed only
after the protocol is stable.

This is an educational and experimental project. It must not handle assets with real
value before an independent security audit.

## 2. Agreed decisions

| Property | Decision |
|---|---|
| Name | OurCoin |
| Symbol | OUR |
| Type | Independent coin and blockchain |
| Consensus | Proof of Work |
| State model | Account balances and nonces |
| Maximum supply | 100,000,000 OUR |
| Target block time | 60 seconds |
| Initial reward | 40 OUR |
| Reward reduction | 20% every 500,000 blocks |
| Premine | None; all coins are mined |
| Release strategy | Local testnet, then a separately designed public network |

The theoretical emission is `40 × 500,000 × (1 + 0.8 + 0.8² + …) = 100,000,000 OUR`.

## 3. Units and emission

- `1 OUR = 100,000,000` atoms.
- Consensus monetary values are integers; `float` is forbidden.
- `MAX_SUPPLY = 10,000,000,000,000,000` atoms.
- The reward era is `block_height // 500_000`.
- The reward is the initial 40 OUR multiplied by `(4/5)^era`, rounded down to atoms.
- A reward must never make the supply exceed `MAX_SUPPLY`.
- Emission ends when the calculated reward is below one atom.
- Fees go to the miner and do not increase supply.
- Genesis has no reward and allocates no funds.

The reward calculation belongs in one consensus module and requires boundary tests for
heights 499,999, 500,000, 999,999 and 1,000,000, plus the end of emission.

## 4. Cryptography

The first version uses SHA-256 for transaction IDs, block IDs, Merkle roots and Proof of
Work, and Ed25519 for transaction signatures. Private keys come from the operating
system's cryptographic random generator.

Addresses encode the network version, a public-key hash and a checksum. Testnet uses the
`tour1` format documented in `docs/protocol.md`. A future mainnet must use a separately
chosen prefix and version; its final format remains unfrozen.

Private keys never enter the blockchain, network or logs. A wallet stores them only in
encrypted form using a key derived from the user's password. Keys and seed material must
never be committed to the repository or embedded in tests.

## 5. Accounts and transactions

Each account contains `balance_atoms` and the next expected `nonce`. A transaction has
version, chain ID, sender public key and address, recipient address, amount, fee, nonce,
expiry height and signature. Its transaction ID is derived as SHA-256 of the complete
canonical signed transaction rather than serialized as an independent field.

A valid transaction belongs to the current chain, derives its sender address from the
public key, has a valid signature over canonical bytes, transfers a positive amount,
uses a non-negative fee, is covered by the sender balance, uses the exact next nonce,
has not expired and has not already been confirmed.

Transaction execution is deterministic. Validation failure must not mutate state.

## 6. Blocks and consensus

A block header contains at least: version, chain ID, height, previous block hash,
transaction root, state root, timestamp, difficulty target, nonce and miner address.
The body contains one reward transaction followed by ordinary transactions.

A valid block links to its parent, has the next height, satisfies the required Proof of
Work and target, has correct transaction and state roots, applies valid transactions in
order, pays exactly the consensus reward plus fees, has an allowed timestamp and stays
within the transaction-count limit.

Nodes choose the branch with the greatest cumulative work, not simply the most blocks.
Testnet adjusts difficulty at heights divisible by 120 toward a 60-second average, using
119 timestamp intervals and limiting each target change to a factor of four. Tests use an
inexpensive test target.

## 7. Testnet and future mainnet

Networks are isolated by different chain IDs, genesis blocks, address prefixes, ports,
data directories and seed peers. The proposed testnet ID is `ourcoin-testnet-v1`; the
future mainnet ID is `ourcoin-mainnet-v1`.

Mainnet must remain inactive until genesis, consensus parameters and serialized formats
are deliberately frozen.

## 8. Repository architecture

```text
ourcoin/
├── AGENTS.md
├── README.md
├── PLAN.md
├── OURCOIN_PROJECT_SPEC.md
├── pyproject.toml
├── docs/
│   ├── architecture.md
│   ├── consensus.md
│   ├── protocol.md
│   ├── threat-model.md
│   └── testnet.md
├── src/ourcoin/
│   └── __init__.py
├── tests/
│   ├── unit/
│   ├── integration/
│   └── vectors/
└── data/
```

Later milestones add small modules for encoding, cryptography, transactions, accounts,
state, blocks, consensus, chain selection, mempool, mining, wallets, storage, P2P, the
node, API and CLI. Consensus, state, storage, networking and interfaces remain separate.

## 9. Implementation milestones

- **M0:** repository, Python 3.13 packaging, documentation and quality tools.
- **M1:** deterministic encoding, SHA-256, Ed25519, addresses and test vectors.
- **M2:** signed transactions, balances, nonces and deterministic state transition.
- **M3:** genesis, blocks, roots, emission, Proof of Work and atomic validation.
- **M4:** competing branches, cumulative work, reorganization and difficulty adjustment.
- **M5:** mempool, miner, wallet and CLI workflows.
- **M6:** transactional SQLite persistence, schema versions and state reconstruction.
- **M7:** P2P handshake, propagation, synchronization, resource limits and peer scoring.
- **M8:** read API, signed-transaction submission and a keyless explorer.
- **M9:** adversarial tests, parser fuzzing, threat model and a long-running testnet.

## 10. Planned CLI

```text
ourcoin init --network testnet
ourcoin wallet create
ourcoin wallet list
ourcoin address show
ourcoin balance ADDRESS
ourcoin send --wallet NAME --to ADDRESS --amount 12.5 --fee 0.01
ourcoin mine --wallet NAME
ourcoin chain info
ourcoin chain validate
ourcoin block show HASH
ourcoin transaction show TXID
ourcoin node start
ourcoin peers list
```

Decimal input is converted from text to atoms without `float`.

## 11. Mandatory consensus tests

Tests must cover altered signed data, false keys and signatures, transaction replay,
repeated or skipped nonces, insufficient funds, invalid amounts, cross-network data,
false mining rewards, supply overflow, reward-era boundaries, invalid parent and roots,
insufficient Proof of Work, invalid difficulty adjustment, cumulative-work fork choice,
reorganizations, interrupted persistence and unknown network message versions.

## 12. Codex rules

The durable development rules are stored in `AGENTS.md`. Any consensus change must also
update `docs/consensus.md` and relevant test vectors.

## 13. First Codex task

Perform M0 only. Create the Python 3.13 `src/ourcoin` project, configure pytest, Ruff and
mypy, create the documents required by M0 and add a minimal import test. Do not implement
cryptography, transactions, blocks, mining, networking or an API. Completion requires an
editable installation and passing tests, lint and type checking.

## 14. Deferred decisions

Before freezing a public protocol, decide the future mainnet address and wallet-backup
formats; block, transaction and mempool limits; mainnet difficulty rules; protocol version
activation; ports, seed peers and genesis parameters; project license; and the
security-update process.
