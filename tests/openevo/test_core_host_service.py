from __future__ import annotations

import asyncio
import errno
import hashlib
import json
import os
from pathlib import Path
import select
import signal
import socket
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from openevo import __version__
from openevo.backend import launcher
from openevo.backend import runtime_identity
from openevo.backend import service
from openevo.backend.contracts.v2.provider import RELEASE_DAEMON_FEATURE_FLAGS_V2
from openevo.backend.contracts.v2.snapshots import (
    events_schema_sha256,
    openapi_sha256,
)
from openevo.backend.runtime_identity import (
    CoreReleaseIdentity,
    HostServiceRoot,
    release_runtime_contract_sha256,
)
from openevo.backend.service import (
    CoreDaemonBundleIdentity,
    CoreServiceError,
    CoreServiceErrorCode,
    CoreServicePredecessor,
    ProcessIdentity,
    ensure_core_service,
    stop_core_service,
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
PUBLISHED_V019_RELEASE = CoreReleaseIdentity(
    digest="a7e838f5041c5fbd9414f156034791463fd1590caa183066281cf9d07d276298",
    registry_digest="0c8d466db17fd0dc312a647c34e35bed04eba4e615799effebec761533c30874",
    framework_lock_sha256="c603f9951bf3234d3ee2b1e648650d162f289c9f724b8417452c0119f0ab2406",
    source_commit="54650e477a76dd07b0a511ad5450c3b8ea615556",
)
PUBLISHED_V019_DAEMON = CoreDaemonBundleIdentity(
    bundle_sha256="58787c1ff65b3659b2386659843820dc2cca752d99f44e4869cb4065606c0294",
    canonical_manifest_sha256=("ec9a11829eadd298adcbf2c7d467b426a38ad62d5ce283d7068a0b78dfdc4287"),
    lifecycle_compatibility=16,
)
DAEMON_A = CoreDaemonBundleIdentity(
    bundle_sha256="2" * 64,
    canonical_manifest_sha256="3" * 64,
    lifecycle_compatibility=3,
)
DAEMON_B = CoreDaemonBundleIdentity(
    bundle_sha256="4" * 64,
    canonical_manifest_sha256="5" * 64,
    lifecycle_compatibility=3,
)
DAEMON_V2 = CoreDaemonBundleIdentity(
    bundle_sha256="6" * 64,
    canonical_manifest_sha256="7" * 64,
    lifecycle_compatibility=2,
)
DAEMON_V4 = CoreDaemonBundleIdentity(
    bundle_sha256="8" * 64,
    canonical_manifest_sha256="9" * 64,
    lifecycle_compatibility=4,
)
DAEMON_V5 = CoreDaemonBundleIdentity(
    bundle_sha256="a" * 64,
    canonical_manifest_sha256="0" * 64,
    lifecycle_compatibility=5,
)
DAEMON_V6 = CoreDaemonBundleIdentity(
    bundle_sha256="b" * 64,
    canonical_manifest_sha256="1" * 64,
    lifecycle_compatibility=6,
)
DAEMON_V7 = CoreDaemonBundleIdentity(
    bundle_sha256="c" * 64,
    canonical_manifest_sha256="2" * 64,
    lifecycle_compatibility=7,
)
DAEMON_V8 = CoreDaemonBundleIdentity(
    bundle_sha256="d" * 64,
    canonical_manifest_sha256="3" * 64,
    lifecycle_compatibility=8,
)
DAEMON_V9 = CoreDaemonBundleIdentity(
    bundle_sha256="e" * 64,
    canonical_manifest_sha256="4" * 64,
    lifecycle_compatibility=9,
)
DAEMON_V10 = CoreDaemonBundleIdentity(
    bundle_sha256="f" * 64,
    canonical_manifest_sha256="5" * 64,
    lifecycle_compatibility=10,
)
DAEMON_V11 = CoreDaemonBundleIdentity(
    bundle_sha256="0" * 64,
    canonical_manifest_sha256="6" * 64,
    lifecycle_compatibility=11,
)
DAEMON_V12 = CoreDaemonBundleIdentity(
    bundle_sha256="9" * 64,
    canonical_manifest_sha256="7" * 64,
    lifecycle_compatibility=12,
)
DAEMON_V13 = CoreDaemonBundleIdentity(
    bundle_sha256="7" * 64,
    canonical_manifest_sha256="8" * 64,
    lifecycle_compatibility=13,
)
DAEMON_V14 = CoreDaemonBundleIdentity(
    bundle_sha256="8" * 64,
    canonical_manifest_sha256="9" * 64,
    lifecycle_compatibility=14,
)
DAEMON_V15 = CoreDaemonBundleIdentity(
    bundle_sha256="6" * 64,
    canonical_manifest_sha256="a" * 64,
    lifecycle_compatibility=15,
)
DAEMON_V16 = CoreDaemonBundleIdentity(
    bundle_sha256="5" * 64,
    canonical_manifest_sha256="b" * 64,
    lifecycle_compatibility=16,
)

_REAL_AUTHENTICATED_STATUS_PROOF = service._authenticated_status_proof


class FakeController:
    boot_id = "11111111-1111-1111-1111-111111111111"

    def __init__(self) -> None:
        self.current: dict[int, ProcessIdentity] = {}
        self.next_start = 100
        self.terminated: list[ProcessIdentity] = []
        self.lock_holders: tuple[ProcessIdentity, ...] = ()
        self.service_children: set[tuple[ProcessIdentity, ProcessIdentity]] = set()
        self.natural_exit_on_wait: set[ProcessIdentity] = set()
        self.waited_for_exit: list[ProcessIdentity] = []

    def capture(self, pid: int) -> ProcessIdentity:
        existing = self.current.get(pid)
        if existing is not None:
            return existing
        identity = ProcessIdentity(pid=pid, boot_id=self.boot_id, start_time_ticks=self.next_start)
        self.next_start += 1
        self.current[pid] = identity
        return identity

    def is_alive(self, identity: ProcessIdentity) -> bool:
        return self.current.get(identity.pid) == identity

    def owns_service_process(
        self,
        launcher: ProcessIdentity,
        claimed: ProcessIdentity,
    ) -> bool:
        return (
            self.is_alive(launcher)
            and self.is_alive(claimed)
            and (launcher == claimed or (launcher, claimed) in self.service_children)
        )

    def wait_for_exit(self, identity: ProcessIdentity, *, deadline: float) -> bool:
        del deadline
        self.waited_for_exit.append(identity)
        if identity in self.natural_exit_on_wait:
            self.current.pop(identity.pid, None)
        return not self.is_alive(identity)

    def terminate(self, identity: ProcessIdentity, *, deadline: float) -> None:
        del deadline
        self.terminated.append(identity)
        if self.current.get(identity.pid) == identity:
            del self.current[identity.pid]

    def find_lock_holders(self, _lock: object) -> tuple[ProcessIdentity, ...]:
        return self.lock_holders


class FakeChild:
    next_pid = 4000

    def __init__(self, argv: list[str], **kwargs: object) -> None:
        self.argv = argv
        self.environment = kwargs.get("env")
        self.pid = FakeChild.next_pid
        FakeChild.next_pid += 1
        self.returncode: int | None = None
        pass_fds = kwargs["pass_fds"]
        assert isinstance(pass_fds, tuple)
        ready_fd = pass_fds[1]
        generation = argv[argv.index("--generation") + 1]
        release_identity = argv[argv.index("--expected-release-identity") + 1]
        source_commit = argv[argv.index("--source-commit") + 1]
        registry_digest = "b" * 64 if release_identity == "a" * 64 else "e" * 64
        os.write(
            ready_fd,
            (
                json.dumps(
                    {
                        "schema_version": 2,
                        "generation": generation,
                        "release_identity": release_identity,
                        "api_major": 2,
                        "openapi_sha256": openapi_sha256(),
                        "event_schema_sha256": events_schema_sha256(),
                        "release_version": __version__,
                        "build_id": "6" * 64,
                        "source_commit": source_commit,
                        "provider_kind": "openevo_daemon",
                        "feature_set_sha256": (service._production_v2_feature_set_sha256()),
                        "registry_digest": registry_digest,
                        "runtime_contract_sha256": release_runtime_contract_sha256(),
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
        identity = controller.capture(child.pid)
        root_path = Path(argv[argv.index("--service-root") + 1])
        with HostServiceRoot(root_path) as root:
            pending = root.read_json("pending.json")
            root.atomic_write_json(
                "pending.json",
                {
                    "schema_version": 3,
                    "phase": "spawn_claimed",
                    "release_identity": pending["release_identity"],
                    "pid": identity.pid,
                    "boot_id": identity.boot_id,
                    "start_time_ticks": identity.start_time_ticks,
                    "port": pending["port"],
                    "generation": pending["generation"],
                    "spawn_lock_device": pending["spawn_lock_device"],
                    "spawn_lock_inode": pending["spawn_lock_inode"],
                },
                replace=True,
            )
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
    assert children[0].environment is None
    assert {result.generation for result in results} == {results[0].generation}
    assert {result.port for result in results} == {results[0].port}
    assert sorted(result.attached for result in results) == [False, True]


def test_frozen_onefile_daemon_child_gets_independent_extraction_environment(
    tmp_path: Path,
    service_fakes: tuple[FakeController, list[FakeChild]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller, children = service_fakes
    root = _root(tmp_path)
    lock = tmp_path / "framework-lock.json"
    lock.write_text("{}", encoding="ascii")
    monkeypatch.setattr(service.sys, "frozen", True, raising=False)
    monkeypatch.setattr(service.sys, "_MEIPASS", str(tmp_path / "_MEI-parent"), raising=False)
    monkeypatch.setenv("PYINSTALLER_RESET_ENVIRONMENT", "0")
    monkeypatch.setenv("OPENEVO_TEST_PARENT_ENV", "preserved")
    claimed: list[ProcessIdentity] = []

    def spawn_onefile(argv: list[str], **kwargs: object) -> FakeChild:
        child = FakeChild(argv, **kwargs)
        launcher = controller.capture(child.pid)
        application = controller.capture(child.pid + 10_000)
        controller.service_children.add((launcher, application))
        controller.natural_exit_on_wait.add(launcher)
        root_path = Path(argv[argv.index("--service-root") + 1])
        with HostServiceRoot(root_path) as service_root:
            pending = service_root.read_json("pending.json")
            service_root.atomic_write_json(
                "pending.json",
                {
                    "schema_version": 3,
                    "phase": "spawn_claimed",
                    "release_identity": pending["release_identity"],
                    "pid": application.pid,
                    "boot_id": application.boot_id,
                    "start_time_ticks": application.start_time_ticks,
                    "port": pending["port"],
                    "generation": pending["generation"],
                    "spawn_lock_device": pending["spawn_lock_device"],
                    "spawn_lock_inode": pending["spawn_lock_inode"],
                },
                replace=True,
            )
        claimed.append(application)
        children.append(child)
        return child

    monkeypatch.setattr(service.subprocess, "Popen", spawn_onefile)

    ensure_core_service(
        service_root=root,
        framework_lock=lock,
        source_commit=SOURCE_COMMIT,
        port=0,
        expected_predecessor=CoreServicePredecessor.absent(),
        daemon_bundle_identity=DAEMON_A,
        process_controller=controller,
    )

    assert len(children) == 1
    environment = children[0].environment
    assert isinstance(environment, dict)
    assert environment["PYINSTALLER_RESET_ENVIRONMENT"] == "1"
    assert environment["OPENEVO_TEST_PARENT_ENV"] == "preserved"
    with HostServiceRoot(root, create=False) as service_root:
        ledger = service_root.read_json("service.json")
    launcher = ProcessIdentity(
        pid=ledger["pid"],
        boot_id=ledger["boot_id"],
        start_time_ticks=ledger["start_time_ticks"],
    )
    assert ledger["pid"] == children[0].pid
    assert ledger["application_pid"] == claimed[0].pid

    stop_core_service(service_root=root, process_controller=controller)

    assert controller.terminated == [claimed[0]]
    assert controller.waited_for_exit == [launcher]


def test_bounded_frozen_smoke_reuses_parent_extraction_until_controlled_stop(
    tmp_path: Path,
    service_fakes: tuple[FakeController, list[FakeChild]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller, children = service_fakes
    root = _root(tmp_path)
    lock = tmp_path / "framework-lock.json"
    lock.write_text("{}", encoding="ascii")
    monkeypatch.setattr(service.sys, "frozen", True, raising=False)
    monkeypatch.setattr(service.sys, "_MEIPASS", str(tmp_path / "_MEI-parent"), raising=False)
    monkeypatch.setenv("PYINSTALLER_RESET_ENVIRONMENT", "1")
    monkeypatch.setenv("OPENEVO_TEST_PARENT_ENV", "preserved")

    attachment = ensure_core_service(
        service_root=root,
        framework_lock=lock,
        source_commit=SOURCE_COMMIT,
        process_controller=controller,
        _reuse_frozen_extraction_for_bounded_smoke=True,
    )
    environment = children[0].environment

    assert isinstance(environment, dict)
    assert "PYINSTALLER_RESET_ENVIRONMENT" not in environment
    assert environment["OPENEVO_TEST_PARENT_ENV"] == "preserved"
    with HostServiceRoot(root, create=False) as service_root:
        ledger = service_root.read_json("service.json")
    assert ledger["schema_version"] == 2
    assert ledger["pid"] == children[0].pid

    assert service.stop_core_service_if_generation(
        service_root=root,
        expected_generation=attachment.generation,
        expected_release_identity=attachment.release_identity,
        process_controller=controller,
    )
    assert not (root / "service.json").exists()


def test_frozen_onefile_startup_failure_terminates_launcher_and_application(
    tmp_path: Path,
    service_fakes: tuple[FakeController, list[FakeChild]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller, children = service_fakes
    root = _root(tmp_path)
    lock = tmp_path / "framework-lock.json"
    lock.write_text("{}", encoding="ascii")
    monkeypatch.setattr(service.sys, "frozen", True, raising=False)
    monkeypatch.setattr(service.sys, "_MEIPASS", str(tmp_path / "_MEI-parent"), raising=False)
    identities: list[tuple[ProcessIdentity, ProcessIdentity]] = []

    def spawn_onefile(argv: list[str], **kwargs: object) -> FakeChild:
        child = FakeChild(argv, **kwargs)
        launcher = controller.capture(child.pid)
        application = controller.capture(child.pid + 10_000)
        controller.service_children.add((launcher, application))
        controller.lock_holders = (launcher,)
        root_path = Path(argv[argv.index("--service-root") + 1])
        with HostServiceRoot(root_path) as service_root:
            pending = service_root.read_json("pending.json")
            service_root.atomic_write_json(
                "pending.json",
                {
                    "schema_version": 3,
                    "phase": "spawn_claimed",
                    "release_identity": pending["release_identity"],
                    "pid": application.pid,
                    "boot_id": application.boot_id,
                    "start_time_ticks": application.start_time_ticks,
                    "port": pending["port"],
                    "generation": pending["generation"],
                    "spawn_lock_device": pending["spawn_lock_device"],
                    "spawn_lock_inode": pending["spawn_lock_inode"],
                },
                replace=True,
            )
        identities.append((launcher, application))
        children.append(child)
        return child

    monkeypatch.setattr(service.subprocess, "Popen", spawn_onefile)

    def fail_status(**_kwargs: object) -> str:
        raise CoreServiceError(
            CoreServiceErrorCode.START_FAILED,
            "injected status failure",
            retryable=True,
        )

    monkeypatch.setattr(service, "_authenticated_status_proof", fail_status)

    with pytest.raises(CoreServiceError) as exc_info:
        ensure_core_service(
            service_root=root,
            framework_lock=lock,
            source_commit=SOURCE_COMMIT,
            expected_predecessor=CoreServicePredecessor.absent(),
            daemon_bundle_identity=DAEMON_A,
            process_controller=controller,
        )

    launcher, application = identities[0]
    assert exc_info.value.code is CoreServiceErrorCode.START_FAILED
    assert controller.terminated == [application, launcher]
    assert children[0].returncode == -15
    assert not (root / "pending.json").exists()
    assert not (root / "service.json").exists()


def test_partial_onefile_process_group_is_cleaned_before_restart(
    tmp_path: Path,
    service_fakes: tuple[FakeController, list[FakeChild]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller, children = service_fakes
    root = _root(tmp_path)
    lock = tmp_path / "framework-lock.json"
    lock.write_text("{}", encoding="ascii")
    groups: list[tuple[ProcessIdentity, ProcessIdentity]] = []

    def spawn_onefile(argv: list[str], **kwargs: object) -> FakeChild:
        child = FakeChild(argv, **kwargs)
        launcher = controller.capture(child.pid)
        application = controller.capture(child.pid + 10_000)
        controller.service_children.add((launcher, application))
        root_path = Path(argv[argv.index("--service-root") + 1])
        with HostServiceRoot(root_path) as service_root:
            pending = service_root.read_json("pending.json")
            service_root.atomic_write_json(
                "pending.json",
                {
                    "schema_version": 3,
                    "phase": "spawn_claimed",
                    "release_identity": pending["release_identity"],
                    "pid": application.pid,
                    "boot_id": application.boot_id,
                    "start_time_ticks": application.start_time_ticks,
                    "port": pending["port"],
                    "generation": pending["generation"],
                    "spawn_lock_device": pending["spawn_lock_device"],
                    "spawn_lock_inode": pending["spawn_lock_inode"],
                },
                replace=True,
            )
        groups.append((launcher, application))
        children.append(child)
        return child

    monkeypatch.setattr(service.subprocess, "Popen", spawn_onefile)
    first = ensure_core_service(
        service_root=root,
        framework_lock=lock,
        source_commit=SOURCE_COMMIT,
        expected_predecessor=CoreServicePredecessor.absent(),
        daemon_bundle_identity=DAEMON_A,
        process_controller=controller,
    )
    first_launcher, first_application = groups[0]
    del controller.current[first_application.pid]

    restarted = ensure_core_service(
        service_root=root,
        framework_lock=lock,
        source_commit=SOURCE_COMMIT,
        port=first.port,
        expected_predecessor=CoreServicePredecessor.absent(),
        daemon_bundle_identity=DAEMON_A,
        process_controller=controller,
    )

    assert restarted.generation != first.generation
    assert controller.terminated == [first_launcher]
    assert len(groups) == 2


@pytest.mark.skipif(sys.platform != "linux", reason="Linux process topology is required")
def test_linux_controller_accepts_direct_session_application_child() -> None:
    program = (
        "import os,time;"
        "child=os.fork();"
        "print(child,flush=True) if child else time.sleep(60);"
        "os.waitpid(child,0) if child else None"
    )
    launcher = subprocess.Popen(
        [sys.executable, "-c", program],
        stdout=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        assert launcher.stdout is not None
        claimed_pid = int(launcher.stdout.readline())
        controller = service.LinuxProcessController()
        launcher_identity = controller.capture(launcher.pid)
        claimed_identity = controller.capture(claimed_pid)

        assert controller.owns_service_process(launcher_identity, claimed_identity)
        assert not controller.owns_service_process(claimed_identity, launcher_identity)
    finally:
        try:
            os.killpg(launcher.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        launcher.wait(timeout=5)


def test_bootstrap_lock_serializes_verification_install_and_lifecycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _root(tmp_path)
    wheel = tmp_path / "openevo.whl"
    framework_lock = tmp_path / "framework-lock.json"
    wheel.write_bytes(b"wheel")
    framework_lock.write_text("{}", encoding="ascii")
    active = 0
    max_active = 0
    calls: list[tuple[str, str]] = []
    thread_invocations: dict[int, int] = {}
    guard = threading.Lock()

    def verify_generation(**kwargs: object) -> None:
        assert kwargs["install_generation"] == "a" * 32

    def run_private(
        argv: list[str],
        *,
        deadline: float,
        pass_fds: tuple[int, ...] = (),
    ) -> int:
        nonlocal active, max_active
        del deadline
        if "--bootstrap-lock-fd" in argv:
            assert pass_fds == (int(argv[argv.index("--bootstrap-lock-fd") + 1]),)
        else:
            assert pass_fds == ()
        attachment = next(
            (item for item in argv if item.startswith("bootstrap-")),
            "verification",
        )
        with guard:
            active += 1
            max_active = max(max_active, active)
            calls.append((attachment, "start"))
        time.sleep(0.02)
        with guard:
            calls.append((attachment, "end"))
            active -= 1
            thread_id = threading.get_ident()
            invocation = thread_invocations.get(thread_id, 0) + 1
            thread_invocations[thread_id] = invocation
        return 0

    monkeypatch.setattr(service, "require_host_global_service_root", lambda path: Path(path))
    monkeypatch.setattr(service, "_verify_generation_install", verify_generation)
    monkeypatch.setattr(service, "_run_private_command", run_private)
    barrier = threading.Barrier(2)
    failures: list[BaseException] = []

    def bootstrap(suffix: str) -> None:
        try:
            barrier.wait()
            service.bootstrap_core_service(
                service_root=root,
                wheel_path=wheel,
                framework_lock=framework_lock,
                source_commit=SOURCE_COMMIT,
                attachment_name=f"bootstrap-{suffix * 32}.json",
                install_generation="a" * 32,
            )
        except BaseException as exc:
            failures.append(exc)

    threads = [threading.Thread(target=bootstrap, args=(suffix,)) for suffix in ("1", "2")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert failures == []
    assert max_active == 1
    assert len(calls) == 4


def test_real_process_bootstrap_lock_serializes_mutation_window(tmp_path: Path) -> None:
    root = _root(tmp_path)
    wheel = tmp_path / "openevo.whl"
    framework_lock = tmp_path / "framework-lock.json"
    events = tmp_path / "events.log"
    wheel.write_bytes(b"wheel")
    framework_lock.write_text("{}", encoding="ascii")
    script = """
import os
from pathlib import Path
import sys
import time

from openevo.backend import service

root, wheel, framework_lock, events, suffix = sys.argv[1:]
service.require_host_global_service_root = lambda path: Path(path)

def run_private(argv, *, deadline, pass_fds=()):
    del argv, deadline, pass_fds
    with open(events, "a", encoding="ascii") as stream:
        stream.write(f"{os.getpid()} start\\n")
        stream.flush()
    time.sleep(0.05)
    with open(events, "a", encoding="ascii") as stream:
        stream.write(f"{os.getpid()} end\\n")
        stream.flush()
    return 0

service._run_private_command = run_private
service._verify_generation_install = lambda **kwargs: None
service.bootstrap_core_service(
    service_root=root,
    wheel_path=wheel,
    framework_lock=framework_lock,
    source_commit="1" * 40,
    attachment_name=f"bootstrap-{suffix * 32}.json",
    install_generation="a" * 32,
)
"""
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).parents[2] / "src")}
    processes = [
        subprocess.Popen(
            [
                sys.executable,
                "-c",
                script,
                str(root),
                str(wheel),
                str(framework_lock),
                str(events),
                suffix,
            ],
            env=env,
        )
        for suffix in ("1", "2")
    ]
    assert [process.wait(timeout=10) for process in processes] == [0, 0]
    process_order = [line.split()[0] for line in events.read_text(encoding="ascii").splitlines()]
    assert len(process_order) == 4
    assert process_order in (
        [str(processes[0].pid)] * 2 + [str(processes[1].pid)] * 2,
        [str(processes[1].pid)] * 2 + [str(processes[0].pid)] * 2,
    )


def test_generation_verification_failure_never_enters_daemon_lifecycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _root(tmp_path)
    wheel = tmp_path / "openevo.whl"
    framework_lock = tmp_path / "framework-lock.json"
    wheel.write_bytes(b"wheel-b")
    framework_lock.write_text("{}", encoding="ascii")
    lifecycle_commands: list[list[str]] = []

    monkeypatch.setattr(service, "require_host_global_service_root", lambda path: Path(path))

    def reject_generation(**_kwargs: object) -> None:
        raise CoreServiceError(
            CoreServiceErrorCode.VERIFICATION_FAILED,
            "generation mismatch",
            retryable=False,
        )

    monkeypatch.setattr(service, "_verify_generation_install", reject_generation)
    monkeypatch.setattr(
        service,
        "_run_private_command",
        lambda argv, **_kwargs: lifecycle_commands.append(argv) or 0,
    )

    with pytest.raises(CoreServiceError) as exc_info:
        service.bootstrap_core_service(
            service_root=root,
            wheel_path=wheel,
            framework_lock=framework_lock,
            source_commit=SOURCE_COMMIT,
            attachment_name=f"bootstrap-{'b' * 32}.json",
            install_generation="a" * 32,
            replace_mismatched=True,
        )

    assert exc_info.value.code is CoreServiceErrorCode.VERIFICATION_FAILED
    assert lifecycle_commands == []


def test_generation_verification_binds_exact_prefix_interpreter_and_wheel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _root(tmp_path)
    generation = "a" * 32
    generation_root = root / "releases" / generation
    generation_root.mkdir(parents=True, mode=0o700)
    interpreter = generation_root / "bin" / "python"
    interpreter.parent.mkdir()
    interpreter.write_bytes(b"python")
    interpreter.chmod(0o755)
    module_path = (
        generation_root / "lib" / "python" / "site-packages" / "openevo" / "backend" / "service.py"
    )
    module_path.parent.mkdir(parents=True)
    module_path.write_bytes(b"module")
    wheel = tmp_path / "openevo.whl"
    wheel.write_bytes(b"wheel")
    framework_lock = tmp_path / "framework-lock.json"
    framework_lock.write_text("{}", encoding="ascii")
    verified: list[Path] = []

    monkeypatch.setattr(service.sys, "prefix", str(generation_root))
    monkeypatch.setattr(service.sys, "executable", str(interpreter))
    monkeypatch.setattr(service, "__file__", str(module_path))
    monkeypatch.setattr(
        service,
        "load_framework_distribution_lock",
        lambda _path: (object(), wheel),
    )
    monkeypatch.setattr(
        service,
        "load_verified_framework_registry",
        lambda path: verified.append(Path(path)) or object(),
    )

    service._verify_generation_install(
        service_root=root,
        wheel_path=wheel,
        framework_lock=framework_lock,
        source_commit=SOURCE_COMMIT,
        install_generation=generation,
    )
    assert verified == [framework_lock]

    monkeypatch.setattr(service.sys, "prefix", str(root / "releases" / ("b" * 32)))
    with pytest.raises(CoreServiceError) as exc_info:
        service._verify_generation_install(
            service_root=root,
            wheel_path=wheel,
            framework_lock=framework_lock,
            source_commit=SOURCE_COMMIT,
            install_generation=generation,
        )
    assert exc_info.value.code is CoreServiceErrorCode.VERIFICATION_FAILED


def test_inherited_bootstrap_lock_must_be_bound_and_held(tmp_path: Path) -> None:
    root = _root(tmp_path)
    with HostServiceRoot(root) as pinned:
        lock_fd = pinned.open_lock("bootstrap.lock")
        try:
            with pytest.raises(CoreServiceError) as exc_info:
                service._require_inherited_lock(pinned, "bootstrap.lock", lock_fd)
            assert exc_info.value.code is CoreServiceErrorCode.STATE_INVALID

            service._flock_until(lock_fd, time.monotonic() + 1)
            service._require_inherited_lock(pinned, "bootstrap.lock", lock_fd)
        finally:
            os.close(lock_fd)


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


def test_deterministic_fault_injection_runs_after_spawn_before_publication(
    tmp_path: Path,
    service_fakes: tuple[FakeController, list[FakeChild]],
) -> None:
    controller, children = service_fakes
    root = _root(tmp_path)
    lock = tmp_path / "framework-lock.json"
    lock.write_text("{}", encoding="ascii")
    injected: list[tuple[str, int]] = []

    class InjectedCrash(RuntimeError):
        pass

    def crash(stage: str, pid: int) -> None:
        injected.append((stage, pid))
        raise InjectedCrash

    with pytest.raises(InjectedCrash):
        ensure_core_service(
            service_root=root,
            framework_lock=lock,
            source_commit=SOURCE_COMMIT,
            process_controller=controller,
            _fault_injector=crash,
        )

    assert injected == [("after_spawn", children[0].pid)]
    assert children[0].returncode == -15
    assert not (root / "service.json").exists()


def test_spawn_cancellation_retains_unconfirmed_child_and_propagates_original(
    tmp_path: Path,
    service_fakes: tuple[FakeController, list[FakeChild]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller, children = service_fakes
    root = _root(tmp_path)
    lock = tmp_path / "framework-lock.json"
    lock.write_text("{}", encoding="ascii")

    class Cancelled(BaseException):
        pass

    cleanup_fails = True

    def cancel_after_spawn(_phase: str, _pid: int) -> None:
        child = children[-1]

        def terminate() -> None:
            if cleanup_fails:
                raise OSError("terminate failed")
            child.returncode = -15

        def wait(timeout: float | None = None) -> int:
            if cleanup_fails:
                raise subprocess.TimeoutExpired("core", timeout)
            assert child.returncode is not None
            return child.returncode

        def kill() -> None:
            if cleanup_fails:
                raise OSError("kill failed")
            child.returncode = -9

        monkeypatch.setattr(child, "terminate", terminate)
        monkeypatch.setattr(child, "wait", wait)
        monkeypatch.setattr(child, "kill", kill)
        raise Cancelled

    with pytest.raises(Cancelled):
        ensure_core_service(
            service_root=root,
            framework_lock=lock,
            source_commit=SOURCE_COMMIT,
            process_controller=controller,
            _fault_injector=cancel_after_spawn,
        )

    child = children[-1]
    assert id(child) in service._ORPHANED_SERVICE_CHILDREN
    assert (root / "pending.json").exists()
    cleanup_fails = False
    service._retry_orphaned_service_children()
    assert id(child) not in service._ORPHANED_SERVICE_CHILDREN
    assert (root / "pending.json").exists()
    with HostServiceRoot(root) as pinned:
        service._recover_pending(
            pinned,
            controller=controller,
            deadline=time.monotonic() + 1,
        )
    assert not (root / "pending.json").exists()


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

    def fetch(_host: str, _port: int, path: str, **_kwargs: object) -> object:
        return version if path == "/version" else status

    monkeypatch.setattr(service, "_fetch_json", fetch)
    proof = service._authenticated_status_proof(
        port=8765,
        bearer="B" * 64,
        release=RELEASE_A,
        generation="3" * 32,
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
            generation="3" * 32,
            deadline=time.monotonic() + 1,
        )
    assert exc_info.value.code is CoreServiceErrorCode.STATUS_INVALID


def test_status_proof_accepts_exact_v2_daemon_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[str] = []
    feature_flags = ["immutable_task_admission"]
    version = {
        "schema_version": "2",
        "api_name": "openevo-core-control-api",
        "preferred_major": 2,
        "supported_majors": [1, 2],
        "mutation_major": 2,
        "mutation_compatible": True,
        "provider_kind": "openevo_daemon",
        "build_channel": "release",
        "source_commit": SOURCE_COMMIT,
        "release_version": "0.1.9",
        "build_id": "6" * 64,
        "feature_flags": feature_flags,
        "feature_set_sha256": hashlib.sha256(
            json.dumps(feature_flags, separators=(",", ":")).encode("ascii")
        ).hexdigest(),
        "registry_sha256": RELEASE_A.registry_digest,
        "runtime_contract_sha256": "7" * 64,
        "contracts": [
            {
                "api_major": 1,
                "access": "read_only_migration",
                "mutation_compatible": False,
                "openapi_sha256": "1" * 64,
                "event_schema_sha256": "2" * 64,
            },
            {
                "api_major": 2,
                "access": "mutation",
                "mutation_compatible": True,
                "openapi_sha256": "3" * 64,
                "event_schema_sha256": "4" * 64,
            },
        ],
    }
    status = {
        "schema_version": "2",
        "status": "ready",
        "source_commit": SOURCE_COMMIT,
        "release_version": "0.1.9",
        "registry_sha256": RELEASE_A.registry_digest,
        "checked_at": "2026-07-23T03:00:00.000000Z",
    }

    def fetch(_host: str, _port: int, path: str, **_kwargs: object) -> object:
        requests.append(path)
        return version if path == "/version" else status

    monkeypatch.setattr(service, "_fetch_json", fetch)
    proof = service._authenticated_status_proof(
        port=8765,
        bearer="B" * 64,
        release=RELEASE_A,
        generation="3" * 32,
        deadline=time.monotonic() + 1,
    )

    assert len(proof) == 64
    assert requests == ["/version", "/v2/system/status"]

    version["feature_flags"] = ["event_replay_v2", "verified_registry"]
    version["feature_set_sha256"] = hashlib.sha256(
        json.dumps(version["feature_flags"], separators=(",", ":")).encode("ascii")
    ).hexdigest()
    version["build_id"] = "8" * 64
    changed_feature_proof = service._authenticated_status_proof(
        port=8765,
        bearer="B" * 64,
        release=RELEASE_A,
        generation="3" * 32,
        deadline=time.monotonic() + 1,
    )
    assert changed_feature_proof != proof

    version["mutation_compatible"] = False
    with pytest.raises(CoreServiceError) as exc_info:
        service._authenticated_status_proof(
            port=8765,
            bearer="B" * 64,
            release=RELEASE_A,
            generation="3" * 32,
            deadline=time.monotonic() + 1,
        )
    assert exc_info.value.code is CoreServiceErrorCode.STATUS_INVALID


def test_release_status_proof_requires_the_complete_production_v2_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    features = list(RELEASE_DAEMON_FEATURE_FLAGS_V2)
    version = {
        "schema_version": "2",
        "api_name": "openevo-core-control-api",
        "preferred_major": 2,
        "supported_majors": [2],
        "mutation_major": 2,
        "mutation_compatible": True,
        "provider_kind": "openevo_daemon",
        "build_channel": "release",
        "source_commit": SOURCE_COMMIT,
        "release_version": __version__,
        "build_id": "6" * 64,
        "feature_flags": features,
        "feature_set_sha256": hashlib.sha256(
            json.dumps(features, separators=(",", ":")).encode("ascii")
        ).hexdigest(),
        "registry_sha256": RELEASE_A.registry_digest,
        "runtime_contract_sha256": release_runtime_contract_sha256(),
        "contracts": [
            {
                "api_major": 2,
                "access": "mutation",
                "mutation_compatible": True,
                "openapi_sha256": openapi_sha256(),
                "event_schema_sha256": events_schema_sha256(),
            }
        ],
    }
    status = {
        "schema_version": "2",
        "status": "ready",
        "source_commit": SOURCE_COMMIT,
        "release_version": __version__,
        "registry_sha256": RELEASE_A.registry_digest,
        "checked_at": "2026-07-23T03:00:00.000000Z",
    }

    def fetch(_host: str, _port: int, path: str, **_kwargs: object) -> object:
        return version if path == "/version" else status

    monkeypatch.setattr(service, "_fetch_json", fetch)
    proof = service._authenticated_status_proof(
        port=8765,
        bearer="B" * 64,
        release=RELEASE_A,
        generation="3" * 32,
        deadline=time.monotonic() + 1,
        require_production_v2=True,
    )
    assert len(proof) == 64

    version["runtime_contract_sha256"] = "7" * 64
    with pytest.raises(CoreServiceError) as exc_info:
        service._authenticated_status_proof(
            port=8765,
            bearer="B" * 64,
            release=RELEASE_A,
            generation="3" * 32,
            deadline=time.monotonic() + 1,
            require_production_v2=True,
        )
    assert exc_info.value.code is CoreServiceErrorCode.STATUS_INVALID


def test_observation_accepts_exact_published_v019_daemon_predecessor(
    tmp_path: Path,
    service_fakes: tuple[FakeController, list[FakeChild]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller, _children = service_fakes
    root = _root(tmp_path)
    (root / "state").mkdir(mode=0o700)
    generation = "3" * 32
    with socket.socket() as available:
        available.bind(("127.0.0.1", 0))
        port = int(available.getsockname()[1])
    bearer = "B" * 64
    launcher_process = controller.capture(4000)
    application_process = controller.capture(5000)
    version = {
        "api_name": "openevo-core-control-api",
        "build_channel": "release",
        "build_id": "4b42bb11dcd5b3aa66d9de112b101e3f248c6d4e722956f16588f1b288e0559c",
        "contracts": [
            {
                "access": "mutation",
                "api_major": 2,
                "event_schema_sha256": (
                    "464a52685dacaedc391fb17bb27516e64842e23d89d12d475679d7a41a0668df"
                ),
                "mutation_compatible": True,
                "openapi_sha256": (
                    "f007726d8b092463a2515500e3cc0c496b52b45e9f24d1fc495b11df9a9a837b"
                ),
                "schema_version": "2",
            }
        ],
        "feature_flags": [
            "atomic_successor_v2",
            "event_replay_v2",
            "project_genesis_v2",
            "project_heads_v2",
            "task_admission_v2",
            "task_execution_v2",
            "verified_capabilities",
            "verified_registry",
            "workspace_snapshots_v2",
        ],
        "feature_set_sha256": ("ba514a0165727757d147ab09d9ee934a0c0eab2411ec5e2244d49237146d3f56"),
        "mutation_compatible": True,
        "mutation_major": 2,
        "preferred_major": 2,
        "provider_kind": "openevo_daemon",
        "registry_sha256": PUBLISHED_V019_RELEASE.registry_digest,
        "release_version": "0.1.9",
        "runtime_contract_sha256": (
            "535e3a05645590c90956769d960884fbbd818280b7517582a72e0b4fb41987f0"
        ),
        "schema_version": "2",
        "source_commit": PUBLISHED_V019_RELEASE.source_commit,
        "supported_majors": [2],
    }
    status = {
        "checked_at": "2026-07-27T18:34:41.849413Z",
        "registry_sha256": PUBLISHED_V019_RELEASE.registry_digest,
        "release_version": "0.1.9",
        "schema_version": "2",
        "source_commit": PUBLISHED_V019_RELEASE.source_commit,
        "status": "ready",
    }

    def fetch(_host: str, _port: int, path: str, **_kwargs: object) -> object:
        return version if path == "/version" else status

    monkeypatch.setattr(service, "_fetch_json", fetch)
    status_proof = _REAL_AUTHENTICATED_STATUS_PROOF(
        port=port,
        bearer=bearer,
        release=PUBLISHED_V019_RELEASE,
        generation=generation,
        deadline=time.monotonic() + 1,
        require_production_v2=False,
    )
    ready = {
        "schema_version": 2,
        "generation": generation,
        "release_identity": PUBLISHED_V019_RELEASE.digest,
        "registry_digest": PUBLISHED_V019_RELEASE.registry_digest,
        "status_proof": status_proof,
        "bundle_sha256": PUBLISHED_V019_DAEMON.bundle_sha256,
        "canonical_manifest_sha256": PUBLISHED_V019_DAEMON.canonical_manifest_sha256,
        "lifecycle_compatibility": PUBLISHED_V019_DAEMON.lifecycle_compatibility,
    }
    ledger = {
        "schema_version": 5,
        "state": "running",
        "release_identity": PUBLISHED_V019_RELEASE.digest,
        "registry_digest": PUBLISHED_V019_RELEASE.registry_digest,
        "framework_lock_sha256": PUBLISHED_V019_RELEASE.framework_lock_sha256,
        "source_commit": PUBLISHED_V019_RELEASE.source_commit,
        "pid": launcher_process.pid,
        "boot_id": launcher_process.boot_id,
        "start_time_ticks": launcher_process.start_time_ticks,
        "application_pid": application_process.pid,
        "application_boot_id": application_process.boot_id,
        "application_start_time_ticks": application_process.start_time_ticks,
        "port": port,
        "generation": generation,
        "ready_sha256": hashlib.sha256(service.canonical_json_bytes(ready)).hexdigest(),
        "bundle_sha256": PUBLISHED_V019_DAEMON.bundle_sha256,
        "canonical_manifest_sha256": PUBLISHED_V019_DAEMON.canonical_manifest_sha256,
        "lifecycle_compatibility": PUBLISHED_V019_DAEMON.lifecycle_compatibility,
    }
    assert service._is_exact_published_v019_predecessor_ledger(ledger)
    for field, drifted in (
        ("release_identity", "0" * 64),
        ("registry_digest", "0" * 64),
        ("framework_lock_sha256", "0" * 64),
        ("source_commit", "0" * 40),
        ("bundle_sha256", "0" * 64),
        ("canonical_manifest_sha256", "0" * 64),
        ("lifecycle_compatibility", 15),
    ):
        changed_ledger = {**ledger, field: drifted}
        assert not service._is_exact_published_v019_predecessor_ledger(changed_ledger)
    assert service._is_v0110_published_v019_upgrade(
        ledger,
        release=RELEASE_A,
        candidate=DAEMON_V16,
    )
    assert not service._is_v0110_published_v019_upgrade(
        ledger,
        release=PUBLISHED_V019_RELEASE,
        candidate=DAEMON_V16,
    )
    assert not service._is_v0110_published_v019_upgrade(
        ledger,
        release=RELEASE_A,
        candidate=PUBLISHED_V019_DAEMON,
    )
    assert not service._is_v0110_published_v019_upgrade(
        ledger,
        release=RELEASE_A,
        candidate=DAEMON_V15,
    )
    floor = service._floor_from_ledger(ledger)
    assert service._is_exact_published_v019_predecessor_floor(floor)
    assert service._is_v0110_published_v019_upgrade(
        floor,
        release=RELEASE_A,
        candidate=DAEMON_V16,
    )
    service._require_floor_compatibility(
        floor,
        DAEMON_V16,
        allow_equal_replacement=True,
    )
    with pytest.raises(CoreServiceError) as floor_exc:
        service._require_floor_compatibility(floor, DAEMON_V16)
    assert floor_exc.value.code is CoreServiceErrorCode.UPDATE_REQUIRED

    def seed_private_file(name: str, payload: bytes) -> None:
        descriptor = os.open(root / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(descriptor, payload)
        finally:
            os.close(descriptor)

    seed_private_file("bearer-token", (bearer + "\n").encode("ascii"))
    seed_private_file("ready.json", service.canonical_json_bytes(ready) + b"\n")
    seed_private_file("service.json", service.canonical_json_bytes(ledger) + b"\n")

    monkeypatch.setattr(
        service,
        "_authenticated_status_proof",
        _REAL_AUTHENTICATED_STATUS_PROOF,
    )
    predecessor = service.observe_core_service_predecessor(
        service_root=root,
        process_controller=controller,
    )

    assert predecessor == CoreServicePredecessor.running(
        generation=generation,
        release_identity=PUBLISHED_V019_RELEASE.digest,
        bundle_sha256=PUBLISHED_V019_DAEMON.bundle_sha256,
        canonical_manifest_sha256=(PUBLISHED_V019_DAEMON.canonical_manifest_sha256),
        lifecycle_compatibility=PUBLISHED_V019_DAEMON.lifecycle_compatibility,
    )

    with pytest.raises(CoreServiceError) as strict_exc:
        _REAL_AUTHENTICATED_STATUS_PROOF(
            port=port,
            bearer=bearer,
            release=PUBLISHED_V019_RELEASE,
            generation=generation,
            deadline=time.monotonic() + 1,
            require_production_v2=True,
        )
    assert strict_exc.value.code is CoreServiceErrorCode.STATUS_INVALID

    published_authority = service._ProductionV2DiscoveryAuthority.PUBLISHED_V019_PREDECESSOR
    for field, drifted in (
        ("release_version", "0.1.8"),
        ("release_version", "0.1.11"),
        ("build_id", "0" * 64),
        ("runtime_contract_sha256", "0" * 64),
    ):
        original = version[field]
        version[field] = drifted
        try:
            with pytest.raises(CoreServiceError) as drift_exc:
                _REAL_AUTHENTICATED_STATUS_PROOF(
                    port=port,
                    bearer=bearer,
                    release=PUBLISHED_V019_RELEASE,
                    generation=generation,
                    deadline=time.monotonic() + 1,
                    require_production_v2=True,
                    production_v2_authority=published_authority,
                )
            assert drift_exc.value.code is CoreServiceErrorCode.STATUS_INVALID
        finally:
            version[field] = original

    def publish_noreplace(directory_fd: int, source: str, destination: str) -> None:
        os.link(
            source,
            destination,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
            follow_symlinks=False,
        )
        os.unlink(source, dir_fd=directory_fd)

    monkeypatch.setattr(runtime_identity, "_rename_noreplace", publish_noreplace)
    monkeypatch.setattr(service, "_authenticated_status_proof", lambda **_kwargs: "9" * 64)
    if not hasattr(os, "pipe2"):

        def pipe2(_flags: int) -> tuple[int, int]:
            read_descriptor, write_descriptor = os.pipe()
            os.set_inheritable(read_descriptor, False)
            os.set_inheritable(write_descriptor, False)
            return read_descriptor, write_descriptor

        monkeypatch.setattr(service.os, "pipe2", pipe2, raising=False)
    replacement = ensure_core_service(
        service_root=root,
        framework_lock=tmp_path / "framework-lock.json",
        source_commit=SOURCE_COMMIT,
        port=port,
        replace_mismatched=True,
        expected_predecessor=predecessor,
        daemon_bundle_identity=DAEMON_V16,
        process_controller=controller,
    )

    assert replacement.release_identity == RELEASE_A.digest
    assert replacement.generation != generation
    assert replacement.bundle_sha256 == DAEMON_V16.bundle_sha256
    assert replacement.lifecycle_compatibility == DAEMON_V16.lifecycle_compatibility
    assert controller.terminated == [application_process, launcher_process]


def test_release_launcher_ready_payload_binds_v2_contract_and_runtime() -> None:
    assert service._identity_requires_production_v2(DAEMON_V8) is False
    assert service._identity_requires_production_v2(DAEMON_V9) is False
    assert service._identity_requires_production_v2(DAEMON_V10) is True
    payload = {
        "schema_version": 2,
        "generation": "3" * 32,
        "release_identity": RELEASE_A.digest,
        "api_major": 2,
        "openapi_sha256": openapi_sha256(),
        "event_schema_sha256": events_schema_sha256(),
        "release_version": __version__,
        "build_id": "6" * 64,
        "source_commit": SOURCE_COMMIT,
        "provider_kind": "openevo_daemon",
        "feature_set_sha256": service._production_v2_feature_set_sha256(),
        "registry_digest": RELEASE_A.registry_digest,
        "runtime_contract_sha256": release_runtime_contract_sha256(),
    }
    assert service._launcher_ready_matches(
        payload,
        release=RELEASE_A,
        generation="3" * 32,
        require_production_v2=True,
    )

    drifted = {**payload, "openapi_sha256": "0" * 64}
    assert not service._launcher_ready_matches(
        drifted,
        release=RELEASE_A,
        generation="3" * 32,
        require_production_v2=True,
    )
    legacy = {
        "schema_version": 1,
        "generation": "3" * 32,
        "release_identity": RELEASE_A.digest,
        "registry_digest": RELEASE_A.registry_digest,
    }
    assert not service._launcher_ready_matches(
        legacy,
        release=RELEASE_A,
        generation="3" * 32,
        require_production_v2=True,
    )


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
            "127.0.0.1",
            8765,
            "/v1/status",
            bearer="S" * 64,
            deadline=time.monotonic() + 1,
        )
    assert exc_info.value.code is CoreServiceErrorCode.STATUS_INVALID


def test_endpoint_probe_authenticates_version_and_rejects_wrong_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    generation = "3" * 32
    requests: list[tuple[str, dict[str, str]]] = []
    response_generation = ["0" * 32]

    class Headers:
        def get_content_type(self) -> str:
            return "application/json"

        def get(self, name: str, default: str | None = None) -> str | None:
            values = {
                "Content-Length": str(len(self.payload)),
                "X-OpenEvo-Core-Generation": response_generation[0],
                "X-OpenEvo-Core-Release-Identity": RELEASE_A.digest,
            }
            return values.get(name, default)

    class Response:
        status = 200

        def __init__(self, path: str) -> None:
            value = (
                {
                    "provider_kind": "openevo_core",
                    "build_channel": "release",
                    "source_commit": SOURCE_COMMIT,
                    "openapi_sha256": "4" * 64,
                    "build_version": "0.1.0",
                }
                if path == "/version"
                else {
                    "registry_status": "verified",
                    "registry_digest": RELEASE_A.registry_digest,
                }
            )
            self.payload = json.dumps(value).encode("ascii")
            self.headers = Headers()
            self.headers.payload = self.payload

        def read(self, _limit: int) -> bytes:
            return self.payload

    class Connection:
        def __init__(self, *args: object, **kwargs: object) -> None:
            del args, kwargs
            self.path = ""

        def request(
            self,
            _method: str,
            path: str,
            *,
            headers: dict[str, str],
        ) -> None:
            self.path = path
            requests.append((path, headers))

        def getresponse(self) -> Response:
            return Response(self.path)

        def close(self) -> None:
            pass

    monkeypatch.setattr(service.http.client, "HTTPConnection", Connection)

    with pytest.raises(CoreServiceError) as exc_info:
        service.authenticate_core_service_endpoint(
            host="127.0.0.1",
            port=8765,
            bearer="S" * 64,
            release_identity=RELEASE_A.digest,
            registry_digest=RELEASE_A.registry_digest,
            source_commit=SOURCE_COMMIT,
            generation=generation,
            deadline=time.monotonic() + 1,
        )

    assert exc_info.value.code is CoreServiceErrorCode.STATUS_INVALID
    assert requests == [("/version", {"Authorization": f"Bearer {'S' * 64}"})]

    response_generation[0] = generation
    requests.clear()
    proof = service.authenticate_core_service_endpoint(
        host="127.0.0.1",
        port=8765,
        bearer="S" * 64,
        release_identity=RELEASE_A.digest,
        registry_digest=RELEASE_A.registry_digest,
        source_commit=SOURCE_COMMIT,
        generation=generation,
        deadline=time.monotonic() + 1,
    )
    assert len(proof) == 64
    assert requests == [
        ("/version", {"Authorization": f"Bearer {'S' * 64}"}),
        ("/v1/status", {"Authorization": f"Bearer {'S' * 64}"}),
    ]


def test_endpoint_probe_uses_verified_unix_socket_transport() -> None:
    generation = "3" * 32
    requests: list[bytes] = []
    servers: list[threading.Thread] = []
    timeouts: list[float] = []

    class Endpoint:
        def verify_authority(self) -> None:
            return None

        def open_verified_socket(self, *, timeout_seconds: float) -> socket.socket:
            timeouts.append(timeout_seconds)
            client, server_socket = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
            client.settimeout(timeout_seconds)

            def serve() -> None:
                with server_socket:
                    request = bytearray()
                    while b"\r\n\r\n" not in request:
                        request.extend(server_socket.recv(4096))
                    requests.append(bytes(request))
                    path = request.split(b" ", 2)[1]
                    payload = json.dumps(
                        {
                            "provider_kind": "openevo_core",
                            "build_channel": "release",
                            "source_commit": SOURCE_COMMIT,
                            "openapi_sha256": "4" * 64,
                            "build_version": "0.1.0",
                        }
                        if path == b"/version"
                        else {
                            "registry_status": "verified",
                            "registry_digest": RELEASE_A.registry_digest,
                        },
                        separators=(",", ":"),
                    ).encode("ascii")
                    response = (
                        b"HTTP/1.1 200 OK\r\n"
                        b"Content-Type: application/json\r\n"
                        + f"Content-Length: {len(payload)}\r\n".encode("ascii")
                        + f"X-OpenEvo-Core-Generation: {generation}\r\n".encode("ascii")
                        + (f"X-OpenEvo-Core-Release-Identity: {RELEASE_A.digest}\r\n").encode(
                            "ascii"
                        )
                        + b"Connection: close\r\n\r\n"
                        + payload
                    )
                    server_socket.sendall(response)

            thread = threading.Thread(target=serve)
            thread.start()
            servers.append(thread)
            return client

    proof = service.authenticate_core_service_endpoint(
        host=None,
        port=None,
        bearer="S" * 64,
        release_identity=RELEASE_A.digest,
        registry_digest=RELEASE_A.registry_digest,
        source_commit=SOURCE_COMMIT,
        generation=generation,
        deadline=time.monotonic() + 2,
        endpoint=Endpoint(),
    )
    for thread in servers:
        thread.join(timeout=1)

    assert len(proof) == 64
    assert len(requests) == 2
    assert len(timeouts) == 2
    assert all(1.0 < timeout <= 2.0 for timeout in timeouts)
    assert requests[0].startswith(b"GET /version HTTP/1.1\r\n")
    assert requests[1].startswith(b"GET /v1/status HTTP/1.1\r\n")
    assert all(f"Authorization: Bearer {'S' * 64}".encode("ascii") in value for value in requests)


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

    with HostServiceRoot(root, create=False) as pinned:
        legacy_ledger = pinned.read_json("service.json")
        legacy_ledger["schema_version"] = 1
        pinned.atomic_write_json("service.json", legacy_ledger, replace=True)
    predecessor = service.observe_core_service_predecessor(
        service_root=root,
        process_controller=controller,
    )
    replacement = ensure_core_service(
        service_root=root,
        framework_lock=lock,
        source_commit=SOURCE_COMMIT,
        port=first.port,
        replace_mismatched=True,
        expected_predecessor=predecessor,
        process_controller=controller,
    )
    assert replacement.release_identity == RELEASE_B.digest
    assert replacement.bearer_token != first_bearer
    assert len(controller.terminated) == 1
    assert len(children) == 2


def test_mixed_version_interleaving_fences_stale_rollback_and_allows_exact_retry(
    tmp_path: Path,
    service_fakes: tuple[FakeController, list[FakeChild]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller, children = service_fakes
    root = _root(tmp_path)
    lock = tmp_path / "framework-lock.json"
    lock.write_text("{}", encoding="ascii")
    old = ensure_core_service(
        service_root=root,
        framework_lock=lock,
        source_commit=SOURCE_COMMIT,
        process_controller=controller,
    )
    stale_old_activation = service.observe_core_service_predecessor(
        service_root=root,
        process_controller=controller,
    )
    assert stale_old_activation == CoreServicePredecessor.running(
        generation=old.generation,
        release_identity=RELEASE_A.digest,
    )

    monkeypatch.setattr(service, "compute_release_identity", lambda **_kwargs: RELEASE_B)
    with pytest.raises(CoreServiceError) as unfenced:
        ensure_core_service(
            service_root=root,
            framework_lock=lock,
            source_commit=SOURCE_COMMIT,
            replace_mismatched=True,
            process_controller=controller,
        )
    assert unfenced.value.code is CoreServiceErrorCode.PREDECESSOR_MISMATCH
    assert controller.terminated == []

    new = ensure_core_service(
        service_root=root,
        framework_lock=lock,
        source_commit=SOURCE_COMMIT,
        replace_mismatched=True,
        expected_predecessor=stale_old_activation,
        process_controller=controller,
    )
    terminated_after_upgrade = list(controller.terminated)

    monkeypatch.setattr(service, "compute_release_identity", lambda **_kwargs: RELEASE_A)
    with pytest.raises(CoreServiceError) as exc_info:
        ensure_core_service(
            service_root=root,
            framework_lock=lock,
            source_commit=SOURCE_COMMIT,
            replace_mismatched=True,
            expected_predecessor=stale_old_activation,
            process_controller=controller,
        )

    assert exc_info.value.code is CoreServiceErrorCode.PREDECESSOR_MISMATCH
    assert controller.terminated == terminated_after_upgrade
    assert controller.is_alive(controller.current[children[-1].pid])

    monkeypatch.setattr(service, "compute_release_identity", lambda **_kwargs: RELEASE_B)
    retry = ensure_core_service(
        service_root=root,
        framework_lock=lock,
        source_commit=SOURCE_COMMIT,
        replace_mismatched=True,
        expected_predecessor=stale_old_activation,
        process_controller=controller,
    )
    assert retry.attached is True
    assert retry.generation == new.generation
    assert len(children) == 2
    with HostServiceRoot(root, create=False) as pinned:
        ledger = pinned.read_json("service.json")
    assert ledger["schema_version"] == 2


def test_daemon_same_release_different_bundle_fails_without_stopping_live_service(
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
        expected_predecessor=CoreServicePredecessor.absent(),
        daemon_bundle_identity=DAEMON_A,
        process_controller=controller,
    )
    predecessor = service.observe_core_service_predecessor(
        service_root=root,
        process_controller=controller,
    )

    with pytest.raises(CoreServiceError) as stale:
        ensure_core_service(
            service_root=root,
            framework_lock=lock,
            source_commit=SOURCE_COMMIT,
            expected_predecessor=CoreServicePredecessor.absent(),
            daemon_bundle_identity=DAEMON_A,
            process_controller=controller,
        )
    assert stale.value.code is CoreServiceErrorCode.PREDECESSOR_MISMATCH
    assert stale.value.retryable is True
    assert controller.terminated == []

    same = ensure_core_service(
        service_root=root,
        framework_lock=lock,
        source_commit=SOURCE_COMMIT,
        expected_predecessor=predecessor,
        daemon_bundle_identity=DAEMON_A,
        process_controller=controller,
    )
    assert same.attached is True
    assert same.generation == first.generation

    with pytest.raises(CoreServiceError) as raised:
        ensure_core_service(
            service_root=root,
            framework_lock=lock,
            source_commit=SOURCE_COMMIT,
            replace_mismatched=True,
            expected_predecessor=predecessor,
            daemon_bundle_identity=DAEMON_B,
            process_controller=controller,
        )

    assert raised.value.code is CoreServiceErrorCode.UPDATE_REQUIRED
    assert raised.value.retryable is False
    assert controller.terminated == []
    assert controller.is_alive(controller.current[children[0].pid])
    assert (
        service.inspect_core_service(
            service_root=root,
            process_controller=controller,
        ).generation
        == first.generation
    )


def test_process_group_daemon_rejects_compatibility_v2(
    tmp_path: Path,
    service_fakes: tuple[FakeController, list[FakeChild]],
) -> None:
    controller, _children = service_fakes
    root = _root(tmp_path)
    lock = tmp_path / "framework-lock.json"
    lock.write_text("{}", encoding="ascii")

    with pytest.raises(CoreServiceError) as exc_info:
        ensure_core_service(
            service_root=root,
            framework_lock=lock,
            source_commit=SOURCE_COMMIT,
            expected_predecessor=CoreServicePredecessor.absent(),
            daemon_bundle_identity=DAEMON_V2,
            process_controller=controller,
        )

    assert exc_info.value.code is CoreServiceErrorCode.START_FAILED
    assert not (root / "service.json").exists()


def test_compatibility_v3_reader_stops_v2_daemon_and_upgrades_its_floor(
    tmp_path: Path,
    service_fakes: tuple[FakeController, list[FakeChild]],
) -> None:
    controller, _children = service_fakes
    root = _root(tmp_path)
    lock = tmp_path / "framework-lock.json"
    lock.write_text("{}", encoding="ascii")
    old = ensure_core_service(
        service_root=root,
        framework_lock=lock,
        source_commit=SOURCE_COMMIT,
        process_controller=controller,
    )
    with HostServiceRoot(root, create=False) as pinned:
        ready = pinned.read_json("ready.json")
        ready.update(
            {
                "schema_version": 2,
                "bundle_sha256": DAEMON_V2.bundle_sha256,
                "canonical_manifest_sha256": DAEMON_V2.canonical_manifest_sha256,
                "lifecycle_compatibility": DAEMON_V2.lifecycle_compatibility,
            }
        )
        ledger = pinned.read_json("service.json")
        ledger.update(
            {
                "schema_version": 3,
                "state": "running",
                "bundle_sha256": DAEMON_V2.bundle_sha256,
                "canonical_manifest_sha256": DAEMON_V2.canonical_manifest_sha256,
                "lifecycle_compatibility": DAEMON_V2.lifecycle_compatibility,
                "ready_sha256": hashlib.sha256(service.canonical_json_bytes(ready)).hexdigest(),
            }
        )
        pinned.atomic_write_json("ready.json", ready, replace=True)
        pinned.atomic_write_json("service.json", ledger, replace=True)

    predecessor = service.observe_core_service_predecessor(
        service_root=root,
        process_controller=controller,
    )
    assert predecessor == CoreServicePredecessor.running(
        generation=old.generation,
        release_identity=old.release_identity,
        bundle_sha256=DAEMON_V2.bundle_sha256,
        canonical_manifest_sha256=DAEMON_V2.canonical_manifest_sha256,
        lifecycle_compatibility=2,
    )
    upgraded = ensure_core_service(
        service_root=root,
        framework_lock=lock,
        source_commit=SOURCE_COMMIT,
        replace_mismatched=True,
        expected_predecessor=predecessor,
        daemon_bundle_identity=DAEMON_A,
        process_controller=controller,
    )
    with HostServiceRoot(root, create=False) as pinned:
        upgraded_ledger = pinned.read_json("service.json")

    assert upgraded.attached is False
    assert upgraded.generation != old.generation
    assert upgraded.lifecycle_compatibility == 3
    assert upgraded_ledger["schema_version"] == 5
    assert upgraded_ledger["lifecycle_compatibility"] == 3


def test_compatibility_v4_reader_stops_v3_daemon_and_upgrades_its_floor(
    tmp_path: Path,
    service_fakes: tuple[FakeController, list[FakeChild]],
) -> None:
    controller, _children = service_fakes
    root = _root(tmp_path)
    lock = tmp_path / "framework-lock.json"
    lock.write_text("{}", encoding="ascii")
    old = ensure_core_service(
        service_root=root,
        framework_lock=lock,
        source_commit=SOURCE_COMMIT,
        expected_predecessor=CoreServicePredecessor.absent(),
        daemon_bundle_identity=DAEMON_A,
        process_controller=controller,
    )
    predecessor = service.observe_core_service_predecessor(
        service_root=root,
        process_controller=controller,
    )

    upgraded = ensure_core_service(
        service_root=root,
        framework_lock=lock,
        source_commit=SOURCE_COMMIT,
        replace_mismatched=True,
        expected_predecessor=predecessor,
        daemon_bundle_identity=DAEMON_V4,
        process_controller=controller,
    )
    with HostServiceRoot(root, create=False) as pinned:
        upgraded_ledger = pinned.read_json("service.json")

    assert upgraded.attached is False
    assert upgraded.generation != old.generation
    assert upgraded.lifecycle_compatibility == 4
    assert upgraded_ledger["schema_version"] == 5
    assert upgraded_ledger["lifecycle_compatibility"] == 4


def test_compatibility_v5_reader_stops_v4_daemon_and_upgrades_its_floor(
    tmp_path: Path,
    service_fakes: tuple[FakeController, list[FakeChild]],
) -> None:
    controller, _children = service_fakes
    root = _root(tmp_path)
    lock = tmp_path / "framework-lock.json"
    lock.write_text("{}", encoding="ascii")
    old = ensure_core_service(
        service_root=root,
        framework_lock=lock,
        source_commit=SOURCE_COMMIT,
        expected_predecessor=CoreServicePredecessor.absent(),
        daemon_bundle_identity=DAEMON_V4,
        process_controller=controller,
    )
    predecessor = service.observe_core_service_predecessor(
        service_root=root,
        process_controller=controller,
    )

    upgraded = ensure_core_service(
        service_root=root,
        framework_lock=lock,
        source_commit=SOURCE_COMMIT,
        replace_mismatched=True,
        expected_predecessor=predecessor,
        daemon_bundle_identity=DAEMON_V5,
        process_controller=controller,
    )
    with HostServiceRoot(root, create=False) as pinned:
        upgraded_ledger = pinned.read_json("service.json")

    assert upgraded.attached is False
    assert upgraded.generation != old.generation
    assert upgraded.lifecycle_compatibility == 5
    assert upgraded_ledger["schema_version"] == 5
    assert upgraded_ledger["lifecycle_compatibility"] == 5


def test_compatibility_v6_reader_stops_v5_daemon_and_upgrades_its_floor(
    tmp_path: Path,
    service_fakes: tuple[FakeController, list[FakeChild]],
) -> None:
    controller, _children = service_fakes
    root = _root(tmp_path)
    lock = tmp_path / "framework-lock.json"
    lock.write_text("{}", encoding="ascii")
    old = ensure_core_service(
        service_root=root,
        framework_lock=lock,
        source_commit=SOURCE_COMMIT,
        expected_predecessor=CoreServicePredecessor.absent(),
        daemon_bundle_identity=DAEMON_V5,
        process_controller=controller,
    )
    predecessor = service.observe_core_service_predecessor(
        service_root=root,
        process_controller=controller,
    )

    upgraded = ensure_core_service(
        service_root=root,
        framework_lock=lock,
        source_commit=SOURCE_COMMIT,
        replace_mismatched=True,
        expected_predecessor=predecessor,
        daemon_bundle_identity=DAEMON_V6,
        process_controller=controller,
    )
    with HostServiceRoot(root, create=False) as pinned:
        upgraded_ledger = pinned.read_json("service.json")

    assert upgraded.attached is False
    assert upgraded.generation != old.generation
    assert upgraded.lifecycle_compatibility == 6
    assert upgraded_ledger["schema_version"] == 5
    assert upgraded_ledger["lifecycle_compatibility"] == 6


def test_v7_candidate_starts_after_conditional_stop_persists_v6_floor(
    tmp_path: Path,
    service_fakes: tuple[FakeController, list[FakeChild]],
) -> None:
    controller, _children = service_fakes
    root = _root(tmp_path)
    lock = tmp_path / "framework-lock.json"
    lock.write_text("{}", encoding="ascii")
    old = ensure_core_service(
        service_root=root,
        framework_lock=lock,
        source_commit=SOURCE_COMMIT,
        expected_predecessor=CoreServicePredecessor.absent(),
        daemon_bundle_identity=DAEMON_V6,
        process_controller=controller,
    )

    stopped = service.stop_core_service_if_generation(
        service_root=root,
        expected_generation=old.generation,
        expected_release_identity=old.release_identity,
        process_controller=controller,
    )
    with HostServiceRoot(root, create=False) as pinned:
        floor = pinned.read_json("service.json")

    upgraded = ensure_core_service(
        service_root=root,
        framework_lock=lock,
        source_commit=SOURCE_COMMIT,
        expected_predecessor=CoreServicePredecessor.absent(),
        daemon_bundle_identity=DAEMON_V7,
        process_controller=controller,
    )

    assert stopped is True
    assert floor["state"] == "stopped"
    assert floor["lifecycle_compatibility"] == 6
    assert upgraded.attached is False
    assert upgraded.generation != old.generation
    assert upgraded.lifecycle_compatibility == 7


def test_v8_candidate_starts_after_conditional_stop_persists_v7_floor(
    tmp_path: Path,
    service_fakes: tuple[FakeController, list[FakeChild]],
) -> None:
    controller, _children = service_fakes
    root = _root(tmp_path)
    lock = tmp_path / "framework-lock.json"
    lock.write_text("{}", encoding="ascii")
    old = ensure_core_service(
        service_root=root,
        framework_lock=lock,
        source_commit=SOURCE_COMMIT,
        expected_predecessor=CoreServicePredecessor.absent(),
        daemon_bundle_identity=DAEMON_V7,
        process_controller=controller,
    )

    stopped = service.stop_core_service_if_generation(
        service_root=root,
        expected_generation=old.generation,
        expected_release_identity=old.release_identity,
        process_controller=controller,
    )
    with HostServiceRoot(root, create=False) as pinned:
        floor = pinned.read_json("service.json")

    upgraded = ensure_core_service(
        service_root=root,
        framework_lock=lock,
        source_commit=SOURCE_COMMIT,
        expected_predecessor=CoreServicePredecessor.absent(),
        daemon_bundle_identity=DAEMON_V8,
        process_controller=controller,
    )

    assert stopped is True
    assert floor["state"] == "stopped"
    assert floor["lifecycle_compatibility"] == 7
    assert upgraded.attached is False
    assert upgraded.generation != old.generation
    assert upgraded.lifecycle_compatibility == 8


def test_v10_candidate_starts_after_conditional_stop_persists_v9_floor(
    tmp_path: Path,
    service_fakes: tuple[FakeController, list[FakeChild]],
) -> None:
    controller, _children = service_fakes
    root = _root(tmp_path)
    lock = tmp_path / "framework-lock.json"
    lock.write_text("{}", encoding="ascii")
    old = ensure_core_service(
        service_root=root,
        framework_lock=lock,
        source_commit=SOURCE_COMMIT,
        expected_predecessor=CoreServicePredecessor.absent(),
        daemon_bundle_identity=DAEMON_V9,
        process_controller=controller,
    )

    stopped = service.stop_core_service_if_generation(
        service_root=root,
        expected_generation=old.generation,
        expected_release_identity=old.release_identity,
        process_controller=controller,
    )
    with HostServiceRoot(root, create=False) as pinned:
        floor = pinned.read_json("service.json")

    upgraded = ensure_core_service(
        service_root=root,
        framework_lock=lock,
        source_commit=SOURCE_COMMIT,
        expected_predecessor=CoreServicePredecessor.absent(),
        daemon_bundle_identity=DAEMON_V10,
        process_controller=controller,
    )

    assert stopped is True
    assert floor["state"] == "stopped"
    assert floor["lifecycle_compatibility"] == 9
    assert upgraded.attached is False
    assert upgraded.generation != old.generation
    assert upgraded.lifecycle_compatibility == 10


def test_v11_candidate_starts_after_conditional_stop_persists_v10_floor(
    tmp_path: Path,
    service_fakes: tuple[FakeController, list[FakeChild]],
) -> None:
    controller, _children = service_fakes
    root = _root(tmp_path)
    lock = tmp_path / "framework-lock.json"
    lock.write_text("{}", encoding="ascii")
    old = ensure_core_service(
        service_root=root,
        framework_lock=lock,
        source_commit=SOURCE_COMMIT,
        expected_predecessor=CoreServicePredecessor.absent(),
        daemon_bundle_identity=DAEMON_V10,
        process_controller=controller,
    )

    stopped = service.stop_core_service_if_generation(
        service_root=root,
        expected_generation=old.generation,
        expected_release_identity=old.release_identity,
        process_controller=controller,
    )
    with HostServiceRoot(root, create=False) as pinned:
        floor = pinned.read_json("service.json")

    upgraded = ensure_core_service(
        service_root=root,
        framework_lock=lock,
        source_commit=SOURCE_COMMIT,
        expected_predecessor=CoreServicePredecessor.absent(),
        daemon_bundle_identity=DAEMON_V11,
        process_controller=controller,
    )

    assert stopped is True
    assert floor["state"] == "stopped"
    assert floor["lifecycle_compatibility"] == 10
    assert upgraded.attached is False
    assert upgraded.generation != old.generation
    assert upgraded.lifecycle_compatibility == 11


def test_v12_candidate_starts_after_conditional_stop_persists_v11_floor(
    tmp_path: Path,
    service_fakes: tuple[FakeController, list[FakeChild]],
) -> None:
    controller, _children = service_fakes
    root = _root(tmp_path)
    lock = tmp_path / "framework-lock.json"
    lock.write_text("{}", encoding="ascii")
    old = ensure_core_service(
        service_root=root,
        framework_lock=lock,
        source_commit=SOURCE_COMMIT,
        expected_predecessor=CoreServicePredecessor.absent(),
        daemon_bundle_identity=DAEMON_V11,
        process_controller=controller,
    )

    stopped = service.stop_core_service_if_generation(
        service_root=root,
        expected_generation=old.generation,
        expected_release_identity=old.release_identity,
        process_controller=controller,
    )
    with HostServiceRoot(root, create=False) as pinned:
        floor = pinned.read_json("service.json")

    upgraded = ensure_core_service(
        service_root=root,
        framework_lock=lock,
        source_commit=SOURCE_COMMIT,
        expected_predecessor=CoreServicePredecessor.absent(),
        daemon_bundle_identity=DAEMON_V12,
        process_controller=controller,
    )

    assert stopped is True
    assert floor["state"] == "stopped"
    assert floor["lifecycle_compatibility"] == 11
    assert upgraded.attached is False
    assert upgraded.generation != old.generation
    assert upgraded.lifecycle_compatibility == 12


def test_v13_candidate_starts_after_conditional_stop_persists_v12_floor(
    tmp_path: Path,
    service_fakes: tuple[FakeController, list[FakeChild]],
) -> None:
    controller, _children = service_fakes
    root = _root(tmp_path)
    lock = tmp_path / "framework-lock.json"
    lock.write_text("{}", encoding="ascii")
    old = ensure_core_service(
        service_root=root,
        framework_lock=lock,
        source_commit=SOURCE_COMMIT,
        expected_predecessor=CoreServicePredecessor.absent(),
        daemon_bundle_identity=DAEMON_V12,
        process_controller=controller,
    )

    stopped = service.stop_core_service_if_generation(
        service_root=root,
        expected_generation=old.generation,
        expected_release_identity=old.release_identity,
        process_controller=controller,
    )
    with HostServiceRoot(root, create=False) as pinned:
        floor = pinned.read_json("service.json")

    upgraded = ensure_core_service(
        service_root=root,
        framework_lock=lock,
        source_commit=SOURCE_COMMIT,
        expected_predecessor=CoreServicePredecessor.absent(),
        daemon_bundle_identity=DAEMON_V13,
        process_controller=controller,
    )

    assert stopped is True
    assert floor["state"] == "stopped"
    assert floor["lifecycle_compatibility"] == 12
    assert upgraded.attached is False
    assert upgraded.generation != old.generation
    assert upgraded.lifecycle_compatibility == 13


def test_v14_candidate_starts_after_conditional_stop_persists_v13_floor(
    tmp_path: Path,
    service_fakes: tuple[FakeController, list[FakeChild]],
) -> None:
    controller, _children = service_fakes
    root = _root(tmp_path)
    lock = tmp_path / "framework-lock.json"
    lock.write_text("{}", encoding="ascii")
    old = ensure_core_service(
        service_root=root,
        framework_lock=lock,
        source_commit=SOURCE_COMMIT,
        expected_predecessor=CoreServicePredecessor.absent(),
        daemon_bundle_identity=DAEMON_V13,
        process_controller=controller,
    )

    stopped = service.stop_core_service_if_generation(
        service_root=root,
        expected_generation=old.generation,
        expected_release_identity=old.release_identity,
        process_controller=controller,
    )
    with HostServiceRoot(root, create=False) as pinned:
        floor = pinned.read_json("service.json")

    upgraded = ensure_core_service(
        service_root=root,
        framework_lock=lock,
        source_commit=SOURCE_COMMIT,
        expected_predecessor=CoreServicePredecessor.absent(),
        daemon_bundle_identity=DAEMON_V14,
        process_controller=controller,
    )

    assert stopped is True
    assert floor["state"] == "stopped"
    assert floor["lifecycle_compatibility"] == 13
    assert upgraded.attached is False
    assert upgraded.generation != old.generation
    assert upgraded.lifecycle_compatibility == 14


def test_v15_candidate_starts_after_conditional_stop_persists_v14_floor(
    tmp_path: Path,
    service_fakes: tuple[FakeController, list[FakeChild]],
) -> None:
    controller, _children = service_fakes
    root = _root(tmp_path)
    lock = tmp_path / "framework-lock.json"
    lock.write_text("{}", encoding="ascii")
    old = ensure_core_service(
        service_root=root,
        framework_lock=lock,
        source_commit=SOURCE_COMMIT,
        expected_predecessor=CoreServicePredecessor.absent(),
        daemon_bundle_identity=DAEMON_V14,
        process_controller=controller,
    )

    stopped = service.stop_core_service_if_generation(
        service_root=root,
        expected_generation=old.generation,
        expected_release_identity=old.release_identity,
        process_controller=controller,
    )
    with HostServiceRoot(root, create=False) as pinned:
        floor = pinned.read_json("service.json")

    upgraded = ensure_core_service(
        service_root=root,
        framework_lock=lock,
        source_commit=SOURCE_COMMIT,
        expected_predecessor=CoreServicePredecessor.absent(),
        daemon_bundle_identity=DAEMON_V15,
        process_controller=controller,
    )

    assert stopped is True
    assert floor["state"] == "stopped"
    assert floor["lifecycle_compatibility"] == 14
    assert upgraded.attached is False
    assert upgraded.generation != old.generation
    assert upgraded.lifecycle_compatibility == 15


def test_v16_candidate_starts_after_conditional_stop_persists_v15_floor(
    tmp_path: Path,
    service_fakes: tuple[FakeController, list[FakeChild]],
) -> None:
    controller, _children = service_fakes
    root = _root(tmp_path)
    lock = tmp_path / "framework-lock.json"
    lock.write_text("{}", encoding="ascii")
    old = ensure_core_service(
        service_root=root,
        framework_lock=lock,
        source_commit=SOURCE_COMMIT,
        expected_predecessor=CoreServicePredecessor.absent(),
        daemon_bundle_identity=DAEMON_V15,
        process_controller=controller,
    )

    stopped = service.stop_core_service_if_generation(
        service_root=root,
        expected_generation=old.generation,
        expected_release_identity=old.release_identity,
        process_controller=controller,
    )
    with HostServiceRoot(root, create=False) as pinned:
        floor = pinned.read_json("service.json")

    upgraded = ensure_core_service(
        service_root=root,
        framework_lock=lock,
        source_commit=SOURCE_COMMIT,
        expected_predecessor=CoreServicePredecessor.absent(),
        daemon_bundle_identity=DAEMON_V16,
        process_controller=controller,
    )

    assert stopped is True
    assert floor["state"] == "stopped"
    assert floor["lifecycle_compatibility"] == 15
    assert upgraded.attached is False
    assert upgraded.generation != old.generation
    assert upgraded.lifecycle_compatibility == 16


def test_dead_newer_daemon_floor_rejects_stale_desktop_downgrade(
    tmp_path: Path,
    service_fakes: tuple[FakeController, list[FakeChild]],
) -> None:
    controller, children = service_fakes
    root = _root(tmp_path)
    lock = tmp_path / "framework-lock.json"
    lock.write_text("{}", encoding="ascii")
    ensure_core_service(
        service_root=root,
        framework_lock=lock,
        source_commit=SOURCE_COMMIT,
        expected_predecessor=CoreServicePredecessor.absent(),
        daemon_bundle_identity=DAEMON_V4,
        process_controller=controller,
    )
    with HostServiceRoot(root, create=False) as pinned:
        newer = pinned.read_json("service.json")
    controller.current.pop(newer["pid"])

    with pytest.raises(CoreServiceError) as exc_info:
        ensure_core_service(
            service_root=root,
            framework_lock=lock,
            source_commit=SOURCE_COMMIT,
            expected_predecessor=CoreServicePredecessor.absent(),
            daemon_bundle_identity=DAEMON_A,
            process_controller=controller,
        )
    with HostServiceRoot(root, create=False) as pinned:
        floor = pinned.read_json("service.json")

    assert exc_info.value.code is CoreServiceErrorCode.UPDATE_REQUIRED
    assert len(children) == 1
    assert floor["state"] == "stopped"
    assert floor["lifecycle_compatibility"] == 4
    assert floor["bundle_sha256"] == DAEMON_V4.bundle_sha256


def test_source_development_service_replaces_dead_newer_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    service_fakes: tuple[FakeController, list[FakeChild]],
) -> None:
    controller, children = service_fakes
    root = _root(tmp_path)
    monkeypatch.setattr(service, "source_development_core_service_root", lambda: root)
    lock = tmp_path / "framework-lock.json"
    lock.write_text("{}", encoding="ascii")
    old = ensure_core_service(
        service_root=root,
        framework_lock=lock,
        source_commit=SOURCE_COMMIT,
        expected_predecessor=CoreServicePredecessor.absent(),
        daemon_bundle_identity=DAEMON_V4,
        process_controller=controller,
    )
    controller.current.pop(children[0].pid)

    replacement = ensure_core_service(
        service_root=root,
        framework_lock=lock,
        source_commit=SOURCE_COMMIT,
        expected_predecessor=CoreServicePredecessor.absent(),
        daemon_bundle_identity=DAEMON_A,
        process_controller=controller,
    )

    assert replacement.attached is False
    assert replacement.generation != old.generation
    assert replacement.lifecycle_compatibility == DAEMON_A.lifecycle_compatibility
    assert len(children) == 2


def test_source_development_stop_does_not_persist_daemon_floor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    service_fakes: tuple[FakeController, list[FakeChild]],
) -> None:
    controller, _children = service_fakes
    root = _root(tmp_path)
    monkeypatch.setattr(service, "source_development_core_service_root", lambda: root)
    lock = tmp_path / "framework-lock.json"
    lock.write_text("{}", encoding="ascii")
    ensure_core_service(
        service_root=root,
        framework_lock=lock,
        source_commit=SOURCE_COMMIT,
        expected_predecessor=CoreServicePredecessor.absent(),
        daemon_bundle_identity=DAEMON_V4,
        process_controller=controller,
    )

    stop_core_service(
        service_root=root,
        process_controller=controller,
        preserve_compatibility_floor=True,
    )

    with HostServiceRoot(root, create=False) as pinned:
        assert pinned.read_optional_json("service.json") is None


def test_daemon_floor_allows_legacy_upgrade_and_rejects_same_abi_downgrade(
    tmp_path: Path,
    service_fakes: tuple[FakeController, list[FakeChild]],
) -> None:
    controller, _children = service_fakes
    root = _root(tmp_path)
    lock = tmp_path / "framework-lock.json"
    lock.write_text("{}", encoding="ascii")
    ensure_core_service(
        service_root=root,
        framework_lock=lock,
        source_commit=SOURCE_COMMIT,
        process_controller=controller,
    )

    stop_core_service(
        service_root=root,
        process_controller=controller,
        preserve_compatibility_floor=True,
    )
    upgraded = ensure_core_service(
        service_root=root,
        framework_lock=lock,
        source_commit=SOURCE_COMMIT,
        expected_predecessor=CoreServicePredecessor.absent(),
        daemon_bundle_identity=DAEMON_A,
        process_controller=controller,
    )
    assert upgraded.attached is False
    stop_core_service(service_root=root, process_controller=controller)

    with pytest.raises(CoreServiceError) as downgrade:
        ensure_core_service(
            service_root=root,
            framework_lock=lock,
            source_commit=SOURCE_COMMIT,
            expected_predecessor=CoreServicePredecessor.absent(),
            daemon_bundle_identity=DAEMON_B,
            process_controller=controller,
        )
    assert downgrade.value.code is CoreServiceErrorCode.UPDATE_REQUIRED
    assert downgrade.value.retryable is False

    with pytest.raises(CoreServiceError) as legacy:
        ensure_core_service(
            service_root=root,
            framework_lock=lock,
            source_commit=SOURCE_COMMIT,
            process_controller=controller,
        )
    assert legacy.value.code is CoreServiceErrorCode.UPDATE_REQUIRED
    with pytest.raises(CoreServiceError) as stopped:
        service.inspect_core_service(
            service_root=root,
            process_controller=controller,
        )
    assert stopped.value.code is CoreServiceErrorCode.STATUS_INVALID
    with HostServiceRoot(root, create=False) as pinned:
        floor = pinned.read_json("service.json")
    assert floor == {
        "bundle_sha256": DAEMON_A.bundle_sha256,
        "canonical_manifest_sha256": DAEMON_A.canonical_manifest_sha256,
        "lifecycle_compatibility": 3,
        "release_identity": RELEASE_A.digest,
        "schema_version": 3,
        "state": "stopped",
    }


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
                "schema_version": 2,
                "phase": "spawn_claimed",
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


def test_claimed_onefile_pending_recovers_application_and_launcher(
    tmp_path: Path,
    service_fakes: tuple[FakeController, list[FakeChild]],
) -> None:
    controller, children = service_fakes
    root = _root(tmp_path)
    lock = tmp_path / "framework-lock.json"
    lock.write_text("{}", encoding="ascii")
    launcher = ProcessIdentity(
        pid=778,
        boot_id=controller.boot_id,
        start_time_ticks=56,
    )
    application = ProcessIdentity(
        pid=779,
        boot_id=controller.boot_id,
        start_time_ticks=57,
    )
    controller.current[launcher.pid] = launcher
    controller.current[application.pid] = application
    controller.lock_holders = (launcher,)
    with HostServiceRoot(root) as pinned:
        spawn_lock_fd = pinned.open_lock("spawn.lock")
        metadata = os.fstat(spawn_lock_fd)
        os.close(spawn_lock_fd)
        pinned.atomic_write_json(
            "pending.json",
            {
                "schema_version": 3,
                "phase": "spawn_claimed",
                "release_identity": RELEASE_A.digest,
                "pid": application.pid,
                "boot_id": application.boot_id,
                "start_time_ticks": application.start_time_ticks,
                "port": 8765,
                "generation": "8" * 32,
                "spawn_lock_device": metadata.st_dev,
                "spawn_lock_inode": metadata.st_ino,
            },
            replace=False,
        )

    attachment = ensure_core_service(
        service_root=root,
        framework_lock=lock,
        source_commit=SOURCE_COMMIT,
        process_controller=controller,
    )

    assert attachment.attached is False
    assert controller.terminated[:2] == [application, launcher]
    assert len(children) == 1
    assert not (root / "pending.json").exists()


def test_spawn_intent_process_probe_recovers_child_before_pid_claim(
    tmp_path: Path,
    service_fakes: tuple[FakeController, list[FakeChild]],
) -> None:
    controller, children = service_fakes
    root = _root(tmp_path)
    lock = tmp_path / "framework-lock.json"
    lock.write_text("{}", encoding="ascii")
    holder = ProcessIdentity(
        pid=778,
        boot_id=controller.boot_id,
        start_time_ticks=56,
    )
    controller.current[holder.pid] = holder
    controller.lock_holders = (holder,)
    with HostServiceRoot(root) as pinned:
        spawn_lock_fd = pinned.open_lock("spawn.lock")
        metadata = os.fstat(spawn_lock_fd)
        os.close(spawn_lock_fd)
        pinned.atomic_write_json(
            "pending.json",
            {
                "schema_version": 2,
                "phase": "spawn_intent",
                "release_identity": RELEASE_A.digest,
                "port": 8765,
                "generation": "8" * 32,
                "spawn_lock_device": metadata.st_dev,
                "spawn_lock_inode": metadata.st_ino,
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
    assert controller.terminated == [holder]
    assert len(children) == 1
    assert not (root / "pending.json").exists()


def test_real_sigkill_after_spawn_converges_via_lock_inode_probe(tmp_path: Path) -> None:
    if sys.platform != "linux":
        pytest.skip("Linux /proc process probe is unavailable")
    root = _root(tmp_path)
    child_pid_path = tmp_path / "child.pid"
    script = """
import fcntl
import os
from pathlib import Path
import signal
import subprocess
import sys

from openevo.backend.runtime_identity import HostServiceRoot

root_path = Path(sys.argv[1])
child_pid_path = Path(sys.argv[2])
with HostServiceRoot(root_path) as root:
    lock_fd = root.open_lock("spawn.lock")
    fcntl.flock(lock_fd, fcntl.LOCK_EX)
    metadata = os.fstat(lock_fd)
    root.atomic_write_json(
        "pending.json",
        {
            "schema_version": 2,
            "phase": "spawn_intent",
            "release_identity": "a" * 64,
            "port": 8765,
            "generation": "8" * 32,
            "spawn_lock_device": metadata.st_dev,
            "spawn_lock_inode": metadata.st_ino,
        },
        replace=False,
    )
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        close_fds=True,
        pass_fds=(lock_fd,),
    )
    child_pid_path.write_text(str(child.pid), encoding="ascii")
    os.kill(os.getpid(), signal.SIGKILL)
"""
    supervisor = subprocess.Popen(
        [sys.executable, "-c", script, str(root), str(child_pid_path)],
        env={**os.environ, "PYTHONPATH": str(Path(__file__).parents[2] / "src")},
    )
    child_pid: int | None = None
    try:
        assert supervisor.wait(timeout=10) == -service.signal.SIGKILL
        deadline = time.monotonic() + 5
        while not child_pid_path.exists():
            assert time.monotonic() < deadline
            time.sleep(0.01)
        child_pid = int(child_pid_path.read_text(encoding="ascii"))
        controller = service.LinuxProcessController()
        child_identity = controller.capture(child_pid)
        assert controller.is_alive(child_identity)
        with HostServiceRoot(root) as pinned:
            lifecycle_lock_fd = pinned.open_lock("lifecycle.lock")
            try:
                service._flock_until(lifecycle_lock_fd, time.monotonic() + 5)
                service._recover_pending(
                    pinned,
                    controller=controller,
                    deadline=time.monotonic() + 5,
                )
            finally:
                os.close(lifecycle_lock_fd)
        assert controller.is_alive(child_identity) is False
        assert not (root / "pending.json").exists()
    finally:
        if supervisor.poll() is None:
            supervisor.kill()
            supervisor.wait(timeout=5)
        if child_pid is not None:
            try:
                os.kill(child_pid, service.signal.SIGKILL)
            except ProcessLookupError:
                pass


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
    assert restarted.bearer_token != first.bearer_token
    assert controller.terminated == []
    assert len(children) == 2


def test_stop_terminates_exact_service_and_clears_publication(
    tmp_path: Path,
    service_fakes: tuple[FakeController, list[FakeChild]],
) -> None:
    controller, _children = service_fakes
    root = _root(tmp_path)
    lock = tmp_path / "framework-lock.json"
    lock.write_text("{}", encoding="ascii")
    ensure_core_service(
        service_root=root,
        framework_lock=lock,
        source_commit=SOURCE_COMMIT,
        process_controller=controller,
    )

    service.stop_core_service(
        service_root=root,
        process_controller=controller,
    )

    assert len(controller.terminated) == 1
    assert not (root / "service.json").exists()
    assert not (root / "ready.json").exists()


def test_conditional_stop_preserves_replacement_generation(
    tmp_path: Path,
    service_fakes: tuple[FakeController, list[FakeChild]],
) -> None:
    controller, _children = service_fakes
    root = _root(tmp_path)
    lock = tmp_path / "framework-lock.json"
    lock.write_text("{}", encoding="ascii")
    original = ensure_core_service(
        service_root=root,
        framework_lock=lock,
        source_commit=SOURCE_COMMIT,
        process_controller=controller,
    )
    service.stop_core_service(service_root=root, process_controller=controller)
    replacement = ensure_core_service(
        service_root=root,
        framework_lock=lock,
        source_commit=SOURCE_COMMIT,
        process_controller=controller,
    )

    stopped = service.stop_core_service_if_generation(
        service_root=root,
        expected_generation=original.generation,
        expected_release_identity=original.release_identity,
        process_controller=controller,
    )

    assert stopped is False
    assert replacement.generation != original.generation
    assert len(controller.terminated) == 1
    with HostServiceRoot(root) as pinned:
        ledger = pinned.read_json("service.json")
    assert ledger["generation"] == replacement.generation
    assert ledger["release_identity"] == replacement.release_identity


def test_conditional_stop_terminates_matching_generation(
    tmp_path: Path,
    service_fakes: tuple[FakeController, list[FakeChild]],
) -> None:
    controller, _children = service_fakes
    root = _root(tmp_path)
    lock = tmp_path / "framework-lock.json"
    lock.write_text("{}", encoding="ascii")
    attachment = ensure_core_service(
        service_root=root,
        framework_lock=lock,
        source_commit=SOURCE_COMMIT,
        process_controller=controller,
    )

    stopped = service.stop_core_service_if_generation(
        service_root=root,
        expected_generation=attachment.generation,
        expected_release_identity=attachment.release_identity,
        process_controller=controller,
    )

    assert stopped is True
    assert len(controller.terminated) == 1
    assert not (root / "service.json").exists()
    assert not (root / "ready.json").exists()


def test_conditional_stop_preserves_generation_on_release_identity_mismatch(
    tmp_path: Path,
    service_fakes: tuple[FakeController, list[FakeChild]],
) -> None:
    controller, _children = service_fakes
    root = _root(tmp_path)
    lock = tmp_path / "framework-lock.json"
    lock.write_text("{}", encoding="ascii")
    attachment = ensure_core_service(
        service_root=root,
        framework_lock=lock,
        source_commit=SOURCE_COMMIT,
        process_controller=controller,
    )

    stopped = service.stop_core_service_if_generation(
        service_root=root,
        expected_generation=attachment.generation,
        expected_release_identity=RELEASE_B.digest,
        process_controller=controller,
    )

    assert stopped is False
    assert controller.terminated == []
    with HostServiceRoot(root) as pinned:
        ledger = pinned.read_json("service.json")
    assert ledger["generation"] == attachment.generation
    assert ledger["release_identity"] == attachment.release_identity


def test_pidfd_esrch_is_already_stopped_and_stop_cleans_publication(
    tmp_path: Path,
    service_fakes: tuple[FakeController, list[FakeChild]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_controller, _children = service_fakes
    root = _root(tmp_path)
    lock = tmp_path / "framework-lock.json"
    lock.write_text("{}", encoding="ascii")
    ensure_core_service(
        service_root=root,
        framework_lock=lock,
        source_commit=SOURCE_COMMIT,
        process_controller=fake_controller,
    )
    with HostServiceRoot(root) as pinned:
        ledger = pinned.read_json("service.json")
    identity = ProcessIdentity(
        pid=ledger["pid"],
        boot_id=ledger["boot_id"],
        start_time_ticks=ledger["start_time_ticks"],
    )
    controller = service.LinuxProcessController.__new__(service.LinuxProcessController)
    process_alive = True
    monkeypatch.setattr(controller, "is_alive", lambda _identity: process_alive)
    monkeypatch.setattr(controller, "capture", lambda _pid: identity)
    monkeypatch.setattr(
        service.os,
        "pidfd_open",
        lambda _pid, _flags: os.open("/dev/null", os.O_RDONLY),
        raising=False,
    )

    def already_stopped(*_args: object) -> None:
        nonlocal process_alive
        process_alive = False
        raise OSError(errno.ESRCH, "gone")

    monkeypatch.setattr(
        service.signal,
        "pidfd_send_signal",
        already_stopped,
        raising=False,
    )

    service.stop_core_service(service_root=root, process_controller=controller)

    assert not (root / "service.json").exists()
    assert not (root / "ready.json").exists()
    assert not (root / "pending.json").exists()


def test_pidfd_helpers_fall_back_to_linux_syscalls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened: list[tuple[int, int]] = []
    signalled: list[tuple[int, int, int]] = []
    monkeypatch.delattr(service.os, "pidfd_open", raising=False)
    monkeypatch.delattr(service.signal, "pidfd_send_signal", raising=False)
    monkeypatch.setattr(
        service,
        "_pidfd_open_via_syscall",
        lambda pid, flags: opened.append((pid, flags)) or 91,
    )
    monkeypatch.setattr(
        service,
        "_pidfd_send_signal_via_syscall",
        lambda pid_fd, sig, flags: signalled.append((pid_fd, sig, flags)),
    )

    assert service._pidfd_open(123, 0) == 91
    service._pidfd_send_signal(91, service.signal.SIGTERM, 0)

    assert opened == [(123, 0)]
    assert signalled == [(91, service.signal.SIGTERM, 0)]


@pytest.mark.skipif(sys.platform != "linux", reason="Linux pidfd ABI only")
def test_pidfd_syscall_fallback_works_without_cpython_wrappers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delattr(service.os, "pidfd_open", raising=False)
    monkeypatch.delattr(service.signal, "pidfd_send_signal", raising=False)

    pid_fd = service._pidfd_open(os.getpid(), 0)
    try:
        service._pidfd_send_signal(pid_fd, 0, 0)
    finally:
        os.close(pid_fd)


def test_process_controller_fails_closed_when_pidfd_probe_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        service,
        "_pidfd_open",
        lambda _pid, _flags: (_ for _ in ()).throw(OSError(errno.ENOSYS, "missing")),
    )

    with pytest.raises(CoreServiceError, match="requires Linux pidfd support"):
        service.LinuxProcessController()


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
