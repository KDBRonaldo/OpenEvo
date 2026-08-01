from __future__ import annotations

import asyncio
from dataclasses import replace
import hashlib
import json
import multiprocessing
import os
import subprocess
from tempfile import TemporaryDirectory
import threading
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock

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
from openevo.gateway.dispatcher import SessionDispatcher, SessionStage
from openevo.gateway.node import (
    CancelAuthorityPersistenceError,
    GatewayExecutionTimeout,
    GatewayNodeManager,
    build_evolution_session_event,
    write_evolution_context_files,
)
from openevo.gateway.session import SessionRegistry
from openevo.gateway.storage import SessionStore
from openevo.gateway import session_files
from openevo.gateway import node as node_module
from openevo.gateway.session_files import capture_session_root_identity
from openevo.rollout.models import (
    SessionDispatchRequest,
    SessionResult,
    SessionStatus,
    SessionTiming,
)
from openevo.rollout.timer import StageTimer
from openevo.runtime import base as runtime_base
from openevo.runtime.base import (
    BaseRuntime,
    RuntimePathSecurityError,
    RuntimeReadbackBudget,
)
from openevo.runtime.codex_isolation import (
    CODEX_SUBSCRIPTION_CANARY_OK,
    codex_subscription_readiness_receipt,
)
from openevo.runtime.managed import (
    MANAGED_CODEX_HOME,
    MANAGED_RUNTIME_IMAGES,
    MANAGED_RUNTIME_RELEASES,
    MANAGED_SUBSCRIPTION_PREPARE_COMMAND,
    MANAGED_WORKSPACE,
    ManagedCredentialMount,
)
from openevo.runtime.models import ExecInput, ExecResult, RuntimeSpec
from openevo.runtime.models import PrepareAction
from openevo.trajectory.builder.agent_transcript import AgentTranscriptBuilder
from openevo.trajectory.models import (
    CompletionRecord,
    CompletionSession,
    EvaluatorSpec,
    Trajectory,
)
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
        base_url: str = "http://127.0.0.1:8200",
    ) -> None:
        self.base_url = base_url.rstrip("/")
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
    session_dir.mkdir(mode=0o700)
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
    runtime_spec = RuntimeSpec(
        profile="managed_science",
        image=MANAGED_RUNTIME_RELEASES["managed_science"].immutable_reference,
        container_user="host",
    )
    request = SessionDispatchRequest(
        session_id="session_1",
        task_id="task_1",
        instruction="Do work.",
        remaining_timeout_seconds=60,
        runtime=runtime_spec,
        agent=AgentSpec(
            harness="codex",
            model_name="gpt-5.5",
            settings={"auth_mode": "subscription", "capture_mode": "transcript"},
        ),
        metadata={},
    )
    assert request.runtime is not None
    object.__setattr__(request.runtime, "container_user", "image")
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
    assert "runtime.container_user='host'" in (managed.final_result.error or "")
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


@pytest.mark.parametrize("capture_mode", ["transcript", "agent_transcript", "pure_text"])
def test_gateway_subscription_admission_accepts_transcript_capture_aliases(
    tmp_path: Path,
    capture_mode: str,
) -> None:
    session_dir = tmp_path / "session"
    session_dir.mkdir(mode=0o700)
    request = SessionDispatchRequest(
        session_id="subscription-capture-alias",
        task_id="task_1",
        instruction="Do work.",
        remaining_timeout_seconds=60,
        runtime=RuntimeSpec(
            profile="managed_science",
            image=MANAGED_RUNTIME_RELEASES["managed_science"].immutable_reference,
            container_user="host",
            workdir=MANAGED_WORKSPACE,
            prepare=[
                PrepareAction(
                    type="exec",
                    command=MANAGED_SUBSCRIPTION_PREPARE_COMMAND,
                )
            ],
        ),
        agent=AgentSpec(
            harness="codex",
            settings={"auth_mode": "subscription", "capture_mode": capture_mode},
        ),
    )

    GatewayNodeManager._validate_subscription_admission(
        request,
        request.runtime,
        session_dir,
    )
    assert request.agent.settings["capture_mode"] == "transcript"


def test_gateway_subscription_admission_rejects_token_capture(tmp_path: Path) -> None:
    session_dir = tmp_path / "session"
    session_dir.mkdir(mode=0o700)
    request = SessionDispatchRequest(
        session_id="subscription-token-capture",
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
            settings={"auth_mode": "subscription", "capture_mode": "token"},
        ),
    )

    with pytest.raises(RuntimeError, match="transcript capture"):
        GatewayNodeManager._validate_subscription_admission(
            request,
            request.runtime,
            session_dir,
        )


@pytest.mark.parametrize("auth_mode", ["proxy", "subscription"])
@pytest.mark.parametrize("capture_mode", ["transcript", "agent_transcript", "pure_text"])
def test_gateway_boundary_writes_back_canonical_capture_mode(
    auth_mode: str,
    capture_mode: str,
) -> None:
    request = SessionDispatchRequest(
        session_id="gateway-capture-matrix",
        task_id="task_1",
        instruction="Do work.",
        remaining_timeout_seconds=60,
        agent=AgentSpec(
            harness="codex",
            settings={"auth_mode": auth_mode, "capture_mode": capture_mode},
        ),
    )

    GatewayNodeManager._canonicalize_request_capture_mode(request)

    assert request.agent.settings["capture_mode"] == "transcript"


@pytest.mark.asyncio
async def test_invalid_subscription_auth_fails_before_runtime_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    source = home / ".codex" / "auth.json"
    source.parent.mkdir(parents=True)
    source.write_text("not-json", encoding="utf-8")
    source.chmod(0o600)
    monkeypatch.setenv("HOME", str(home))
    session_dir = tmp_path / "session"
    session_dir.mkdir(mode=0o700)
    (session_dir / "artifacts").mkdir()
    request = SessionDispatchRequest(
        session_id="invalid-auth-before-runtime",
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
        session_dir=session_dir,
        artifacts_dir=session_dir / "artifacts",
        session_root_identity=capture_session_root_identity(session_dir),
    )
    manager = GatewayNodeManager.__new__(GatewayNodeManager)
    manager.node_id = "gateway-test"
    manager.default_runtime = None
    manager._cleanup_retries = {}
    manager._cleanup_journal_dir = tmp_path / "journal"
    manager._docker_ownership_root = tmp_path / "docker-ownership"
    manager._docker_host_path = None
    create = Mock(side_effect=AssertionError("runtime must not be created"))
    monkeypatch.setattr("openevo.gateway.node.create_runtime", create)

    await manager._handle_init(managed)

    create.assert_not_called()
    assert managed.runtime is None
    assert managed.final_result is not None
    assert managed.final_result.status == SessionStatus.ERROR
    assert managed.credential_dir is None


@pytest.mark.asyncio
async def test_subscription_initialization_exception_log_redacts_credential_canary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "initialization-secret-canary"
    session_dir = tmp_path / "session"
    session_dir.mkdir(mode=0o700)
    (session_dir / "artifacts").mkdir()
    request = SessionDispatchRequest(
        session_id="initialization-log-canary",
        task_id="task_1",
        instruction="Do work.",
        remaining_timeout_seconds=60,
        runtime=RuntimeSpec(
            profile="managed_science",
            image=MANAGED_RUNTIME_RELEASES["managed_science"].immutable_reference,
            container_user="host",
            workdir=MANAGED_WORKSPACE,
            prepare=[
                PrepareAction(
                    type="exec",
                    command=MANAGED_SUBSCRIPTION_PREPARE_COMMAND,
                )
            ],
        ),
        agent=AgentSpec(
            harness="codex",
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
    manager._cleanup_retries = {}
    manager._cleanup_journal_dir = tmp_path / "journal"
    manager._docker_ownership_root = tmp_path / "docker-ownership"
    manager._docker_host_path = None
    credential_dir = tmp_path / "credentials"
    credential_dir.mkdir(mode=0o700)
    auth = credential_dir / "auth.json"
    auth.write_text(f'{{"access_token":"{secret}"}}\n', encoding="utf-8")
    auth.chmod(0o600)
    state = auth.stat(follow_symlinks=False)
    auth_identity = (
        state.st_dev,
        state.st_ino,
        state.st_mode,
        state.st_uid,
        state.st_nlink,
        state.st_size,
        state.st_mtime_ns,
        state.st_ctime_ns,
    )
    managed.credential_dir = credential_dir
    managed.credential_root_identity = capture_session_root_identity(credential_dir)
    managed.credential_auth_identity = auth_identity
    managed.credential_redactor = session_files.CredentialRedactor.from_auth_json(
        auth.read_bytes()
    )
    managed.credential_mount = ManagedCredentialMount(
        root=credential_dir,
        root_identity=managed.credential_root_identity,
        auth_identity=auth_identity,
    )
    monkeypatch.setattr(
        node_module,
        "create_runtime",
        Mock(side_effect=RuntimeError(f"runtime rejected {secret}")),
    )

    with caplog.at_level("ERROR", logger="openevo.gateway.node"):
        await manager._handle_init(managed)

    assert secret not in caplog.text
    assert "Traceback" not in caplog.text
    assert "[REDACTED: Codex credential]" in caplog.text


@pytest.mark.asyncio
async def test_subscription_postprocess_exception_log_redacts_credential_canary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "postprocess-secret-canary"

    class FailingPostprocessHarness(RunStepHarness):
        @property
        def subscription_credential_isolation_receipt(self) -> dict[str, object]:
            return codex_subscription_readiness_receipt()

        def run_steps(self, instruction: str) -> list[ExecInput]:
            del instruction
            return []

        async def postprocess(self, runtime, result) -> None:
            del runtime, result
            raise RuntimeError(f"postprocess exposed {secret}")

    session_dir = tmp_path / "session"
    credential_dir = tmp_path / "credentials"
    log_dir = tmp_path / "logs"
    for root in (session_dir, credential_dir, log_dir):
        root.mkdir(mode=0o700)
    auth = credential_dir / "auth.json"
    auth.write_text(f'{{"access_token":"{secret}"}}\n', encoding="utf-8")
    auth.chmod(0o600)
    request = SessionDispatchRequest(
        session_id="postprocess-log-canary",
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
    runtime = BindMountRuntime(session_dir)
    managed = ManagedSession(
        request=request,
        timer=StageTimer(),
        session_dir=session_dir,
        artifacts_dir=session_dir / "artifacts",
        session_root_identity=capture_session_root_identity(session_dir),
        log_authority_dir=log_dir,
        log_authority_identity=capture_session_root_identity(log_dir),
        credential_dir=credential_dir,
        credential_root_identity=capture_session_root_identity(credential_dir),
        credential_redactor=session_files.CredentialRedactor.from_auth_json(auth.read_bytes()),
        runtime=runtime,
    )
    managed.execution_deadline = asyncio.get_running_loop().time() + 60
    manager = GatewayNodeManager.__new__(GatewayNodeManager)
    manager.node_id = "gateway-test"
    manager.gateway_url = "http://gateway.test"
    manager.evolution = EvolutionConfig(enabled=False)
    manager.evolution_client = None
    manager._cleanup_retries = {}
    manager._cleanup_journal_dir = tmp_path / "journal"
    harness = FailingPostprocessHarness(request.agent)
    monkeypatch.setattr(manager, "_resolve_agent_harness", lambda _request: harness)

    with caplog.at_level("ERROR", logger="openevo.gateway.node"):
        await manager._handle_run(managed)

    assert secret not in caplog.text
    assert "Traceback" not in caplog.text
    assert "[REDACTED: Codex credential]" in caplog.text


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


def test_gateway_rejects_subscription_runtime_environment_extensions(
    tmp_path: Path,
) -> None:
    runtime = RuntimeSpec(
        profile="managed_science",
        image=MANAGED_RUNTIME_IMAGES["managed_science"],
        container_user="host",
        env={"BASH_ENV": "/openevo/session/workspace/attacker.sh"},
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
        ),
    )

    with pytest.raises(RuntimeError, match="non-Core fields"):
        GatewayNodeManager._validate_subscription_admission(request, runtime, tmp_path)


@pytest.mark.parametrize(
    "field",
    ["prepare", "eval_prepare"],
)
def test_gateway_rejects_subscription_runtime_action_extensions(
    tmp_path: Path,
    field: str,
) -> None:
    action = PrepareAction(type="exec", command="touch /tmp/attacker")
    runtime = RuntimeSpec(
        profile="managed_science",
        image=MANAGED_RUNTIME_RELEASES["managed_science"].immutable_reference,
        container_user="host",
        workdir=MANAGED_WORKSPACE,
        **{field: [action]},
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
        ),
    )

    message = "eval_prepare actions" if field == "eval_prepare" else "Core-managed prepare recipe"
    with pytest.raises(RuntimeError, match=message):
        GatewayNodeManager._validate_subscription_admission(request, runtime, tmp_path)


def test_gateway_accepts_exact_subscription_prepare_recipe(tmp_path: Path) -> None:
    runtime = RuntimeSpec(
        profile="managed_science",
        image=MANAGED_RUNTIME_RELEASES["managed_science"].immutable_reference,
        container_user="host",
        workdir=MANAGED_WORKSPACE,
        prepare=[
            PrepareAction(
                type="exec",
                command=MANAGED_SUBSCRIPTION_PREPARE_COMMAND,
            )
        ],
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
        ),
    )

    GatewayNodeManager._validate_subscription_admission(request, runtime, tmp_path)


def test_gateway_accepts_core_workspace_upload_before_subscription_prepare(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runtime = RuntimeSpec(
        profile="managed_science",
        image=MANAGED_RUNTIME_RELEASES["managed_science"].immutable_reference,
        container_user="host",
        workdir=MANAGED_WORKSPACE,
        prepare=[
            PrepareAction(
                type="upload_dir",
                source=str(workspace),
                target=MANAGED_WORKSPACE,
            ),
            PrepareAction(
                type="exec",
                command=MANAGED_SUBSCRIPTION_PREPARE_COMMAND,
            ),
        ],
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
        ),
    )

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
async def test_required_export_without_client_remains_pending() -> None:
    manager = GatewayNodeManager.__new__(GatewayNodeManager)
    manager.evolution = EvolutionConfig(
        enabled=True,
        event_export={"fail_open": True},
    )
    manager.evolution_client = None

    assert await manager._export_evolution_event(_session_result()) is False


@pytest.mark.asyncio
async def test_required_fail_closed_export_without_client_raises() -> None:
    manager = GatewayNodeManager.__new__(GatewayNodeManager)
    manager.evolution = EvolutionConfig(
        enabled=True,
        event_export={"fail_open": False},
    )
    manager.evolution_client = None

    with pytest.raises(RuntimeError, match="export client is unavailable"):
        await manager._export_evolution_event(_session_result())


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

    def get(self, session_id: str) -> object:
        del session_id
        return self

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
        return True

    async def remove_credential_dir(
        credential_dir,
        session_id,
        credential_root_identity=None,
        credential_auth_identity=None,
    ):
        calls.append("remove_credential_dir")
        return True

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


def _assert_only_retired_cleanup_record(journal_dir: Path) -> dict:
    records = list(journal_dir.glob("*.json"))
    assert len(records) == 1
    payload = json.loads(records[0].read_text(encoding="utf-8"))
    assert payload["version"] == 9
    assert payload["kind"] == "retired"
    assert payload["epoch"] >= 0
    assert payload["epoch_token"]
    assert payload["generation"]
    return payload


def _retire_minimal_cleanup_session(
    manager: GatewayNodeManager,
    session_dir: Path,
    session_id: str,
):
    managed = _managed_postrun_session(
        session_dir,
        _session_result(session_id=session_id),
    )
    managed.session_root_identity = capture_session_root_identity(session_dir)
    active = manager._persist_cleanup_ownership(manager._cleanup_ownership_for(managed))
    return manager._retire_cleanup_ownership(active)


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
        "callback_push",
        "clear_result_payload",
        "delete_session",
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
async def test_proxy_workspace_handoff_uses_terminal_finalization_path(tmp_path) -> None:
    calls: list[str] = []
    manager = _postrun_manager(calls=calls)
    managed = _managed_postrun_session(tmp_path, _session_result())
    managed.request = managed.request.model_copy(update={"workspace_handoff": object()})
    managed.runtime = object()

    async def terminal_finalization(captured: ManagedSession) -> None:
        assert captured is managed
        calls.append("terminal_finalization")

    async def standard_postrun(captured: ManagedSession) -> None:
        assert captured is managed
        calls.append("standard_postrun")

    manager._handle_terminal_finalization_postrun = terminal_finalization
    manager._handle_standard_postrun = standard_postrun

    await manager._handle_postrun(managed)

    assert calls == ["terminal_finalization"]


@pytest.mark.asyncio
async def test_subscription_stops_runtime_and_redacts_background_output_before_result(
    tmp_path: Path,
) -> None:
    calls: list[str] = []
    manager = _postrun_manager(calls=calls)
    manager.builders = default_builder_registry()
    manager.node_id = "node-a"
    secret = "access-canary"
    redactor = session_files.CredentialRedactor.from_auth_json(b'{"access_token":"access-canary"}')
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
async def test_subscription_evaluator_runs_before_runtime_is_stopped(
    tmp_path: Path,
) -> None:
    calls: list[str] = []
    manager = _postrun_manager(calls=calls)

    class EvaluatedRuntime(FakeRuntime):
        stopped = False

        async def stop(self) -> None:
            self.stopped = True
            calls.append("runtime_absent")

    runtime = EvaluatedRuntime()
    request = SessionDispatchRequest(
        session_id="subscription-evaluator",
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
        evaluator=EvaluatorSpec(strategy="pass_at_k"),
    )
    managed = ManagedSession(
        request=request,
        timer=StageTimer(),
        session_dir=tmp_path,
        artifacts_dir=tmp_path / "artifacts",
        session_root_identity=capture_session_root_identity(tmp_path),
        runtime=runtime,
        agent_result=AgentRunResult(status="completed", return_code=0),
    )

    async def build_result(captured: ManagedSession) -> SessionResult:
        assert captured.runtime is runtime
        assert runtime.stopped is False
        calls.append("evaluate")
        return _session_result(session_id=request.session_id)

    async def finalize(
        captured: ManagedSession,
        *,
        result: SessionResult | None,
    ) -> None:
        assert captured is managed
        assert result is not None
        assert captured.final_result is result
        calls.append("finalize")

    manager._build_session_result = build_result
    manager._finalize_after_runtime_absence = finalize

    await manager._handle_postrun(managed)

    assert calls.index("evaluate") < calls.index("runtime_absent")
    assert calls[-1] == "finalize"


