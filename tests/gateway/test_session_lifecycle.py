from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from openevo.gateway.dispatcher import ManagedSession, SessionDispatcher, SessionStage
from openevo.gateway.node import GatewayNodeManager
from openevo.gateway.session import SessionRegistry
from openevo.gateway.session_files import CredentialRedactor
from openevo.gateway.storage import SessionStore
from openevo.harness.models import AgentSpec
from openevo.rollout.models import SessionDispatchRequest, SessionStatus
from openevo.rollout.timer import StageTimer
from openevo.runtime.base import BaseRuntime
from openevo.runtime.docker import DockerRuntime
from openevo.runtime.models import ExecInput, ExecResult, PrepareAction, RuntimeSpec
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
        assert list(manager._cleanup_journal_dir.glob("*.json")) == []
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
