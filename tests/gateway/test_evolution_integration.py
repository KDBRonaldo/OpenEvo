from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from openevo.harness.base import BaseHarness
from openevo.harness.models import AgentRunResult, AgentSpec
from openevo.harness.presets.codex import CodexHarness
from openevo.config import EvolutionConfig
from openevo.evolution.models import (
    ArtifactRegisterRequest,
    ArtifactType,
    ContextResolveRequest,
)
from openevo.evolution.store import EvolutionStore
from openevo.gateway.dispatcher import ManagedSession
from openevo.gateway.node import (
    GatewayNodeManager,
    build_evolution_session_event,
    write_evolution_context_files,
)
from openevo.gateway.session import SessionRegistry
from openevo.gateway import session_files
from openevo.gateway.session_files import capture_session_root_identity
from openevo.rollout.models import (
    SessionDispatchRequest,
    SessionResult,
    SessionStatus,
    SessionTiming,
)
from openevo.rollout.timer import StageTimer
from openevo.runtime.base import BaseRuntime
from openevo.runtime.managed import MANAGED_CODEX_HOME, MANAGED_RUNTIME_IMAGES
from openevo.runtime.models import ExecInput, ExecResult, RuntimeSpec
from openevo.runtime.models import PrepareAction
from openevo.trajectory.builder.agent_transcript import AgentTranscriptBuilder
from openevo.trajectory.models import CompletionRecord, CompletionSession, Trajectory
from openevo.trajectory.registry import default_builder_registry


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

    assert event["source"] == "openevo"
    assert event["event_type"] == "openevo.session_completed"
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


def test_codex_subscription_auth_is_staged_into_private_credential_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    source = home / ".codex" / "auth.json"
    source.parent.mkdir(parents=True)
    source.write_text('{"subscription": true}\n', encoding="utf-8")
    source.chmod(0o600)
    monkeypatch.setenv("HOME", str(home))
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    request = SessionDispatchRequest(
        session_id="session_1",
        task_id="task_1",
        instruction="Do work.",
        remaining_timeout_seconds=60,
        agent=AgentSpec(
            harness="codex",
            model_name="gpt-5.5",
            settings={"auth_mode": "subscription", "capture_mode": "transcript"},
        ),
        metadata={},
    )

    manager = GatewayNodeManager.__new__(GatewayNodeManager)
    manager._stage_codex_subscription_auth(request, session_dir)

    staged = session_dir / "auth.json"
    assert staged.read_text(encoding="utf-8") == '{"subscription": true}\n'
    assert staged.stat().st_mode & 0o777 == 0o600
    assert staged.parent.stat().st_mode & 0o777 == 0o700


def test_codex_subscription_auth_staging_reports_missing_host_login(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    request = SessionDispatchRequest(
        session_id="session_1",
        task_id="task_1",
        instruction="Do work.",
        remaining_timeout_seconds=60,
        agent=AgentSpec(
            harness="codex",
            model_name="gpt-5.5",
            settings={"auth_mode": "subscription", "capture_mode": "transcript"},
        ),
        metadata={},
    )

    manager = GatewayNodeManager.__new__(GatewayNodeManager)
    with pytest.raises(RuntimeError, match="Codex subscription auth was not found"):
        manager._stage_codex_subscription_auth(request, session_dir)


@pytest.mark.asyncio
async def test_gateway_rejects_subscription_before_image_user_runtime_or_auth_staging(
    tmp_path: Path,
) -> None:
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    request = SessionDispatchRequest(
        session_id="session_1",
        task_id="task_1",
        instruction="Do work.",
        remaining_timeout_seconds=60,
        runtime=RuntimeSpec(
            profile="managed_science",
            image=MANAGED_RUNTIME_IMAGES["managed_science"],
            container_user="image",
        ),
        agent=AgentSpec(
            harness="codex",
            model_name="gpt-5.5",
            settings={"auth_mode": "subscription", "capture_mode": "transcript"},
        ),
        metadata={},
    )
    managed = ManagedSession(
        request=request,
        timer=StageTimer(),
        session_dir=session_dir,
        artifacts_dir=session_dir / "artifacts",
        session_root_identity=capture_session_root_identity(session_dir),
    )
    manager = GatewayNodeManager.__new__(GatewayNodeManager)
    manager.node_id = "gateway-test"
    manager.default_runtime = None

    await manager._handle_init(managed)

    assert managed.runtime is None
    assert managed.final_result is not None
    assert managed.final_result.status == SessionStatus.ERROR
    assert "runtime.container_user='host'" in (
        managed.final_result.error or ""
    )
    assert not (session_dir / ".codex").exists()


@pytest.mark.asyncio
async def test_gateway_rejects_host_user_custom_image_before_auth_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    request = SessionDispatchRequest(
        session_id="session_1",
        task_id="task_1",
        instruction="Do work.",
        remaining_timeout_seconds=60,
        runtime=RuntimeSpec(image="custom:latest", container_user="host"),
        agent=AgentSpec(
            harness="codex",
            model_name="gpt-5.5",
            settings={"auth_mode": "subscription", "capture_mode": "transcript"},
        ),
        metadata={},
    )
    managed = ManagedSession(
        request=request,
        timer=StageTimer(),
        session_dir=session_dir,
        artifacts_dir=session_dir / "artifacts",
        session_root_identity=capture_session_root_identity(session_dir),
    )
    manager = GatewayNodeManager.__new__(GatewayNodeManager)
    manager.node_id = "gateway-test"
    manager.default_runtime = None
    stage_auth = Mock(side_effect=AssertionError("auth must not be staged"))
    monkeypatch.setattr(manager, "_stage_codex_subscription_auth", stage_auth)

    await manager._handle_init(managed)

    assert managed.runtime is None
    assert managed.final_result is not None
    assert "managed runtime profile" in (managed.final_result.error or "")
    stage_auth.assert_not_called()


@pytest.mark.asyncio
async def test_gateway_rejects_non_codex_subscription_before_auth_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    request = SessionDispatchRequest(
        session_id="session_1",
        task_id="task_1",
        instruction="Do work.",
        remaining_timeout_seconds=60,
        runtime=RuntimeSpec(
            profile="managed_science",
            image=MANAGED_RUNTIME_IMAGES["managed_science"],
            container_user="host",
        ),
        agent=AgentSpec(
            harness="claude_code",
            settings={"auth_mode": "subscription", "capture_mode": "transcript"},
        ),
    )
    managed = ManagedSession(
        request=request,
        timer=StageTimer(),
        session_dir=session_dir,
        artifacts_dir=session_dir / "artifacts",
        session_root_identity=capture_session_root_identity(session_dir),
    )
    manager = GatewayNodeManager.__new__(GatewayNodeManager)
    manager.node_id = "gateway-test"
    manager.default_runtime = None
    stage_auth = Mock(side_effect=AssertionError("auth must not be staged"))
    monkeypatch.setattr(manager, "_stage_codex_subscription_auth", stage_auth)

    await manager._handle_init(managed)

    assert managed.runtime is None
    assert managed.final_result is not None
    assert "requires the Codex harness" in (managed.final_result.error or "")
    stage_auth.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("capture_mode", "agent_env", "error"),
    [
        ("pure_text", {}, "capture_mode='transcript'"),
        (
            "transcript",
            {"CODEX_HOME": "/openevo/session/artifacts/.codex"},
            "CODEX_HOME is Core-owned",
        ),
    ],
)
async def test_gateway_rejects_non_exact_subscription_contract_before_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capture_mode: str,
    agent_env: dict[str, str],
    error: str,
) -> None:
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    request = SessionDispatchRequest(
        session_id="session_1",
        task_id="task_1",
        instruction="Do work.",
        remaining_timeout_seconds=60,
        runtime=RuntimeSpec(
            profile="managed_science",
            image=MANAGED_RUNTIME_IMAGES["managed_science"],
            container_user="host",
        ),
        agent=AgentSpec(
            harness="codex",
            settings={"auth_mode": "subscription", "capture_mode": capture_mode},
            env=agent_env,
        ),
    )
    managed = ManagedSession(
        request=request,
        timer=StageTimer(),
        session_dir=session_dir,
        artifacts_dir=session_dir / "artifacts",
        session_root_identity=capture_session_root_identity(session_dir),
    )
    manager = GatewayNodeManager.__new__(GatewayNodeManager)
    manager.node_id = "gateway-test"
    manager.default_runtime = None
    stage_auth = Mock(side_effect=AssertionError("auth must not be staged"))
    monkeypatch.setattr(manager, "_stage_codex_subscription_auth", stage_auth)

    await manager._handle_init(managed)

    assert managed.runtime is None
    assert managed.final_result is not None
    assert error in (managed.final_result.error or "")
    stage_auth.assert_not_called()