class _PostrunBaseException(BaseException):
    pass


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_stage", ["postrun", "evaluator"])
@pytest.mark.parametrize("failure_type", [asyncio.CancelledError, _PostrunBaseException])
async def test_subscription_postrun_base_exception_still_stops_all_runtimes(
    tmp_path: Path,
    failure_stage: str,
    failure_type: type[BaseException],
) -> None:
    calls: list[str] = []
    manager = _postrun_manager(calls=calls)
    main_runtime = FakeRuntime()
    eval_runtime = FakeRuntime()
    stopped: list[BaseRuntime] = []
    request = SessionDispatchRequest(
        session_id="subscription-base-exception",
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
        evaluator=EvaluatorSpec(strategy="pass_at_k"),
    )
    managed = ManagedSession(
        request=request,
        timer=StageTimer(),
        session_dir=tmp_path,
        artifacts_dir=tmp_path / "artifacts",
        session_root_identity=capture_session_root_identity(tmp_path),
        runtime=main_runtime,
        agent_result=AgentRunResult(status="completed", return_code=0),
        cancel_requested=True,
    )

    async def run_postrun_steps(captured: ManagedSession) -> None:
        assert captured is managed
        calls.append("postrun")
        if failure_stage == "postrun":
            raise failure_type()

    async def build_result(captured: ManagedSession) -> SessionResult:
        assert captured is managed
        calls.append("evaluator")
        if failure_stage == "evaluator":
            raise failure_type()
        return _session_result(session_id=request.session_id)

    async def drain(captured: ManagedSession) -> BaseRuntime:
        assert captured is managed
        calls.append("drain")
        return eval_runtime

    async def stop_runtime(
        runtime: BaseRuntime,
        session_id: str,
        label: str,
    ) -> bool:
        assert session_id == request.session_id
        assert label in {"eval runtime", "runtime"}
        stopped.append(runtime)
        return True

    manager._run_postrun_steps = run_postrun_steps
    manager._build_session_result = build_result
    manager._drain_eval_prewarm_task = drain
    manager._stop_runtime_best_effort = stop_runtime

    with pytest.raises(failure_type):
        await manager._handle_terminal_finalization_postrun(managed)

    assert calls[-1] == "drain"
    assert stopped == [eval_runtime, main_runtime]
    retry = manager._cleanup_retries[managed.session_id]
    assert retry.finalization_state is not None
    assert retry.finalization_state.cancel_requested is True


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_type", [asyncio.CancelledError, _PostrunBaseException])
async def test_standard_postrun_base_exception_drains_and_stops_all_runtimes(
    tmp_path: Path,
    failure_type: type[BaseException],
) -> None:
    calls: list[str] = []
    manager = _postrun_manager(calls=calls)
    manager._cleanup_journal_dir = tmp_path / "journal"
    session_dir = tmp_path / "session"
    session_dir.mkdir(mode=0o700)
    main_runtime = FakeRuntime()
    eval_runtime = FakeRuntime()
    stopped: list[BaseRuntime] = []
    managed = _managed_postrun_session(
        session_dir,
        _session_result(session_id="standard-base-exception"),
    )
    managed.session_root_identity = capture_session_root_identity(session_dir)
    managed.runtime = main_runtime

    async def fail_postrun(_: ManagedSession) -> None:
        raise failure_type("postrun interrupted")

    async def drain(_: ManagedSession) -> BaseRuntime:
        calls.append("drain")
        return eval_runtime

    async def stop_runtime(
        runtime: BaseRuntime,
        session_id: str,
        label: str,
    ) -> bool:
        assert session_id == managed.session_id
        assert label in {"eval runtime", "runtime"}
        stopped.append(runtime)
        return True

    manager._run_postrun_steps = fail_postrun
    manager._drain_eval_prewarm_task = drain
    manager._stop_runtime_best_effort = stop_runtime

    with pytest.raises(failure_type):
        await manager._handle_standard_postrun(managed)

    assert calls[-1] == "drain"
    assert set(stopped) == {eval_runtime, main_runtime}
    assert managed.session_id in manager._cleanup_retries
    assert list(manager._cleanup_journal_dir.glob("*.json"))


@pytest.mark.asyncio
async def test_standard_postrun_combines_primary_and_cleanup_base_exceptions(
    tmp_path: Path,
) -> None:
    manager = _postrun_manager(calls=[])
    manager._cleanup_journal_dir = tmp_path / "journal"
    session_dir = tmp_path / "session"
    session_dir.mkdir(mode=0o700)
    main_runtime = FakeRuntime()
    eval_runtime = FakeRuntime()
    stopped: list[BaseRuntime] = []
    managed = _managed_postrun_session(
        session_dir,
        _session_result(session_id="standard-combined-failure"),
    )
    managed.session_root_identity = capture_session_root_identity(session_dir)
    managed.runtime = main_runtime

    async def fail_postrun(_: ManagedSession) -> None:
        raise _PostrunBaseException("primary postrun failure")

    async def drain(_: ManagedSession) -> BaseRuntime:
        return eval_runtime

    async def stop_runtime(
        runtime: BaseRuntime,
        session_id: str,
        label: str,
    ) -> bool:
        del session_id, label
        stopped.append(runtime)
        if runtime is eval_runtime:
            raise _PostrunBaseException("eval cleanup failure")
        return True

    manager._run_postrun_steps = fail_postrun
    manager._drain_eval_prewarm_task = drain
    manager._stop_runtime_best_effort = stop_runtime

    with pytest.raises(BaseExceptionGroup) as captured:
        await manager._handle_standard_postrun(managed)

    assert [str(error) for error in captured.value.exceptions] == [
        "primary postrun failure",
        "eval cleanup failure",
    ]
    assert stopped == [eval_runtime, main_runtime]
    assert managed.session_id in manager._cleanup_retries


@pytest.mark.asyncio
async def test_standard_postrun_cancellation_during_stop_waits_for_all_cleanup(
    tmp_path: Path,
) -> None:
    manager = _postrun_manager(calls=[])
    manager._cleanup_journal_dir = tmp_path / "journal"
    session_dir = tmp_path / "session"
    session_dir.mkdir(mode=0o700)
    main_runtime = FakeRuntime()
    eval_runtime = FakeRuntime()
    managed = _managed_postrun_session(
        session_dir,
        _session_result(session_id="standard-stop-cancel"),
    )
    managed.session_root_identity = capture_session_root_identity(session_dir)
    managed.runtime = main_runtime
    handler_task = asyncio.current_task()
    assert handler_task is not None
    stopped: list[BaseRuntime] = []

    async def drain(_: ManagedSession) -> BaseRuntime:
        return eval_runtime

    async def stop_runtime(
        runtime: BaseRuntime,
        session_id: str,
        label: str,
    ) -> bool:
        del session_id, label
        if runtime is eval_runtime:
            handler_task.cancel()
            await asyncio.sleep(0)
        stopped.append(runtime)
        return True

    manager._drain_eval_prewarm_task = drain
    manager._stop_runtime_best_effort = stop_runtime

    with pytest.raises(asyncio.CancelledError):
        await manager._handle_standard_postrun(managed)

    assert set(stopped) == {eval_runtime, main_runtime}
    assert managed.session_id in manager._cleanup_retries


@pytest.mark.asyncio
async def test_subscription_cancel_overrides_existing_evaluator_result(
    tmp_path: Path,
) -> None:
    calls: list[str] = []
    manager = _postrun_manager(calls=calls)
    managed = _managed_postrun_session(
        tmp_path,
        _session_result(session_id="subscription-cancel-result"),
    )
    managed.request.agent = AgentSpec(
        harness="codex",
        settings={"auth_mode": "subscription", "capture_mode": "transcript"},
    )
    managed.request.evaluator = EvaluatorSpec(strategy="pass_at_k")
    managed.runtime = FakeRuntime()
    managed.cancel_requested = True
    captured_results: list[SessionResult] = []

    async def stop_all(targets, session_id: str) -> bool:
        del targets, session_id
        return True

    async def deliver(captured: ManagedSession, result: SessionResult) -> bool:
        assert captured is managed
        captured_results.append(result)
        return False

    manager._stop_terminal_runtimes_with_retry = stop_all
    manager._deliver_terminal_result = deliver

    await manager._handle_terminal_finalization_postrun(managed)

    assert len(captured_results) == 1
    result = captured_results[0]
    assert result is not None
    assert result.status == SessionStatus.ERROR
    assert result.error == "session cancelled"


def _attach_cancellable_subscription(
    manager: GatewayNodeManager,
    managed: ManagedSession,
) -> None:
    manager._dispatcher = SessionDispatcher(
        max_init_workers=1,
        max_run_workers=1,
        max_postrun_workers=1,
    )
    manager._dispatcher._started = True
    manager._dispatcher._sessions[managed.session_id] = managed
    managed.stage = SessionStage.RUNNING
    manager._register_cleanup_retry(managed, finalize_terminal=True)


@pytest.mark.asyncio
async def test_cancel_authority_is_durable_before_runtime_side_effect(
    tmp_path: Path,
) -> None:
    calls: list[str] = []
    manager = _postrun_manager(calls=calls)
    manager._cleanup_journal_dir = tmp_path / "journal"
    session_dir = tmp_path / "session"
    session_dir.mkdir(mode=0o700)

    class InspectingRuntime(FakeRuntime):
        async def cancel(self) -> None:
            journal = json.loads(next(manager._cleanup_journal_dir.glob("*.json")).read_text())
            assert journal["subscription_finalization"]["cancel_requested"] is True
            calls.append("runtime_cancel")

    managed = _managed_postrun_session(
        session_dir,
        _session_result(session_id="durable-cancel-before-side-effect"),
    )
    managed.request.agent = AgentSpec(
        harness="codex",
        settings={"auth_mode": "subscription", "capture_mode": "transcript"},
    )
    managed.session_root_identity = capture_session_root_identity(session_dir)
    managed.runtime = InspectingRuntime()
    _attach_cancellable_subscription(manager, managed)

    assert await manager.cancel(managed.session_id) is True

    assert calls == ["runtime_cancel"]
    assert managed.cancel_requested is True
    persisted = json.loads(next(manager._cleanup_journal_dir.glob("*.json")).read_text())
    assert persisted["subscription_finalization"]["cancel_requested"] is True


@pytest.mark.asyncio
async def test_cancel_persistence_failure_does_not_touch_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _postrun_manager(calls=[])
    manager._cleanup_journal_dir = tmp_path / "journal"
    session_dir = tmp_path / "session"
    session_dir.mkdir(mode=0o700)

    class ObservedRuntime(FakeRuntime):
        cancel_called = False

        async def cancel(self) -> None:
            self.cancel_called = True

    runtime = ObservedRuntime()
    managed = _managed_postrun_session(
        session_dir,
        _session_result(session_id="cancel-fsync-failure"),
    )
    managed.request.agent = AgentSpec(
        harness="codex",
        settings={"auth_mode": "subscription", "capture_mode": "transcript"},
    )
    managed.session_root_identity = capture_session_root_identity(session_dir)
    managed.runtime = runtime
    _attach_cancellable_subscription(manager, managed)

    def fail_fsync(root_fd: int) -> None:
        del root_fd
        raise OSError("injected cancel fsync failure")

    monkeypatch.setattr(manager, "_fsync_cleanup_journal_directory", fail_fsync)

    with pytest.raises(CancelAuthorityPersistenceError) as captured:
        await manager.cancel(managed.session_id)

    assert isinstance(captured.value.__cause__, OSError)
    assert runtime.cancel_called is False
    assert managed.cancel_requested is False
    assert managed.cancel_event.is_set() is False


@pytest.mark.asyncio
async def test_cancel_crash_window_recovers_durable_cancel_authority(
    tmp_path: Path,
) -> None:
    manager = _postrun_manager(calls=[])
    manager._cleanup_journal_dir = tmp_path / "journal"
    session_dir = tmp_path / "session"
    session_dir.mkdir(mode=0o700)

    class CrashingRuntime(FakeRuntime):
        async def cancel(self) -> None:
            raise _PostrunBaseException("crash after runtime cancel began")

    managed = _managed_postrun_session(
        session_dir,
        _session_result(session_id="cancel-crash-window"),
    )
    managed.request.agent = AgentSpec(
        harness="codex",
        settings={"auth_mode": "subscription", "capture_mode": "transcript"},
    )
    managed.session_root_identity = capture_session_root_identity(session_dir)
    managed.runtime = CrashingRuntime()
    _attach_cancellable_subscription(manager, managed)

    with pytest.raises(_PostrunBaseException):
        await manager.cancel(managed.session_id)

    restarted = _postrun_manager(calls=[])
    restarted._cleanup_journal_dir = manager._cleanup_journal_dir
    restarted._load_cleanup_retries()
    recovered = restarted._cleanup_retries[managed.session_id]
    assert recovered.finalization_state is not None
    assert recovered.finalization_state.cancel_requested is True


def test_durable_cancel_is_monotonic_over_completed_result_transition(
    tmp_path: Path,
) -> None:
    manager = _postrun_manager(calls=[])
    manager._cleanup_journal_dir = tmp_path / "journal"
    session_dir = tmp_path / "session"
    session_dir.mkdir(mode=0o700)
    managed = _managed_postrun_session(
        session_dir,
        _session_result(session_id="cancel-complete-race"),
    )
    managed.request.agent = AgentSpec(
        harness="codex",
        settings={"auth_mode": "subscription", "capture_mode": "transcript"},
    )
    managed.session_root_identity = capture_session_root_identity(session_dir)
    manager._register_cleanup_retry(managed, finalize_terminal=True)

    manager._persist_terminal_finalization_authority(
        managed,
        cancel_requested=True,
    )
    manager._record_terminal_agent_result(
        managed,
        AgentRunResult(status="completed", return_code=0),
    )

    persisted = json.loads(next(manager._cleanup_journal_dir.glob("*.json")).read_text())
    assert persisted["subscription_finalization"]["cancel_requested"] is True
    assert manager._cleanup_retries[managed.session_id].finalization_state.cancel_requested is True

    restarted = _postrun_manager(calls=[])
    restarted._cleanup_journal_dir = manager._cleanup_journal_dir
    restarted._load_cleanup_retries()
    recovered = restarted._cleanup_retries[managed.session_id]
    assert recovered.finalization_state is not None
    assert recovered.finalization_state.cancel_requested is True


def test_subscription_capture_redaction_never_modifies_workspace_files(
    tmp_path: Path,
) -> None:
    secret = "access-canary"
    session_dir = tmp_path / "session"
    log_dir = tmp_path / "core-logs"
    credential_dir = tmp_path / "credentials"
    for root in (session_dir, log_dir, credential_dir):
        root.mkdir()
    workspace_file = session_dir / "workspace" / "research-output.bin"
    workspace_file.parent.mkdir()
    original = secret.encode() + b"\n" + b"x" * (session_files._CAPTURE_REDACTION_MAX_BYTES + 1)
    workspace_file.write_bytes(original)
    authority_log = log_dir / "logs" / "agent" / "step.00.stdout.log"
    authority_log.parent.mkdir(parents=True)
    authority_log.write_text(f"captured {secret}", encoding="utf-8")
    managed = ManagedSession(
        request=SessionDispatchRequest(
            session_id="workspace-preservation",
            task_id="task_1",
            instruction="Do work.",
            remaining_timeout_seconds=60,
            agent=AgentSpec(
                harness="codex",
                settings={"auth_mode": "subscription", "capture_mode": "transcript"},
            ),
        ),
        timer=StageTimer(),
        session_dir=session_dir,
        artifacts_dir=session_dir / "artifacts",
        session_root_identity=capture_session_root_identity(session_dir),
        log_authority_dir=log_dir,
        log_authority_identity=capture_session_root_identity(log_dir),
        credential_dir=credential_dir,
        credential_root_identity=capture_session_root_identity(credential_dir),
        credential_redactor=session_files.CredentialRedactor.from_auth_json(
            b'{"access_token":"access-canary"}'
        ),
    )

    GatewayNodeManager._redact_core_capture_authority(managed)

    assert workspace_file.read_bytes() == original
    assert secret not in authority_log.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_subscription_postrun_retries_runtime_absence_within_a_bound(
    tmp_path: Path,
) -> None:
    calls: list[str] = []
    manager = _postrun_manager(calls=calls)
    managed = _managed_postrun_session(tmp_path, _session_result())
    managed.request.agent = AgentSpec(
        harness="codex",
        settings={"auth_mode": "subscription", "capture_mode": "transcript"},
    )
    managed.runtime = FakeRuntime()
    attempts = 0

    async def stop_runtime(runtime, session_id, label):
        nonlocal attempts
        del runtime, session_id, label
        attempts += 1
        return attempts == 3

    manager._stop_runtime_best_effort = stop_runtime

    await manager._handle_postrun(managed)

    assert attempts == 3
    assert manager.session_registry.results[-1].status == SessionStatus.COMPLETED
    assert managed.session_id not in manager._cleanup_retries


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
    log_authority_dir = tmp_path / "core-logs"
    session_dir.mkdir()
    credential_dir.mkdir()
    log_authority_dir.mkdir()

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
    managed.log_authority_dir = log_authority_dir
    managed.log_authority_identity = capture_session_root_identity(log_authority_dir)

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
    assert "core-logs" in journal_text
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

    async def remove_logs(*args, **kwargs):
        del args, kwargs
        calls.append("logs")
        return True

    restarted._stop_recovered_container = stop_recovered
    restarted._remove_session_dir_best_effort = remove_session
    restarted._remove_credential_dir_best_effort = remove_credential
    restarted._remove_log_authority_best_effort = remove_logs

    restarted._load_cleanup_retries()
    await restarted._reconcile_cleanup_retries()

    assert calls == [
        "stop:sha256:journal-eval-container:openevo-journal-session-eval",
        "stop:sha256:journal-container:openevo-journal-session",
        "credential",
        "session",
        "logs",
    ]
    assert restarted._cleanup_retries == {}
    _assert_only_retired_cleanup_record(journal_dir)


def test_cleanup_journal_root_replacement_retains_displaced_records(
    tmp_path: Path,
) -> None:
    journal_dir = tmp_path / "journal"
    session_dir = tmp_path / "session"
    session_dir.mkdir(mode=0o700)
    managed = _managed_postrun_session(
        session_dir,
        _session_result(session_id="journal-root-replaced"),
    )
    managed.session_root_identity = capture_session_root_identity(session_dir)
    first = _postrun_manager(calls=[])
    first._cleanup_journal_dir = journal_dir
    first._register_cleanup_retry(managed)

    displaced = tmp_path / "journal-displaced"
    journal_dir.rename(displaced)
    journal_dir.mkdir(mode=0o700)

    restarted = _postrun_manager(calls=[])
    restarted._cleanup_journal_dir = journal_dir
    with pytest.raises(RuntimeError, match="journal root identity"):
        restarted._load_cleanup_retries()

    assert len(list(displaced.glob("*.json"))) == 1
    assert list(journal_dir.iterdir()) == []


def test_cleanup_journal_update_uses_held_root_when_path_is_replaced(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    journal_dir = tmp_path / "journal"
    session_dir = tmp_path / "session"
    session_dir.mkdir(mode=0o700)
    managed = _managed_postrun_session(
        session_dir,
        _session_result(session_id="journal-update-root-replaced"),
    )
    managed.session_root_identity = capture_session_root_identity(session_dir)
    manager = _postrun_manager(calls=[])
    manager._cleanup_journal_dir = journal_dir
    manager._register_cleanup_retry(managed)
    original_journal = next(journal_dir.glob("*.json")).read_bytes()
    managed.final_result = None
    displaced = tmp_path / "journal-displaced"
    original_open = node_module.os.open
    replaced = False

    def replace_root_before_pending_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal replaced
        if not replaced and os.fspath(path).endswith(".pending"):
            replaced = True
            journal_dir.rename(displaced)
            journal_dir.mkdir(mode=0o700)
        if dir_fd is None:
            return original_open(path, flags, mode)
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(node_module.os, "open", replace_root_before_pending_open)

    with pytest.raises(RuntimeError, match="journal root identity"):
        manager._persist_cleanup_ownership(manager._cleanup_retries[managed.session_id])

    assert replaced is True
    assert list(journal_dir.iterdir()) == []
    displaced_journals = list(displaced.glob("*.json"))
    assert len(displaced_journals) == 1
    assert displaced_journals[0].read_bytes() == original_journal
    assert len(list(displaced.glob("*.pending"))) == 1


def test_cleanup_journal_recovery_rejects_symlinked_ancestor(
    tmp_path: Path,
) -> None:
    authority_parent = tmp_path / "authority"
    journal_dir = authority_parent / "journal"
    session_dir = tmp_path / "session"
    authority_parent.mkdir(mode=0o700)
    session_dir.mkdir(mode=0o700)
    managed = _managed_postrun_session(
        session_dir,
        _session_result(session_id="journal-ancestor-symlink"),
    )
    managed.session_root_identity = capture_session_root_identity(session_dir)
    first = _postrun_manager(calls=[])
    first._cleanup_journal_dir = journal_dir
    first._register_cleanup_retry(managed)

    displaced = tmp_path / "authority-displaced"
    authority_parent.rename(displaced)
    authority_parent.symlink_to(displaced, target_is_directory=True)

    restarted = _postrun_manager(calls=[])
    restarted._cleanup_journal_dir = journal_dir
    with pytest.raises(RuntimeError, match="journal ancestor"):
        restarted._load_cleanup_retries()

    assert len(list((displaced / "journal").glob("*.json"))) == 1


@pytest.mark.parametrize(
    ("budget_name", "budget_value", "error"),
    [
        ("_CLEANUP_JOURNAL_MAX_ROWS", 0, "row budget"),
        ("_CLEANUP_JOURNAL_MAX_FILENAME_BYTES", 8, "filename budget"),
        ("_CLEANUP_JOURNAL_MAX_METADATA_BYTES", 1, "metadata budget"),
        ("_CLEANUP_JOURNAL_MAX_TOTAL_BYTES", 1, "aggregate byte budget"),
    ],
)
def test_cleanup_journal_recovery_preflights_budgets_before_record_reads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    budget_name: str,
    budget_value: int,
    error: str,
) -> None:
    journal_dir = tmp_path / "journal"
    session_dir = tmp_path / "session"
    session_dir.mkdir(mode=0o700)
    managed = _managed_postrun_session(
        session_dir,
        _session_result(session_id=f"journal-budget-{budget_name}"),
    )
    managed.session_root_identity = capture_session_root_identity(session_dir)
    first = _postrun_manager(calls=[])
    first._cleanup_journal_dir = journal_dir
    first._register_cleanup_retry(managed)

    restarted = _postrun_manager(calls=[])
    restarted._cleanup_journal_dir = journal_dir
    monkeypatch.setattr(node_module, budget_name, budget_value)

    def reject_record_read(*args, **kwargs):
        del args, kwargs
        raise AssertionError("journal content was read before recovery preflight")

    monkeypatch.setattr(restarted, "_read_cleanup_journal_record", reject_record_read)
    with pytest.raises(RuntimeError, match=error):
        restarted._load_cleanup_retries()


