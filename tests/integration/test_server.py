"""LAN authentication, mount failures, worker cancellation and install configuration."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest
from werkzeug.security import generate_password_hash

from infocon_librarian.server import init_archive, load_config, set_password
from infocon_librarian.service_runtime import (
    ArchiveGuard,
    ServiceInterrupted,
    ServiceRuntime,
    activate,
    interruptible_sleep,
    start_worker,
)
from infocon_librarian.transfer.http_downloader import download
from infocon_librarian.web.app import create_app

BASE = "http://192.168.1.50:8080"
PASSWORD = "test password for the mirror"


@pytest.fixture()
def app():
    application = create_app(secret_key="persistent-service-secret-for-tests")
    application.config.update(
        TESTING=True,
        TRUSTED_HOSTS=["192.168.1.50"],
        SERVICE_AUTH={"password_hash": generate_password_hash(PASSWORD)},
    )
    return application


def login(client, password=PASSWORD, origin=BASE):
    response = client.get("/login", base_url=BASE)
    assert response.mimetype == "text/html"
    with client.session_transaction(base_url=BASE) as session:
        csrf = session["login_csrf"]
    return client.post(
        "/login",
        base_url=BASE,
        headers={"Origin": origin},
        data={"csrf_token": csrf, "password": password},
    )


def test_service_login_and_logout(app):
    client = app.test_client()
    assert client.get("/", base_url=BASE).location.endswith("/login")
    assert client.get("/api/health", base_url=BASE).status_code == 401
    assert login(client).status_code == 302
    assert client.get("/", base_url=BASE).status_code == 200
    assert client.get_cookie("session", domain="192.168.1.50").http_only
    with client.session_transaction(base_url=BASE) as session:
        csrf = session["csrf_token"]
    assert client.post("/logout", base_url=BASE).status_code == 403
    response = client.post("/logout", base_url=BASE, headers={"Origin": BASE, "X-Csrf-Token": csrf})
    assert response.status_code == 302
    assert client.get("/api/health", base_url=BASE).status_code == 401


def test_login_rejects_csrf_origin_and_password(app):
    client = app.test_client()
    assert (
        client.post(
            "/login", base_url=BASE, data={"password": PASSWORD}, headers={"Origin": BASE}
        ).status_code
        == 403
    )
    assert login(client, origin="http://evil.example").status_code == 403
    assert login(client, password="wrong").status_code == 401
    assert client.get("/api/health", base_url=BASE).status_code == 401


def test_host_validation_and_bootstrap_disabled(app):
    client = app.test_client()
    assert client.get("/login", base_url="http://evil.example:8080").status_code == 400
    assert client.get("/bootstrap/anything", base_url=BASE).status_code == 404
    # Supplying an allowed-looking Origin never bypasses the trusted Host check.
    assert (
        client.post(
            "/login", base_url="http://evil.example:8080", headers={"Origin": BASE}
        ).status_code
        == 400
    )


def test_rate_limit(app):
    client = app.test_client()
    for _ in range(5):
        assert login(client, password="wrong").status_code == 401
    assert login(client).status_code == 429


def test_session_survives_app_restart_but_not_secret_rotation(app):
    client = app.test_client()
    assert login(client).status_code == 302
    cookie = client.get_cookie("session", domain="192.168.1.50")
    restarted = create_app(secret_key=app.secret_key)
    restarted.config.update(SERVICE_AUTH=app.config["SERVICE_AUTH"], TRUSTED_HOSTS=["192.168.1.50"])
    second = restarted.test_client()
    second.set_cookie("session", cookie.value, domain="192.168.1.50")
    assert second.get("/api/health", base_url=BASE).status_code == 200
    restarted.secret_key = "rotated-secret-for-password-reset"
    assert second.get("/api/health", base_url=BASE).status_code == 401


def test_service_unavailable_blocks_mutations_and_reports_health(app):
    runtime = ServiceRuntime(Mock())
    app.config["SERVICE_RUNTIME"] = runtime
    client = app.test_client()
    assert login(client).status_code == 302
    runtime.interrupt("NAS missing")
    assert client.post("/api/checks", base_url=BASE).status_code == 503
    # A second request also returns: the mutation lock was released on failure.
    assert client.post("/api/checks", base_url=BASE).status_code == 503
    assert client.get("/api/health", base_url=BASE).json["status"] == "degraded"


def test_guard_requires_exact_mount_source_and_marker(tmp_path, monkeypatch):
    root = tmp_path / "archive"
    root.mkdir()
    (root / ".infocon-archive-id").write_text("correct-id\n")
    guard = ArchiveGuard(root, root, "nas:/archive", "correct-id")
    original = Path.read_text
    mountinfo = f"1 0 0:1 / {root} rw - nfs4 nas:/archive rw\n"

    def read(path, *args, **kwargs):
        if str(path) == "/proc/self/mountinfo":
            return mountinfo
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    guard.check()
    (root / ".infocon-archive-id").write_text("wrong-id")
    with pytest.raises(ServiceInterrupted, match="identity"):
        guard.check()
    mountinfo = f"1 0 0:1 / {root} rw - nfs4 other:/archive rw\n"
    with pytest.raises(ServiceInterrupted, match="mount/source"):
        guard.check()
    mountinfo = ""
    with pytest.raises(ServiceInterrupted, match="mount/source"):
        guard.check()


def test_archive_initialization_never_overwrites_marker(tmp_path, monkeypatch):
    monkeypatch.setattr(ArchiveGuard, "check_mount", lambda _: None)
    cfg = {
        "archive_root": str(tmp_path),
        "mount_path": str(tmp_path),
        "mount_source": "nas:/archive",
        "archive_id": "expected",
    }
    init_archive(cfg)
    with pytest.raises(FileExistsError):
        init_archive(cfg)
    assert (tmp_path / ".infocon-archive-id").read_text().strip() == "expected"


def test_runtime_stays_stopped_after_mount_returns():
    guard, adapter = Mock(), Mock()
    runtime = ServiceRuntime(guard, adapter)
    guard.check.side_effect = OSError("NAS offline")
    with pytest.raises(ServiceInterrupted):
        runtime.checkpoint(force=True)
    adapter.close.assert_called_once()
    guard.check.side_effect = None
    with pytest.raises(ServiceInterrupted):
        runtime.checkpoint(force=True)


def test_stop_wakes_workers_and_rejects_new_work():
    runtime = ServiceRuntime(Mock())
    activate(runtime)
    reached = threading.Event()
    finished = threading.Event()

    def worker():
        reached.set()
        try:
            interruptible_sleep(60)
        except ServiceInterrupted:
            finished.set()

    try:
        start_worker(threading.Thread(target=worker))
        assert reached.wait(1)
        runtime.interrupt("shutdown")
        assert runtime.drain(1)
        assert finished.is_set()
        with pytest.raises(ServiceInterrupted):
            start_worker(threading.Thread(target=lambda: None))
    finally:
        activate(None)


def test_cancellation_keeps_partial_download_and_does_not_publish(tmp_path):
    runtime = ServiceRuntime(Mock())
    destination = tmp_path / "movie.bin"

    class Stream(httpx.SyncByteStream):
        def __iter__(self):
            yield b"x" * (4 * 1024 * 1024)
            runtime.interrupt("shutdown")
            yield b"y" * (1024 * 1024)

    def handle(request):
        return httpx.Response(200, headers={"ETag": '"v1"'}, stream=Stream())

    activate(runtime)
    try:
        with (
            httpx.Client(transport=httpx.MockTransport(handle)) as client,
            pytest.raises(ServiceInterrupted),
        ):
            download("https://example.test/movie", destination, http_client=client)
    finally:
        activate(None)
    assert not destination.exists()
    assert destination.with_suffix(".bin.part").stat().st_size == 4 * 1024 * 1024
    assert "bytes_written=4194304" in (tmp_path / "movie.bin.part.meta").read_text()


def test_password_file_permissions_and_rotation(tmp_path, monkeypatch):
    monkeypatch.setattr("getpass.getpass", lambda _: PASSWORD)
    path = tmp_path / "auth.json"
    set_password(path)
    first = json.loads(path.read_text())
    assert path.stat().st_mode & 0o777 == 0o600
    assert PASSWORD not in path.read_text()
    set_password(path)
    assert json.loads(path.read_text())["secret_key"] != first["secret_key"]


def test_example_config_requires_real_archive_id(tmp_path):
    source = Path("deploy/linux/server.toml.example").read_text()
    config = tmp_path / "server.toml"
    config.write_text(source)
    with pytest.raises(ValueError):
        load_config(config)
    config.write_text(
        source.replace("REPLACE-WITH-GENERATED-ID", "34c0ffda-ae65-4445-af01-5905ac4cb420")
    )
    cfg = load_config(config)
    assert cfg["host"] == "0.0.0.0"
    assert cfg["port"] == 8080


def test_torrent_shutdown_saves_resume_and_restart_reuses_it(tmp_path, monkeypatch):
    from infocon_librarian.domain.models import TransferProgress, TransferState
    from infocon_librarian.services import torrent_transfer_runner as runner
    from infocon_librarian.storage.database import open_db
    from infocon_librarian.storage.migrations import migrate
    from infocon_librarian.storage.plan_repository import PlanRepository
    from infocon_librarian.storage.repositories import ArchiveRootRepository
    from tests.support.fake_engine import FakeTorrentEngine

    db_path = tmp_path / "state" / "librarian.db"
    conn = open_db(db_path)
    migrate(conn)
    repo = PlanRepository(conn)
    root = ArchiveRootRepository(conn).upsert(str(tmp_path / "archive"), "fp")
    plan = repo.create_plan(root.id)
    item = repo.add_item(
        plan.id,
        method="torrent",
        status="pending",
        destination_relpath="defcon/test",
        collection_key="defcon/test",
        url="https://example.test/test.torrent",
    )
    repo.update_plan_state(plan.id, "running")
    adapter = FakeTorrentEngine()
    runtime = ServiceRuntime(Mock())
    monkeypatch.setattr(runner, "_fetch_torrent_bytes", lambda _: b"torrent")

    def interrupt_poll(job):
        runtime.interrupt("shutdown")
        return TransferProgress(
            job_id=job,
            state=TransferState.DOWNLOADING,
            total_bytes=1024,
            downloaded_bytes=512,
            uploaded_bytes=0,
            download_rate=0,
            upload_rate=0,
            num_peers=1,
            last_error=None,
        )

    monkeypatch.setattr(adapter, "poll", interrupt_poll)
    activate(runtime)
    try:
        runner.run_torrent_transfer(
            plan.id,
            db_path=db_path,
            archive_root=tmp_path / "archive",
            adapter=adapter,
            broadcast=lambda _: None,
        )
    finally:
        activate(None)
    resume = db_path.parent / "jobs" / f"{item.id}.resume"
    assert resume.read_bytes() == b"fake-resume"
    assert repo.get_item(item.id).status == "downloading"
    assert repo.reset_orphaned_running_plans() == 1
    assert repo.get_item(item.id).status == "pending"

    def complete_poll(job):
        return TransferProgress(
            job_id=job,
            state=TransferState.COMPLETE,
            total_bytes=1024,
            downloaded_bytes=1024,
            uploaded_bytes=0,
            download_rate=0,
            upload_rate=0,
            num_peers=0,
            last_error=None,
        )

    monkeypatch.setattr(adapter, "poll", complete_poll)
    repo.update_plan_state(plan.id, "running")
    runner.run_torrent_transfer(
        plan.id,
        db_path=db_path,
        archive_root=tmp_path / "archive",
        adapter=adapter,
        broadcast=lambda _: None,
    )
    assert adapter.started_params[-1].resume_data == b"fake-resume"
    assert repo.get_item(item.id).status == "complete"
    assert repo.get_plan(plan.id).state == "complete"
    assert not resume.exists()
    conn.close()


def test_state_directory_is_exclusive(tmp_path):
    pytest.importorskip("fcntl")
    from infocon_librarian.server import service_lock

    path = tmp_path / "service.lock"
    with service_lock(path), pytest.raises(ValueError, match="Another service"), service_lock(path):
        pytest.fail("second service must not start")
    with service_lock(path):
        pass


def test_network_state_directory_rejected(monkeypatch):
    from infocon_librarian.service_runtime import require_local_state

    monkeypatch.setattr(
        Path,
        "read_text",
        lambda _: (
            "1 0 0:1 / / rw - ext4 /dev/vda1 rw\n2 1 0:2 / /mnt/state rw - nfs4 nas:/state rw\n"
        ),
    )
    require_local_state(Path("/var/lib/infocon-librarian"))
    with pytest.raises(ValueError, match="local filesystem"):
        require_local_state(Path("/mnt/state/infocon"))


@pytest.mark.parametrize("other_status", ["downloading", "complete"])
@pytest.mark.parametrize("method", ["https", "torrent"])
def test_mixed_plan_waits_for_other_worker_and_receipt_covers_both(
    tmp_path,
    monkeypatch,
    method,
    other_status,
):
    from infocon_librarian.domain.models import TransferProgress, TransferState
    from infocon_librarian.services import http_transfer_runner as http_runner
    from infocon_librarian.services import torrent_transfer_runner as torrent_runner
    from infocon_librarian.storage.database import open_db
    from infocon_librarian.storage.migrations import migrate
    from infocon_librarian.storage.plan_repository import PlanRepository
    from infocon_librarian.storage.repositories import ArchiveRootRepository
    from tests.support.fake_engine import FakeTorrentEngine

    conn = open_db(tmp_path / "state.db")
    migrate(conn)
    repo = PlanRepository(conn)
    root = ArchiveRootRepository(conn).upsert(str(tmp_path / "archive"), "fp")
    plan = repo.create_plan(root.id)
    own = repo.add_item(
        plan.id,
        method=method,
        status="pending",
        destination_relpath="defcon/one",
        collection_key="defcon/one",
        url="https://example.test/one",
    )
    other = repo.add_item(
        plan.id,
        method="torrent" if method == "https" else "https",
        status=other_status,
        destination_relpath="defcon/two",
    )
    repo.update_plan_state(plan.id, "running")
    if method == "https":
        monkeypatch.setattr(http_runner, "_download_item", lambda *a, **k: {"status": "complete"})
        http_runner.run_http_transfer(
            plan.id,
            db_path=tmp_path / "state.db",
            archive_root=tmp_path / "archive",
            broadcast=lambda _: None,
        )
    else:
        adapter = FakeTorrentEngine()
        monkeypatch.setattr(torrent_runner, "_fetch_torrent_bytes", lambda _: b"torrent")
        monkeypatch.setattr(
            adapter,
            "poll",
            lambda job: TransferProgress(
                job_id=job,
                state=TransferState.COMPLETE,
                total_bytes=1,
                downloaded_bytes=1,
                uploaded_bytes=0,
                download_rate=0,
                upload_rate=0,
                num_peers=0,
                last_error=None,
            ),
        )
        torrent_runner.run_torrent_transfer(
            plan.id,
            db_path=tmp_path / "state.db",
            archive_root=tmp_path / "archive",
            adapter=adapter,
            broadcast=lambda _: None,
        )
    assert repo.get_item(own.id).status == "complete"
    if other_status == "downloading":
        assert repo.get_plan(plan.id).state == "running"
        assert not list((tmp_path / "receipts").glob("*.json"))
    else:
        assert repo.get_plan(plan.id).state == "complete"
        receipt = json.loads(next((tmp_path / "receipts").glob("*.json")).read_text())
        assert {it["plan_item_id"] for it in receipt["items"]} == {own.id, other.id}
        assert not repo.finish_if_running(plan.id, "failed")
    conn.close()
