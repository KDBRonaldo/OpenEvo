from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from polar.agent.base import BaseHarness
from polar.agent.models import AgentSpec
from polar.config import EvolutionConfig
from polar.gateway.dispatcher import ManagedSession
from polar.gateway.node import (
    GatewayNodeManager,
    build_evolution_session_event,
    write_evolution_context_files,
)
from polar.rollout.models import (
    SessionDispatchRequest,
    SessionResult,
    SessionStatus,
    SessionTiming,
)
from polar.rollout.timer import StageTimer
from polar.runtime.base import BaseRuntime
from polar.runtime.models import ExecInput, ExecResult, RuntimeSpec
from polar.trajectory.models import Trajectory


class FakeHarness(BaseHarness):
    def __init__(self) -> None:
        super().__init__(AgentSpec(harness="fake"))

    def run_steps(self, instruction: str) -> list[ExecInput]:
        return []


class RunStepHarness(BaseHarness):
    def __init__(self, agent_spec: AgentSpec) -> None:
        super().__init__(agent_spec)
        self.env = dict(agent_spec.env)

    def run_steps(self, instruction: str) -> list[ExecInput]:
        return [ExecInput(command="echo run")]


class FakeEvolutionClient:
    def __init__(
        self,
        context: dict | None = None,
        error: Exception | None = None,
        export_error: Exception | None = None,
        export_delay: float = 0.0,
        calls: list[str] | None = None,
    ) -> None:
        self.context = context or {
            "context_id": "ctx_1",
            "memory": {"rendered_text": "Remember parser precedence."},
            "adapter_merge_spec": {"merge_mode": "reference_only"},
        }
        self.error = error
        self.export_error = export_error
        self.export_delay = export_delay
        self.calls = calls
        self.payloads: list[dict] = []
        self.exported_events: list[dict] = []

    async def resolve_context(self, payload: dict) -> dict:
        self.payloads.append(payload)
        if self.error is not None:
            raise self.error
        return self.context

    async def export_event(self, payload: dict) -> dict:
        if self.calls is not None:
            self.calls.append("export")
        self.exported_events.append(payload)
        if self.export_delay:
            await asyncio.sleep(self.export_delay)
        if self.export_error is not None:
            raise self.export_error
        return {"accepted": True}


def test_build_evolution_session_event():
    result = SessionResult(
        session_id="ses_1",
        task_id="task_1",
        status=SessionStatus.COMPLETED,
        trajectory=Trajectory(
            status="COMPLETED",
            traces=[],
            metadata={"model_used": "Qwen/Qwen3.6-27B"},
        ),
        timing=SessionTiming(),
        node_id="node-a",
        metadata={"policy_version": "policy_1", "rollout_step": 4},
    )

    event = build_evolution_session_event(result)

    assert event["source"] == "polar"
    assert event["event_type"] == "polar.session_completed"
    assert event["source_event_id"] == "session:ses_1"
    assert event["policy_version"] == "policy_1"
    assert event["rollout_step"] == 4
    assert event["payload"]["session_result"]["session_id"] == "ses_1"


def test_build_evolution_session_event_preserves_explicit_falsey_metadata():
    result = SessionResult(
        session_id="ses_1",
        task_id="task_1",
        status=SessionStatus.COMPLETED,
        trajectory=Trajectory(
            status="COMPLETED",
            traces=[],
            metadata={
                "model_used": "Qwen/Qwen3.6-27B",
                "policy_version": "trajectory_policy",
                "rollout_step": 9,
            },
        ),
        timing=SessionTiming(),
        node_id="node-a",
        metadata={"policy_version": "", "rollout_step": 0},
    )

    event = build_evolution_session_event(result)

    assert event["policy_version"] == ""
    assert event["rollout_step"] == 0


def _session_result(
    *,
    session_id: str = "ses_1",
    metadata: dict | None = None,
) -> SessionResult:
    return SessionResult(
        session_id=session_id,
        task_id="task_1",
        status=SessionStatus.COMPLETED,
        trajectory=Trajectory(
            status="COMPLETED",
            traces=[],
            metadata={"model_used": "Qwen/Qwen3.6-27B"},
        ),
        timing=SessionTiming(),
        node_id="node-a",
        metadata=metadata or {"policy_version": "policy_1", "rollout_step": 4},
    )


