from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from openevo.gateway.dispatcher import ManagedSession, SessionDispatcher, SessionStage
from openevo.gateway import node as node_module, session_files
from openevo.gateway.node import GatewayNodeManager, GatewayReadinessError
from openevo.gateway.session import SessionRegistry
from openevo.gateway.session_files import CredentialRedactor, HeldCodexCredentialAuthority
from openevo.gateway.storage import SessionStore
from openevo.harness.models import AgentSpec
from openevo.rollout.models import SessionDispatchRequest, SessionStatus
from openevo.rollout.timer import StageTimer
from openevo.runtime.base import BaseRuntime
from openevo.runtime.docker import DockerRuntime
from openevo.runtime.models import ExecInput, ExecResult, PrepareAction, RuntimeSpec
from openevo.runtime.managed import MANAGED_RUNTIME_RELEASES
from openevo.trajectory.models import EvaluatorSpec, StrategySpec
from openevo.trajectory.registry import (
    default_builder_registry,
    default_evaluator_registry,
)


class RecordingRuntime(BaseRuntime):
    def __init__(
        self,
        spec: RuntimeSpec,
        session_id: str,
        session_dir: Path,
        events: list[str],
    ) -> None:
        super().__init__(spec, session_id, session_dir)
        self.events = events

    @property
    def runtime_id(self) -> str:
        return f"recording-{self.session_id}"

    async def start(self) -> None:
        self.events.append("start")

    async def stop(self) -> None:
        self.events.append("stop")

    async def exec(
        self,
        command: str,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_sec: float | None = None,
    ) -> ExecResult:
        self.events.append(f"exec:{command}")
        if command == "run-agent":
            stdout = json.dumps(
                {
                    "type": "message",
                    "message": {
                        "role": "assistant",
                        "content": "lifecycle transcript captured",
                    },
                }
            )
            return ExecResult(stdout=stdout, return_code=0)
        return ExecResult(stdout="prepared", return_code=0)

    async def upload_file(self, local_path: str, remote_path: str) -> None:
        raise AssertionError("upload_file was not requested")

    async def upload_dir(self, local_path: str, remote_path: str) -> None:
        raise AssertionError("upload_dir was not requested")

    async def download_file(self, remote_path: str, local_path: str) -> None:
        raise AssertionError("download_file was not requested")

    async def download_dir(self, remote_path: str, local_path: str) -> None:
        raise AssertionError("download_dir was not requested")


def _subscription_dispatch_request(session_id: str) -> SessionDispatchRequest:
    return SessionDispatchRequest(
        session_id=session_id,
        task_id=f"task-{session_id}",
        instruction="Exercise subscription admission.",
        remaining_timeout_seconds=10,
        runtime=RuntimeSpec(
            profile="managed_science",
            image=MANAGED_RUNTIME_RELEASES["managed_science"].trusted_digest,
            container_user="host",
        ),
        agent=AgentSpec(
            harness="codex",
            settings={"auth_mode": "subscription", "capture_mode": "transcript"},
        ),
    )


async def _accept_managed_image_authority(spec: RuntimeSpec) -> None:
    assert spec.image == MANAGED_RUNTIME_RELEASES["managed_science"].trusted_digest


async def _reject_managed_image_authority(_spec: RuntimeSpec) -> None:
    raise RuntimeError("managed image tag changed")


@pytest.mark.asyncio
async def test_managed_image_tag_mutation_has_zero_session_side_effects(
    tmp_path: Path,
) -> None:
    auth = tmp_path / "home" / ".codex" / "auth.json"
    auth.parent.mkdir(parents=True)
    auth.write_text('{"access_token":"image-race-secret"}\n', encoding="utf-8")
    auth.chmod(0o600)
    authority = HeldCodexCredentialAuthority.open(auth)
    registry = SessionRegistry()
    storage = SessionStore()
    manager = GatewayNodeManager(
        node_id="image-race",
        gateway_url="http://gateway.test",
        max_init_workers=1,
        max_run_workers=1,
        max_postrun_workers=1,
        storage=storage,
        session_registry=registry,
        builders=default_builder_registry(),
        evaluators=default_evaluator_registry(),
        session_base_dir=str(tmp_path),
        credential_authority=authority,
        managed_image_authority_verifier=_reject_managed_image_authority,
    )
    before = {path.relative_to(tmp_path) for path in tmp_path.rglob("*")}
    before_fds = len(os.listdir("/proc/self/fd"))
    try:
        with pytest.raises(GatewayReadinessError, match="image authority"):
            await manager.dispatch(_subscription_dispatch_request("image-tag-race"))
        assert registry.get("image-tag-race") is None
        assert storage.get_session_metadata("image-tag-race") is None
        assert (await manager._dispatcher.snapshot()).active_count == 0
        assert {path.relative_to(tmp_path) for path in tmp_path.rglob("*")} == before
        assert len(os.listdir("/proc/self/fd")) == before_fds
    finally:
        await manager._client.aclose()
        authority.close()


@pytest.mark.asyncio
async def test_closed_dispatcher_is_typed_and_rolls_back_all_session_side_effects(
    tmp_path: Path,
) -> None:
    auth = tmp_path / "home" / ".codex" / "auth.json"
    auth.parent.mkdir(parents=True)
    auth.write_text('{"access_token":"dispatcher-secret"}\n', encoding="utf-8")
    auth.chmod(0o600)
    authority = HeldCodexCredentialAuthority.open(auth)
    registry = SessionRegistry()
    storage = SessionStore()
    manager = GatewayNodeManager(
        node_id="dispatcher-closed",
        gateway_url="http://gateway.test",
        max_init_workers=1,
        max_run_workers=1,
        max_postrun_workers=1,
        storage=storage,
        session_registry=registry,
        builders=default_builder_registry(),
        evaluators=default_evaluator_registry(),
        session_base_dir=str(tmp_path),
        credential_authority=authority,
        managed_image_authority_verifier=_accept_managed_image_authority,
    )
    before = {path.relative_to(tmp_path) for path in tmp_path.rglob("*")}
    try:
        with pytest.raises(GatewayReadinessError, match="dispatcher"):
            await manager.dispatch(_subscription_dispatch_request("dispatcher-closed"))
        assert registry.get("dispatcher-closed") is None
        assert storage.get_session_metadata("dispatcher-closed") is None
        assert (await manager._dispatcher.snapshot()).active_count == 0
        assert {path.relative_to(tmp_path) for path in tmp_path.rglob("*")} == before
    finally:
        await manager._client.aclose()
        authority.close()


