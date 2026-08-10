# OurCoin implementation plan

Only one milestone is implemented at a time. Every milestone requires tests and review
before work starts on the next one.

- [x] **M0 — repository:** Python 3.13 project, documentation and quality tooling.
- [x] **M1 — encoding and cryptography:** canonical encoding, SHA-256, Ed25519 and addresses.
- [x] **M2 — transactions and account state:** signed transactions, balances and nonces.
- [x] **M3 — blocks, emission and Proof of Work:** genesis, block validation and mining.
- [x] **M4 — chain and reorganizations:** cumulative work, forks, rollback and difficulty.
- [x] **M5 — mempool, miner and CLI:** transaction selection, wallets and local workflows.
- [x] **M6 — persistent storage:** transactional SQLite storage and state reconstruction.
- [ ] **M7 — peer-to-peer network:** synchronization, propagation and peer limits.
- [ ] **M8 — API and explorer:** read API, transaction submission and local explorer.
- [ ] **M9 — testnet hardening:** adversarial tests, fuzzing and long-running testnet.

M6 is complete with identity-isolated SQLite storage, atomic block/state commits, full
branch replay, state reindexing and a persistent testnet shell.
