from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time

import pytest

from openevo.backend import service_supervisor as supervisor_module
from openevo.gateway import session_files as session_files_module
from openevo.backend.contracts.v1.models import LogEntryV1, ServiceSummaryV1
from openevo.backend.service_supervisor import (
    BoundedProbeCommandRunner,
    CoreServiceSupervisor,
    HealthCheckResult,
    LocalManagedScienceRuntimeProbe,
    ManagedScienceRuntimeReadiness,
    ManagedScienceRuntimeRequest,
    ProcessIdentity,
    ProbeCommandResult,
    RealSubprocessBackend,
    ServiceComponent,
    ServiceExecutionMode,
    ServiceHealthProbe,
    ServiceLaunchMode,
    ServiceProcessSpec,
    ServiceReleaseIdentity,
    ServiceRunReadinessCode,
    ServiceStatus,
    SupervisorBusyError,
    SupervisorStateError,
)
from openevo.config import TopologyConfig
from openevo.internal_auth import InternalServiceIdentity
from openevo.gateway.session_files import (
    HeldCodexCredentialAuthority,
    PreparedCodexCredentialSnapshot,
    SessionFileSecurityError,
)
from openevo.runtime.managed import MANAGED_RUNTIME_RELEASES
from tests.framework_testkit import verified_builtin_registry


INSTALL_DIGEST = "a" * 64
REGISTRY_DIGEST = "b" * 64


class FakeProcessBackend:
    def __init__(self) -> None:
        self.spawned: list[ServiceProcessSpec] = []
        self.identities: dict[str, ProcessIdentity] = {}
        self.alive: dict[str, bool] = {}
        self.returncodes: dict[str, int] = {}
        self.callbacks = {}
        self.terminated: list[str] = []
        self.killed: list[str] = []
        self.fail_component: ServiceComponent | None = None
        self.spawn_gate: threading.Event | None = None
        self.spawn_entered = threading.Event()
        self._condition = threading.Condition()

    def spawn(self, spec, on_output, on_exit) -> ProcessIdentity:
        if self.fail_component is spec.component:
            raise OSError("controlled spawn failure")
        if self.spawn_gate is not None:
            self.spawn_entered.set()
            assert self.spawn_gate.wait(timeout=2)
        identity = ProcessIdentity(
            pid=10_000 + len(self.spawned),
            birth_token=f"{len(self.spawned) + 1:064x}",
            session_id=10_000 + len(self.spawned),
            process_group_id=10_000 + len(self.spawned),
            ownership_digest=spec.identity_digest,
        )
        with self._condition:
            self.spawned.append(spec)
            self.identities[spec.service_id] = identity
            self.alive[spec.service_id] = True
            self.callbacks.setdefault(spec.service_id, []).append((identity, on_output, on_exit))
            self._condition.notify_all()
        return identity

    def is_alive(self, identity: ProcessIdentity) -> bool:
        for service_id, candidate in self.identities.items():
            if candidate == identity:
                return self.alive.get(service_id, False)
        return False

    def terminate(self, identity: ProcessIdentity) -> None:
        service_id = self._service_id(identity)
        self.terminated.append(service_id)
        self.alive[service_id] = False

    def kill(self, identity: ProcessIdentity) -> None:
        service_id = self._service_id(identity)
        self.killed.append(service_id)
        self.alive[service_id] = False

    def wait(self, identity: ProcessIdentity, timeout: float | None) -> int | None:
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._condition:
            while self.is_alive(identity):
                if deadline is None:
                    self._condition.wait()
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._condition.wait(remaining)
        return self.returncodes.get(self._service_id(identity), 0)

    def recover_stale_group(self, identity: ProcessIdentity, deadline: float) -> bool:
        del identity
        return time.monotonic() < deadline

    def emit(self, service_id: str, payload: bytes) -> None:
        identity, callback, _ = self.callbacks[service_id][-1]
        callback(identity, payload)

    def crash(self, service_id: str, returncode: int = 17) -> None:
        with self._condition:
            self.alive[service_id] = False
            self.returncodes[service_id] = returncode
            identity, _, callback = self.callbacks[service_id][-1]
            self._condition.notify_all()
        callback(identity, returncode)

    def _service_id(self, identity: ProcessIdentity) -> str:
        return next(key for key, value in self.identities.items() if value == identity)


class FakeHealthChecker:
    def __init__(self) -> None:
        self.failed: set[str] = set()
        self.checked: list[str] = []
        self.block_service: str | None = None
        self.block_entered = threading.Event()
        self.cancellations: list[threading.Event | None] = []

    def wait_ready(
        self,
        spec,
        identity,
        process_backend,
        deadline,
        cancellation=None,
    ) -> HealthCheckResult:
        self.checked.append(spec.service_id)
        self.cancellations.append(cancellation)
        if self.block_service == spec.service_id:
            self.block_entered.set()
            while time.monotonic() < deadline:
                if cancellation is not None and cancellation.is_set():
                    return HealthCheckResult(ready=False, message="cancelled")
                time.sleep(0.002)
            return HealthCheckResult(ready=False, message="readiness deadline exceeded")
        if spec.service_id in self.failed:
            return HealthCheckResult(ready=False, message="controlled health failure")
        return HealthCheckResult(
            ready=process_backend.is_alive(identity),
            message="healthy",
        )


class FakePortProbe:
    def __init__(self, conflicts: set[int] | None = None) -> None:
        self.conflicts = conflicts or set()
        self.checked = 0

    def reserve(self, host: str) -> socket.socket:
        assert host == "127.0.0.1"
        self.checked += 1
        if self.conflicts:
            raise OSError("controlled listener reservation failure")
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind((host, 0))
        listener.listen()
        return listener


class FakeManagedScienceRuntimeProbe:
    def __init__(self, *, ready: bool = True) -> None:
        self.ready = ready
        self.requests: list[ManagedScienceRuntimeRequest] = []
        self.auth_path: Path | None = None

    def configure_auth_path(self, path: Path) -> None:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_text('{"tokens":{"access_token":"test-secret"}}', encoding="utf-8")
        path.chmod(0o600)
        self.auth_path = path

    def verify(self, request, deadline, cancellation=None) -> ManagedScienceRuntimeReadiness:
        assert time.monotonic() < deadline
        assert cancellation is None or not cancellation.is_set()
        self.requests.append(request)
        authority = (
            HeldCodexCredentialAuthority.open(self.auth_path)
            if self.ready and self.auth_path is not None
            else None
        )
        snapshot = authority.prepare_snapshot() if authority is not None else None
        if authority is not None:
            authority.close()
        return ManagedScienceRuntimeReadiness(
            ready=self.ready,
            code=(
                ServiceRunReadinessCode.READY
                if self.ready
                else ServiceRunReadinessCode.RUNTIME_IMAGE_UNAVAILABLE
            ),
            identity_digest="f" * 64 if self.ready else None,
            runtime_image_immutable_reference=(
                MANAGED_RUNTIME_RELEASES["managed_science"].trusted_digest
                if self.ready
                else None
            ),
            message=(
                "Managed Science runtime bootstrap is verified."
                if self.ready
                else "Managed Science runtime image is not prepared."
            ),
            credential_authority=snapshot,
        )


class BlockingManagedScienceRuntimeProbe(FakeManagedScienceRuntimeProbe):
    def __init__(self) -> None:
        super().__init__()
        self.entered = threading.Event()

    def verify(self, request, deadline, cancellation=None) -> ManagedScienceRuntimeReadiness:
        self.entered.set()
        while time.monotonic() < deadline:
            if cancellation is not None and cancellation.wait(0.005):
                return ManagedScienceRuntimeReadiness(
                    ready=False,
                    code=ServiceRunReadinessCode.SERVICE_GROUP_UNAVAILABLE,
                    identity_digest=None,
                    runtime_image_immutable_reference=None,
                    message="Managed Science runtime probe was cancelled.",
                )
        raise AssertionError("cancellation did not interrupt the runtime probe")


class FakeProbeCommandRunner:
    def __init__(
        self,
        image_id: str = MANAGED_RUNTIME_RELEASES["managed_science"].trusted_digest.removeprefix(
            "sha256:"
        ),
        *,
        results: dict[tuple[str, ...], ProbeCommandResult] | None = None,
    ) -> None:
        self.image_id = image_id
        self.results = results or {}
        self.calls: list[tuple[str, ...]] = []
        self.environments: list[dict[str, str]] = []

    def hold_executable(self, name: str):
        assert name == "codex"
        runner = self

        class FakeExecutable:
            identity_digest = "e" * 64

            def run(self, argv, deadline, cancellation=None, *, env=None):
                return runner.run(
                    ("codex", *argv),
                    deadline,
                    cancellation,
                    env=env,
                )

            def close(self) -> None:
                return None

        return FakeExecutable()

    def run(
        self,
        argv,
        deadline,
        cancellation=None,
        *,
        env=None,
        pass_fds=(),
    ) -> ProbeCommandResult:
        assert time.monotonic() < deadline
        assert cancellation is None or not cancellation.is_set()
        assert pass_fds == ()
        self.calls.append(argv)
        self.environments.append(dict(env or {}))
        if argv in self.results:
            return self.results[argv]
        if argv == ("codex", "--version"):
            return ProbeCommandResult(0, b"codex-cli 1.2.3\n", b"")
        if argv == ("codex", "login", "status"):
            return ProbeCommandResult(0, b"", b"Logged in using ChatGPT\n")
        if argv == ("docker", "--version"):
            return ProbeCommandResult(0, b"Docker version 27.0.1\n", b"")
        payload = [
            {
                "Id": f"sha256:{self.image_id}",
                "RepoDigests": [],
                "Config": {"Labels": {"io.openevo.managed-runtime": "true"}},
            }
        ]
        return ProbeCommandResult(0, json.dumps(payload).encode(), b"")


@pytest.fixture
def framework_lock(tmp_path: Path) -> Path:
    path = tmp_path / "framework-lock.json"
    path.write_text('{"schema_version":1}\n', encoding="utf-8")
    path.chmod(0o600)
    return path


def _supervisor(
    tmp_path: Path,
    framework_lock: Path,
    *,
    backend: FakeProcessBackend | None = None,
    health: FakeHealthChecker | None = None,
    ports: FakePortProbe | None = None,
    startup_timeout: float = 1,
    stop_timeout: float = 0.2,
    max_log_entries: int = 100,
    max_log_bytes: int = 32_768,
    runtime_probe: FakeManagedScienceRuntimeProbe | None = None,
    max_restart_operations: int = 256,
    run_admission_url: str | None = ("http://127.0.0.1:19000/internal/v1/run-admissions/verify"),
) -> tuple[
    CoreServiceSupervisor,
    FakeProcessBackend,
    FakeHealthChecker,
    FakeManagedScienceRuntimeProbe,
]:
    backend = backend or FakeProcessBackend()
    health = health or FakeHealthChecker()
    runtime_probe = runtime_probe or FakeManagedScienceRuntimeProbe()
    runtime_probe.configure_auth_path(tmp_path / "codex-home" / ".codex" / "auth.json")
    supervisor = CoreServiceSupervisor(
        launch_mode=ServiceLaunchMode.DEVELOPMENT_TEST,
        service_root=tmp_path / "core-services",
        framework_lock=framework_lock,
        release_identity=ServiceReleaseIdentity(
            install_digest=INSTALL_DIGEST,
            registry_digest=REGISTRY_DIGEST,
        ),
        process_backend=backend,
        health_checker=health,
        port_probe=ports or FakePortProbe(),
        managed_runtime_probe=runtime_probe,
        run_admission_url=run_admission_url,
        startup_timeout=startup_timeout,
        stop_timeout=stop_timeout,
        max_log_entries=max_log_entries,
        max_log_bytes=max_log_bytes,
        max_restart_operations=max_restart_operations,
    )
    return supervisor, backend, health, runtime_probe


