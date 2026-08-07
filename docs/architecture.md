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
- `cli.py` parses fixed-point OUR amounts and exposes the M5 demo, shell and wallet commands.

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

The M5 node is deliberately in memory. It is an integration boundary, not yet a daemon or a
network peer; transactional chain storage and restart reconstruction belong to M6.