@pytest.mark.asyncio
async def test_restart_removes_journaled_credential_staging_crash_window(
    tmp_path: Path,
) -> None:
    journal_dir = tmp_path / "journal"
    session_dir = tmp_path / "session"
    credential_dir = tmp_path / "credentials"
    session_dir.mkdir(mode=0o700)
    credential_dir.mkdir(mode=0o700)
    abandoned_stage = credential_dir / ".openevo-credential-staging-crashed"
    abandoned_stage.mkdir(mode=0o700)
    secret = abandoned_stage / "auth.json"
    secret.write_text('{"access_token":"sigkill-canary"}\n', encoding="utf-8")
    secret.chmod(0o600)

    managed = _managed_postrun_session(
        session_dir,
        _session_result(session_id="stage-crash"),
    )
    managed.session_root_identity = capture_session_root_identity(session_dir)
    managed.credential_dir = credential_dir
    managed.credential_root_identity = capture_session_root_identity(credential_dir)
    first = GatewayNodeManager.__new__(GatewayNodeManager)
    first._cleanup_retries = {}
    first._cleanup_journal_dir = journal_dir
    first._register_cleanup_retry(managed)

    restarted = GatewayNodeManager.__new__(GatewayNodeManager)
    restarted._cleanup_retries = {}
    restarted._cleanup_journal_dir = journal_dir
    restarted._load_cleanup_retries()
    await restarted._reconcile_cleanup_retries()

    assert not credential_dir.exists()
    assert not session_dir.exists()
    _assert_only_retired_cleanup_record(journal_dir)


@pytest.mark.asyncio
async def test_credential_cleanup_fault_remains_journaled_and_retries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    journal_dir = tmp_path / "journal"
    session_dir = tmp_path / "session"
    credential_dir = tmp_path / "credentials"
    session_dir.mkdir(mode=0o700)
    credential_dir.mkdir(mode=0o700)
    auth = credential_dir / "auth.json"
    auth.write_text('{"access_token":"cleanup-canary"}\n', encoding="utf-8")
    auth.chmod(0o600)
    managed = _managed_postrun_session(
        session_dir,
        _session_result(session_id="credential-cleanup-fault"),
    )
    managed.session_root_identity = capture_session_root_identity(session_dir)
    managed.credential_dir = credential_dir
    managed.credential_root_identity = capture_session_root_identity(credential_dir)
    first = GatewayNodeManager.__new__(GatewayNodeManager)
    first._cleanup_retries = {}
    first._cleanup_journal_dir = journal_dir
    first._register_cleanup_retry(managed)

    restarted = GatewayNodeManager.__new__(GatewayNodeManager)
    restarted._cleanup_retries = {}
    restarted._cleanup_journal_dir = journal_dir
    restarted._load_cleanup_retries()
    original_remove = restarted._remove_credential_dir_best_effort
    attempts = 0

    async def fail_once(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return False
        return await original_remove(*args, **kwargs)

    monkeypatch.setattr(restarted, "_remove_credential_dir_best_effort", fail_once)

    await restarted._reconcile_cleanup_retries()
    assert credential_dir.exists()
    assert auth.exists()
    assert list(journal_dir.glob("*.json"))

    await restarted._reconcile_cleanup_retries()
    assert not credential_dir.exists()
    _assert_only_retired_cleanup_record(journal_dir)


@pytest.mark.asyncio
async def test_recovery_scrubs_journal_bound_auth_before_cleanup_budget_exhaustion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    journal_dir = tmp_path / "journal"
    session_dir = tmp_path / "session"
    credential_dir = tmp_path / "credentials"
    session_dir.mkdir(mode=0o700)
    credential_dir.mkdir(mode=0o700)
    auth = credential_dir / "auth.json"
    auth.write_text('{"access_token":"journal-budget-canary"}\n', encoding="utf-8")
    auth.chmod(0o600)
    auth_identity = session_files._auth_identity(auth.stat(follow_symlinks=False))
    for name in ("000-attacker", "001-attacker"):
        nested = credential_dir / name
        nested.mkdir()
        (nested / "entry").write_text("budget", encoding="utf-8")

    managed = _managed_postrun_session(
        session_dir,
        _session_result(session_id="credential-budget"),
    )
    managed.session_root_identity = capture_session_root_identity(session_dir)
    managed.credential_dir = credential_dir
    managed.credential_root_identity = capture_session_root_identity(credential_dir)
    managed.credential_mount = ManagedCredentialMount(
        root=credential_dir,
        root_identity=managed.credential_root_identity,
        auth_identity=auth_identity,
    )
    first = GatewayNodeManager.__new__(GatewayNodeManager)
    first._cleanup_retries = {}
    first._cleanup_journal_dir = journal_dir
    first._register_cleanup_retry(managed)

    journal_path = next(journal_dir.glob("*.json"))
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    assert journal["version"] == 9
    assert journal["kind"] == "active"
    assert journal["credential_root"]["auth_identity"] == list(auth_identity)

    real_remove = session_files.remove_credential_tree

    def exhaust_budget(credential_root, root_identity, expected_auth_identity):
        return real_remove(
            credential_root,
            root_identity,
            expected_auth_identity,
            max_nodes=1,
        )

    monkeypatch.setattr(node_module, "remove_credential_tree", exhaust_budget)
    restarted = GatewayNodeManager.__new__(GatewayNodeManager)
    restarted._cleanup_retries = {}
    restarted._cleanup_journal_dir = journal_dir
    restarted._load_cleanup_retries()

    await restarted._reconcile_cleanup_retries()

    assert auth.stat(follow_symlinks=False).st_size == 0
    assert credential_dir.exists()
    assert journal_path.exists()
    assert restarted._cleanup_retries[managed.session_id].credential_auth_identity == auth_identity


@pytest.mark.asyncio
async def test_recovery_scrubs_auth_published_after_prewrite_identity_journal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class SimulatedProcessCrash(BaseException):
        pass

    source = tmp_path / "home" / ".codex" / "auth.json"
    source.parent.mkdir(parents=True)
    source.write_text('{"access_token":"publication-crash-canary"}\n', encoding="utf-8")
    source.chmod(0o600)
    session_dir = tmp_path / "session"
    credential_dir = tmp_path / "credentials"
    session_dir.mkdir(mode=0o700)
    credential_dir.mkdir(mode=0o700)
    managed = ManagedSession(
        request=SessionDispatchRequest(
            session_id="credential-publication-crash",
            task_id="task-publication-crash",
            instruction="Do work.",
            remaining_timeout_seconds=60,
            agent=AgentSpec(
                harness="codex",
                settings={"auth_mode": "subscription", "capture_mode": "transcript"},
            ),
        ),
        timer=StageTimer(),
        session_dir=session_dir,
        artifacts_dir=session_dir / "artifacts",
        session_root_identity=capture_session_root_identity(session_dir),
        credential_dir=credential_dir,
        credential_root_identity=capture_session_root_identity(credential_dir),
    )
    journal_dir = tmp_path / "journal"
    manager = GatewayNodeManager.__new__(GatewayNodeManager)
    manager._cleanup_journal_dir = journal_dir
    manager._persist_cleanup_ownership(manager._cleanup_ownership_for(managed))

    def persist_identity(identity: session_files.CredentialFileIdentity) -> None:
        managed.credential_auth_identity = identity
        manager._persist_cleanup_ownership(manager._cleanup_ownership_for(managed))

    real_rename = session_files._rename_noreplace

    def publish_then_crash(*args, **kwargs) -> None:
        real_rename(*args, **kwargs)
        raise SimulatedProcessCrash

    monkeypatch.setattr(session_files, "_rename_noreplace", publish_then_crash)

    with pytest.raises(SimulatedProcessCrash):
        session_files.stage_codex_subscription_auth(
            source=source,
            session_dir=credential_dir,
            session_identity=managed.credential_root_identity,
            target_home_parts=(),
            on_identity=persist_identity,
        )

    published = credential_dir / "auth.json"
    journal_path = next(journal_dir.glob("*.json"))
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    journal_identity = tuple(journal["credential_root"]["auth_identity"])
    published_state = published.stat(follow_symlinks=False)
    assert journal_identity[:2] == (published_state.st_dev, published_state.st_ino)
    assert journal_identity[5] == 0
    held_auth = os.open(published, os.O_RDONLY | os.O_CLOEXEC)

    try:
        restarted = GatewayNodeManager.__new__(GatewayNodeManager)
        restarted._cleanup_retries = {}
        restarted._cleanup_journal_dir = journal_dir
        restarted._load_cleanup_retries()

        await restarted._reconcile_cleanup_retries()

        assert os.pread(held_auth, 1, 0) == b""
        assert not credential_dir.exists()
        assert not session_dir.exists()
        retired = _assert_only_retired_cleanup_record(journal_dir)
        assert retired["session_id"] == managed.session_id
    finally:
        os.close(held_auth)


@pytest.mark.asyncio
async def test_subscription_finalization_journal_recovers_terminal_result_after_restart(
    tmp_path: Path,
) -> None:
    journal_dir = tmp_path / "cleanup-journal"
    session_dir = tmp_path / "session"
    credential_dir = tmp_path / "credentials"
    log_authority_dir = tmp_path / "core-logs"
    for root in (session_dir, credential_dir, log_authority_dir):
        root.mkdir()
    (credential_dir / "auth.json").write_text(
        '{"access_token":"access-canary"}',
        encoding="utf-8",
    )
    (credential_dir / "auth.json").chmod(0o600)

    class JournalRuntime(FakeRuntime):
        @property
        def runtime_id(self) -> str:
            return "openevo-recovered-subscription"

        @property
        def container_id(self) -> str:
            return "sha256:recovered-subscription"

    original = _session_result(
        session_id="recovered-subscription",
        metadata={"scientific_result": "42", "leaked": "access-canary"},
    )
    managed = _managed_postrun_session(session_dir, original)
    managed.request.agent = AgentSpec(
        harness="codex",
        settings={"auth_mode": "subscription", "capture_mode": "transcript"},
    )
    managed.runtime = JournalRuntime()
    managed.session_root_identity = capture_session_root_identity(session_dir)
    managed.credential_dir = credential_dir
    managed.credential_root_identity = capture_session_root_identity(credential_dir)
    managed.credential_mount = ManagedCredentialMount(
        root=credential_dir,
        root_identity=managed.credential_root_identity,
        auth_identity=session_files._auth_identity(
            (credential_dir / "auth.json").stat(follow_symlinks=False)
        ),
    )
    managed.credential_redactor = session_files.CredentialRedactor.from_auth_json(
        b'{"access_token":"access-canary"}'
    )
    managed.log_authority_dir = log_authority_dir
    managed.log_authority_identity = capture_session_root_identity(log_authority_dir)

    first_calls: list[str] = []
    first = _postrun_manager(calls=first_calls)
    first._cleanup_journal_dir = journal_dir
    first._stop_runtime_best_effort = AsyncMock(return_value=False)

    await first._handle_postrun(managed)

    assert first.session_registry.results == []
    journal_text = next(journal_dir.glob("*.json")).read_text(encoding="utf-8")
    assert "scientific_result" in journal_text
    assert "access-canary" not in journal_text

    recovered_calls: list[str] = []
    restarted = _postrun_manager(calls=recovered_calls)
    restarted._cleanup_journal_dir = journal_dir
    restarted.session_registry = SessionRegistry()
    restarted.storage = RecordingStorage(recovered_calls)
    restarted._stop_recovered_container = AsyncMock(side_effect=[False, True])

    restarted._load_cleanup_retries()
    await restarted._reconcile_cleanup_retries()

    assert restarted.session_registry.get(original.session_id) is None
    assert "callback_push" not in recovered_calls
    assert "remove_session_dir" not in recovered_calls
    assert list(journal_dir.glob("*.json"))

    await restarted._reconcile_cleanup_retries()

    info = restarted.session_registry.get(original.session_id)
    assert info is not None
    assert info.status == SessionStatus.COMPLETED
    assert info.result is None  # callback delivery releases only the in-memory payload
    assert recovered_calls.index("callback_push") < recovered_calls.index("remove_session_dir")
    _assert_only_retired_cleanup_record(journal_dir)


@pytest.mark.asyncio
async def test_terminal_agent_result_is_durable_before_postprocess_and_recovers_after_crash(
    tmp_path: Path,
) -> None:
    journal_dir = tmp_path / "cleanup-journal"
    session_dir = tmp_path / "session"
    credential_dir = tmp_path / "credentials"
    log_authority_dir = tmp_path / "core-logs"
    for root in (session_dir, credential_dir, log_authority_dir):
        root.mkdir(mode=0o700)
    (session_dir / "artifacts").mkdir()
    auth = credential_dir / "auth.json"
    auth.write_text('{"access_token":"access-canary"}', encoding="utf-8")
    auth.chmod(0o600)

    class TerminalRuntime(FakeRuntime):
        @property
        def runtime_id(self) -> str:
            return "openevo-terminal-crash"

        @property
        def container_id(self) -> str:
            return "sha256:terminal-crash"

        async def exec(self, command, **kwargs):
            del command, kwargs
            return ExecResult(
                return_code=0,
                stdout=(
                    '{"type":"item.completed","item":{"type":"agent_message",'
                    '"text":"durable scientific answer"}}\n'
                ),
                stderr="",
            )

    request = SessionDispatchRequest(
        session_id="terminal-crash",
        task_id="task_1",
        instruction="Do work.",
        remaining_timeout_seconds=60,
        callback_url="http://rollout.test/callback",
        agent=AgentSpec(
            harness="codex",
            settings={"auth_mode": "subscription", "capture_mode": "transcript"},
        ),
    )
    managed = ManagedSession(
        request=request,
        timer=StageTimer(),
        session_dir=session_dir,
        artifacts_dir=session_dir / "artifacts",
        session_root_identity=capture_session_root_identity(session_dir),
        log_authority_dir=log_authority_dir,
        log_authority_identity=capture_session_root_identity(log_authority_dir),
        credential_dir=credential_dir,
        credential_root_identity=capture_session_root_identity(credential_dir),
        credential_mount=ManagedCredentialMount(
            root=credential_dir,
            root_identity=capture_session_root_identity(credential_dir),
            auth_identity=session_files._auth_identity(auth.stat(follow_symlinks=False)),
        ),
        credential_redactor=session_files.CredentialRedactor.from_auth_json(auth.read_bytes()),
        runtime=TerminalRuntime(),
    )
    first = _postrun_manager(calls=[])
    first._cleanup_journal_dir = journal_dir
    first._remaining_budget = lambda _managed: 30.0

    result = await first._run_exec_inputs(
        managed.runtime,
        [ExecInput(command="codex")],
        {},
        managed,
    )

    journal_path = next(journal_dir.glob("*.json"))
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    assert result.status == "completed"
    assert journal["phase"] == "terminal_finalization"
    assert journal["subscription_finalization"]["agent_result"]["status"] == "completed"

    delivered: list[SessionResult] = []
    restarted = _postrun_manager(calls=[])
    restarted._cleanup_journal_dir = journal_dir
    restarted.session_registry = SessionRegistry()
    restarted.storage = SessionStore()
    restarted.builders = default_builder_registry()
    restarted._stop_recovered_container = AsyncMock(return_value=True)

    async def capture_result(callback_url, recovered):
        del callback_url
        delivered.append(recovered)
        return False

    restarted._push_result = capture_result
    restarted._load_cleanup_retries()
    await restarted._reconcile_cleanup_retries()

    assert len(delivered) == 1
    assert "durable scientific answer" in delivered[0].model_dump_json()
    assert journal_path.exists()
    assert session_dir.exists()
    assert log_authority_dir.exists()


@pytest.mark.asyncio
async def test_subscription_finalization_rebuilds_transcript_after_restart(
    tmp_path: Path,
) -> None:
    journal_dir = tmp_path / "cleanup-journal"
    session_dir = tmp_path / "session"
    credential_dir = tmp_path / "credentials"
    log_authority_dir = tmp_path / "core-logs"
    for root in (session_dir, credential_dir, log_authority_dir):
        root.mkdir(mode=0o700)
    (session_dir / "artifacts").mkdir()
    auth = credential_dir / "auth.json"
    auth.write_text('{"access_token":"access-canary"}', encoding="utf-8")
    auth.chmod(0o600)
    log_identity = capture_session_root_identity(log_authority_dir)
    session_files.write_verified_session_log(
        log_authority_dir,
        log_identity,
        directory_parts=("logs", "agent"),
        leaf_name="step.00.stdout.log",
        content=(
            '{"type":"item.completed","item":{"type":"agent_message",'
            '"text":"scientific answer 42 access-canary"}}\n'
        ),
    )

    class JournalRuntime(FakeRuntime):
        @property
        def runtime_id(self) -> str:
            return "openevo-rebuild-subscription"

        @property
        def container_id(self) -> str:
            return "sha256:rebuild-subscription"

    request = SessionDispatchRequest(
        session_id="rebuild-subscription",
        task_id="task_1",
        instruction="Do work.",
        remaining_timeout_seconds=60,
        callback_url="http://rollout.test/callback",
        agent=AgentSpec(
            harness="codex",
            settings={"auth_mode": "subscription", "capture_mode": "transcript"},
        ),
    )
    managed = ManagedSession(
        request=request,
        timer=StageTimer(),
        session_dir=session_dir,
        artifacts_dir=session_dir / "artifacts",
        session_root_identity=capture_session_root_identity(session_dir),
        log_authority_dir=log_authority_dir,
        log_authority_identity=log_identity,
        credential_dir=credential_dir,
        credential_root_identity=capture_session_root_identity(credential_dir),
        credential_mount=ManagedCredentialMount(
            root=credential_dir,
            root_identity=capture_session_root_identity(credential_dir),
            auth_identity=session_files._auth_identity(auth.stat(follow_symlinks=False)),
        ),
        credential_redactor=session_files.CredentialRedactor.from_auth_json(auth.read_bytes()),
        runtime=JournalRuntime(),
        agent_result=AgentRunResult(
            status="completed",
            return_code=0,
            metadata={"last_step": 0, "log_dir": str(log_authority_dir / "logs" / "agent")},
        ),
    )
    first = _postrun_manager(calls=[])
    first._cleanup_journal_dir = journal_dir
    first._stop_runtime_best_effort = AsyncMock(return_value=False)

    await first._handle_postrun(managed)

    delivered: list[SessionResult] = []
    restarted = _postrun_manager(calls=[])
    restarted._cleanup_journal_dir = journal_dir
    restarted.session_registry = SessionRegistry()
    restarted.storage = SessionStore()
    restarted.builders = default_builder_registry()
    restarted._stop_recovered_container = AsyncMock(return_value=True)

    async def capture_result(callback_url, result):
        del callback_url
        delivered.append(result)
        return False

    restarted._push_result = capture_result
    restarted._load_cleanup_retries()
    await restarted._reconcile_cleanup_retries()

    assert len(delivered) == 1
    assert delivered[0].status == SessionStatus.COMPLETED
    serialized = delivered[0].model_dump_json()
    assert "scientific answer 42" in serialized
    assert "access-canary" not in serialized
    assert list(journal_dir.glob("*.json"))
    assert session_dir.exists()
    assert credential_dir.exists()

    restarted._push_result = AsyncMock(return_value=True)
    await restarted._reconcile_cleanup_retries()

    assert len(delivered) == 1
    _assert_only_retired_cleanup_record(journal_dir)


@pytest.mark.asyncio
async def test_handle_postrun_fail_open_export_error_retains_authority_and_callbacks(
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
        "callback_push",
        "clear_result_payload",
    ]
    assert "ses_1" in manager._cleanup_retries

    client.export_error = None
    await manager._reconcile_cleanup_retries()

    assert calls.count("export") == 2
    assert calls.count("callback_push") == 1
    assert calls.count("delete_session") == 1
    assert calls.count("remove_session_dir") == 1
    assert "ses_1" not in manager._cleanup_retries


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

    await manager._handle_postrun(_managed_postrun_session(tmp_path, _session_result()))

    assert calls == [
        "postrun_steps",
        "drain_eval",
        "set_result",
        "export",
    ]
    assert "ses_1" in manager._cleanup_retries


@pytest.mark.asyncio
async def test_terminal_delivery_restart_skips_export_after_durable_success(
    tmp_path: Path,
) -> None:
    calls: list[str] = []
    client = FakeEvolutionClient(calls=calls)
    journal_dir = tmp_path / "journal"
    session_dir = tmp_path / "session"
    credential_dir = tmp_path / "credentials"
    log_dir = tmp_path / "logs"
    for root in (session_dir, credential_dir, log_dir):
        root.mkdir(mode=0o700)
    managed = _managed_postrun_session(session_dir, _session_result())
    managed.request.agent = AgentSpec(
        harness="codex",
        settings={"auth_mode": "subscription", "capture_mode": "transcript"},
    )
    managed.session_root_identity = capture_session_root_identity(session_dir)
    managed.credential_dir = credential_dir
    managed.credential_root_identity = capture_session_root_identity(credential_dir)
    managed.log_authority_dir = log_dir
    managed.log_authority_identity = capture_session_root_identity(log_dir)

    first = _postrun_manager(
        calls=calls,
        evolution=EvolutionConfig(enabled=True),
        evolution_client=client,
    )
    first._cleanup_journal_dir = journal_dir
    first._push_result = AsyncMock(return_value=False)

    assert await first._deliver_terminal_result(managed, managed.final_result) is False
    assert calls.count("export") == 1
    assert first.storage.deleted == []
    journal_path = next(journal_dir.glob("*.json"))
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    assert journal["terminal_delivery"]["export_succeeded"] is True
    assert journal["terminal_delivery"]["callback_succeeded"] is False

    restarted = _postrun_manager(
        calls=calls,
        evolution=EvolutionConfig(enabled=True),
        evolution_client=client,
    )
    restarted._cleanup_journal_dir = journal_dir
    restarted._push_result = AsyncMock(side_effect=[False, True])
    restarted._load_cleanup_retries()

    await restarted._reconcile_cleanup_retries()
    assert calls.count("export") == 1
    assert journal_path.exists()
    assert restarted.storage.deleted == []

    await restarted._reconcile_cleanup_retries()
    assert calls.count("export") == 1
    retired = _assert_only_retired_cleanup_record(journal_dir)
    assert retired["terminal_delivery"]["export_succeeded"] is True
    assert retired["terminal_delivery"]["callback_succeeded"] is True
    assert restarted.storage.deleted == [managed.session_id]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault",
    ["replace", "directory_fsync", "final_directory_fsync"],
)
async def test_terminal_delivery_publish_failure_blocks_recovery_until_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    journal_dir = tmp_path / "journal"
    session_dir = tmp_path / "session"
    session_dir.mkdir(mode=0o700)
    result = _session_result(session_id="delivery-journal-fault")
    managed = _managed_postrun_session(session_dir, result)
    managed.final_result = None
    managed.session_root_identity = capture_session_root_identity(session_dir)
    manager = _postrun_manager(calls=[])
    manager._cleanup_journal_dir = journal_dir
    manager._register_cleanup_retry(managed)
    journal_path = next(journal_dir.glob("*.json"))
    original_journal = journal_path.read_bytes()

    if fault == "replace":
        original_replace = node_module.os.replace
        failed = False

        def fail_once(source, destination, **kwargs):
            nonlocal failed
            if not failed and os.fspath(destination) == journal_path.name:
                failed = True
                raise OSError("injected journal replace failure")
            return original_replace(source, destination, **kwargs)

        monkeypatch.setattr(node_module.os, "replace", fail_once)
    else:
        original_fsync = manager._fsync_cleanup_journal_directory
        calls = 0

        def fail_after_replace(path: Path) -> None:
            nonlocal calls
            calls += 1
            failure_call = 1 if fault == "directory_fsync" else 2
            if calls == failure_call:
                raise OSError("injected journal directory fsync failure")
            original_fsync(path)

        monkeypatch.setattr(manager, "_fsync_cleanup_journal_directory", fail_after_replace)

    with pytest.raises(OSError, match="injected journal"):
        await manager._deliver_terminal_result(managed, result)

    assert managed.final_result is None
    assert manager._cleanup_retries[managed.session_id].phase == "runtime_active"
    assert manager.storage.deleted == []
    assert journal_path.read_bytes() == original_journal
    assert list(journal_dir.glob("*.pending"))

    restarted = _postrun_manager(calls=[])
    restarted._cleanup_journal_dir = journal_dir
    with pytest.raises(RuntimeError, match="incomplete update"):
        restarted._load_cleanup_retries()
    assert restarted.storage.deleted == []
    assert session_dir.exists()

    if fault == "replace":
        monkeypatch.setattr(node_module.os, "replace", original_replace)
    else:
        monkeypatch.setattr(manager, "_fsync_cleanup_journal_directory", original_fsync)

    assert await manager._deliver_terminal_result(managed, result) is True
    assert managed.final_result is not None
    assert manager.storage.deleted == [managed.session_id]
    assert list(journal_dir.glob("*.pending")) == []


