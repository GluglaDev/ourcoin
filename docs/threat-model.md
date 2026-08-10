# Threat model

The complete threat model is a milestone M9 deliverable. From M0 onward, all file, CLI,
API and peer input is untrusted, secrets must never be logged, and validation failures
must not partially mutate state.

## M5 wallet and local-node boundary

- Wallet files use scrypt plus AES-256-GCM with randomized salt and nonce; public wallet
  identity fields are authenticated.
- Writes use a restrictive permission request and a same-directory temporary file. Creating
  a new wallet refuses an existing destination atomically; explicit overwrite uses atomic
  replacement on the same filesystem.
- Passwords and private seeds necessarily exist in Python process memory while a wallet is
  unlocked. Python does not guarantee complete zeroization, so a compromised process can
  recover them.
- The shell state is ephemeral and has no peer authentication, persistence or remote API.
  It must not be exposed as a service or used for assets with real monetary value.
- Mempool limits and per-sender caps reduce simple memory exhaustion but do not replace the
  peer-level admission, rate limiting and eviction rules required in M7.

## M6 persistent storage boundary

- SQLite enables foreign keys, WAL journaling, `synchronous=FULL`, a busy timeout and untrusted
  schema restrictions. A block, its transactions and any canonical state switch share one
  explicit transaction.
- Validation happens on an independent in-memory chain copy. A validation or write error rolls
  back SQLite and leaves the active chain unchanged; the mempool remains deliberately volatile.
- Startup and `chain validate` replay canonical bytes through the existing consensus engine and
  compare the result with cached tip, supply, accounts and replay IDs.
- `chain reindex` repairs only derived indexes and state after validating raw blocks. It refuses
  corrupted block bytes, an unknown schema, a foreign chain ID or a different genesis.
- WAL improves crash durability but is not a backup. Operators must copy the database together
  with its WAL using SQLite-aware backup procedures while the node is stopped or coordinated.
- M6 has no multi-process writer coordination beyond SQLite locking, no pruning and no storage
  quotas. These remain operational risks for later node and networking milestones.

## M7 local P2P boundary

- TCP frames have fixed prefixes, checksums, an 8 MiB payload ceiling and bounded canonical
  payload collections. Unknown protocol versions and malformed input close the connection.
- The handshake binds peers to both `chain_id` and genesis hash and rejects self-connections
  and duplicate node IDs. Node IDs are ephemeral identifiers, not authentication keys.
- Global and per-IP connection limits, per-minute message/byte limits and peer scores bound
  basic resource abuse. Invalid blocks and transactions add penalties; threshold violations
  disconnect and temporarily ban the loopback address.
- Network input never bypasses transaction, Proof-of-Work, state or chain validation. SQLite
  persistence still uses the M6 candidate-copy and atomic-commit boundary.
- M7 is localhost-only. It provides no confidentiality, peer authentication, Sybil resistance,
  eclipse resistance, public discovery, bandwidth scheduling or denial-of-service protection
  suitable for an internet-facing node. Exposing its port beyond loopback is unsupported.
- The mempool remains volatile and peer announcements are not persisted. A restart recovers the
  chain and account state, then learns pending transactions only from new peer traffic.
