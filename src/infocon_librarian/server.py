"""Linux VM configuration, installation helpers and single-process WSGI service."""

from __future__ import annotations

import argparse
import contextlib
import getpass
import json
import logging
import os
import secrets
import signal
import threading
import tomllib
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from werkzeug.security import generate_password_hash

from infocon_librarian.archive.root import validate_root
from infocon_librarian.config import config_at
from infocon_librarian.service_runtime import (
    ArchiveGuard,
    ServiceInterrupted,
    ServiceRuntime,
    activate,
    require_local_state,
)

log = logging.getLogger(__name__)


def load_config(path: Path) -> dict[str, Any]:
    with path.open("rb") as fh:
        cfg = tomllib.load(fh)
    required = ("archive_root", "mount_path", "mount_source", "archive_id", "allowed_hosts")
    for key in required:
        if not cfg.get(key):
            raise ValueError(f"Missing required configuration: {key}")
    for key in ("archive_root", "mount_path", "state_dir", "auth_file"):
        if key in cfg and not Path(cfg[key]).is_absolute():
            raise ValueError(f"{key} must be an absolute path")
    uuid.UUID(cfg["archive_id"])
    hosts = cfg["allowed_hosts"]
    if not isinstance(hosts, list) or not all(
        isinstance(h, str) and h and not h.startswith(".") and not any(c in h for c in "/*:@")
        for h in hosts
    ):
        raise ValueError("allowed_hosts must contain explicit hostnames or IPv4 addresses")
    port = cfg.get("port", 8080)
    if not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("port must be between 1 and 65535")
    return cfg


def guard_from_config(cfg: dict[str, Any]) -> ArchiveGuard:
    return ArchiveGuard(
        Path(cfg["archive_root"]), Path(cfg["mount_path"]), cfg["mount_source"], cfg["archive_id"]
    )