@pytest.mark.asyncio
async def test_dispatcher_shutdown_after_reservation_rejects_enqueue_and_rolls_back(
    tmp_path: Path,
) -> None:
    auth = tmp_path / "home" / ".codex" / "auth.json"
    auth.parent.mkdir(parents=True)
    auth.write_text('{"access_token":"dispatcher-race-secret"}\n', encoding="utf-8")
    auth.chmod(0o600)
    authority = HeldCodexCredentialAuthority.open(auth)
    registry = SessionRegistry()
    storage = SessionStore()
    manager = GatewayNodeManager(
        node_id="dispatcher-shutdown-race",
        gateway_url="http://gateway.test",
        max_init_workers=1,
        max_run_workers=1,
        max_postrun_workers=1,
        storage=storage,
        session_registry=registry,
        builders=default_builder_registry(),
        evaluators=default_evaluator_registry(),
        session_base_dir=str(tmp_path),
        credential_authority=authority,
        managed_image_authority_verifier=_accept_managed_image_authority,
    )
    manager._dispatcher._started = True
    manager._dispatcher._accepting = True
    real_enqueue = manager._dispatcher.enqueue
    stop_task: asyncio.Task[list[ManagedSession]] | None = None

    async def stop_before_enqueue(managed: ManagedSession, *, admission) -> None:
        nonlocal stop_task
        stop_task = asyncio.create_task(manager._dispatcher.stop())
        await asyncio.sleep(0)
        assert manager._dispatcher._accepting is False
        await real_enqueue(managed, admission=admission)

    manager._dispatcher.enqueue = stop_before_enqueue  # type: ignore[method-assign]
    try:
        with pytest.raises(GatewayReadinessError, match="dispatcher"):
            await manager.dispatch(_subscription_dispatch_request("dispatcher-race"))
        assert stop_task is not None
        assert await stop_task == []
        assert registry.get("dispatcher-race") is None
        assert storage.get_session_metadata("dispatcher-race") is None
        assert (await manager._dispatcher.snapshot()).active_count == 0
        assert list(tmp_path.glob("session-dispatch-*")) == []
        assert list(tmp_path.glob("credentials-dispatch-*")) == []
    finally:
        await manager._client.aclose()
        authority.close()


@pytest.mark.asyncio
async def test_dispatcher_queue_failure_is_typed_and_rolls_back_session_state(
    tmp_path: Path,
) -> None:
    class FailingQueue:
        def put_nowait(self, _session_id: str) -> None:
            raise RuntimeError("injected queue publication failure")

    auth = tmp_path / "home" / ".codex" / "auth.json"
    auth.parent.mkdir(parents=True)
    auth.write_text('{"access_token":"queue-failure-secret"}\n', encoding="utf-8")
    auth.chmod(0o600)
    authority = HeldCodexCredentialAuthority.open(auth)
    registry = SessionRegistry()
    storage = SessionStore()
    manager = GatewayNodeManager(
        node_id="dispatcher-queue-failure",
        gateway_url="http://gateway.test",
        max_init_workers=1,
        max_run_workers=1,
        max_postrun_workers=1,
        storage=storage,
        session_registry=registry,
        builders=default_builder_registry(),
        evaluators=default_evaluator_registry(),
        session_base_dir=str(tmp_path),
        credential_authority=authority,
        managed_image_authority_verifier=_accept_managed_image_authority,
    )
    manager._dispatcher._started = True
    manager._dispatcher._accepting = True
    manager._dispatcher._init_queue = FailingQueue()  # type: ignore[assignment]
    try:
        with pytest.raises(GatewayReadinessError, match="dispatcher"):
            await manager.dispatch(_subscription_dispatch_request("queue-failure"))
        assert registry.get("queue-failure") is None
        assert storage.get_session_metadata("queue-failure") is None
        assert (await manager._dispatcher.snapshot()).active_count == 0
        assert list(tmp_path.glob("session-queue-fa-*")) == []
        assert list(tmp_path.glob("credentials-queue-fa-*")) == []
    finally:
        manager._dispatcher._started = False
        manager._dispatcher._accepting = False
        await manager._client.aclose()
        authority.close()


@pytest.mark.asyncio
async def test_subscription_authority_race_has_zero_session_side_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    auth = tmp_path / "home" / ".codex" / "auth.json"
    auth.parent.mkdir(parents=True)
    auth.write_text('{"access_token":"original-secret"}\n', encoding="utf-8")
    auth.chmod(0o600)
    authority = HeldCodexCredentialAuthority.open(auth)
    registry = SessionRegistry()
    storage = SessionStore()
    manager = GatewayNodeManager(
        node_id="credential-race",
        gateway_url="http://gateway.test",
        max_init_workers=1,
        max_run_workers=1,
        max_postrun_workers=1,
        storage=storage,
        session_registry=registry,
        builders=default_builder_registry(),
        evaluators=default_evaluator_registry(),
        session_base_dir=str(tmp_path),
        credential_authority=authority,
        managed_image_authority_verifier=_accept_managed_image_authority,
    )
    before = {path.relative_to(tmp_path) for path in tmp_path.rglob("*")}
    original_copy = session_files._copy_exact
    replaced = False

    def replace_during_snapshot(source_fd: int, target_fd: int, size: int) -> None:
        nonlocal replaced
        original_copy(source_fd, target_fd, size)
        if not replaced:
            replaced = True
            replacement = auth.with_name("auth.replacement")
            replacement.write_text('{"access_token":"replacement"}\n', encoding="utf-8")
            replacement.chmod(0o600)
            os.replace(replacement, auth)

    monkeypatch.setattr(session_files, "_copy_exact", replace_during_snapshot)
    try:
        with pytest.raises(GatewayReadinessError):
            await manager.dispatch(_subscription_dispatch_request("authority-race"))
        assert registry.get("authority-race") is None
        assert storage.get_session_metadata("authority-race") is None
        assert (await manager._dispatcher.snapshot()).active_count == 0
        assert {path.relative_to(tmp_path) for path in tmp_path.rglob("*")} == before
    finally:
        await manager._client.aclose()
        authority.close()