@pytest.mark.parametrize("env_name", ["HOME", "PATH", "CODEX_HOME"])
@pytest.mark.parametrize("env_owner", ["agent", "runtime", "action"])
def test_gateway_rejects_subscription_closed_environment_overrides(
    tmp_path: Path,
    env_name: str,
    env_owner: str,
) -> None:
    agent_env = {env_name: "/attacker"} if env_owner == "agent" else {}
    runtime_env = {env_name: "/attacker"} if env_owner == "runtime" else {}
    prepare = (
        [PrepareAction(type="exec", command="true", env={env_name: "/attacker"})]
        if env_owner == "action"
        else []
    )
    runtime = RuntimeSpec(
        profile="managed_science",
        image=MANAGED_RUNTIME_IMAGES["managed_science"],
        container_user="host",
        env=runtime_env,
        prepare=prepare,
    )
    request = SessionDispatchRequest(
        session_id="session_1",
        task_id="task_1",
        instruction="Do work.",
        remaining_timeout_seconds=60,
        runtime=runtime,
        agent=AgentSpec(
            harness="codex",
            settings={"auth_mode": "subscription", "capture_mode": "transcript"},
            env=agent_env,
        ),
    )

    with pytest.raises(RuntimeError, match=f"{env_name} is Core-owned"):
        GatewayNodeManager._validate_subscription_admission(request, runtime, tmp_path)


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
    assert client.exported_events[0]["event_type"] == "openevo.session_completed"
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


class EmptyCompletionStorage:
    def load_completion_session(self, session_id: str) -> CompletionSession:
        return CompletionSession(session_id=session_id, metadata={"source": "subscription"})


class OneCompletionStorage:
    def load_completion_session(self, session_id: str) -> CompletionSession:
        return CompletionSession(
            session_id=session_id,
            metadata={"source": "proxy"},
            completions=[
                CompletionRecord(
                    completion_id="completion_1",
                    request={"messages": [{"role": "user", "content": "Do work."}]},
                    response={
                        "choices": [
                            {
                                "input_token_ids": [1, 2],
                                "token_ids": [3],
                                "message": {
                                    "role": "assistant",
                                    "content": "Done.",
                                },
                                "logprobs": {"content": [{"token_id": 3, "logprob": -0.1}]},
                            }
                        ]
                    },
                )
            ],
        )


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
    manager._cleanup_retries = {}

    async def run_postrun_steps(managed):
        calls.append("postrun_steps")

    async def drain_eval_prewarm_task(managed):
        calls.append("drain_eval")
        return None

    async def push_result(callback_url, result):
        calls.append("callback_push")
        return True

    async def remove_session_dir(session_dir, session_id, session_root_identity=None):
        calls.append("remove_session_dir")

    async def remove_credential_dir(
        credential_dir,
        session_id,
        credential_root_identity=None,
    ):
        calls.append("remove_credential_dir")

    manager._run_postrun_steps = run_postrun_steps
    manager._drain_eval_prewarm_task = drain_eval_prewarm_task
    manager._push_result = push_result
    manager._remove_session_dir_best_effort = remove_session_dir
    manager._remove_credential_dir_best_effort = remove_credential_dir
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
async def test_handle_postrun_does_not_cleanup_session_when_runtime_removal_fails(
    tmp_path,
):
    calls: list[str] = []
    manager = _postrun_manager(calls=calls)

    async def stop_runtime(runtime, session_id, label):
        del runtime, session_id, label
        calls.append("stop_failed")
        return False

    manager._stop_runtime_best_effort = stop_runtime
    managed = _managed_postrun_session(tmp_path, _session_result())
    managed.runtime = FakeRuntime()
    managed.credential_dir = tmp_path / "credentials"

    async def remove_credentials(*args, **kwargs):
        del args, kwargs
        calls.append("remove_credentials")
        return True

    manager._remove_credential_dir_best_effort = remove_credentials

    await manager._handle_postrun(managed)

    assert "stop_failed" in calls
    assert "remove_session_dir" not in calls
    assert "remove_credentials" not in calls
    retry = manager._cleanup_retries[managed.session_id]
    assert retry.session_id == managed.session_id
    assert retry.runtime_id == managed.runtime.runtime_id
    assert retry.session_dir == managed.session_dir
    assert retry.credential_dir == managed.credential_dir