def set_password(path: Path) -> None:
    password = getpass.getpass("New service password (at least 12 characters): ")
    if len(password) < 12 or password != getpass.getpass("Repeat password: "):
        raise ValueError("Passwords must match and contain at least 12 characters")
    path.parent.mkdir(parents=True, exist_ok=True)
    # Rotating the signing key also invalidates sessions after a password reset.
    data = {"password_hash": generate_password_hash(password), "secret_key": secrets.token_hex(32)}
    temporary = path.with_name(path.name + ".new")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh)
            fh.flush()
            os.fsync(fh.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    print("Password saved. Restart the service if it is running.")


def init_archive(cfg: dict[str, Any]) -> None:
    guard = guard_from_config(cfg)
    guard.check_mount()
    validate_root(guard.root)
    marker = guard.root / ".infocon-archive-id"
    with marker.open("x") as fh:
        fh.write(cfg["archive_id"] + "\n")
    guard.check()
    print(f"Initialized archive identity at {marker}")


@contextlib.contextmanager
def service_lock(path: Path) -> Iterator[None]:
    """Prevent a second service from recovering a live process's plans."""
    import fcntl

    with path.open("a") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("Another service already owns this state directory") from exc
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def serve(cfg: dict[str, Any]) -> None:
    state = Path(cfg.get("state_dir", "/var/lib/infocon-librarian"))
    if state.resolve().is_relative_to(Path(cfg["mount_path"]).resolve()):
        raise ValueError("state_dir must be outside the NAS mount")
    require_local_state(state)
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    with service_lock(state / ".service.lock"):
        _serve(cfg)


def _serve(cfg: dict[str, Any]) -> None:
    from waitress import create_server

    from infocon_librarian.storage.database import open_db
    from infocon_librarian.storage.migrations import migrate
    from infocon_librarian.storage.plan_repository import PlanRepository
    from infocon_librarian.storage.settings_repository import SettingsRepository
    from infocon_librarian.structured_logging import configure_logging
    from infocon_librarian.torrent.libtorrent_adapter import LibtorrentAdapter
    from infocon_librarian.web.app import create_app

    state = config_at(Path(cfg.get("state_dir", "/var/lib/infocon-librarian")))
    if state.data_dir.resolve().is_relative_to(Path(cfg["mount_path"]).resolve()):
        raise ValueError("state_dir must be on the VM local disk, outside the NAS mount")
    state.ensure_dirs()
    configure_logging(log_dir=state.log_dir)
    auth = json.loads(
        Path(cfg.get("auth_file", "/var/lib/infocon-librarian/auth.json")).read_text()
    )
    if not auth.get("password_hash") or len(auth.get("secret_key", "")) < 32:
        raise ValueError("Invalid auth file; run set-password first")
    guard = guard_from_config(cfg)
    guard.check()
    root = validate_root(guard.root, app_data_dir=state.data_dir)
    with contextlib.closing(open_db(state.db_path)) as db:
        migrate(db)
        recovered = PlanRepository(db).reset_orphaned_running_plans()
        settings = SettingsRepository(db)
        adapter = LibtorrentAdapter(
            listen_interfaces=cfg.get("torrent_listen", "0.0.0.0:6881"),
            download_limit=settings.get_int("torrent_download_limit") * 1024,
            upload_limit=settings.get_int("torrent_upload_limit") * 1024,
        )
    runtime = ServiceRuntime(guard, adapter)
    app = create_app(
        db_path=state.db_path,
        archive_root_info=root,
        adapter=adapter,
        secret_key=auth["secret_key"],
    )
    app.config.update(
        SERVICE_AUTH=auth,
        TRUSTED_HOSTS=cfg["allowed_hosts"],
        SERVICE_RUNTIME=runtime,
        SESSION_COOKIE_SECURE=cfg.get("secure_cookies", False),
    )
    activate(runtime)
    quit_event = threading.Event()
    previous = {}
    for sig in (signal.SIGINT, signal.SIGTERM):
        previous[sig] = signal.signal(sig, lambda *_: quit_event.set())
    server = None
    try:
        server = create_server(
            app,
            host=cfg.get("host", "0.0.0.0"),
            port=cfg.get("port", 8080),
            threads=16,
            clear_untrusted_proxy_headers=True,
        )
        web = threading.Thread(target=server.run, daemon=True, name="wsgi")
        web.start()
        log.info(
            "Service listening on %s:%s; recovered %d interrupted plans",
            cfg.get("host", "0.0.0.0"),
            cfg.get("port", 8080),
            recovered,
        )

        def monitor() -> None:
            while not quit_event.wait(2):
                try:
                    runtime.checkpoint(force=True)
                except ServiceInterrupted:
                    # Stay available for diagnostics; restart explicitly after
                    # checking the mount. No automatic transfer resumption.
                    return

        threading.Thread(target=monitor, daemon=True, name="nas-monitor").start()
        while not quit_event.wait(0.5):
            if not web.is_alive():
                raise RuntimeError("WSGI server exited unexpectedly")
    finally:
        quit_event.set()
        runtime.interrupt("Service shutting down")
        drained = runtime.drain(65)
        if not drained:
            log.error("Worker shutdown timed out; restart will recover interrupted plans")
        if server is not None:
            server.close()
        adapter.close()
        if server is not None:
            server.task_dispatcher.shutdown(timeout=5)
        if drained:
            activate(None)
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="infocon-server")
    parser.add_argument("--config", type=Path, default=Path("/etc/infocon-librarian/server.toml"))
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("serve")
    sub.add_parser("init-archive")
    sub.add_parser("archive-id", help="Generate an ID to put in server.toml")
    password = sub.add_parser("set-password")
    password.add_argument(
        "--auth-file", type=Path, default=Path("/var/lib/infocon-librarian/auth.json")
    )
    args = parser.parse_args(argv)
    try:
        if args.command == "set-password":
            set_password(args.auth_file)
        elif args.command == "archive-id":
            print(uuid.uuid4())
        elif args.command == "init-archive":
            init_archive(load_config(args.config))
        else:
            serve(load_config(args.config))
    except (OSError, ValueError, ServiceInterrupted) as exc:
        parser.exit(1, f"error: {exc}\n")


if __name__ == "__main__":
    main()