@pytest.mark.asyncio
async def test_closed_subscription_authority_has_zero_session_side_effects(
    tmp_path: Path,
) -> None:
    auth = tmp_path / "home" / ".codex" / "auth.json"
    auth.parent.mkdir(parents=True)
    auth.write_text('{"access_token":"closed-secret"}\n', encoding="utf-8")
    auth.chmod(0o600)
    authority = HeldCodexCredentialAuthority.open(auth)
    manager = GatewayNodeManager(
        node_id="credential-closed",
        gateway_url="http://gateway.test",
        max_init_workers=1,
        max_run_workers=1,
        max_postrun_workers=1,
        storage=SessionStore(),
        session_registry=SessionRegistry(),
        builders=default_builder_registry(),
        evaluators=default_evaluator_registry(),
        session_base_dir=str(tmp_path),
        credential_authority=authority,
        managed_image_authority_verifier=_accept_managed_image_authority,
    )
    before = {path.relative_to(tmp_path) for path in tmp_path.rglob("*")}
    authority.close()
    try:
        with pytest.raises(GatewayReadinessError):
            await manager.dispatch(_subscription_dispatch_request("closed-authority"))
        assert manager.session_registry.get("closed-authority") is None
        assert manager.storage.get_session_metadata("closed-authority") is None
        assert (await manager._dispatcher.snapshot()).active_count == 0
        assert {path.relative_to(tmp_path) for path in tmp_path.rglob("*")} == before
    finally:
        await manager._client.aclose()


@pytest.mark.asyncio
async def test_subscription_publication_failure_rolls_back_session_side_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    auth = tmp_path / "home" / ".codex" / "auth.json"
    auth.parent.mkdir(parents=True)
    auth.write_text('{"access_token":"publication-secret"}\n', encoding="utf-8")
    auth.chmod(0o600)
    authority = HeldCodexCredentialAuthority.open(auth)
    manager = GatewayNodeManager(
        node_id="credential-publication",
        gateway_url="http://gateway.test",
        max_init_workers=1,
        max_run_workers=1,
        max_postrun_workers=1,
        storage=SessionStore(),
        session_registry=SessionRegistry(),
        builders=default_builder_registry(),
        evaluators=default_evaluator_registry(),
        session_base_dir=str(tmp_path),
        credential_authority=authority,
        managed_image_authority_verifier=_accept_managed_image_authority,
    )
    manager._dispatcher._started = True
    manager._dispatcher._accepting = True

    def fail_publication(*_args, **_kwargs) -> None:
        raise OSError("injected renameat2 failure")

    monkeypatch.setattr(session_files, "_rename_noreplace", fail_publication)
    try:
        with pytest.raises(GatewayReadinessError):
            await manager.dispatch(_subscription_dispatch_request("publication-failure"))
        assert manager.session_registry.get("publication-failure") is None
        assert manager.storage.get_session_metadata("publication-failure") is None
        assert (await manager._dispatcher.snapshot()).active_count == 0
        assert list(tmp_path.glob("session-publicat-*")) == []
        assert list(tmp_path.glob("credentials-publicat-*")) == []
        journal_records = [
            json.loads(path.read_text(encoding="utf-8"))
            for path in tmp_path.rglob("*.json")
            if path.name != "auth.json"
        ]
        assert any(
            record.get("kind") == "retired"
            and record.get("session_id") == "publication-failure"
            for record in journal_records
        )
    finally:
        await manager._client.aclose()
        authority.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure_point",
    ["session_root", "log_authority", "registry", "storage", "journal"],
)
async def test_admission_publication_failures_are_typed_and_fully_rolled_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
) -> None:
    secret = f"{failure_point}-private-publication-detail"
    auth = tmp_path / "home" / ".codex" / "auth.json"
    auth.parent.mkdir(parents=True)
    auth.write_text('{"access_token":"publication-secret"}\n', encoding="utf-8")
    auth.chmod(0o600)
    authority = HeldCodexCredentialAuthority.open(auth)
    manager = GatewayNodeManager(
        node_id=f"publication-{failure_point}",
        gateway_url="http://gateway.test",
        max_init_workers=1,
        max_run_workers=1,
        max_postrun_workers=1,
        storage=SessionStore(),
        session_registry=SessionRegistry(),
        builders=default_builder_registry(),
        evaluators=default_evaluator_registry(),
        session_base_dir=str(tmp_path),
        credential_authority=authority,
        managed_image_authority_verifier=_accept_managed_image_authority,
    )
    manager._dispatcher._started = True
    manager._dispatcher._accepting = True

    def fail(*_args, **_kwargs):
        raise OSError(secret)

    if failure_point == "session_root":
        monkeypatch.setattr(node_module, "mkdtemp", fail)
    elif failure_point == "log_authority":
        monkeypatch.setattr(node_module, "create_session_log_authority", fail)
    elif failure_point == "registry":
        monkeypatch.setattr(manager.session_registry, "register", fail)
    elif failure_point == "storage":
        monkeypatch.setattr(manager.storage, "ensure_session", fail)
    else:
        monkeypatch.setattr(manager, "_persist_cleanup_ownership", fail)

    session_id = f"publication-{failure_point}"
    try:
        with pytest.raises(GatewayReadinessError) as raised:
            await manager.dispatch(_subscription_dispatch_request(session_id))
        assert secret not in str(raised.value)
        assert manager.session_registry.get(session_id) is None
        assert manager.storage.get_session_metadata(session_id) is None
        assert manager._dispatcher._admission_tokens == set()
        assert (await manager._dispatcher.snapshot()).active_count == 0
        assert list(tmp_path.glob(f"session-{session_id[:8]}-*")) == []
        assert list(tmp_path.glob(f"credentials-{session_id[:8]}-*")) == []
    finally:
        manager._dispatcher._started = False
        manager._dispatcher._accepting = False
        await manager._client.aclose()
        authority.close()


@pytest.mark.asyncio
async def test_admission_rollback_attempts_registry_after_storage_cleanup_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    auth = tmp_path / "home" / ".codex" / "auth.json"
    auth.parent.mkdir(parents=True)
    auth.write_text('{"access_token":"rollback-secret"}\n', encoding="utf-8")
    auth.chmod(0o600)
    authority = HeldCodexCredentialAuthority.open(auth)
    manager = GatewayNodeManager(
        node_id="independent-rollback",
        gateway_url="http://gateway.test",
        max_init_workers=1,
        max_run_workers=1,
        max_postrun_workers=1,
        storage=SessionStore(),
        session_registry=SessionRegistry(),
        builders=default_builder_registry(),
        evaluators=default_evaluator_registry(),
        session_base_dir=str(tmp_path),
        credential_authority=authority,
        managed_image_authority_verifier=_accept_managed_image_authority,
    )
    manager._dispatcher._started = True
    manager._dispatcher._accepting = True
    removed: list[str] = []
    real_remove = manager.session_registry.remove

    def fail_journal(*_args, **_kwargs) -> None:
        raise OSError("journal publication failed")

    def fail_storage_cleanup(_session_id: str) -> int:
        raise OSError("storage cleanup failed")

    def record_registry_cleanup(session_id: str) -> None:
        removed.append(session_id)
        real_remove(session_id)

    monkeypatch.setattr(manager, "_persist_cleanup_ownership", fail_journal)
    monkeypatch.setattr(manager.storage, "delete_session", fail_storage_cleanup)
    monkeypatch.setattr(manager.session_registry, "remove", record_registry_cleanup)
    try:
        with pytest.raises(GatewayReadinessError):
            await manager.dispatch(_subscription_dispatch_request("independent-rollback"))
        assert removed == ["independent-rollback"]
        assert manager.session_registry.get("independent-rollback") is None
        assert manager._dispatcher._admission_tokens == set()
        assert list(tmp_path.glob("session-independ-*")) == []
        assert list(tmp_path.glob("credentials-independ-*")) == []
    finally:
        manager._dispatcher._started = False
        manager._dispatcher._accepting = False
        await manager._client.aclose()
        authority.close()


