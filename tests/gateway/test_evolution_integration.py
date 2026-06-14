from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from polar.agent.base import BaseHarness
from polar.agent.models import AgentSpec
from polar.config import EvolutionConfig
from polar.gateway.dispatcher import ManagedSession
from polar.gateway.node import GatewayNodeManager, write_evolution_context_files
from polar.rollout.models import SessionDispatchRequest
from polar.rollout.timer import StageTimer
from polar.runtime.base import BaseRuntime
from polar.runtime.models import ExecInput, ExecResult, RuntimeSpec


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
    def __init__(self, context: dict | None = None, error: Exception | None = None) -> None:
        self.context = context or {
            "context_id": "ctx_1",
            "memory": {"rendered_text": "Remember parser precedence."},
            "adapter_merge_spec": {"merge_mode": "reference_only"},
        }
        self.error = error
        self.payloads: list[dict] = []

    async def resolve_context(self, payload: dict) -> dict:
        self.payloads.append(payload)
        if self.error is not None:
            raise self.error
        return self.context


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