def _ensure_subscription(
    supervisor: CoreServiceSupervisor,
    *,
    codex_model: str = "gpt-5.1-codex-mini",
):
    return supervisor.ensure(
        ServiceExecutionMode.CODEX_SUBSCRIPTION_TRANSCRIPT,
        codex_model=codex_model,
        runtime_image="openevo/science-runtime:0.1.0",
    )


def _ensure_subscription_binding(
    supervisor: CoreServiceSupervisor,
    *,
    codex_model: str = "gpt-5.1-codex-mini",
):
    return supervisor.ensure_run_binding(
        ServiceExecutionMode.CODEX_SUBSCRIPTION_TRANSCRIPT,
        codex_model=codex_model,
        runtime_image="openevo/science-runtime:0.1.0",
    )


def test_subscription_plan_is_deterministic_and_ready_requires_health_and_identity(
    tmp_path: Path,
    framework_lock: Path,
) -> None:
    supervisor, backend, health, runtime_probe = _supervisor(tmp_path, framework_lock)
    try:
        snapshot = _ensure_subscription(supervisor)
        assert snapshot.services_available is True
        assert snapshot.run_ready is True
        assert snapshot.run_readiness_code is ServiceRunReadinessCode.READY
        assert snapshot.runtime_image == "openevo/science-runtime:0.1.0"
        assert snapshot.runtime_image_immutable_reference == (
            MANAGED_RUNTIME_RELEASES["managed_science"].trusted_digest
        )
        assert [service.id for service in snapshot.services] == [
            "evolution-backend",
            "rollout",
            "gateway",
            "evolution-worker",
        ]
        assert [spec.component for spec in backend.spawned] == [
            ServiceComponent.EVOLUTION_BACKEND,
            ServiceComponent.ROLLOUT,
            ServiceComponent.GATEWAY,
            ServiceComponent.EVOLUTION_WORKER,
        ]
        assert health.checked == [service.id for service in snapshot.services]
        assert all(service.status is ServiceStatus.RUNNING for service in snapshot.services)
        assert all(service.pid is not None for service in snapshot.services)
        assert all(service.identity_digest for service in snapshot.services)
        topology = json.loads((tmp_path / "core-services" / "topology.json").read_text())
        parsed_topology = TopologyConfig.load(tmp_path / "core-services" / "topology.json")
        assert topology["evolution"] == {
            "enabled": True,
            "backend_url": (f"http://127.0.0.1:{snapshot.service('evolution-backend').port}"),
            "context": {
                "target_dir": "/openevo/session/evolution",
                "timeout_seconds": 10,
                "fail_open": False,
            },
            "event_export": {
                "enabled": True,
                "timeout_seconds": 10,
                "fail_open": False,
            },
        }
        assert topology["gateway"]["nodes"][0]["port"] > 0
        assert topology["gateway"]["nodes"][0]["model_served"] == "gpt-5.1-codex-mini"
        assert topology["rollout"]["port"] > 0
        assert parsed_topology.gateway.nodes[0].default_runtime is None
        assert parsed_topology.evolution is not None
        assert parsed_topology.evolution.enabled is True
        assert parsed_topology.evolution.context.fail_open is False
        assert parsed_topology.evolution.event_export.fail_open is False
        assert runtime_probe.requests == [
            ManagedScienceRuntimeRequest(
                runtime_image="openevo/science-runtime:0.1.0",
                codex_model="gpt-5.1-codex-mini",
            )
        ]
        assert all("codex" not in part.lower() for spec in backend.spawned for part in spec.argv)
        assert all(spec.argv[1] == "-I" for spec in backend.spawned)
        assert all("PYTHONPATH" not in spec.env for spec in backend.spawned)
        assert all("PYTHONHOME" not in spec.env for spec in backend.spawned)
        assert all(
            spec.env["OPENEVO_CORE_RUN_ADMISSION_URL"]
            == "http://127.0.0.1:19000/internal/v1/run-admissions/verify"
            for spec in backend.spawned
        )
        binding = supervisor.run_binding()
        assert binding.execution_mode is ServiceExecutionMode.CODEX_SUBSCRIPTION_TRANSCRIPT
        assert binding.runtime_image == "openevo/science-runtime:0.1.0"
        assert binding.runtime_image_immutable_reference == (
            MANAGED_RUNTIME_RELEASES["managed_science"].trusted_digest
        )
        assert binding.runtime_identity_digest == snapshot.runtime_identity_digest
        assert binding.generation_digest == snapshot.generation_digest
        assert binding.registry_digest == REGISTRY_DIGEST
        assert binding.rollout_url.startswith("http://127.0.0.1:")
        assert binding.evolution_backend_url.startswith("http://127.0.0.1:")
        assert binding.gateway_url.startswith("http://127.0.0.1:")
        assert supervisor.authenticates_run_service(binding.request_headers()) is True
        assert "credential" not in repr(binding).lower()
        child_cwd = tmp_path / "core-services" / "child-cwd"
        assert all(spec.cwd == os.fspath(child_cwd) for spec in backend.spawned)
        assert child_cwd.stat().st_mode & 0o777 == 0o700
        credential = backend.spawned[0].internal_identity
        assert credential is not None
        assert credential.credential not in repr(backend.spawned[0])
        assert (
            credential.credential not in (tmp_path / "core-services" / "ledger.json").read_text()
        )
        assert (
            credential.credential not in (tmp_path / "core-services" / "topology.json").read_text()
        )
        assert (
            len({service.port for service in snapshot.services if service.port is not None}) == 3
        )
    finally:
        supervisor.close()


def test_live_service_group_without_run_admission_owner_is_not_run_ready(
    tmp_path: Path,
    framework_lock: Path,
) -> None:
    supervisor, _, _, _ = _supervisor(
        tmp_path,
        framework_lock,
        run_admission_url=None,
    )
    try:
        snapshot = _ensure_subscription(supervisor)

        assert snapshot.services_available is True
        assert snapshot.run_ready is False
        assert snapshot.run_readiness_code is ServiceRunReadinessCode.RUN_ADMISSION_UNAVAILABLE
        with pytest.raises(SupervisorStateError, match="not ready for a run"):
            supervisor.run_binding()
    finally:
        supervisor.close()


def test_spawn_success_without_health_is_rolled_back(
    tmp_path: Path,
    framework_lock: Path,
) -> None:
    health = FakeHealthChecker()
    health.failed.add("gateway")
    supervisor, backend, _, _ = _supervisor(tmp_path, framework_lock, health=health)
    try:
        snapshot = _ensure_subscription(supervisor)
        assert snapshot.services_available is False
        assert snapshot.run_ready is False
        assert snapshot.run_readiness_code is ServiceRunReadinessCode.SERVICE_GROUP_UNAVAILABLE
        assert snapshot.service("gateway").status is ServiceStatus.FAILED
        assert backend.terminated == ["gateway", "rollout", "evolution-backend"]
        assert snapshot.service("evolution-worker").status is ServiceStatus.STOPPED
    finally:
        supervisor.close()


def test_total_startup_deadline_rolls_back_partial_group(
    tmp_path: Path,
    framework_lock: Path,
) -> None:
    health = FakeHealthChecker()
    health.block_service = "rollout"
    supervisor, backend, _, _ = _supervisor(
        tmp_path,
        framework_lock,
        health=health,
        startup_timeout=0.03,
    )
    try:
        started = time.monotonic()
        snapshot = _ensure_subscription(supervisor)
        assert time.monotonic() - started < 0.3
        assert snapshot.services_available is False
        assert backend.terminated == ["rollout", "evolution-backend"]
        assert snapshot.service("rollout").error_code == "service_readiness_timeout"
    finally:
        supervisor.close()


def test_listener_reservation_failure_prevents_any_spawn(
    tmp_path: Path,
    framework_lock: Path,
) -> None:
    ports = FakePortProbe({8080})
    supervisor, backend, _, _ = _supervisor(tmp_path, framework_lock, ports=ports)
    try:
        with pytest.raises(SupervisorStateError, match="listener reservation"):
            _ensure_subscription(supervisor)
        assert backend.spawned == []
    finally:
        supervisor.close()


def test_partial_spawn_failure_rolls_back_in_reverse_order(
    tmp_path: Path,
    framework_lock: Path,
) -> None:
    backend = FakeProcessBackend()
    backend.fail_component = ServiceComponent.GATEWAY
    supervisor, backend, _, _ = _supervisor(tmp_path, framework_lock, backend=backend)
    try:
        snapshot = _ensure_subscription(supervisor)
        assert snapshot.services_available is False
        assert backend.terminated == ["rollout", "evolution-backend"]
        assert snapshot.service("gateway").error_code == "service_spawn_failed"
    finally:
        supervisor.close()


def test_concurrent_ensure_is_idempotent(
    tmp_path: Path,
    framework_lock: Path,
) -> None:
    backend = FakeProcessBackend()
    backend.spawn_gate = threading.Event()
    supervisor, backend, _, _ = _supervisor(tmp_path, framework_lock, backend=backend)
    snapshots = []

    def ensure() -> None:
        snapshots.append(_ensure_subscription(supervisor))

    first = threading.Thread(target=ensure)
    second = threading.Thread(target=ensure)
    try:
        first.start()
        assert backend.spawn_entered.wait(timeout=1)
        second.start()
        backend.spawn_gate.set()
        first.join(timeout=2)
        second.join(timeout=2)
        assert len(backend.spawned) == 4
        assert len(snapshots) == 2
        assert snapshots[0].generation_digest == snapshots[1].generation_digest
    finally:
        backend.spawn_gate.set()
        supervisor.close()