@pytest.mark.asyncio
async def test_export_evolution_event_sends_built_event_when_enabled():
    client = FakeEvolutionClient()
    manager = GatewayNodeManager.__new__(GatewayNodeManager)
    manager.evolution = EvolutionConfig(enabled=True)
    manager.evolution_client = client

    await manager._export_evolution_event(_session_result())

    assert len(client.exported_events) == 1
    assert client.exported_events[0]["event_type"] == "polar.session_completed"
    assert client.exported_events[0]["source_event_id"] == "session:ses_1"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "evolution",
    [
        EvolutionConfig(enabled=False),
        EvolutionConfig(enabled=True, event_export={"enabled": False}),
    ],
)
async def test_export_evolution_event_skips_when_disabled(evolution):
    client = FakeEvolutionClient()
    manager = GatewayNodeManager.__new__(GatewayNodeManager)
    manager.evolution = evolution
    manager.evolution_client = client

    await manager._export_evolution_event(_session_result())

    assert client.exported_events == []


@pytest.mark.asyncio
async def test_export_evolution_event_fail_open_returns(caplog):
    client = FakeEvolutionClient(export_error=RuntimeError("backend down"))
    manager = GatewayNodeManager.__new__(GatewayNodeManager)
    manager.evolution = EvolutionConfig(
        enabled=True,
        event_export={"fail_open": True},
    )
    manager.evolution_client = client

    await manager._export_evolution_event(_session_result())

    assert client.exported_events
    assert "Evolution event export failed for session ses_1" in caplog.text


@pytest.mark.asyncio
async def test_export_evolution_event_fail_closed_raises():
    client = FakeEvolutionClient(export_error=RuntimeError("backend down"))
    manager = GatewayNodeManager.__new__(GatewayNodeManager)
    manager.evolution = EvolutionConfig(
        enabled=True,
        event_export={"fail_open": False},
    )
    manager.evolution_client = client

    with pytest.raises(RuntimeError, match="backend down"):
        await manager._export_evolution_event(_session_result())


@pytest.mark.asyncio
async def test_export_evolution_event_timeout_fail_open_returns(caplog):
    client = FakeEvolutionClient(export_delay=0.2)
    manager = GatewayNodeManager.__new__(GatewayNodeManager)
    manager.evolution = EvolutionConfig(
        enabled=True,
        event_export={"fail_open": True, "timeout_seconds": 0.01},
    )
    manager.evolution_client = client

    started = asyncio.get_running_loop().time()
    await manager._export_evolution_event(_session_result())
    elapsed = asyncio.get_running_loop().time() - started

    assert elapsed < 0.15
    assert client.exported_events
    assert "Evolution event export failed for session ses_1" in caplog.text


@pytest.mark.asyncio
async def test_export_evolution_event_timeout_fail_closed_raises():
    client = FakeEvolutionClient(export_delay=0.2)
    manager = GatewayNodeManager.__new__(GatewayNodeManager)
    manager.evolution = EvolutionConfig(
        enabled=True,
        event_export={"fail_open": False, "timeout_seconds": 0.01},
    )
    manager.evolution_client = client

    started = asyncio.get_running_loop().time()
    with pytest.raises(TimeoutError):
        await manager._export_evolution_event(_session_result())
    elapsed = asyncio.get_running_loop().time() - started

    assert elapsed < 0.15
    assert client.exported_events


class RecordingSessionRegistry:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls
        self.results: list[SessionResult] = []
        self.cleared: list[str] = []

    def set_result(self, session_id: str, result: SessionResult) -> None:
        self.calls.append("set_result")
        self.results.append(result)

    def clear_result_payload(self, session_id: str) -> None:
        self.calls.append("clear_result_payload")
        self.cleared.append(session_id)


class RecordingStorage:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls
        self.deleted: list[str] = []

    def delete_session(self, session_id: str) -> None:
        self.calls.append("delete_session")
        self.deleted.append(session_id)


def _postrun_manager(
    *,
    calls: list[str],
    evolution: EvolutionConfig | None = None,
    evolution_client: FakeEvolutionClient | None = None,
):
    manager = GatewayNodeManager.__new__(GatewayNodeManager)
    manager.node_id = "node-a"
    manager.evolution = evolution
    manager.evolution_client = evolution_client
    manager.session_registry = RecordingSessionRegistry(calls)
    manager.storage = RecordingStorage(calls)

    async def run_postrun_steps(managed):
        calls.append("postrun_steps")

    async def drain_eval_prewarm_task(managed):
        calls.append("drain_eval")
        return None

    async def push_result(callback_url, result):
        calls.append("callback_push")
        return True

    async def remove_session_dir(session_dir, session_id):
        calls.append("remove_session_dir")

    manager._run_postrun_steps = run_postrun_steps
    manager._drain_eval_prewarm_task = drain_eval_prewarm_task
    manager._push_result = push_result
    manager._remove_session_dir_best_effort = remove_session_dir
    return manager


