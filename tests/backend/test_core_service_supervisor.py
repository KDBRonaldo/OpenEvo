from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import sys
import threading
import time

import pytest

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
    ServiceProcessSpec,
    ServiceReleaseIdentity,
    ServiceStatus,
    SupervisorBusyError,
    SupervisorStateError,
)
from openevo.config import TopologyConfig
from openevo.internal_auth import InternalServiceIdentity


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
            self.callbacks.setdefault(spec.service_id, []).append(
                (identity, on_output, on_exit)
            )
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

    def wait_ready(
        self,
        spec,
        identity,
        process_backend,
        deadline,
        cancellation=None,
    ) -> HealthCheckResult:
        self.checked.append(spec.service_id)
        if self.block_service == spec.service_id:
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

    def verify(self, request, deadline, cancellation=None) -> ManagedScienceRuntimeReadiness:
        assert time.monotonic() < deadline
        assert cancellation is None or not cancellation.is_set()
        self.requests.append(request)
        return ManagedScienceRuntimeReadiness(
            ready=self.ready,
            identity_digest="f" * 64 if self.ready else None,
            message=(
                "Managed Science runtime bootstrap is verified."
                if self.ready
                else "Managed Science runtime image is not prepared."
            ),
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
                    identity_digest=None,
                    message="Managed Science runtime probe was cancelled.",
                )
        raise AssertionError("cancellation did not interrupt the runtime probe")


class FakeProbeCommandRunner:
    def __init__(self, image_id: str = "1" * 64) -> None:
        self.image_id = image_id
        self.calls: list[tuple[str, ...]] = []

    def run(self, argv, deadline, cancellation=None) -> ProbeCommandResult:
        assert time.monotonic() < deadline
        assert cancellation is None or not cancellation.is_set()
        self.calls.append(argv)
        if argv == ("codex", "--version"):
            return ProbeCommandResult(0, b"codex-cli 1.2.3\n", b"")
        payload = [
            {
                "Id": f"sha256:{self.image_id}",
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
) -> tuple[
    CoreServiceSupervisor,
    FakeProcessBackend,
    FakeHealthChecker,
    FakeManagedScienceRuntimeProbe,
]:
    backend = backend or FakeProcessBackend()
    health = health or FakeHealthChecker()
    runtime_probe = runtime_probe or FakeManagedScienceRuntimeProbe()
    supervisor = CoreServiceSupervisor(
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
        startup_timeout=startup_timeout,
        stop_timeout=stop_timeout,
        max_log_entries=max_log_entries,
        max_log_bytes=max_log_bytes,
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


def test_subscription_plan_is_deterministic_and_ready_requires_health_and_identity(
    tmp_path: Path,
    framework_lock: Path,
) -> None:
    supervisor, backend, health, runtime_probe = _supervisor(tmp_path, framework_lock)
    try:
        snapshot = _ensure_subscription(supervisor)
        assert snapshot.services_available is True
        assert snapshot.run_ready is False
        assert snapshot.run_readiness_code == "admission_pinned_run_owner_unavailable"
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
        assert "evolution" not in topology
        assert topology["gateway"]["nodes"][0]["port"] > 0
        assert topology["gateway"]["nodes"][0]["model_served"] == "gpt-5.1-codex-mini"
        assert topology["rollout"]["port"] > 0
        assert parsed_topology.gateway.nodes[0].default_runtime is None
        assert runtime_probe.requests == [
            ManagedScienceRuntimeRequest(
                runtime_image="openevo/science-runtime:0.1.0",
                codex_model="gpt-5.1-codex-mini",
            )
        ]
        assert all("codex" not in part.lower() for spec in backend.spawned for part in spec.argv)
        credential = backend.spawned[0].internal_identity
        assert credential is not None
        assert credential.credential not in repr(backend.spawned[0])
        assert credential.credential not in (tmp_path / "core-services" / "ledger.json").read_text()
        assert credential.credential not in (tmp_path / "core-services" / "topology.json").read_text()
        assert len({service.port for service in snapshot.services if service.port is not None}) == 3
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
    thread = threading.Thread(target=lambda: outcomes.append(_ensure_subscription(supervisor)))
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
    assert readiness.identity_digest is not None
    assert command_runner.calls == [
        ("codex", "--version"),
        ("docker", "image", "inspect", "openevo/science-runtime:0.1.0"),
    ]
    assert "not-read-by-probe" not in readiness.message


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
    assert readiness.identity_digest is None
    assert "symlink" in readiness.message


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
