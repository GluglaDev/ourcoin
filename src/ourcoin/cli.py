"""Command-line entry points for local OurCoin workflows through M7."""

import argparse
import asyncio
import getpass
import json
import shlex
import sys
from collections.abc import Sequence
from pathlib import Path

from ourcoin.consensus import ATOMS_PER_OUR
from ourcoin.encoding import U64_MAX
from ourcoin.node import LocalNode, NodeError
from ourcoin.p2p import (
    DEFAULT_LISTEN_HOST,
    DEFAULT_TESTNET_PORT,
    LOOPBACK_HOSTS,
    P2PError,
    P2PNode,
)
from ourcoin.storage import SQLiteChainStorage, StorageError, StorageInfo
from ourcoin.wallet import Wallet, WalletError

DEFAULT_FEE_ATOMS = ATOMS_PER_OUR // 100


class CliError(ValueError):
    """Raised for invalid human-facing command input."""


def parse_our_amount(value: str, *, allow_zero: bool = False) -> int:
    """Parse a fixed-point OUR value without ever using binary floating point."""

    if type(value) is not str or not value or value.strip() != value:
        raise CliError("amount must be plain decimal text")
    whole, separator, fraction = value.partition(".")
    if not whole.isascii() or not whole.isdecimal():
        raise CliError("amount must be a non-negative plain decimal")
    if separator and (not fraction or not fraction.isascii() or not fraction.isdecimal()):
        raise CliError("amount fraction must contain decimal digits")
    if len(fraction) > 8:
        raise CliError("amount supports at most 8 decimal places")
    atoms = int(whole) * ATOMS_PER_OUR + int(fraction.ljust(8, "0") or "0")
    if atoms > U64_MAX:
        raise CliError("amount exceeds the uint64 range")
    if atoms == 0 and not allow_zero:
        raise CliError("amount must be positive")
    return atoms


def format_atoms(atoms: int) -> str:
    if type(atoms) is not int or not 0 <= atoms <= U64_MAX:
        raise CliError("atoms must be an unsigned 64-bit integer")
    whole, fraction = divmod(atoms, ATOMS_PER_OUR)
    return f"{whole}.{fraction:08d}"


def demo_summary() -> dict[str, object]:
    """Run a deterministic two-wallet workflow and return public information only."""

    node = LocalNode()
    alice = Wallet.create("alice")
    bob = Wallet.create("bob")
    node.mine(alice.address)
    transaction = node.send(
        alice,
        recipient_address=bob.address,
        amount_atoms=parse_our_amount("12.5"),
        fee_atoms=parse_our_amount("0.01", allow_zero=True),
    )
    mined = node.mine(bob.address)
    return {
        "network": node.network.chain_id,
        "height": node.chain.height,
        "block_hash": mined.block.block_hash.hex(),
        "transaction_id": transaction.txid_hex,
        "mempool_size": len(node.mempool),
        "total_supply_our": format_atoms(node.chain.total_supply_atoms),
        "alice": {
            "address": alice.address,
            "balance_our": format_atoms(node.account(alice.address).balance_atoms),
        },
        "bob": {
            "address": bob.address,
            "balance_our": format_atoms(node.account(bob.address).balance_atoms),
        },
    }


def _resolve_address(value: str, wallets: dict[str, Wallet]) -> str:
    wallet = wallets.get(value)
    return wallet.address if wallet is not None else value


def _shell_help() -> str:
    return (
        "commands: wallet NAME | address NAME | balance NAME_OR_ADDRESS | mine NAME | "
        "send FROM TO AMOUNT [FEE] | chain | help | quit"
    )