@pytest.mark.asyncio
async def test_subscription_stops_runtime_and_redacts_background_output_before_result(
    tmp_path: Path,
) -> None:
    calls: list[str] = []
    manager = _postrun_manager(calls=calls)
    manager.builders = default_builder_registry()
    manager.node_id = "node-a"
    secret = "access-canary"
    redactor = session_files.CredentialRedactor.from_auth_json(
        b'{"access_token":"access-canary"}'
    )
    log_dir = tmp_path / "logs" / "agent"
    log_dir.mkdir(parents=True)
    log_path = log_dir / "step.00.stdout.log"
    log_path.write_text("initial", encoding="utf-8")

    class BackgroundRuntime(FakeRuntime):
        @property
        def runtime_id(self) -> str:
            return "credential-container-id"

        async def stop(self) -> None:
            calls.append("runtime_absent")
            log_path.write_text(f"background {secret}", encoding="utf-8")

    async def build_result(managed: ManagedSession) -> SessionResult:
        calls.append("build_result")
        captured = log_path.read_text(encoding="utf-8")
        return _session_result(
            session_id=managed.session_id,
            metadata={"captured": captured, "defensive": secret},
        )

    manager._build_session_result = build_result
    request = SessionDispatchRequest(
        session_id="subscription-session",
        task_id="task_1",
        instruction="Do work.",
        remaining_timeout_seconds=60,
        runtime=RuntimeSpec(
            profile="managed_science",
            image=MANAGED_RUNTIME_IMAGES["managed_science"],
            container_user="host",
        ),
        agent=AgentSpec(
            harness="codex",
            model_name="gpt-5.5",
            settings={"auth_mode": "subscription", "capture_mode": "transcript"},
        ),
    )
    managed = ManagedSession(
        request=request,
        timer=StageTimer(),
        session_dir=tmp_path,
        artifacts_dir=tmp_path / "artifacts",
        session_root_identity=capture_session_root_identity(tmp_path),
        credential_dir=tmp_path.parent / "credentials-subscription-session",
        credential_root_identity=(1, 2, 3),
        credential_redactor=redactor,
        runtime=BackgroundRuntime(),
        agent_result=AgentRunResult(status="completed", return_code=0),
    )

    await manager._handle_postrun(managed)

    assert calls.index("runtime_absent") < calls.index("build_result")
    normalized = manager.session_registry.results[-1]
    serialized = normalized.model_dump_json()
    assert "background" in serialized
    assert secret not in serialized


@pytest.mark.asyncio
async def test_cleanup_retry_reconciliation_retries_owned_runtime_and_roots(
    tmp_path: Path,
) -> None:
    calls: list[str] = []
    manager = _postrun_manager(calls=calls)
    attempts = 0

    async def stop_runtime(runtime, session_id, label):
        nonlocal attempts
        del runtime, session_id, label
        attempts += 1
        return attempts > 1

    manager._stop_runtime_best_effort = stop_runtime
    managed = _managed_postrun_session(tmp_path, _session_result())
    managed.runtime = FakeRuntime()
    managed.credential_dir = tmp_path / "credentials"

    await manager._handle_postrun(managed)
    assert managed.session_id in manager._cleanup_retries

    await manager._reconcile_cleanup_retries()

    assert managed.session_id not in manager._cleanup_retries
    assert calls.count("remove_session_dir") == 1


@pytest.mark.asyncio
async def test_cleanup_ownership_persists_for_new_manager_startup_reconciliation(
    tmp_path: Path,
) -> None:
    journal_dir = tmp_path / "cleanup-journal"
    session_dir = tmp_path / "session"
    credential_dir = tmp_path / "credentials"
    session_dir.mkdir()
    credential_dir.mkdir()

    class JournalRuntime(FakeRuntime):
        @property
        def runtime_id(self) -> str:
            return "openevo-journal-session"

        @property
        def container_id(self) -> str:
            return "sha256:journal-container"

    class EvalJournalRuntime(FakeRuntime):
        @property
        def runtime_id(self) -> str:
            return "openevo-journal-session-eval"

        @property
        def container_id(self) -> str:
            return "sha256:journal-eval-container"

    managed = _managed_postrun_session(session_dir, _session_result(session_id="journal"))
    managed.runtime = JournalRuntime()
    managed.eval_runtime = EvalJournalRuntime()
    managed.session_root_identity = capture_session_root_identity(session_dir)
    managed.credential_dir = credential_dir
    managed.credential_root_identity = capture_session_root_identity(credential_dir)

    first = GatewayNodeManager.__new__(GatewayNodeManager)
    first._cleanup_retries = {}
    first._cleanup_journal_dir = journal_dir
    first._register_cleanup_retry(managed, eval_runtime=managed.eval_runtime)

    journal_files = list(journal_dir.glob("*.json"))
    assert len(journal_files) == 1
    journal_text = journal_files[0].read_text(encoding="utf-8")
    assert "sha256:journal-container" in journal_text
    assert "sha256:journal-eval-container" in journal_text
    assert "credential" in journal_text
    assert "access_token" not in journal_text

    calls: list[str] = []
    restarted = GatewayNodeManager.__new__(GatewayNodeManager)
    restarted._cleanup_retries = {}
    restarted._cleanup_journal_dir = journal_dir

    async def stop_recovered(container_id: str, runtime_id: str | None) -> bool:
        calls.append(f"stop:{container_id}:{runtime_id}")
        return True

    async def remove_session(*args, **kwargs):
        del args, kwargs
        calls.append("session")
        return True

    async def remove_credential(*args, **kwargs):
        del args, kwargs
        calls.append("credential")
        return True

    restarted._stop_recovered_container = stop_recovered
    restarted._remove_session_dir_best_effort = remove_session
    restarted._remove_credential_dir_best_effort = remove_credential

    restarted._load_cleanup_retries()
    await restarted._reconcile_cleanup_retries()

    assert calls == [
        "stop:sha256:journal-eval-container:openevo-journal-session-eval",
        "stop:sha256:journal-container:openevo-journal-session",
        "session",
        "credential",
    ]
    assert restarted._cleanup_retries == {}
    assert list(journal_dir.glob("*.json")) == []


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
        await manager._handle_postrun(_managed_postrun_session(tmp_path, _session_result()))

    assert calls == [
        "postrun_steps",
        "drain_eval",
        "set_result",
        "export",
        "remove_session_dir",
    ]


