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