def _managed_postrun_session(tmp_path, result: SessionResult) -> ManagedSession:
    return ManagedSession(
        request=SessionDispatchRequest(
            session_id=result.session_id,
            task_id=result.task_id,
            instruction="Do work.",
            remaining_timeout_seconds=60,
            agent=AgentSpec(harness="fake"),
            callback_url="http://rollout.test/callback",
        ),
        timer=StageTimer(),
        session_dir=tmp_path,
        artifacts_dir=tmp_path / "artifacts",
        final_result=result,
    )


@pytest.mark.asyncio
async def test_handle_postrun_exports_after_set_result_before_cleanup_and_callback(
    tmp_path,
):
    calls: list[str] = []
    client = FakeEvolutionClient(calls=calls)
    manager = _postrun_manager(
        calls=calls,
        evolution=EvolutionConfig(enabled=True),
        evolution_client=client,
    )

    await manager._handle_postrun(_managed_postrun_session(tmp_path, _session_result()))

    assert calls == [
        "postrun_steps",
        "drain_eval",
        "set_result",
        "export",
        "delete_session",
        "callback_push",
        "clear_result_payload",
        "remove_session_dir",
    ]
    assert client.exported_events[0]["source_event_id"] == "session:ses_1"


@pytest.mark.asyncio
async def test_handle_postrun_fail_open_export_error_still_cleans_and_callbacks(
    tmp_path,
):
    calls: list[str] = []
    client = FakeEvolutionClient(
        export_error=RuntimeError("backend down"),
        calls=calls,
    )
    manager = _postrun_manager(
        calls=calls,
        evolution=EvolutionConfig(enabled=True, event_export={"fail_open": True}),
        evolution_client=client,
    )

    await manager._handle_postrun(_managed_postrun_session(tmp_path, _session_result()))

    assert calls == [
        "postrun_steps",
        "drain_eval",
        "set_result",
        "export",
        "delete_session",
        "callback_push",
        "clear_result_payload",
        "remove_session_dir",
    ]


@pytest.mark.asyncio
async def test_handle_postrun_fail_closed_export_error_skips_delete_and_callback(
    tmp_path,
):
    calls: list[str] = []
    client = FakeEvolutionClient(
        export_error=RuntimeError("backend down"),
        calls=calls,
    )
    manager = _postrun_manager(
        calls=calls,
        evolution=EvolutionConfig(enabled=True, event_export={"fail_open": False}),
        evolution_client=client,
    )

    with pytest.raises(RuntimeError, match="backend down"):
        await manager._handle_postrun(
            _managed_postrun_session(tmp_path, _session_result())
        )

    assert calls == [
        "postrun_steps",
        "drain_eval",
        "set_result",
        "export",
        "remove_session_dir",
    ]


class FakeRuntime:
    def __init__(self) -> None:
        self.uploads: dict[str, str] = {}

    async def exec(self, command, **kwargs):
        return None

    async def upload_file(self, source, target):
        self.uploads[target] = Path(source).read_text(encoding="utf-8")

    async def upload_dir(self, source, target):
        self.uploads[target] = str(source)


class BindMountRuntime(BaseRuntime):
    def __init__(self, session_dir):
        super().__init__(
            RuntimeSpec(image="runtime:latest"),
            session_id="session_1",
            session_dir=session_dir,
        )
        self.exec_envs: list[dict[str, str]] = []

    @property
    def runtime_id(self) -> str:
        return "bind-mount-runtime"

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def exec(
        self,
        command: str,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_sec: float | None = None,
    ) -> ExecResult:
        self.exec_envs.append(dict(env or {}))
        return ExecResult(return_code=0)

    async def upload_file(self, local_path: str, remote_path: str) -> None:
        copied = self._copy_to_bind_mount(local_path, remote_path)
        assert copied is True

    async def upload_dir(self, local_path: str, remote_path: str) -> None:
        copied = self._copy_to_bind_mount(local_path, remote_path)
        assert copied is True

    async def download_file(self, remote_path: str, local_path: str) -> None:
        return None

    async def download_dir(self, remote_path: str, local_path: str) -> None:
        return None


