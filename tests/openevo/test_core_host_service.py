from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import select
import socket
import threading
import time
from types import SimpleNamespace

import pytest

from openevo.backend import launcher
from openevo.backend import service
from openevo.backend.runtime_identity import CoreReleaseIdentity, HostServiceRoot
from openevo.backend.service import (
    CoreServiceError,
    CoreServiceErrorCode,
    ProcessIdentity,
    ensure_core_service,
)


SOURCE_COMMIT = "1" * 40
RELEASE_A = CoreReleaseIdentity(
    digest="a" * 64,
    registry_digest="b" * 64,
    framework_lock_sha256="c" * 64,
    source_commit=SOURCE_COMMIT,
)
RELEASE_B = CoreReleaseIdentity(
    digest="d" * 64,
    registry_digest="e" * 64,
    framework_lock_sha256="f" * 64,
    source_commit=SOURCE_COMMIT,
)


class FakeController:
    boot_id = "11111111-1111-1111-1111-111111111111"

    def __init__(self) -> None:
        self.current: dict[int, ProcessIdentity] = {}
        self.next_start = 100
        self.terminated: list[ProcessIdentity] = []

    def capture(self, pid: int) -> ProcessIdentity:
        identity = ProcessIdentity(pid=pid, boot_id=self.boot_id, start_time_ticks=self.next_start)
        self.next_start += 1
        self.current[pid] = identity
        return identity

    def is_alive(self, identity: ProcessIdentity) -> bool:
        return self.current.get(identity.pid) == identity

    def terminate(self, identity: ProcessIdentity, *, deadline: float) -> None:
        del deadline
        self.terminated.append(identity)
        if self.current.get(identity.pid) == identity:
            del self.current[identity.pid]


class FakeChild:
    next_pid = 4000

    def __init__(self, argv: list[str], **kwargs: object) -> None:
        self.argv = argv
        self.pid = FakeChild.next_pid
        FakeChild.next_pid += 1
        self.returncode: int | None = None
        pass_fds = kwargs["pass_fds"]
        assert isinstance(pass_fds, tuple)
        ready_fd = pass_fds[1]
        generation = argv[argv.index("--generation") + 1]
        release_identity = argv[argv.index("--expected-release-identity") + 1]
        registry_digest = "b" * 64 if release_identity == "a" * 64 else "e" * 64
        os.write(
            ready_fd,
            (
                json.dumps(
                    {
                        "schema_version": 1,
                        "generation": generation,
                        "release_identity": release_identity,
                        "registry_digest": registry_digest,
                    },
                    separators=(",", ":"),
                    sort_keys=True,
                )
                + "\n"
            ).encode("ascii"),
        )

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.returncode = -15

    def kill(self) -> None:
        self.returncode = -9

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        self.returncode = self.returncode if self.returncode is not None else 0
        return self.returncode