@pytest.mark.parametrize("fault", ["replace", "directory_fsync"])
def test_terminal_failure_publish_is_copy_on_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    journal_dir = tmp_path / "journal"
    session_dir = tmp_path / "session"
    credential_dir = tmp_path / "credentials"
    for root in (session_dir, credential_dir):
        root.mkdir(mode=0o700)
    managed = _managed_postrun_session(session_dir, _session_result(session_id="failure-cow"))
    managed.request.agent = AgentSpec(
        harness="codex",
        settings={"auth_mode": "subscription", "capture_mode": "transcript"},
    )
    managed.session_root_identity = capture_session_root_identity(session_dir)
    managed.credential_dir = credential_dir
    managed.credential_root_identity = capture_session_root_identity(credential_dir)
    manager = _postrun_manager(calls=[])
    manager._cleanup_journal_dir = journal_dir
    manager._register_cleanup_retry(managed)
    journal_path = next(journal_dir.glob("*.json"))
    original_journal = journal_path.read_bytes()
    original_ownership = manager._cleanup_retries[managed.session_id]

    if fault == "replace":
        original_replace = node_module.os.replace

        def fail_replace(source, destination, **kwargs):
            if os.fspath(destination) == journal_path.name:
                raise OSError("injected terminal failure replace")
            return original_replace(source, destination, **kwargs)

        monkeypatch.setattr(node_module.os, "replace", fail_replace)
    else:
        original_fsync = manager._fsync_cleanup_journal_directory
        calls = 0

        def fail_directory_fsync(path: Path) -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OSError("injected terminal failure directory fsync")
            original_fsync(path)

        monkeypatch.setattr(manager, "_fsync_cleanup_journal_directory", fail_directory_fsync)

    with pytest.raises(OSError, match="injected terminal failure"):
        manager._set_terminal_failure(
            managed,
            SessionStatus.ERROR,
            "terminal failure must remain prospective",
        )

    assert managed.pending_status is None
    assert managed.pending_error is None
    assert manager._cleanup_retries[managed.session_id] is original_ownership
    assert journal_path.read_bytes() == original_journal
    assert list(journal_dir.glob("*.pending"))


def test_cleanup_journal_lock_serializes_replace_fsync_rollback_transition(
    tmp_path: Path,
) -> None:
    journal_dir = tmp_path / "journal"
    session_dir = tmp_path / "session"
    session_dir.mkdir(mode=0o700)
    managed = _managed_postrun_session(
        session_dir,
        _session_result(session_id="journal-transition-barrier"),
    )
    managed.session_root_identity = capture_session_root_identity(session_dir)
    initial_manager = _postrun_manager(calls=[])
    initial_manager._cleanup_journal_dir = journal_dir
    initial_manager._register_cleanup_retry(managed)
    initial = initial_manager._cleanup_retries[managed.session_id]

    failing_manager = _postrun_manager(calls=[])
    failing_manager._cleanup_journal_dir = journal_dir
    successful_manager = _postrun_manager(calls=[])
    successful_manager._cleanup_journal_dir = journal_dir
    entered_failed_fsync = threading.Event()
    release_failed_fsync = threading.Event()
    original_fsync = failing_manager._fsync_cleanup_journal_directory
    injected = False

    def fail_after_replace(root_fd: int) -> None:
        nonlocal injected
        if not injected:
            injected = True
            entered_failed_fsync.set()
            assert release_failed_fsync.wait(timeout=2)
            raise OSError("injected transition fsync failure")
        original_fsync(root_fd)

    failing_manager._fsync_cleanup_journal_directory = fail_after_replace
    failures: list[BaseException] = []

    def run_failure() -> None:
        try:
            failing_manager._persist_cleanup_ownership(
                replace(initial, runtime_id="failed-transition")
            )
        except BaseException as exc:
            failures.append(exc)

    def run_success() -> None:
        try:
            successful_manager._persist_cleanup_ownership(
                replace(initial, runtime_id="committed-transition")
            )
        except BaseException as exc:
            failures.append(exc)

    failed_thread = threading.Thread(target=run_failure)
    successful_thread = threading.Thread(target=run_success)
    failed_thread.start()
    assert entered_failed_fsync.wait(timeout=2)
    successful_thread.start()
    time.sleep(0.05)
    assert successful_thread.is_alive()
    release_failed_fsync.set()
    failed_thread.join(timeout=2)
    successful_thread.join(timeout=2)

    assert not failed_thread.is_alive()
    assert not successful_thread.is_alive()
    assert len(failures) == 1
    assert isinstance(failures[0], OSError)
    payload = json.loads(next(journal_dir.glob("*.json")).read_text())
    assert payload["runtime"]["runtime_id"] == "committed-transition"
    assert list(journal_dir.glob("*.pending")) == []


def test_cleanup_journal_stale_writer_cannot_regress_terminal_delivery(
    tmp_path: Path,
) -> None:
    journal_dir = tmp_path / "journal"
    session_dir = tmp_path / "session"
    session_dir.mkdir(mode=0o700)
    result = _session_result(session_id="journal-stale-terminal-writer")
    managed = _managed_postrun_session(session_dir, result)
    managed.request.callback_url = "http://rollout.test/callback"
    managed.session_root_identity = capture_session_root_identity(session_dir)

    authoritative = _postrun_manager(calls=[])
    authoritative._cleanup_journal_dir = journal_dir
    authoritative._register_cleanup_retry(managed)
    runtime_active = authoritative._cleanup_retries[managed.session_id]

    stale_writer = _postrun_manager(calls=[])
    stale_writer._cleanup_journal_dir = journal_dir
    stale_entered = threading.Event()
    release_stale = threading.Event()
    original_acquire = stale_writer._acquire_cleanup_journal_lock

    def delay_stale_lock(authority):
        stale_entered.set()
        assert release_stale.wait(timeout=2)
        return original_acquire(authority)

    stale_writer._acquire_cleanup_journal_lock = delay_stale_lock
    stale_failures: list[BaseException] = []

    def persist_stale_runtime_active() -> None:
        try:
            stale_writer._persist_cleanup_ownership(
                replace(runtime_active, runtime_id="stale-runtime-active")
            )
        except BaseException as exc:
            stale_failures.append(exc)

    stale_thread = threading.Thread(target=persist_stale_runtime_active)
    stale_thread.start()
    assert stale_entered.wait(timeout=2)
    try:
        delivery = authoritative._prepare_terminal_delivery(managed, result)
        delivery = authoritative._advance_terminal_delivery(
            delivery,
            callback_succeeded=True,
        )
    finally:
        release_stale.set()
        stale_thread.join(timeout=2)

    assert not stale_thread.is_alive()
    assert len(stale_failures) == 1
    assert isinstance(stale_failures[0], RuntimeError)
    assert "revision compare-and-swap failed" in str(stale_failures[0])

    journal_path = next(journal_dir.glob("*.json"))
    payload = json.loads(journal_path.read_text(encoding="utf-8"))
    assert payload["version"] == 9
    assert payload["kind"] == "active"
    assert payload["revision"] == delivery.revision
    assert payload["phase"] == "terminal_delivery"
    assert payload["terminal_delivery"]["callback_succeeded"] is True
    assert payload["runtime"]["runtime_id"] != "stale-runtime-active"

    with pytest.raises(RuntimeError, match="phase cannot regress"):
        authoritative._persist_cleanup_ownership(
            replace(runtime_active, revision=delivery.revision)
        )

    assert delivery.delivery_state is not None
    with pytest.raises(RuntimeError, match="callback proof cannot regress"):
        authoritative._persist_cleanup_ownership(
            replace(
                delivery,
                delivery_state=replace(
                    delivery.delivery_state,
                    callback_succeeded=False,
                ),
            )
        )

    persisted = json.loads(journal_path.read_text(encoding="utf-8"))
    assert persisted["phase"] == "terminal_delivery"
    assert persisted["terminal_delivery"]["callback_succeeded"] is True


def test_cleanup_journal_retirement_blocks_precreation_stale_writer(
    tmp_path: Path,
) -> None:
    journal_dir = tmp_path / "journal"
    session_dir = tmp_path / "session"
    session_dir.mkdir(mode=0o700)
    result = _session_result(session_id="journal-retirement-aba")
    managed = _managed_postrun_session(session_dir, result)
    managed.request.callback_url = "http://rollout.test/callback"
    managed.session_root_identity = capture_session_root_identity(session_dir)

    authoritative = _postrun_manager(calls=[])
    authoritative._cleanup_journal_dir = journal_dir
    stale_writer = _postrun_manager(calls=[])
    stale_writer._cleanup_journal_dir = journal_dir
    stale_candidate = stale_writer._cleanup_ownership_for(managed)
    stale_entered = threading.Event()
    release_stale = threading.Event()
    original_acquire = stale_writer._acquire_cleanup_journal_lock

    def delay_stale_lock(authority):
        stale_entered.set()
        assert release_stale.wait(timeout=2)
        return original_acquire(authority)

    stale_writer._acquire_cleanup_journal_lock = delay_stale_lock
    stale_failures: list[BaseException] = []

    def persist_stale_precreation_writer() -> None:
        try:
            stale_writer._persist_cleanup_ownership(stale_candidate)
        except BaseException as exc:
            stale_failures.append(exc)

    stale_thread = threading.Thread(target=persist_stale_precreation_writer)
    stale_thread.start()
    assert stale_entered.wait(timeout=2)
    try:
        authoritative._register_cleanup_retry(managed)
        delivery = authoritative._prepare_terminal_delivery(managed, result)
        delivery = authoritative._advance_terminal_delivery(
            delivery,
            callback_succeeded=True,
        )
        authoritative._retire_cleanup_ownership(delivery)
    finally:
        release_stale.set()
        stale_thread.join(timeout=2)

    assert not stale_thread.is_alive()
    assert len(stale_failures) == 1
    assert isinstance(stale_failures[0], RuntimeError)
    assert "retired" in str(stale_failures[0])

    journal_path = next(journal_dir.glob("*.json"))
    tombstone = json.loads(journal_path.read_text(encoding="utf-8"))
    assert tombstone["version"] == 9
    assert tombstone["kind"] == "retired"
    assert tombstone["generation"] == delivery.generation
    assert tombstone["revision"] == delivery.revision + 1
    assert tombstone["terminal_delivery"] == {
        "result_digest": delivery.delivery_state.result_digest,
        "export_succeeded": True,
        "callback_succeeded": True,
    }

    restarted = _postrun_manager(calls=[])
    restarted._cleanup_journal_dir = journal_dir
    restarted._load_cleanup_retries()
    assert restarted._cleanup_retries == {}


def test_cleanup_journal_compacts_more_than_row_budget_lifecycles_and_restarts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    journal_dir = tmp_path / "journal"
    session_dir = tmp_path / "session"
    session_dir.mkdir(mode=0o700)
    monkeypatch.setattr(node_module.os, "fsync", lambda descriptor: None)
    monkeypatch.setattr(node_module, "_CLEANUP_JOURNAL_COMPACT_AT_ROWS", 64)

    manager = _postrun_manager(calls=[])
    manager._cleanup_journal_dir = journal_dir
    lifecycle_count = node_module._CLEANUP_JOURNAL_MAX_ROWS + 17
    for index in range(lifecycle_count):
        _retire_minimal_cleanup_session(manager, session_dir, f"bounded-life-{index}")

    records = list(journal_dir.glob("*.json"))
    assert len(records) < node_module._CLEANUP_JOURNAL_MAX_ROWS
    epoch = json.loads(
        (journal_dir / node_module._CLEANUP_JOURNAL_EPOCH_NAME).read_text(encoding="utf-8")
    )
    assert epoch["epoch"] >= 1

    restarted = _postrun_manager(calls=[])
    restarted._cleanup_journal_dir = journal_dir
    restarted._load_cleanup_retries()
    assert restarted._cleanup_retries == {}
    assert list(journal_dir.glob("*.json")) == []

    _retire_minimal_cleanup_session(restarted, session_dir, "bounded-after-restart")
    final_restart = _postrun_manager(calls=[])
    final_restart._cleanup_journal_dir = journal_dir
    final_restart._load_cleanup_retries()
    assert final_restart._cleanup_retries == {}
    final_epoch = json.loads(
        (journal_dir / node_module._CLEANUP_JOURNAL_EPOCH_NAME).read_text(encoding="utf-8")
    )
    assert final_epoch["retired_count"] == lifecycle_count + 1
    assert final_epoch["retirement_digest"] != (
        node_module._CLEANUP_JOURNAL_RETIREMENT_DIGEST_SEED
    )


