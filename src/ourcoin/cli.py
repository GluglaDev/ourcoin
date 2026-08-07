"""Command-line entry points for M5 local OurCoin workflows."""

import argparse
import getpass
import json
import shlex
import sys
from collections.abc import Sequence
from pathlib import Path

from ourcoin.consensus import ATOMS_PER_OUR
from ourcoin.encoding import U64_MAX
from ourcoin.node import LocalNode, NodeError
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


def run_shell() -> None:
    """Run an explicitly ephemeral, single-process local testnet session."""

    node = LocalNode()
    wallets: dict[str, Wallet] = {}
    print("OurCoin M5 local shell (in-memory; closing it discards all state)")
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


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ourcoin")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("demo", help="run a public two-wallet local workflow")
    subparsers.add_parser("shell", help="open an ephemeral local node session")
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
            run_shell()
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
    except (CliError, NodeError, WalletError, OSError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
