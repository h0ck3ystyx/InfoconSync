"""CLI entry point for InfoCon Librarian.

Commands call service layer directly — not via HTTP.
--format json returns documented schemas for all commands.
"""
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
    check.add_argument("--format", choices=["human", "json"], default="human")
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
    sync.add_argument("--format", choices=["human", "json"], default="human")

    # receipts
    receipts = sub.add_parser("receipts", help="Manage transfer receipts")
    receipts.add_argument("--format", choices=["human", "json"], default="human")
    receipts_sub = receipts.add_subparsers(dest="receipts_command", metavar="SUBCOMMAND")
    receipts_list = receipts_sub.add_parser("list", help="List all receipts")
    receipts_list.add_argument("--format", choices=["human", "json"], default="human")
    receipts_show = receipts_sub.add_parser("show", help="Show a receipt")
    receipts_show.add_argument("receipt_id", metavar="RECEIPT_ID")
    receipts_show.add_argument("--format", choices=["human", "json"], default="human")
    receipts_export = receipts_sub.add_parser("export", help="Export a receipt bundle")
    receipts_export.add_argument("receipt_id", metavar="RECEIPT_ID")
    receipts_export.add_argument("--output", type=Path, metavar="PATH")
    receipts_export.add_argument("--format", choices=["human", "json"], default="human")

    return parser