class FakeRuntime:
    def __init__(self, *, workdir: str | None = None) -> None:
        self.spec = RuntimeSpec(image="runtime:latest", workdir=workdir)
        self.runtime_session_dir = "/openevo/session"
        self.uploads: dict[str, str] = {}
        self.exec_commands: list[str] = []

    @property
    def runtime_id(self) -> str:
        return "fake-runtime"

    async def exec(self, command, **kwargs):
        self.exec_commands.append(command)
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
        self.exec_commands: list[str] = []
        self.exec_results: list[ExecResult] = []

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
        self.exec_commands.append(command)
        self.exec_envs.append(dict(env or {}))
        if self.exec_results:
            return self.exec_results.pop(0)
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
        "agent_system": {
            "rendered_text": "Prefer repository-local conventions.",
            "target_path": "AGENTS.md",
        },
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
        target_dir="/openevo/session/evolution",
    )

    assert (
        json.loads(runtime.uploads["/openevo/session/evolution/context.json"])["context_id"]
        == "ctx_1"
    )
    assert runtime.uploads["/openevo/session/evolution/memory.md"] == ("Remember parser precedence.")
    assert runtime.uploads["/openevo/session/evolution/agent_system.md"] == (
        "Prefer repository-local conventions."
    )
    assert (tmp_path / ".openevo" / "evolution_upload" / "context.json").is_file()
    assert not (tmp_path / (".openevo" + ".evolution_upload")).exists()
    assert runtime.uploads["/openevo/session/AGENTS.md"] == ("Prefer repository-local conventions.")
    assert (
        json.loads(runtime.uploads["/openevo/session/evolution/adapters.json"])["merge_mode"]
        == "reference_only"
    )
    assert env["OPENEVO_EVOLUTION_CONTEXT"] == "/openevo/session/evolution/context.json"
    assert env["OPENEVO_MEMORY_FILE"] == "/openevo/session/evolution/memory.md"
    assert env["OPENEVO_AGENT_SYSTEM_FILE"] == "/openevo/session/evolution/agent_system.md"
    assert env["OPENEVO_AGENT_SYSTEM_TARGET"] == "/openevo/session/AGENTS.md"
    assert json.loads(env["OPENEVO_AGENT_SYSTEM_TARGETS"]) == ["/openevo/session/AGENTS.md"]
    assert env["OPENEVO_AGENTS_MD"] == "/openevo/session/AGENTS.md"


@pytest.mark.asyncio
async def test_resolved_store_context_stages_all_text_artifacts(tmp_path):
    memory_text = "Remember to preserve parser precedence."
    agent_system_text = "Inspect repository conventions before editing."
    skill_text = "---\nname: parser-probe\n---\nUse recursive descent.\n"

    memory_source = tmp_path / "sources" / "memory.md"
    agent_system_source = tmp_path / "sources" / "AGENTS.md"
    skill_source = tmp_path / "sources" / "parser-skill"
    memory_source.parent.mkdir()
    skill_source.mkdir()
    memory_source.write_text(memory_text, encoding="utf-8")
    agent_system_source.write_text(agent_system_text, encoding="utf-8")
    (skill_source / "SKILL.md").write_text(skill_text, encoding="utf-8")

    store = EvolutionStore(
        db_path=tmp_path / "evolution.db",
        artifact_root=tmp_path / "artifacts",
    )
    store.initialize()
    memory = store.register_artifact(
        ArtifactRegisterRequest(
            type=ArtifactType.TEXT_MEMORY,
            name="parser memory",
            uri=memory_source.as_uri(),
            compatibility={"task_tags": ["parser"], "agent_harness": ["codex"]},
            scores={"quality": 0.9},
            promoted=True,
        )
    )
    skill = store.register_artifact(
        ArtifactRegisterRequest(
            type=ArtifactType.SKILL_BUNDLE,
            name="parser skill",
            uri=skill_source.as_uri(),
            compatibility={"task_tags": ["parser"], "agent_harness": ["codex"]},
            scores={"quality": 0.8},
            promoted=True,
        )
    )
    agent_system = store.register_artifact(
        ArtifactRegisterRequest(
            type=ArtifactType.AGENT_SYSTEM,
            name="parser agent system",
            uri=agent_system_source.as_uri(),
            manifest={"target_path": "AGENTS.md"},
            compatibility={"task_tags": ["parser"], "agent_harness": ["codex"]},
            scores={"quality": 0.85},
            promoted=True,
        )
    )

    resolved = store.resolve_context(
        ContextResolveRequest(
            task_id="parser-task",
            instruction="Fix the parser.",
            agent={"harness": "codex"},
            metadata={"task_tags": ["parser"]},
        )
    )
    assert resolved.selection["artifact_ids"] == [
        memory.artifact_id,
        agent_system.artifact_id,
        skill.artifact_id,
    ]

    runtime = BindMountRuntime(tmp_path)
    env = await write_evolution_context_files(
        runtime=runtime,
        context=resolved.model_dump(),
        host_dir=tmp_path,
        target_dir="/openevo/session/evolution",
    )

    assert (tmp_path / "evolution" / "memory.md").read_text(encoding="utf-8") == memory_text
    assert (tmp_path / "evolution" / "agent_system.md").read_text(
        encoding="utf-8"
    ) == agent_system_text
    assert (tmp_path / "AGENTS.md").read_text(encoding="utf-8") == agent_system_text
    staged_skill = tmp_path / "evolution" / "skills" / skill.artifact_id
    assert (staged_skill / "SKILL.md").read_text(encoding="utf-8") == skill_text
    assert env["OPENEVO_MEMORY_FILE"] == "/openevo/session/evolution/memory.md"
    assert env["OPENEVO_SKILLS_DIR"] == "/openevo/session/evolution/skills"
    assert env["OPENEVO_AGENT_SYSTEM_FILE"] == (
        "/openevo/session/evolution/agent_system.md"
    )
    assert env["OPENEVO_AGENT_SYSTEM_TARGET"] == "/openevo/session/AGENTS.md"
    assert env["OPENEVO_AGENTS_MD"] == "/openevo/session/AGENTS.md"


