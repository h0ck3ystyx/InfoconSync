"""CLI entry point with stable flags for all planned commands."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="infocon-librarian",
        description="Local-first InfoCon archive steward",
    )
    parser.add_argument(
        "--root",
        type=Path,
        metavar="PATH",
        help="Archive root directory",
    )
    parser.add_argument(
        "--format",
        choices=["human", "json"],
        default="human",
        help="Output format (default: human)",
    )

    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    # check
    check = sub.add_parser("check", help="Check for upstream changes")
    check.add_argument("--section", metavar="NAME", help="Limit to a specific section")
    check.add_argument(
        "--fresh",
        action="store_true",
        help="Bypass cache and fetch fresh upstream listings",
    )
    check.add_argument(
        "--format",
        choices=["human", "json"],
        default="human",
    )
    check.add_argument(
        "--torrent-mode",
        choices=["auto", "only", "off"],
        default="auto",
        metavar="MODE",
    )

    # plan
    plan = sub.add_parser("plan", help="Build a transfer plan")
    plan.add_argument("--new", action="store_true", help="Include new collections")
    plan.add_argument("--changed", action="store_true", help="Include changed collections")
    plan.add_argument("--format", choices=["human", "json"], default="human")
    plan.add_argument("--dry-run", action="store_true", help="Print plan without saving")
    plan.add_argument(
        "--torrent-mode",
        choices=["auto", "only", "off"],
        default="auto",
        metavar="MODE",
    )
    plan.add_argument("--allow-http-fallback", action="store_true", default=False)
    plan.add_argument("--download-limit", type=int, metavar="RATE", default=0)
    plan.add_argument("--upload-limit", type=int, metavar="RATE", default=0)

    # verify
    verify = sub.add_parser("verify", help="Piece-verify a local collection")
    verify.add_argument("collection", metavar="COLLECTION")
    verify.add_argument("--format", choices=["human", "json"], default="human")

    # sync
    sync = sub.add_parser("sync", help="Execute a transfer plan")
    sync.add_argument("plan_id", metavar="PLAN_ID")
    sync.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate plan without executing transfers",
    )
    sync.add_argument(
        "--torrent-mode",
        choices=["auto", "only", "off"],
        default="auto",
        metavar="MODE",
    )
    sync.add_argument("--allow-http-fallback", action="store_true", default=False)
    sync.add_argument("--download-limit", type=int, metavar="RATE", default=0)
    sync.add_argument("--upload-limit", type=int, metavar="RATE", default=0)
    sync.add_argument(
        "--seed-for",
        metavar="DURATION",
        default="0",
        help="Opt-in seeding duration after completion (e.g. 1h, 30m)",
    )
    sync.add_argument("--enable-dht", action="store_true", default=False)
    sync.add_argument("--enable-pex", action="store_true", default=False)
    sync.add_argument(
        "--verify-on-complete",
        action="store_true",
        default=True,
        help="Run piece verification after torrent completes (default: on)",
    )

    # receipts
    receipts = sub.add_parser("receipts", help="Manage transfer receipts")
    receipts_sub = receipts.add_subparsers(dest="receipts_command", metavar="SUBCOMMAND")
    receipts_sub.add_parser("list", help="List all receipts")
    receipts_show = receipts_sub.add_parser("show", help="Show a receipt")
    receipts_show.add_argument("receipt_id", metavar="RECEIPT_ID")
    receipts_export = receipts_sub.add_parser("export", help="Export a receipt bundle")
    receipts_export.add_argument("receipt_id", metavar="RECEIPT_ID")
    receipts_export.add_argument("--output", type=Path, metavar="PATH")

    return parser


def _error(msg: str, fmt: str = "human", code: int = 1) -> None:
    if fmt == "json":
        json.dump({"error": msg}, sys.stderr)
        sys.stderr.write("\n")
    else:
        sys.stderr.write(f"error: {msg}\n")
    sys.exit(code)


def main(argv: list[str] | None = None) -> None:
    parser = _build_parser()
    args = parser.parse_args(argv)
    fmt = getattr(args, "format", "human")

    if args.command is None:
        parser.print_help()
        sys.exit(0)

    if args.command in ("check", "plan", "verify", "sync", "receipts"):
        _error(
            f"Command '{args.command}' is not yet implemented (Phase 0/1 spike).",
            fmt=fmt,
            code=1,
        )