@pytest.fixture
def service_fakes(monkeypatch: pytest.MonkeyPatch) -> tuple[FakeController, list[FakeChild]]:
    controller = FakeController()
    children: list[FakeChild] = []

    def spawn(argv: list[str], **kwargs: object) -> FakeChild:
        child = FakeChild(argv, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(service, "load_verified_framework_registry", lambda _path: object())
    monkeypatch.setattr(service, "require_host_global_service_root", lambda path: Path(path))
    monkeypatch.setattr(service, "compute_release_identity", lambda **_kwargs: RELEASE_A)
    monkeypatch.setattr(service.subprocess, "Popen", spawn)
    monkeypatch.setattr(service, "_authenticated_status_proof", lambda **_kwargs: "9" * 64)
    return controller, children


def _root(tmp_path: Path) -> Path:
    value = tmp_path / "core"
    value.mkdir(mode=0o700)
    return value


def test_second_project_and_concurrent_bootstrap_attach_one_daemon(
    tmp_path: Path,
    service_fakes: tuple[FakeController, list[FakeChild]],
) -> None:
    controller, children = service_fakes
    root = _root(tmp_path)
    lock = tmp_path / "framework-lock.json"
    lock.write_text("{}", encoding="ascii")
    results: list[object] = []
    failures: list[BaseException] = []
    barrier = threading.Barrier(2)

    def start_for_project() -> None:
        try:
            barrier.wait()
            results.append(
                ensure_core_service(
                    service_root=root,
                    framework_lock=lock,
                    source_commit=SOURCE_COMMIT,
                    port=0,
                    process_controller=controller,
                )
            )
        except BaseException as exc:
            failures.append(exc)

    threads = [threading.Thread(target=start_for_project) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert failures == []
    assert len(results) == 2
    assert len(children) == 1
    assert {result.generation for result in results} == {results[0].generation}
    assert {result.port for result in results} == {results[0].port}
    assert sorted(result.attached for result in results) == [False, True]


def test_port_conflict_is_typed_and_readiness_is_not_published(
    tmp_path: Path,
    service_fakes: tuple[FakeController, list[FakeChild]],
) -> None:
    controller, _children = service_fakes
    root = _root(tmp_path)
    lock = tmp_path / "framework-lock.json"
    lock.write_text("{}", encoding="ascii")
    occupied = socket.socket()
    occupied.bind(("127.0.0.1", 0))
    port = occupied.getsockname()[1]
    try:
        with pytest.raises(CoreServiceError) as exc_info:
            ensure_core_service(
                service_root=root,
                framework_lock=lock,
                source_commit=SOURCE_COMMIT,
                port=port,
                process_controller=controller,
            )
    finally:
        occupied.close()
    assert exc_info.value.code is CoreServiceErrorCode.PORT_UNAVAILABLE
    assert not (root / "ready.json").exists()


def test_status_proof_requires_authenticated_verified_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    version = {
        "provider_kind": "openevo_core",
        "build_channel": "release",
        "source_commit": SOURCE_COMMIT,
        "openapi_sha256": "4" * 64,
        "build_version": "0.1.0",
    }
    status = {"registry_status": "verified", "registry_digest": RELEASE_A.registry_digest}

    def fetch(_port: int, path: str, **_kwargs: object) -> object:
        return version if path == "/version" else status

    monkeypatch.setattr(service, "_fetch_json", fetch)
    proof = service._authenticated_status_proof(
        port=8765,
        bearer="B" * 64,
        release=RELEASE_A,
        deadline=time.monotonic() + 1,
    )
    assert len(proof) == 64
    assert "B" * 64 not in proof

    status["registry_digest"] = "0" * 64
    with pytest.raises(CoreServiceError) as exc_info:
        service._authenticated_status_proof(
            port=8765,
            bearer="B" * 64,
            release=RELEASE_A,
            deadline=time.monotonic() + 1,
        )
    assert exc_info.value.code is CoreServiceErrorCode.STATUS_INVALID


@pytest.mark.parametrize(
    ("status", "payload"),
    [
        (302, b"{}"),
        (200, b'{"ready":true,"ready":false}'),
    ],
)
def test_status_probe_rejects_redirect_and_duplicate_json(
    status: int,
    payload: bytes,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Headers:
        def get_content_type(self) -> str:
            return "application/json"

        def get(self, name: str) -> str | None:
            return str(len(payload)) if name == "Content-Length" else None

    class Response:
        headers = Headers()

        def __init__(self) -> None:
            self.status = status

        def read(self, _limit: int) -> bytes:
            return payload

    class Connection:
        def __init__(self, *args: object, **kwargs: object) -> None:
            del args, kwargs

        def request(self, *args: object, **kwargs: object) -> None:
            del args, kwargs

        def getresponse(self) -> Response:
            return Response()

        def close(self) -> None:
            pass

    monkeypatch.setattr(service.http.client, "HTTPConnection", Connection)
    with pytest.raises(CoreServiceError) as exc_info:
        service._fetch_json(
            8765,
            "/v1/status",
            bearer="S" * 64,
            deadline=time.monotonic() + 1,
        )
    assert exc_info.value.code is CoreServiceErrorCode.STATUS_INVALID


def test_identity_mismatch_requires_controlled_replacement(
    tmp_path: Path,
    service_fakes: tuple[FakeController, list[FakeChild]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller, children = service_fakes
    root = _root(tmp_path)
    lock = tmp_path / "framework-lock.json"
    lock.write_text("{}", encoding="ascii")
    first = ensure_core_service(
        service_root=root,
        framework_lock=lock,
        source_commit=SOURCE_COMMIT,
        port=0,
        process_controller=controller,
    )
    first_bearer = first.bearer_token
    monkeypatch.setattr(service, "compute_release_identity", lambda **_kwargs: RELEASE_B)

    with pytest.raises(CoreServiceError) as exc_info:
        ensure_core_service(
            service_root=root,
            framework_lock=lock,
            source_commit=SOURCE_COMMIT,
            port=first.port,
            process_controller=controller,
        )
    assert exc_info.value.code is CoreServiceErrorCode.IDENTITY_MISMATCH
    assert controller.terminated == []

    replacement = ensure_core_service(
        service_root=root,
        framework_lock=lock,
        source_commit=SOURCE_COMMIT,
        port=first.port,
        replace_mismatched=True,
        process_controller=controller,
    )
    assert replacement.release_identity == RELEASE_B.digest
    assert replacement.bearer_token != first_bearer
    assert len(controller.terminated) == 1
    assert len(children) == 2


def test_pending_process_is_recovered_before_restart(
    tmp_path: Path,
    service_fakes: tuple[FakeController, list[FakeChild]],
) -> None:
    controller, children = service_fakes
    root = _root(tmp_path)
    lock = tmp_path / "framework-lock.json"
    lock.write_text("{}", encoding="ascii")
    pending_identity = ProcessIdentity(
        pid=777,
        boot_id=controller.boot_id,
        start_time_ticks=55,
    )
    controller.current[pending_identity.pid] = pending_identity
    with HostServiceRoot(root) as pinned:
        pinned.atomic_write_json(
            "pending.json",
            {
                "schema_version": 1,
                "release_identity": RELEASE_A.digest,
                "pid": pending_identity.pid,
                "boot_id": pending_identity.boot_id,
                "start_time_ticks": pending_identity.start_time_ticks,
                "port": 8765,
                "generation": "8" * 32,
            },
            replace=False,
        )

    attachment = ensure_core_service(
        service_root=root,
        framework_lock=lock,
        source_commit=SOURCE_COMMIT,
        port=0,
        process_controller=controller,
    )

    assert attachment.attached is False
    assert controller.terminated == [pending_identity]
    assert len(children) == 1
    assert not (root / "pending.json").exists()


def test_dead_process_restarts_without_signalling_reused_pid(
    tmp_path: Path,
    service_fakes: tuple[FakeController, list[FakeChild]],
) -> None:
    controller, children = service_fakes
    root = _root(tmp_path)
    lock = tmp_path / "framework-lock.json"
    lock.write_text("{}", encoding="ascii")
    first = ensure_core_service(
        service_root=root,
        framework_lock=lock,
        source_commit=SOURCE_COMMIT,
        port=0,
        process_controller=controller,
    )
    with HostServiceRoot(root) as pinned:
        ledger = pinned.read_json("service.json")
    old = ProcessIdentity(
        pid=ledger["pid"],
        boot_id=ledger["boot_id"],
        start_time_ticks=ledger["start_time_ticks"],
    )
    controller.current[old.pid] = ProcessIdentity(
        pid=old.pid,
        boot_id=old.boot_id,
        start_time_ticks=old.start_time_ticks + 1,
    )

    restarted = ensure_core_service(
        service_root=root,
        framework_lock=lock,
        source_commit=SOURCE_COMMIT,
        port=first.port,
        process_controller=controller,
    )
    assert restarted.generation != first.generation
    assert controller.terminated == []
    assert len(children) == 2


@pytest.mark.asyncio
async def test_launcher_does_not_signal_ready_before_server_started(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bind_gate = asyncio.Event()
    exit_gate = asyncio.Event()
    instances: list[object] = []

    class FakeServer:
        def __init__(self, _config: object) -> None:
            self.started = False
            self.should_exit = False
            instances.append(self)

        async def serve(self, *, sockets: list[socket.socket]) -> None:
            assert sockets
            await bind_gate.wait()
            self.started = True
            await exit_gate.wait()

    monkeypatch.setattr("uvicorn.Config", lambda *args, **kwargs: SimpleNamespace())
    monkeypatch.setattr("uvicorn.Server", FakeServer)
    read_fd, write_fd = os.pipe2(os.O_NONBLOCK | os.O_CLOEXEC)
    inherited = socket.socket()
    task = asyncio.create_task(
        launcher._run_supervised_server(
            object(),
            inherited_socket=inherited,
            ready_fd=write_fd,
            ready_payload={"schema_version": 1},
        )
    )
    await asyncio.sleep(0.03)
    assert select.select([read_fd], [], [], 0)[0] == []
    bind_gate.set()
    deadline = time.monotonic() + 1
    while not select.select([read_fd], [], [], 0)[0]:
        assert time.monotonic() < deadline
        await asyncio.sleep(0.01)
    assert os.read(read_fd, 4096) == b'{"schema_version":1}\n'
    exit_gate.set()
    assert await task == 0
    os.close(read_fd)
    inherited.close()