@pytest.mark.asyncio
async def test_subscription_snapshot_is_point_of_no_return_for_source_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = '{"access_token":"original-secret"}\n'
    auth = tmp_path / "home" / ".codex" / "auth.json"
    auth.parent.mkdir(parents=True)
    auth.write_text(original, encoding="utf-8")
    auth.chmod(0o600)
    authority = HeldCodexCredentialAuthority.open(auth)
    manager = GatewayNodeManager(
        node_id="credential-commit",
        gateway_url="http://gateway.test",
        max_init_workers=1,
        max_run_workers=1,
        max_postrun_workers=1,
        storage=SessionStore(),
        session_registry=SessionRegistry(),
        builders=default_builder_registry(),
        evaluators=default_evaluator_registry(),
        session_base_dir=str(tmp_path),
        credential_authority=authority,
        managed_image_authority_verifier=_accept_managed_image_authority,
    )
    manager._dispatcher._started = True
    manager._dispatcher._accepting = True
    real_stage = manager._stage_codex_subscription_auth
    admitted: list[ManagedSession] = []

    def replace_after_snapshot(*args, **kwargs):
        replacement = auth.with_name("auth.replacement")
        replacement.write_text('{"access_token":"replacement"}\n', encoding="utf-8")
        replacement.chmod(0o600)
        os.replace(replacement, auth)
        return real_stage(*args, **kwargs)

    async def capture_enqueue(managed: ManagedSession, *, admission) -> None:
        admitted.append(managed)
        await manager._dispatcher.release_admission(admission)

    monkeypatch.setattr(manager, "_stage_codex_subscription_auth", replace_after_snapshot)
    monkeypatch.setattr(manager._dispatcher, "enqueue", capture_enqueue)
    try:
        await manager.dispatch(_subscription_dispatch_request("snapshot-commit"))
        assert len(admitted) == 1
        managed = admitted[0]
        assert managed.credential_dir is not None
        assert (managed.credential_dir / "auth.json").read_text() == original
        assert manager.session_registry.get("snapshot-commit") is not None
    finally:
        await manager._client.aclose()
        authority.close()


@pytest.mark.asyncio
async def test_subscription_dispatch_cancellation_releases_snapshot_and_session_state(
    tmp_path: Path,
) -> None:
    auth = tmp_path / "home" / ".codex" / "auth.json"
    auth.parent.mkdir(parents=True)
    auth.write_text('{"access_token":"cancellation-secret"}\n', encoding="utf-8")
    auth.chmod(0o600)
    authority = HeldCodexCredentialAuthority.open(auth)
    manager = GatewayNodeManager(
        node_id="credential-cancellation",
        gateway_url="http://gateway.test",
        max_init_workers=1,
        max_run_workers=1,
        max_postrun_workers=1,
        storage=SessionStore(),
        session_registry=SessionRegistry(),
        builders=default_builder_registry(),
        evaluators=default_evaluator_registry(),
        session_base_dir=str(tmp_path),
        credential_authority=authority,
        managed_image_authority_verifier=_accept_managed_image_authority,
    )
    manager._dispatcher._started = True
    manager._dispatcher._accepting = True
    before_fds = len(os.listdir("/proc/self/fd"))
    enqueue_entered = asyncio.Event()
    enqueue_blocked = asyncio.Event()

    async def block_enqueue(_managed: ManagedSession, *, admission) -> None:
        del admission
        enqueue_entered.set()
        await enqueue_blocked.wait()

    manager._dispatcher.enqueue = block_enqueue  # type: ignore[method-assign]
    task = asyncio.create_task(
        manager.dispatch(_subscription_dispatch_request("cancelled-admission"))
    )
    try:
        await asyncio.wait_for(enqueue_entered.wait(), timeout=5)
        assert manager.session_registry.get("cancelled-admission") is not None
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert manager.session_registry.get("cancelled-admission") is None
        assert manager.storage.get_session_metadata("cancelled-admission") is None
        assert (await manager._dispatcher.snapshot()).active_count == 0
        assert list(tmp_path.glob("session-cancelle-*")) == []
        assert list(tmp_path.glob("credentials-cancelle-*")) == []
        journal_records = [
            json.loads(path.read_text(encoding="utf-8"))
            for path in tmp_path.rglob("*.json")
            if path.name != "auth.json"
        ]
        assert any(
            record.get("kind") == "retired"
            and record.get("session_id") == "cancelled-admission"
            for record in journal_records
        )
        assert len(os.listdir("/proc/self/fd")) == before_fds
    finally:
        enqueue_blocked.set()
        manager._dispatcher._started = False
        await manager._client.aclose()
        authority.close()