@pytest.mark.asyncio
async def test_write_evolution_context_files_uses_runtime_workdir_for_agent_system_target(
    tmp_path,
):
    runtime = FakeRuntime(workdir="/workspace/repo")
    context = {
        "context_id": "ctx_1",
        "agent_system": {
            "rendered_text": "Prefer repository-local conventions.",
            "target_path": "AGENTS.md",
        },
    }

    env = await write_evolution_context_files(
        runtime=runtime,
        context=context,
        host_dir=tmp_path,
        target_dir="/openevo/session/evolution",
    )

    assert runtime.uploads["/workspace/repo/AGENTS.md"] == ("Prefer repository-local conventions.")
    assert env["OPENEVO_AGENT_SYSTEM_TARGET"] == "/workspace/repo/AGENTS.md"
    assert env["OPENEVO_AGENTS_MD"] == "/workspace/repo/AGENTS.md"
    assert "mkdir -p /workspace/repo" in runtime.exec_commands


@pytest.mark.asyncio
async def test_write_evolution_context_files_avoids_bind_mount_same_file(tmp_path):
    runtime = BindMountRuntime(tmp_path)
    context = {
        "context_id": "ctx_1",
        "memory": {"rendered_text": "Remember parser precedence."},
        "agent_system": {
            "rendered_text": "Use OpenHands repository microagents.",
            "target_path": ".openhands/microagents/repo.md",
        },
        "adapter_merge_spec": {"merge_mode": "reference_only"},
    }

    env = await write_evolution_context_files(
        runtime=runtime,
        context=context,
        host_dir=tmp_path,
        target_dir="/openevo/session/evolution",
    )

    assert (
        json.loads((tmp_path / "evolution" / "context.json").read_text())["context_id"] == "ctx_1"
    )
    assert (tmp_path / "evolution" / "memory.md").read_text() == ("Remember parser precedence.")
    assert (tmp_path / "evolution" / "agent_system.md").read_text() == (
        "Use OpenHands repository microagents."
    )
    assert (tmp_path / ".openhands" / "microagents" / "repo.md").read_text() == (
        "Use OpenHands repository microagents."
    )
    assert (
        json.loads((tmp_path / "evolution" / "adapters.json").read_text())["merge_mode"]
        == "reference_only"
    )
    assert (tmp_path / "evolution" / "skills").is_dir()
    assert env["OPENEVO_EVOLUTION_CONTEXT"] == "/openevo/session/evolution/context.json"
    assert env["OPENEVO_AGENT_SYSTEM_TARGET"] == ("/openevo/session/.openhands/microagents/repo.md")
    assert json.loads(env["OPENEVO_AGENT_SYSTEM_TARGETS"]) == [
        "/openevo/session/.openhands/microagents/repo.md"
    ]
    assert "OPENEVO_AGENTS_MD" not in env


@pytest.mark.asyncio
async def test_write_evolution_context_files_skips_unsafe_agent_system_target_but_uploads_canonical(
    tmp_path,
):
    runtime = FakeRuntime()
    context = {
        "context_id": "ctx_1",
        "agent_system": {
            "rendered_text": "Do not overwrite project config.",
            "target_path": "pyproject.toml",
        },
    }

    env = await write_evolution_context_files(
        runtime=runtime,
        context=context,
        host_dir=tmp_path,
        target_dir="/openevo/session/evolution",
    )

    assert runtime.uploads["/openevo/session/evolution/agent_system.md"] == (
        "Do not overwrite project config."
    )
    assert "/openevo/session/pyproject.toml" not in runtime.uploads
    assert env["OPENEVO_AGENT_SYSTEM_FILE"] == "/openevo/session/evolution/agent_system.md"
    assert "OPENEVO_AGENT_SYSTEM_TARGET" not in env
    assert "OPENEVO_AGENT_SYSTEM_TARGETS" not in env
    assert "warnings" in json.loads(runtime.uploads["/openevo/session/evolution/context.json"])


@pytest.mark.asyncio
async def test_write_evolution_context_files_stages_skill_bundle_artifacts(tmp_path):
    skill_source = tmp_path / "source_skill"
    skill_source.mkdir()
    (skill_source / "SKILL.md").write_text(
        "---\nname: parser-memory\n---\nRemember parser precedence.",
        encoding="utf-8",
    )
    runtime = BindMountRuntime(tmp_path)
    context = {
        "context_id": "ctx_1",
        "skills": [
            {
                "artifact_id": "artifact skill/1",
                "name": "Parser Memory",
                "uri": skill_source.as_uri(),
            }
        ],
    }

    await write_evolution_context_files(
        runtime=runtime,
        context=context,
        host_dir=tmp_path,
        target_dir="/openevo/session/evolution",
    )

    staged_skill = tmp_path / "evolution" / "skills" / "artifact-skill-1"
    assert (staged_skill / "SKILL.md").read_text(encoding="utf-8") == (
        "---\nname: parser-memory\n---\nRemember parser precedence."
    )
    manifest = json.loads((staged_skill / "artifact.json").read_text(encoding="utf-8"))
    assert manifest["artifact_id"] == "artifact skill/1"
    assert manifest["uri"] == skill_source.as_uri()


