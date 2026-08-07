# Protocol

## Canonical encoding v1

- Unsigned integers use fixed widths of 1, 2, 4 or 8 bytes in big-endian order.
- Byte strings are `u32 byte_length || bytes`.
- Text is NFC-normalized, encoded as strict UTF-8 and then encoded as a byte string.
- Ordered byte sequences are `u32 item_count` followed by length-prefixed items.
- Boolean values are not integers and have no implicit encoding.
- Structure schemas define an exact field order; field names and Python object metadata are
  not serialized.
- Parsers reject truncation, trailing data, invalid UTF-8, non-NFC text and caller-defined
  resource-limit violations.

## Testnet address v1

The textual address is:

```text
"tour1" || lowercase_base32(version || public_key_hash || checksum)
```

- `version` is one byte and equals `0x6f` on `ourcoin-testnet-v1`.
- `public_key_hash` is the first 20 bytes of SHA-256 over a raw Ed25519 public key.
- `checksum` is the first 4 bytes of double SHA-256 over version and public-key hash.
- The 25-byte payload encodes to exactly 40 Base32 characters without padding.
- Mixed or uppercase forms, invalid checksums and mismatched network versions are rejected.

Example:

```text
tour1n4q74mo7ufkkeylcnp4fibdp2itrw67njmkxszhk
```

## Transaction v1

The signed payload is the following exact concatenation:

```text
bytes("OURCOIN:TRANSACTION:V1")
u16(version)
text(chain_id)
bytes(sender_public_key)
text(sender_address)
text(recipient_address)
u64(amount_atoms)
u64(fee_atoms)
u64(nonce)
u64(valid_until_height)
```

The full transaction is `signed_payload || bytes(signature)`. The signature is a raw
64-byte Ed25519 signature. `txid` is SHA-256 of the full canonical transaction and is
derived rather than serialized as an independent field.

Version 1 limits chain IDs to 64 UTF-8 bytes and addresses to 128 UTF-8 bytes. Public keys
must be exactly 32 bytes and signatures exactly 64 bytes. Decoders reject trailing bytes.

The public cross-platform vector is stored in `tests/vectors/transaction.json`. It contains
only public transaction material; no private key or seed is persisted.

Block-body transport encoding remains unfrozen until persistence and P2P milestones.

## Merkle tree v1

- An empty root is `SHA-256(0x00)`.
- A single leaf is its own root.
- Internal nodes are `SHA-256(0x01 || left || right)`.
- An odd final node is duplicated at each level.
- Transaction leaves are 32-byte transaction IDs.
- State leaves are SHA-256 of the account-state domain, address, balance and nonce, ordered
  lexicographically by address.

## Reward transaction v1

```text
bytes("OURCOIN:REWARD:V1")
u16(version)
text(chain_id)
u64(height)
text(miner_address)
u64(amount_atoms)
```

Its ID is SHA-256 of these canonical bytes. It is not signed and is valid only as the one
reward transaction of its matching block.

## Block header v1

```text
bytes("OURCOIN:BLOCK:HEADER:V1")
u16(version)
text(chain_id)
u64(height)
bytes(previous_block_hash)
bytes(transactions_root)
bytes(state_root)
u64(timestamp)
bytes(difficulty_target_as_32_byte_big_endian)
u64(nonce)
text(miner_address)
```

The block ID is SHA-256 of the header. Proof of Work is valid when the header hash interpreted
as an unsigned big-endian integer is less than or equal to the target.

The transaction root contains the reward ID followed by ordinary transaction IDs. The state
root describes the state after ordinary transfers and the miner reward. M3 block bodies use
an immutable tuple of at most 10,000 ordinary transactions; their transport encoding remains
deferred until persistence and P2P milestones.

## Chain work and difficulty v1

Nodes compare branches using the sum of `floor(2^256 / (target + 1))` for every header,
including genesis. Block count alone is not a fork-choice rule. Equal cumulative work does
not trigger a tip switch.

For a candidate height not divisible by 120, its target equals its parent's target. At a
positive height divisible by 120, the target is calculated from the timestamps of its parent
and its ancestor 120 heights earlier:

```text
expected_span = 119 * 60
bounded_span = clamp(parent.timestamp - ancestor.timestamp,
                     expected_span / 4,
                     expected_span * 4)
new_target = clamp(parent.target * bounded_span // expected_span,
                   1,
                   2^256 - 1)
```

All operations are integer operations. A header carrying any other target is invalid even if
its hash happens to satisfy that target.

## Encrypted wallet file v1 (non-consensus)

M5 wallet files are JSON containers identified by `ourcoin-wallet-v1`. Public metadata
contains the wallet name, testnet chain ID, address and public key. A fresh 16-byte salt feeds
scrypt with `N=16384`, `r=8`, `p=1`; the resulting 32-byte key encrypts the raw Ed25519 private
seed with AES-256-GCM and a fresh 12-byte nonce. All public identity metadata is authenticated
as associated data, so changing it makes decryption fail.

The file format is local storage, not part of transaction or block consensus. Private key
material is never written as plaintext, and the CLI obtains passwords interactively instead
of accepting them in process arguments.