def test_atomic_run_binding_lease_blocks_another_model_generation_until_release(
    tmp_path: Path,
    framework_lock: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supervisor, backend, _, _ = _supervisor(tmp_path, framework_lock)
    binding_entered = threading.Event()
    binding_allowed = threading.Event()
    replacement_started = threading.Event()
    original_run_binding = supervisor._run_binding_locked
    first_results = []
    replacement_results = []
    errors: list[BaseException] = []
    replacement_errors: list[SupervisorStateError] = []

    def blocking_run_binding():
        binding_entered.set()
        assert binding_allowed.wait(5)
        return original_run_binding()

    monkeypatch.setattr(supervisor, "_run_binding_locked", blocking_run_binding)

    def first() -> None:
        try:
            first_results.append(_ensure_subscription_binding(supervisor))
        except BaseException as exc:
            errors.append(exc)

    def replace() -> None:
        replacement_started.set()
        try:
            replacement_results.append(
                _ensure_subscription(supervisor, codex_model="gpt-5.2-codex")
            )
        except SupervisorStateError as exc:
            replacement_errors.append(exc)
        except BaseException as exc:
            errors.append(exc)

    first_thread = threading.Thread(target=first)
    replacement_thread = threading.Thread(target=replace)
    try:
        first_thread.start()
        assert binding_entered.wait(5)
        replacement_thread.start()
        assert replacement_started.wait(5)
        time.sleep(0.05)
        assert replacement_thread.is_alive()
        assert replacement_results == []

        binding_allowed.set()
        first_thread.join(5)
        replacement_thread.join(5)

        assert not first_thread.is_alive()
        assert not replacement_thread.is_alive()
        assert errors == []
        assert len(first_results) == 1
        snapshot, lease = first_results[0]
        assert lease is not None
        binding = lease.binding
        assert binding.execution_mode is snapshot.execution_mode
        assert binding.runtime_image == snapshot.runtime_image
        assert binding.runtime_identity_digest == snapshot.runtime_identity_digest
        assert binding.generation_digest == snapshot.generation_digest
        assert replacement_results == []
        assert len(replacement_errors) == 1
        assert "leased to an active run" in str(replacement_errors[0])
        assert len(backend.spawned) == 4
        assert backend.terminated == []
        replay = _ensure_subscription(supervisor)
        assert replay.generation_digest == snapshot.generation_digest

        lease.close()
        replacement = _ensure_subscription(supervisor, codex_model="gpt-5.2-codex")
        assert replacement.generation_digest != snapshot.generation_digest
        assert len(backend.spawned) == 8
    finally:
        binding_allowed.set()
        first_thread.join(5)
        replacement_thread.join(5)
        supervisor.close()


def test_auth_replacement_after_snapshot_does_not_revoke_active_run_authority(
    tmp_path: Path,
    framework_lock: Path,
) -> None:
    supervisor, _, _, runtime_probe = _supervisor(tmp_path, framework_lock)
    lease = None
    try:
        snapshot, lease = _ensure_subscription_binding(supervisor)
        assert snapshot.run_ready
        assert lease is not None
        assert runtime_probe.auth_path is not None
        replacement = runtime_probe.auth_path.with_name("auth.replacement")
        replacement.write_text(
            '{"tokens":{"access_token":"replacement-secret"}}',
            encoding="utf-8",
        )
        replacement.chmod(0o600)
        os.replace(replacement, runtime_probe.auth_path)

        assert supervisor.authenticates_run_service(lease.binding.request_headers()) is True
        assert supervisor.run_binding() == lease.binding
    finally:
        if lease is not None:
            lease.close()
        supervisor.close()


def test_child_exit_is_observed_and_restart_is_idempotent_by_operation(
    tmp_path: Path,
    framework_lock: Path,
) -> None:
    supervisor, backend, _, _ = _supervisor(tmp_path, framework_lock)
    try:
        assert _ensure_subscription(supervisor).services_available
        backend.crash("gateway", 23)
        deadline = time.monotonic() + 1
        while supervisor.get("gateway").status is not ServiceStatus.FAILED:
            assert time.monotonic() < deadline
            time.sleep(0.005)
        assert supervisor.get("gateway").error_code == "service_process_exited"

        results = []

        def restart() -> None:
            results.append(supervisor.restart("gateway", operation_id="restart-gateway-1"))

        threads = [threading.Thread(target=restart) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=2)
        assert len([spec for spec in backend.spawned if spec.service_id == "gateway"]) == 2
        assert all(result.status is ServiceStatus.RUNNING for result in results)
    finally:
        supervisor.close()


def test_delayed_exit_callback_cannot_clear_replacement(
    tmp_path: Path,
    framework_lock: Path,
) -> None:
    supervisor, backend, _, runtime_probe = _supervisor(tmp_path, framework_lock)
    try:
        first = _ensure_subscription(supervisor)
        old_identity, _, old_exit = backend.callbacks["gateway"][-1]
        backend.crash("gateway", 23)
        replacement = supervisor.restart("gateway", operation_id="replace-generation")
        assert replacement.status is ServiceStatus.RUNNING
        assert replacement.pid != old_identity.pid
        assert len(runtime_probe.requests) == 2
        assert supervisor._ledger.generation_digest != first.generation_digest

        old_exit(old_identity, 91)

        current = supervisor.get("gateway")
        assert current.status is ServiceStatus.RUNNING
        assert current.pid == replacement.pid
    finally:
        supervisor.close()


def test_log_snapshot_is_bounded_redacted_and_maps_to_frozen_contract(
    tmp_path: Path,
    framework_lock: Path,
) -> None:
    supervisor, backend, _, _ = _supervisor(
        tmp_path,
        framework_lock,
        max_log_entries=3,
        max_log_bytes=180,
    )
    try:
        _ensure_subscription(supervisor)
        internal_identity = backend.spawned[0].internal_identity
        assert internal_identity is not None
        backend.emit("gateway", f"{internal_identity.credential}\n".encode())
        backend.emit("gateway", b"Authorization: Bearer secret-token\n")
        backend.emit("gateway", b"opened /home/alice/private/model.bin\n")
        backend.emit("gateway", b"proxy http://user:pass@example.test/path?q=secret\n")
        backend.emit(
            "gateway",
            b'{"message":"request failed","AWS_SECRET_ACCESS_KEY":"aws-secret",'
            b'"future_secret_field":"unknown-secret"}\n',
        )
        backend.emit("gateway", b"last safe line\n")
        logs = supervisor.logs("gateway", limit=100)
        assert 1 <= len(logs) <= 3
        rendered = "\n".join(entry.message for entry in logs)
        assert "secret-token" not in rendered
        assert internal_identity.credential not in rendered
        assert "/home/alice" not in rendered
        assert "user:pass" not in rendered
        assert "q=secret" not in rendered
        assert "aws-secret" not in rendered
        assert "unknown-secret" not in rendered
        assert all(isinstance(entry.to_contract(), LogEntryV1) for entry in logs)
        assert isinstance(supervisor.get("gateway").to_contract(), ServiceSummaryV1)
        ledger = (tmp_path / "core-services" / "ledger.json").read_text()
        assert "secret-token" not in ledger
        assert "/home/alice" not in ledger
    finally:
        supervisor.close()


def test_streaming_log_redaction_hides_fragmented_credentials_and_flushes_carry(
    tmp_path: Path,
    framework_lock: Path,
) -> None:
    supervisor, backend, _, _ = _supervisor(tmp_path, framework_lock)
    try:
        _ensure_subscription(supervisor)
        internal_identity = backend.spawned[0].internal_identity
        assert internal_identity is not None
        credential = internal_identity.credential
        fragments = (credential[:11], credential[11:37], credential[37:])

        backend.emit("gateway", f"Authorization: Bearer {fragments[0]}".encode())
        for fragment in fragments[1:]:
            backend.emit("gateway", fragment.encode())
        backend.emit("rollout", f"Bearer {credential[:23]}".encode())
        backend.crash("gateway", 19)
        backend.crash("rollout", 20)

        rendered = "\n".join(
            entry.message
            for service_id in ("gateway", "rollout")
            for entry in supervisor.logs(service_id, limit=100)
        )
        ledger = (tmp_path / "core-services" / "ledger.json").read_text()
        assert "<redacted>" in rendered
        assert credential not in rendered
        assert credential not in ledger
        assert all(fragment not in rendered for fragment in fragments)
        assert all(fragment not in ledger for fragment in fragments)
        assert credential[:23] not in rendered
        assert credential[:23] not in ledger
    finally:
        supervisor.close()


@pytest.mark.parametrize(
    ("payload", "forbidden", "preserved"),
    [
        (
            b"Authorization: Bearer auth-secret-value\n",
            b"auth-secret-value",
            b"<redacted>",
        ),
        (
            b"Cookie: session=cookie-secret; csrf=csrf-secret\n",
            b"cookie-secret",
            b"<redacted>",
        ),
        (b"X-API-Key: api-key-secret\n", b"api-key-secret", b"<redacted>"),
        (
            b"request https://user:pass@example.test/path?q=query-secret#fragment\n",
            b"query-secret",
            b"example.test/path",
        ),
        (
            b"db postgresql://db-user:db-pass@db.internal/app?sslmode=query-secret#writer",
            b"db-pass",
            b"db.internal/app",
        ),
        (
            b"cache redis://:redis-pass@[::1]:6379/0?client=query-secret#worker\n",
            b"redis-pass",
            b"[::1]:6379/0",
        ),
        (
            b"snapshot file:///var/lib/openevo/cache?mode=query-secret#worker\n",
            b"query-secret",
            b"file:///var/lib/openevo/cache",
        ),
        (
            b'{"message":"failed","authorization":"json-secret",'
            b'"nested":{"api_key":"nested-secret"}}\n',
            b"json-secret",
            b'"message":"failed"',
        ),
        (b"worker --api-key cli-secret --model gpt-5\n", b"cli-secret", b"--model gpt-5"),
        (b"OPENAI_API_KEY env-secret worker ready\n", b"env-secret", b"worker ready"),
        (
            "状态 ready --password utf8-secret".encode(),
            b"utf8-secret",
            "状态 ready".encode(),
        ),
    ],
)
def test_generic_log_redaction_is_safe_at_every_chunk_boundary(
    payload: bytes,
    forbidden: bytes,
    preserved: bytes,
) -> None:
    credential = "supervisor-credential-secret"
    for split in range(len(payload) + 1):
        redactor = supervisor_module._BoundedLogStreamRedactor(credential)
        rendered = redactor.feed(payload[:split])
        rendered += redactor.feed(payload[split:])
        rendered += redactor.flush()
        assert forbidden not in rendered
        assert b"<redacted>" in rendered
        assert preserved in rendered
        assert redactor.buffered_bytes == 0


@pytest.mark.parametrize(
    ("message", "forbidden", "preserved"),
    [
        (
            "db postgresql://db-user:db-pass@db.internal/app?sslmode=require#writer",
            ("db-user", "db-pass", "sslmode=require", "writer"),
            ("db.internal/app",),
        ),
        (
            "cache redis://:redis-pass@[::1]:6379/0?client=worker#primary",
            ("redis-pass", "client=worker", "primary"),
            ("[::1]:6379/0",),
        ),
        (
            "snapshot file:///var/lib/openevo/cache?mode=file-secret#primary",
            ("mode=file-secret", "primary"),
            ("file:///var/lib/openevo/cache",),
        ),
        (
            "worker --api-key cli-secret --model gpt-5",
            ("cli-secret",),
            ("worker", "--model gpt-5"),
        ),
        (
            "OPENAI_API_KEY env-secret worker ready",
            ("env-secret",),
            ("worker ready",),
        ),
        (
            "redis cache ready; query plan complete; token budget 128",
            (),
            ("redis cache ready", "query plan complete", "token budget 128"),
        ),
        (
            "public endpoint redis://cache.internal:6379/0",
            (),
            ("redis://cache.internal:6379/0",),
        ),
    ],
)
def test_plain_and_allowlisted_json_strings_use_the_same_sanitizer(
    message: str,
    forbidden: tuple[str, ...],
    preserved: tuple[str, ...],
) -> None:
    plain = supervisor_module._sanitize(message)
    structured = json.loads(
        supervisor_module._sanitize(json.dumps({"event": "probe", "message": message}))
    )["message"]

    assert structured == plain
    assert all(value not in plain for value in forbidden)
    assert all(value in plain for value in preserved)


def test_structured_unknown_field_names_retain_scalar_uri_sanitization() -> None:
    rendered = supervisor_module._sanitize(
        json.dumps(
            {
                "message": "worker ready",
                "https://user:pass@example.test/path?token=secret#tail": "ignored",
            }
        )
    )
    structured = json.loads(rendered)

    assert structured["message"] == "worker ready"
    assert "user:pass" not in rendered
    assert "token=secret" not in rendered
    assert "#tail" not in rendered
    safe_key = "https://<redacted>@example.test/path?<redacted>"
    assert structured[safe_key] == "<redacted>"


@pytest.mark.parametrize(
    ("message", "expected", "forbidden"),
    [
        (
            'worker --api-key "cli secret with spaces and \\"escaped\\" text" --model gpt-5',
            "worker --api-key <redacted> --model gpt-5",
            ("cli secret", "escaped", 'text"'),
        ),
        (
            "OPENAI_API_KEY 'env secret with spaces and \\'escaped\\' text' worker ready",
            "OPENAI_API_KEY <redacted> worker ready",
            ("env secret", "escaped", "text'"),
        ),
        (
            'worker --api-key "closed secret"quote-tail --model gpt-5',
            "worker --api-key <redacted> --model gpt-5",
            ("closed secret", "quote-tail"),
        ),
        (
            'worker --password "unterminated secret with quote tail --model gpt-5',
            "worker --password <redacted>",
            ("unterminated", "quote tail", "--model", "gpt-5"),
        ),
        (
            "OPENAI_API_KEY 'unterminated env secret worker ready",
            "OPENAI_API_KEY <redacted>",
            ("unterminated", "worker ready"),
        ),
    ],
)
def test_secret_option_and_env_quoted_values_use_closed_plain_and_json_grammar(
    message: str,
    expected: str,
    forbidden: tuple[str, ...],
) -> None:
    plain = supervisor_module._sanitize(message)
    structured = json.loads(
        supervisor_module._sanitize(json.dumps({"event": "probe", "message": message}))
    )["message"]

    assert plain == expected
    assert structured == expected
    assert all(value not in plain for value in forbidden)


@pytest.mark.parametrize(
    "message",
    [
        '状态 ready --api-key "cli secret with spaces and \\"escaped\\" text" worker healthy',
        "状态 ready OPENAI_API_KEY 'env secret with spaces and \\'escaped\\' text' worker healthy",
        '状态 ready --api-key "unterminated secret with quote tail worker healthy',
        "状态 ready OPENAI_API_KEY 'unterminated env secret worker healthy",
    ],
)
@pytest.mark.parametrize("terminator", [b"\n", b""])
def test_quoted_secret_redaction_is_chunk_utf8_and_eof_invariant(
    message: str,
    terminator: bytes,
) -> None:
    payload = message.encode("utf-8") + terminator
    expected = supervisor_module._sanitize(message).encode("utf-8") + b"\n"

    for chunk_size in (1, 2, 3, 5, 13, len(payload) + 1):
        redactor = supervisor_module._BoundedLogStreamRedactor("supervisor-credential-secret")
        rendered = bytearray()
        for offset in range(0, len(payload), chunk_size):
            rendered.extend(redactor.feed(payload[offset : offset + chunk_size]))
        rendered.extend(redactor.flush())

        assert bytes(rendered) == expected
        assert b"secret" not in rendered
        assert b"quote tail" not in rendered
        assert redactor.buffered_bytes == 0


def test_credential_eof_partial_prefix_has_deterministic_sensitive_minimum() -> None:
    urlsafe_alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"

    for first_character in urlsafe_alphabet:
        credential = first_character + "ensitive-credential-material-for-supervisor"
        benign = f"healthy worker marker {first_character}".encode("ascii")
        benign_redactor = supervisor_module._BoundedLogStreamRedactor(credential)
        assert benign_redactor.feed(benign) + benign_redactor.flush() == benign + b"\n"

        sensitive_prefix = credential[:16].encode("ascii")
        prefix_redactor = supervisor_module._BoundedLogStreamRedactor(credential)
        rendered = prefix_redactor.feed(b"request " + sensitive_prefix)
        rendered += prefix_redactor.flush()
        assert rendered == b"request <redacted>\n"
        assert sensitive_prefix not in rendered


@pytest.mark.parametrize(
    "chunk_sizes",
    [
        (1,),
        (2, 3, 5, 7, 11),
        (17, 64, 257, 1024),
    ],
)
def test_durable_logs_and_ledger_redact_all_forms_across_chunks_utf8_eof_and_oversize(
    tmp_path: Path,
    framework_lock: Path,
    chunk_sizes: tuple[int, ...],
) -> None:
    supervisor, backend, _, _ = _supervisor(tmp_path, framework_lock)
    safe_line = "状态 ready; redis cache healthy"
    payload = (
        b"db postgresql://db-user:db-pass@db.internal/app?sslmode=query-secret#writer\n"
        b"cache redis://:redis-pass@cache.internal:6379/0?client=client-secret#primary\n"
        b'{"event":"probe","message":"OPENAI_API_KEY json-env-secret worker ready; '
        b'redis://:json-redis-secret@json-cache.internal:6379/0?client=json-query#json-fragment"}\n'
        b"worker --api-key cli-secret --model gpt-5\n" + safe_line.encode("utf-8") + b"\n"
    )
    offset = 0
    chunk_index = 0
    try:
        _ensure_subscription(supervisor)
        while offset < len(payload):
            size = chunk_sizes[chunk_index % len(chunk_sizes)]
            backend.emit("gateway", payload[offset : offset + size])
            offset += size
            chunk_index += 1
        backend.emit("gateway", b"\noversize " + b"x" * 8_000)
        backend.emit("gateway", b"x" * 9_000)
        backend.emit(
            "gateway",
            b" postgresql://oversize-user:oversize-secret@db.internal/app?x=oversize-query\n",
        )
        eof_payload = b"eof OPENAI_API_KEY eof-secret worker stopped"
        for offset in range(0, len(eof_payload), 3):
            backend.emit("gateway", eof_payload[offset : offset + 3])
        backend.crash("gateway", 29)

        rendered = "\n".join(entry.message for entry in supervisor.logs("gateway", limit=100))
        ledger = (tmp_path / "core-services" / "ledger.json").read_text(encoding="utf-8")
        forbidden = (
            "db-user",
            "db-pass",
            "query-secret",
            "writer",
            "redis-pass",
            "client-secret",
            "primary",
            "json-env-secret",
            "json-redis-secret",
            "json-query",
            "json-fragment",
            "cli-secret",
            "oversize-secret",
            "oversize-query",
            "eof-secret",
        )
        assert all(value not in rendered for value in forbidden)
        assert all(value not in ledger for value in forbidden)
        assert safe_line in rendered
        assert "--model gpt-5" in rendered
        assert "worker stopped" in rendered
        assert "<redacted-oversize-line>" in rendered
        assert all(
            len(entry.message.encode("utf-8")) <= 16_384 for entry in supervisor.logs("gateway")
        )
    finally:
        supervisor.close()


@pytest.mark.parametrize("chunk_sizes", [(1,), (2, 3, 5, 7), (31, 64, 127)])
@pytest.mark.parametrize("terminator", [b"\n", b""])
def test_log_redactor_drops_oversize_line_with_bounded_carry_and_flushes_eof(
    chunk_sizes: tuple[int, ...],
    terminator: bytes,
) -> None:
    redactor = supervisor_module._BoundedLogStreamRedactor(
        "supervisor-credential-secret",
        max_line_bytes=64,
    )
    secret = b"oversize-secret-value"
    payload = b"Authorization: Bearer " + b"x" * 512 + secret + terminator
    rendered = bytearray()
    offset = 0
    chunk_index = 0

    while offset < len(payload):
        size = chunk_sizes[chunk_index % len(chunk_sizes)]
        rendered.extend(redactor.feed(payload[offset : offset + size]))
        assert redactor.buffered_bytes <= 64
        offset += size
        chunk_index += 1
    rendered.extend(redactor.flush())

    assert bytes(rendered) == b"<redacted-oversize-line>\n"
    assert secret not in rendered
    assert redactor.buffered_bytes == 0


def test_release_mode_requires_verified_registry_and_reverifies_each_ensure(
    tmp_path: Path,
    framework_lock: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = verified_builtin_registry(tmp_path / "verified-registry")
    real_identity = supervisor_module.release_identity_from_verified_registry
    calls = 0

    def observed_identity(candidate: object) -> ServiceReleaseIdentity:
        nonlocal calls
        calls += 1
        return real_identity(candidate)

    monkeypatch.setattr(
        supervisor_module,
        "release_identity_from_verified_registry",
        observed_identity,
    )
    supervisor = CoreServiceSupervisor(
        launch_mode=ServiceLaunchMode.RELEASE,
        service_root=tmp_path / "release-services",
        framework_lock=framework_lock,
        verified_registry=registry,
    )
    try:
        snapshot = supervisor.ensure(
            ServiceExecutionMode.SELF_DEPLOYED,
            model_ref="Qwen/release-probe",
        )
        assert snapshot.services_available is False
        assert calls == 2

        from openevo.evolution.framework import loading

        def reject_inventory(_attestation: object) -> None:
            raise RuntimeError("controlled installed inventory mismatch")

        monkeypatch.setattr(loading, "_reverify_distribution_inventory", reject_inventory)
        with pytest.raises(SupervisorStateError, match="could not be revalidated"):
            supervisor.ensure(
                ServiceExecutionMode.SELF_DEPLOYED,
                model_ref="Qwen/release-probe",
            )
    finally:
        supervisor.close()


def test_release_restart_completed_replay_reverifies_inventory_before_return(
    tmp_path: Path,
    framework_lock: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = verified_builtin_registry(tmp_path / "verified-restart-registry")
    backend = FakeProcessBackend()
    runtime_probe = FakeManagedScienceRuntimeProbe()
    runtime_probe.configure_auth_path(tmp_path / "release-codex" / "auth.json")

    class ReleaseHealthChecker(FakeHealthChecker):
        pass

    monkeypatch.setattr(supervisor_module, "RealSubprocessBackend", lambda: backend)
    monkeypatch.setattr(supervisor_module, "DefaultHealthChecker", ReleaseHealthChecker)
    monkeypatch.setattr(supervisor_module, "SocketPortProbe", FakePortProbe)
    monkeypatch.setattr(
        supervisor_module,
        "_probe_rollout_registration",
        lambda *_args: (True, "healthy"),
    )
    monkeypatch.setattr(
        supervisor_module,
        "LocalManagedScienceRuntimeProbe",
        lambda: runtime_probe,
    )
    supervisor = CoreServiceSupervisor(
        launch_mode=ServiceLaunchMode.RELEASE,
        service_root=tmp_path / "release-restart-services",
        framework_lock=framework_lock,
        verified_registry=registry,
    )
    try:
        _ensure_subscription(supervisor)
        completed = supervisor.restart("gateway", operation_id="release-replay")
        spawn_count = len(backend.spawned)
        ledger_before = (tmp_path / "release-restart-services" / "ledger.json").read_bytes()

        from openevo.evolution.framework import loading

        def reject_inventory(_attestation: object) -> None:
            raise RuntimeError("controlled attested inventory change")

        monkeypatch.setattr(loading, "_reverify_distribution_inventory", reject_inventory)

        with pytest.raises(SupervisorStateError, match="could not be revalidated"):
            supervisor.restart("gateway", operation_id="release-replay")

        assert len(backend.spawned) == spawn_count
        assert (
            tmp_path / "release-restart-services" / "ledger.json"
        ).read_bytes() == ledger_before
        assert supervisor._restart_results["release-replay"] == ("gateway", completed)
    finally:
        supervisor.close()


def test_release_restart_inventory_failure_is_transactional_between_reverifications(
    tmp_path: Path,
    framework_lock: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = verified_builtin_registry(tmp_path / "verified-transaction-registry")
    backend = FakeProcessBackend()
    runtime_probe = FakeManagedScienceRuntimeProbe()
    runtime_probe.configure_auth_path(tmp_path / "release-codex" / "auth.json")

    class ReleaseHealthChecker(FakeHealthChecker):
        pass

    monkeypatch.setattr(supervisor_module, "RealSubprocessBackend", lambda: backend)
    monkeypatch.setattr(supervisor_module, "DefaultHealthChecker", ReleaseHealthChecker)
    monkeypatch.setattr(supervisor_module, "SocketPortProbe", FakePortProbe)
    monkeypatch.setattr(
        supervisor_module,
        "_probe_rollout_registration",
        lambda *_args: (True, "healthy"),
    )
    monkeypatch.setattr(
        supervisor_module,
        "LocalManagedScienceRuntimeProbe",
        lambda: runtime_probe,
    )
    supervisor = CoreServiceSupervisor(
        launch_mode=ServiceLaunchMode.RELEASE,
        service_root=tmp_path / "release-transaction-services",
        framework_lock=framework_lock,
        verified_registry=registry,
    )
    try:
        assert _ensure_subscription(supervisor).services_available
        real_identity = supervisor_module.release_identity_from_verified_registry
        reverify_calls = 0

        def change_between_reverifications(candidate: object) -> ServiceReleaseIdentity:
            nonlocal reverify_calls
            reverify_calls += 1
            if reverify_calls == 2:
                raise RuntimeError("controlled inventory change between restart checks")
            return real_identity(candidate)

        monkeypatch.setattr(
            supervisor_module,
            "release_identity_from_verified_registry",
            change_between_reverifications,
        )
        plan_key_before = supervisor._active_plan_key
        handles_before = dict(supervisor._handles)
        specs_before = dict(supervisor._specs)
        credential_before = supervisor._active_credential
        runtime_request_before = supervisor._active_runtime_request
        ledger_before = copy.deepcopy(supervisor._ledger)
        ledger_bytes_before = (
            tmp_path / "release-transaction-services" / "ledger.json"
        ).read_bytes()
        replay_before = dict(supervisor._restart_results)
        spawned_before = list(backend.spawned)

        with pytest.raises(SupervisorStateError, match="could not be revalidated"):
            supervisor.restart("gateway", operation_id="inventory-race")

        assert reverify_calls == 2
        assert supervisor._active_plan_key == plan_key_before
        assert supervisor._handles == handles_before
        assert supervisor._specs == specs_before
        assert supervisor._active_credential == credential_before
        assert supervisor._active_runtime_request == runtime_request_before
        assert supervisor._ledger == ledger_before
        assert (
            tmp_path / "release-transaction-services" / "ledger.json"
        ).read_bytes() == ledger_bytes_before
        assert supervisor._restart_results == replay_before
        assert backend.spawned == spawned_before
    finally:
        supervisor.close()


def test_release_mode_rejects_development_injection(
    tmp_path: Path,
    framework_lock: Path,
) -> None:
    with pytest.raises(ValueError, match="verified registry"):
        CoreServiceSupervisor(
            launch_mode=ServiceLaunchMode.RELEASE,
            service_root=tmp_path / "release-services",
            framework_lock=framework_lock,
            release_identity=ServiceReleaseIdentity(INSTALL_DIGEST, REGISTRY_DIGEST),
        )


def test_release_registry_reverification_is_private_and_rejects_unsealed_objects(
    tmp_path: Path,
    framework_lock: Path,
) -> None:
    with pytest.raises(TypeError, match="verified registry loader"):
        CoreServiceSupervisor(
            launch_mode=ServiceLaunchMode.RELEASE,
            service_root=tmp_path / "unsealed-release-services",
            framework_lock=framework_lock,
            verified_registry=object(),
        )

    completed = subprocess.run(
        (
            sys.executable,
            "-c",
            "import openevo.evolution.framework as framework;"
            "assert not hasattr(framework, 'reverify_distribution_install');"
            "assert 'reverify_distribution_install' not in framework.__all__",
        ),
        check=False,
        capture_output=True,
        timeout=10,
    )
    assert completed.returncode == 0, completed.stderr.decode("utf-8", errors="replace")


def test_log_budget_is_global_across_all_services(
    tmp_path: Path,
    framework_lock: Path,
) -> None:
    supervisor, backend, _, _ = _supervisor(
        tmp_path,
        framework_lock,
        max_log_entries=3,
        max_log_bytes=1_000,
    )
    try:
        _ensure_subscription(supervisor)
        for service_id in ("evolution-backend", "rollout", "gateway", "evolution-worker"):
            backend.emit(service_id, f"log from {service_id}\n".encode())
        assert sum(len(supervisor.logs(service.id)) for service in supervisor.list()) == 3
    finally:
        supervisor.close()


def test_self_deployed_is_typed_unavailable_and_never_claims_model_ready(
    tmp_path: Path,
    framework_lock: Path,
) -> None:
    supervisor, backend, _, _ = _supervisor(tmp_path, framework_lock)
    try:
        snapshot = supervisor.ensure(
            ServiceExecutionMode.SELF_DEPLOYED,
            model_ref="Qwen/Qwen3-Coder-30B-A3B-Instruct",
        )
        assert snapshot.services_available is False
        assert snapshot.run_ready is False
        assert backend.spawned == []
        inference = snapshot.service("inference")
        assert inference.status is ServiceStatus.UNAVAILABLE
        assert inference.model_preparation is not None
        assert inference.model_preparation.status == "unresolved"
        assert inference.model_preparation.next_interface == "model_preparer_v1"
        contract = inference.to_contract()
        assert isinstance(contract, ServiceSummaryV1)
        assert contract.status.value == "unavailable"
        assert contract.model_preparation.status.value == "unresolved"
    finally:
        supervisor.close()


def test_root_and_ledger_fail_closed_on_symlink_and_malicious_state(
    tmp_path: Path,
    framework_lock: Path,
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    service_root = tmp_path / "linked-services"
    service_root.symlink_to(outside, target_is_directory=True)
    with pytest.raises(SupervisorStateError, match="symlink"):
        CoreServiceSupervisor(
            launch_mode=ServiceLaunchMode.DEVELOPMENT_TEST,
            service_root=service_root,
            framework_lock=framework_lock,
            release_identity=ServiceReleaseIdentity(INSTALL_DIGEST, REGISTRY_DIGEST),
        )

    service_root.unlink()
    service_root.mkdir(mode=0o700)
    ledger = service_root / "ledger.json"
    ledger.write_text('{"schema_version":1,"unexpected":true}', encoding="utf-8")
    ledger.chmod(0o600)
    with pytest.raises(SupervisorStateError, match="ledger"):
        CoreServiceSupervisor(
            launch_mode=ServiceLaunchMode.DEVELOPMENT_TEST,
            service_root=service_root,
            framework_lock=framework_lock,
            release_identity=ServiceReleaseIdentity(INSTALL_DIGEST, REGISTRY_DIGEST),
        )


def test_root_replacement_fails_operations_without_touching_replacement(
    tmp_path: Path,
    framework_lock: Path,
) -> None:
    supervisor, _, _, _ = _supervisor(tmp_path, framework_lock)
    original = tmp_path / "core-services"
    moved = tmp_path / "moved-services"
    original.rename(moved)
    original.mkdir(mode=0o700)
    marker = original / "marker"
    marker.write_text("replacement", encoding="utf-8")
    try:
        with pytest.raises(SupervisorStateError, match="binding"):
            supervisor.list()
        with pytest.raises(SupervisorBusyError):
            _supervisor(tmp_path, framework_lock)
        assert marker.read_text(encoding="utf-8") == "replacement"
    finally:
        original.rename(tmp_path / "replacement-services")
        moved.rename(original)
        supervisor.close()


def test_startup_recovery_never_signals_pid_from_old_ledger(
    tmp_path: Path,
    framework_lock: Path,
) -> None:
    supervisor, backend, _, _ = _supervisor(tmp_path, framework_lock)
    _ensure_subscription(supervisor)
    supervisor._abandon_for_test()

    recovered_backend = FakeProcessBackend()
    recovered, _, _, _ = _supervisor(
        tmp_path,
        framework_lock,
        backend=recovered_backend,
    )
    try:
        assert recovered_backend.terminated == []
        assert recovered_backend.killed == []
        assert all(service.pid is None for service in recovered.list())
        assert all(service.status is ServiceStatus.FAILED for service in recovered.list())
    finally:
        recovered.close()


def test_close_is_bounded_and_escalates_to_kill(
    tmp_path: Path,
    framework_lock: Path,
) -> None:
    backend = FakeProcessBackend()

    def ignore_terminate(identity: ProcessIdentity) -> None:
        backend.terminated.append(backend._service_id(identity))

    backend.terminate = ignore_terminate  # type: ignore[method-assign]
    supervisor, backend, _, _ = _supervisor(
        tmp_path,
        framework_lock,
        backend=backend,
        stop_timeout=0.03,
    )
    _ensure_subscription(supervisor)
    started = time.monotonic()
    supervisor.close(total_timeout=0.08)
    assert time.monotonic() - started < 0.3
    assert backend.killed


def test_identity_change_does_not_spawn_replacement_when_children_cannot_stop(
    tmp_path: Path,
    framework_lock: Path,
) -> None:
    backend = FakeProcessBackend()
    supervisor, backend, _, _ = _supervisor(
        tmp_path,
        framework_lock,
        backend=backend,
        stop_timeout=0.01,
    )
    _ensure_subscription(supervisor)

    def ignore_signal(identity: ProcessIdentity) -> None:
        del identity

    backend.terminate = ignore_signal  # type: ignore[method-assign]
    backend.kill = ignore_signal  # type: ignore[method-assign]
    snapshot = _ensure_subscription(supervisor, codex_model="gpt-5.2-codex")

    assert len(backend.spawned) == 4
    assert snapshot.services_available is False
    assert all(service.error_code == "service_stop_timeout" for service in snapshot.services)
    assert all(service.pid is not None for service in snapshot.services)
    with pytest.raises(SupervisorStateError, match="ownership was retained"):
        supervisor.close(total_timeout=0.02)
    backend.kill = FakeProcessBackend.kill.__get__(backend, FakeProcessBackend)
    supervisor.close(total_timeout=0.05)


def test_second_host_global_owner_is_rejected(
    tmp_path: Path,
    framework_lock: Path,
) -> None:
    supervisor, _, _, _ = _supervisor(tmp_path, framework_lock)
    try:
        with pytest.raises(SupervisorBusyError):
            _supervisor(tmp_path, framework_lock)
    finally:
        supervisor.close()


def test_failed_initialization_releases_host_global_owner(tmp_path: Path) -> None:
    framework_lock = tmp_path / "framework-lock.json"
    framework_lock.write_text("{}", encoding="utf-8")
    framework_lock.chmod(0o666)
    with pytest.raises(SupervisorStateError, match="mode 0600"):
        _supervisor(tmp_path, framework_lock)

    framework_lock.chmod(0o600)
    supervisor, _, _, _ = _supervisor(tmp_path, framework_lock)
    supervisor.close()


def test_immutable_framework_lock_is_accepted(
    tmp_path: Path,
    framework_lock: Path,
) -> None:
    framework_lock.chmod(0o400)

    supervisor, _, _, _ = _supervisor(tmp_path, framework_lock)

    supervisor.close()


def test_framework_lock_path_replacement_fails_before_service_stop(
    tmp_path: Path,
    framework_lock: Path,
) -> None:
    supervisor, backend, _, _ = _supervisor(tmp_path, framework_lock)
    try:
        assert _ensure_subscription(supervisor).services_available
        original = framework_lock.with_suffix(".original")
        framework_lock.rename(original)
        framework_lock.write_bytes(original.read_bytes())
        framework_lock.chmod(0o600)

        with pytest.raises(SupervisorStateError, match="pathname binding changed"):
            supervisor.restart("gateway", operation_id="lock-replaced")

        assert backend.terminated == []
        assert all(backend.alive.values())
    finally:
        original = framework_lock.with_suffix(".original")
        if original.exists():
            framework_lock.unlink(missing_ok=True)
            original.rename(framework_lock)
        supervisor.close()


def test_cancel_interrupts_blocked_runtime_probe_within_bound(
    tmp_path: Path,
    framework_lock: Path,
) -> None:
    runtime_probe = BlockingManagedScienceRuntimeProbe()
    supervisor, backend, _, _ = _supervisor(
        tmp_path,
        framework_lock,
        runtime_probe=runtime_probe,
        startup_timeout=10,
        stop_timeout=0.05,
    )
    errors: list[BaseException] = []

    def ensure() -> None:
        try:
            _ensure_subscription(supervisor)
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=ensure)
    thread.start()
    assert runtime_probe.entered.wait(timeout=1)
    started = time.monotonic()
    supervisor.cancel(total_timeout=0.2)
    thread.join(timeout=1)

    assert time.monotonic() - started < 0.5
    assert not thread.is_alive()
    assert backend.spawned == []
    assert len(errors) == 1
    assert isinstance(errors[0], SupervisorStateError)


def test_cancel_interrupts_readiness_and_reaps_started_services(
    tmp_path: Path,
    framework_lock: Path,
) -> None:
    health = FakeHealthChecker()
    health.block_service = "rollout"
    supervisor, backend, _, _ = _supervisor(
        tmp_path,
        framework_lock,
        health=health,
        startup_timeout=10,
        stop_timeout=0.05,
    )
    outcomes: list[object] = []

    def ensure() -> None:
        try:
            outcomes.append(_ensure_subscription(supervisor))
        except BaseException as exc:
            outcomes.append(exc)

    thread = threading.Thread(target=ensure)
    thread.start()
    deadline = time.monotonic() + 1
    while "rollout" not in health.checked:
        assert time.monotonic() < deadline
        time.sleep(0.005)

    started = time.monotonic()
    supervisor.cancel(total_timeout=0.3)
    thread.join(timeout=1)

    assert time.monotonic() - started < 0.6
    assert not thread.is_alive()
    assert backend.terminated == ["rollout", "evolution-backend"]
    assert len(outcomes) == 1
    assert isinstance(outcomes[0], SupervisorStateError)
    assert str(outcomes[0]) == "service operation was cancelled"


def test_cancel_interrupts_reused_generation_readiness_and_preserves_error(
    tmp_path: Path,
    framework_lock: Path,
) -> None:
    health = FakeHealthChecker()
    supervisor, backend, _, _ = _supervisor(
        tmp_path,
        framework_lock,
        health=health,
        startup_timeout=0.4,
        stop_timeout=0.02,
    )
    _ensure_subscription(supervisor)
    original_generation = supervisor._ledger.generation_digest
    health.block_service = "evolution-backend"
    errors: list[BaseException] = []

    def ensure_again() -> None:
        try:
            _ensure_subscription(supervisor)
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=ensure_again)
    thread.start()
    assert health.block_entered.wait(timeout=1)
    started = time.monotonic()
    supervisor.cancel(total_timeout=0.2)
    thread.join(timeout=1)

    assert time.monotonic() - started < 0.25
    assert not thread.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], SupervisorStateError)
    assert str(errors[0]) == "service operation was cancelled"
    assert health.cancellations[-1] is not None
    assert health.cancellations[-1].is_set()
    assert supervisor._ledger.generation_digest == original_generation
    assert set(backend.terminated[-4:]) == {
        "evolution-backend",
        "rollout",
        "gateway",
        "evolution-worker",
    }


def test_symlinked_ledger_is_rejected_without_reading_target(
    tmp_path: Path,
    framework_lock: Path,
) -> None:
    service_root = tmp_path / "core-services"
    service_root.mkdir(mode=0o700)
    target = tmp_path / "attacker-ledger.json"
    target.write_text("{}", encoding="utf-8")
    target.chmod(0o600)
    (service_root / "ledger.json").symlink_to(target)
    with pytest.raises(SupervisorStateError, match="symlink"):
        _supervisor(tmp_path, framework_lock)


def test_subscription_runtime_bootstrap_failure_is_unavailable_without_spawn(
    tmp_path: Path,
    framework_lock: Path,
) -> None:
    runtime_probe = FakeManagedScienceRuntimeProbe(ready=False)
    supervisor, backend, _, _ = _supervisor(
        tmp_path,
        framework_lock,
        runtime_probe=runtime_probe,
    )
    try:
        snapshot = _ensure_subscription(supervisor)
        assert snapshot.services_available is False
        assert snapshot.run_ready is False
        assert snapshot.run_readiness_code is ServiceRunReadinessCode.RUNTIME_IMAGE_UNAVAILABLE
        assert backend.spawned == []
        assert all(service.status is ServiceStatus.UNAVAILABLE for service in snapshot.services)
        assert snapshot.runtime_identity_digest is None
        assert "not prepared" in snapshot.status_message
        with pytest.raises(SupervisorStateError, match="not restartable"):
            supervisor.restart("gateway", operation_id="bypass-runtime-probe")
    finally:
        supervisor.close()


def test_local_managed_runtime_probe_binds_image_codex_and_private_auth(
    tmp_path: Path,
) -> None:
    auth = tmp_path / "auth.json"
    auth.write_text('{"tokens":"not-read-by-probe"}', encoding="utf-8")
    auth.chmod(0o600)
    command_runner = FakeProbeCommandRunner()
    probe = LocalManagedScienceRuntimeProbe(
        command_runner=command_runner,
        codex_auth_path=auth,
    )
    request = ManagedScienceRuntimeRequest(
        runtime_image="openevo/science-runtime:0.1.0",
        codex_model="gpt-5.1-codex-mini",
    )

    readiness = probe.verify(request, time.monotonic() + 1)

    assert readiness.ready is True
    assert readiness.code is ServiceRunReadinessCode.READY
    assert readiness.identity_digest is not None
    assert command_runner.calls == [
        ("codex", "--version"),
        ("codex", "login", "status"),
        ("docker", "--version"),
        ("docker", "image", "inspect", "openevo/science-runtime:0.1.0"),
    ]
    assert "not-read-by-probe" not in readiness.message
    assert readiness.credential_authority is not None
    readiness.credential_authority.close()


def test_local_probe_login_uses_snapshot_and_resists_source_path_aba(
    tmp_path: Path,
) -> None:
    original = b'{"tokens":{"access_token":"original-private-token"}}'
    auth = tmp_path / "auth.json"
    auth.write_bytes(original)
    auth.chmod(0o600)
    probe_root = tmp_path / "probe-root"
    probe_root.mkdir(mode=0o700)

    class AbaRunner(FakeProbeCommandRunner):
        observed_login_auth: bytes | None = None

        def run(
            self,
            argv,
            deadline,
            cancellation=None,
            *,
            env=None,
            pass_fds=(),
        ) -> ProbeCommandResult:
            if argv == ("codex", "login", "status"):
                assert env is not None
                snapshot_home = Path(env["CODEX_HOME"])
                assert snapshot_home.parent == probe_root
                self.observed_login_auth = (snapshot_home / "auth.json").read_bytes()
                held_original = auth.with_name("auth.original")
                replacement = auth.with_name("auth.replacement")
                replacement.write_text(
                    '{"tokens":{"access_token":"replacement-private-token"}}',
                    encoding="utf-8",
                )
                replacement.chmod(0o600)
                os.replace(auth, held_original)
                os.replace(replacement, auth)
                os.replace(auth, replacement)
                os.replace(held_original, auth)
                replacement.unlink()
            return super().run(
                argv,
                deadline,
                cancellation,
                env=env,
                pass_fds=pass_fds,
            )

    runner = AbaRunner()
    readiness = LocalManagedScienceRuntimeProbe(
        command_runner=runner,
        codex_auth_path=auth,
        credential_probe_root=probe_root,
    ).verify(
        ManagedScienceRuntimeRequest(
            runtime_image="openevo/science-runtime:0.1.0",
            codex_model="gpt-5.1-codex-mini",
        ),
        time.monotonic() + 1,
    )

    assert readiness.ready is True
    assert runner.observed_login_auth == original
    assert auth.read_bytes() == original
    assert list(probe_root.iterdir()) == []
    snapshot = readiness.credential_authority
    assert isinstance(snapshot, PreparedCodexCredentialSnapshot)
    descriptor = snapshot.duplicate_verified_descriptor()
    try:
        assert os.pread(descriptor, snapshot.size, 0) == original
    finally:
        os.close(descriptor)
        snapshot.close()


def test_local_probe_source_replacement_during_snapshot_never_runs_login(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    auth = tmp_path / "auth.json"
    auth.write_text('{"tokens":{"access_token":"original"}}', encoding="utf-8")
    auth.chmod(0o600)
    probe_root = tmp_path / "probe-root"
    probe_root.mkdir(mode=0o700)
    original_copy = session_files_module._copy_exact

    def replace_during_copy(source_fd: int, target_fd: int, size: int) -> None:
        original_copy(source_fd, target_fd, size)
        replacement = auth.with_name("auth.replacement")
        replacement.write_text(
            '{"tokens":{"access_token":"replacement"}}',
            encoding="utf-8",
        )
        replacement.chmod(0o600)
        os.replace(replacement, auth)

    monkeypatch.setattr(session_files_module, "_copy_exact", replace_during_copy)
    runner = FakeProbeCommandRunner()
    readiness = LocalManagedScienceRuntimeProbe(
        command_runner=runner,
        codex_auth_path=auth,
        credential_probe_root=probe_root,
    ).verify(
        ManagedScienceRuntimeRequest(
            runtime_image="openevo/science-runtime:0.1.0",
            codex_model="gpt-5.1-codex-mini",
        ),
        time.monotonic() + 1,
    )

    assert readiness.ready is False
    assert readiness.code is ServiceRunReadinessCode.CODEX_SUBSCRIPTION_AUTH_UNAVAILABLE
    assert runner.calls == [("codex", "--version")]
    assert list(probe_root.iterdir()) == []


@pytest.mark.parametrize(
    "version_result",
    [
        ProbeCommandResult(0, b"", b""),
        ProbeCommandResult(0, b"codex-cli latest\n", b""),
        ProbeCommandResult(0, b"codex-cli 1.2\n", b""),
        ProbeCommandResult(0, b"codex-cli 01.2.3\n", b""),
        ProbeCommandResult(0, b"codex-cli 1.02.3\n", b""),
        ProbeCommandResult(0, b"codex-cli 1.2.03\n", b""),
        ProbeCommandResult(0, b"codex-cli 1.2.3-01\n", b""),
        ProbeCommandResult(0, b"codex-cli 1.2.3+\n", b""),
        ProbeCommandResult(0, b" codex-cli 1.2.3\n", b""),
        ProbeCommandResult(0, b"codex-cli 1.2.3\nextra\n", b""),
        ProbeCommandResult(0, b"x" * 4097, b""),
    ],
)
def test_local_probe_rejects_empty_unbounded_or_malformed_codex_version(
    tmp_path: Path,
    version_result: ProbeCommandResult,
) -> None:
    auth = tmp_path / "auth.json"
    auth.write_text('{"tokens":{"access_token":"private"}}', encoding="utf-8")
    auth.chmod(0o600)
    runner = FakeProbeCommandRunner(
        results={("codex", "--version"): version_result}
    )

    readiness = LocalManagedScienceRuntimeProbe(
        command_runner=runner,
        codex_auth_path=auth,
    ).verify(
        ManagedScienceRuntimeRequest(
            runtime_image="openevo/science-runtime:0.1.0",
            codex_model="gpt-5.1-codex-mini",
        ),
        time.monotonic() + 1,
    )

    assert readiness.ready is False
    assert readiness.code is ServiceRunReadinessCode.CODEX_CLI_UNAVAILABLE
    assert runner.calls == [("codex", "--version")]


def test_local_probe_rejects_stderr_only_version(
    tmp_path: Path,
) -> None:
    auth = tmp_path / "auth.json"
    auth.write_text('{"tokens":{"access_token":"private"}}', encoding="utf-8")
    auth.chmod(0o600)

    runner = FakeProbeCommandRunner(
        results={
            ("codex", "--version"): ProbeCommandResult(
                0,
                b"",
                b"codex-cli 1.2.3\n",
            ),
        }
    )
    readiness = LocalManagedScienceRuntimeProbe(
        command_runner=runner,
        codex_auth_path=auth,
    ).verify(
        ManagedScienceRuntimeRequest(
            runtime_image="openevo/science-runtime:0.1.0",
            codex_model="gpt-5.1-codex-mini",
        ),
        time.monotonic() + 1,
    )

    assert readiness.ready is False
    assert readiness.code is ServiceRunReadinessCode.CODEX_CLI_UNAVAILABLE
    assert runner.calls == [("codex", "--version")]


def test_local_probe_retains_bounded_stderr_as_non_authoritative_evidence(
    tmp_path: Path,
) -> None:
    auth = tmp_path / "auth.json"
    auth.write_text('{"tokens":{"access_token":"private"}}', encoding="utf-8")
    auth.chmod(0o600)

    def readiness_for(stderr: bytes) -> ManagedScienceRuntimeReadiness:
        runner = FakeProbeCommandRunner(
            results={
                ("codex", "--version"): ProbeCommandResult(
                    0,
                    b"codex-cli 1.2.3\n",
                    stderr,
                ),
            }
        )
        return LocalManagedScienceRuntimeProbe(
            command_runner=runner,
            codex_auth_path=auth,
        ).verify(
            ManagedScienceRuntimeRequest(
                runtime_image="openevo/science-runtime:0.1.0",
                codex_model="gpt-5.1-codex-mini",
            ),
            time.monotonic() + 1,
        )

    first = readiness_for(b"diagnostic one\n")
    second = readiness_for(b"diagnostic two\n")
    try:
        assert first.ready is True
        assert second.ready is True
        assert first.identity_digest != second.identity_digest
    finally:
        assert first.credential_authority is not None
        assert second.credential_authority is not None
        first.credential_authority.close()
        second.credential_authority.close()


def test_local_probe_accepts_legal_semver_prerelease_and_build(tmp_path: Path) -> None:
    auth = tmp_path / "auth.json"
    auth.write_text('{"tokens":{"access_token":"private"}}', encoding="utf-8")
    auth.chmod(0o600)
    runner = FakeProbeCommandRunner(
        results={
            ("codex", "--version"): ProbeCommandResult(
                0,
                b"codex-cli 1.2.3-beta-1.0+build.01\n",
                b"",
            )
        }
    )

    readiness = LocalManagedScienceRuntimeProbe(
        command_runner=runner,
        codex_auth_path=auth,
    ).verify(
        ManagedScienceRuntimeRequest(
            runtime_image="openevo/science-runtime:0.1.0",
            codex_model="gpt-5.1-codex-mini",
        ),
        time.monotonic() + 1,
    )

    try:
        assert readiness.ready is True
    finally:
        assert readiness.credential_authority is not None
        readiness.credential_authority.close()


def test_runtime_probe_exception_closes_held_credential_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    auth = tmp_path / "auth.json"
    auth.write_text('{"tokens":"private-auth-material"}', encoding="utf-8")
    auth.chmod(0o600)
    opened: list[HeldCodexCredentialAuthority] = []
    real_open = HeldCodexCredentialAuthority.open

    def capture_open(path: Path) -> HeldCodexCredentialAuthority:
        authority = real_open(path)
        opened.append(authority)
        return authority

    class ExplodingRunner(FakeProbeCommandRunner):
        def run(
            self,
            argv,
            deadline,
            cancellation=None,
            *,
            env=None,
            pass_fds=(),
        ) -> ProbeCommandResult:
            if argv == ("docker", "--version"):
                raise RuntimeError("controlled runtime probe failure")
            return super().run(
                argv,
                deadline,
                cancellation,
                env=env,
                pass_fds=pass_fds,
            )

    monkeypatch.setattr(
        supervisor_module.HeldCodexCredentialAuthority,
        "open",
        staticmethod(capture_open),
    )
    probe = LocalManagedScienceRuntimeProbe(
        command_runner=ExplodingRunner(),
        codex_auth_path=auth,
    )
    before_fds = len(os.listdir("/proc/self/fd"))

    with pytest.raises(RuntimeError, match="controlled runtime probe failure"):
        probe.verify(
            ManagedScienceRuntimeRequest(
                runtime_image="openevo/science-runtime:0.1.0",
                codex_model="gpt-5.1-codex-mini",
            ),
            time.monotonic() + 1,
        )

    assert len(opened) == 1
    with pytest.raises(SessionFileSecurityError, match="closed"):
        opened[0].verify()
    assert len(os.listdir("/proc/self/fd")) == before_fds


def test_local_managed_runtime_probe_rejects_wrong_release_digest_before_run(
    tmp_path: Path,
) -> None:
    auth = tmp_path / "auth.json"
    auth.write_text('{"tokens":"not-read-by-probe"}', encoding="utf-8")
    auth.chmod(0o600)
    probe = LocalManagedScienceRuntimeProbe(
        command_runner=FakeProbeCommandRunner(image_id="1" * 64),
        codex_auth_path=auth,
    )

    readiness = probe.verify(
        ManagedScienceRuntimeRequest(
            runtime_image="openevo/science-runtime:0.1.0",
            codex_model="gpt-5.1-codex-mini",
        ),
        time.monotonic() + 1,
    )

    assert readiness.ready is False
    assert readiness.code is ServiceRunReadinessCode.RUNTIME_EVIDENCE_INVALID
    assert readiness.identity_digest is None
    assert readiness.message == "Managed Science bootstrap evidence is invalid."


@pytest.mark.parametrize(
    ("failed_command", "result", "expected_code"),
    [
        (
            ("codex", "--version"),
            ProbeCommandResult(127, b"", b"codex: not found"),
            ServiceRunReadinessCode.CODEX_CLI_UNAVAILABLE,
        ),
        (
            ("codex", "login", "status"),
            ProbeCommandResult(1, b"", b"refresh-token=do-not-report"),
            ServiceRunReadinessCode.CODEX_SUBSCRIPTION_AUTH_UNAVAILABLE,
        ),
        (
            ("codex", "login", "status"),
            ProbeCommandResult(0, b"Logged in using an API key\n", b""),
            ServiceRunReadinessCode.CODEX_SUBSCRIPTION_AUTH_UNAVAILABLE,
        ),
        (
            ("docker", "--version"),
            ProbeCommandResult(127, b"", b"docker: not found"),
            ServiceRunReadinessCode.RUNTIME_EXECUTABLE_UNAVAILABLE,
        ),
        (
            ("docker", "image", "inspect", "openevo/science-runtime:0.1.0"),
            ProbeCommandResult(1, b"", b"No such image"),
            ServiceRunReadinessCode.RUNTIME_IMAGE_UNAVAILABLE,
        ),
    ],
)
def test_local_managed_runtime_probe_reports_typed_prerequisite_failures_without_output(
    tmp_path: Path,
    failed_command: tuple[str, ...],
    result: ProbeCommandResult,
    expected_code: ServiceRunReadinessCode,
) -> None:
    auth = tmp_path / "auth.json"
    auth.write_text('{"tokens":"private-auth-material"}', encoding="utf-8")
    auth.chmod(0o600)
    runner = FakeProbeCommandRunner(results={failed_command: result})
    probe = LocalManagedScienceRuntimeProbe(
        command_runner=runner,
        codex_auth_path=auth,
    )

    readiness = probe.verify(
        ManagedScienceRuntimeRequest(
            runtime_image="openevo/science-runtime:0.1.0",
            codex_model="gpt-5.1-codex-mini",
        ),
        time.monotonic() + 1,
    )

    assert readiness.ready is False
    assert readiness.code is expected_code
    assert readiness.identity_digest is None
    assert "refresh-token" not in readiness.message
    assert "do-not-report" not in readiness.message
    assert "private-auth-material" not in readiness.message


def test_local_managed_runtime_probe_rejects_symlinked_auth(
    tmp_path: Path,
) -> None:
    target = tmp_path / "outside-auth.json"
    target.write_text("secret", encoding="utf-8")
    target.chmod(0o600)
    auth = tmp_path / "auth.json"
    auth.symlink_to(target)
    probe = LocalManagedScienceRuntimeProbe(
        command_runner=FakeProbeCommandRunner(),
        codex_auth_path=auth,
    )

    readiness = probe.verify(
        ManagedScienceRuntimeRequest(
            runtime_image="openevo/science-runtime:0.1.0",
            codex_model="gpt-5.1-codex-mini",
        ),
        time.monotonic() + 1,
    )

    assert readiness.ready is False
    assert readiness.code is ServiceRunReadinessCode.CODEX_SUBSCRIPTION_AUTH_UNAVAILABLE
    assert readiness.identity_digest is None
    assert readiness.message == (
        "Codex subscription login evidence is invalid on the remote Core host."
    )


def test_local_managed_runtime_probe_rejects_missing_auth_evidence(tmp_path: Path) -> None:
    runner = FakeProbeCommandRunner()
    probe = LocalManagedScienceRuntimeProbe(
        command_runner=runner,
        codex_auth_path=tmp_path / "missing-auth.json",
    )

    readiness = probe.verify(
        ManagedScienceRuntimeRequest(
            runtime_image="openevo/science-runtime:0.1.0",
            codex_model="gpt-5.1-codex-mini",
        ),
        time.monotonic() + 1,
    )

    assert readiness.ready is False
    assert readiness.code is ServiceRunReadinessCode.CODEX_SUBSCRIPTION_AUTH_UNAVAILABLE
    assert runner.calls == [("codex", "--version")]


def test_real_probe_command_is_cancelled_and_reaped_within_bound() -> None:
    runner = BoundedProbeCommandRunner()
    cancellation = threading.Event()
    results: list[ProbeCommandResult] = []
    thread = threading.Thread(
        target=lambda: results.append(
            runner.run(
                (sys.executable, "-c", "import time; time.sleep(60)"),
                time.monotonic() + 10,
                cancellation,
            )
        )
    )
    thread.start()
    time.sleep(0.05)
    started = time.monotonic()
    cancellation.set()
    thread.join(timeout=1)

    assert time.monotonic() - started < 0.5
    assert not thread.is_alive()
    assert results and results[0].returncode == 130


def test_probe_cancellation_remains_bounded_after_child_closes_output_pipes() -> None:
    runner = BoundedProbeCommandRunner()
    cancellation = threading.Event()
    results: list[ProbeCommandResult] = []
    thread = threading.Thread(
        target=lambda: results.append(
            runner.run(
                (
                    sys.executable,
                    "-c",
                    "import os,time;os.close(1);os.close(2);time.sleep(60)",
                ),
                time.monotonic() + 10,
                cancellation,
            )
        )
    )
    thread.start()
    time.sleep(0.05)

    cancellation.set()
    thread.join(timeout=1)

    assert not thread.is_alive()
    assert results and results[0].returncode == 130


def test_probe_output_aggregate_limit_kills_and_reaps_entire_process_group(
    tmp_path: Path,
) -> None:
    grandchild_pid_path = tmp_path / "probe-grandchild.pid"
    code = (
        "import os,pathlib,subprocess,sys,time;"
        "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);"
        f"pathlib.Path({str(grandchild_pid_path)!r}).write_text(str(child.pid));"
        "os.write(1,b'o'*3000);os.write(2,b'e'*3000);time.sleep(60)"
    )
    runner = BoundedProbeCommandRunner(max_output_bytes=4096)

    started = time.monotonic()
    result = runner.run((sys.executable, "-c", code), time.monotonic() + 5)

    assert result.returncode == 125
    assert result.stdout == b""
    assert result.stderr == b"bootstrap probe output exceeded its aggregate limit"
    assert time.monotonic() - started < 1
    grandchild_pid = int(grandchild_pid_path.read_text(encoding="ascii"))
    deadline = time.monotonic() + 1
    while _pid_is_live(grandchild_pid) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not _pid_is_live(grandchild_pid)


def test_real_subprocess_backend_smoke_uses_pid_identity_and_bounded_stop() -> None:
    backend = RealSubprocessBackend()
    exits: list[int] = []
    output: list[bytes] = []
    spec = ServiceProcessSpec(
        service_id="smoke",
        display_name="Smoke",
        component=ServiceComponent.EVOLUTION_WORKER,
        argv=(
            sys.executable,
            "-c",
            "import time; print('ready', flush=True); time.sleep(30)",
        ),
        env={"PATH": os.environ.get("PATH", "")},
        argv_digest="c" * 64,
        env_digest="d" * 64,
        identity_digest="e" * 64,
        port=None,
        health_probe=ServiceHealthProbe.process(),
    )
    identity = backend.spawn(
        spec,
        lambda _identity, payload: output.append(payload),
        lambda _identity, returncode: exits.append(returncode),
    )
    try:
        assert identity.pid > 0
        assert len(identity.birth_token) == 64
        assert backend.is_alive(identity)
        deadline = time.monotonic() + 1
        while not output:
            assert time.monotonic() < deadline
            time.sleep(0.005)
        backend.terminate(identity)
        assert backend.wait(identity, 1) is not None
        assert not backend.is_alive(identity)
    finally:
        if backend.is_alive(identity):
            backend.kill(identity)
            backend.wait(identity, 1)


@pytest.mark.parametrize("_attempt", range(3))
def test_real_subprocess_tracked_capacity_reclaims_only_reaped_groups(_attempt: int) -> None:
    backend = RealSubprocessBackend(max_tracked_processes=1)
    long_running = _sleep_process_spec("capacity-live", "7" * 64, 60)
    first = backend.spawn(long_running, lambda *_args: None, lambda *_args: None)
    try:
        with pytest.raises(SupervisorStateError, match="capacity"):
            backend.spawn(
                _sleep_process_spec("capacity-rejected", "8" * 64, 60),
                lambda *_args: None,
                lambda *_args: None,
            )
        backend.kill(first)
        assert backend.wait(first, 1) is not None

        for index in range(4):
            exited = threading.Event()
            identity = backend.spawn(
                _sleep_process_spec(f"reaped-{index}", f"{index + 1:064x}", 0),
                lambda *_args: None,
                lambda *_args: exited.set(),
            )
            assert backend.wait(identity, 1) == 0
            assert exited.wait(1)
    finally:
        if backend.is_alive(first):
            backend.kill(first)
            backend.wait(first, 1)


def test_restart_operation_capacity_preserves_replay_and_rejects_conflicts(
    tmp_path: Path,
    framework_lock: Path,
) -> None:
    supervisor, backend, _, _ = _supervisor(
        tmp_path,
        framework_lock,
        max_restart_operations=2,
    )
    try:
        _ensure_subscription(supervisor)
        first = supervisor.restart("gateway", operation_id="bounded-op-1")
        supervisor.restart("gateway", operation_id="bounded-op-2")
        spawn_count = len(backend.spawned)

        assert supervisor.restart("gateway", operation_id="bounded-op-1") == first
        assert len(backend.spawned) == spawn_count
        with pytest.raises(SupervisorStateError, match="different request"):
            supervisor.restart("rollout", operation_id="bounded-op-1")
        with pytest.raises(SupervisorBusyError, match="capacity"):
            supervisor.restart("gateway", operation_id="bounded-op-3")
        assert len(backend.spawned) == spawn_count
    finally:
        supervisor.close()


def test_real_process_group_reaps_child_and_grandchild() -> None:
    backend = RealSubprocessBackend()
    output: list[bytes] = []
    spec = _grandchild_probe_spec("d" * 64)
    identity = backend.spawn(
        spec,
        lambda _identity, payload: output.append(payload),
        lambda _identity, _returncode: None,
    )
    try:
        grandchild_pid = _wait_for_probe_pid(output)
        assert _pid_is_live(grandchild_pid)
        backend.terminate(identity)
        if backend.wait(identity, 0.5) is None:
            backend.kill(identity)
        assert backend.wait(identity, 1) is not None
        assert not backend.is_alive(identity)
        assert not _pid_is_live(grandchild_pid)
    finally:
        if backend.is_alive(identity):
            backend.kill(identity)
            backend.wait(identity, 1)


def test_real_subprocess_inherits_credential_and_prebound_listener_fds() -> None:
    backend = RealSubprocessBackend()
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = int(listener.getsockname()[1])
    internal_identity = InternalServiceIdentity(
        service_id="fd-probe",
        generation_digest="1" * 64,
        registry_digest="2" * 64,
        framework_lock_digest="3" * 64,
        credential="real-fd-credential-value-0123456789abcdef",
    )
    code = (
        "import json,socket,time;"
        "from openevo.internal_auth import inherited_listen_fd,read_internal_service_identity;"
        "identity=read_internal_service_identity(required=True,expected_service_id='fd-probe');"
        "sock=socket.socket(fileno=inherited_listen_fd());"
        "print(json.dumps({'auth':identity.auth_digest,'port':sock.getsockname()[1]}),flush=True);"
        "time.sleep(60)"
    )
    output: list[bytes] = []
    spec = ServiceProcessSpec(
        service_id="fd-probe",
        display_name="FD probe",
        component=ServiceComponent.EVOLUTION_WORKER,
        argv=(sys.executable, "-c", code),
        env={"PATH": os.environ.get("PATH", "")},
        argv_digest="4" * 64,
        env_digest="5" * 64,
        identity_digest="6" * 64,
        port=port,
        health_probe=ServiceHealthProbe.process(),
        internal_identity=internal_identity,
        listen_fd=listener.fileno(),
    )
    identity = backend.spawn(
        spec,
        lambda _identity, payload: output.append(payload),
        lambda _identity, _returncode: None,
    )
    listener.close()
    try:
        deadline = time.monotonic() + 1
        while not output:
            assert time.monotonic() < deadline
            time.sleep(0.005)
        observed = json.loads(output[0])
        assert observed == {"auth": internal_identity.auth_digest, "port": port}
        assert internal_identity.credential not in repr(spec)
        assert internal_identity.credential not in "\0".join(spec.argv)
        with socket.create_connection(("127.0.0.1", port), timeout=0.2):
            pass
    finally:
        if backend.is_alive(identity):
            backend.kill(identity)
            backend.wait(identity, 1)


def test_real_startup_recovery_reaps_recognized_stale_group() -> None:
    original_owner = RealSubprocessBackend()
    identity = original_owner.spawn(
        _grandchild_probe_spec("f" * 64),
        lambda _identity, _payload: None,
        lambda _identity, _returncode: None,
    )
    recovered_owner = RealSubprocessBackend()
    try:
        assert recovered_owner.recover_stale_group(identity, time.monotonic() + 1.5)
        assert not original_owner.is_alive(identity)
    finally:
        if original_owner.is_alive(identity):
            original_owner.kill(identity)
            original_owner.wait(identity, 1)


def test_real_startup_recovery_refuses_unowned_process_group() -> None:
    backend = RealSubprocessBackend()
    identity = ProcessIdentity(
        pid=os.getpid(),
        birth_token="0" * 64,
        session_id=os.getsid(0),
        process_group_id=os.getpgrp(),
        ownership_digest="1" * 64,
    )

    assert backend.recover_stale_group(identity, time.monotonic() + 0.1) is False
    assert os.getpid() > 0


def _grandchild_probe_spec(identity_digest: str) -> ServiceProcessSpec:
    code = (
        "import subprocess,sys,time;"
        "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);"
        "print(child.pid,flush=True);time.sleep(60)"
    )
    return ServiceProcessSpec(
        service_id="group-probe",
        display_name="Group probe",
        component=ServiceComponent.EVOLUTION_WORKER,
        argv=(sys.executable, "-c", code),
        env={"PATH": os.environ.get("PATH", "")},
        argv_digest="a" * 64,
        env_digest="b" * 64,
        identity_digest=identity_digest,
        port=None,
        health_probe=ServiceHealthProbe.process(),
    )


def _sleep_process_spec(
    service_id: str,
    identity_digest: str,
    seconds: int,
) -> ServiceProcessSpec:
    return ServiceProcessSpec(
        service_id=service_id,
        display_name=service_id,
        component=ServiceComponent.EVOLUTION_WORKER,
        argv=(sys.executable, "-c", f"import time;time.sleep({seconds})"),
        env={"PATH": os.environ.get("PATH", "")},
        argv_digest="a" * 64,
        env_digest="b" * 64,
        identity_digest=identity_digest,
        port=None,
        health_probe=ServiceHealthProbe.process(),
    )


def _wait_for_probe_pid(output: list[bytes]) -> int:
    deadline = time.monotonic() + 1
    while not output:
        assert time.monotonic() < deadline
        time.sleep(0.005)
    return int(output[0].decode("ascii").strip())


def _pid_is_live(pid: int) -> bool:
    try:
        text = (Path("/proc") / str(pid) / "stat").read_text(encoding="ascii")
    except OSError:
        return False
    end = text.rfind(")")
    return text[end + 2 :].split()[0] != "Z"