def _run_shell_session(node: LocalNode) -> None:
    wallets: dict[str, Wallet] = {}
    print("OurCoin M6 local shell (blockchain state is persistent; wallets are in-memory)")
    print(_shell_help())
    while True:
        try:
            line = input("ourcoin> ")
        except EOFError:
            print()
            return
        try:
            parts = shlex.split(line)
            if not parts:
                continue
            command, *arguments = parts
            if command in {"quit", "exit"}:
                return
            if command == "help":
                print(_shell_help())
            elif command == "wallet" and len(arguments) == 1:
                name = arguments[0]
                if name in wallets:
                    raise CliError("wallet name already exists in this session")
                wallets[name] = Wallet.create(name)
                print(wallets[name].address)
            elif command == "address" and len(arguments) == 1:
                print(wallets[arguments[0]].address)
            elif command == "balance" and len(arguments) == 1:
                address = _resolve_address(arguments[0], wallets)
                print(format_atoms(node.account(address).balance_atoms))
            elif command == "mine" and len(arguments) == 1:
                wallet = wallets[arguments[0]]
                result = node.mine(wallet.address)
                print(f"height={node.chain.height} block={result.block.block_hash.hex()}")
            elif command == "send" and len(arguments) in {3, 4}:
                sender_name, recipient_name, amount_text, *fee_text = arguments
                transaction = node.send(
                    wallets[sender_name],
                    recipient_address=_resolve_address(recipient_name, wallets),
                    amount_atoms=parse_our_amount(amount_text),
                    fee_atoms=(
                        parse_our_amount(fee_text[0], allow_zero=True)
                        if fee_text
                        else DEFAULT_FEE_ATOMS
                    ),
                )
                print(transaction.txid_hex)
            elif command == "chain" and not arguments:
                print(
                    f"height={node.chain.height} mempool={len(node.mempool)} "
                    f"supply={format_atoms(node.chain.total_supply_atoms)}"
                )
            else:
                raise CliError("unknown command or wrong number of arguments")
        except (CliError, KeyError, NodeError, ValueError) as error:
            message = "wallet does not exist" if isinstance(error, KeyError) else str(error)
            print(f"error: {message}")


def run_shell(data_dir: str | Path = Path("data")) -> None:
    """Run a persistent single-process local testnet session."""

    with LocalNode.open_persistent(data_dir) as node:
        _run_shell_session(node)


def _storage_info_document(info: StorageInfo) -> dict[str, object]:
    return {
        "schema_version": info.schema_version,
        "chain_id": info.chain_id,
        "genesis_hash": info.genesis_hash.hex(),
        "database_path": str(info.database_path.resolve()),
        "height": info.height,
        "tip_hash": info.tip_hash.hex(),
        "cumulative_work": str(info.cumulative_work),
        "issued_supply_atoms": info.issued_supply_atoms,
        "issued_supply_our": format_atoms(info.issued_supply_atoms),
        "block_count": info.block_count,
        "account_count": info.account_count,
    }


def _parse_peer_endpoint(value: str) -> tuple[str, int]:
    host, separator, port_text = value.rpartition(":")
    if not separator or host not in LOOPBACK_HOSTS:
        raise CliError("peer must use localhost as HOST:PORT")
    try:
        port = int(port_text)
    except ValueError as error:
        raise CliError("peer port must be a decimal integer") from error
    if not 1 <= port <= 0xFFFF:
        raise CliError("peer port must be between 1 and 65535")
    return host, port