def test_cleanup_journal_startup_compacts_legacy_v8_row_budget_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    journal_dir = tmp_path / "journal"
    monkeypatch.setattr(node_module.os, "fsync", lambda descriptor: None)
    manager = _postrun_manager(calls=[])
    manager._cleanup_journal_dir = journal_dir
    manager._capture_cleanup_journal_creation_epoch()

    for index in range(node_module._CLEANUP_JOURNAL_MAX_ROWS):
        session_id = f"legacy-v8-retired-{index}"
        path = journal_dir / manager._cleanup_journal_name(session_id)
        path.write_text(
            json.dumps(
                {
                    "version": 8,
                    "kind": "retired",
                    "session_id": session_id,
                    "generation": f"{index:032x}",
                    "revision": 2,
                    "terminal_delivery": None,
                },
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        path.chmod(0o600)

    restarted = _postrun_manager(calls=[])
    restarted._cleanup_journal_dir = journal_dir
    restarted._load_cleanup_retries()

    assert restarted._cleanup_retries == {}
    assert list(journal_dir.glob("*.json")) == []
    epoch = json.loads(
        (journal_dir / node_module._CLEANUP_JOURNAL_EPOCH_NAME).read_text(encoding="utf-8")
    )
    assert epoch["epoch"] == 1
    assert epoch["retired_count"] == node_module._CLEANUP_JOURNAL_MAX_ROWS


def test_cleanup_journal_legacy_root_seals_epoch_before_v8_compaction(
    tmp_path: Path,
) -> None:
    journal_dir = tmp_path / "journal"
    session_dir = tmp_path / "session"
    session_dir.mkdir(mode=0o700)
    manager = _postrun_manager(calls=[])
    manager._cleanup_journal_dir = journal_dir
    _retire_minimal_cleanup_session(manager, session_dir, "legacy-root-retired")

    record_path = next(journal_dir.glob("*.json"))
    record = json.loads(record_path.read_text(encoding="utf-8"))
    record["version"] = 8
    for key in (
        "epoch",
        "epoch_token",
        "retired_epoch",
        "retired_epoch_token",
    ):
        record.pop(key)
    record_path.write_text(json.dumps(record), encoding="utf-8")
    record_path.chmod(0o600)

    marker_path = next(journal_dir.parent.glob(".*.root.json"))
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    marker["version"] = 1
    marker.pop("epoch_required")
    marker_path.write_text(json.dumps(marker), encoding="utf-8")
    marker_path.chmod(0o600)
    (journal_dir / node_module._CLEANUP_JOURNAL_EPOCH_NAME).unlink()

    restarted = _postrun_manager(calls=[])
    restarted._cleanup_journal_dir = journal_dir
    restarted._load_cleanup_retries()

    assert restarted._cleanup_retries == {}
    sealed_marker = json.loads(marker_path.read_text(encoding="utf-8"))
    assert sealed_marker["version"] == 2
    assert sealed_marker["epoch_required"] is True
    epoch = json.loads(
        (journal_dir / node_module._CLEANUP_JOURNAL_EPOCH_NAME).read_text(encoding="utf-8")
    )
    assert epoch["epoch"] == 1
    assert epoch["retired_count"] == 1


def test_cleanup_journal_sealed_root_rejects_epoch_reset(
    tmp_path: Path,
) -> None:
    journal_dir = tmp_path / "journal"
    session_dir = tmp_path / "session"
    session_dir.mkdir(mode=0o700)
    manager = _postrun_manager(calls=[])
    manager._cleanup_journal_dir = journal_dir
    managed = _managed_postrun_session(
        session_dir,
        _session_result(session_id="epoch-reset-active"),
    )
    managed.session_root_identity = capture_session_root_identity(session_dir)
    manager._persist_cleanup_ownership(manager._cleanup_ownership_for(managed))
    active_path = manager._cleanup_journal_path(managed.session_id)
    (journal_dir / node_module._CLEANUP_JOURNAL_EPOCH_NAME).unlink()

    restarted = _postrun_manager(calls=[])
    restarted._cleanup_journal_dir = journal_dir
    with pytest.raises(RuntimeError, match="epoch authority is missing"):
        restarted._load_cleanup_retries()

    assert active_path.exists()


def test_cleanup_journal_compaction_epoch_permanently_rejects_old_writer(
    tmp_path: Path,
) -> None:
    journal_dir = tmp_path / "journal"
    session_dir = tmp_path / "session"
    session_dir.mkdir(mode=0o700)
    managed = _managed_postrun_session(
        session_dir,
        _session_result(session_id="compacted-stale-writer"),
    )
    managed.session_root_identity = capture_session_root_identity(session_dir)

    stale_writer = _postrun_manager(calls=[])
    stale_writer._cleanup_journal_dir = journal_dir
    stale_candidate = stale_writer._cleanup_ownership_for(managed)

    authoritative = _postrun_manager(calls=[])
    authoritative._cleanup_journal_dir = journal_dir
    active = authoritative._persist_cleanup_ownership(
        authoritative._cleanup_ownership_for(managed)
    )
    retired = authoritative._retire_cleanup_ownership(active)
    authoritative._compact_cleanup_journal()

    assert retired.generation == active.generation
    assert list(journal_dir.glob("*.json")) == []
    compacted_epoch = json.loads(
        (journal_dir / node_module._CLEANUP_JOURNAL_EPOCH_NAME).read_text(encoding="utf-8")
    )
    assert compacted_epoch["retired_count"] == 1
    with pytest.raises(RuntimeError, match="cleanup journal creation epoch is stale"):
        stale_writer._persist_cleanup_ownership(stale_candidate)

    restarted = _postrun_manager(calls=[])
    restarted._cleanup_journal_dir = journal_dir
    restarted._load_cleanup_retries()
    with pytest.raises(RuntimeError, match="cleanup journal creation epoch is stale"):
        stale_writer._persist_cleanup_ownership(stale_candidate)


def test_cleanup_journal_compaction_rejects_concurrent_precreation_stale_writer(
    tmp_path: Path,
) -> None:
    journal_dir = tmp_path / "journal"
    session_dir = tmp_path / "session"
    session_dir.mkdir(mode=0o700)
    managed = _managed_postrun_session(
        session_dir,
        _session_result(session_id="concurrent-compaction-stale"),
    )
    managed.session_root_identity = capture_session_root_identity(session_dir)

    stale_writer = _postrun_manager(calls=[])
    stale_writer._cleanup_journal_dir = journal_dir
    stale_candidate = stale_writer._cleanup_ownership_for(managed)
    entered = threading.Event()
    release = threading.Event()
    original_acquire = stale_writer._acquire_cleanup_journal_lock

    def delay_stale_lock(authority):
        entered.set()
        assert release.wait(timeout=2)
        return original_acquire(authority)

    stale_writer._acquire_cleanup_journal_lock = delay_stale_lock
    failures: list[BaseException] = []

    def persist_stale() -> None:
        try:
            stale_writer._persist_cleanup_ownership(stale_candidate)
        except BaseException as exc:
            failures.append(exc)

    thread = threading.Thread(target=persist_stale)
    thread.start()
    assert entered.wait(timeout=2)
    try:
        authoritative = _postrun_manager(calls=[])
        authoritative._cleanup_journal_dir = journal_dir
        active = authoritative._persist_cleanup_ownership(
            authoritative._cleanup_ownership_for(managed)
        )
        authoritative._retire_cleanup_ownership(active)
        authoritative._compact_cleanup_journal()
    finally:
        release.set()
        thread.join(timeout=2)

    assert not thread.is_alive()
    assert len(failures) == 1
    assert isinstance(failures[0], RuntimeError)
    assert "creation epoch is stale" in str(failures[0])
    assert list(journal_dir.glob("*.json")) == []


def test_cleanup_journal_late_retirement_is_summarized_in_current_epoch(
    tmp_path: Path,
) -> None:
    journal_dir = tmp_path / "journal"
    session_dir = tmp_path / "session"
    session_dir.mkdir(mode=0o700)
    manager = _postrun_manager(calls=[])
    manager._cleanup_journal_dir = journal_dir

    old_managed = _managed_postrun_session(
        session_dir,
        _session_result(session_id="active-across-epoch"),
    )
    old_managed.session_root_identity = capture_session_root_identity(session_dir)
    old_active = manager._persist_cleanup_ownership(manager._cleanup_ownership_for(old_managed))
    _retire_minimal_cleanup_session(manager, session_dir, "epoch-advancer")
    manager._compact_cleanup_journal()

    assert manager._cleanup_journal_path(old_managed.session_id).exists()
    manager._retire_cleanup_ownership(old_active)
    late_tombstone = json.loads(
        manager._cleanup_journal_path(old_managed.session_id).read_text(encoding="utf-8")
    )
    assert late_tombstone["epoch"] == 0
    assert late_tombstone["retired_epoch"] == 1

    manager._compact_cleanup_journal()
    epoch = json.loads(
        (journal_dir / node_module._CLEANUP_JOURNAL_EPOCH_NAME).read_text(encoding="utf-8")
    )
    assert epoch["epoch"] == 2
    assert epoch["retired_count"] == 2
    assert list(journal_dir.glob("*.json")) == []


def test_cleanup_journal_write_backpressure_preserves_active_records(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    journal_dir = tmp_path / "journal"
    session_dir = tmp_path / "session"
    session_dir.mkdir(mode=0o700)
    monkeypatch.setattr(node_module, "_CLEANUP_JOURNAL_MAX_ROWS", 8)
    monkeypatch.setattr(node_module, "_CLEANUP_JOURNAL_COMPACT_AT_ROWS", 6)
    monkeypatch.setattr(node_module.os, "fsync", lambda descriptor: None)

    manager = _postrun_manager(calls=[])
    manager._cleanup_journal_dir = journal_dir
    for index in range(8):
        managed = _managed_postrun_session(
            session_dir,
            _session_result(session_id=f"active-capacity-{index}"),
        )
        managed.session_root_identity = capture_session_root_identity(session_dir)
        manager._persist_cleanup_ownership(manager._cleanup_ownership_for(managed))

    blocked = _managed_postrun_session(
        session_dir,
        _session_result(session_id="active-capacity-blocked"),
    )
    blocked.session_root_identity = capture_session_root_identity(session_dir)
    with pytest.raises(RuntimeError, match="capacity is occupied by active records"):
        manager._persist_cleanup_ownership(manager._cleanup_ownership_for(blocked))

    restarted = _postrun_manager(calls=[])
    restarted._cleanup_journal_dir = journal_dir
    restarted._load_cleanup_retries()
    assert len(restarted._cleanup_retries) == 8
    assert blocked.session_id not in restarted._cleanup_retries


@pytest.mark.parametrize("version", range(1, 8))
def test_cleanup_journal_compaction_fails_closed_before_legacy_record_deletion(
    tmp_path: Path,
    version: int,
) -> None:
    journal_dir = tmp_path / "journal"
    session_dir = tmp_path / "session"
    session_dir.mkdir(mode=0o700)
    manager = _postrun_manager(calls=[])
    manager._cleanup_journal_dir = journal_dir
    _retire_minimal_cleanup_session(manager, session_dir, f"retired-before-v{version}")
    epoch_path = journal_dir / node_module._CLEANUP_JOURNAL_EPOCH_NAME
    epoch_before = epoch_path.read_bytes()

    legacy_session_id = f"malformed-legacy-v{version}"
    legacy_path = journal_dir / manager._cleanup_journal_name(legacy_session_id)
    legacy_path.write_text(
        json.dumps({"version": version, "session_id": legacy_session_id}),
        encoding="utf-8",
    )
    legacy_path.chmod(0o600)

    restarted = _postrun_manager(calls=[])
    restarted._cleanup_journal_dir = journal_dir
    with pytest.raises(RuntimeError, match="cleanup ownership journal is invalid"):
        restarted._load_cleanup_retries()

    assert epoch_path.read_bytes() == epoch_before
    assert len(list(journal_dir.glob("*.json"))) == 2


class _SimulatedCleanupCompactionCrash(BaseException):
    pass


@pytest.mark.parametrize(
    "checkpoint",
    [
        "epoch_candidate_fsynced",
        "epoch_replaced",
        "epoch_directory_fsynced",
        "tombstone_unlinked",
        "tombstones_directory_fsynced",
    ],
)
def test_cleanup_journal_compaction_recovers_every_crash_boundary(
    tmp_path: Path,
    checkpoint: str,
) -> None:
    journal_dir = tmp_path / "journal"
    session_dir = tmp_path / "session"
    session_dir.mkdir(mode=0o700)
    manager = _postrun_manager(calls=[])
    manager._cleanup_journal_dir = journal_dir

    stale_managed = _managed_postrun_session(
        session_dir,
        _session_result(session_id=f"stale-at-{checkpoint}"),
    )
    stale_managed.session_root_identity = capture_session_root_identity(session_dir)
    stale_candidate = manager._cleanup_ownership_for(stale_managed)
    stale_active = manager._persist_cleanup_ownership(stale_candidate)
    manager._retire_cleanup_ownership(stale_active)
    _retire_minimal_cleanup_session(manager, session_dir, f"retired-1-at-{checkpoint}")
    _retire_minimal_cleanup_session(manager, session_dir, f"retired-2-at-{checkpoint}")

    active_managed = _managed_postrun_session(
        session_dir,
        _session_result(session_id=f"active-at-{checkpoint}"),
    )
    active_managed.session_root_identity = capture_session_root_identity(session_dir)
    active = manager._persist_cleanup_ownership(manager._cleanup_ownership_for(active_managed))

    def crash_at_boundary(label: str) -> None:
        if label == checkpoint:
            raise _SimulatedCleanupCompactionCrash(label)

    manager._cleanup_journal_compaction_checkpoint = crash_at_boundary
    with pytest.raises(_SimulatedCleanupCompactionCrash):
        manager._compact_cleanup_journal()

    restarted = _postrun_manager(calls=[])
    restarted._cleanup_journal_dir = journal_dir
    restarted._load_cleanup_retries()
    assert set(restarted._cleanup_retries) == {active.session_id}
    recovered = restarted._cleanup_retries[active.session_id]
    assert recovered.generation == active.generation
    assert recovered.revision == active.revision
    assert len(list(journal_dir.glob("*.json"))) == 1
    recovered_epoch = json.loads(
        (journal_dir / node_module._CLEANUP_JOURNAL_EPOCH_NAME).read_text(encoding="utf-8")
    )
    assert recovered_epoch["retired_count"] == 3

    with pytest.raises(RuntimeError, match="cleanup journal creation epoch is stale"):
        manager._persist_cleanup_ownership(stale_candidate)


def test_cleanup_journal_v7_recovery_retires_with_generation_and_rejects_legacy_writer(
    tmp_path: Path,
) -> None:
    journal_dir = tmp_path / "journal"
    session_dir = tmp_path / "session"
    session_dir.mkdir(mode=0o700)
    managed = _managed_postrun_session(
        session_dir,
        _session_result(session_id="journal-v7-retirement"),
    )
    managed.session_root_identity = capture_session_root_identity(session_dir)

    first = _postrun_manager(calls=[])
    first._cleanup_journal_dir = journal_dir
    first._register_cleanup_retry(managed)
    journal_path = next(journal_dir.glob("*.json"))
    legacy_payload = json.loads(journal_path.read_text(encoding="utf-8"))
    legacy_payload["version"] = 7
    legacy_payload.pop("kind")
    legacy_payload.pop("generation")
    legacy_payload.pop("epoch")
    legacy_payload.pop("epoch_token")
    journal_path.write_text(json.dumps(legacy_payload), encoding="utf-8")
    journal_path.chmod(0o600)

    restarted = _postrun_manager(calls=[])
    restarted._cleanup_journal_dir = journal_dir
    restarted._load_cleanup_retries()
    legacy = restarted._cleanup_retries[managed.session_id]
    assert legacy.generation is None

    restarted._retire_cleanup_ownership(legacy)
    tombstone = json.loads(journal_path.read_text(encoding="utf-8"))
    assert tombstone["version"] == 9
    assert tombstone["kind"] == "retired"
    assert tombstone["generation"]

    with pytest.raises(RuntimeError, match="retired"):
        restarted._persist_cleanup_ownership(legacy)


def test_cleanup_journal_process_lock_is_bounded_and_released_after_holder_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    journal_dir = tmp_path / "journal"
    session_dir = tmp_path / "session"
    session_dir.mkdir(mode=0o700)
    managed = _managed_postrun_session(
        session_dir,
        _session_result(session_id="journal-process-lock"),
    )
    managed.session_root_identity = capture_session_root_identity(session_dir)
    manager = _postrun_manager(calls=[])
    manager._cleanup_journal_dir = journal_dir
    manager._register_cleanup_retry(managed)
    ownership = manager._cleanup_retries[managed.session_id]

    context = multiprocessing.get_context("fork")
    acquired = context.Event()
    release = context.Event()

    def hold_process_lock() -> None:
        holder = GatewayNodeManager.__new__(GatewayNodeManager)
        holder._cleanup_journal_dir = journal_dir
        authority = holder._open_cleanup_journal_authority(initialize=False)
        assert authority is not None
        descriptor = holder._acquire_cleanup_journal_lock(authority)
        acquired.set()
        release.wait(timeout=5)
        holder._release_cleanup_journal_lock(descriptor)
        authority.close()

    process = context.Process(target=hold_process_lock)
    process.start()
    assert acquired.wait(timeout=2)
    monkeypatch.setattr(node_module, "_CLEANUP_JOURNAL_LOCK_TIMEOUT_SECONDS", 0.05)

    contender = _postrun_manager(calls=[])
    contender._cleanup_journal_dir = journal_dir
    with pytest.raises(RuntimeError, match="process lock timed out"):
        contender._persist_cleanup_ownership(replace(ownership, runtime_id="contender"))

    release.set()
    process.join(timeout=2)
    assert process.exitcode == 0

    committed = contender._persist_cleanup_ownership(
        replace(ownership, runtime_id="after-holder-exit")
    )
    assert committed.runtime_id == "after-holder-exit"


@pytest.mark.asyncio
async def test_credential_capable_exception_logs_never_emit_traceback_canary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "credential-log-traceback-canary"
    session_dir = tmp_path / "session"
    credential_dir = tmp_path / "credentials"
    for root in (session_dir, credential_dir):
        root.mkdir(mode=0o700)
    auth = credential_dir / "auth.json"
    auth.write_text(f'{{"access_token":"{secret}"}}', encoding="utf-8")
    auth.chmod(0o600)
    managed = _managed_postrun_session(session_dir, _session_result(session_id="safe-logs"))
    managed.request.agent = AgentSpec(
        harness="codex",
        settings={"auth_mode": "subscription", "capture_mode": "transcript"},
    )
    managed.credential_dir = credential_dir
    managed.credential_root_identity = capture_session_root_identity(credential_dir)
    managed.credential_redactor = session_files.CredentialRedactor.from_auth_json(
        auth.read_bytes()
    )
    manager = _postrun_manager(
        calls=[],
        evolution=EvolutionConfig(enabled=True),
        evolution_client=FakeEvolutionClient(export_error=RuntimeError(secret)),
    )

    class CanaryFailure(RuntimeError):
        pass

    class FailingRuntime(FakeRuntime):
        async def stop(self) -> None:
            traceback_local = secret
            raise CanaryFailure(f"stop failed {traceback_local}") from ValueError(secret)

    async def fail_cleanup(*args, **kwargs):
        del args, kwargs
        traceback_local = secret
        raise CanaryFailure(f"cleanup failed {traceback_local}") from ValueError(secret)

    def fail_credential_cleanup(*args, **kwargs):
        del args, kwargs
        traceback_local = secret
        raise CanaryFailure(f"credential cleanup failed {traceback_local}") from ValueError(secret)

    manager._cleanup_retries[managed.session_id] = manager._cleanup_ownership_for(managed)
    monkeypatch.setattr(manager, "_remove_session_dir_best_effort", fail_cleanup)
    monkeypatch.setattr(node_module, "remove_credential_tree", fail_credential_cleanup)
    with caplog.at_level("WARNING", logger="openevo.gateway.node"):
        assert not await manager._stop_runtime_best_effort(
            FailingRuntime(), managed.session_id, "runtime"
        )
        await manager._export_evolution_event(_session_result(session_id=managed.session_id))
        assert not await GatewayNodeManager._remove_credential_dir_best_effort(
            manager,
            credential_dir,
            managed.session_id,
            managed.credential_root_identity,
            session_files._auth_identity(auth.stat(follow_symlinks=False)),
        )
        await manager._reconcile_cleanup_retries()

    assert secret not in caplog.text
    assert "Traceback" not in caplog.text
    assert "CanaryFailure" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("recovered_evolution", "recovered_client"),
    [
        (None, None),
        (EvolutionConfig(enabled=False), None),
        (
            EvolutionConfig(enabled=True, backend_url="http://127.0.0.1:8300"),
            FakeEvolutionClient(base_url="http://127.0.0.1:8300"),
        ),
    ],
)
async def test_required_export_config_drift_remains_pending_after_restart(
    tmp_path: Path,
    recovered_evolution: EvolutionConfig | None,
    recovered_client: FakeEvolutionClient | None,
) -> None:
    journal_dir = tmp_path / "journal"
    session_dir = tmp_path / "session"
    session_dir.mkdir(mode=0o700)
    calls: list[str] = []
    original_config = EvolutionConfig(
        enabled=True,
        backend_url="http://127.0.0.1:8200",
        event_export={"enabled": True, "fail_open": True},
    )
    original_client = FakeEvolutionClient(
        export_error=RuntimeError("backend unavailable"),
        calls=calls,
        base_url=original_config.backend_url,
    )
    managed = _managed_postrun_session(session_dir, _session_result())
    managed.session_root_identity = capture_session_root_identity(session_dir)
    first = _postrun_manager(
        calls=calls,
        evolution=original_config,
        evolution_client=original_client,
    )
    first._cleanup_journal_dir = journal_dir

    assert await first._deliver_terminal_result(managed, managed.final_result) is False
    journal_path = next(journal_dir.glob("*.json"))
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    assert journal["terminal_delivery"]["export_required"] is True
    assert journal["terminal_delivery"]["export_authority"]["backend_url"] == (
        original_config.backend_url
    )

    recovered_calls: list[str] = []
    restarted = _postrun_manager(
        calls=recovered_calls,
        evolution=recovered_evolution,
        evolution_client=recovered_client,
    )
    restarted._cleanup_journal_dir = journal_dir
    restarted._load_cleanup_retries()
    await restarted._reconcile_cleanup_retries()

    assert journal_path.exists()
    assert session_dir.exists()
    assert "delete_session" not in recovered_calls
    assert "remove_session_dir" not in recovered_calls
    if recovered_client is not None:
        assert recovered_client.exported_events == []


def test_current_cleanup_journal_requires_explicit_recovery_phase(tmp_path: Path) -> None:
    journal_dir = tmp_path / "journal"
    session_dir = tmp_path / "session"
    session_dir.mkdir(mode=0o700)
    managed = _managed_postrun_session(session_dir, _session_result())
    managed.session_root_identity = capture_session_root_identity(session_dir)
    first = _postrun_manager(calls=[])
    first._cleanup_journal_dir = journal_dir
    first._register_cleanup_retry(managed)

    journal_path = next(journal_dir.glob("*.json"))
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    assert journal["phase"] == "runtime_active"
    journal.pop("phase")
    journal_path.write_text(json.dumps(journal), encoding="utf-8")
    journal_path.chmod(0o600)

    restarted = _postrun_manager(calls=[])
    restarted._cleanup_journal_dir = journal_dir
    with pytest.raises(RuntimeError, match="cleanup ownership journal is invalid"):
        restarted._load_cleanup_retries()

    assert journal_path.exists()
    assert session_dir.exists()


def test_v5_cleanup_journal_remains_readable_after_v9_upgrade(tmp_path: Path) -> None:
    journal_dir = tmp_path / "journal"
    session_dir = tmp_path / "session"
    session_dir.mkdir(mode=0o700)
    managed = _managed_postrun_session(session_dir, _session_result(session_id="v5-recovery"))
    managed.session_root_identity = capture_session_root_identity(session_dir)
    first = _postrun_manager(calls=[])
    first._cleanup_journal_dir = journal_dir
    first._register_cleanup_retry(managed)

    journal_path = next(journal_dir.glob("*.json"))
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    journal["version"] = 5
    journal.pop("revision")
    journal.pop("kind")
    journal.pop("generation")
    journal.pop("epoch")
    journal.pop("epoch_token")
    journal_path.write_text(json.dumps(journal), encoding="utf-8")
    journal_path.chmod(0o600)

    restarted = _postrun_manager(calls=[])
    restarted._cleanup_journal_dir = journal_dir
    restarted._load_cleanup_retries()

    recovered = restarted._cleanup_retries[managed.session_id]
    assert recovered.phase == "runtime_active"
    assert recovered.credential_auth_identity is None


def test_v5_credential_terminal_finalization_fails_closed_without_auth_authority(
    tmp_path: Path,
) -> None:
    journal_dir = tmp_path / "journal"
    session_dir = tmp_path / "session"
    credential_dir = tmp_path / "credentials"
    for root in (session_dir, credential_dir):
        root.mkdir(mode=0o700)
    auth = credential_dir / "auth.json"
    auth.write_text('{"access_token":"historical-canary"}', encoding="utf-8")
    auth.chmod(0o600)
    auth_identity = session_files._auth_identity(auth.stat(follow_symlinks=False))
    managed = _managed_postrun_session(
        session_dir,
        _session_result(session_id="v5-credential-finalization"),
    )
    managed.request.agent = AgentSpec(
        harness="codex",
        settings={"auth_mode": "subscription", "capture_mode": "transcript"},
    )
    managed.session_root_identity = capture_session_root_identity(session_dir)
    managed.credential_dir = credential_dir
    managed.credential_root_identity = capture_session_root_identity(credential_dir)
    managed.credential_mount = ManagedCredentialMount(
        root=credential_dir,
        root_identity=managed.credential_root_identity,
        auth_identity=auth_identity,
    )
    managed.credential_redactor = session_files.CredentialRedactor.from_auth_json(
        auth.read_bytes()
    )
    first = _postrun_manager(calls=[])
    first._cleanup_journal_dir = journal_dir
    first._register_cleanup_retry(managed, finalize_terminal=True)

    journal_path = next(journal_dir.glob("*.json"))
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    journal["version"] = 5
    journal.pop("revision")
    journal["credential_root"].pop("auth_identity")
    journal_path.write_text(json.dumps(journal), encoding="utf-8")
    journal_path.chmod(0o600)
    auth.unlink()
    auth.write_text('{"access_token":"replacement-canary"}', encoding="utf-8")
    auth.chmod(0o600)

    restarted = _postrun_manager(calls=[])
    restarted._cleanup_journal_dir = journal_dir
    with pytest.raises(RuntimeError, match="cleanup ownership journal is invalid"):
        restarted._load_cleanup_retries()

    assert journal_path.exists()
    assert auth.read_text(encoding="utf-8") == '{"access_token":"replacement-canary"}'


def test_terminal_finalization_phase_requires_durable_authority(tmp_path: Path) -> None:
    journal_dir = tmp_path / "journal"
    session_dir = tmp_path / "session"
    session_dir.mkdir(mode=0o700)
    managed = _managed_postrun_session(session_dir, _session_result())
    managed.session_root_identity = capture_session_root_identity(session_dir)
    first = _postrun_manager(calls=[])
    first._cleanup_journal_dir = journal_dir
    first._register_cleanup_retry(managed)

    journal_path = next(journal_dir.glob("*.json"))
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    journal["phase"] = "terminal_finalization"
    journal["subscription_finalization"] = None
    journal_path.write_text(json.dumps(journal), encoding="utf-8")
    journal_path.chmod(0o600)

    restarted = _postrun_manager(calls=[])
    restarted._cleanup_journal_dir = journal_dir
    with pytest.raises(RuntimeError, match="cleanup ownership journal is invalid"):
        restarted._load_cleanup_retries()

    assert journal_path.exists()
    assert session_dir.exists()


@pytest.mark.asyncio
async def test_legacy_journal_without_phase_stops_runtime_but_preserves_roots(
    tmp_path: Path,
) -> None:
    journal_dir = tmp_path / "journal"
    session_dir = tmp_path / "session"
    log_dir = tmp_path / "logs"
    for root in (session_dir, log_dir):
        root.mkdir(mode=0o700)
    managed = _managed_postrun_session(session_dir, _session_result())
    managed.session_root_identity = capture_session_root_identity(session_dir)
    managed.log_authority_dir = log_dir
    managed.log_authority_identity = capture_session_root_identity(log_dir)

    class LegacyRuntime:
        runtime_id = "legacy-runtime"
        container_id = "sha256:legacy-runtime"

    managed.runtime = LegacyRuntime()
    first = _postrun_manager(calls=[])
    first._cleanup_journal_dir = journal_dir
    first._register_cleanup_retry(managed)

    journal_path = next(journal_dir.glob("*.json"))
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    journal["version"] = 4
    journal.pop("revision")
    journal.pop("phase")
    journal.pop("kind")
    journal.pop("generation")
    journal.pop("epoch")
    journal.pop("epoch_token")
    journal["terminal_delivery"] = None
    journal_path.write_text(json.dumps(journal), encoding="utf-8")
    journal_path.chmod(0o600)

    calls: list[str] = []
    restarted = _postrun_manager(calls=calls)
    restarted._cleanup_journal_dir = journal_dir
    restarted._stop_recovered_container = AsyncMock(return_value=True)
    restarted._load_cleanup_retries()
    await restarted._reconcile_cleanup_retries()
    restarted._cleanup_retries = {}
    restarted._load_cleanup_retries()
    await restarted._reconcile_cleanup_retries()

    assert journal_path.exists()
    assert json.loads(journal_path.read_text(encoding="utf-8"))["version"] == 4
    assert session_dir.exists()
    assert log_dir.exists()
    assert "remove_session_dir" not in calls
    assert restarted._stop_recovered_container.await_count == 2


@pytest.mark.asyncio
async def test_terminal_delivery_journal_rejects_result_digest_tampering(
    tmp_path: Path,
) -> None:
    calls: list[str] = []
    journal_dir = tmp_path / "journal"
    session_dir = tmp_path / "session"
    session_dir.mkdir(mode=0o700)
    managed = _managed_postrun_session(session_dir, _session_result())
    managed.session_root_identity = capture_session_root_identity(session_dir)
    manager = _postrun_manager(calls=calls)
    manager._cleanup_journal_dir = journal_dir
    manager._push_result = AsyncMock(return_value=False)

    assert await manager._deliver_terminal_result(managed, managed.final_result) is False
    journal_path = next(journal_dir.glob("*.json"))
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    journal["terminal_delivery"]["result_digest"] = "0" * 64
    journal_path.write_text(json.dumps(journal), encoding="utf-8")
    journal_path.chmod(0o600)

    restarted = _postrun_manager(calls=[])
    restarted._cleanup_journal_dir = journal_dir
    with pytest.raises(RuntimeError, match="cleanup ownership journal is invalid"):
        restarted._load_cleanup_retries()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "timeout_seconds",
    [float("nan"), float("inf"), float("-inf"), 0.0, -1.0],
)
async def test_terminal_delivery_restart_rejects_invalid_export_timeout(
    tmp_path: Path,
    timeout_seconds: float,
) -> None:
    journal_dir = tmp_path / "journal"
    session_dir = tmp_path / "session"
    session_dir.mkdir(mode=0o700)
    client = FakeEvolutionClient(export_error=RuntimeError("backend unavailable"))
    manager = _postrun_manager(
        calls=[],
        evolution=EvolutionConfig(enabled=True),
        evolution_client=client,
    )
    manager._cleanup_journal_dir = journal_dir
    managed = _managed_postrun_session(session_dir, _session_result())
    managed.session_root_identity = capture_session_root_identity(session_dir)

    assert await manager._deliver_terminal_result(managed, managed.final_result) is False
    journal_path = next(journal_dir.glob("*.json"))
    payload = json.loads(journal_path.read_text(encoding="utf-8"))
    payload["terminal_delivery"]["export_authority"]["timeout_seconds"] = timeout_seconds
    journal_path.write_text(json.dumps(payload), encoding="utf-8")
    journal_path.chmod(0o600)

    restarted = _postrun_manager(calls=[])
    restarted._cleanup_journal_dir = journal_dir
    with pytest.raises(RuntimeError, match="cleanup ownership journal is invalid"):
        restarted._load_cleanup_retries()


@pytest.mark.asyncio
async def test_callback_retries_use_stable_result_idempotency_proof() -> None:
    manager = GatewayNodeManager.__new__(GatewayNodeManager)
    response = Mock()
    response.raise_for_status.return_value = None
    manager._client = AsyncMock()
    manager._client.post.return_value = response
    result = _session_result()
    expected_digest = manager._terminal_result_digest(result)

    assert await manager._push_result("http://rollout.test/callback", result)
    assert await manager._push_result("http://rollout.test/callback", result)

    for call in manager._client.post.await_args_list:
        assert call.kwargs["headers"] == {
            "Idempotency-Key": f"openevo-session-result-{expected_digest}",
            "X-OpenEvo-Result-SHA256": expected_digest,
        }


class FakeRuntime(BaseRuntime):
    def __init__(self, *, workdir: str | None = None) -> None:
        self._temporary = TemporaryDirectory(prefix="openevo-gateway-fake-runtime-")
        super().__init__(
            RuntimeSpec(image="runtime:latest", workdir=workdir),
            "fake-runtime-session",
            Path(self._temporary.name),
        )
        self.uploads: dict[str, str] = {}
        self.runtime_files: dict[str, bytes] = {}
        self.runtime_dirs: set[str] = set()
        self.exec_commands: list[str] = []
        self.downloads: list[tuple[str, str]] = []

    @property
    def runtime_id(self) -> str:
        return "fake-runtime"

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        self._destroyed = True

    async def exec(self, command, **kwargs):
        self.exec_commands.append(command)
        if "_OPENEVO_TARGET_READBACK_V1" in command:
            target_root = (self.spec.workdir or self.runtime_session_dir).rstrip("/")
            prefix = f"{target_root}/"
            entries = []
            for path, payload in self.runtime_files.items():
                if not path.startswith(prefix):
                    continue
                relative_path = path.removeprefix(prefix)
                parts = relative_path.split("/")
                if relative_path not in {
                    "AGENTS.md",
                    "agents.md",
                    "CLAUDE.md",
                    "GEMINI.md",
                } and not (
                    len(parts) == 3
                    and parts[:2] == [".openhands", "microagents"]
                    and parts[2].endswith(".md")
                ):
                    continue
                entries.append(
                    {
                        "relative_path": relative_path,
                        "size_bytes": len(payload),
                        "sha256": hashlib.sha256(payload).hexdigest(),
                    }
                )
            entries.sort(key=lambda item: item["relative_path"])
            return ExecResult(
                return_code=0,
                stdout=json.dumps(
                    {
                        "schema_version": "1",
                        "files": entries,
                        "consumed": {
                            "files": len(entries),
                            "nodes": 2 * len(entries),
                            "bytes": sum(item["size_bytes"] for item in entries),
                        },
                    }
                ),
            )
        return ExecResult(return_code=0)

    async def upload_file(self, source, target):
        payload = Path(source).read_bytes()
        self.uploads[target] = payload.decode("utf-8")
        self.set_runtime_file(target, payload)

    async def upload_dir(self, source, target):
        self.uploads[target] = str(source)
        self.runtime_dirs.add(target.rstrip("/"))
        host_target = self._session_host_path(target.rstrip("/"))
        if host_target is not None:
            host_target.mkdir(parents=True, exist_ok=True)
        source_root = Path(source)
        for path in source_root.rglob("*"):
            if path.is_file():
                remote = f"{target.rstrip('/')}/{path.relative_to(source_root).as_posix()}"
                self.set_runtime_file(remote, path.read_bytes())

    async def download_file(self, remote_path: str, local_path: str) -> None:
        self.downloads.append((remote_path, local_path))
        payload = self.runtime_files.get(remote_path)
        if payload is None:
            raise FileNotFoundError(remote_path)
        target = Path(local_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)

    async def download_dir(self, remote_path: str, local_path: str) -> None:
        self.downloads.append((remote_path, local_path))
        prefix = f"{remote_path.rstrip('/')}/"
        if remote_path.rstrip("/") not in self.runtime_dirs and not any(
            path.startswith(prefix) for path in self.runtime_files
        ):
            raise FileNotFoundError(remote_path)
        target_root = Path(local_path)
        target_root.mkdir(parents=True, exist_ok=True)
        for path, payload in self.runtime_files.items():
            if not path.startswith(prefix):
                continue
            target = target_root / path.removeprefix(prefix)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)

    def set_runtime_file(self, path: str, payload: bytes) -> None:
        self.runtime_files[path] = payload
        host_path = self._session_host_path(path)
        if host_path is not None:
            host_path.parent.mkdir(parents=True, exist_ok=True)
            host_path.write_bytes(payload)
        self._record_runtime_parents(path)

    def remove_runtime_file(self, path: str) -> bytes:
        payload = self.runtime_files.pop(path)
        host_path = self._session_host_path(path)
        if host_path is not None:
            host_path.unlink()
        return payload

    def _session_host_path(self, path: str) -> Path | None:
        prefix = f"{self.runtime_session_dir.rstrip('/')}/"
        if not path.startswith(prefix):
            return None
        relative = path.removeprefix(prefix)
        if not relative or any(part in {"", ".", ".."} for part in relative.split("/")):
            raise AssertionError("fake runtime path must be canonical")
        return self.session_dir.joinpath(*relative.split("/"))

    def _record_runtime_parents(self, path: str) -> None:
        parent = Path(path).parent.as_posix()
        while parent not in {"", ".", "/"}:
            self.runtime_dirs.add(parent)
            parent = Path(parent).parent.as_posix()


