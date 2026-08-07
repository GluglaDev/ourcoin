# OurCoin (OUR)

OurCoin is an educational, experimental cryptocurrency implemented in Python 3.13.
It will use its own account-based blockchain, Proof of Work consensus, wallets and
peer-to-peer network. It is not a token on another chain and is not a Bitcoin Core fork.

Milestone **M5 — mempool, miner and CLI** is complete. The repository contains a
nonce-aware pending pool, fee-prioritized mining, authenticated encrypted wallet files and
a single-process local node that reconciles pending transactions after reorganizations.
Persistence and networking are not implemented yet.

## Safety

OurCoin must not be used for assets with real monetary value without an independent
security audit and a deliberately designed public protocol.

## Development setup

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
```

Run the quality checks:

```powershell
python -m pytest
python -m ruff check .
python -m mypy src
```

Try the public local workflow or open an ephemeral session:

```powershell
ourcoin demo
ourcoin shell
```

The shell supports `wallet`, `address`, `balance`, `send`, `mine` and `chain`. Its wallets
and chain are held only in memory and disappear when the process exits; persistence is M6.
Encrypted wallet files can be created with `ourcoin wallet create --name NAME --output FILE`
and inspected after unlocking with `ourcoin wallet show --file FILE`. Passwords are prompted
without being accepted as command-line arguments.

See [OURCOIN_PROJECT_SPEC.md](OURCOIN_PROJECT_SPEC.md) for the protocol assumptions and
[PLAN.md](PLAN.md) for the milestone roadmap.