@pytest.mark.asyncio
async def test_write_evolution_context_files_skips_bad_skill_without_dropping_memory(
    tmp_path,
):
    runtime = BindMountRuntime(tmp_path)
    context = {
        "context_id": "ctx_1",
        "memory": {"rendered_text": "Remember parser precedence."},
        "skills": [
            {
                "artifact_id": "bad_skill",
                "name": "Bad Skill",
                "uri": "https://example.invalid/skills/bad",
            }
        ],
        "adapter_merge_spec": {
            "base_model": "Qwen/Qwen3.6-27B",
            "merge_mode": "runtime_lora",
            "adapters": [{"adapter_id": "parser-memory"}],
        },
    }

    await write_evolution_context_files(
        runtime=runtime,
        context=context,
        host_dir=tmp_path,
        target_dir="/openevo/session/evolution",
    )

    assert (tmp_path / "evolution" / "memory.md").read_text(encoding="utf-8") == (
        "Remember parser precedence."
    )
    assert (
        json.loads((tmp_path / "evolution" / "adapters.json").read_text())["merge_mode"]
        == "runtime_lora"
    )
    written_context = json.loads((tmp_path / "evolution" / "context.json").read_text())
    assert "warnings" in written_context
    assert "bad_skill" in written_context["warnings"][0]


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
    assert env["OPENEVO_EVOLUTION_CONTEXT"] == "/openevo/session/evolution/context.json"
    assert env["OPENEVO_MEMORY_FILE"] == "/openevo/session/evolution/memory.md"
    assert env["OPENEVO_SKILLS_DIR"] == "/openevo/session/evolution/skills"
    assert env["OPENEVO_ADAPTER_MERGE_SPEC"] == "/openevo/session/evolution/adapters.json"
    assert request.metadata["evolution"] == {
        "context_id": "ctx_1",
        "context_injected": True,
    }


@pytest.mark.asyncio
async def test_resolve_and_inject_evolution_context_updates_session_registry_adapter_spec(
    tmp_path,
):
    runtime = FakeRuntime()
    context = {
        "context_id": "ctx_1",
        "adapter_merge_spec": {
            "base_model": "Qwen/Qwen3.6-27B",
            "merge_mode": "runtime_lora",
            "adapters": [{"adapter_id": "parser-memory"}],
        },
    }
    client = FakeEvolutionClient(context=context)
    registry = SessionRegistry()
    registry.register("session_1", task_id="task_1", metadata={"source": "test"})
    manager = GatewayNodeManager.__new__(GatewayNodeManager)
    manager.evolution = EvolutionConfig(enabled=True)
    manager.evolution_client = client
    manager.model_served = "Qwen/Qwen3.6-27B"
    manager.session_registry = registry
    request = SessionDispatchRequest(
        session_id="session_1",
        task_id="task_1",
        instruction="Fix parser precedence.",
        remaining_timeout_seconds=60,
        agent=AgentSpec(harness="fake"),
        metadata={},
    )
    managed = ManagedSession(
        request=request,
        timer=StageTimer(),
        session_dir=tmp_path,
        artifacts_dir=tmp_path / "artifacts",
        runtime=runtime,
    )
    managed.execution_deadline = asyncio.get_running_loop().time() + 60

    await manager._resolve_and_inject_evolution_context(managed, FakeHarness())

    info = registry.get("session_1")
    assert info is not None
    assert info.metadata is not None
    assert info.metadata["adapter_merge_spec"] == context["adapter_merge_spec"]
    assert info.metadata["evolution"]["context_id"] == "ctx_1"


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
        runtime.exec_envs[-1]["OPENEVO_EVOLUTION_CONTEXT"] == "/openevo/session/evolution/context.json"
    )
    assert runtime.exec_envs[-1]["OPENEVO_MEMORY_FILE"] == ("/openevo/session/evolution/memory.md")
    assert request.agent.env["OPENEVO_SKILLS_DIR"] == "/openevo/session/evolution/skills"


@pytest.mark.asyncio
async def test_handle_run_codex_subscription_auth_mode_unsets_openevo_proxy_env(
    tmp_path,
    monkeypatch,
):
    runtime = BindMountRuntime(tmp_path)
    manager = GatewayNodeManager.__new__(GatewayNodeManager)
    manager.evolution = EvolutionConfig(enabled=False)
    manager.evolution_client = None
    manager.gateway_url = "http://gateway.test"
    manager.default_runtime = None
    request = SessionDispatchRequest(
        session_id="session_1",
        task_id="task_1",
        instruction="Do work.",
        remaining_timeout_seconds=60,
        agent=AgentSpec(
            harness="codex",
            model_name="gpt-5.5",
            settings={"auth_mode": "subscription", "capture_mode": "transcript"},
        ),
        metadata={},
    )
    harness = CodexHarness(request.agent)
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

    codex_commands = [command for command in runtime.exec_commands if "codex exec" in command]
    assert len(codex_commands) == 1
    command = codex_commands[0]
    assert command.startswith("env ")
    for key in (
        "OPENAI_BASE_URL",
        "OPENAI_API_KEY",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_API_KEY",
        "GOOGLE_API_URL",
        "GOOGLE_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_GEMINI_BASE_URL",
    ):
        assert f"-u {key}" in command
    assert 'model_provider="harness_proxy"' not in command
    assert "model_providers.harness_proxy" not in command
    assert "auth.json" not in command
    assert "--model gpt-5.5" in command
    assert runtime.exec_envs[-1]["OPENAI_BASE_URL"] == "http://gateway.test/v1"
    assert runtime.exec_envs[-1]["OPENAI_API_KEY"] == "session_1"
    assert runtime.exec_envs[-1]["CODEX_HOME"] == MANAGED_CODEX_HOME