@pytest.mark.asyncio
async def test_write_evolution_context_files(tmp_path):
    runtime = FakeRuntime()
    context = {
        "context_id": "ctx_1",
        "memory": {"rendered_text": "Remember parser precedence."},
        "skills": [],
        "adapter_merge_spec": {
            "base_model": "Qwen/Qwen3.6-27B",
            "merge_mode": "reference_only",
            "adapters": [],
        },
        "selection": {},
    }

    env = await write_evolution_context_files(
        runtime=runtime,
        context=context,
        host_dir=tmp_path,
        target_dir="/polar/session/evolution",
    )

    assert (
        json.loads(runtime.uploads["/polar/session/evolution/context.json"])[
            "context_id"
        ]
        == "ctx_1"
    )
    assert runtime.uploads["/polar/session/evolution/memory.md"] == (
        "Remember parser precedence."
    )
    assert (
        json.loads(runtime.uploads["/polar/session/evolution/adapters.json"])[
            "merge_mode"
        ]
        == "reference_only"
    )
    assert env["POLAR_EVOLUTION_CONTEXT"] == "/polar/session/evolution/context.json"
    assert env["POLAR_MEMORY_FILE"] == "/polar/session/evolution/memory.md"


@pytest.mark.asyncio
async def test_write_evolution_context_files_avoids_bind_mount_same_file(tmp_path):
    runtime = BindMountRuntime(tmp_path)
    context = {
        "context_id": "ctx_1",
        "memory": {"rendered_text": "Remember parser precedence."},
        "adapter_merge_spec": {"merge_mode": "reference_only"},
    }

    env = await write_evolution_context_files(
        runtime=runtime,
        context=context,
        host_dir=tmp_path,
        target_dir="/polar/session/evolution",
    )

    assert json.loads((tmp_path / "evolution" / "context.json").read_text())[
        "context_id"
    ] == "ctx_1"
    assert (tmp_path / "evolution" / "memory.md").read_text() == (
        "Remember parser precedence."
    )
    assert json.loads((tmp_path / "evolution" / "adapters.json").read_text())[
        "merge_mode"
    ] == "reference_only"
    assert (tmp_path / "evolution" / "skills").is_dir()
    assert env["POLAR_EVOLUTION_CONTEXT"] == "/polar/session/evolution/context.json"


@pytest.mark.asyncio
async def test_resolve_and_inject_evolution_context_updates_metadata_and_returns_env(
    tmp_path,
):
    runtime = FakeRuntime()
    client = FakeEvolutionClient()
    manager = GatewayNodeManager.__new__(GatewayNodeManager)
    manager.evolution = EvolutionConfig(enabled=True)
    manager.evolution_client = client
    manager.model_served = "Qwen/Qwen3.6-27B"
    request = SessionDispatchRequest(
        session_id="session_1",
        task_id="task_1",
        instruction="Fix parser precedence.",
        remaining_timeout_seconds=60,
        agent=AgentSpec(harness="fake", env={"EXISTING": "1"}),
        metadata={"policy_version": "v1", "rollout_step": 7, "source": "test"},
    )
    managed = ManagedSession(
        request=request,
        timer=StageTimer(),
        session_dir=tmp_path,
        artifacts_dir=tmp_path / "artifacts",
        runtime=runtime,
    )
    managed.execution_deadline = asyncio.get_running_loop().time() + 60

    env = await manager._resolve_and_inject_evolution_context(
        managed,
        FakeHarness(),
    )

    assert client.payloads == [
        {
            "task_id": "task_1",
            "instruction": "Fix parser precedence.",
            "agent": request.agent.model_dump(mode="json"),
            "base_model": "Qwen/Qwen3.6-27B",
            "policy_version": "v1",
            "rollout_step": 7,
            "metadata": {"policy_version": "v1", "rollout_step": 7, "source": "test"},
        }
    ]
    assert env["POLAR_EVOLUTION_CONTEXT"] == "/polar/session/evolution/context.json"
    assert env["POLAR_MEMORY_FILE"] == "/polar/session/evolution/memory.md"
    assert env["POLAR_SKILLS_DIR"] == "/polar/session/evolution/skills"
    assert env["POLAR_ADAPTER_MERGE_SPEC"] == "/polar/session/evolution/adapters.json"
    assert request.metadata["evolution"] == {
        "context_id": "ctx_1",
        "context_injected": True,
    }


@pytest.mark.asyncio
async def test_resolve_and_inject_evolution_context_fail_open_records_error(tmp_path):
    manager = GatewayNodeManager.__new__(GatewayNodeManager)
    manager.evolution = EvolutionConfig(enabled=True, context={"fail_open": True})
    manager.evolution_client = FakeEvolutionClient(error=RuntimeError("backend down"))
    manager.model_served = "served-model"
    request = SessionDispatchRequest(
        session_id="session_1",
        task_id="task_1",
        instruction="Do work.",
        remaining_timeout_seconds=60,
        agent=AgentSpec(harness="fake"),
        metadata={},
    )
    managed = ManagedSession(
        request=request,
        timer=StageTimer(),
        session_dir=tmp_path,
        artifacts_dir=tmp_path / "artifacts",
        runtime=FakeRuntime(),
    )
    managed.execution_deadline = asyncio.get_running_loop().time() + 60

    env = await manager._resolve_and_inject_evolution_context(
        managed,
        FakeHarness(),
    )

    assert env == {}
    assert request.metadata["evolution"] == {
        "context_injected": False,
        "error": "backend down",
    }


