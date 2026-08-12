# Architecture

OurCoin is organized into five boundaries: deterministic consensus rules, account state,
persistent storage, peer networking and user-facing interfaces.

M1 introduces three dependency-light foundations:

- `encoding.py` owns platform-independent binary primitives;
- `crypto.py` is the only wrapper around SHA-256 and Ed25519 operations;
- `address.py` owns the checksummed textual representation of account identifiers;
- `config.py` contains immutable, network-specific identifiers.
- `transaction.py` defines immutable signed transfers and their canonical parser;
- `account.py` defines immutable balance and nonce values;
- `state.py` applies complete transaction batches to a working copy before committing.
- `merkle.py` provides the shared domain-separated tree construction;
- `block.py` defines reward transactions, headers and block bodies;
- `consensus.py` owns emission, genesis, block construction and atomic block execution;
- `miner.py` performs only the bounded Proof-of-Work nonce search.
- `chain.py` stores validated branches, branch-specific immutable state snapshots,
  cumulative work and the active-tip pointer.
- `mempool.py` stores validated pending transfers, reserves confirmed balances and selects
  eligible sender heads by fee without skipping nonces.
- `wallet.py` keeps Ed25519 signing material in memory and stores it in authenticated,
  password-encrypted files.
- `node.py` joins the chain, mempool and miner for single-process local workflows.
- `cli.py` parses fixed-point OUR amounts and exposes local workflows, storage commands and
  the persistent M7 peer service.
- `storage.py` owns the SQLite schema, network identity checks, atomic persistence, replay
  validation and reindexing; it never stores wallet secrets.
- `p2p_protocol.py` owns bounded canonical network frames and payload decoders. It treats every
  received byte as untrusted and does not define consensus encodings.
- `p2p.py` owns localhost TCP connections, handshakes, synchronization, relay, admission limits
  and peer scoring. It delegates all block and transaction decisions to `LocalNode`.

Later consensus objects must construct their signed or hashed bytes from the primitives in
`encoding.py`. They must not serialize dictionaries, JSON objects or Python object state.
Candidate construction and validation reuse the same state-transition and reward functions;
the mining module does not contain independent consensus rules.

M4 validates every new block against its own parent's snapshot, including side branches.
When a branch gains more cumulative work, the active view switches atomically to its already
validated snapshot. The returned reorganization record lists the disconnected path, common
ancestor and forward connected path for future mempool and persistence integration.

M5 consumes that reorganization record. Transactions confirmed on the connected path leave
the mempool, remaining entries are revalidated against the new active snapshot, and eligible
transactions from disconnected blocks are returned in chronological order. A side branch
that does not become active does not change pending transactions.

M6 keeps the in-memory `Chain` as the validation authority. A persistent node validates an
incoming block on an independent chain copy, commits the block and any canonical state switch
inside one SQLite transaction, and replaces its active in-memory chain only after the commit.
This prevents a failed database write from advancing either representation.

SQLite stores every validated branch in acceptance order. Startup replays those blocks through
the existing `Chain.add_block()` path, reproducing equal-work fork decisions without duplicating
consensus rules in storage. Accounts, confirmed transaction IDs, canonical-height mappings and
the current supply are derived caches that `chain reindex` can atomically rebuild from blocks.

Storage paths include both chain ID and genesis hash. Runtime opening is restricted to the
defined testnet; the path boundary is generic only so future networks cannot share data.

M7 wraps a persistent `LocalNode` without moving validation into the networking layer. Each
connection must exchange `HELLO` first and match both the testnet chain ID and exact genesis
hash. A peer advertises its active tip and cumulative work; a node that is behind sends a
bounded block locator, validates returned blocks through the normal node path, commits them
through M6 storage, and relays only accepted data. Side branches already held locally remain
preserved, while initial synchronization transfers the remote peer's active chain.

The asyncio service owns sockets only. `LocalNode` owns the chain and volatile mempool, and
`SQLiteChainStorage` owns durability. CLI shutdown closes peer tasks and listening sockets
before closing SQLite. M7 does not persist the mempool or wallet data.
