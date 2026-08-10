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

The signed transaction encoding is unchanged when carried in an M7 P2P frame.

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
an immutable tuple of at most 10,000 ordinary transactions.

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

## SQLite storage schema v1 (non-consensus)

M6 stores canonical block components rather than Python objects or executable serialization:

- `header_bytes` is exactly `BlockHeader.to_bytes()`;
- `reward_bytes` is exactly `RewardTransaction.to_bytes()`;
- every ordinary transaction BLOB is exactly `Transaction.to_bytes()` and carries its body
  position and derived transaction ID.

The body is reconstructed from those ordered components. M6 does not introduce a combined
block transport envelope and does not change any transaction, reward, header or block hash.

Schema version 1 contains network metadata, accepted blocks, ordered block transactions, the
canonical height index, accounts and the confirmed-transaction replay index. Heights, balances,
nonces and issued supply are canonical eight-byte unsigned big-endian BLOBs. Cumulative work is
a positive 40-byte unsigned big-endian BLOB, which covers the full consensus height range
without SQLite's signed-64-bit integer limit. Acceptance order is local node metadata used to
reproduce equal-work fork choices after restart.

Both `PRAGMA user_version` and the metadata schema version equal `1`. The database identity is
bound to `chain_id` and the exact genesis hash. These storage details are local and do not take
part in block validation or peer consensus.

## P2P wire protocol v1 (non-consensus)

Every TCP message uses this exact fixed prefix followed by its payload:

```text
magic[4] = "OURP"
u16 protocol_version = 1
u8 message_type
u32 payload_length
checksum[4] = first_4_bytes(SHA-256(payload))
payload[payload_length]
```

The maximum payload is 8 MiB. Parsers reject another magic, unknown version or message type,
truncation, trailing bytes, a bad checksum and an oversized declared payload before dispatch.
The first message in each direction must be `HELLO`. Its canonical payload contains a random
16-byte node ID, chain ID, exact genesis hash, active tip hash, height, positive cumulative
work encoded in 40 bytes, and listening port. A connection is rejected for a foreign identity,
self-connection or duplicate node ID.

Version 1 defines these messages:

- `HELLO` (1): peer and chain identity plus active-chain summary;
- `GET_BLOCKS` (2): a newest-first locator of at most 64 block hashes;
- `BLOCK` (3): one bounded block transport envelope;
- `TRANSACTION` (4): the existing canonical `Transaction.to_bytes()` value;
- `SYNC_COMPLETE` (5): tip hash, height and cumulative work;
- `PING` (6) and `PONG` (7): one canonical `u64` nonce;
- `REJECT` (8): bounded numeric code and public reason text.

An idle connection sends a nonce-bearing `PING`; the matching `PONG` proves liveness. A missing
or mismatched answer closes the connection.

The block transport envelope is
`bytes("OURCOIN:P2P:BLOCK:V1") || bytes(header) || bytes(reward) || sequence(transactions)`.
The three components are exactly the existing M1/M3 canonical encodings, so the envelope does
not change block IDs, transaction IDs, signatures, Merkle roots or consensus validation.

Locators use the active tip followed by increasingly sparse ancestors and always include
genesis. A response carries at most 128 consecutive active-chain blocks. If more remain, the
receiver observes the higher `SYNC_COMPLETE` work and requests the next batch. Every received
block and transaction passes through the existing node validation path before relay.