async def run_network_node(
    data_dir: str | Path,
    *,
    host: str = DEFAULT_LISTEN_HOST,
    port: int = DEFAULT_TESTNET_PORT,
    peer_endpoints: Sequence[str] = (),
    stop_event: asyncio.Event | None = None,
) -> None:
    """Run a persistent localhost-only testnet peer until stopped."""

    local_node = LocalNode.open_persistent(data_dir)
    peer_node = P2PNode(local_node, host=host, port=port)
    try:
        await peer_node.start()
        connected: list[dict[str, object]] = []
        for value in peer_endpoints:
            peer_host, peer_port = _parse_peer_endpoint(value)
            info = await peer_node.connect(peer_host, peer_port)
            connected.append(
                {
                    "node_id": info.node_id,
                    "host": info.host,
                    "port": info.port,
                }
            )
        print(
            json.dumps(
                {
                    "chain_id": local_node.network.chain_id,
                    "height": local_node.chain.height,
                    "host": peer_node.host,
                    "node_id": peer_node.node_id.hex(),
                    "peers": connected,
                    "port": peer_node.port,
                    "tip_hash": local_node.chain.tip_hash.hex(),
                },
                sort_keys=True,
            ),
            flush=True,
        )
        event = asyncio.Event() if stop_event is None else stop_event
        await event.wait()
    finally:
        await peer_node.close()
        local_node.close()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ourcoin")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("demo", help="run a public two-wallet local workflow")
    shell = subparsers.add_parser("shell", help="open a persistent local node session")
    shell.add_argument("--data-dir", type=Path, default=Path("data"))
    chain = subparsers.add_parser("chain", help="inspect and rebuild persistent chain data")
    chain_commands = chain.add_subparsers(dest="chain_command", required=True)
    chain_info = chain_commands.add_parser("info", help="show persistent chain metadata")
    chain_info.add_argument("--data-dir", type=Path, default=Path("data"))
    chain_validate = chain_commands.add_parser(
        "validate", help="validate SQLite data by replaying all blocks"
    )
    chain_validate.add_argument("--data-dir", type=Path, default=Path("data"))
    chain_reindex = chain_commands.add_parser(
        "reindex", help="atomically rebuild persistent derived state"
    )
    chain_reindex.add_argument("--data-dir", type=Path, default=Path("data"))
    node = subparsers.add_parser("node", help="run the local testnet peer service")
    node_commands = node.add_subparsers(dest="node_command", required=True)
    node_start = node_commands.add_parser("start", help="start a persistent P2P node")
    node_start.add_argument("--data-dir", type=Path, default=Path("data"))
    node_start.add_argument("--host", choices=sorted(LOOPBACK_HOSTS), default=DEFAULT_LISTEN_HOST)
    node_start.add_argument("--port", type=int, default=DEFAULT_TESTNET_PORT)
    node_start.add_argument(
        "--peer",
        action="append",
        default=[],
        metavar="HOST:PORT",
        help="connect to a localhost peer; may be repeated",
    )
    wallet = subparsers.add_parser("wallet", help="manage encrypted wallet files")
    wallet_commands = wallet.add_subparsers(dest="wallet_command", required=True)
    create = wallet_commands.add_parser("create", help="create an encrypted wallet")
    create.add_argument("--name", required=True)
    create.add_argument("--output", type=Path, required=True)
    show = wallet_commands.add_parser("show", help="unlock and show public wallet metadata")
    show.add_argument("--file", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "demo":
            print(json.dumps(demo_summary(), indent=2, sort_keys=True))
        elif arguments.command == "shell":
            run_shell(arguments.data_dir)
        elif arguments.command == "chain":
            create = arguments.chain_command == "info"
            with SQLiteChainStorage.open(arguments.data_dir, create=create) as storage:
                if arguments.chain_command == "info":
                    document = _storage_info_document(storage.info())
                elif arguments.chain_command == "validate":
                    validation_report = storage.validate()
                    document = {
                        "valid": validation_report.valid,
                        "block_count": validation_report.block_count,
                        "account_count": validation_report.account_count,
                        "height": validation_report.height,
                        "tip_hash": validation_report.tip_hash.hex(),
                    }
                else:
                    reindex_report = storage.reindex()
                    document = {
                        "reindexed": True,
                        "block_count": reindex_report.block_count,
                        "account_count": reindex_report.account_count,
                        "height": reindex_report.height,
                        "tip_hash": reindex_report.tip_hash.hex(),
                    }
                print(json.dumps(document, indent=2, sort_keys=True))
        elif arguments.command == "node":
            try:
                asyncio.run(
                    run_network_node(
                        arguments.data_dir,
                        host=arguments.host,
                        port=arguments.port,
                        peer_endpoints=arguments.peer,
                    )
                )
            except KeyboardInterrupt:
                return 0
        elif arguments.wallet_command == "create":
            password = getpass.getpass("Wallet password: ")
            confirmation = getpass.getpass("Confirm password: ")
            if password != confirmation:
                raise CliError("password confirmation does not match")
            wallet = Wallet.create(arguments.name)
            wallet.save_encrypted(arguments.output, password)
            print(json.dumps({"name": wallet.name, "address": wallet.address}, sort_keys=True))
        elif arguments.wallet_command == "show":
            password = getpass.getpass("Wallet password: ")
            wallet = Wallet.load_encrypted(arguments.file, password)
            print(
                json.dumps(
                    {
                        "name": wallet.name,
                        "address": wallet.address,
                        "network": wallet.network.chain_id,
                    },
                    sort_keys=True,
                )
            )
    except (CliError, NodeError, P2PError, StorageError, WalletError, OSError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
