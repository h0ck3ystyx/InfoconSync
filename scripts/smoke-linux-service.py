"""Exercise the installed Linux service using an isolated mounted test archive.

Run only in the disposable smoke container with an anonymous /archive volume;
see docs/linux-vm.md. No upstream requests or live archive are used.
"""

from __future__ import annotations

import contextlib
import json
import os
import pwd
import signal
import socket
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

import httpx
from werkzeug.security import generate_password_hash

from infocon_librarian.storage.database import open_db
from infocon_librarian.storage.migrations import migrate
from infocon_librarian.storage.plan_repository import PlanRepository
from infocon_librarian.storage.repositories import ArchiveRootRepository


def main() -> None:
    root = Path("/archive")
    assert root.is_mount(), "Mount a disposable volume at /archive"
    assert not list(root.iterdir()), "Use an empty disposable volume"
    # Run the installed application as the same unprivileged account as systemd.
    account = pwd.getpwnam("infocon")
    if os.getuid() == 0:
        os.chown(root, account.pw_uid, account.pw_gid)
        os.setgroups([account.pw_gid])
        os.setgid(account.pw_gid)
        os.setuid(account.pw_uid)
    source = next(
        line.split(" - ", 1)[1].split()[1]
        for line in Path("/proc/self/mountinfo").read_text().splitlines()
        if line.split(" - ", 1)[0].split()[4] == str(root)
    )
    marker = root / ".infocon-archive-id"
    archive_id = str(uuid.uuid4())
    marker.write_text(archive_id + "\n")
    address = socket.gethostbyname(socket.gethostname())
    base = f"http://{address}:8080"
    password = "test mirror service password"

    with tempfile.TemporaryDirectory(prefix="infocon-service-") as directory:
        work = Path(directory)
        state = work / "state"
        state.mkdir()
        auth = state / "auth.json"
        auth.write_text(
            json.dumps(
                {
                    "password_hash": generate_password_hash(password),
                    "secret_key": os.urandom(32).hex(),
                }
            )
        )
        auth.chmod(0o600)
        config = work / "server.toml"
        config.write_text(f'''host = "0.0.0.0"
port = 8080
allowed_hosts = ["{address}"]
archive_root = "/archive"
mount_path = "/archive"
mount_source = "{source}"
archive_id = "{archive_id}"
state_dir = "{state}"
auth_file = "{auth}"
torrent_listen = "127.0.0.1:0"
''')
        command = ["infocon-server", "--config", str(config), "serve"]
        process = None
        with (work / "service.log").open("w+") as logs:

            def start():
                child = subprocess.Popen(command, cwd="/tmp", stdout=logs, stderr=logs)
                for _ in range(100):
                    assert child.poll() is None, "Service exited during startup"
                    try:
                        if httpx.get(base + "/login", trust_env=False).status_code == 200:
                            return child
                    except httpx.TransportError:
                        pass
                    time.sleep(0.1)
                child.terminate()
                child.wait(timeout=10)
                raise AssertionError("Service did not become ready")

            def stop(child):
                child.send_signal(signal.SIGTERM)
                assert child.wait(timeout=15) == 0

            try:
                process = start()
                with httpx.Client(base_url=base, trust_env=False) as client:
                    assert client.get("/api/health").status_code == 401
                    assert client.get("/login", headers={"Host": "evil.example"}).status_code == 400
                    page = client.get("/login")
                    import re

                    csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text)[1]
                    assert (
                        client.post(
                            "/login",
                            headers={"Origin": base},
                            data={"password": password, "csrf_token": csrf},
                        ).status_code
                        == 302
                    )
                    assert client.get("/").status_code == 200
                    for asset in ("app.js", "app.css"):
                        assert client.get("/static/" + asset).status_code == 200
                    assert client.get("/api/health").json()["status"] == "ok"
                    csrf = client.cookies["csrftoken"]
                    headers = {"Origin": base, "X-Csrf-Token": csrf}
                    marker.unlink()
                    # Force a guard check through the mutation path.
                    assert client.post("/api/checks", json={}, headers=headers).status_code == 503
                    assert client.get("/api/health").json()["status"] == "degraded"
                    marker.write_text(archive_id + "\n")
                    assert client.post("/api/checks", json={}, headers=headers).status_code == 503
                    stop(process)
                    process = None
                    # Seed an orphaned plan and completed item to verify startup recovery.
                    with contextlib.closing(open_db(state / "librarian.db")) as db:
                        migrate(db)
                        repo = PlanRepository(db)
                        archive = ArchiveRootRepository(db).upsert(str(root), "fixture")
                        plan = repo.create_plan(archive.id)
                        complete = repo.add_item(
                            plan.id,
                            method="https",
                            status="complete",
                            destination_relpath="defcon/done",
                        )
                        incomplete = repo.add_item(
                            plan.id,
                            method="https",
                            status="downloading",
                            destination_relpath="defcon/partial",
                        )
                        repo.update_plan_state(plan.id, "running")
                    process = start()
                    # Signing key and cookies survive restart; health recovers.
                    assert client.get("/api/health").json()["status"] == "ok"
                    recovered = client.get(f"/api/plans/{plan.id}").json()
                    assert recovered["state"] == "preflighted"
                    statuses = {item["item_id"]: item["status"] for item in recovered["items"]}
                    assert statuses[complete.id] == "complete"
                    assert statuses[incomplete.id] == "pending"
                    stop(process)
                    process = None
                print("PASS: installed service, LAN binding, login, assets, NAS identity failure,")
                print("      recovery, persistent sessions and clean SIGTERM shutdown")
            except BaseException:
                logs.flush()
                logs.seek(0)
                print(logs.read())
                raise
            finally:
                if process is not None:
                    process.terminate()
                    try:
                        process.wait(timeout=15)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                marker.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