class LocalTargetInventoryRuntime:
    def __init__(self, root: Path) -> None:
        self.spec = RuntimeSpec(image="runtime:latest", workdir=str(root))
        self.runtime_session_dir = str(root)

    async def exec(self, command: str, **_kwargs: object) -> ExecResult:
        completed = subprocess.run(
            command,
            shell=True,
            check=False,
            capture_output=True,
            text=True,
        )
        return ExecResult(
            return_code=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )


@pytest.mark.asyncio
async def test_runtime_agent_system_target_inventory_is_bounded_and_no_follow(
    tmp_path: Path,
) -> None:
    (tmp_path / "AGENTS.md").write_text("actual root target", encoding="utf-8")
    microagents = tmp_path / ".openhands" / "microagents"
    microagents.mkdir(parents=True)
    (microagents / "repo.md").write_text("actual nested target", encoding="utf-8")
    (microagents / "ignored.txt").write_text("not an instruction target", encoding="utf-8")
    runtime = LocalTargetInventoryRuntime(tmp_path)
    budget = RuntimeReadbackBudget()

    inventory = await node_module._runtime_agent_system_target_inventory(
        runtime,
        budget=budget,
    )

    assert [item["relative_path"] for item in inventory] == [
        "agent_system_targets/.openhands/microagents/repo.md",
        "agent_system_targets/AGENTS.md",
    ]
    assert inventory[1]["sha256"] == hashlib.sha256(b"actual root target").hexdigest()
    assert budget.files_consumed == 2
    assert budget.bytes_consumed == len(b"actual root targetactual nested target")
    outside = tmp_path.parent / "outside-target.md"
    outside.write_text("must not be read", encoding="utf-8")
    (tmp_path / "CLAUDE.md").symlink_to(outside)
    with pytest.raises(ValueError, match="target readback failed"):
        await node_module._runtime_agent_system_target_inventory(
            runtime,
            budget=RuntimeReadbackBudget(),
        )