@pytest.mark.asyncio
async def test_resolve_and_inject_evolution_context_fail_open_replaces_non_dict_metadata(
    tmp_path,
):
    manager = GatewayNodeManager.__new__(GatewayNodeManager)
    manager.evolution = EvolutionConfig(enabled=True, context={"fail_open": True})
    manager.evolution_client = FakeEvolutionClient(error=RuntimeError("backend down"))
    manager.model_served = "served-model"
    request = SessionDispatchRequest(
        session_id="session_1",
        task_id="task_1",
        instruction="Do work.",
        remaining_timeout_seconds=60,
        agent=AgentSpec(harness="fake"),
        metadata={"evolution": "legacy"},
    )
    managed = ManagedSession(
        request=request,
        timer=StageTimer(),
        session_dir=tmp_path,
        artifacts_dir=tmp_path / "artifacts",
        runtime=FakeRuntime(),
    )
    managed.execution_deadline = asyncio.get_running_loop().time() + 60

    env = await manager._resolve_and_inject_evolution_context(
        managed,
        FakeHarness(),
    )

    assert env == {}
    assert request.metadata["evolution"] == {
        "context_injected": False,
        "error": "backend down",
    }


@pytest.mark.asyncio
async def test_resolve_and_inject_evolution_context_fail_closed_raises(tmp_path):
    manager = GatewayNodeManager.__new__(GatewayNodeManager)
    manager.evolution = EvolutionConfig(enabled=True, context={"fail_open": False})
    manager.evolution_client = FakeEvolutionClient(error=RuntimeError("backend down"))
    manager.model_served = "served-model"
    request = SessionDispatchRequest(
        session_id="session_1",
        task_id="task_1",
        instruction="Do work.",
        remaining_timeout_seconds=60,
        agent=AgentSpec(harness="fake"),
        metadata={},
    )
    managed = ManagedSession(
        request=request,
        timer=StageTimer(),
        session_dir=tmp_path,
        artifacts_dir=tmp_path / "artifacts",
        runtime=FakeRuntime(),
    )
    managed.execution_deadline = asyncio.get_running_loop().time() + 60

    with pytest.raises(RuntimeError, match="backend down"):
        await manager._resolve_and_inject_evolution_context(
            managed,
            FakeHarness(),
        )


@pytest.mark.asyncio
async def test_handle_run_passes_evolution_env_to_runtime_exec(tmp_path, monkeypatch):
    runtime = BindMountRuntime(tmp_path)
    client = FakeEvolutionClient()
    manager = GatewayNodeManager.__new__(GatewayNodeManager)
    manager.evolution = EvolutionConfig(enabled=True)
    manager.evolution_client = client
    manager.model_served = "served-model"
    manager.gateway_url = "http://gateway.test"
    manager.default_runtime = None
    request = SessionDispatchRequest(
        session_id="session_1",
        task_id="task_1",
        instruction="Do work.",
        remaining_timeout_seconds=60,
        agent=AgentSpec(harness="fake", env={"EXISTING": "1"}),
        metadata={},
    )
    harness = RunStepHarness(request.agent)
    monkeypatch.setattr(manager, "_resolve_agent_harness", lambda _: harness)
    managed = ManagedSession(
        request=request,
        timer=StageTimer(),
        session_dir=tmp_path,
        artifacts_dir=tmp_path / "artifacts",
        runtime=runtime,
    )
    managed.execution_deadline = asyncio.get_running_loop().time() + 60

    await manager._handle_run(managed)

    assert managed.final_result is None
    assert managed.agent_result is not None
    assert managed.agent_result.status == "completed"
    assert runtime.exec_envs[-1]["EXISTING"] == "1"
    assert (
        runtime.exec_envs[-1]["POLAR_EVOLUTION_CONTEXT"]
        == "/polar/session/evolution/context.json"
    )
    assert runtime.exec_envs[-1]["POLAR_MEMORY_FILE"] == (
        "/polar/session/evolution/memory.md"
    )
    assert request.agent.env["POLAR_SKILLS_DIR"] == "/polar/session/evolution/skills"