@pytest.mark.asyncio
async def test_dispatch_runs_runtime_lifecycle_and_builds_stdout_trajectory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    for name in (
        "ALL_PROXY",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "all_proxy",
        "http_proxy",
        "https_proxy",
    ):
        monkeypatch.delenv(name, raising=False)

    def recording_runtime_factory(
        spec: RuntimeSpec,
        session_id: str,
        session_dir: Path,
        *,
        docker_ownership_root: Path | None = None,
    ) -> RecordingRuntime:
        assert docker_ownership_root == manager._docker_ownership_root
        assert not docker_ownership_root.is_relative_to(session_dir)
        events.append("factory")
        return RecordingRuntime(spec, session_id, session_dir, events)

    monkeypatch.setattr(
        "openevo.gateway.node.create_runtime",
        recording_runtime_factory,
    )
    registry = SessionRegistry()
    manager = GatewayNodeManager(
        node_id="gateway-probe",
        gateway_url="http://gateway.test",
        max_init_workers=1,
        max_run_workers=1,
        max_postrun_workers=1,
        storage=SessionStore(),
        session_registry=registry,
        builders=default_builder_registry(),
        evaluators=default_evaluator_registry(),
        session_base_dir=str(tmp_path),
    )
    request = SessionDispatchRequest(
        session_id="lifecycle-probe",
        task_id="task-probe",
        instruction="Exercise the gateway lifecycle.",
        remaining_timeout_seconds=10,
        runtime=RuntimeSpec(
            image="probe-image",
            prepare=[PrepareAction(type="exec", command="prepare-runtime")],
        ),
        agent=AgentSpec(
            harness="shell",
            settings={"capture_mode": "transcript"},
            custom_shell=ExecInput(command="run-agent"),
        ),
        builder=StrategySpec(strategy="agent_transcript"),
    )

    await manager.start()
    try:
        await manager.dispatch(request)
        async with asyncio.timeout(5):
            while True:
                session = registry.get(request.session_id)
                if session is not None and session.status in SessionStatus.terminal():
                    break
                await asyncio.sleep(0.01)
        async with asyncio.timeout(5):
            while await manager.active_sessions() != 0:
                await asyncio.sleep(0.01)

        assert session is not None
        assert session.result is not None
        assert session.result.status == SessionStatus.COMPLETED
        trajectory = session.result.trajectory
        assert trajectory.status == "COMPLETED"
        assert trajectory.metadata["builder"] == "agent_transcript"
        assert trajectory.metadata["capture_mode"] == "transcript"
        assert trajectory.metadata["token_level_metrics_available"] is False
        assert trajectory.traces[0].prompt_messages == [
            {"role": "user", "content": request.instruction}
        ]
        assert trajectory.traces[0].response_messages == [
            {
                "role": "assistant",
                "content": "lifecycle transcript captured",
            }
        ]
        assert manager.storage.get_session_metadata(request.session_id) is None
        assert {path.name for path in tmp_path.iterdir()} == {".openevo-gateway-cleanup"}
        records = list(manager._cleanup_journal_dir.glob("*.json"))
        assert len(records) == 1
        retired = json.loads(records[0].read_text(encoding="utf-8"))
        assert retired["version"] == 9
        assert retired["kind"] == "retired"
        assert retired["epoch"] >= 0
        assert retired["retired_epoch"] >= retired["epoch"]
        assert list(manager._cleanup_journal_dir.parent.glob(".*.root.json"))
    finally:
        await manager.close()

    assert events == [
        "factory",
        "start",
        "exec:prepare-runtime",
        "exec:run-agent",
        "stop",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("symlink_position", ["parent", "final"])
async def test_init_rejects_prepare_target_symlink_before_runtime_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    symlink_position: str,
) -> None:
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    source = tmp_path / "source.txt"
    source.write_text("source", encoding="utf-8")
    if symlink_position == "parent":
        (session_dir / "workspace").symlink_to(outside, target_is_directory=True)
        target = "/openevo/session/workspace/target.txt"
    else:
        (session_dir / "target.txt").symlink_to(outside / "target.txt")
        target = "/openevo/session/target.txt"
    request = SessionDispatchRequest(
        session_id="prepare-admission",
        task_id="task",
        instruction="Do work.",
        remaining_timeout_seconds=60,
        runtime=RuntimeSpec(
            image="runtime:latest",
            prepare=[
                PrepareAction(
                    type="upload_file",
                    source=str(source),
                    target=target,
                )
            ],
        ),
        agent=AgentSpec(harness="shell", custom_shell=ExecInput(command="true")),
    )
    managed = ManagedSession(
        request=request,
        timer=StageTimer(),
        session_dir=session_dir,
        artifacts_dir=session_dir / "artifacts",
        session_root_identity=(
            session_dir.stat().st_dev,
            session_dir.stat().st_ino,
            session_dir.stat().st_uid,
        ),
    )
    manager = GatewayNodeManager.__new__(GatewayNodeManager)
    manager.node_id = "gateway-test"
    manager.default_runtime = None
    manager._docker_ownership_root = tmp_path / "docker-authority"
    runtime_factory_called = False

    def reject_runtime_factory(*args, **kwargs):
        nonlocal runtime_factory_called
        del args, kwargs
        runtime_factory_called = True
        raise AssertionError("unsafe prepare target reached runtime construction")

    monkeypatch.setattr("openevo.gateway.node.create_runtime", reject_runtime_factory)

    await manager._handle_init(managed)

    assert runtime_factory_called is False
    assert managed.final_result is not None
    assert managed.final_result.status == SessionStatus.ERROR
    assert "prepare target" in (managed.final_result.error or "")
    assert list(outside.iterdir()) == []