@pytest.mark.asyncio
async def test_runtime_agent_system_target_failure_exhausts_shared_budget(
    tmp_path: Path,
) -> None:
    (tmp_path / "AGENTS.md").write_bytes(b"over-budget")
    runtime = LocalTargetInventoryRuntime(tmp_path)
    budget = RuntimeReadbackBudget(max_bytes=4)

    with pytest.raises(ValueError, match="target readback failed"):
        await node_module._runtime_agent_system_target_inventory(
            runtime,
            budget=budget,
        )

    assert budget.files_consumed == budget.max_files
    assert budget.nodes_consumed == budget.max_nodes
    assert budget.bytes_consumed == budget.max_bytes


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
        if "-probe.sh" in command and "command_execution" in command:
            return ExecResult(
                stdout=f"{CODEX_SUBSCRIPTION_CANARY_OK}\n",
                return_code=0,
            )
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
        copied = self._copy_from_bind_mount(remote_path, Path(local_path))
        assert copied is True

    async def download_dir(self, remote_path: str, local_path: str) -> None:
        copied = self._copy_from_bind_mount(remote_path, Path(local_path))
        assert copied is True


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
    assert runtime.uploads["/openevo/session/evolution/memory.md"] == (
        "Remember parser precedence."
    )
    assert runtime.uploads["/openevo/session/evolution/agent_system.md"] == (
        "Prefer repository-local conventions."
    )
    assert not (tmp_path / ".openevo" / "evolution_upload").exists()
    assert runtime.uploads["/openevo/session/AGENTS.md"] == (
        "Prefer repository-local conventions."
    )
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
async def test_evolution_agent_system_upload_rejects_agent_replaced_fixed_target(
    tmp_path: Path,
) -> None:
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    runtime = BindMountRuntime(session_dir)
    outside = tmp_path / "outside-agents.md"
    outside.write_text("outside remains", encoding="utf-8")
    (session_dir / "AGENTS.md").symlink_to(outside)
    context = {
        "context_id": "ctx_1",
        "agent_system": {
            "rendered_text": "Use verified instructions.",
            "target_path": "AGENTS.md",
        },
        "skills": [],
        "adapter_merge_spec": {},
    }

    with pytest.raises(RuntimeError):
        await write_evolution_context_files(
            runtime=runtime,
            context=context,
            host_dir=session_dir,
            target_dir="/openevo/session/evolution",
        )

    assert outside.read_text(encoding="utf-8") == "outside remains"


@pytest.mark.asyncio
async def test_resolved_store_context_stages_all_text_artifacts(tmp_path):
    memory_text = "Remember to preserve parser precedence."
    agent_system_text = "Inspect repository conventions before editing."
    skill_text = "---\nname: parser-probe\n---\nUse recursive descent.\n"

    artifact_root = tmp_path / "artifacts"
    store = EvolutionStore(
        db_path=tmp_path / "evolution.db",
        artifact_root=artifact_root,
    )
    store.initialize()
    memory_source = artifact_root / "sources" / "memory.md"
    agent_system_source = artifact_root / "sources" / "AGENTS.md"
    skill_source = artifact_root / "sources" / "parser-skill"
    memory_source.parent.mkdir()
    skill_source.mkdir()
    memory_source.write_text(memory_text, encoding="utf-8")
    agent_system_source.write_text(agent_system_text, encoding="utf-8")
    (skill_source / "SKILL.md").write_text(skill_text, encoding="utf-8")

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
    assert env["OPENEVO_AGENT_SYSTEM_FILE"] == ("/openevo/session/evolution/agent_system.md")
    assert env["OPENEVO_AGENT_SYSTEM_TARGET"] == "/openevo/session/AGENTS.md"
    assert env["OPENEVO_AGENTS_MD"] == "/openevo/session/AGENTS.md"


async def _stage_gateway_runtime_receipt_context(
    tmp_path: Path,
    *,
    target_dir: str = "/openevo/session/evolution",
) -> tuple[
    GatewayNodeManager,
    ManagedSession,
    Any,
    dict[str, Any],
    list[str],
    FakeRuntime,
]:
    memory_text = "Remember the verified parser precedence."
    agent_system_text = "Read the repository instructions before editing."
    skill_text = "---\nname: verified-parser\n---\nUse recursive descent.\n"
    artifact_root = tmp_path / "receipt-artifacts"
    store = EvolutionStore(
        db_path=tmp_path / "receipt-evolution.db",
        artifact_root=artifact_root,
    )
    store.initialize()
    sources = artifact_root / "receipt-sources"
    skill_source = sources / "parser-skill"
    skill_source.mkdir(parents=True)
    memory_source = sources / "memory.md"
    agent_source = sources / "AGENTS.md"
    memory_source.write_text(memory_text, encoding="utf-8")
    agent_source.write_text(agent_system_text, encoding="utf-8")
    (skill_source / "SKILL.md").write_text(skill_text, encoding="utf-8")

    registered = [
        store.register_artifact(
            ArtifactRegisterRequest(
                type=ArtifactType.TEXT_MEMORY,
                name="verified memory",
                uri=memory_source.as_uri(),
                compatibility={"task_tags": ["parser"], "agent_harness": ["codex"]},
                scores={"quality": 0.9},
                promoted=False,
            )
        ),
        store.register_artifact(
            ArtifactRegisterRequest(
                type=ArtifactType.SKILL_BUNDLE,
                name="verified skill",
                uri=skill_source.as_uri(),
                compatibility={"task_tags": ["parser"], "agent_harness": ["codex"]},
                scores={"quality": 0.8},
                promoted=False,
            )
        ),
        store.register_artifact(
            ArtifactRegisterRequest(
                type=ArtifactType.AGENT_SYSTEM,
                name="verified agent system",
                uri=agent_source.as_uri(),
                manifest={"target_path": "AGENTS.md"},
                compatibility={"task_tags": ["parser"], "agent_harness": ["codex"]},
                scores={"quality": 0.85},
                promoted=False,
            )
        ),
    ]
    artifact_ids = sorted(item.artifact_id for item in registered)
    context = store.resolve_context(
        ContextResolveRequest(
            task_id="receipt-task",
            instruction="Fix parser precedence.",
            agent={"harness": "codex"},
            metadata={
                "task_tags": ["parser"],
                "evolution": {"context_artifact_ids": artifact_ids},
            },
        )
    ).model_dump(mode="json")
    runtime = FakeRuntime()
    manager = GatewayNodeManager.__new__(GatewayNodeManager)
    manager.evolution = EvolutionConfig(
        enabled=True,
        context={"fail_open": True, "target_dir": target_dir},
    )
    manager.evolution_client = FakeEvolutionClient(context=context)
    manager.model_served = "served-model"
    request = SessionDispatchRequest(
        session_id="session-receipt",
        task_id="receipt-task",
        instruction="Fix parser precedence.",
        remaining_timeout_seconds=60,
        agent=AgentSpec(harness="fake"),
        metadata={
            "openevo": {"revision_id": "revision-verified"},
            "evolution": {"context_artifact_ids": artifact_ids},
        },
    )
    managed = ManagedSession(
        request=request,
        timer=StageTimer(),
        session_dir=tmp_path,
        artifacts_dir=tmp_path / "session-artifacts",
        runtime=runtime,
    )
    managed.execution_deadline = asyncio.get_running_loop().time() + 60

    injection = await manager._resolve_and_inject_evolution_context(managed, FakeHarness())
    return manager, managed, injection, context, artifact_ids, runtime


@pytest.mark.asyncio
async def test_gateway_runtime_receipt_binds_three_selected_artifacts(tmp_path: Path) -> None:
    (
        manager,
        managed,
        injection,
        context,
        artifact_ids,
        runtime,
    ) = await _stage_gateway_runtime_receipt_context(tmp_path)
    request = managed.request
    receipt = await node_module._runtime_injection_receipt_from_readback(
        runtime=runtime,
        target_dir=manager.evolution.context.target_dir,
        plan=injection.staged.injection_plan,
    )
    manager._publish_runtime_injection_receipt(managed, receipt)

    assert request.metadata["evolution"]["runtime_injection_receipt"] == receipt
    assert receipt["schema_version"] == "3"
    assert receipt["context_id"] == context["context_id"]
    assert receipt["revision_id"] == "revision-verified"
    assert (
        receipt["instruction_sha256"]
        == hashlib.sha256(request.instruction.encode("utf-8")).hexdigest()
    )
    assert len(receipt["runtime_tree_sha256"]) == 64
    assert receipt["files"]
    inventory = {item["artifact_id"]: item for item in context["selection"]["artifacts"]}
    received = {item["artifact_id"]: item for item in receipt["artifacts"]}
    assert set(received) == set(artifact_ids)
    for artifact_id, item in received.items():
        assert item["artifact_type"] == inventory[artifact_id]["artifact_type"]
        assert item["content_sha256"] == inventory[artifact_id]["content_sha256"]
        assert item["runtime_paths"]
        assert len(item["runtime_tree_sha256"]) == 64
    assert {item["artifact_type"] for item in receipt["artifacts"]} == {
        "agent_system",
        "skill_bundle",
        "text_memory",
    }
    assert "file://" not in runtime.uploads["/openevo/session/evolution/context.json"]
    assert str(tmp_path) not in runtime.uploads["/openevo/session/evolution/context.json"]
    assert runtime.downloads == []


@pytest.mark.asyncio
async def test_gateway_runtime_receipt_ignores_plugin_reported_inventory(
    tmp_path: Path,
) -> None:
    (
        manager,
        _managed,
        injection,
        _context,
        _artifact_ids,
        _runtime,
    ) = await _stage_gateway_runtime_receipt_context(tmp_path)

    class SelfReportingRuntime:
        called = False
        spec = RuntimeSpec(image="runtime:latest")
        runtime_session_dir = "/openevo/session"

        async def download_dir(self, *args: object, **kwargs: object) -> object:
            del kwargs
            self.called = True
            destination = Path(str(args[1]))
            destination.mkdir(parents=True, exist_ok=True)
            (destination / "forged.txt").write_text("forged", encoding="utf-8")
            return {"files": "plugin-controlled"}

        async def exec(self, *_args: object, **_kwargs: object) -> ExecResult:
            return ExecResult(
                return_code=0,
                stdout=json.dumps(
                    {
                        "schema_version": "1",
                        "files": [],
                        "consumed": {"files": 0, "nodes": 0, "bytes": 0},
                    }
                ),
            )

    runtime = SelfReportingRuntime()
    with pytest.raises(ValueError):
        await node_module._runtime_injection_receipt_from_readback(
            runtime=runtime,
            target_dir=manager.evolution.context.target_dir,
            plan=injection.staged.injection_plan,
        )

    assert runtime.called is True


@pytest.mark.asyncio
async def test_gateway_runtime_receipt_custom_target_uses_compatible_download(
    tmp_path: Path,
) -> None:
    target_dir = "/custom/evolution"
    (
        manager,
        _managed,
        injection,
        _context,
        _artifact_ids,
        runtime,
    ) = await _stage_gateway_runtime_receipt_context(tmp_path, target_dir=target_dir)

    receipt = await node_module._runtime_injection_receipt_from_readback(
        runtime=runtime,
        target_dir=target_dir,
        plan=injection.staged.injection_plan,
    )

    assert receipt["schema_version"] == "3"
    assert runtime.downloads and runtime.downloads[0][0] == target_dir


@pytest.mark.asyncio
async def test_gateway_compatibility_cleanup_preserves_background_root_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        manager,
        _managed,
        injection,
        _context,
        _artifact_ids,
        runtime,
    ) = await _stage_gateway_runtime_receipt_context(tmp_path)
    runtime.spec = runtime.spec.model_copy(update={"import_path": "tests.runtime:PluginRuntime"})
    monkeypatch.setattr(runtime_base.tempfile, "gettempdir", lambda: str(tmp_path))
    original_download = runtime.download_dir
    original_rename = runtime_base._rename_readback_cleanup_noreplace
    race_requested = threading.Event()
    race_complete = threading.Event()
    root_path: Path | None = None
    displaced = tmp_path / "displaced-readback-root"

    def background_writer() -> None:
        assert race_requested.wait(2)
        assert root_path is not None
        root_path.rename(displaced)
        root_path.mkdir(mode=0o700)
        (root_path / "replacement.txt").write_text("replacement", encoding="utf-8")
        race_complete.set()

    writer = threading.Thread(target=background_writer, daemon=True)

    async def download_and_arm(remote_path: str, local_path: str) -> None:
        nonlocal root_path
        await original_download(remote_path, local_path)
        root_path = Path(local_path).parent
        writer.start()

    def race_root_after_identity_check(
        source_fd: int,
        source_name: str,
        target_fd: int,
        target_name: str,
    ) -> None:
        if source_name.startswith("openevo-evolution-readback-"):
            race_requested.set()
            assert race_complete.wait(2)
        original_rename(source_fd, source_name, target_fd, target_name)

    monkeypatch.setattr(runtime, "download_dir", download_and_arm)
    monkeypatch.setattr(
        runtime_base,
        "_rename_readback_cleanup_noreplace",
        race_root_after_identity_check,
    )

    with pytest.raises(RuntimePathSecurityError, match="quarantine identity"):
        await node_module._runtime_injection_receipt_from_readback(
            runtime=runtime,
            target_dir=manager.evolution.context.target_dir,
            plan=injection.staged.injection_plan,
        )

    writer.join(timeout=2)
    assert writer.is_alive() is False
    assert displaced.is_dir()
    quarantines = list(tmp_path.glob(".openevo-readback-quarantine-*"))
    assert len(quarantines) == 1
    assert (quarantines[0] / "replacement.txt").read_text(encoding="utf-8") == ("replacement")


@pytest.mark.asyncio
async def test_gateway_deadline_is_bounded_when_public_download_refuses_cancel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        manager,
        _managed,
        injection,
        _context,
        _artifact_ids,
        _runtime,
    ) = await _stage_gateway_runtime_receipt_context(tmp_path)
    monkeypatch.setattr(runtime_base.tempfile, "gettempdir", lambda: str(tmp_path))

    class RefusingRuntime:
        started = asyncio.Event()
        release = threading.Event()
        cancellation_seen = False

        async def download_dir(self, _remote_path: str, local_path: str) -> None:
            target = Path(local_path)
            target.mkdir(exist_ok=True)
            (target / "partial.txt").write_text("partial", encoding="utf-8")
            self.started.set()
            while not self.release.is_set():
                try:
                    await asyncio.sleep(0.01)
                except asyncio.CancelledError:
                    self.cancellation_seen = True

    runtime = RefusingRuntime()
    receipt = asyncio.create_task(
        node_module._runtime_injection_receipt_from_readback(
            runtime=runtime,
            target_dir="/custom/evolution",
            plan=injection.staged.injection_plan,
        )
    )
    await asyncio.wait_for(runtime.started.wait(), timeout=1)
    started = asyncio.get_running_loop().time()

    with pytest.raises(RuntimePathSecurityError, match="hard join bound"):
        await asyncio.wait_for(receipt, timeout=0.05)

    try:
        assert asyncio.get_running_loop().time() - started < 1.8
        assert runtime.cancellation_seen is True
        assert list(tmp_path.glob(".openevo-readback-quarantine-*"))
    finally:
        runtime.release.set()
        await asyncio.sleep(0.1)
        assert list(tmp_path.glob(".openevo-readback-quarantine-*"))


@pytest.mark.asyncio
async def test_gateway_compatibility_readback_shares_budget_with_agent_system(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        manager,
        _managed,
        injection,
        _context,
        _artifact_ids,
        _runtime,
    ) = await _stage_gateway_runtime_receipt_context(tmp_path)

    class PublicDownloadRuntime:
        async def download_dir(self, _remote_path: str, local_path: str) -> None:
            target = Path(local_path)
            target.mkdir(exist_ok=True)
            for index in range(257):
                (target / f"file-{index:03d}.txt").write_bytes(b"x")

    observed: dict[str, object] = {}

    async def agent_inventory(
        _runtime: object,
        *,
        budget: RuntimeReadbackBudget,
        sealed: bool,
    ) -> list[dict[str, object]]:
        observed["before"] = (
            budget.files_consumed,
            budget.nodes_consumed,
            budget.bytes_consumed,
        )
        observed["sealed"] = sealed
        observed["budget"] = budget
        budget.consume_report(files=1, nodes=2, bytes_read=1)
        return [
            {
                "relative_path": "agent_system_targets/AGENTS.md",
                "size_bytes": 1,
                "sha256": hashlib.sha256(b"a").hexdigest(),
            }
        ]

    monkeypatch.setattr(
        node_module,
        "_runtime_agent_system_target_inventory",
        agent_inventory,
    )
    monkeypatch.setattr(
        node_module,
        "receipt_from_runtime_readback",
        lambda _authority, files: {"files": list(files)},
    )

    receipt = await node_module._runtime_injection_receipt_from_readback(
        runtime=PublicDownloadRuntime(),
        target_dir="/custom/evolution",
        plan=injection.staged.injection_plan,
    )

    assert observed["before"] == (257, 518, 257)
    assert observed["sealed"] is False
    budget = observed["budget"]
    assert isinstance(budget, RuntimeReadbackBudget)
    assert budget.files_consumed == 258
    assert budget.nodes_consumed == 520
    assert budget.bytes_consumed == 258
    assert len(receipt["files"]) == 258


@pytest.mark.asyncio
@pytest.mark.parametrize("fallback", ["non_linux", "unavailable", "third_party"])
async def test_gateway_runtime_receipt_without_sealed_primitive_uses_compatible_download(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fallback: str,
) -> None:
    (
        manager,
        _managed,
        injection,
        _context,
        _artifact_ids,
        runtime,
    ) = await _stage_gateway_runtime_receipt_context(tmp_path)
    if fallback == "non_linux":
        monkeypatch.setattr(runtime_base.sys, "platform", "darwin")
    elif fallback == "unavailable":
        monkeypatch.setattr(
            node_module,
            "_has_sealed_session_bind_readback",
            lambda _runtime: False,
        )
    else:
        runtime.spec = runtime.spec.model_copy(
            update={"import_path": "tests.runtime:PluginRuntime"}
        )

    receipt = await node_module._runtime_injection_receipt_from_readback(
        runtime=runtime,
        target_dir=manager.evolution.context.target_dir,
        plan=injection.staged.injection_plan,
    )

    assert receipt["schema_version"] == "3"
    assert runtime.downloads


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation",
    [
        "replace_instruction",
        "replace_memory",
        "replace_skill",
        "extra_skill",
        "missing_agent_target",
        "wrong_agent_target",
        "extra_agent_target",
    ],
)
async def test_gateway_runtime_receipt_fails_closed_on_runtime_readback_drift(
    tmp_path: Path,
    mutation: str,
) -> None:
    (
        manager,
        managed,
        injection,
        _context,
        _artifact_ids,
        runtime,
    ) = await _stage_gateway_runtime_receipt_context(tmp_path)
    if mutation == "replace_instruction":
        runtime.set_runtime_file(
            "/openevo/session/evolution/instruction.txt",
            b"replaced",
        )
    elif mutation == "replace_memory":
        runtime.set_runtime_file(
            "/openevo/session/evolution/memory.md",
            b"replaced",
        )
    elif mutation == "replace_skill":
        skill_path = next(
            path
            for path in runtime.runtime_files
            if path.endswith("/SKILL.md") and path.startswith("/openevo/session/evolution/skills/")
        )
        runtime.set_runtime_file(skill_path, b"replaced")
    elif mutation == "extra_skill":
        runtime.set_runtime_file(
            "/openevo/session/evolution/skills/unexpected/SKILL.md",
            b"unexpected",
        )
    elif mutation == "missing_agent_target":
        runtime.remove_runtime_file("/openevo/session/AGENTS.md")
    elif mutation == "wrong_agent_target":
        runtime.set_runtime_file(
            "/openevo/session/CLAUDE.md",
            runtime.remove_runtime_file("/openevo/session/AGENTS.md"),
        )
    else:
        runtime.set_runtime_file(
            "/openevo/session/CLAUDE.md",
            b"unexpected target",
        )

    with pytest.raises((FileNotFoundError, ValueError)):
        await node_module._runtime_injection_receipt_from_readback(
            runtime=runtime,
            target_dir=manager.evolution.context.target_dir,
            plan=injection.staged.injection_plan,
        )

    assert "runtime_injection_receipt" not in managed.request.metadata["evolution"]