@pytest.mark.asyncio
async def test_subscription_stdout_and_transcript_redact_auth_canaries(tmp_path) -> None:
    auth = (
        b'{"tokens":{"access_token":"access-canary",'
        b'"refresh_token":"refresh-canary"}}'
    )
    redactor = session_files.CredentialRedactor.from_auth_json(auth)
    runtime = BindMountRuntime(tmp_path)
    runtime.exec_results = [
        ExecResult(
            return_code=0,
            stdout=(
                '{"type":"item.completed","item":{"type":"agent_message",'
                '"text":"access-canary refresh-canary"}}\n'
            ),
            stderr=auth.decode(),
        )
    ]
    manager = GatewayNodeManager.__new__(GatewayNodeManager)
    manager.gateway_url = "http://gateway.test"
    request = SessionDispatchRequest(
        session_id="session_1",
        task_id="task_1",
        instruction="Do work.",
        remaining_timeout_seconds=60,
        runtime=RuntimeSpec(
            profile="managed_science",
            image=MANAGED_RUNTIME_IMAGES["managed_science"],
            container_user="host",
        ),
        agent=AgentSpec(
            harness="codex",
            settings={"auth_mode": "subscription", "capture_mode": "transcript"},
        ),
    )
    managed = ManagedSession(
        request=request,
        timer=StageTimer(),
        session_dir=tmp_path,
        artifacts_dir=tmp_path / "artifacts",
        runtime=runtime,
        credential_redactor=redactor,
    )
    managed.execution_deadline = asyncio.get_running_loop().time() + 60

    await manager._run_exec_inputs(
        runtime,
        [ExecInput(command="codex exec")],
        {},
        managed,
    )
    completion = CompletionSession(
        session_id="session_1",
        metadata={
            "agent_result_metadata": {
                "log_dir": str(tmp_path / "logs" / "agent"),
                "last_step": 0,
            }
        },
    )
    trajectory = await AgentTranscriptBuilder().build(completion)

    persisted = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (tmp_path / "logs" / "agent").iterdir()
    ) + trajectory.model_dump_json()
    assert "access-canary" not in persisted
    assert "refresh-canary" not in persisted
    assert auth.decode() not in persisted


@pytest.mark.asyncio
async def test_codex_subscription_without_proxy_completions_builds_transcript_trajectory(
    tmp_path,
    monkeypatch,
):
    log_dir = tmp_path / "logs" / "agent"
    log_dir.mkdir(parents=True)
    (log_dir / "step.00.stdout.log").write_text(
        json.dumps(
            {
                "type": "message",
                "message": {"role": "assistant", "content": "Used subscription mode."},
            }
        ),
        encoding="utf-8",
    )
    manager = GatewayNodeManager.__new__(GatewayNodeManager)
    manager.node_id = "node-a"
    manager.storage = EmptyCompletionStorage()
    manager.builders = default_builder_registry()
    manager.session_registry = SessionRegistry()
    manager.session_registry.register("session_1", task_id="task_1")
    request = SessionDispatchRequest(
        session_id="session_1",
        task_id="task_1",
        instruction="Do work.",
        remaining_timeout_seconds=60,
        agent=AgentSpec(
            harness="codex",
            settings={"auth_mode": "subscription", "capture_mode": "transcript"},
        ),
        metadata={},
    )
    managed = ManagedSession(
        request=request,
        timer=StageTimer(),
        session_dir=tmp_path,
        artifacts_dir=tmp_path / "artifacts",
        session_root_identity=capture_session_root_identity(tmp_path),
        agent_result=AgentRunResult(
            status="completed",
            return_code=0,
            metadata={"log_dir": str(log_dir), "last_step": 0},
        ),
    )
    managed.execution_deadline = asyncio.get_running_loop().time() + 60

    def reject_path_reopen(*args, **kwargs):
        del args, kwargs
        raise AssertionError("subscription transcript must not be reopened by pathname")

    monkeypatch.setattr(Path, "read_text", reject_path_reopen)

    result = await manager._build_session_result(managed)

    assert result.status == "COMPLETED"
    assert result.error is None
    assert result.trajectory.status == "COMPLETED"
    assert result.trajectory.error is None
    assert result.trajectory.metadata["builder"] == "agent_transcript"
    assert result.trajectory.metadata["capture_mode"] == "transcript"
    assert result.trajectory.metadata["token_level_metrics_available"] is False
    trace = result.trajectory.traces[0]
    assert trace.prompt_messages == [{"role": "user", "content": "Do work."}]
    assert trace.response_messages == [{"role": "assistant", "content": "Used subscription mode."}]
    assert trace.response_ids == []
    assert trace.response_logprobs is None


@pytest.mark.asyncio
async def test_transcript_capture_mode_fallback_is_not_codex_specific(
    tmp_path,
):
    log_dir = tmp_path / "logs" / "agent"
    log_dir.mkdir(parents=True)
    (log_dir / "step.00.stdout.log").write_text(
        json.dumps(
            {
                "type": "message",
                "message": {
                    "role": "assistant",
                    "content": "Used non-Codex transcript mode.",
                },
            }
        ),
        encoding="utf-8",
    )
    manager = GatewayNodeManager.__new__(GatewayNodeManager)
    manager.node_id = "node-a"
    manager.storage = EmptyCompletionStorage()
    manager.builders = default_builder_registry()
    manager.session_registry = SessionRegistry()
    manager.session_registry.register("session_1", task_id="task_1")
    request = SessionDispatchRequest(
        session_id="session_1",
        task_id="task_1",
        instruction="Do work.",
        remaining_timeout_seconds=60,
        agent=AgentSpec(
            harness="claude_code",
            settings={"capture_mode": "transcript"},
        ),
        metadata={},
    )
    managed = ManagedSession(
        request=request,
        timer=StageTimer(),
        session_dir=tmp_path,
        artifacts_dir=tmp_path / "artifacts",
        agent_result=AgentRunResult(
            status="completed",
            return_code=0,
            metadata={"log_dir": str(log_dir), "last_step": 0},
        ),
    )
    managed.execution_deadline = asyncio.get_running_loop().time() + 60

    result = await manager._build_session_result(managed)

    assert result.status == "COMPLETED"
    assert result.trajectory.metadata["builder"] == "agent_transcript"
    assert result.trajectory.metadata["capture_mode"] == "transcript"
    assert result.trajectory.traces[0].response_messages == [
        {"role": "assistant", "content": "Used non-Codex transcript mode."}
    ]


