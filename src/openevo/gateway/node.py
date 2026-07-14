"""Gateway-node execution lifecycle for dispatched rollout sessions."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import re
import shlex
import shutil
import stat
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory, mkdtemp
from typing import Any, Callable, cast
from urllib.parse import unquote, urlparse

import httpx

from openevo.config import EvolutionConfig
from openevo.gateway.dispatcher import (
    DispatcherSnapshot,
    ManagedSession,
    SessionDispatcher,
    SessionStage,
)
from openevo.gateway.session import SessionRegistry
from openevo.gateway.session_files import (
    CredentialFileIdentity,
    CredentialRedactor,
    SessionFileSecurityError,
    StagedCodexCredential,
    VerifiedSessionTranscript,
    capture_session_root_identity,
    create_session_log_authority,
    load_staged_codex_subscription_redactor,
    read_verified_session_transcript,
    redact_core_capture_tree,
    remove_credential_tree,
    remove_session_tree,
    stage_codex_subscription_auth,
    write_verified_session_log,
)
from openevo.gateway.storage import SessionStore
from openevo.harness.capture import transcript_capture_enabled
from openevo.harness.base import BaseHarness
from openevo.harness.factory import create_harness
from openevo.harness.models import AgentRunResult
from openevo.rollout.models import (
    NodeHeartbeatRequest,
    NodeRegistrationRequest,
    NodeStageMetrics,
    SessionDispatchRequest,
    SessionResult,
    SessionStatus,
)
from openevo.rollout.timer import StageTimer
from openevo.runtime.base import (
    BaseRuntime,
    RuntimePathSecurityError,
    validate_session_bind_path,
)
from openevo.runtime.factory import create_runtime
from openevo.runtime.docker import DockerRuntime
from openevo.runtime.managed import (
    MANAGED_SUBSCRIPTION_ENV,
    ManagedCredentialMount,
    reject_managed_subscription_env,
    require_managed_runtime_binding,
    require_managed_subscription_runtime,
)
from openevo.runtime.models import ExecInput, RuntimeSpec
from openevo.trajectory.models import (
    CompletionSession,
    EvalResult,
    EvaluatorSpec,
    StrategySpec,
    Trace,
    Trajectory,
)
from openevo.trajectory.builder.agent_transcript import AgentTranscriptBuilder
from openevo.trajectory.registry import StrategyRegistry
from openevo.evolution.agent_system import normalize_agent_system_target_path
from openevo.evolution.client import EvolutionClient

logger = logging.getLogger(__name__)

_SAFE_SKILL_NAME_RE = re.compile(r"[^A-Za-z0-9_.-]+")
_SUBSCRIPTION_AUTH_MODES = {"subscription", "chatgpt_subscription"}
_FINALIZATION_BUDGET_SECONDS = 10.0
_SUBSCRIPTION_STOP_ATTEMPTS = 3
_SUBSCRIPTION_STOP_RETRY_DELAY_SECONDS = 0.1
_CLEANUP_RETRY_INTERVAL_SECONDS = 1.0
_CLEANUP_JOURNAL_MAX_BYTES = 16 * 1024 * 1024
_CLEANUP_JOURNAL_MAX_ROWS = 4096
_CLEANUP_JOURNAL_MAX_FILENAME_BYTES = 128
_CLEANUP_JOURNAL_MAX_METADATA_BYTES = 1024 * 1024
_CLEANUP_JOURNAL_MAX_TOTAL_BYTES = 64 * 1024 * 1024
_CLEANUP_JOURNAL_ROOT_MARKER_MAX_BYTES = 64 * 1024
_CLEANUP_JOURNAL_RECORD_RE = re.compile(r"[0-9a-f]{64}\.json")
_CLEANUP_JOURNAL_PENDING_RE = re.compile(r"[0-9a-f]{64}\.pending")
_CLEANUP_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
_RECOVERY_PHASE_RUNTIME_ACTIVE = "runtime_active"
_RECOVERY_PHASE_TERMINAL_FINALIZATION = "terminal_finalization"
_RECOVERY_PHASE_TERMINAL_DELIVERY = "terminal_delivery"
_RECOVERY_PHASES = {
    _RECOVERY_PHASE_RUNTIME_ACTIVE,
    _RECOVERY_PHASE_TERMINAL_FINALIZATION,
    _RECOVERY_PHASE_TERMINAL_DELIVERY,
}
_UNSET = object()


class GatewayExecutionTimeout(TimeoutError):
    """Raised when a session exhausts its shared gateway execution budget."""


@dataclass(frozen=True, slots=True)
class SubscriptionFinalizationState:
    """Private redacted authority needed to resume terminal publication."""

    request: SessionDispatchRequest
    agent_result: AgentRunResult | None
    final_result: SessionResult | None
    pending_status: SessionStatus | None
    pending_error: str | None
    cancel_requested: bool
    timer_marks: dict[str, float]


@dataclass(frozen=True, slots=True)
class EvolutionExportAuthority:
    """Persisted destination and behavior identity for one required export."""

    backend_url: str
    timeout_seconds: float
    fail_open: bool
    identity_digest: str


@dataclass(frozen=True, slots=True)
class TerminalDeliveryState:
    """Monotonic proof for idempotent export and callback delivery."""

    result: SessionResult
    result_digest: str
    callback_url: str | None
    export_required: bool
    export_authority: EvolutionExportAuthority | None
    export_succeeded: bool
    callback_required: bool
    callback_succeeded: bool

    @property
    def complete(self) -> bool:
        return self.export_succeeded and self.callback_succeeded


@dataclass(slots=True)
class CleanupRetryOwnership:
    """Reachable ownership retained until runtime absence and root cleanup succeed."""

    session_id: str
    session_dir: Path
    session_root_identity: tuple[int, int, int] | None
    log_authority_dir: Path | None
    log_authority_identity: tuple[int, int, int] | None
    credential_dir: Path | None
    credential_root_identity: tuple[int, int, int] | None
    credential_auth_identity: CredentialFileIdentity | None
    runtime_id: str | None
    container_id: str | None
    eval_runtime_id: str | None
    eval_container_id: str | None
    runtime: BaseRuntime | None
    phase: str | None
    eval_runtime: BaseRuntime | None = None
    managed: ManagedSession | None = None
    finalize_subscription: bool = False
    finalization_state: SubscriptionFinalizationState | None = None
    delivery_state: TerminalDeliveryState | None = None


@dataclass(slots=True)
class _CleanupJournalAuthority:
    """Held no-follow authority for one immutable cleanup journal root."""

    path: Path
    ancestor_fds: list[int]
    ancestor_identities: tuple[tuple[int, int, int, int], ...]
    root_fd: int
    root_identity: tuple[int, int, int, int]

    def close(self) -> None:
        if self.root_fd >= 0:
            os.close(self.root_fd)
            self.root_fd = -1
        while self.ancestor_fds:
            os.close(self.ancestor_fds.pop())


async def write_evolution_context_files(
    *,
    runtime: BaseRuntime,
    context: dict,
    host_dir: Path,
    target_dir: str,
) -> dict[str, str]:
    del host_dir  # Staging must stay outside the agent-writable session bind.
    with TemporaryDirectory(prefix="openevo-evolution-upload-") as temporary:
        evolution_dir = Path(temporary)
        skills_dir = evolution_dir / "skills"
        agent_system_targets_dir = evolution_dir / "agent_system_targets"
        skills_dir.mkdir(mode=0o700)
        agent_system_targets_dir.mkdir(mode=0o700)
        context_path = evolution_dir / "context.json"
        memory_path = evolution_dir / "memory.md"
        agent_system_path = evolution_dir / "agent_system.md"
        adapters_path = evolution_dir / "adapters.json"

        _stage_evolution_skill_bundles(context, skills_dir)
        agent_system_text = _agent_system_rendered_text(context)
        agent_system_env: dict[str, str] = {}
        if agent_system_text:
            agent_system_env["OPENEVO_AGENT_SYSTEM_FILE"] = f"{target_dir}/agent_system.md"
            agent_system_env.update(
                await _stage_evolution_agent_system(
                    runtime=runtime,
                    context=context,
                    targets_dir=agent_system_targets_dir,
                    rendered=agent_system_text,
                )
            )

        context_path.write_text(
            json.dumps(context, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        memory_path.write_text(
            str((context.get("memory") or {}).get("rendered_text") or ""),
            encoding="utf-8",
        )
        agent_system_path.write_text(
            agent_system_text,
            encoding="utf-8",
        )
        adapters_path.write_text(
            json.dumps(
                context.get("adapter_merge_spec") or {},
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )

        await runtime.upload_file(str(context_path), f"{target_dir}/context.json")
        await runtime.upload_file(str(memory_path), f"{target_dir}/memory.md")
        if agent_system_text:
            await runtime.upload_file(
                str(agent_system_path),
                f"{target_dir}/agent_system.md",
            )
        await runtime.upload_file(str(adapters_path), f"{target_dir}/adapters.json")
        await runtime.upload_dir(str(skills_dir), f"{target_dir}/skills")

    env = {
        "OPENEVO_EVOLUTION_CONTEXT": f"{target_dir}/context.json",
        "OPENEVO_MEMORY_FILE": f"{target_dir}/memory.md",
        "OPENEVO_SKILLS_DIR": f"{target_dir}/skills",
        "OPENEVO_ADAPTER_MERGE_SPEC": f"{target_dir}/adapters.json",
    }
    env.update(agent_system_env)
    return env


def _stage_evolution_skill_bundles(context: dict, skills_dir: Path) -> None:
    skills = context.get("skills") or []
    if not isinstance(skills, list):
        return
    for index, skill in enumerate(skills):
        if not isinstance(skill, dict):
            continue
        try:
            source = _artifact_file_uri_path(skill.get("uri"))
            if source is None:
                raise ValueError("skill artifact URI is not a file:// URI")
            if not source.exists():
                raise FileNotFoundError(f"skill artifact path does not exist: {source}")
            dest = skills_dir / _safe_skill_dir_name(skill, index)
            if source.is_dir():
                shutil.copytree(source, dest)
            else:
                dest.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, dest / source.name)
            (dest / "artifact.json").write_text(
                json.dumps(skill, indent=2, sort_keys=True),
                encoding="utf-8",
            )
        except Exception as exc:
            warning = (
                "Skipped evolution skill artifact "
                f"{skill.get('artifact_id') or skill.get('name') or index}: {exc}"
            )
            logger.warning(warning)
            context.setdefault("warnings", []).append(warning)


async def _stage_evolution_agent_system(
    *,
    runtime: BaseRuntime,
    context: dict,
    targets_dir: Path,
    rendered: str,
) -> dict[str, str]:
    agent_system = context.get("agent_system") or {}
    if not isinstance(agent_system, dict):
        return {}

    target_specs = _agent_system_target_specs(agent_system, rendered)
    target_root = _agent_system_target_root(runtime)
    remote_targets: list[str] = []
    agents_md_target: str | None = None
    for index, spec in enumerate(target_specs):
        try:
            target_path = PurePosixPath(
                normalize_agent_system_target_path(spec.get("target_path"))
            )
        except ValueError as exc:
            warning = f"Skipped evolution agent system target {index}: {exc}"
            logger.warning(warning)
            context.setdefault("warnings", []).append(warning)
            continue

        target_text = str(spec.get("rendered_text") or rendered)
        local_target = targets_dir / Path(*PurePosixPath(target_path).parts)
        local_target.parent.mkdir(parents=True, exist_ok=True)
        local_target.write_text(target_text, encoding="utf-8")
        remote_target = (target_root / target_path).as_posix()
        remote_parent = PurePosixPath(remote_target).parent.as_posix()
        if remote_parent not in ("", "."):
            await runtime.exec(f"mkdir -p {shlex.quote(remote_parent)}")
        await runtime.upload_file(str(local_target), remote_target)
        remote_targets.append(remote_target)
        if target_path.name == "AGENTS.md" and agents_md_target is None:
            agents_md_target = remote_target

    if not remote_targets:
        return {}
    env = {
        "OPENEVO_AGENT_SYSTEM_TARGET": remote_targets[0],
        "OPENEVO_AGENT_SYSTEM_TARGETS": json.dumps(remote_targets),
    }
    if agents_md_target is not None:
        env["OPENEVO_AGENTS_MD"] = agents_md_target
    return env


def _agent_system_rendered_text(context: dict) -> str:
    agent_system = context.get("agent_system") or {}
    if not isinstance(agent_system, dict):
        return ""
    return str(agent_system.get("rendered_text") or "")


def _agent_system_target_root(runtime: BaseRuntime) -> PurePosixPath:
    workdir = getattr(getattr(runtime, "spec", None), "workdir", None)
    if isinstance(workdir, str) and workdir.strip():
        return PurePosixPath(workdir.strip())
    runtime_session_dir = getattr(runtime, "runtime_session_dir", "/openevo/session")
    return PurePosixPath(str(runtime_session_dir))


def _agent_system_target_specs(
    agent_system: dict[str, Any],
    rendered: str,
) -> list[dict[str, Any]]:
    targets = agent_system.get("targets")
    if isinstance(targets, list) and targets:
        grouped: dict[str, list[str]] = {}
        for target in targets:
            if not isinstance(target, dict):
                continue
            target_path = (
                target.get("target_path") or agent_system.get("target_path") or "AGENTS.md"
            )
            grouped.setdefault(str(target_path), []).append(
                str(target.get("rendered_text") or rendered)
            )
        if grouped:
            return [
                {"target_path": target_path, "rendered_text": "\n\n".join(parts)}
                for target_path, parts in grouped.items()
            ]
    return [
        {
            "target_path": agent_system.get("target_path") or "AGENTS.md",
            "rendered_text": rendered,
        }
    ]


def _artifact_file_uri_path(uri: Any) -> Path | None:
    if not isinstance(uri, str) or not uri:
        return None
    parsed = urlparse(uri)
    if parsed.scheme != "file":
        return None
    if parsed.netloc and parsed.netloc not in ("", "localhost"):
        raise ValueError(f"unsupported file URI host for evolution artifact: {uri}")
    return Path(unquote(parsed.path))


def _safe_skill_dir_name(skill: dict, index: int) -> str:
    raw = skill.get("artifact_id") or skill.get("name") or f"skill-{index}"
    normalized = _SAFE_SKILL_NAME_RE.sub("-", str(raw)).strip(".-")
    return normalized or f"skill-{index}"


def _instruction_with_evolution_context(instruction: str, context: dict) -> str:
    agent_system = str((context.get("agent_system") or {}).get("rendered_text") or "").strip()
    memory = str((context.get("memory") or {}).get("rendered_text") or "").strip()
    if not agent_system and not memory:
        return instruction
    parts: list[str] = []
    if agent_system:
        parts.append(
            f"Use the following evolved agent system instructions for this task:\n{agent_system}"
        )
    if memory:
        parts.append(f"Use the following long-term memory for this task:\n{memory}")
    parts.append(f"Task:\n{instruction}")
    return "\n\n".join(parts)


def _existing_evolution_metadata(metadata: dict) -> dict:
    existing = metadata.get("evolution")
    if isinstance(existing, dict):
        return dict(existing)
    return {}


def build_evolution_session_event(result: SessionResult) -> dict:
    metadata = dict(result.metadata or {})
    trajectory_metadata = dict(result.trajectory.metadata or {})
    return {
        "source": "openevo",
        "event_type": "openevo.session_completed",
        "source_event_id": f"session:{result.session_id}",
        "task_id": result.task_id,
        "session_id": result.session_id,
        "policy_version": _metadata_value(
            metadata,
            trajectory_metadata,
            "policy_version",
        ),
        "rollout_step": _metadata_value(
            metadata,
            trajectory_metadata,
            "rollout_step",
        ),
        "agent": metadata.get("agent") or {},
        "base_model": trajectory_metadata.get("model_used"),
        "reward": _mean_trace_reward(result.trajectory.traces),
        "status": str(result.status),
        "payload": {"session_result": result.model_dump(mode="json")},
    }


def _metadata_value(
    metadata: dict[str, Any],
    fallback_metadata: dict[str, Any],
    key: str,
) -> Any:
    if key in metadata and metadata[key] is not None:
        return metadata[key]
    return fallback_metadata.get(key)


def _mean_trace_reward(traces: list[Trace]) -> float | None:
    rewards = [trace.reward for trace in traces if trace.reward is not None]
    if not rewards:
        return None
    return float(sum(rewards) / len(rewards))


def _completion_session_with_agent_metadata(
    completion_session: CompletionSession,
    request: SessionDispatchRequest,
    agent_result: AgentRunResult | None,
) -> CompletionSession:
    metadata = {
        **dict(completion_session.metadata),
        **dict(request.metadata),
        "agent_harness": request.agent.harness,
        "agent_model_name": request.agent.model_name,
        "agent_instruction": request.instruction,
    }
    if agent_result is not None:
        metadata["agent_result"] = agent_result.model_dump(mode="json")
    return completion_session.model_copy(update={"metadata": metadata})


def _transcript_step_index(agent_result: AgentRunResult) -> int | None:
    last_step = agent_result.metadata.get("last_step", 0)
    try:
        step_index = int(last_step)
    except (TypeError, ValueError):
        return None
    return step_index if step_index >= 0 else None


def _is_codex_subscription_agent(agent) -> bool:
    return agent.harness == "codex" and _is_subscription_agent(agent)


def _is_subscription_agent(agent) -> bool:
    auth_mode = agent.settings.get("auth_mode")
    return isinstance(auth_mode, str) and auth_mode in _SUBSCRIPTION_AUTH_MODES


class GatewayNodeManager:
    """Run the INIT/READY/RUN/POST_RUN lifecycle on one gateway node."""

    def __init__(
        self,
        *,
        node_id: str,
        gateway_url: str,
        max_init_workers: int,
        max_run_workers: int,
        max_postrun_workers: int,
        storage: SessionStore,
        session_registry: SessionRegistry,
        builders: StrategyRegistry,
        evaluators: StrategyRegistry,
        default_runtime: RuntimeSpec | None = None,
        session_base_dir: str | None = None,
        rollout_server_url: str | None = None,
        heartbeat_interval_seconds: int = 30,
        model_served: str | None = None,
        evolution: EvolutionConfig | None = None,
        evolution_client: EvolutionClient | None = None,
        internal_headers: dict[str, str] | None = None,
    ) -> None:
        self.node_id = node_id
        self.gateway_url = gateway_url.rstrip("/")
        self.max_init_workers = max_init_workers
        self.max_run_workers = max_run_workers
        self.max_postrun_workers = max_postrun_workers
        self.storage = storage
        self.session_registry = session_registry
        self.builders = builders
        self.evaluators = evaluators
        self.default_runtime = default_runtime
        self._session_base_dir = session_base_dir
        self.model_served = model_served
        self.evolution = evolution
        self.evolution_client = evolution_client
        self._internal_headers = dict(internal_headers or {})
        self._client = httpx.AsyncClient(
            timeout=30.0,
            headers=self._internal_headers,
            trust_env=False,
        )
        self._dispatcher = SessionDispatcher(
            max_init_workers=max_init_workers,
            max_run_workers=max_run_workers,
            max_postrun_workers=max_postrun_workers,
        )
        self._dispatcher.on_init = self._handle_init
        self._dispatcher.on_run = self._handle_run
        self._dispatcher.on_postrun = self._handle_postrun
        self._dispatcher.on_stage_change = self._handle_dispatcher_stage_change

        self._rollout_server_url = rollout_server_url.rstrip("/") if rollout_server_url else None
        self._heartbeat_interval_seconds = heartbeat_interval_seconds
        self._control_client: httpx.AsyncClient | None = None
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._rollout_registered = False
        self._cleanup_retry_task: asyncio.Task[None] | None = None
        self._cleanup_reconcile_lock = asyncio.Lock()
        self._cleanup_retries: dict[str, CleanupRetryOwnership] = {}
        cleanup_base = Path(session_base_dir).absolute() if session_base_dir else Path("/tmp")
        node_key = hashlib.sha256(node_id.encode("utf-8")).hexdigest()[:24]
        self._cleanup_journal_dir = cleanup_base / ".openevo-gateway-cleanup" / node_key
        self._docker_ownership_root = cleanup_base / ".openevo-gateway-docker-ownership" / node_key
        self._log_authority_root = cleanup_base / ".openevo-gateway-log-authority" / node_key

    async def start(self) -> None:
        await DockerRuntime.recover_ownership_root(self._docker_ownership_root)
        self._load_cleanup_retries()
        await self._reconcile_cleanup_retries()
        await self._dispatcher.start()
        self._cleanup_retry_task = asyncio.create_task(self._cleanup_retry_loop())
        if self._rollout_server_url is not None:
            self._control_client = httpx.AsyncClient(
                base_url=self._rollout_server_url,
                timeout=15.0,
                headers=self._internal_headers,
                trust_env=False,
            )
            await self._register_with_rollout_server()
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

    async def close(self) -> None:
        if self._cleanup_retry_task is not None:
            self._cleanup_retry_task.cancel()
            await asyncio.gather(self._cleanup_retry_task, return_exceptions=True)
            self._cleanup_retry_task = None
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            await asyncio.gather(self._heartbeat_task, return_exceptions=True)
            self._heartbeat_task = None
        if self._control_client is not None:
            await self._control_client.aclose()
            self._control_client = None
        shutdown_sessions = await self._dispatcher.stop()
        for managed in shutdown_sessions:
            eval_runtime = await self._drain_eval_prewarm_task(managed)
            self._register_cleanup_retry(
                managed,
                eval_runtime=eval_runtime or managed.eval_runtime,
                finalize_subscription=_is_codex_subscription_agent(managed.request.agent),
            )
        await self._reconcile_cleanup_retries()
        if self.evolution_client is not None:
            await self.evolution_client.close()
            self.evolution_client = None
        await self._client.aclose()

    async def _register_with_rollout_server(self) -> None:
        if self._control_client is None:
            return
        try:
            response = await self._control_client.post(
                "/nodes/register",
                json=NodeRegistrationRequest(
                    node_id=self.node_id,
                    gateway_url=self.gateway_url,
                    max_init_workers=self.max_init_workers,
                    max_run_workers=self.max_run_workers,
                    max_postrun_workers=self.max_postrun_workers,
                    heartbeat_interval_seconds=self._heartbeat_interval_seconds,
                ).model_dump(mode="json"),
            )
            response.raise_for_status()
            payload = response.json()
            self._rollout_registered = bool(
                payload.get("node_id") == self.node_id
                and payload.get("gateway_url") == self.gateway_url
                and payload.get("healthy") is True
                and payload.get("draining") is False
            )
        except Exception:
            self._rollout_registered = False
            logger.warning("Node registration failed", exc_info=True)

    async def _heartbeat_loop(self) -> None:
        assert self._control_client is not None
        while True:
            await asyncio.sleep(self._heartbeat_interval_seconds)
            try:
                metrics = await self.stage_metrics()
                response = await self._control_client.post(
                    f"/nodes/{self.node_id}/heartbeat",
                    json=NodeHeartbeatRequest(metrics=metrics).model_dump(mode="json"),
                )
                if response.status_code == 404:
                    await self._register_with_rollout_server()
                    continue
                response.raise_for_status()
                self._rollout_registered = True
            except asyncio.CancelledError:
                raise
            except Exception:
                self._rollout_registered = False
                logger.warning("Node heartbeat failed", exc_info=True)

    async def internal_rollout_readiness(self) -> tuple[bool, str]:
        if self._control_client is None or not self._rollout_registered:
            return False, "gateway is not registered with rollout"
        try:
            response = await self._control_client.get("/health")
            response.raise_for_status()
            payload = response.json()
            registration = payload.get("gateway_registration")
            if not isinstance(registration, dict):
                return False, "rollout health lacks gateway registration"
            if (
                registration.get("node_id") != self.node_id
                or registration.get("gateway_url") != self.gateway_url
                or registration.get("registered") is not True
                or registration.get("schedulable") is not True
            ):
                return False, "gateway is not schedulable in rollout"
            return True, "gateway is registered and schedulable"
        except Exception:
            return False, "gateway could not authenticate rollout health"

    async def dispatch(self, request: SessionDispatchRequest) -> None:
        session_id = request.session_id
        if self.session_registry.get(session_id) is not None:
            raise ValueError(
                f"session {session_id} already exists; rollout session IDs are single-use"
            )

        session_dir: Path | None = None
        session_root_identity: tuple[int, int, int] | None = None
        log_authority_dir: Path | None = None
        log_authority_identity: tuple[int, int, int] | None = None
        try:
            info = self.session_registry.register(
                session_id,
                task_id=request.task_id,
                registered=True,
                status=SessionStatus.REGISTERED,
                metadata=dict(request.metadata),
            )
            self.storage.ensure_session(
                info.session_id,
                model_requested=None,
                model_used=None,
                api_type=None,
                task_id=info.task_id,
                created_at=info.created_at.isoformat(),
                metadata=dict(request.metadata),
            )

            timer = StageTimer()
            timer.mark("dispatch", "started")
            session_dir = Path(
                mkdtemp(prefix=f"session-{session_id[:8]}-", dir=self._session_base_dir)
            )
            artifacts_dir = session_dir / "artifacts"
            artifacts_dir.mkdir()
            session_root_identity = capture_session_root_identity(session_dir)
            log_authority_dir, log_authority_identity = create_session_log_authority(
                self._log_authority_root,
                session_id,
            )
            managed = ManagedSession(
                request=request,
                timer=timer,
                session_dir=session_dir,
                artifacts_dir=artifacts_dir,
                session_root_identity=session_root_identity,
                log_authority_dir=log_authority_dir,
                log_authority_identity=log_authority_identity,
            )
            self._persist_cleanup_ownership(self._cleanup_ownership_for(managed))
            await self._dispatcher.enqueue(managed)
        except Exception:
            self.storage.delete_session(session_id)
            self.session_registry.remove(session_id)
            if session_dir is not None:
                await self._remove_session_dir_best_effort(
                    session_dir,
                    session_id,
                    session_root_identity,
                )
            if log_authority_dir is not None:
                await self._remove_log_authority_best_effort(
                    log_authority_dir,
                    session_id,
                    log_authority_identity,
                )
            self._clear_cleanup_ownership(session_id)
            raise

    async def cancel(self, session_id: str) -> bool:
        return await self._dispatcher.cancel(session_id)

    async def active_sessions(self) -> int:
        return await self._dispatcher.active_count()

    async def stage_metrics(self) -> NodeStageMetrics:
        snapshot = await self._dispatcher.snapshot()
        return self._snapshot_to_metrics(snapshot)

    def _handle_dispatcher_stage_change(self, managed: ManagedSession) -> None:
        status = {
            SessionStage.INIT: SessionStatus.INITIALIZING,
            SessionStage.READY: SessionStatus.READY,
            SessionStage.RUNNING: SessionStatus.RUNNING,
            SessionStage.POSTRUN: SessionStatus.POST_RUN,
        }.get(managed.stage)
        if status is not None:
            self.session_registry.set_status(managed.request.session_id, status)

    # ------------------------------------------------------------------
    # INIT stage
    # ------------------------------------------------------------------

    async def _handle_init(self, managed: ManagedSession) -> None:
        request = managed.request
        self._start_execution_deadline(managed)
        managed.timer.mark("init", "started")
        try:
            runtime_spec = self._resolve_runtime_spec(request)
            self._validate_subscription_admission(request, runtime_spec, managed.session_dir)
            self._validate_runtime_admission(runtime_spec, managed)
            if _is_codex_subscription_agent(request.agent):
                runtime_spec = runtime_spec.model_copy(
                    update={
                        "env": {
                            **runtime_spec.env,
                            **MANAGED_SUBSCRIPTION_ENV,
                        }
                    }
                )
            if _is_codex_subscription_agent(request.agent):
                credential_dir = Path(
                    mkdtemp(
                        prefix=f"credentials-{request.session_id[:8]}-",
                        dir=managed.session_dir.parent,
                    )
                )
                managed.credential_dir = credential_dir
                managed.credential_root_identity = capture_session_root_identity(credential_dir)
                self._persist_cleanup_ownership(self._cleanup_ownership_for(managed))

                def persist_auth_identity(auth_identity: CredentialFileIdentity) -> None:
                    managed.credential_auth_identity = auth_identity
                    self._persist_cleanup_ownership(self._cleanup_ownership_for(managed))

                staged_credential = self._stage_codex_subscription_auth(
                    request,
                    managed.credential_dir,
                    managed.credential_root_identity,
                    on_identity=persist_auth_identity,
                )
                managed.credential_redactor = staged_credential.redactor
                managed.credential_auth_identity = staged_credential.auth_identity
                managed.credential_mount = ManagedCredentialMount(
                    root=managed.credential_dir,
                    root_identity=managed.credential_root_identity,
                    auth_identity=staged_credential.auth_identity,
                )
                self._persist_cleanup_ownership(self._cleanup_ownership_for(managed))
            if managed.credential_dir is None:
                runtime = create_runtime(
                    runtime_spec,
                    request.session_id,
                    managed.session_dir,
                    docker_ownership_root=self._docker_ownership_root,
                )
            else:
                runtime = create_runtime(
                    runtime_spec,
                    request.session_id,
                    managed.session_dir,
                    credential_mount=managed.credential_mount,
                    docker_ownership_root=self._docker_ownership_root,
                )
            managed.runtime = runtime
            await self._await_with_budget(runtime.start(), managed)
            self._persist_cleanup_ownership(self._cleanup_ownership_for(managed))
            # Run ordered prepare actions
            await self._run_runtime_prepare(runtime, runtime_spec, request, managed)
            if managed.credential_redactor is not None:
                self._redact_core_capture_authority(managed)
        except GatewayExecutionTimeout as exc:
            self._set_terminal_failure(
                managed,
                SessionStatus.TIMEOUT,
                str(exc),
            )
        except Exception as exc:
            if managed.cancel_requested:
                logger.info("Initialization cancelled for session %s", request.session_id)
            elif _is_codex_subscription_agent(request.agent):
                self._log_credential_safe_exception(
                    managed,
                    "Initialization failed",
                    exc,
                )
            else:
                logger.exception("Initialization failed for session %s", request.session_id)
            self._set_terminal_failure(
                managed,
                SessionStatus.ERROR,
                f"runtime initialization failed: {exc}",
            )
        finally:
            managed.timer.mark("init", "finished")

    def _resolve_runtime_spec(self, request: SessionDispatchRequest) -> RuntimeSpec:
        spec = request.runtime or self.default_runtime
        if spec is None:
            raise RuntimeError(
                "no runtime configured: request has no runtime and gateway "
                "node has no default_runtime"
            )
        return spec

    @staticmethod
    def _validate_runtime_admission(
        runtime_spec: RuntimeSpec,
        managed: ManagedSession,
    ) -> None:
        try:
            is_managed = require_managed_runtime_binding(
                profile=runtime_spec.profile,
                image=runtime_spec.image,
                backend=runtime_spec.backend,
                container_user=runtime_spec.container_user,
            )
            if is_managed and (runtime_spec.import_path is not None or runtime_spec.kwargs):
                raise ValueError(
                    "Core-managed runtime profiles forbid custom runtime loaders and options"
                )
        except ValueError as exc:
            raise RuntimeError(f"runtime admission failed: {exc}") from exc

        try:
            root_state = managed.session_dir.stat(follow_symlinks=False)
            expected = managed.session_root_identity
            if (
                expected is not None
                and (
                    root_state.st_dev,
                    root_state.st_ino,
                    root_state.st_uid,
                )
                != expected
            ):
                raise RuntimePathSecurityError("session root identity changed")
            full_identity = (
                root_state.st_dev,
                root_state.st_ino,
                root_state.st_mode,
                root_state.st_uid,
            )
            for action in [
                *runtime_spec.prepare,
                *(runtime_spec.eval_prepare or []),
            ]:
                if action.type not in {"upload_file", "upload_dir"}:
                    continue
                if action.target is None:
                    raise RuntimePathSecurityError("prepare target is missing")
                resolved = validate_session_bind_path(
                    managed.session_dir,
                    action.target,
                    expected_identity=full_identity,
                )
                if resolved is None:
                    raise RuntimePathSecurityError(
                        "prepare target is outside the session authority"
                    )
        except (OSError, RuntimePathSecurityError) as exc:
            raise RuntimeError(f"runtime prepare target admission failed: {exc}") from exc

    def _stage_codex_subscription_auth(
        self,
        request: SessionDispatchRequest,
        credential_dir: Path,
        credential_root_identity: tuple[int, int, int] | None = None,
        *,
        on_identity: Callable[[CredentialFileIdentity], None] | None = None,
    ) -> StagedCodexCredential:
        if not _is_codex_subscription_agent(request.agent):
            raise RuntimeError("credential staging requires a Codex subscription agent")

        identity = credential_root_identity or capture_session_root_identity(credential_dir)
        try:
            return stage_codex_subscription_auth(
                source=Path.home() / ".codex" / "auth.json",
                session_dir=credential_dir,
                session_identity=identity,
                target_home_parts=(),
                on_identity=on_identity,
            )
        except SessionFileSecurityError as exc:
            raise RuntimeError(str(exc)) from exc

    @staticmethod
    def _validate_subscription_admission(
        request: SessionDispatchRequest,
        runtime_spec: RuntimeSpec,
        session_dir: Path,
    ) -> None:
        if not _is_subscription_agent(request.agent):
            return
        if request.agent.harness != "codex":
            raise RuntimeError(
                "managed subscription execution currently requires the Codex harness"
            )
        if request.agent.settings.get("capture_mode") != "transcript":
            raise RuntimeError("subscription execution requires capture_mode='transcript'")
        try:
            reject_managed_subscription_env(request.agent.env, owner="agent")
            reject_managed_subscription_env(
                runtime_spec.env,
                owner="runtime",
                allow_exact=True,
            )
            for action in [
                *runtime_spec.prepare,
                *(runtime_spec.eval_prepare or []),
            ]:
                reject_managed_subscription_env(
                    action.env,
                    owner="runtime action",
                )
        except ValueError as exc:
            raise RuntimeError(str(exc)) from exc
        try:
            require_managed_subscription_runtime(
                profile=runtime_spec.profile,
                image=runtime_spec.image,
                backend=runtime_spec.backend,
                container_user=runtime_spec.container_user,
            )
        except ValueError as exc:
            raise RuntimeError(str(exc)) from exc
        if runtime_spec.import_path is not None or runtime_spec.kwargs:
            raise RuntimeError("subscription execution forbids custom runtime loaders and options")

        auth_path = (Path.home() / ".codex" / "auth.json").resolve()
        protected_paths = (auth_path, session_dir.resolve())
        for action in runtime_spec.prepare:
            if action.type not in {"upload_file", "upload_dir"} or not action.source:
                continue
            source = Path(action.source).expanduser().resolve()
            for protected in protected_paths:
                if source == protected or (
                    action.type == "upload_dir" and protected.is_relative_to(source)
                ):
                    raise RuntimeError(
                        "subscription workspace sync cannot include Core credential "
                        "or session roots"
                    )

    @staticmethod
    def _redact_core_capture_authority(managed: ManagedSession) -> None:
        redactor = managed.credential_redactor
        if redactor is None:
            return
        root = managed.log_authority_dir
        identity = managed.log_authority_identity
        if root is not None and identity is not None:
            redact_core_capture_tree(root, identity, redactor)

    def _ensure_log_authority(self, managed: ManagedSession) -> Path:
        if managed.log_authority_dir is not None and managed.log_authority_identity is not None:
            return managed.log_authority_dir
        if managed.log_authority_dir is not None or managed.log_authority_identity is not None:
            raise SessionFileSecurityError("session log authority is incomplete")
        authority_root = getattr(self, "_log_authority_root", None)
        if authority_root is None:
            authority_root = managed.session_dir.parent / ".openevo-gateway-log-authority-direct"
        (
            managed.log_authority_dir,
            managed.log_authority_identity,
        ) = create_session_log_authority(
            authority_root,
            managed.session_id,
        )
        return managed.log_authority_dir

    async def _run_runtime_prepare(
        self,
        runtime: BaseRuntime,
        spec: RuntimeSpec,
        request: SessionDispatchRequest,
        managed: ManagedSession,
        *,
        actions: list | None = None,
        log_prefix: str = "prepare",
    ) -> None:
        """Execute an ordered prepare action list (``spec.prepare`` by default)."""
        steps = actions if actions is not None else spec.prepare
        base_env = self._runtime_env(request, managed, runtime_override=runtime)
        for i, action in enumerate(steps):
            if managed.cancel_requested:
                return
            if action.type == "upload_file":
                await runtime.upload_file(action.source, action.target)
            elif action.type == "upload_dir":
                await runtime.upload_dir(action.source, action.target)
            elif action.type == "exec":
                merged_env = {**base_env, **(action.env or {})}
                effective_cwd = action.cwd or runtime.runtime_session_dir
                result = await runtime.exec(
                    action.command,
                    cwd=effective_cwd,
                    env=merged_env,
                    timeout_sec=self._remaining_budget(managed),
                )
                await self._write_exec_log(
                    managed,
                    ("logs",),
                    f"{log_prefix}.{i:02d}",
                    result.stdout,
                    result.stderr,
                )
                if result.return_code == -1:
                    raise RuntimeError(f"{log_prefix} action {i} timed out")
                if result.return_code != 0:
                    raise RuntimeError(
                        f"{log_prefix} action {i} failed with exit code {result.return_code}"
                    )

    # ------------------------------------------------------------------
    # RUN stage
    # ------------------------------------------------------------------

    async def _handle_run(self, managed: ManagedSession) -> None:
        request = managed.request
        if managed.has_terminal_outcome or managed.cancel_requested:
            return
        managed.timer.mark("run", "started")

        harness: BaseHarness | None = None
        try:
            runtime = managed.runtime
            if runtime is None:
                raise RuntimeError("runtime is required for execution")

            self._start_eval_prewarm(managed)
            harness = self._resolve_agent_harness(request)

            evolution_env = await self._resolve_and_inject_evolution_context(
                managed,
                harness,
            )
            if evolution_env:
                harness.env.update(evolution_env)
                request.agent.env.update(evolution_env)

            # Setup
            await self._await_with_budget(harness.setup(runtime), managed)

            # Run
            steps = harness.run_steps(request.instruction)
            env = self._runtime_env(request, managed, include_agent_env=True)
            agent_result = await self._run_exec_inputs(runtime, steps, env, managed)

            # Postprocess always runs so harnesses can collect artifacts from
            # failed or timed-out agent runs before post-run evaluation.
            await self._await_with_budget(harness.postprocess(runtime, agent_result), managed)
            self._redact_core_capture_authority(managed)

        except GatewayExecutionTimeout as exc:
            # Don't set final_result — let _handle_postrun build a partial
            # trajectory from the completions captured so far.
            if managed.agent_result is None:
                self._record_terminal_agent_result(
                    managed,
                    AgentRunResult(
                        status="timeout",
                        return_code=-1,
                        error=str(exc),
                    ),
                )
            else:
                self._record_terminal_agent_result(
                    managed,
                    managed.agent_result.model_copy(
                        update={
                            "status": "timeout",
                            "return_code": -1,
                            "error": managed.agent_result.error or str(exc),
                        }
                    ),
                )
        except Exception as exc:
            if managed.cancel_requested:
                logger.info("Agent execution cancelled for session %s", request.session_id)
            elif _is_codex_subscription_agent(request.agent):
                self._log_credential_safe_exception(
                    managed,
                    "Agent setup, execution, or postprocess failed",
                    exc,
                )
                self._set_terminal_failure(
                    managed,
                    SessionStatus.ERROR,
                    f"agent execution failed: {exc}",
                )
            else:
                logger.exception("Agent execution failed for session %s", request.session_id)
                self._set_terminal_failure(
                    managed,
                    SessionStatus.ERROR,
                    f"agent execution failed: {exc}",
                )
        finally:
            if harness is not None:
                managed.postrun_steps = harness.postrun_steps()
            managed.timer.mark("run", "finished")

    def _resolve_agent_harness(self, request: SessionDispatchRequest) -> BaseHarness:
        return create_harness(request.agent)

    async def _resolve_and_inject_evolution_context(
        self,
        managed: ManagedSession,
        harness: BaseHarness,
    ) -> dict[str, str]:
        request = managed.request
        if self.evolution is None or not self.evolution.enabled or self.evolution_client is None:
            return {}
        if managed.runtime is None:
            return {}
        payload = {
            "task_id": request.task_id,
            "instruction": request.instruction,
            "agent": request.agent.model_dump(mode="json"),
            "base_model": self.model_served,
            "policy_version": request.metadata.get("policy_version"),
            "rollout_step": request.metadata.get("rollout_step"),
            "metadata": dict(request.metadata),
        }
        try:
            context = await self._await_with_budget(
                self.evolution_client.resolve_context(payload),
                managed,
            )
            env = await self._await_with_budget(
                write_evolution_context_files(
                    runtime=managed.runtime,
                    context=context,
                    host_dir=managed.session_dir,
                    target_dir=self.evolution.context.target_dir,
                ),
                managed,
            )
            request.instruction = _instruction_with_evolution_context(
                request.instruction,
                context,
            )
            adapter_merge_spec = context.get("adapter_merge_spec")
            if isinstance(adapter_merge_spec, dict):
                request.metadata["adapter_merge_spec"] = adapter_merge_spec
            evolution_metadata = {
                **_existing_evolution_metadata(request.metadata),
                "context_id": context.get("context_id"),
                "context_injected": True,
            }
            request.metadata["evolution"] = evolution_metadata
            registry_metadata: dict[str, Any] = {"evolution": evolution_metadata}
            if isinstance(adapter_merge_spec, dict):
                registry_metadata["adapter_merge_spec"] = adapter_merge_spec
            session_registry = getattr(self, "session_registry", None)
            if session_registry is not None:
                session_registry.update_metadata(
                    request.session_id,
                    registry_metadata,
                )
            return env
        except Exception as exc:
            if not self.evolution.context.fail_open:
                raise
            request.metadata["evolution"] = {
                **_existing_evolution_metadata(request.metadata),
                "context_injected": False,
                "error": str(exc),
            }
            self._log_credential_safe_exception(
                managed,
                "Evolution context resolution failed",
                exc,
                level=logging.WARNING,
            )
            return {}

    async def _export_evolution_event(self, result: SessionResult) -> bool:
        authority = self._current_export_authority()
        if authority is None:
            return True
        return await self._export_evolution_event_with_authority(result, authority)

    @staticmethod
    def _export_authority_digest(
        *,
        backend_url: str,
        timeout_seconds: float,
        fail_open: bool,
    ) -> str:
        canonical = json.dumps(
            {
                "backend_url": backend_url,
                "fail_open": fail_open,
                "timeout_seconds": timeout_seconds,
            },
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()

    def _current_export_authority(self) -> EvolutionExportAuthority | None:
        evolution = self.evolution
        if evolution is None or not evolution.enabled or not evolution.event_export.enabled:
            return None
        backend_url = evolution.backend_url.rstrip("/")
        timeout_seconds = float(evolution.event_export.timeout_seconds)
        fail_open = evolution.event_export.fail_open
        return EvolutionExportAuthority(
            backend_url=backend_url,
            timeout_seconds=timeout_seconds,
            fail_open=fail_open,
            identity_digest=self._export_authority_digest(
                backend_url=backend_url,
                timeout_seconds=timeout_seconds,
                fail_open=fail_open,
            ),
        )

    async def _export_evolution_event_with_authority(
        self,
        result: SessionResult,
        authority: EvolutionExportAuthority,
    ) -> bool:
        current = self._current_export_authority()
        if current != authority:
            raise RuntimeError(
                "required evolution event export configuration changed after terminalization"
            )
        if self.evolution_client is None:
            if not authority.fail_open:
                raise RuntimeError("evolution event export client is unavailable")
            logger.warning(
                "Evolution event export client is unavailable for session %s",
                result.session_id,
            )
            return False
        client_base_url = getattr(self.evolution_client, "base_url", None)
        if not isinstance(client_base_url, str) or (
            client_base_url.rstrip("/") != authority.backend_url
        ):
            raise RuntimeError(
                "required evolution event export client destination changed after terminalization"
            )
        try:
            await asyncio.wait_for(
                self.evolution_client.export_event(build_evolution_session_event(result)),
                timeout=authority.timeout_seconds,
            )
        except Exception as exc:
            if not authority.fail_open:
                raise
            self._log_credential_safe_exception(
                None,
                "Evolution event export failed",
                exc,
                session_id=result.session_id,
                level=logging.WARNING,
            )
            return False
        return True

    async def _run_exec_inputs(
        self,
        runtime: BaseRuntime,
        steps: list[ExecInput],
        env: dict[str, str],
        managed: ManagedSession,
    ) -> AgentRunResult:
        """Execute steps and durably record the terminal agent result."""
        log_dir = self._ensure_log_authority(managed) / "logs" / "agent"

        for i, step in enumerate(steps):
            if managed.cancel_requested:
                return self._record_terminal_agent_result(
                    managed,
                    AgentRunResult(status="failed", return_code=-1, error="cancelled"),
                )
            merged_env = {**env, **(step.env or {})}
            result = await runtime.exec(
                step.command,
                cwd=step.cwd,
                env=merged_env,
                timeout_sec=self._remaining_budget(managed),
            )
            await self._write_exec_log(
                managed,
                ("logs", "agent"),
                f"step.{i:02d}",
                result.stdout,
                result.stderr,
            )
            if result.return_code == -1:
                return self._record_terminal_agent_result(
                    managed,
                    AgentRunResult(
                        status="timeout",
                        return_code=-1,
                        error=f"step {i} timed out",
                        metadata=self._step_metadata(log_dir, i, managed),
                    ),
                )
            if result.return_code != 0:
                return self._record_terminal_agent_result(
                    managed,
                    AgentRunResult(
                        status="failed",
                        return_code=result.return_code,
                        error=f"step {i} exited with code {result.return_code}",
                        metadata=self._step_metadata(log_dir, i, managed),
                    ),
                )

        return self._record_terminal_agent_result(
            managed,
            AgentRunResult(
                status="completed",
                return_code=0,
                metadata=self._step_metadata(log_dir, len(steps) - 1, managed),
            ),
        )

    def _record_terminal_agent_result(
        self,
        managed: ManagedSession,
        result: AgentRunResult,
    ) -> AgentRunResult:
        if _is_codex_subscription_agent(managed.request.agent):
            self._persist_subscription_finalization_authority(
                managed,
                agent_result=result,
            )
        managed.agent_result = result
        return result

    def _persist_subscription_finalization_authority(
        self,
        managed: ManagedSession,
        *,
        agent_result: AgentRunResult | None = None,
        pending_status: SessionStatus | None | object = _UNSET,
        pending_error: str | None | object = _UNSET,
    ) -> None:
        retries = getattr(self, "_cleanup_retries", None)
        if retries is None:
            retries = {}
            self._cleanup_retries = retries
        ownership = retries.get(managed.session_id)
        if ownership is None:
            ownership = self._cleanup_ownership_for(
                managed,
                eval_runtime=managed.eval_runtime,
                finalize_subscription=True,
            )
        updated = replace(
            ownership,
            managed=managed,
            phase=_RECOVERY_PHASE_TERMINAL_FINALIZATION,
            finalize_subscription=True,
            finalization_state=self._subscription_finalization_state(
                managed,
                agent_result=agent_result,
                pending_status=pending_status,
                pending_error=pending_error,
            ),
        )
        self._persist_cleanup_ownership(updated)
        retries[managed.session_id] = updated

    # ------------------------------------------------------------------
    # Evaluator runtime prewarm
    # ------------------------------------------------------------------

    def _start_eval_prewarm(self, managed: ManagedSession) -> None:
        """Spawn a background task to prewarm a fresh evaluator runtime."""
        request = managed.request
        if request.evaluator is None or not request.evaluator.refresh_runtime:
            return
        if managed.eval_prewarm_task is not None:
            return
        managed.eval_prewarm_task = asyncio.create_task(self._prepare_eval_runtime(managed))

    async def _prepare_eval_runtime(self, managed: ManagedSession) -> BaseRuntime | None:
        """Create and prepare a fresh runtime for the evaluator. Returns None on failure."""
        request = managed.request
        runtime_spec = self._resolve_runtime_spec(request)
        eval_session_dir = managed.session_dir / "eval_runtime"
        eval_artifacts_dir = eval_session_dir / "artifacts"
        eval_artifacts_dir.mkdir(parents=True, exist_ok=True)

        eval_runtime = create_runtime(
            runtime_spec,
            f"{request.session_id}-eval",
            eval_session_dir,
            docker_ownership_root=self._docker_ownership_root,
        )
        managed.eval_runtime = eval_runtime
        try:
            await self._await_with_budget(eval_runtime.start(), managed)
            if _is_codex_subscription_agent(request.agent):
                self._persist_cleanup_ownership(
                    self._cleanup_ownership_for(
                        managed,
                        eval_runtime=eval_runtime,
                    )
                )
            eval_actions = (
                runtime_spec.eval_prepare
                if runtime_spec.eval_prepare is not None
                else runtime_spec.prepare
            )
            await self._run_runtime_prepare(
                eval_runtime,
                runtime_spec,
                request,
                managed,
                actions=eval_actions,
                log_prefix="eval_prepare",
            )
            return eval_runtime
        except asyncio.CancelledError:
            try:
                await eval_runtime.stop()
            except Exception:
                managed.runtime_cleanup_blocked = True
            raise
        except Exception as exc:
            self._log_credential_safe_exception(
                managed,
                "Eval runtime prewarm failed",
                exc,
                level=logging.WARNING,
            )
            try:
                await eval_runtime.stop()
            except Exception:
                managed.runtime_cleanup_blocked = True
            return None

    async def _acquire_prepared_eval_runtime(self, managed: ManagedSession) -> BaseRuntime | None:
        """Await the prewarm task and return its runtime, if any."""
        task = managed.eval_prewarm_task
        if task is None:
            return None
        try:
            return await asyncio.wait_for(
                asyncio.shield(task), timeout=self._remaining_budget(managed)
            )
        except asyncio.TimeoutError as exc:
            raise GatewayExecutionTimeout(
                "timed out waiting for a fresh evaluator runtime"
            ) from exc

    async def _drain_eval_prewarm_task(self, managed: ManagedSession) -> BaseRuntime | None:
        """Resolve the prewarm task during teardown. Cancel if still running."""
        task = managed.eval_prewarm_task
        if task is None:
            return None
        if not task.done():
            task.cancel()
        try:
            runtime = await task
            if runtime is not None:
                managed.eval_runtime = runtime
            return runtime
        except (asyncio.CancelledError, Exception):
            return None

    # ------------------------------------------------------------------
    # POSTRUN stage
    # ------------------------------------------------------------------

    async def _handle_postrun(self, managed: ManagedSession) -> None:
        if _is_codex_subscription_agent(managed.request.agent) and managed.runtime is not None:
            await self._handle_subscription_postrun(managed)
            return
        await self._handle_standard_postrun(managed)

    async def _handle_standard_postrun(self, managed: ManagedSession) -> None:
        request = managed.request
        result: SessionResult | None = managed.final_result
        runtimes_removed = not managed.runtime_cleanup_blocked
        managed.timer.mark("postrun", "started")
        try:
            if result is None:
                if managed.agent_result is not None:
                    result = await self._build_session_result(managed)
                if managed.cancel_requested:
                    result = self._terminal_result_from_base(
                        result or self._cancelled_result(request, managed.timer),
                        SessionStatus.ERROR,
                        "session cancelled",
                    )
                elif result is None:
                    result = await self._build_session_result(managed)
        except GatewayExecutionTimeout as exc:
            result = self._timeout_result(request, managed.timer, str(exc))
        except Exception as exc:
            if _is_codex_subscription_agent(request.agent):
                self._log_credential_safe_exception(
                    managed,
                    "Post-run handling failed",
                    exc,
                )
            else:
                logger.exception("Post-run handling failed for session %s", request.session_id)
            result = self._error_result(request, managed.timer, f"post-run failed: {exc}")
        finally:
            managed.timer.mark("postrun", "finished")
            managed.timer.mark("teardown", "started")
            await self._run_postrun_steps(managed)
            try:
                self._redact_core_capture_authority(managed)
            except Exception as exc:
                self._log_credential_safe_exception(
                    managed,
                    "Credential capture redaction failed",
                    exc,
                )
                result = self._error_result(
                    request,
                    managed.timer,
                    f"credential capture redaction failed: {exc}",
                )
            stop_tasks = []
            eval_runtime = await self._drain_eval_prewarm_task(managed) or managed.eval_runtime
            if eval_runtime is not None:
                stop_tasks.append(
                    self._stop_runtime_best_effort(
                        eval_runtime, request.session_id, "eval runtime"
                    )
                )
            if managed.runtime is not None:
                stop_tasks.append(
                    self._stop_runtime_best_effort(managed.runtime, request.session_id, "runtime")
                )
            if stop_tasks:
                stop_results = await asyncio.gather(
                    *stop_tasks,
                    return_exceptions=True,
                )
                runtimes_removed = runtimes_removed and all(
                    outcome is True for outcome in stop_results
                )
            managed.timer.mark("teardown", "finished")
            managed.timer.mark("return", "finished")

        if result is None:
            result = self._error_result(
                request,
                managed.timer,
                "post-run finished without producing a session result",
            )
        if runtimes_removed:
            self._record_cleanup_runtimes_absent(managed)
        delivered = await self._deliver_terminal_result(managed, result)
        if runtimes_removed:
            if delivered:
                roots_removed = await self._remove_owned_roots(managed)
                if not roots_removed:
                    self._register_cleanup_retry(managed)
                else:
                    self._cleanup_retries.pop(request.session_id, None)
                    self._clear_cleanup_ownership(request.session_id)
        elif request.session_id not in self._cleanup_retries:
            self._register_cleanup_retry(managed)
            logger.error(
                "Retaining credential and session roots because a runtime "
                "was not proven removed for session %s",
                request.session_id,
            )

    async def _handle_subscription_postrun(self, managed: ManagedSession) -> None:
        request = managed.request
        result = managed.final_result
        managed.timer.mark("postrun", "started")
        await self._run_postrun_steps(managed)
        if result is None and request.evaluator is not None:
            try:
                result = await self._build_session_result(managed)
            except GatewayExecutionTimeout as exc:
                result = self._timeout_result(request, managed.timer, str(exc))
            except Exception as exc:
                self._log_credential_safe_exception(
                    managed,
                    "Subscription evaluation failed",
                    exc,
                )
                result = self._error_result(
                    request,
                    managed.timer,
                    f"subscription evaluation failed: {exc}",
                )
            managed.final_result = result
        managed.timer.mark("teardown", "started")
        eval_runtime = await self._drain_eval_prewarm_task(managed) or managed.eval_runtime
        stop_targets = [
            (eval_runtime, "eval runtime"),
            (managed.runtime, "runtime"),
        ]
        runtimes_removed = (
            not managed.runtime_cleanup_blocked
            and await self._stop_subscription_runtimes_with_retry(
                stop_targets,
                request.session_id,
            )
        )
        managed.timer.mark("teardown", "finished")
        if not runtimes_removed:
            self._register_cleanup_retry(
                managed,
                eval_runtime=eval_runtime,
                finalize_subscription=True,
            )
            logger.error(
                "Retaining subscription cleanup ownership because runtime absence "
                "was not proven for session %s",
                request.session_id,
            )
            return

        self._record_cleanup_runtimes_absent(managed)
        await self._finalize_subscription_after_runtime_absence(managed, result=result)

    async def _stop_subscription_runtimes_with_retry(
        self,
        targets: list[tuple[BaseRuntime | None, str]],
        session_id: str,
    ) -> bool:
        pending = [(runtime, label) for runtime, label in targets if runtime is not None]
        for attempt in range(_SUBSCRIPTION_STOP_ATTEMPTS):
            if not pending:
                return True
            outcomes = await asyncio.gather(
                *(
                    self._stop_runtime_best_effort(runtime, session_id, label)
                    for runtime, label in pending
                ),
                return_exceptions=True,
            )
            pending = [
                target
                for target, outcome in zip(pending, outcomes, strict=True)
                if outcome is not True
            ]
            if pending and attempt + 1 < _SUBSCRIPTION_STOP_ATTEMPTS:
                await asyncio.sleep(_SUBSCRIPTION_STOP_RETRY_DELAY_SECONDS)
        return not pending

    async def _finalize_subscription_after_runtime_absence(
        self,
        managed: ManagedSession,
        *,
        result: SessionResult | None,
    ) -> None:
        request = managed.request
        try:
            self._start_finalization_deadline(managed)
            self._redact_core_capture_authority(managed)
            if result is None:
                if managed.agent_result is not None:
                    result = await self._build_session_result(managed)
                if managed.cancel_requested:
                    error = "session cancelled"
                    result = self._terminal_result_from_base(
                        result or self._cancelled_result(request, managed.timer),
                        SessionStatus.ERROR,
                        error,
                    )
                elif managed.pending_status == SessionStatus.TIMEOUT:
                    error = managed.pending_error or "session execution timeout"
                    result = self._terminal_result_from_base(
                        result or self._timeout_result(request, managed.timer, error),
                        SessionStatus.TIMEOUT,
                        error,
                    )
                elif managed.pending_status == SessionStatus.ERROR:
                    error = managed.pending_error or "session execution failed"
                    result = self._terminal_result_from_base(
                        result or self._error_result(request, managed.timer, error),
                        SessionStatus.ERROR,
                        error,
                    )
                elif result is None:
                    result = await self._build_session_result(managed)
        except Exception as exc:
            self._log_credential_safe_exception(
                managed,
                "Subscription finalization failed",
                exc,
            )
            result = self._error_result(
                request,
                managed.timer,
                f"subscription finalization failed: {exc}",
            )
        finally:
            managed.timer.mark("postrun", "finished")
            managed.timer.mark("return", "finished")

        delivered = await self._deliver_terminal_result(managed, result)
        if delivered:
            roots_removed = await self._remove_owned_roots(managed)
            if roots_removed:
                self._cleanup_retries.pop(request.session_id, None)
                self._clear_cleanup_ownership(request.session_id)
            else:
                self._register_cleanup_retry(managed)

    async def _deliver_terminal_result(
        self,
        managed: ManagedSession,
        result: SessionResult,
    ) -> bool:
        normalized = result.model_copy(
            update={
                "timing": managed.timer.to_session_timing(),
                "node_id": self.node_id,
                "error": result.error or result.trajectory.error,
            }
        )
        normalized = self._redact_in_memory_result(managed, normalized)
        ownership = self._prepare_terminal_delivery(managed, normalized)
        managed.final_result = normalized
        return await self._resume_terminal_delivery(ownership)

    async def _build_session_result(self, managed: ManagedSession) -> SessionResult:
        request = managed.request
        agent_result = managed.agent_result
        if agent_result is None:
            return self._error_result(
                request,
                managed.timer,
                "session did not produce an agent result",
            )

        self.session_registry.set_status(request.session_id, SessionStatus.BUILDING)
        managed.timer.mark("build", "started")
        try:
            verified_transcript: VerifiedSessionTranscript | None = None
            runtime_spec = managed.runtime.spec if managed.runtime is not None else request.runtime
            verified_authority_capture = transcript_capture_enabled(
                request.agent.settings.get("capture_mode")
            ) and (
                _is_codex_subscription_agent(request.agent)
                or (runtime_spec is not None and runtime_spec.profile is not None)
            )
            allow_transcript_path_open = not verified_authority_capture
            await_build = (
                self._await_with_finalization_budget
                if verified_authority_capture
                else self._await_with_budget
            )
            if not allow_transcript_path_open:
                if managed.log_authority_dir is None or managed.log_authority_identity is None:
                    raise RuntimeError("managed transcript requires a pinned log authority")
                step_index = _transcript_step_index(agent_result)
                if step_index is not None:
                    verified_transcript = await await_build(
                        asyncio.to_thread(
                            read_verified_session_transcript,
                            managed.log_authority_dir,
                            managed.log_authority_identity,
                            step_index=step_index,
                            require_private_root=True,
                        ),
                        managed,
                    )
            initial_agent_result = (
                agent_result if request.builder.strategy == "agent_transcript" else None
            )
            trajectory = await await_build(
                asyncio.to_thread(
                    self._build_trajectory,
                    request,
                    agent_result=initial_agent_result,
                    verified_transcript=verified_transcript,
                    allow_transcript_path_open=allow_transcript_path_open,
                ),
                managed,
            )
            if self._should_build_agent_transcript_fallback(
                request,
                agent_result,
                trajectory,
            ):
                trajectory = await await_build(
                    asyncio.to_thread(
                        self._build_trajectory,
                        request,
                        agent_result=agent_result,
                        builder_spec=StrategySpec(strategy="agent_transcript"),
                        verified_transcript=verified_transcript,
                        allow_transcript_path_open=allow_transcript_path_open,
                    ),
                    managed,
                )
        finally:
            managed.timer.mark("build", "finished")

        error = trajectory.error
        if agent_result.status == "timeout":
            trajectory = trajectory.model_copy(
                update={"status": "TIMEOUT", "error": agent_result.error or error}
            )
        elif agent_result.status == "failed":
            trajectory = trajectory.model_copy(
                update={"status": "ERROR", "error": agent_result.error or error}
            )

        managed.timer.mark("eval", "started")
        try:
            if request.evaluator is not None:
                self.session_registry.set_status(request.session_id, SessionStatus.EVALUATING)
                trajectory = await self._run_eval(
                    request,
                    trajectory,
                    agent_result=agent_result,
                    managed=managed,
                )
        except GatewayExecutionTimeout as exc:
            # Preserve the built trajectory even when eval times out.
            self._log_credential_safe_exception(
                managed,
                "Eval timed out",
                exc,
                level=logging.WARNING,
            )
            if trajectory.status not in ("TIMEOUT", "ERROR"):
                trajectory = trajectory.model_copy(
                    update={"status": "TIMEOUT", "error": f"eval timed out: {exc}"}
                )
        except Exception as exc:
            self._log_credential_safe_exception(managed, "Eval failed", exc)
            trajectory = trajectory.model_copy(
                update={"status": "ERROR", "error": f"evaluator failed: {exc}"}
            )
        finally:
            managed.timer.mark("eval", "finished")

        error = trajectory.error or error
        return SessionResult(
            session_id=request.session_id,
            task_id=request.task_id,
            status=trajectory.status,
            trajectory=trajectory,
            timing=managed.timer.to_session_timing(),
            node_id=self.node_id,
            error=error,
            metadata=dict(request.metadata),
        )

    def _build_trajectory(
        self,
        request: SessionDispatchRequest,
        *,
        agent_result: AgentRunResult | None = None,
        builder_spec: StrategySpec | None = None,
        verified_transcript: VerifiedSessionTranscript | None = None,
        allow_transcript_path_open: bool = True,
    ) -> Trajectory:
        completion_session = self.storage.load_completion_session(request.session_id)
        effective_builder_spec = builder_spec or request.builder
        if effective_builder_spec.strategy == "agent_transcript":
            completion_session = _completion_session_with_agent_metadata(
                completion_session,
                request,
                agent_result,
            )
        builder = self.builders.create(effective_builder_spec)
        if (
            effective_builder_spec.strategy == "agent_transcript"
            and not allow_transcript_path_open
        ):
            if not isinstance(builder, AgentTranscriptBuilder):
                raise RuntimeError(
                    "managed transcript requires the built-in verified-byte builder"
                )
            result = builder.build_verified_transcript(
                completion_session,
                transcript_bytes=(
                    None if verified_transcript is None else verified_transcript.content
                ),
                transcript_path=(
                    None if verified_transcript is None else verified_transcript.path
                ),
            )
        else:
            result = builder.build(completion_session)
        if asyncio.iscoroutine(result):
            trajectory = asyncio.run(result)
        else:
            trajectory = result
        return Trajectory.model_validate(trajectory)

    @staticmethod
    def _should_build_agent_transcript_fallback(
        request: SessionDispatchRequest,
        agent_result: AgentRunResult,
        trajectory: Trajectory,
    ) -> bool:
        if trajectory.error != "no completions":
            return False
        if request.builder.strategy == "agent_transcript":
            return False
        settings = request.agent.settings
        if not transcript_capture_enabled(settings.get("capture_mode")):
            return False
        return bool(agent_result.metadata)

    async def _run_eval(
        self,
        request: SessionDispatchRequest,
        trajectory: Trajectory,
        *,
        agent_result: AgentRunResult,
        managed: ManagedSession,
    ) -> Trajectory:
        evaluator_spec = request.evaluator
        if evaluator_spec is None:
            return trajectory

        live_runtime = managed.runtime
        if live_runtime is None:
            raise RuntimeError("runtime is required for evaluation")

        fresh_eval_runtime: BaseRuntime | None = None
        if evaluator_spec.refresh_runtime:
            fresh_eval_runtime = await self._acquire_prepared_eval_runtime(managed)
            if fresh_eval_runtime is None:
                return trajectory.model_copy(
                    update={
                        "status": "ERROR",
                        "error": "refresh_runtime=true requires a fresh runtime: eval runtime prewarm did not produce a usable runtime",
                    }
                )

        # Convert EvaluatorSpec to StrategySpec for registry
        strategy_spec = StrategySpec(
            strategy=evaluator_spec.strategy,
            config=evaluator_spec.config,
        )

        try:
            evaluator = self.evaluators.create(strategy_spec)
            eval_result = await self._await_with_budget(
                evaluator.evaluate(
                    trajectory,
                    session_id=request.session_id,
                    task_id=request.task_id,
                    session_dir=managed.session_dir,
                    artifacts_dir=managed.artifacts_dir,
                    agent_result=agent_result,
                    env=dict(evaluator_spec.env),
                    timeout_seconds=self._remaining_budget(managed),
                    runtime=live_runtime,
                    fresh_eval_runtime=fresh_eval_runtime,
                    runtime_spec=request.runtime or self.default_runtime,
                    refresh_runtime=evaluator_spec.refresh_runtime,
                ),
                managed,
            )
        except Exception as exc:
            self._log_credential_safe_exception(
                managed,
                f"Evaluator {evaluator_spec.strategy} failed",
                exc,
            )
            return trajectory.model_copy(
                update={"status": "ERROR", "error": f"evaluator failed: {exc}"}
            )

        return self._merge_eval_result(trajectory, eval_result, evaluator_spec)

    @staticmethod
    def _merge_eval_result(
        trajectory: Trajectory,
        eval_result: EvalResult,
        evaluator_spec: EvaluatorSpec,
    ) -> Trajectory:
        """Apply rewards from EvalResult to trajectory traces."""
        traces = list(trajectory.traces)

        if eval_result.trace_rewards is not None:
            if len(eval_result.trace_rewards) != len(traces):
                return trajectory.model_copy(
                    update={
                        "status": "ERROR",
                        "error": (
                            f"evaluator returned {len(eval_result.trace_rewards)} "
                            f"trace_rewards but trajectory has {len(traces)} traces"
                        ),
                    }
                )
            traces = [
                trace.model_copy(update={"reward": reward})
                for trace, reward in zip(traces, eval_result.trace_rewards)
            ]
        elif eval_result.outcome_reward is not None and traces:
            # Broadcast trajectory-level reward
            traces = [
                trace.model_copy(update={"reward": eval_result.outcome_reward}) for trace in traces
            ]

        eval_metadata = {
            "strategy": evaluator_spec.strategy,
            "outcome_reward": eval_result.outcome_reward,
            "trace_rewards": eval_result.trace_rewards,
            **eval_result.metadata,
        }
        metadata = {**trajectory.metadata, "evaluation": eval_metadata}
        return trajectory.model_copy(update={"traces": traces, "metadata": metadata})

    # ------------------------------------------------------------------
    # Environment and helpers
    # ------------------------------------------------------------------

    def _runtime_env(
        self,
        request: SessionDispatchRequest,
        managed: ManagedSession,
        *,
        include_agent_env: bool = False,
        runtime_override: BaseRuntime | None = None,
    ) -> dict[str, str]:
        runtime = runtime_override or managed.runtime
        if runtime is None:
            session_dir = str(managed.session_dir)
            artifacts_dir = str(managed.artifacts_dir)
            logs_dir = str(managed.session_dir / "logs")
            agent_log_dir = str(managed.session_dir / "logs" / "agent")
            runtime_env: dict[str, str] = {}
        else:
            session_dir = runtime.runtime_session_dir
            artifacts_dir = runtime.runtime_artifacts_dir
            logs_dir = runtime.runtime_logs_dir
            agent_log_dir = runtime.runtime_agent_log_dir
            runtime_env = dict(runtime.spec.env)
        agent_env = dict(request.agent.env) if include_agent_env else {}
        credential_env = (
            MANAGED_SUBSCRIPTION_ENV if _is_codex_subscription_agent(request.agent) else {}
        )
        return {
            "ANTHROPIC_BASE_URL": self.gateway_url,
            "ANTHROPIC_API_KEY": request.session_id,
            "OPENAI_BASE_URL": f"{self.gateway_url.rstrip('/')}/v1",
            "OPENAI_API_KEY": request.session_id,
            "GOOGLE_API_URL": self.gateway_url,
            "GOOGLE_API_KEY": request.session_id,
            "SESSION_ID": request.session_id,
            "TASK_ID": request.task_id,
            "SESSION_DIR": session_dir,
            "ARTIFACTS_DIR": artifacts_dir,
            "LOGS_DIR": logs_dir,
            "AGENT_LOG_DIR": agent_log_dir,
            **{key: str(value) for key, value in runtime_env.items()},
            **{key: str(value) for key, value in agent_env.items()},
            **credential_env,
        }

    async def _write_exec_log(
        self,
        managed: ManagedSession,
        directory_parts: tuple[str, ...],
        prefix: str,
        stdout: str | None,
        stderr: str | None,
    ) -> None:
        authority_dir = self._ensure_log_authority(managed)
        identity = managed.log_authority_identity
        if identity is None:
            raise SessionFileSecurityError("session log authority is not pinned")
        redactor = managed.credential_redactor
        for stream_name, value in (("stdout", stdout), ("stderr", stderr)):
            if not value:
                continue
            rendered = redactor.redact(value) if redactor is not None else value
            await asyncio.to_thread(
                write_verified_session_log,
                authority_dir,
                identity,
                directory_parts=directory_parts,
                leaf_name=f"{prefix}.{stream_name}.log",
                content=rendered,
            )

    @staticmethod
    def _step_metadata(log_dir: Path, step_index: int, managed: ManagedSession) -> dict:
        return {
            "log_dir": str(log_dir),
            "last_step": step_index,
            "cwd": str(managed.session_dir),
        }

    def _error_result(
        self,
        request: SessionDispatchRequest,
        timer: StageTimer,
        error: str,
    ) -> SessionResult:
        return SessionResult(
            session_id=request.session_id,
            task_id=request.task_id,
            status="ERROR",
            trajectory=Trajectory(
                status="ERROR",
                metadata={
                    "builder": request.builder.strategy,
                    "record_count": 0,
                    "task_metadata": dict(request.metadata),
                },
                traces=[],
                error=error,
            ),
            timing=timer.to_session_timing(),
            node_id=self.node_id,
            error=error,
            metadata=dict(request.metadata),
        )

    def _set_terminal_failure(
        self,
        managed: ManagedSession,
        status: SessionStatus,
        error: str,
    ) -> None:
        if _is_codex_subscription_agent(managed.request.agent) and (
            managed.runtime is not None or managed.credential_dir is not None
        ):
            self._persist_subscription_finalization_authority(
                managed,
                pending_status=status,
                pending_error=error,
            )
            managed.pending_status = status
            managed.pending_error = error
            return
        if status == SessionStatus.TIMEOUT:
            managed.final_result = self._timeout_result(
                managed.request,
                managed.timer,
                error,
            )
        else:
            managed.final_result = self._error_result(
                managed.request,
                managed.timer,
                error,
            )

    def _timeout_result(
        self,
        request: SessionDispatchRequest,
        timer: StageTimer,
        error: str,
    ) -> SessionResult:
        return SessionResult(
            session_id=request.session_id,
            task_id=request.task_id,
            status="TIMEOUT",
            trajectory=Trajectory(
                status="TIMEOUT",
                metadata={
                    "builder": request.builder.strategy,
                    "record_count": 0,
                    "task_metadata": dict(request.metadata),
                },
                traces=[],
                error=error,
            ),
            timing=timer.to_session_timing(),
            node_id=self.node_id,
            error=error,
            metadata=dict(request.metadata),
        )

    def _cancelled_result(
        self, request: SessionDispatchRequest, timer: StageTimer
    ) -> SessionResult:
        return self._error_result(request, timer, "session cancelled")

    @staticmethod
    def _terminal_result_from_base(
        result: SessionResult,
        status: SessionStatus,
        error: str,
    ) -> SessionResult:
        trajectory = result.trajectory.model_copy(update={"status": status, "error": error})
        return result.model_copy(
            update={"status": status, "trajectory": trajectory, "error": error}
        )

    async def _push_result(self, callback_url: str | None, result: SessionResult) -> bool:
        """POST the terminal result to the rollout server. Return True on success."""
        if not callback_url:
            return False
        try:
            result_digest = self._terminal_result_digest(result)
            response = await self._client.post(
                callback_url,
                json=result.model_dump(mode="json"),
                headers={
                    "Idempotency-Key": f"openevo-session-result-{result_digest}",
                    "X-OpenEvo-Result-SHA256": result_digest,
                },
            )
            response.raise_for_status()
            return True
        except Exception as exc:
            self._log_credential_safe_exception(
                None,
                "Failed to deliver terminal callback",
                exc,
                session_id=result.session_id,
                level=logging.WARNING,
            )
            return False

    @staticmethod
    def _terminal_result_digest(result: SessionResult) -> str:
        canonical = json.dumps(
            result.model_dump(mode="json"),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()

    @staticmethod
    def _snapshot_to_metrics(snapshot: DispatcherSnapshot) -> NodeStageMetrics:
        return NodeStageMetrics(
            init_queue_depth=snapshot.init_queue_depth,
            init_inflight=snapshot.init_inflight,
            ready_depth=snapshot.ready_depth,
            run_inflight=snapshot.run_inflight,
            postrun_queue_depth=snapshot.postrun_queue_depth,
            postrun_inflight=snapshot.postrun_inflight,
        )

    def _remaining_budget(self, managed: ManagedSession) -> float:
        deadline = managed.execution_deadline
        if deadline is None:
            raise RuntimeError("session execution deadline was not initialized")
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise GatewayExecutionTimeout("session execution timeout")
        return remaining

    async def _await_with_budget(
        self,
        awaitable,
        managed: ManagedSession,
    ):
        try:
            return await asyncio.wait_for(
                awaitable,
                timeout=self._remaining_budget(managed),
            )
        except asyncio.TimeoutError as exc:
            raise GatewayExecutionTimeout("session execution timeout") from exc

    async def _await_with_finalization_budget(
        self,
        awaitable,
        managed: ManagedSession,
    ):
        self._start_finalization_deadline(managed)
        deadline = managed.finalization_deadline
        assert deadline is not None
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            if asyncio.iscoroutine(awaitable):
                awaitable.close()
            raise GatewayExecutionTimeout("session transcript finalization timeout")
        try:
            return await asyncio.wait_for(awaitable, timeout=remaining)
        except asyncio.TimeoutError as exc:
            raise GatewayExecutionTimeout("session transcript finalization timeout") from exc

    @staticmethod
    def _start_execution_deadline(managed: ManagedSession) -> None:
        if managed.execution_deadline is not None:
            return
        managed.execution_deadline = (
            asyncio.get_running_loop().time() + managed.request.remaining_timeout_seconds
        )

    @staticmethod
    def _start_finalization_deadline(managed: ManagedSession) -> None:
        if managed.finalization_deadline is not None:
            return
        managed.finalization_deadline = (
            asyncio.get_running_loop().time() + _FINALIZATION_BUDGET_SECONDS
        )

    async def _run_postrun_steps(self, managed: ManagedSession) -> None:
        if not managed.postrun_steps or managed.runtime is None:
            return
        env = self._runtime_env(managed.request, managed, include_agent_env=True)
        for i, step in enumerate(managed.postrun_steps):
            try:
                merged_env = {**env, **(step.env or {})}
                result = await managed.runtime.exec(
                    step.command,
                    cwd=step.cwd,
                    env=merged_env,
                    timeout_sec=self._remaining_budget(managed),
                )
                await self._write_exec_log(
                    managed,
                    ("logs", "teardown"),
                    f"step.{i:02d}",
                    result.stdout,
                    result.stderr,
                )
            except Exception as exc:
                if _is_codex_subscription_agent(managed.request.agent):
                    self._log_credential_safe_exception(
                        managed,
                        "Teardown step failed",
                        exc,
                    )
                else:
                    logger.debug(
                        "Teardown step failed for session %s",
                        managed.request.session_id,
                        exc_info=True,
                    )

    async def _stop_runtime_best_effort(
        self,
        runtime: BaseRuntime,
        session_id: str,
        label: str,
    ) -> bool:
        try:
            await runtime.stop()
            return True
        except Exception as exc:
            self._log_credential_safe_exception(
                None,
                f"Failed to stop {label}",
                exc,
                session_id=session_id,
                level=logging.WARNING,
            )
            return False

    @staticmethod
    def _redact_in_memory_result(
        managed: ManagedSession,
        result: SessionResult,
    ) -> SessionResult:
        redactor = managed.credential_redactor
        if redactor is None:
            return result

        def redact(value: Any) -> Any:
            if isinstance(value, str):
                return redactor.redact(value)
            if isinstance(value, list):
                return [redact(item) for item in value]
            if isinstance(value, tuple):
                return tuple(redact(item) for item in value)
            if isinstance(value, dict):
                return {redactor.redact(str(key)): redact(item) for key, item in value.items()}
            return value

        return SessionResult.model_validate(redact(result.model_dump(mode="python")))

    @staticmethod
    def _log_credential_safe_exception(
        managed: ManagedSession | None,
        message: str,
        exc: Exception,
        *,
        session_id: str | None = None,
        level: int = logging.ERROR,
    ) -> None:
        exception_type = type(exc).__name__
        redactor = None if managed is None else managed.credential_redactor
        rendered = exception_type
        if redactor is not None:
            detail = redactor.redact(str(exc))
            if detail:
                rendered = f"{exception_type}: {detail}"
        logger.log(
            level,
            "%s for session %s [%s]",
            message,
            session_id or (managed.session_id if managed is not None else "unknown"),
            rendered,
        )

    async def _remove_owned_roots(self, managed: ManagedSession) -> bool:
        credential_removed = True
        if managed.credential_dir is not None:
            credential_removed = await self._remove_credential_dir_best_effort(
                managed.credential_dir,
                managed.session_id,
                managed.credential_root_identity,
                (
                    managed.credential_auth_identity
                    or (
                        None
                        if managed.credential_mount is None
                        else managed.credential_mount.auth_identity
                    )
                ),
            )
        tasks = [
            self._remove_session_dir_best_effort(
                managed.session_dir,
                managed.session_id,
                managed.session_root_identity,
            )
        ]
        if managed.log_authority_dir is not None:
            tasks.append(
                self._remove_log_authority_best_effort(
                    managed.log_authority_dir,
                    managed.session_id,
                    managed.log_authority_identity,
                )
            )
        outcomes = await asyncio.gather(*tasks, return_exceptions=True)
        return credential_removed and all(
            outcome is not False and not isinstance(outcome, BaseException) for outcome in outcomes
        )

    def _register_cleanup_retry(
        self,
        managed: ManagedSession,
        *,
        eval_runtime: BaseRuntime | None = None,
        finalize_subscription: bool = False,
    ) -> None:
        retries = getattr(self, "_cleanup_retries", None)
        if retries is None:
            retries = {}
            self._cleanup_retries = retries
        ownership = self._cleanup_ownership_for(
            managed,
            eval_runtime=eval_runtime,
            finalize_subscription=finalize_subscription,
        )
        previous = retries.get(managed.session_id)
        if previous is not None:
            ownership.delivery_state = previous.delivery_state
            if ownership.credential_auth_identity is None:
                ownership.credential_auth_identity = previous.credential_auth_identity
            if previous.delivery_state is not None:
                ownership.phase = previous.phase
            if ownership.finalization_state is None:
                ownership.finalization_state = previous.finalization_state
                ownership.finalize_subscription = previous.finalize_subscription
                if previous.finalization_state is not None:
                    ownership.phase = previous.phase
        self._persist_cleanup_ownership(ownership)
        retries[managed.session_id] = ownership

    def _record_cleanup_runtimes_absent(self, managed: ManagedSession) -> None:
        ownership = self._cleanup_retries.get(managed.session_id)
        if ownership is not None:
            ownership = self._record_cleanup_ownership_runtimes_absent(ownership)
        managed.runtime = None
        managed.eval_runtime = None

    def _record_cleanup_ownership_runtimes_absent(
        self,
        ownership: CleanupRetryOwnership,
    ) -> CleanupRetryOwnership:
        updated = replace(
            ownership,
            runtime_id=None,
            container_id=None,
            eval_runtime_id=None,
            eval_container_id=None,
            runtime=None,
            eval_runtime=None,
        )
        self._persist_cleanup_ownership(updated)
        self._cleanup_retries[ownership.session_id] = updated
        return updated

    def _prepare_terminal_delivery(
        self,
        managed: ManagedSession,
        result: SessionResult,
    ) -> CleanupRetryOwnership:
        retries = self._cleanup_retries
        ownership = retries.get(managed.session_id)
        if ownership is None:
            ownership = self._cleanup_ownership_for(
                managed,
                eval_runtime=managed.eval_runtime,
                finalize_subscription=_is_codex_subscription_agent(managed.request.agent),
            )
        finalization_state = ownership.finalization_state
        if finalization_state is not None:
            finalization_state = replace(finalization_state, final_result=result)

        result_digest = self._terminal_result_digest(result)
        state = ownership.delivery_state
        if state is None:
            export_authority = self._current_export_authority()
            export_required = export_authority is not None
            callback_required = managed.request.callback_url is not None
            state = TerminalDeliveryState(
                result=result,
                result_digest=result_digest,
                callback_url=managed.request.callback_url,
                export_required=export_required,
                export_authority=export_authority,
                export_succeeded=not export_required,
                callback_required=callback_required,
                callback_succeeded=not callback_required,
            )
        elif state.result_digest != result_digest or state.result != result:
            raise RuntimeError("terminal delivery result identity changed")
        updated = replace(
            ownership,
            managed=managed,
            finalization_state=finalization_state,
            delivery_state=state,
            phase=_RECOVERY_PHASE_TERMINAL_DELIVERY,
        )
        self._persist_cleanup_ownership(updated)
        retries[managed.session_id] = updated
        return updated

    async def _resume_terminal_delivery(
        self,
        ownership: CleanupRetryOwnership,
    ) -> bool:
        state = ownership.delivery_state
        if state is None:
            raise RuntimeError("terminal delivery authority is missing")
        result = state.result
        session_id = ownership.session_id
        if self.session_registry.get(session_id) is None:
            self.session_registry.register(
                session_id,
                task_id=result.task_id,
                registered=True,
                status=SessionStatus.POST_RUN,
                metadata=dict(result.metadata),
            )
        self.session_registry.set_result(session_id, result)

        export_failed_open = False
        if not state.export_succeeded:
            authority = state.export_authority
            if authority is None:
                logger.warning(
                    "Required terminal evolution export authority is missing for session %s",
                    session_id,
                )
                return False
            try:
                exported = await self._export_evolution_event_with_authority(
                    result,
                    authority,
                )
            except Exception as exc:
                self._log_credential_safe_exception(
                    ownership.managed,
                    "Terminal evolution export remains pending",
                    exc,
                    session_id=session_id,
                    level=logging.WARNING,
                )
                return False
            if exported:
                ownership = self._advance_terminal_delivery(
                    ownership,
                    export_succeeded=True,
                )
                state = ownership.delivery_state
                assert state is not None
            else:
                export_failed_open = True

        if not state.callback_succeeded and (state.export_succeeded or export_failed_open):
            if await self._push_result(state.callback_url, result):
                ownership = self._advance_terminal_delivery(
                    ownership,
                    callback_succeeded=True,
                )
                state = ownership.delivery_state
                assert state is not None

        if state.callback_required and state.callback_succeeded:
            self.session_registry.clear_result_payload(session_id)

        if not state.complete:
            return False
        self.storage.delete_session(session_id)
        return True

    def _advance_terminal_delivery(
        self,
        ownership: CleanupRetryOwnership,
        *,
        export_succeeded: bool = False,
        callback_succeeded: bool = False,
    ) -> CleanupRetryOwnership:
        state = ownership.delivery_state
        if state is None:
            raise RuntimeError("terminal delivery authority is missing")
        updated = TerminalDeliveryState(
            result=state.result,
            result_digest=state.result_digest,
            callback_url=state.callback_url,
            export_required=state.export_required,
            export_authority=state.export_authority,
            export_succeeded=state.export_succeeded or export_succeeded,
            callback_required=state.callback_required,
            callback_succeeded=state.callback_succeeded or callback_succeeded,
        )
        updated_ownership = replace(ownership, delivery_state=updated)
        self._persist_cleanup_ownership(updated_ownership)
        self._cleanup_retries[ownership.session_id] = updated_ownership
        return updated_ownership

    @staticmethod
    def _redact_finalization_value(
        value: Any,
        redactor: CredentialRedactor | None,
    ) -> Any:
        if redactor is None:
            return value
        if isinstance(value, str):
            return redactor.redact(value)
        if isinstance(value, list):
            return [
                GatewayNodeManager._redact_finalization_value(item, redactor) for item in value
            ]
        if isinstance(value, dict):
            return {
                key: GatewayNodeManager._redact_finalization_value(item, redactor)
                for key, item in value.items()
            }
        return value

    @classmethod
    def _subscription_finalization_state(
        cls,
        managed: ManagedSession,
        *,
        agent_result: AgentRunResult | None = None,
        pending_status: SessionStatus | None | object = _UNSET,
        pending_error: str | None | object = _UNSET,
    ) -> SubscriptionFinalizationState:
        redactor = managed.credential_redactor

        def redacted_model(model, model_type):
            if model is None:
                return None
            payload = cls._redact_finalization_value(
                model.model_dump(mode="json"),
                redactor,
            )
            return model_type.model_validate(payload)

        effective_pending_status = (
            managed.pending_status
            if pending_status is _UNSET
            else cast(SessionStatus | None, pending_status)
        )
        effective_pending_error = (
            managed.pending_error if pending_error is _UNSET else cast(str | None, pending_error)
        )
        if effective_pending_error is not None and redactor is not None:
            effective_pending_error = redactor.redact(str(effective_pending_error))
        return SubscriptionFinalizationState(
            request=redacted_model(managed.request, SessionDispatchRequest),
            agent_result=redacted_model(
                agent_result if agent_result is not None else managed.agent_result,
                AgentRunResult,
            ),
            final_result=redacted_model(managed.final_result, SessionResult),
            pending_status=effective_pending_status,
            pending_error=effective_pending_error,
            cancel_requested=managed.cancel_requested,
            timer_marks=dict(managed.timer._marks),
        )

    @staticmethod
    def _cleanup_ownership_for(
        managed: ManagedSession,
        *,
        eval_runtime: BaseRuntime | None = None,
        finalize_subscription: bool = False,
    ) -> CleanupRetryOwnership:
        runtime = managed.runtime
        finalization_state = (
            GatewayNodeManager._subscription_finalization_state(managed)
            if finalize_subscription
            else None
        )
        return CleanupRetryOwnership(
            session_id=managed.session_id,
            session_dir=managed.session_dir,
            session_root_identity=managed.session_root_identity,
            log_authority_dir=managed.log_authority_dir,
            log_authority_identity=managed.log_authority_identity,
            credential_dir=managed.credential_dir,
            credential_root_identity=managed.credential_root_identity,
            credential_auth_identity=(
                managed.credential_auth_identity
                or (
                    None
                    if managed.credential_mount is None
                    else managed.credential_mount.auth_identity
                )
            ),
            runtime_id=(str(getattr(runtime, "runtime_id", "")) or None),
            container_id=getattr(runtime, "container_id", None),
            eval_runtime_id=(str(getattr(eval_runtime, "runtime_id", "")) or None),
            eval_container_id=getattr(eval_runtime, "container_id", None),
            runtime=runtime,
            phase=(
                _RECOVERY_PHASE_TERMINAL_FINALIZATION
                if finalize_subscription
                else _RECOVERY_PHASE_RUNTIME_ACTIVE
            ),
            eval_runtime=eval_runtime,
            managed=managed,
            finalize_subscription=finalize_subscription,
            finalization_state=finalization_state,
        )

    @staticmethod
    def _cleanup_journal_identity(state: os.stat_result) -> tuple[int, int, int, int]:
        return (state.st_dev, state.st_ino, state.st_uid, stat.S_IFMT(state.st_mode))

    @staticmethod
    def _cleanup_journal_marker_name(path: Path) -> str:
        digest = hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:24]
        return f".{path.name}.{digest}.root.json"

    @staticmethod
    def _write_all(descriptor: int, content: bytes, *, label: str) -> None:
        offset = 0
        while offset < len(content):
            written = os.write(descriptor, content[offset:])
            if written <= 0:
                raise RuntimeError(f"{label} write made no progress")
            offset += written

    @classmethod
    def _read_cleanup_journal_root_marker(
        cls,
        parent_fd: int,
        marker_name: str,
    ) -> object:
        before = os.stat(marker_name, dir_fd=parent_fd, follow_symlinks=False)
        descriptor = os.open(
            marker_name,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=parent_fd,
        )
        try:
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_uid != os.geteuid()
                or opened.st_nlink != 1
                or stat.S_IMODE(opened.st_mode) != 0o600
                or opened.st_size <= 0
                or opened.st_size > _CLEANUP_JOURNAL_ROOT_MARKER_MAX_BYTES
                or cls._cleanup_journal_identity(before) != cls._cleanup_journal_identity(opened)
            ):
                raise RuntimeError("cleanup journal root identity marker is invalid")
            chunks: list[bytes] = []
            remaining = opened.st_size
            while remaining:
                chunk = os.read(descriptor, remaining)
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            after = os.stat(marker_name, dir_fd=parent_fd, follow_symlinks=False)
            if (
                remaining
                or os.read(descriptor, 1)
                or cls._cleanup_journal_identity(after) != cls._cleanup_journal_identity(opened)
                or after.st_size != opened.st_size
            ):
                raise RuntimeError("cleanup journal root identity marker changed during read")
            try:
                return json.loads(b"".join(chunks).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise RuntimeError("cleanup journal root identity marker is invalid") from exc
        finally:
            os.close(descriptor)

    def _open_cleanup_journal_authority(
        self,
        *,
        initialize: bool,
    ) -> _CleanupJournalAuthority | None:
        path = getattr(self, "_cleanup_journal_dir", None)
        if path is None:
            return None
        path = Path(path)
        if (
            not path.is_absolute()
            or path != Path(os.path.normpath(path))
            or path.name in {"", ".", ".."}
        ):
            raise RuntimeError("cleanup journal path must be normalized and absolute")

        parts = path.parts
        ancestor_fds: list[int] = []
        ancestor_identities: list[tuple[int, int, int, int]] = []
        root_fd = -1
        try:
            anchor_fd = os.open("/", _CLEANUP_DIRECTORY_FLAGS)
            ancestor_fds.append(anchor_fd)
            ancestor_identities.append(self._cleanup_journal_identity(os.fstat(anchor_fd)))
            for component in parts[1:-1]:
                current_fd = ancestor_fds[-1]
                try:
                    before = os.stat(component, dir_fd=current_fd, follow_symlinks=False)
                except FileNotFoundError:
                    if not initialize:
                        while ancestor_fds:
                            os.close(ancestor_fds.pop())
                        return None
                    os.mkdir(component, mode=0o700, dir_fd=current_fd)
                    before = os.stat(component, dir_fd=current_fd, follow_symlinks=False)
                if not stat.S_ISDIR(before.st_mode) or stat.S_ISLNK(before.st_mode):
                    raise RuntimeError("cleanup journal ancestor path is not a directory")
                next_fd = os.open(component, _CLEANUP_DIRECTORY_FLAGS, dir_fd=current_fd)
                opened = os.fstat(next_fd)
                if self._cleanup_journal_identity(before) != self._cleanup_journal_identity(
                    opened
                ):
                    os.close(next_fd)
                    raise RuntimeError("cleanup journal ancestor identity changed")
                ancestor_fds.append(next_fd)
                ancestor_identities.append(self._cleanup_journal_identity(opened))

            parent_fd = ancestor_fds[-1]
            parent_opened = os.fstat(parent_fd)
            if (
                parent_opened.st_uid != os.geteuid()
                or stat.S_IMODE(parent_opened.st_mode) != 0o700
            ):
                raise RuntimeError("cleanup journal authority parent is not private")
            root_name = path.name
            marker_name = self._cleanup_journal_marker_name(path)
            try:
                root_before = os.stat(root_name, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                root_before = None
            try:
                os.stat(marker_name, dir_fd=parent_fd, follow_symlinks=False)
                marker_exists = True
            except FileNotFoundError:
                marker_exists = False

            if root_before is None and not marker_exists:
                if not initialize:
                    while ancestor_fds:
                        os.close(ancestor_fds.pop())
                    return None
                os.mkdir(root_name, mode=0o700, dir_fd=parent_fd)
                root_before = os.stat(root_name, dir_fd=parent_fd, follow_symlinks=False)
                root_fd = os.open(root_name, _CLEANUP_DIRECTORY_FLAGS, dir_fd=parent_fd)
                root_opened = os.fstat(root_fd)
                root_identity = self._cleanup_journal_identity(root_opened)
                if self._cleanup_journal_identity(root_before) != root_identity:
                    raise RuntimeError("cleanup journal root identity changed during creation")
                if (
                    not stat.S_ISDIR(root_opened.st_mode)
                    or root_opened.st_uid != os.geteuid()
                    or stat.S_IMODE(root_opened.st_mode) != 0o700
                ):
                    raise RuntimeError("cleanup journal root identity is not private")
                marker_payload = {
                    "version": 1,
                    "path": str(path),
                    "ancestor_identities": [list(item) for item in ancestor_identities],
                    "root_identity": list(root_identity),
                }
                marker_bytes = json.dumps(
                    marker_payload,
                    ensure_ascii=True,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
                if len(marker_bytes) > _CLEANUP_JOURNAL_ROOT_MARKER_MAX_BYTES:
                    raise RuntimeError("cleanup journal root identity marker exceeds its limit")
                marker_fd = os.open(
                    marker_name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=parent_fd,
                )
                try:
                    self._write_all(
                        marker_fd,
                        marker_bytes,
                        label="cleanup journal root identity marker",
                    )
                    os.fchmod(marker_fd, 0o600)
                    os.fsync(marker_fd)
                finally:
                    os.close(marker_fd)
                os.fsync(root_fd)
                os.fsync(parent_fd)
            elif root_before is None or not marker_exists:
                raise RuntimeError("cleanup journal root identity authority is incomplete")
            else:
                marker_payload = self._read_cleanup_journal_root_marker(
                    parent_fd,
                    marker_name,
                )
                if (
                    not isinstance(marker_payload, dict)
                    or set(marker_payload)
                    != {"version", "path", "ancestor_identities", "root_identity"}
                    or marker_payload["version"] != 1
                    or marker_payload["path"] != str(path)
                    or marker_payload["ancestor_identities"]
                    != [list(item) for item in ancestor_identities]
                ):
                    raise RuntimeError(
                        "cleanup journal ancestor identity does not match authority"
                    )
                persisted_root_identity = marker_payload["root_identity"]
                if (
                    not isinstance(persisted_root_identity, list)
                    or len(persisted_root_identity) != 4
                    or any(not isinstance(item, int) for item in persisted_root_identity)
                ):
                    raise RuntimeError("cleanup journal root identity marker is invalid")
                root_fd = os.open(root_name, _CLEANUP_DIRECTORY_FLAGS, dir_fd=parent_fd)
                root_opened = os.fstat(root_fd)
                root_identity = self._cleanup_journal_identity(root_opened)
                if (
                    self._cleanup_journal_identity(root_before) != root_identity
                    or list(root_identity) != persisted_root_identity
                ):
                    raise RuntimeError("cleanup journal root identity does not match authority")
                if (
                    not stat.S_ISDIR(root_opened.st_mode)
                    or root_opened.st_uid != os.geteuid()
                    or stat.S_IMODE(root_opened.st_mode) != 0o700
                ):
                    raise RuntimeError("cleanup journal root identity is not private")

            return _CleanupJournalAuthority(
                path=path,
                ancestor_fds=ancestor_fds,
                ancestor_identities=tuple(ancestor_identities),
                root_fd=root_fd,
                root_identity=root_identity,
            )
        except Exception:
            if root_fd >= 0:
                os.close(root_fd)
            while ancestor_fds:
                os.close(ancestor_fds.pop())
            raise

    def _verify_cleanup_journal_authority(
        self,
        authority: _CleanupJournalAuthority,
    ) -> None:
        if any(
            self._cleanup_journal_identity(os.fstat(descriptor)) != expected
            for descriptor, expected in zip(
                authority.ancestor_fds,
                authority.ancestor_identities,
                strict=True,
            )
        ):
            raise RuntimeError("cleanup journal ancestor descriptor identity changed")
        if self._cleanup_journal_identity(os.fstat(authority.root_fd)) != authority.root_identity:
            raise RuntimeError("cleanup journal root descriptor identity changed")
        reopened = self._open_cleanup_journal_authority(initialize=False)
        if reopened is None:
            raise RuntimeError("cleanup journal root identity authority disappeared")
        try:
            if (
                reopened.ancestor_identities != authority.ancestor_identities
                or reopened.root_identity != authority.root_identity
            ):
                raise RuntimeError("cleanup journal root identity authority changed")
        finally:
            reopened.close()

    def _cleanup_journal_path(self, session_id: str) -> Path:
        name = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
        return self._cleanup_journal_dir / f"{name}.json"

    def _persist_cleanup_ownership(self, ownership: CleanupRetryOwnership) -> None:
        journal_dir = getattr(self, "_cleanup_journal_dir", None)
        if journal_dir is None:
            return
        journal_dir = Path(journal_dir)
        state = ownership.finalization_state
        delivery = ownership.delivery_state
        payload = {
            "version": 6,
            "session_id": ownership.session_id,
            "phase": ownership.phase,
            "runtime": {
                "runtime_id": ownership.runtime_id,
                "container_id": ownership.container_id,
            },
            "eval_runtime": {
                "runtime_id": ownership.eval_runtime_id,
                "container_id": ownership.eval_container_id,
            },
            "session_root": {
                "path": str(ownership.session_dir),
                "identity": list(ownership.session_root_identity or ()),
            },
            "log_root": (
                None
                if ownership.log_authority_dir is None
                else {
                    "path": str(ownership.log_authority_dir),
                    "identity": list(ownership.log_authority_identity or ()),
                }
            ),
            "credential_root": (
                None
                if ownership.credential_dir is None
                else {
                    "path": str(ownership.credential_dir),
                    "identity": list(ownership.credential_root_identity or ()),
                    "auth_identity": list(ownership.credential_auth_identity or ()),
                }
            ),
            "subscription_finalization": (
                None
                if state is None
                else {
                    "request": state.request.model_dump(mode="json"),
                    "agent_result": (
                        None
                        if state.agent_result is None
                        else state.agent_result.model_dump(mode="json")
                    ),
                    "final_result": (
                        None
                        if state.final_result is None or delivery is not None
                        else state.final_result.model_dump(mode="json")
                    ),
                    "pending_status": (
                        None if state.pending_status is None else str(state.pending_status)
                    ),
                    "pending_error": state.pending_error,
                    "cancel_requested": state.cancel_requested,
                    "timer_marks": state.timer_marks,
                }
            ),
            "terminal_delivery": (
                None
                if delivery is None
                else {
                    "result": delivery.result.model_dump(mode="json"),
                    "result_digest": delivery.result_digest,
                    "callback_url": delivery.callback_url,
                    "export_required": delivery.export_required,
                    "export_authority": (
                        None
                        if delivery.export_authority is None
                        else {
                            "backend_url": delivery.export_authority.backend_url,
                            "timeout_seconds": delivery.export_authority.timeout_seconds,
                            "fail_open": delivery.export_authority.fail_open,
                            "identity_digest": delivery.export_authority.identity_digest,
                        }
                    ),
                    "export_succeeded": delivery.export_succeeded,
                    "callback_required": delivery.callback_required,
                    "callback_succeeded": delivery.callback_succeeded,
                }
            ),
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(encoded) > _CLEANUP_JOURNAL_MAX_BYTES:
            raise RuntimeError("cleanup ownership journal exceeds the byte limit")
        authority = self._open_cleanup_journal_authority(initialize=True)
        if authority is None:
            raise RuntimeError("cleanup journal root identity authority is unavailable")
        try:
            self._verify_cleanup_journal_authority(authority)
        except Exception:
            authority.close()
            raise
        destination = self._cleanup_journal_path(ownership.session_id)
        temporary = destination.with_name(f".{destination.name}.{os.getpid()}.{id(ownership)}.tmp")
        pending = destination.with_suffix(".pending")
        rollback = destination.with_name(f".{destination.name}.rollback.tmp")
        try:
            previous = self._read_private_cleanup_file(
                destination,
                max_bytes=_CLEANUP_JOURNAL_MAX_BYTES,
                allow_missing=True,
            )
        except Exception:
            authority.close()
            raise
        replaced = False
        descriptor = -1
        try:
            self._ensure_cleanup_pending_marker(pending, journal_dir)

            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                0o600,
            )
            offset = 0
            while offset < len(encoded):
                written = os.write(descriptor, encoded[offset:])
                if written <= 0:
                    raise RuntimeError("cleanup ownership journal write made no progress")
                offset += written
            os.fchmod(descriptor, 0o600)
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1
            os.replace(temporary, destination)
            replaced = True
            self._fsync_cleanup_journal_directory(journal_dir)
            pending.unlink()
            self._fsync_cleanup_journal_directory(journal_dir)
            self._verify_cleanup_journal_authority(authority)
        except Exception:
            if descriptor >= 0:
                os.close(descriptor)
                descriptor = -1
            if replaced:
                try:
                    if previous is None:
                        destination.unlink(missing_ok=True)
                    else:
                        rollback_fd = os.open(
                            rollback,
                            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                            0o600,
                        )
                        try:
                            offset = 0
                            while offset < len(previous):
                                written = os.write(rollback_fd, previous[offset:])
                                if written <= 0:
                                    raise RuntimeError(
                                        "cleanup ownership journal rollback made no progress"
                                    )
                                offset += written
                            os.fsync(rollback_fd)
                        finally:
                            os.close(rollback_fd)
                        os.replace(rollback, destination)
                    self._fsync_cleanup_journal_directory(journal_dir)
                except Exception:
                    logger.error(
                        "Cleanup ownership journal rollback could not be proven for %s",
                        ownership.session_id,
                    )
            try:
                self._ensure_cleanup_pending_marker(pending, journal_dir)
            except OSError:
                logger.error(
                    "Cleanup ownership journal pending marker could not be proven for %s",
                    ownership.session_id,
                )
            raise
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            for leftover in (temporary, rollback):
                try:
                    leftover.unlink()
                except FileNotFoundError:
                    pass
            authority.close()

    @staticmethod
    def _fsync_cleanup_journal_directory(journal_dir: Path) -> None:
        directory_fd = os.open(
            journal_dir,
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
        )
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    @classmethod
    def _ensure_cleanup_pending_marker(
        cls,
        pending: Path,
        journal_dir: Path,
    ) -> None:
        descriptor = -1
        try:
            try:
                descriptor = os.open(
                    pending,
                    os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                    0o600,
                )
                marker = b"pending\n"
                if os.write(descriptor, marker) != len(marker):
                    raise RuntimeError(
                        "cleanup ownership journal pending marker write was incomplete"
                    )
            except FileExistsError:
                descriptor = os.open(
                    pending,
                    os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
                )
                state = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(state.st_mode)
                    or state.st_uid != os.geteuid()
                    or state.st_nlink != 1
                    or stat.S_IMODE(state.st_mode) != 0o600
                    or state.st_size not in {len(b"pending\n"), len(b"blocked\n")}
                ):
                    raise RuntimeError("cleanup ownership journal pending marker is invalid")
                content = os.read(descriptor, state.st_size)
                if content not in {b"pending\n", b"blocked\n"} or os.read(descriptor, 1):
                    raise RuntimeError("cleanup ownership journal pending marker is invalid")
            os.fsync(descriptor)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        cls._fsync_cleanup_journal_directory(journal_dir)

    @staticmethod
    def _read_private_cleanup_file(
        path: Path,
        *,
        max_bytes: int,
        allow_missing: bool,
    ) -> bytes | None:
        try:
            descriptor = os.open(
                path,
                os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
            )
        except FileNotFoundError:
            if allow_missing:
                return None
            raise
        try:
            state = os.fstat(descriptor)
            if (
                not stat.S_ISREG(state.st_mode)
                or state.st_uid != os.geteuid()
                or state.st_nlink != 1
                or stat.S_IMODE(state.st_mode) != 0o600
                or state.st_size <= 0
                or state.st_size > max_bytes
            ):
                raise RuntimeError("cleanup ownership journal file is not private")
            chunks: list[bytes] = []
            remaining = state.st_size
            while remaining:
                chunk = os.read(descriptor, remaining)
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            if remaining or os.read(descriptor, 1):
                raise RuntimeError("cleanup ownership journal changed during read")
            return b"".join(chunks)
        finally:
            os.close(descriptor)

    def _read_cleanup_journal_record(
        self,
        authority: _CleanupJournalAuthority,
        name: str,
        expected: tuple[int, int, int, int, int, int],
    ) -> bytes:
        descriptor = os.open(
            name,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=authority.root_fd,
        )
        try:
            opened = os.fstat(descriptor)
            actual = (
                opened.st_dev,
                opened.st_ino,
                opened.st_uid,
                opened.st_mode,
                opened.st_nlink,
                opened.st_size,
            )
            if actual != expected:
                raise RuntimeError("cleanup ownership journal changed before read")
            chunks: list[bytes] = []
            remaining = opened.st_size
            while remaining:
                chunk = os.read(descriptor, remaining)
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            after = os.stat(name, dir_fd=authority.root_fd, follow_symlinks=False)
            rebound = (
                after.st_dev,
                after.st_ino,
                after.st_uid,
                after.st_mode,
                after.st_nlink,
                after.st_size,
            )
            if remaining or os.read(descriptor, 1) or rebound != expected:
                raise RuntimeError("cleanup ownership journal changed during read")
            return b"".join(chunks)
        finally:
            os.close(descriptor)

    def _load_cleanup_retries(self) -> None:
        authority = self._open_cleanup_journal_authority(initialize=False)
        if authority is None:
            return
        try:
            records: list[tuple[str, tuple[int, int, int, int, int, int]]] = []
            row_count = 0
            metadata_bytes = 0
            total_bytes = 0
            with os.scandir(authority.root_fd) as entries:
                for entry in entries:
                    row_count += 1
                    if row_count > _CLEANUP_JOURNAL_MAX_ROWS:
                        raise RuntimeError("cleanup ownership journal exceeds the row budget")
                    name = entry.name
                    filename_bytes = os.fsencode(name)
                    if len(filename_bytes) > _CLEANUP_JOURNAL_MAX_FILENAME_BYTES:
                        raise RuntimeError("cleanup ownership journal exceeds the filename budget")
                    metadata_bytes += len(filename_bytes) + 6 * 8
                    if metadata_bytes > _CLEANUP_JOURNAL_MAX_METADATA_BYTES:
                        raise RuntimeError("cleanup ownership journal exceeds the metadata budget")
                    if _CLEANUP_JOURNAL_PENDING_RE.fullmatch(name) is not None:
                        raise RuntimeError("cleanup ownership journal has an incomplete update")
                    if _CLEANUP_JOURNAL_RECORD_RE.fullmatch(name) is None:
                        raise RuntimeError(
                            "cleanup ownership journal filename metadata is invalid"
                        )
                    opened = entry.stat(follow_symlinks=False)
                    if (
                        not stat.S_ISREG(opened.st_mode)
                        or opened.st_uid != os.geteuid()
                        or opened.st_nlink != 1
                        or stat.S_IMODE(opened.st_mode) != 0o600
                        or opened.st_size <= 0
                        or opened.st_size > _CLEANUP_JOURNAL_MAX_BYTES
                    ):
                        raise RuntimeError("cleanup ownership journal file metadata is invalid")
                    total_bytes += opened.st_size
                    if total_bytes > _CLEANUP_JOURNAL_MAX_TOTAL_BYTES:
                        raise RuntimeError(
                            "cleanup ownership journal exceeds the aggregate byte budget"
                        )
                    records.append(
                        (
                            name,
                            (
                                opened.st_dev,
                                opened.st_ino,
                                opened.st_uid,
                                opened.st_mode,
                                opened.st_nlink,
                                opened.st_size,
                            ),
                        )
                    )

            self._verify_cleanup_journal_authority(authority)
            recovered: dict[str, CleanupRetryOwnership] = {}
            for name, expected in sorted(records):
                raw = self._read_cleanup_journal_record(authority, name, expected)
                path = authority.path / name
                try:
                    payload = json.loads(raw.decode("utf-8"))
                    ownership = self._cleanup_ownership_from_payload(payload, path)
                except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
                    raise RuntimeError("cleanup ownership journal is invalid") from exc
                if (
                    ownership.session_id in recovered
                    or ownership.session_id in self._cleanup_retries
                ):
                    raise RuntimeError("cleanup ownership journal session identity is duplicated")
                recovered[ownership.session_id] = ownership
            self._verify_cleanup_journal_authority(authority)
            self._cleanup_retries.update(recovered)
        finally:
            authority.close()

    @staticmethod
    def _cleanup_ownership_from_payload(
        payload: object,
        path: Path,
    ) -> CleanupRetryOwnership:
        if not isinstance(payload, dict):
            raise ValueError("cleanup ownership payload is not closed")
        version = payload.get("version")
        expected_keys = {
            "version",
            "session_id",
            "runtime",
            "eval_runtime",
            "session_root",
            "credential_root",
        }
        if version in {2, 3, 4, 5, 6}:
            expected_keys.add("log_root")
        if version in {3, 4, 5, 6}:
            expected_keys.add("subscription_finalization")
        if version in {4, 5, 6}:
            expected_keys.add("terminal_delivery")
        if version in {5, 6}:
            expected_keys.add("phase")
        if set(payload) != expected_keys:
            raise ValueError("cleanup ownership payload is not closed")
        if version not in {1, 2, 3, 4, 5, 6} or not isinstance(payload["session_id"], str):
            raise ValueError("cleanup ownership identity is invalid")
        phase = payload.get("phase")
        if version in {5, 6} and phase not in _RECOVERY_PHASES:
            raise ValueError("cleanup recovery phase is invalid")
        if version not in {5, 6}:
            phase = None
        session_id = payload["session_id"]
        expected_name = f"{hashlib.sha256(session_id.encode('utf-8')).hexdigest()}.json"
        if path.name != expected_name:
            raise ValueError("cleanup ownership filename does not match session identity")

        def runtime_identity(value: object) -> tuple[str | None, str | None]:
            if not isinstance(value, dict) or set(value) != {
                "runtime_id",
                "container_id",
            }:
                raise ValueError("cleanup runtime identity is invalid")
            runtime_id = value["runtime_id"]
            container_id = value["container_id"]
            if runtime_id is not None and not isinstance(runtime_id, str):
                raise ValueError("cleanup runtime ID is invalid")
            if container_id is not None and not isinstance(container_id, str):
                raise ValueError("cleanup container ID is invalid")
            return runtime_id, container_id

        def root_identity(value: object) -> tuple[Path, tuple[int, int, int]]:
            if not isinstance(value, dict) or set(value) != {"path", "identity"}:
                raise ValueError("cleanup root identity is invalid")
            root_path = Path(value["path"])
            identity = value["identity"]
            if (
                not root_path.is_absolute()
                or not isinstance(identity, list)
                or len(identity) != 3
                or any(not isinstance(item, int) for item in identity)
            ):
                raise ValueError("cleanup root identity is invalid")
            return root_path, tuple(identity)

        runtime_id, container_id = runtime_identity(payload["runtime"])
        eval_runtime_id, eval_container_id = runtime_identity(payload["eval_runtime"])
        session_dir, session_identity = root_identity(payload["session_root"])
        log_payload = payload.get("log_root")
        if log_payload is None:
            log_authority_dir = None
            log_authority_identity = None
        else:
            log_authority_dir, log_authority_identity = root_identity(log_payload)
        credential_payload = payload["credential_root"]
        if credential_payload is None:
            credential_dir = None
            credential_identity = None
            credential_auth_identity = None
        else:
            if version == 6:
                if not isinstance(credential_payload, dict) or set(credential_payload) != {
                    "path",
                    "identity",
                    "auth_identity",
                }:
                    raise ValueError("cleanup credential identity is invalid")
                credential_dir, credential_identity = root_identity(
                    {
                        "path": credential_payload["path"],
                        "identity": credential_payload["identity"],
                    }
                )
                auth_identity = credential_payload["auth_identity"]
                if not isinstance(auth_identity, list) or (
                    auth_identity
                    and (
                        len(auth_identity) != 8
                        or any(not isinstance(item, int) for item in auth_identity)
                    )
                ):
                    raise ValueError("cleanup credential auth identity is invalid")
                credential_auth_identity = tuple(auth_identity) if auth_identity else None
            else:
                credential_dir, credential_identity = root_identity(credential_payload)
                credential_auth_identity = None
        finalization_state = None
        finalization_payload = payload.get("subscription_finalization")
        if finalization_payload is not None:
            finalization_keys = {
                "request",
                "agent_result",
                "final_result",
                "pending_status",
                "pending_error",
                "cancel_requested",
                "timer_marks",
            }
            if (
                not isinstance(finalization_payload, dict)
                or set(finalization_payload) != finalization_keys
                or not isinstance(finalization_payload["cancel_requested"], bool)
                or (
                    finalization_payload["pending_error"] is not None
                    and not isinstance(finalization_payload["pending_error"], str)
                )
                or not isinstance(finalization_payload["timer_marks"], dict)
            ):
                raise ValueError("subscription finalization payload is invalid")
            timer_marks = finalization_payload["timer_marks"]
            if any(
                not isinstance(key, str)
                or isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                for key, value in timer_marks.items()
            ):
                raise ValueError("subscription finalization timer is invalid")
            pending_status_payload = finalization_payload["pending_status"]
            pending_status = (
                None if pending_status_payload is None else SessionStatus(pending_status_payload)
            )
            if pending_status is not None and pending_status not in {
                SessionStatus.ERROR,
                SessionStatus.TIMEOUT,
            }:
                raise ValueError("subscription pending status is invalid")
            request = SessionDispatchRequest.model_validate(finalization_payload["request"])
            if request.session_id != session_id or not _is_codex_subscription_agent(request.agent):
                raise ValueError("subscription finalization request is invalid")
            agent_payload = finalization_payload["agent_result"]
            result_payload = finalization_payload["final_result"]
            agent_result = (
                None if agent_payload is None else AgentRunResult.model_validate(agent_payload)
            )
            final_result = (
                None if result_payload is None else SessionResult.model_validate(result_payload)
            )
            if final_result is not None and final_result.session_id != session_id:
                raise ValueError("subscription terminal result identity is invalid")
            finalization_state = SubscriptionFinalizationState(
                request=request,
                agent_result=agent_result,
                final_result=final_result,
                pending_status=pending_status,
                pending_error=finalization_payload["pending_error"],
                cancel_requested=finalization_payload["cancel_requested"],
                timer_marks={key: float(value) for key, value in timer_marks.items()},
            )
        delivery_state = None
        delivery_payload = payload.get("terminal_delivery")
        if delivery_payload is not None:
            delivery_keys = {
                "result",
                "result_digest",
                "callback_url",
                "export_required",
                "export_succeeded",
                "callback_required",
                "callback_succeeded",
            }
            if version in {5, 6}:
                delivery_keys.add("export_authority")
            if (
                not isinstance(delivery_payload, dict)
                or set(delivery_payload) != delivery_keys
                or not isinstance(delivery_payload["result_digest"], str)
                or re.fullmatch(r"[0-9a-f]{64}", delivery_payload["result_digest"]) is None
                or (
                    delivery_payload["callback_url"] is not None
                    and not isinstance(delivery_payload["callback_url"], str)
                )
                or any(
                    not isinstance(delivery_payload[key], bool)
                    for key in (
                        "export_required",
                        "export_succeeded",
                        "callback_required",
                        "callback_succeeded",
                    )
                )
            ):
                raise ValueError("terminal delivery payload is invalid")
            result = SessionResult.model_validate(delivery_payload["result"])
            if result.session_id != session_id:
                raise ValueError("terminal delivery session identity is invalid")
            result_digest = GatewayNodeManager._terminal_result_digest(result)
            if result_digest != delivery_payload["result_digest"]:
                raise ValueError("terminal delivery result digest is invalid")
            callback_url = delivery_payload["callback_url"]
            export_required = delivery_payload["export_required"]
            export_authority = None
            authority_payload = delivery_payload.get("export_authority")
            if authority_payload is not None:
                authority_keys = {
                    "backend_url",
                    "timeout_seconds",
                    "fail_open",
                    "identity_digest",
                }
                if (
                    not isinstance(authority_payload, dict)
                    or set(authority_payload) != authority_keys
                    or not isinstance(authority_payload["backend_url"], str)
                    or not authority_payload["backend_url"]
                    or isinstance(authority_payload["timeout_seconds"], bool)
                    or not isinstance(authority_payload["timeout_seconds"], (int, float))
                    or not math.isfinite(float(authority_payload["timeout_seconds"]))
                    or float(authority_payload["timeout_seconds"]) <= 0
                    or not isinstance(authority_payload["fail_open"], bool)
                    or not isinstance(authority_payload["identity_digest"], str)
                ):
                    raise ValueError("terminal export authority is invalid")
                backend_url = authority_payload["backend_url"]
                timeout_seconds = float(authority_payload["timeout_seconds"])
                fail_open = authority_payload["fail_open"]
                identity_digest = GatewayNodeManager._export_authority_digest(
                    backend_url=backend_url,
                    timeout_seconds=timeout_seconds,
                    fail_open=fail_open,
                )
                if identity_digest != authority_payload["identity_digest"]:
                    raise ValueError("terminal export authority digest is invalid")
                export_authority = EvolutionExportAuthority(
                    backend_url=backend_url,
                    timeout_seconds=timeout_seconds,
                    fail_open=fail_open,
                    identity_digest=identity_digest,
                )
            export_succeeded = delivery_payload["export_succeeded"]
            callback_required = delivery_payload["callback_required"]
            callback_succeeded = delivery_payload["callback_succeeded"]
            if callback_required != (callback_url is not None):
                raise ValueError("terminal delivery callback authority is invalid")
            if version in {5, 6} and export_required != (export_authority is not None):
                raise ValueError("terminal delivery export authority is invalid")
            if (not export_required and not export_succeeded) or (
                not callback_required and not callback_succeeded
            ):
                raise ValueError("terminal delivery skipped phase is not complete")
            if (
                finalization_state is not None
                and finalization_state.final_result is not None
                and finalization_state.final_result != result
            ):
                raise ValueError("terminal delivery finalization result changed")
            delivery_state = TerminalDeliveryState(
                result=result,
                result_digest=result_digest,
                callback_url=callback_url,
                export_required=export_required,
                export_authority=export_authority,
                export_succeeded=export_succeeded,
                callback_required=callback_required,
                callback_succeeded=callback_succeeded,
            )
        if phase == _RECOVERY_PHASE_TERMINAL_FINALIZATION and (
            finalization_state is None or delivery_state is not None
        ):
            raise ValueError("terminal finalization phase authority is invalid")
        if phase == _RECOVERY_PHASE_TERMINAL_DELIVERY and delivery_state is None:
            raise ValueError("terminal delivery phase authority is invalid")
        if phase == _RECOVERY_PHASE_RUNTIME_ACTIVE and (
            finalization_state is not None or delivery_state is not None
        ):
            raise ValueError("runtime-active phase authority is invalid")
        if (
            version < 6
            and credential_dir is not None
            and phase == _RECOVERY_PHASE_TERMINAL_FINALIZATION
        ):
            raise ValueError("legacy credential finalization lacks exact auth identity authority")
        return CleanupRetryOwnership(
            session_id=session_id,
            session_dir=session_dir,
            session_root_identity=session_identity,
            log_authority_dir=log_authority_dir,
            log_authority_identity=log_authority_identity,
            credential_dir=credential_dir,
            credential_root_identity=credential_identity,
            credential_auth_identity=credential_auth_identity,
            runtime_id=runtime_id,
            container_id=container_id,
            eval_runtime_id=eval_runtime_id,
            eval_container_id=eval_container_id,
            runtime=None,
            phase=phase,
            eval_runtime=None,
            finalize_subscription=finalization_state is not None,
            finalization_state=finalization_state,
            delivery_state=delivery_state,
        )

    def _clear_cleanup_ownership(self, session_id: str) -> None:
        authority = self._open_cleanup_journal_authority(initialize=False)
        if authority is None:
            return
        try:
            name = self._cleanup_journal_path(session_id).name
            try:
                os.unlink(name, dir_fd=authority.root_fd)
            except FileNotFoundError:
                return
            os.fsync(authority.root_fd)
            self._verify_cleanup_journal_authority(authority)
        finally:
            authority.close()

    async def _stop_recovered_container(
        self,
        container_id: str,
        runtime_id: str | None,
    ) -> bool:
        ownership_root = getattr(self, "_docker_ownership_root", None)
        recovery_session_dir = (
            ownership_root.parent / ".cleanup-recovery-session"
            if ownership_root is not None
            else Path("/tmp/.openevo-cleanup-recovery-session")
        )
        runtime = DockerRuntime(
            RuntimeSpec(image="openevo-cleanup-recovery", container_user="host"),
            runtime_id or f"recovered-{container_id[:12]}",
            recovery_session_dir,
            ownership_root=ownership_root,
        )
        runtime._container_id = container_id
        runtime._ownership_state = "candidate"
        return await self._stop_runtime_best_effort(
            runtime,
            runtime_id or container_id,
            "recovered runtime",
        )

    async def _cleanup_retry_loop(self) -> None:
        while True:
            await asyncio.sleep(_CLEANUP_RETRY_INTERVAL_SECONDS)
            await self._reconcile_cleanup_retries()

    async def _reconcile_cleanup_retries(self) -> None:
        retries = getattr(self, "_cleanup_retries", None)
        if not retries:
            return
        lock = getattr(self, "_cleanup_reconcile_lock", None)
        if lock is None:
            lock = asyncio.Lock()
            self._cleanup_reconcile_lock = lock
        async with lock:
            for session_id, ownership in list(retries.items()):
                try:
                    await self._reconcile_cleanup_ownership(ownership)
                except Exception as exc:
                    self._log_credential_safe_exception(
                        ownership.managed,
                        "Cleanup reconciliation failed",
                        exc,
                        session_id=session_id,
                        level=logging.WARNING,
                    )

    def _restore_subscription_finalization(
        self,
        ownership: CleanupRetryOwnership,
    ) -> ManagedSession:
        state = ownership.finalization_state
        if state is None:
            raise RuntimeError("subscription finalization authority is missing")
        if (
            ownership.credential_dir is None
            or ownership.credential_root_identity is None
            or ownership.credential_auth_identity is None
        ):
            raise RuntimeError("subscription credential authority is missing")
        redactor = load_staged_codex_subscription_redactor(
            ownership.credential_dir,
            ownership.credential_root_identity,
            ownership.credential_auth_identity,
        )
        timer = StageTimer()
        timer._marks = dict(state.timer_marks)
        managed = ManagedSession(
            request=state.request,
            timer=timer,
            session_dir=ownership.session_dir,
            artifacts_dir=ownership.session_dir / "artifacts",
            session_root_identity=ownership.session_root_identity,
            log_authority_dir=ownership.log_authority_dir,
            log_authority_identity=ownership.log_authority_identity,
            credential_dir=ownership.credential_dir,
            credential_root_identity=ownership.credential_root_identity,
            credential_auth_identity=ownership.credential_auth_identity,
            credential_mount=(
                None
                if ownership.credential_auth_identity is None
                else ManagedCredentialMount(
                    root=ownership.credential_dir,
                    root_identity=ownership.credential_root_identity,
                    auth_identity=ownership.credential_auth_identity,
                )
            ),
            credential_redactor=redactor,
            agent_result=state.agent_result,
            final_result=state.final_result,
            pending_status=state.pending_status,
            pending_error=state.pending_error,
            cancel_requested=state.cancel_requested,
            stage=SessionStage.POSTRUN,
        )
        if self.session_registry.get(managed.session_id) is None:
            self.session_registry.register(
                managed.session_id,
                task_id=managed.request.task_id,
                registered=True,
                status=SessionStatus.POST_RUN,
                metadata=dict(managed.request.metadata),
            )
        return managed

    async def _reconcile_cleanup_ownership(
        self,
        ownership: CleanupRetryOwnership,
    ) -> None:
        session_id = ownership.session_id
        retries = self._cleanup_retries
        managed = ownership.managed
        if managed is None:
            recovered_targets = [
                (ownership.eval_container_id, ownership.eval_runtime_id),
                (ownership.container_id, ownership.runtime_id),
            ]
            recovered_outcomes = await asyncio.gather(
                *(
                    self._stop_recovered_container(container_id, runtime_id)
                    for container_id, runtime_id in recovered_targets
                    if container_id is not None
                ),
                return_exceptions=True,
            )
            expected_runtime_count = sum(
                runtime_id is not None or container_id is not None
                for container_id, runtime_id in recovered_targets
            )
            if len(recovered_outcomes) != expected_runtime_count or not all(
                outcome is True for outcome in recovered_outcomes
            ):
                return
            if ownership.phase is None:
                logger.error(
                    "Retaining cleanup roots because recovery phase authority is missing "
                    "for session %s",
                    session_id,
                )
                return
            self._record_cleanup_ownership_runtimes_absent(ownership)
            if ownership.phase == _RECOVERY_PHASE_TERMINAL_DELIVERY:
                delivered = await self._resume_terminal_delivery(ownership)
                if not delivered:
                    return
            elif ownership.phase == _RECOVERY_PHASE_TERMINAL_FINALIZATION:
                managed = self._restore_subscription_finalization(ownership)
                ownership.managed = managed
                await self._finalize_subscription_after_runtime_absence(
                    managed,
                    result=ownership.finalization_state.final_result,
                )
                return
            if ownership.credential_dir is not None:
                credential_removed = await self._remove_credential_dir_best_effort(
                    ownership.credential_dir,
                    session_id,
                    ownership.credential_root_identity,
                    ownership.credential_auth_identity,
                )
            else:
                credential_removed = True
            root_tasks = [
                self._remove_session_dir_best_effort(
                    ownership.session_dir,
                    session_id,
                    ownership.session_root_identity,
                )
            ]
            if ownership.log_authority_dir is not None:
                root_tasks.append(
                    self._remove_log_authority_best_effort(
                        ownership.log_authority_dir,
                        session_id,
                        ownership.log_authority_identity,
                    )
                )
            root_outcomes = await asyncio.gather(
                *root_tasks,
                return_exceptions=True,
            )
            if credential_removed and all(
                outcome is not False and not isinstance(outcome, BaseException)
                for outcome in root_outcomes
            ):
                retries.pop(session_id, None)
                self._clear_cleanup_ownership(session_id)
            return
        targets = [
            (ownership.eval_runtime, "eval runtime cleanup retry"),
            (ownership.runtime, "runtime cleanup retry"),
        ]
        outcomes = await asyncio.gather(
            *(
                self._stop_runtime_best_effort(runtime, session_id, label)
                for runtime, label in targets
                if runtime is not None
            ),
            return_exceptions=True,
        )
        if not all(outcome is True for outcome in outcomes):
            return
        if ownership.phase is None:
            logger.error(
                "Retaining cleanup roots because recovery phase authority is missing "
                "for session %s",
                session_id,
            )
            return
        ownership = self._record_cleanup_ownership_runtimes_absent(ownership)
        if ownership.phase == _RECOVERY_PHASE_TERMINAL_DELIVERY:
            delivered = await self._resume_terminal_delivery(ownership)
            if not delivered:
                return
        elif ownership.phase == _RECOVERY_PHASE_TERMINAL_FINALIZATION:
            await self._finalize_subscription_after_runtime_absence(
                managed,
                result=managed.final_result,
            )
            return
        if await self._remove_owned_roots(managed):
            retries.pop(session_id, None)
            self._clear_cleanup_ownership(session_id)

    async def _remove_session_dir_best_effort(
        self,
        session_dir: Path,
        session_id: str,
        session_root_identity: tuple[int, int, int] | None = None,
    ) -> bool:
        try:
            identity = session_root_identity or capture_session_root_identity(session_dir)
            await asyncio.to_thread(remove_session_tree, session_dir, identity)
            return True
        except FileNotFoundError:
            return True
        except Exception as exc:
            self._log_credential_safe_exception(
                None,
                "Failed to remove session directory",
                exc,
                session_id=session_id,
                level=logging.WARNING,
            )
            return False

    async def _remove_credential_dir_best_effort(
        self,
        credential_dir: Path,
        session_id: str,
        credential_root_identity: tuple[int, int, int] | None = None,
        credential_auth_identity: CredentialFileIdentity | None = None,
    ) -> bool:
        try:
            identity = credential_root_identity or capture_session_root_identity(credential_dir)
            await asyncio.to_thread(
                remove_credential_tree,
                credential_dir,
                identity,
                credential_auth_identity,
            )
            return True
        except FileNotFoundError:
            return True
        except Exception as exc:
            self._log_credential_safe_exception(
                None,
                "Failed to remove credential directory",
                exc,
                session_id=session_id,
                level=logging.WARNING,
            )
            return False

    async def _remove_log_authority_best_effort(
        self,
        log_authority_dir: Path,
        session_id: str,
        log_authority_identity: tuple[int, int, int] | None = None,
    ) -> bool:
        try:
            identity = log_authority_identity or capture_session_root_identity(log_authority_dir)
            await asyncio.to_thread(
                remove_session_tree,
                log_authority_dir,
                identity,
            )
            for parent in (
                log_authority_dir.parent,
                log_authority_dir.parent.parent,
            ):
                try:
                    parent.rmdir()
                except OSError:
                    break
            return True
        except FileNotFoundError:
            return True
        except Exception as exc:
            self._log_credential_safe_exception(
                None,
                "Failed to remove log authority",
                exc,
                session_id=session_id,
                level=logging.WARNING,
            )
            return False
