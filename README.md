# OurCoin (OUR)

OurCoin is an educational, experimental cryptocurrency implemented in Python 3.13.
It will use its own account-based blockchain, Proof of Work consensus, wallets and
peer-to-peer network. It is not a token on another chain and is not a Bitcoin Core fork.

Milestone **M7 — peer-to-peer network** is complete. Local testnet nodes communicate over
bounded, canonical TCP frames, verify network identity during the handshake, synchronize the
active chain and propagate validated blocks and transactions. SQLite preserves the blockchain,
canonical account state and issued supply across node restarts.

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

The shell supports `wallet`, `address`, `balance`, `send`, `mine` and `chain`. Blockchain
state is stored under `data/<chain_id>/<genesis_hash>/blockchain.sqlite3`; session wallets
remain in memory and disappear when the process exits.
Encrypted wallet files can be created with `ourcoin wallet create --name NAME --output FILE`
and inspected after unlocking with `ourcoin wallet show --file FILE`. Passwords are prompted
without being accepted as command-line arguments.

Inspect or verify persistent chain data:

```powershell
ourcoin chain info
ourcoin chain validate
ourcoin chain reindex
```

All persistent commands accept `--data-dir PATH`. M7 supports only the defined testnet;
mainnet activation remains a separate consensus-design task.

Start two localhost peers in separate terminals:

```powershell
ourcoin node start --data-dir data/node-a --port 19733
ourcoin node start --data-dir data/node-b --port 19734 --peer 127.0.0.1:19733
```

`node start` binds only to `127.0.0.1`, `::1` or `localhost`. There are no public seeds,
internet exposure, NAT traversal or encrypted transport in M7. Stop a node with Ctrl+C;
the network connection and SQLite database are then closed in a controlled order.

See [OURCOIN_PROJECT_SPEC.md](OURCOIN_PROJECT_SPEC.md) for the protocol assumptions and
[PLAN.md](PLAN.md) for the milestone roadmap.
