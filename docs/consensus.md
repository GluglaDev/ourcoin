# Consensus

The accepted testnet assumptions are recorded in `OURCOIN_PROJECT_SPEC.md`.

## M1 foundations

- Consensus hashes are SHA-256 digests of explicitly defined canonical bytes.
- Transaction signatures use Ed25519 with raw 32-byte private seeds, 32-byte public keys
  and 64-byte signatures.
- Public-key hashes are the first 20 bytes of SHA-256 over the raw public key.
- Text is NFC-normalized before encoding. Decoders reject non-NFC encodings.
- Integers are unsigned, fixed-width and big-endian. Variable data uses a `u32` byte length.

Exact transaction and block field layouts are not defined until their respective milestones.

## M2 transaction rules

- Version 1 transactions are bound to one `chain_id` and cover all fields with Ed25519.
- The sender address must be derived from the included public key.
- Amount is positive, fee is non-negative and their sum fits `u64`.
- `nonce` must equal the account's exact next nonce; successful execution increments it once.
- A transaction is valid through `valid_until_height`, inclusive.
- A confirmed `txid` cannot execute again.
- The sender balance must cover amount plus fee.
- A failed transaction or batch does not mutate account state or the replay index.
- A self-transfer changes only the fee and nonce.
- Fees leave sender state during transaction execution and M3 block execution credits them
  to the miner together with the subsidy.

## M3 blocks and emission

- Genesis is the exact public vector in `tests/vectors/genesis.json`, has height 0, no
  ordinary transactions, zero reward and zero initial supply.
- A normal block has exactly one reward transaction represented separately before its tuple
  of ordinary transactions.
- The subsidy is 40 OUR through height 499,999 and is multiplied by `4/5`, rounded down to
  atoms, at every 500,000-block boundary. Height 0 is a special zero-reward block.
- Subsidy is capped by the remaining `MAX_SUPPLY_ATOMS`; fees never increase total supply.
- The reward amount equals subsidy plus all ordinary-transaction fees.
- The transaction root commits to the reward transaction first, then ordinary transactions.
- The state root commits to sorted address, balance and nonce leaves after all transfers and
  the reward have executed.
- Block validation executes on a copied account state and returns the copy only on success.
- Outside an M4 adjustment boundary, the target equals the parent's target.
- The timestamp must be strictly greater than the parent's timestamp. A stronger time policy
  is deferred until chain rules are implemented.
- The branch-independent M3 safety limit is 10,000 ordinary transactions per block.

## M4 chain selection and difficulty

- Per-block work is `floor(2^256 / (target + 1))`.
- Cumulative work includes genesis and every block on the branch.
- The active branch is changed only when another validated tip has strictly greater
  cumulative work. Equal-work branches remain stored without replacing the current tip.
- Side-branch blocks execute against their own parent's immutable state snapshot.
- A tip switch atomically changes state and supply together and reports the common ancestor,
  disconnected hashes and forward connected hashes.
- Difficulty is recalculated for block heights divisible by 120.
- At height `H`, the window spans the ancestor at `H - 120` through the parent at `H - 1`,
  which contains 119 timestamp intervals. The expected span is `119 × 60 = 7140` seconds.
- `new_target = floor(parent_target × bounded_actual_span / 7140)`.
- The actual span is clamped to `[7140 / 4, 7140 × 4]`, limiting one adjustment to a factor
  of four harder or easier. The final target is clamped to `[1, 2^256 - 1]`.

## M5 mempool and mining policy

M5 does not change transaction or block consensus. A node may use another mempool policy and
still produce valid blocks. The reference local policy is:

- reject confirmed, duplicate and conflicting `(sender, nonce)` transactions;
- accept only a contiguous pending nonce sequence starting at the confirmed account nonce;
- reserve amount plus fee for all pending transactions from a sender;
- limit the pool to 50,000 transactions and each sender to 64 pending transactions;
- select the highest absolute-fee eligible sender head, breaking equal fees by `txid`;
- unlock the next nonce from a sender only after its preceding transaction is selected;
- revalidate pending entries after every active-tip change and reconsider eligible transfers
  from disconnected blocks.

Miners remain responsible for building a fully valid candidate. Consensus validation never
trusts the order or validity decisions made by the mempool.