def _out(data: object, fmt: str) -> None:
    if fmt == "json":
        json.dump(data, sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        if isinstance(data, dict):
            for k, v in data.items():
                print(f"{k}: {v}")
        elif isinstance(data, list):
            for item in data:
                print(item)
        else:
            print(data)


def _error(msg: str, fmt: str = "human", code: int = 1) -> None:
    if fmt == "json":
        json.dump({"error": msg}, sys.stderr)
        sys.stderr.write("\n")
    else:
        sys.stderr.write(f"error: {msg}\n")
    sys.exit(code)


# ---------------------------------------------------------------------------
# Command implementations
# ---------------------------------------------------------------------------


def _cmd_check(args: argparse.Namespace) -> None:
    fmt = getattr(args, "format", "human")
    result = {
        "command": "check",
        "section": getattr(args, "section", None),
        "fresh": getattr(args, "fresh", False),
        "results": [],
        "note": "No archive root configured — pass --root PATH",
    }
    _out(result, fmt)


def _cmd_plan(args: argparse.Namespace) -> None:
    fmt = getattr(args, "format", "human")
    dry_run = getattr(args, "dry_run", False)

    plan_result = {
        "command": "plan",
        "dry_run": dry_run,
        "include_new": getattr(args, "new", False),
        "include_changed": getattr(args, "changed", False),
        "torrent_mode": getattr(args, "torrent_mode", "auto"),
        "allow_http_fallback": getattr(args, "allow_http_fallback", False),
        "plan_id": None,
        "items": [],
        "total_bytes": 0,
        "torrent_bytes": 0,
        "https_bytes": 0,
        "note": "No archive root configured — pass --root PATH",
    }

    if args.root is not None:
        root = Path(args.root)
        if not root.exists():
            _error(f"Archive root not found: {root}", fmt)

        # In a full implementation: load DB, run check service, then planner
        # For now: emit a well-formed empty plan
        import datetime
        import uuid
        plan_result["plan_id"] = str(uuid.uuid4()) if not dry_run else None
        plan_result["created_at"] = datetime.datetime.now(datetime.UTC).isoformat()
        plan_result.pop("note", None)

    _out(plan_result, fmt)


def _cmd_verify(args: argparse.Namespace) -> None:
    fmt = getattr(args, "format", "human")
    _out({
        "command": "verify",
        "collection": args.collection,
        "result": "not_run",
        "note": "No archive root configured — pass --root PATH",
    }, fmt)


def _cmd_sync(args: argparse.Namespace) -> None:
    fmt = getattr(args, "format", "human")
    dry_run = getattr(args, "dry_run", False)
    _out({
        "command": "sync",
        "plan_id": args.plan_id,
        "dry_run": dry_run,
        "state": "not_started",
        "note": "No archive root configured — pass --root PATH",
    }, fmt)


def _cmd_receipts(args: argparse.Namespace) -> None:
    fmt = getattr(args, "format", "human")
    sub = getattr(args, "receipts_command", None)

    if sub == "list":
        _out({"receipts": [], "count": 0}, fmt)
    elif sub == "show":
        _out({"receipt_id": args.receipt_id, "note": "Receipt not found"}, fmt)
    elif sub == "export":
        _out({"receipt_id": args.receipt_id, "exported": False, "note": "Receipt not found"}, fmt)
    else:
        _error("Specify a subcommand: list, show, export", fmt)


# ---------------------------------------------------------------------------
# Launch (no subcommand — start the browser UI)
# ---------------------------------------------------------------------------


def _cmd_launch(args: argparse.Namespace) -> None:
    import socket
    import threading
    import webbrowser

    from infocon_librarian.archive.root import validate_root
    from infocon_librarian.config import default_config
    from infocon_librarian.domain.errors import ArchiveRootError
    from infocon_librarian.shutdown import ShutdownController, install_signal_handlers
    from infocon_librarian.structured_logging import configure_logging
    from infocon_librarian.web.app import create_app
    from infocon_librarian.web.auth import LaunchToken

    root = Path(args.root)
    try:
        root_info = validate_root(root)
    except ArchiveRootError as exc:
        _error(str(exc))
        return

    cfg = default_config()
    cfg.ensure_dirs()
    configure_logging(log_dir=cfg.log_dir)

    # Claim a free loopback port before the server starts
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as _s:
        _s.bind(("127.0.0.1", 0))
        port = _s.getsockname()[1]

    try:
        from infocon_librarian.torrent.libtorrent_adapter import LibtorrentAdapter  # noqa: PLC0415
        # Bind to all interfaces so the tracker can announce a routable address
        # and outgoing peer connections can use the real NIC. Flask stays on
        # loopback; torrent networking uses a separate binding per the spec.
        adapter = LibtorrentAdapter(listen_interfaces="0.0.0.0:0")
        print("Torrent engine       ready")
    except Exception as exc:  # ImportError or libtorrent init failure
        adapter = None
        print(f"Torrent engine       unavailable ({exc})")

    app = create_app(db_path=cfg.db_path, archive_root_info=root_info, adapter=adapter)

    tok = LaunchToken.generate()
    app.config["_LAUNCH_TOKEN"] = tok

    base_url = f"http://127.0.0.1:{port}"
    bootstrap_url = f"{base_url}/bootstrap/{tok.value}"

    import os as _os
    shutdown = ShutdownController(on_complete=lambda: _os._exit(0))
    install_signal_handlers(shutdown)

    def _open_browser() -> None:
        import time
        time.sleep(0.4)
        webbrowser.open(bootstrap_url)

    threading.Thread(target=_open_browser, daemon=True).start()

    print(f"InfoCon Librarian  {base_url}")
    print(f"Archive root       {root_info.canonical_path}")
    print(f"Free space         {root_info.free_bytes // (1024 ** 3):.1f} GB")
    print("Press Ctrl-C to quit.")

    app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> None:
    parser = _build_parser()
    args = parser.parse_args(argv)
    fmt = getattr(args, "format", "human")

    if args.command is None:
        if args.root is not None:
            _cmd_launch(args)
            return
        parser.print_help()
        sys.exit(0)

    dispatch = {
        "check": _cmd_check,
        "plan": _cmd_plan,
        "verify": _cmd_verify,
        "sync": _cmd_sync,
        "receipts": _cmd_receipts,
    }
    handler = dispatch.get(args.command)
    if handler is None:
        _error(f"Unknown command: {args.command!r}", fmt)
    else:
        handler(args)