@pytest.mark.asyncio
async def test_transcript_fallback_requires_explicit_capture_mode(tmp_path):
    log_dir = tmp_path / "logs" / "agent"
    log_dir.mkdir(parents=True)
    (log_dir / "step.00.stdout.log").write_text(
        json.dumps(
            {
                "type": "message",
                "message": {"role": "assistant", "content": "Should not be used."},
            }
        ),
        encoding="utf-8",
    )
    manager = GatewayNodeManager.__new__(GatewayNodeManager)
    manager.node_id = "node-a"
    manager.storage = EmptyCompletionStorage()
    manager.builders = default_builder_registry()
    manager.session_registry = SessionRegistry()
    manager.session_registry.register("session_1", task_id="task_1")
    request = SessionDispatchRequest(
        session_id="session_1",
        task_id="task_1",
        instruction="Do work.",
        remaining_timeout_seconds=60,
        agent=AgentSpec(
            harness="codex",
            settings={"auth_mode": "subscription"},
        ),
        metadata={},
    )
    managed = ManagedSession(
        request=request,
        timer=StageTimer(),
        session_dir=tmp_path,
        artifacts_dir=tmp_path / "artifacts",
        agent_result=AgentRunResult(
            status="completed",
            return_code=0,
            metadata={"log_dir": str(log_dir), "last_step": 0},
        ),
    )
    managed.execution_deadline = asyncio.get_running_loop().time() + 60

    result = await manager._build_session_result(managed)

    assert result.status == "ERROR"
    assert result.trajectory.metadata["builder"] == "per_request"
    assert result.trajectory.error == "no completions"
    assert result.trajectory.traces == []


@pytest.mark.asyncio
async def test_proxy_completion_path_does_not_inject_agent_metadata_into_task_metadata(
    tmp_path,
):
    manager = GatewayNodeManager.__new__(GatewayNodeManager)
    manager.node_id = "node-a"
    manager.storage = OneCompletionStorage()
    manager.builders = default_builder_registry()
    manager.session_registry = SessionRegistry()
    manager.session_registry.register("session_1", task_id="task_1")
    request = SessionDispatchRequest(
        session_id="session_1",
        task_id="task_1",
        instruction="Do work.",
        remaining_timeout_seconds=60,
        agent=AgentSpec(
            harness="codex",
            settings={"auth_mode": "proxy"},
            env={"OPENAI_API_KEY": "secret"},
        ),
        metadata={"policy_version": "policy_1"},
    )
    managed = ManagedSession(
        request=request,
        timer=StageTimer(),
        session_dir=tmp_path,
        artifacts_dir=tmp_path / "artifacts",
        agent_result=AgentRunResult(
            status="completed",
            return_code=0,
            metadata={"log_dir": str(tmp_path / "logs" / "agent"), "last_step": 0},
        ),
    )
    managed.execution_deadline = asyncio.get_running_loop().time() + 60

    result = await manager._build_session_result(managed)

    assert result.status == "COMPLETED"
    assert result.trajectory.metadata["builder"] == "per_request"
    assert result.trajectory.metadata["task_metadata"] == {"source": "proxy"}
    assert "agent" not in result.trajectory.metadata["task_metadata"]
    assert "agent_result" not in result.trajectory.metadata["task_metadata"]
    assert result.trajectory.traces[0].response_ids == [3]
    assert result.trajectory.traces[0].response_logprobs == [-0.1]


@pytest.mark.asyncio
async def test_handle_run_prepends_rendered_memory_to_agent_instruction(tmp_path, monkeypatch):
    class InstructionHarness(RunStepHarness):
        def __init__(self, agent_spec: AgentSpec) -> None:
            super().__init__(agent_spec)
            self.instructions: list[str] = []

        def run_steps(self, instruction: str) -> list[ExecInput]:
            self.instructions.append(instruction)
            return [ExecInput(command="echo run")]

    runtime = BindMountRuntime(tmp_path)
    client = FakeEvolutionClient(
        context={
            "context_id": "ctx_1",
            "memory": {"rendered_text": "Always preserve parser precedence."},
        }
    )
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
        agent=AgentSpec(harness="fake"),
        metadata={},
    )
    harness = InstructionHarness(request.agent)
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

    assert harness.instructions == [
        (
            "Use the following long-term memory for this task:\n"
            "Always preserve parser precedence.\n\n"
            "Task:\nDo work."
        )
    ]


@pytest.mark.asyncio
async def test_handle_run_prepends_agent_system_before_memory(tmp_path, monkeypatch):
    class InstructionHarness(RunStepHarness):
        def __init__(self, agent_spec: AgentSpec) -> None:
            super().__init__(agent_spec)
            self.instructions: list[str] = []

        def run_steps(self, instruction: str) -> list[ExecInput]:
            self.instructions.append(instruction)
            return [ExecInput(command="echo run")]

    runtime = BindMountRuntime(tmp_path)
    client = FakeEvolutionClient(
        context={
            "context_id": "ctx_1",
            "agent_system": {
                "rendered_text": "Prefer repository-local conventions.",
                "target_path": "AGENTS.md",
            },
            "memory": {"rendered_text": "Always preserve parser precedence."},
        }
    )
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
        agent=AgentSpec(harness="fake"),
        metadata={},
    )
    harness = InstructionHarness(request.agent)
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

    assert harness.instructions == [
        (
            "Use the following evolved agent system instructions for this task:\n"
            "Prefer repository-local conventions.\n\n"
            "Use the following long-term memory for this task:\n"
            "Always preserve parser precedence.\n\n"
            "Task:\nDo work."
        )
    ]