@pytest.mark.asyncio
async def test_eval_runtime_uses_core_authority_outside_main_session_mount(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (
        "ALL_PROXY",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "all_proxy",
        "http_proxy",
        "https_proxy",
    ):
        monkeypatch.delenv(name, raising=False)
    events: list[str] = []
    manager = GatewayNodeManager(
        node_id="eval-authority-probe",
        gateway_url="http://gateway.test",
        max_init_workers=1,
        max_run_workers=1,
        max_postrun_workers=1,
        storage=SessionStore(),
        session_registry=SessionRegistry(),
        builders=default_builder_registry(),
        evaluators=default_evaluator_registry(),
        session_base_dir=str(tmp_path),
    )
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    runtime_spec = RuntimeSpec(image="runtime:latest")
    request = SessionDispatchRequest(
        session_id="eval-authority",
        task_id="task",
        instruction="work",
        remaining_timeout_seconds=10,
        runtime=runtime_spec,
        agent=AgentSpec(harness="shell", custom_shell=ExecInput(command="true")),
        evaluator=EvaluatorSpec(strategy="pass_at_k", refresh_runtime=True),
    )
    managed = ManagedSession(
        request=request,
        timer=StageTimer(),
        session_dir=session_dir,
        artifacts_dir=session_dir / "artifacts",
    )
    managed.execution_deadline = asyncio.get_running_loop().time() + 10

    def recording_runtime_factory(
        spec: RuntimeSpec,
        session_id: str,
        runtime_session_dir: Path,
        *,
        docker_ownership_root: Path | None = None,
    ) -> RecordingRuntime:
        assert docker_ownership_root == manager._docker_ownership_root
        assert runtime_session_dir.is_relative_to(session_dir)
        assert not docker_ownership_root.is_relative_to(session_dir)
        return RecordingRuntime(spec, session_id, runtime_session_dir, events)

    monkeypatch.setattr("openevo.gateway.node.create_runtime", recording_runtime_factory)
    try:
        runtime = await manager._prepare_eval_runtime(managed)
    finally:
        await manager._client.aclose()

    assert runtime is managed.eval_runtime
    assert events == ["start"]


@pytest.mark.asyncio
async def test_gateway_start_recovers_private_docker_authority_before_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (
        "ALL_PROXY",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "all_proxy",
        "http_proxy",
        "https_proxy",
    ):
        monkeypatch.delenv(name, raising=False)
    manager = GatewayNodeManager(
        node_id="docker-recovery-probe",
        gateway_url="http://gateway.test",
        max_init_workers=1,
        max_run_workers=1,
        max_postrun_workers=1,
        storage=SessionStore(),
        session_registry=SessionRegistry(),
        builders=default_builder_registry(),
        evaluators=default_evaluator_registry(),
        session_base_dir=str(tmp_path),
    )
    crashed = DockerRuntime(
        RuntimeSpec(image="runtime:latest", container_user="host"),
        "crashed-gateway-runtime",
        tmp_path / "old-session",
        ownership_root=manager._docker_ownership_root,
    )
    container_id = "b" * 64
    crashed._prepare_create_ownership()
    crashed._cidfile.write_text(container_id + "\n", encoding="ascii")
    crashed._close_ownership_lock()
    container_present = True
    commands: list[tuple[str, ...]] = []

    async def run_command(self, *args, **kwargs):
        nonlocal container_present
        del self, kwargs
        commands.append(args)
        if args[1:3] == ("container", "inspect"):
            if container_present:
                return 0, container_id + "\n", None
            return 1, None, f"Error: No such object: {container_id}"
        if args[1] == "rm":
            container_present = False
        return 0, None, None

    monkeypatch.setattr(DockerRuntime, "_run_local_command", run_command)
    try:
        await manager.start()
    finally:
        await manager.close()

    assert container_present is False
    assert commands
    assert all(command[-1] == container_id for command in commands)
    assert list(manager._docker_ownership_root.iterdir()) == []


@pytest.mark.asyncio
async def test_dispatcher_shutdown_isolates_cancel_failures(tmp_path: Path) -> None:
    calls: list[str] = []

    class CancelRuntime:
        def __init__(self, name: str, *, fail: bool = False) -> None:
            self.name = name
            self.fail = fail

        async def cancel(self) -> None:
            calls.append(self.name)
            if self.fail:
                raise RuntimeError("cancel failed")

    def managed(session_id: str, runtime: CancelRuntime) -> ManagedSession:
        return ManagedSession(
            request=SessionDispatchRequest(
                session_id=session_id,
                task_id="task",
                instruction="work",
                remaining_timeout_seconds=10,
                runtime=RuntimeSpec(image="runtime:latest"),
                agent=AgentSpec(harness="shell", custom_shell=ExecInput(command="true")),
            ),
            timer=StageTimer(),
            session_dir=tmp_path / session_id,
            artifacts_dir=tmp_path / session_id / "artifacts",
            runtime=runtime,  # type: ignore[arg-type]
        )

    dispatcher = SessionDispatcher(
        max_init_workers=1,
        max_run_workers=1,
        max_postrun_workers=1,
    )
    dispatcher._started = True
    dispatcher._sessions = {
        "first": managed("first", CancelRuntime("first", fail=True)),
        "second": managed("second", CancelRuntime("second")),
    }

    await dispatcher.stop()

    assert calls == ["first", "second"]


@pytest.mark.asyncio
async def test_credential_capable_dispatcher_boundaries_omit_traceback_canary(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "dispatcher-boundary-traceback-canary"

    class CanaryFailure(RuntimeError):
        pass

    class FailingRuntime:
        async def cancel(self) -> None:
            traceback_local = secret
            raise CanaryFailure(traceback_local) from ValueError(secret)

    managed = ManagedSession(
        request=SessionDispatchRequest(
            session_id="credential-dispatcher",
            task_id="task",
            instruction="work",
            remaining_timeout_seconds=10,
            runtime=RuntimeSpec(image="runtime:latest"),
            agent=AgentSpec(
                harness="codex",
                settings={"auth_mode": "subscription", "capture_mode": "transcript"},
            ),
        ),
        timer=StageTimer(),
        session_dir=tmp_path / "credential-dispatcher",
        artifacts_dir=tmp_path / "credential-dispatcher" / "artifacts",
        credential_redactor=CredentialRedactor.from_auth_json(
            f'{{"access_token":"{secret}"}}'.encode()
        ),
        runtime=FailingRuntime(),  # type: ignore[arg-type]
        stage=SessionStage.RUNNING,
    )
    dispatcher = SessionDispatcher(
        max_init_workers=1,
        max_run_workers=1,
        max_postrun_workers=1,
    )

    async def fail_stage(_: ManagedSession) -> None:
        traceback_local = secret
        raise CanaryFailure(traceback_local) from ValueError(secret)

    def fail_stage_change(_: ManagedSession) -> None:
        traceback_local = secret
        raise CanaryFailure(traceback_local) from ValueError(secret)

    with caplog.at_level("WARNING", logger="openevo.gateway.dispatcher"):
        dispatcher._started = True
        dispatcher._sessions[managed.session_id] = managed
        await dispatcher.stop()

        dispatcher._started = True
        dispatcher._sessions[managed.session_id] = managed
        managed.cancel_requested = False
        await dispatcher.cancel(managed.session_id)

        await dispatcher._safe_invoke(fail_stage, managed, SessionStage.RUNNING)
        dispatcher.on_stage_change = fail_stage_change
        dispatcher._notify_stage_change(managed)

    assert secret not in caplog.text
    assert "Traceback" not in caplog.text
    assert caplog.text.count("CanaryFailure") == 4


@pytest.mark.asyncio
async def test_manager_shutdown_reconciles_session_without_created_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (
        "ALL_PROXY",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "all_proxy",
        "http_proxy",
        "https_proxy",
    ):
        monkeypatch.delenv(name, raising=False)
    session_dir = tmp_path / "runtime-create-failed"
    session_dir.mkdir()
    managed = ManagedSession(
        request=SessionDispatchRequest(
            session_id="runtime-create-failed",
            task_id="task",
            instruction="work",
            remaining_timeout_seconds=10,
            runtime=RuntimeSpec(image="runtime:latest"),
            agent=AgentSpec(harness="shell", custom_shell=ExecInput(command="true")),
        ),
        timer=StageTimer(),
        session_dir=session_dir,
        artifacts_dir=session_dir / "artifacts",
        session_root_identity=(
            session_dir.stat().st_dev,
            session_dir.stat().st_ino,
            session_dir.stat().st_uid,
        ),
    )
    manager = GatewayNodeManager(
        node_id="shutdown-probe",
        gateway_url="http://gateway.test",
        max_init_workers=1,
        max_run_workers=1,
        max_postrun_workers=1,
        storage=SessionStore(),
        session_registry=SessionRegistry(),
        builders=default_builder_registry(),
        evaluators=default_evaluator_registry(),
        session_base_dir=str(tmp_path),
    )
    manager._dispatcher._started = True
    manager._dispatcher._sessions[managed.session_id] = managed

    await manager.close()

    assert not session_dir.exists()
    assert manager._cleanup_retries == {}


@pytest.mark.asyncio
async def test_manager_shutdown_reconciles_independent_eval_prewarm_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (
        "ALL_PROXY",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "all_proxy",
        "http_proxy",
        "https_proxy",
    ):
        monkeypatch.delenv(name, raising=False)
    events: list[str] = []
    session_dir = tmp_path / "eval-prewarm"
    session_dir.mkdir()
    eval_runtime = RecordingRuntime(
        RuntimeSpec(image="runtime:latest"),
        "eval-prewarm-runtime",
        session_dir / "eval_runtime",
        events,
    )

    async def prepared_eval_runtime() -> BaseRuntime:
        return eval_runtime

    managed = ManagedSession(
        request=SessionDispatchRequest(
            session_id="eval-prewarm",
            task_id="task",
            instruction="work",
            remaining_timeout_seconds=10,
            runtime=RuntimeSpec(image="runtime:latest"),
            agent=AgentSpec(harness="shell", custom_shell=ExecInput(command="true")),
        ),
        timer=StageTimer(),
        session_dir=session_dir,
        artifacts_dir=session_dir / "artifacts",
        session_root_identity=(
            session_dir.stat().st_dev,
            session_dir.stat().st_ino,
            session_dir.stat().st_uid,
        ),
        eval_prewarm_task=asyncio.create_task(prepared_eval_runtime()),
    )
    manager = GatewayNodeManager(
        node_id="shutdown-eval-probe",
        gateway_url="http://gateway.test",
        max_init_workers=1,
        max_run_workers=1,
        max_postrun_workers=1,
        storage=SessionStore(),
        session_registry=SessionRegistry(),
        builders=default_builder_registry(),
        evaluators=default_evaluator_registry(),
        session_base_dir=str(tmp_path),
    )
    manager._dispatcher._started = True
    manager._dispatcher._sessions[managed.session_id] = managed
    await asyncio.sleep(0)

    await manager.close()

    assert events == ["stop"]
    assert not session_dir.exists()
    assert manager._cleanup_retries == {}


@pytest.mark.asyncio
async def test_ready_cancel_failure_still_enqueues_postrun_cleanup(tmp_path: Path) -> None:
    class FailingCancelRuntime:
        async def cancel(self) -> None:
            raise RuntimeError("cancel failed")

    dispatcher = SessionDispatcher(
        max_init_workers=1,
        max_run_workers=1,
        max_postrun_workers=1,
    )
    dispatcher._started = True
    managed = ManagedSession(
        request=SessionDispatchRequest(
            session_id="ready-session",
            task_id="task",
            instruction="work",
            remaining_timeout_seconds=10,
            runtime=RuntimeSpec(image="runtime:latest"),
            agent=AgentSpec(harness="shell", custom_shell=ExecInput(command="true")),
        ),
        timer=StageTimer(),
        session_dir=tmp_path / "ready-session",
        artifacts_dir=tmp_path / "ready-session" / "artifacts",
        runtime=FailingCancelRuntime(),  # type: ignore[arg-type]
        stage=SessionStage.READY,
    )
    dispatcher._sessions[managed.session_id] = managed

    assert await dispatcher.cancel(managed.session_id) is True
    assert managed.stage == SessionStage.POSTRUN
    assert await dispatcher._postrun_queue.get() == managed.session_id


@pytest.mark.asyncio
async def test_ready_wait_cancel_does_not_release_unowned_slot(tmp_path: Path) -> None:
    dispatcher = SessionDispatcher(
        max_init_workers=1,
        max_run_workers=1,
        max_postrun_workers=1,
    )
    dispatcher._started = True
    managed = ManagedSession(
        request=SessionDispatchRequest(
            session_id="ready-wait-cancel",
            task_id="task",
            instruction="work",
            remaining_timeout_seconds=10,
            runtime=RuntimeSpec(image="runtime:latest"),
            agent=AgentSpec(harness="shell", custom_shell=ExecInput(command="true")),
        ),
        timer=StageTimer(),
        session_dir=tmp_path / "ready-wait-cancel",
        artifacts_dir=tmp_path / "ready-wait-cancel" / "artifacts",
        stage=SessionStage.READY,
    )
    dispatcher._sessions[managed.session_id] = managed

    await dispatcher._ready_slots.acquire()
    waiter = asyncio.create_task(dispatcher._acquire_ready_slot(managed))
    await asyncio.sleep(0)

    assert await dispatcher.cancel(managed.session_id) is True
    assert await waiter is False
    assert managed.ready_slot_owned is False
    dispatcher._ready_slots.release()

    await asyncio.wait_for(dispatcher._ready_slots.acquire(), timeout=0.1)
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(dispatcher._ready_slots.acquire(), timeout=0.01)


@pytest.mark.asyncio
async def test_ready_acquired_slot_is_released_once_across_repeated_cancel(
    tmp_path: Path,
) -> None:
    dispatcher = SessionDispatcher(
        max_init_workers=1,
        max_run_workers=1,
        max_postrun_workers=1,
    )
    dispatcher._started = True
    managed = ManagedSession(
        request=SessionDispatchRequest(
            session_id="ready-owned-cancel",
            task_id="task",
            instruction="work",
            remaining_timeout_seconds=10,
            runtime=RuntimeSpec(image="runtime:latest"),
            agent=AgentSpec(harness="shell", custom_shell=ExecInput(command="true")),
        ),
        timer=StageTimer(),
        session_dir=tmp_path / "ready-owned-cancel",
        artifacts_dir=tmp_path / "ready-owned-cancel" / "artifacts",
        stage=SessionStage.READY,
    )
    dispatcher._sessions[managed.session_id] = managed

    assert await dispatcher._acquire_ready_slot(managed) is True
    assert managed.ready_slot_owned is True
    assert await dispatcher.cancel(managed.session_id) is True
    assert await dispatcher.cancel(managed.session_id) is True
    assert managed.ready_slot_owned is False

    await asyncio.wait_for(dispatcher._ready_slots.acquire(), timeout=0.1)
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(dispatcher._ready_slots.acquire(), timeout=0.01)


@pytest.mark.asyncio
async def test_ready_acquire_cancel_race_preserves_exact_capacity(tmp_path: Path) -> None:
    dispatcher = SessionDispatcher(
        max_init_workers=1,
        max_run_workers=1,
        max_postrun_workers=1,
    )
    dispatcher._started = True
    managed = ManagedSession(
        request=SessionDispatchRequest(
            session_id="ready-acquire-cancel-race",
            task_id="task",
            instruction="work",
            remaining_timeout_seconds=10,
            runtime=RuntimeSpec(image="runtime:latest"),
            agent=AgentSpec(harness="shell", custom_shell=ExecInput(command="true")),
        ),
        timer=StageTimer(),
        session_dir=tmp_path / "ready-acquire-cancel-race",
        artifacts_dir=tmp_path / "ready-acquire-cancel-race" / "artifacts",
        stage=SessionStage.READY,
    )
    dispatcher._sessions[managed.session_id] = managed

    await dispatcher._ready_slots.acquire()
    waiter = asyncio.create_task(dispatcher._acquire_ready_slot(managed))
    await asyncio.sleep(0)
    dispatcher._ready_slots.release()
    cancel_task = asyncio.create_task(dispatcher.cancel(managed.session_id))

    _, cancelled = await asyncio.gather(waiter, cancel_task)
    assert cancelled is True
    assert managed.ready_slot_owned is False
    await asyncio.wait_for(dispatcher._ready_slots.acquire(), timeout=0.1)
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(dispatcher._ready_slots.acquire(), timeout=0.01)


@pytest.mark.asyncio
async def test_ready_wait_cancel_keeps_other_session_backpressure(tmp_path: Path) -> None:
    dispatcher = SessionDispatcher(
        max_init_workers=1,
        max_run_workers=1,
        max_postrun_workers=1,
    )
    dispatcher._started = True

    def ready_session(session_id: str) -> ManagedSession:
        return ManagedSession(
            request=SessionDispatchRequest(
                session_id=session_id,
                task_id="task",
                instruction="work",
                remaining_timeout_seconds=10,
                runtime=RuntimeSpec(image="runtime:latest"),
                agent=AgentSpec(
                    harness="shell",
                    custom_shell=ExecInput(command="true"),
                ),
            ),
            timer=StageTimer(),
            session_dir=tmp_path / session_id,
            artifacts_dir=tmp_path / session_id / "artifacts",
            stage=SessionStage.READY,
        )

    owner = ready_session("ready-owner")
    waiter_session = ready_session("ready-backpressure-waiter")
    dispatcher._sessions[owner.session_id] = owner
    dispatcher._sessions[waiter_session.session_id] = waiter_session

    assert await dispatcher._acquire_ready_slot(owner) is True
    waiter = asyncio.create_task(dispatcher._acquire_ready_slot(waiter_session))
    await asyncio.sleep(0)
    assert await dispatcher.cancel(waiter_session.session_id) is True
    assert await waiter is False

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(dispatcher._ready_slots.acquire(), timeout=0.01)
    assert dispatcher._release_ready_slot(owner) is True
    await asyncio.wait_for(dispatcher._ready_slots.acquire(), timeout=0.1)


class _DispatcherCancelBaseException(BaseException):
    pass


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure_type",
    [asyncio.CancelledError, _DispatcherCancelBaseException],
)
async def test_ready_cancel_base_exception_enqueues_postrun_before_propagation(
    tmp_path: Path,
    failure_type: type[BaseException],
) -> None:
    class CancellingRuntime:
        async def cancel(self) -> None:
            raise failure_type("runtime cancel interrupted")

    dispatcher = SessionDispatcher(
        max_init_workers=1,
        max_run_workers=1,
        max_postrun_workers=1,
    )
    dispatcher._started = True
    managed = ManagedSession(
        request=SessionDispatchRequest(
            session_id="ready-cancelled-error",
            task_id="task",
            instruction="work",
            remaining_timeout_seconds=10,
            runtime=RuntimeSpec(image="runtime:latest"),
            agent=AgentSpec(harness="shell", custom_shell=ExecInput(command="true")),
        ),
        timer=StageTimer(),
        session_dir=tmp_path / "ready-cancelled-error",
        artifacts_dir=tmp_path / "ready-cancelled-error" / "artifacts",
        runtime=CancellingRuntime(),  # type: ignore[arg-type]
        stage=SessionStage.READY,
    )
    dispatcher._sessions[managed.session_id] = managed

    with pytest.raises(failure_type, match="runtime cancel interrupted"):
        await dispatcher.cancel(managed.session_id)

    assert managed.stage == SessionStage.POSTRUN
    assert await dispatcher._postrun_queue.get() == managed.session_id


class _DispatcherPostrunBaseException(BaseException):
    pass


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure_type",
    [asyncio.CancelledError, _DispatcherPostrunBaseException],
)
async def test_postrun_base_exception_pops_session_without_killing_worker(
    tmp_path: Path,
    failure_type: type[BaseException],
) -> None:
    dispatcher = SessionDispatcher(
        max_init_workers=1,
        max_run_workers=1,
        max_postrun_workers=1,
    )
    completed = asyncio.Event()
    calls: list[str] = []

    async def postrun(managed: ManagedSession) -> None:
        calls.append(managed.session_id)
        if managed.session_id == "postrun-failure":
            raise failure_type("postrun callback interrupted")
        completed.set()

    def make_managed(session_id: str) -> ManagedSession:
        return ManagedSession(
            request=SessionDispatchRequest(
                session_id=session_id,
                task_id="task",
                instruction="work",
                remaining_timeout_seconds=10,
                runtime=RuntimeSpec(image="runtime:latest"),
                agent=AgentSpec(harness="shell", custom_shell=ExecInput(command="true")),
            ),
            timer=StageTimer(),
            session_dir=tmp_path / session_id,
            artifacts_dir=tmp_path / session_id / "artifacts",
            stage=SessionStage.POSTRUN,
        )

    dispatcher.on_postrun = postrun
    await dispatcher.start()
    try:
        for session_id in ("postrun-failure", "postrun-success"):
            dispatcher._sessions[session_id] = make_managed(session_id)
            await dispatcher._postrun_queue.put(session_id)

        await asyncio.wait_for(completed.wait(), timeout=1)
        await asyncio.sleep(0)

        assert calls == ["postrun-failure", "postrun-success"]
        assert dispatcher._sessions == {}
        assert dispatcher._workers[-1].done() is False
    finally:
        await dispatcher.stop()