@pytest.mark.asyncio
async def test_gateway_exact_context_renders_revision_memory_order(tmp_path: Path) -> None:
    artifact_root = tmp_path / "ordered-artifacts"
    store = EvolutionStore(
        db_path=tmp_path / "ordered-evolution.db",
        artifact_root=artifact_root,
    )
    store.initialize()
    sources = artifact_root / "sources"
    sources.mkdir(parents=True)
    high_score_source = sources / "high-score.md"
    low_score_source = sources / "low-score.md"
    high_score_source.write_text("High score memory.", encoding="utf-8")
    low_score_source.write_text("Low score memory.", encoding="utf-8")
    high_score = store.register_artifact(
        ArtifactRegisterRequest(
            type=ArtifactType.TEXT_MEMORY,
            name="high score memory",
            uri=high_score_source.as_uri(),
            scores={"quality": 1.0},
            promoted=False,
        )
    )
    low_score = store.register_artifact(
        ArtifactRegisterRequest(
            type=ArtifactType.TEXT_MEMORY,
            name="low score memory",
            uri=low_score_source.as_uri(),
            scores={"quality": 0.1},
            promoted=False,
        )
    )
    artifact_ids = [low_score.artifact_id, high_score.artifact_id]
    context = store.resolve_context(
        ContextResolveRequest(
            task_id="ordered-task",
            instruction="Use revision order.",
            metadata={"evolution": {"context_artifact_ids": artifact_ids}},
        )
    ).model_dump(mode="json")
    runtime = FakeRuntime()
    manager = GatewayNodeManager.__new__(GatewayNodeManager)
    manager.evolution = EvolutionConfig(enabled=True, context={"fail_open": False})
    manager.evolution_client = FakeEvolutionClient(context=context)
    manager.model_served = "served-model"
    request = SessionDispatchRequest(
        session_id="session-ordered",
        task_id="ordered-task",
        instruction="Use revision order.",
        remaining_timeout_seconds=60,
        agent=AgentSpec(harness="fake"),
        metadata={
            "openevo": {"revision_id": "revision-ordered"},
            "evolution": {"context_artifact_ids": artifact_ids},
        },
    )
    managed = ManagedSession(
        request=request,
        timer=StageTimer(),
        session_dir=tmp_path,
        artifacts_dir=tmp_path / "ordered-session-artifacts",
        runtime=runtime,
    )
    managed.execution_deadline = asyncio.get_running_loop().time() + 60

    injection = await manager._resolve_and_inject_evolution_context(managed, FakeHarness())
    assert context["selection"]["artifact_ids"] == artifact_ids
    assert runtime.uploads["/openevo/session/evolution/memory.md"] == (
        "Low score memory.\n\nHigh score memory."
    )
    receipt = await node_module._runtime_injection_receipt_from_readback(
        runtime=runtime,
        target_dir=manager.evolution.context.target_dir,
        plan=injection.staged.injection_plan,
    )
    assert [item["artifact_id"] for item in receipt["artifacts"]] == artifact_ids


@pytest.mark.asyncio
async def test_gateway_runtime_receipt_rejects_forged_selection_digest(
    tmp_path: Path,
) -> None:
    artifact_root = tmp_path / "forged-artifacts"
    store = EvolutionStore(
        db_path=tmp_path / "forged-evolution.db",
        artifact_root=artifact_root,
    )
    store.initialize()
    source = artifact_root / "memory.md"
    source.write_text("Verified memory.", encoding="utf-8")
    artifact = store.register_artifact(
        ArtifactRegisterRequest(
            type=ArtifactType.TEXT_MEMORY,
            name="verified memory",
            uri=source.as_uri(),
            promoted=False,
        )
    )
    context = store.resolve_context(
        ContextResolveRequest(
            task_id="task",
            instruction="Work.",
            agent={},
            metadata={"evolution": {"context_artifact_ids": [artifact.artifact_id]}},
        )
    ).model_dump(mode="json")
    context["selection"]["artifacts"][0]["content_sha256"] = "0" * 64
    manager = GatewayNodeManager.__new__(GatewayNodeManager)
    manager.evolution = EvolutionConfig(enabled=True, context={"fail_open": True})
    manager.evolution_client = FakeEvolutionClient(context=context)
    manager.model_served = "served-model"
    request = SessionDispatchRequest(
        session_id="session-forged",
        task_id="task",
        instruction="Work.",
        remaining_timeout_seconds=60,
        agent=AgentSpec(harness="fake"),
        metadata={
            "openevo": {"revision_id": "revision-verified"},
            "evolution": {"context_artifact_ids": [artifact.artifact_id]},
        },
    )
    managed = ManagedSession(
        request=request,
        timer=StageTimer(),
        session_dir=tmp_path,
        artifacts_dir=tmp_path / "session-artifacts",
        runtime=FakeRuntime(),
    )
    managed.execution_deadline = asyncio.get_running_loop().time() + 60

    with pytest.raises(ValueError, match="artifact digest"):
        await manager._resolve_and_inject_evolution_context(managed, FakeHarness())

    assert "runtime_injection_receipt" not in request.metadata["evolution"]


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
    assert env["OPENEVO_AGENT_SYSTEM_TARGET"] == (
        "/openevo/session/.openhands/microagents/repo.md"
    )
    assert json.loads(env["OPENEVO_AGENT_SYSTEM_TARGETS"]) == [
        "/openevo/session/.openhands/microagents/repo.md"
    ]
    assert "OPENEVO_AGENTS_MD" not in env


@pytest.mark.asyncio
async def test_write_evolution_context_files_rejects_unsafe_agent_system_target(
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

    with pytest.raises(ValueError, match="supported harness instruction path"):
        await write_evolution_context_files(
            runtime=runtime,
            context=context,
            host_dir=tmp_path,
            target_dir="/openevo/session/evolution",
        )

    assert runtime.uploads == {}


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
    assert sorted(path.name for path in staged_skill.iterdir()) == ["SKILL.md"]


@pytest.mark.asyncio
async def test_write_evolution_context_files_rejects_bad_skill_without_partial_staging(
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

    with pytest.raises(ValueError, match="file://"):
        await write_evolution_context_files(
            runtime=runtime,
            context=context,
            host_dir=tmp_path,
            target_dir="/openevo/session/evolution",
        )

    assert not (tmp_path / "evolution").exists()


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
        "error": "context_resolution_failed",
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
        "error": "context_resolution_failed",
    }


@pytest.mark.asyncio
async def test_exact_context_resolution_never_fails_open(tmp_path: Path) -> None:
    manager = GatewayNodeManager.__new__(GatewayNodeManager)
    manager.evolution = EvolutionConfig(enabled=True, context={"fail_open": True})
    manager.evolution_client = FakeEvolutionClient(error=RuntimeError("backend down"))
    manager.model_served = "served-model"
    request = SessionDispatchRequest(
        session_id="session-exact-empty",
        task_id="task-exact-empty",
        instruction="Do work.",
        remaining_timeout_seconds=60,
        agent=AgentSpec(harness="fake"),
        metadata={"evolution": {"context_artifact_ids": []}},
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
        await manager._resolve_and_inject_evolution_context(managed, FakeHarness())


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
        runtime.exec_envs[-1]["OPENEVO_EVOLUTION_CONTEXT"]
        == "/openevo/session/evolution/context.json"
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
    assert len(codex_commands) == 2
    canary_command, command = codex_commands
    assert "-probe.sh" in canary_command
    assert canary_command.count("/opt/codex/bin/codex exec ") == 2
    assert "sandbox linux" not in canary_command
    assert "command_execution" in canary_command
    assert command.startswith("/bin/bash -o pipefail -c ")
    assert "env -u" in command
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
    auth = b'{"tokens":{"access_token":"access-canary","refresh_token":"refresh-canary"}}'
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

    agent_result = await manager._run_exec_inputs(
        runtime,
        [ExecInput(command="codex exec")],
        {},
        managed,
    )
    completion = CompletionSession(
        session_id="session_1",
        metadata={
            "agent_result_metadata": {
                "log_dir": agent_result.metadata["log_dir"],
                "last_step": 0,
            }
        },
    )
    trajectory = await AgentTranscriptBuilder().build(completion)

    persisted = (
        "\n".join(
            path.read_text(encoding="utf-8")
            for path in (managed.log_authority_dir / "logs" / "agent").iterdir()
        )
        + trajectory.model_dump_json()
    )
    assert "access-canary" not in persisted
    assert "refresh-canary" not in persisted
    assert auth.decode() not in persisted


@pytest.mark.asyncio
@pytest.mark.parametrize("hostile_leaf", ["symlink", "fifo"])
async def test_run_exec_inputs_rejects_precreated_log_leaf_without_blocking(
    tmp_path: Path,
    hostile_leaf: str,
) -> None:
    session_dir = tmp_path / "session"
    session_log_dir = session_dir / "logs" / "agent"
    session_log_dir.mkdir(parents=True)
    authority_dir = tmp_path / "core-log-authority"
    authority_log_dir = authority_dir / "logs" / "agent"
    authority_log_dir.mkdir(parents=True)
    outside = tmp_path / "outside.log"
    outside.write_text("outside remains", encoding="utf-8")
    hostile_paths = [
        session_log_dir / "step.00.stdout.log",
        authority_log_dir / "step.00.stdout.log",
    ]
    if hostile_leaf == "symlink":
        for path in hostile_paths:
            path.symlink_to(outside)
        unblocker = None
    else:
        for path in hostile_paths:
            os.mkfifo(path, mode=0o600)

        def unblock_unsafe_writer() -> None:
            time.sleep(0.2)
            for path in hostile_paths:
                descriptor = os.open(path, os.O_RDWR | os.O_NONBLOCK)
                os.close(descriptor)

        unblocker = threading.Thread(target=unblock_unsafe_writer, daemon=True)
        unblocker.start()

    runtime = BindMountRuntime(session_dir)
    runtime.exec_results = [
        ExecResult(return_code=0, stdout='{"type":"agent_message","text":"safe"}\n')
    ]
    request = SessionDispatchRequest(
        session_id="session_1",
        task_id="task_1",
        instruction="Do work.",
        remaining_timeout_seconds=60,
        runtime=RuntimeSpec(image="runtime:latest"),
        agent=AgentSpec(
            harness="shell",
            settings={"capture_mode": "transcript"},
            custom_shell=ExecInput(command="agent"),
        ),
    )
    managed_kwargs = {
        "request": request,
        "timer": StageTimer(),
        "session_dir": session_dir,
        "artifacts_dir": session_dir / "artifacts",
        "session_root_identity": capture_session_root_identity(session_dir),
        "runtime": runtime,
    }
    if "log_authority_dir" in ManagedSession.__dataclass_fields__:
        managed_kwargs.update(
            {
                "log_authority_dir": authority_dir,
                "log_authority_identity": capture_session_root_identity(authority_dir),
            }
        )
    managed = ManagedSession(**managed_kwargs)
    managed.execution_deadline = asyncio.get_running_loop().time() + 60
    manager = GatewayNodeManager.__new__(GatewayNodeManager)
    manager.gateway_url = "http://gateway.test"
    started = time.monotonic()

    with pytest.raises(session_files.SessionFileSecurityError):
        await manager._run_exec_inputs(
            runtime,
            [ExecInput(command="agent")],
            {},
            managed,
        )

    elapsed = time.monotonic() - started
    if unblocker is not None:
        unblocker.join(timeout=1)
    assert elapsed < 0.15
    assert outside.read_text(encoding="utf-8") == "outside remains"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("terminal", "return_code", "expected_status", "expected_error"),
    [
        ("timeout", -1, SessionStatus.TIMEOUT, "timed out"),
        ("cancel", -9, SessionStatus.ERROR, "session cancelled"),
    ],
)
async def test_subscription_finalization_preserves_transcript_after_execution_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    terminal: str,
    return_code: int,
    expected_status: SessionStatus,
    expected_error: str,
) -> None:
    session_dir = tmp_path / "session"
    session_dir.mkdir(mode=0o700)
    authority_dir = tmp_path / "core-log-authority"
    authority_dir.mkdir(mode=0o700)
    (authority_dir / "logs" / "agent").mkdir(parents=True)
    runtime = BindMountRuntime(session_dir)
    runtime.exec_results = [
        ExecResult(
            return_code=return_code,
            stdout='{"type":"agent_message","text":"captured before terminal"}\n',
        )
    ]
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
        builder={"strategy": "agent_transcript"},
    )
    managed_kwargs = {
        "request": request,
        "timer": StageTimer(),
        "session_dir": session_dir,
        "artifacts_dir": session_dir / "artifacts",
        "session_root_identity": capture_session_root_identity(session_dir),
        "runtime": runtime,
    }
    if "log_authority_dir" in ManagedSession.__dataclass_fields__:
        managed_kwargs.update(
            {
                "log_authority_dir": authority_dir,
                "log_authority_identity": capture_session_root_identity(authority_dir),
            }
        )
    managed = ManagedSession(**managed_kwargs)
    manager = GatewayNodeManager.__new__(GatewayNodeManager)
    manager.node_id = "node-a"
    manager.gateway_url = "http://gateway.test"
    manager.storage = EmptyCompletionStorage()
    manager.builders = default_builder_registry()
    manager.session_registry = SessionRegistry()
    manager.session_registry.register("session_1", task_id="task_1")
    manager._cleanup_retries = {}
    manager._retire_cleanup_ownership = Mock()
    delivered: list[SessionResult] = []

    async def capture_result(
        captured_managed: ManagedSession,
        result: SessionResult,
    ) -> None:
        assert captured_managed is managed
        delivered.append(result)

    async def retain_roots(captured_managed: ManagedSession) -> bool:
        assert captured_managed is managed
        return False

    monkeypatch.setattr(manager, "_deliver_terminal_result", capture_result)
    monkeypatch.setattr(manager, "_remove_owned_roots", retain_roots)
    managed.execution_deadline = asyncio.get_running_loop().time() + 60
    managed.agent_result = await manager._run_exec_inputs(
        runtime,
        [ExecInput(command="codex exec")],
        {},
        managed,
    )
    if terminal == "timeout":
        managed.pending_status = SessionStatus.TIMEOUT
        managed.pending_error = "step 0 timed out"
    else:
        managed.cancel_requested = True
    managed.execution_deadline = asyncio.get_running_loop().time() - 1

    await manager._finalize_after_runtime_absence(
        managed,
        result=None,
    )

    assert len(delivered) == 1
    result = delivered[0]
    assert result.status == expected_status
    assert expected_error in (result.error or "")
    assert result.trajectory.status == expected_status
    assert result.trajectory.metadata["builder"] == "agent_transcript"
    assert result.trajectory.metadata["capture_mode"] == "transcript"
    assert result.trajectory.metadata["token_level_metrics_available"] is False
    assert result.trajectory.traces[0].response_messages == [
        {"role": "assistant", "content": "captured before terminal"}
    ]


@pytest.mark.asyncio
async def test_handle_run_postprocess_timeout_preserves_step_transcript_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class SlowPostprocessHarness(RunStepHarness):
        @property
        def subscription_credential_isolation_receipt(self) -> dict[str, object]:
            return codex_subscription_readiness_receipt()

        async def postprocess(
            self,
            runtime: BaseRuntime,
            result: AgentRunResult,
        ) -> None:
            del runtime, result
            await asyncio.sleep(1)

    session_dir = tmp_path / "session"
    session_dir.mkdir()
    runtime = BindMountRuntime(session_dir)
    runtime.exec_results = [
        ExecResult(
            return_code=-1,
            stdout='{"type":"agent_message","text":"before timeout"}\n',
        )
    ]
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
        builder={"strategy": "agent_transcript"},
    )
    harness = SlowPostprocessHarness(request.agent)
    manager = GatewayNodeManager.__new__(GatewayNodeManager)
    manager.node_id = "node-a"
    manager.gateway_url = "http://gateway.test"
    manager.evolution = EvolutionConfig(enabled=False)
    manager.evolution_client = None
    manager.storage = EmptyCompletionStorage()
    manager.builders = default_builder_registry()
    manager.session_registry = SessionRegistry()
    manager.session_registry.register("session_1", task_id="task_1")
    monkeypatch.setattr(manager, "_resolve_agent_harness", lambda _: harness)
    managed = ManagedSession(
        request=request,
        timer=StageTimer(),
        session_dir=session_dir,
        artifacts_dir=session_dir / "artifacts",
        session_root_identity=capture_session_root_identity(session_dir),
        runtime=runtime,
    )
    managed.execution_deadline = asyncio.get_running_loop().time() + 0.1

    await manager._handle_run(managed)

    assert managed.agent_result is not None
    assert managed.agent_result.status == "timeout"
    assert managed.agent_result.metadata["last_step"] == 0
    assert managed.execution_deadline < asyncio.get_running_loop().time()
    result = await manager._build_session_result(managed)
    assert result.status == SessionStatus.TIMEOUT
    assert result.trajectory.metadata["capture_mode"] == "transcript"
    assert result.trajectory.metadata["token_level_metrics_available"] is False
    assert result.trajectory.traces[0].response_messages == [
        {"role": "assistant", "content": "before timeout"}
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "settings",
    [
        {"auth_mode": "subscription", "capture_mode": "transcript"},
        {"capture_mode": "transcript"},
    ],
    ids=["subscription", "self-deployed"],
)
async def test_managed_runtime_without_proxy_completions_uses_verified_transcript_bytes(
    tmp_path,
    monkeypatch,
    settings: dict[str, str],
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
        runtime=RuntimeSpec(
            profile="managed_science",
            image=MANAGED_RUNTIME_IMAGES["managed_science"],
            container_user="host",
        ),
        agent=AgentSpec(
            harness="codex",
            settings=settings,
        ),
        metadata={},
    )
    managed = ManagedSession(
        request=request,
        timer=StageTimer(),
        session_dir=tmp_path,
        artifacts_dir=tmp_path / "artifacts",
        session_root_identity=capture_session_root_identity(tmp_path),
        log_authority_dir=tmp_path,
        log_authority_identity=capture_session_root_identity(tmp_path),
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
async def test_subscription_transcript_read_is_async_and_finalization_budgeted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log_dir = tmp_path / "logs" / "agent"
    log_dir.mkdir(parents=True)
    transcript = log_dir / "step.00.stdout.log"
    transcript.write_bytes(b'{"type":"agent_message","text":"late"}\n')
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
    )
    managed = ManagedSession(
        request=request,
        timer=StageTimer(),
        session_dir=tmp_path,
        artifacts_dir=tmp_path / "artifacts",
        session_root_identity=capture_session_root_identity(tmp_path),
        log_authority_dir=tmp_path,
        log_authority_identity=capture_session_root_identity(tmp_path),
        agent_result=AgentRunResult(
            status="completed",
            return_code=0,
            metadata={"log_dir": str(log_dir), "last_step": 0},
        ),
    )
    managed.execution_deadline = asyncio.get_running_loop().time() - 1
    managed.finalization_deadline = asyncio.get_running_loop().time() + 0.05

    original_reader = session_files.read_verified_session_transcript

    def delayed_reader(*args, **kwargs):
        time.sleep(0.2)
        return original_reader(*args, **kwargs)

    monkeypatch.setattr("openevo.gateway.node.read_verified_session_transcript", delayed_reader)
    event_loop_progress = asyncio.Event()

    async def tick() -> None:
        await asyncio.sleep(0.01)
        event_loop_progress.set()

    tick_task = asyncio.create_task(tick())
    started = asyncio.get_running_loop().time()
    with pytest.raises(GatewayExecutionTimeout, match="finalization timeout"):
        await manager._build_session_result(managed)
    elapsed = asyncio.get_running_loop().time() - started
    await tick_task

    assert event_loop_progress.is_set()
    assert elapsed < 0.15


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
async def test_handle_run_stages_agent_system_without_prepending_it(tmp_path, monkeypatch):
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

    assert (tmp_path / "AGENTS.md").read_text(encoding="utf-8") == (
        "Prefer repository-local conventions."
    )
    assert harness.instructions == ["Do work."]
