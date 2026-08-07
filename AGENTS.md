# OurCoin development rules

## Goal

Build an educational independent cryptocurrency named OurCoin (OUR) in Python 3.13.
It has its own account-based blockchain and Proof-of-Work network. Work on the testnet
only unless the user explicitly starts a separate mainnet-design task.

## Scope

- Implement one milestone from PLAN.md at a time.
- Do not copy Bitcoin Core or introduce Bitcoin-specific behavior not present in
  OURCOIN_PROJECT_SPEC.md.
- Do not silently change consensus constants or serialized formats.
- Keep consensus, state, storage, networking and interfaces separated.
- Prefer minimal dependencies and small typed modules.

## Consensus safety

- Never use float for amounts, rewards, difficulty or consensus calculations.
- Canonical encoding must be deterministic across platforms.
- Validate all untrusted file, CLI, API and peer input.
- Never log, transmit or commit private keys, passwords or wallet seed material.
- Update docs/consensus.md and test vectors with every intentional consensus change.
- A validation failure must not partially mutate persistent or in-memory state.

## Verification

After each change run:

- `python -m pytest`
- `python -m ruff check .`
- `python -m mypy src`

Before completing a task:

- review the diff for consensus or security regressions;
- report changed files and commands run;
- report exact test results;
- list unresolved risks and assumptions;
- do not claim completion if verification failed.
