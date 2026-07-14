"""Gateway-node execution lifecycle for dispatched rollout sessions."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import shlex
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory, mkdtemp
from typing import Any
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
    CredentialRedactor,
    SessionFileSecurityError,
    VerifiedSessionTranscript,
    capture_session_root_identity,
    create_session_log_authority,
    read_verified_session_transcript,
    redact_session_capture_tree,
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


class GatewayExecutionTimeout(TimeoutError):
    """Raised when a session exhausts its shared gateway execution budget."""


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
    runtime_id: str | None
    container_id: str | None
    eval_runtime_id: str | None
    eval_container_id: str | None
    runtime: BaseRuntime | None
    eval_runtime: BaseRuntime | None = None
    managed: ManagedSession | None = None
    finalize_subscription: bool = False


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
            agent_system_env["OPENEVO_AGENT_SYSTEM_FILE"] = (
                f"{target_dir}/agent_system.md"
            )
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
        self._cleanup_retries: dict[str, CleanupRetryOwnership] = {}
        cleanup_base = (
            Path(session_base_dir).absolute() if session_base_dir else Path("/tmp")
        )
        node_key = hashlib.sha256(node_id.encode("utf-8")).hexdigest()[:24]
        self._cleanup_journal_dir = (
            cleanup_base / ".openevo-gateway-cleanup" / node_key
        )
        self._docker_ownership_root = (
            cleanup_base / ".openevo-gateway-docker-ownership" / node_key
        )
        self._log_authority_root = (
            cleanup_base / ".openevo-gateway-log-authority" / node_key
        )

    async def start(self) -> None:
        await DockerRuntime.recover_ownership_root(self._docker_ownership_root)
        self._load_cleanup_retries()
        await self._reconcile_cleanup_retries()
        await self._dispatcher.start()
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
            log_authority_dir, log_authority_identity = (
                create_session_log_authority(
                    self._log_authority_root,
                    session_id,
                )
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
                managed.credential_root_identity = capture_session_root_identity(
                    credential_dir
                )
                self._persist_cleanup_ownership(
                    self._cleanup_ownership_for(managed)
                )
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
                    credential_dir=managed.credential_dir,
                    docker_ownership_root=self._docker_ownership_root,
                )
            managed.runtime = runtime
            await self._await_with_budget(runtime.start(), managed)
            self._persist_cleanup_ownership(self._cleanup_ownership_for(managed))
            # Run ordered prepare actions
            await self._run_runtime_prepare(runtime, runtime_spec, request, managed)
            if managed.credential_dir is not None:
                managed.credential_redactor = self._stage_codex_subscription_auth(
                    request,
                    managed.credential_dir,
                    managed.credential_root_identity,
                )
                self._redact_session_captures(managed)
        except GatewayExecutionTimeout as exc:
            self._set_terminal_failure(
                managed,
                SessionStatus.TIMEOUT,
                str(exc),
            )
        except Exception as exc:
            if managed.cancel_requested:
                logger.info("Initialization cancelled for session %s", request.session_id)
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
            if is_managed and (
                runtime_spec.import_path is not None or runtime_spec.kwargs
            ):
                raise ValueError(
                    "Core-managed runtime profiles forbid custom runtime loaders "
                    "and options"
                )
        except ValueError as exc:
            raise RuntimeError(f"runtime admission failed: {exc}") from exc

        try:
            root_state = managed.session_dir.stat(follow_symlinks=False)
            expected = managed.session_root_identity
            if expected is not None and (
                root_state.st_dev,
                root_state.st_ino,
                root_state.st_uid,
            ) != expected:
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
    ) -> CredentialRedactor:
        if not _is_codex_subscription_agent(request.agent):
            raise RuntimeError("credential staging requires a Codex subscription agent")

        identity = credential_root_identity or capture_session_root_identity(
            credential_dir
        )
        try:
            return stage_codex_subscription_auth(
                source=Path.home() / ".codex" / "auth.json",
                session_dir=credential_dir,
                session_identity=identity,
                target_home_parts=(),
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
            raise RuntimeError(
                "subscription execution requires capture_mode='transcript'"
            )
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
            raise RuntimeError(
                "subscription execution forbids custom runtime loaders and options"
            )

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
    def _redact_session_captures(managed: ManagedSession) -> None:
        redactor = managed.credential_redactor
        if redactor is None:
            return
        roots = [
            (managed.session_dir, managed.session_root_identity),
            (managed.log_authority_dir, managed.log_authority_identity),
        ]
        for root, identity in roots:
            if root is not None and identity is not None:
                redact_session_capture_tree(root, identity, redactor)

    def _ensure_log_authority(self, managed: ManagedSession) -> Path:
        if (
            managed.log_authority_dir is not None
            and managed.log_authority_identity is not None
        ):
            return managed.log_authority_dir
        if (
            managed.log_authority_dir is not None
            or managed.log_authority_identity is not None
        ):
            raise SessionFileSecurityError("session log authority is incomplete")
        authority_root = getattr(self, "_log_authority_root", None)
        if authority_root is None:
            authority_root = (
                managed.session_dir.parent
                / ".openevo-gateway-log-authority-direct"
            )
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
            managed.agent_result = agent_result

            # Postprocess always runs so harnesses can collect artifacts from
            # failed or timed-out agent runs before post-run evaluation.
            await self._await_with_budget(harness.postprocess(runtime, agent_result), managed)
            self._redact_session_captures(managed)

        except GatewayExecutionTimeout as exc:
            # Don't set final_result — let _handle_postrun build a partial
            # trajectory from the completions captured so far.
            if managed.agent_result is None:
                managed.agent_result = AgentRunResult(
                    status="timeout",
                    return_code=-1,
                    error=str(exc),
                )
            else:
                managed.agent_result = managed.agent_result.model_copy(
                    update={
                        "status": "timeout",
                        "return_code": -1,
                        "error": managed.agent_result.error or str(exc),
                    }
                )
        except Exception as exc:
            if managed.cancel_requested:
                logger.info("Agent execution cancelled for session %s", request.session_id)
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
            logger.warning(
                "Evolution context resolution failed for session %s: %s",
                request.session_id,
                exc,
            )
            return {}

    async def _export_evolution_event(self, result: SessionResult) -> None:
        if (
            self.evolution is None
            or not self.evolution.enabled
            or not self.evolution.event_export.enabled
            or self.evolution_client is None
        ):
            return
        try:
            await asyncio.wait_for(
                self.evolution_client.export_event(build_evolution_session_event(result)),
                timeout=self.evolution.event_export.timeout_seconds,
            )
        except Exception as exc:
            if not self.evolution.event_export.fail_open:
                raise
            logger.warning(
                "Evolution event export failed for session %s: %s",
                result.session_id,
                exc,
            )

    async def _run_exec_inputs(
        self,
        runtime: BaseRuntime,
        steps: list[ExecInput],
        env: dict[str, str],
        managed: ManagedSession,
    ) -> AgentRunResult:
        """Execute a list of ExecInput steps and return an AgentRunResult."""
        log_dir = self._ensure_log_authority(managed) / "logs" / "agent"

        for i, step in enumerate(steps):
            if managed.cancel_requested:
                return AgentRunResult(status="failed", return_code=-1, error="cancelled")
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
                return AgentRunResult(
                    status="timeout",
                    return_code=-1,
                    error=f"step {i} timed out",
                    metadata=self._step_metadata(log_dir, i, managed),
                )
            if result.return_code != 0:
                return AgentRunResult(
                    status="failed",
                    return_code=result.return_code,
                    error=f"step {i} exited with code {result.return_code}",
                    metadata=self._step_metadata(log_dir, i, managed),
                )

        return AgentRunResult(
            status="completed",
            return_code=0,
            metadata=self._step_metadata(log_dir, len(steps) - 1, managed),
        )

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
            logger.warning(
                "Eval runtime prewarm failed for session %s: %s",
                request.session_id,
                exc,
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
            logger.exception("Post-run handling failed for session %s", request.session_id)
            result = self._error_result(request, managed.timer, f"post-run failed: {exc}")
        finally:
            managed.timer.mark("postrun", "finished")
            managed.timer.mark("teardown", "started")
            await self._run_postrun_steps(managed)
            try:
                self._redact_session_captures(managed)
            except Exception as exc:
                logger.exception(
                    "Credential capture redaction failed for session %s",
                    request.session_id,
                )
                result = self._error_result(
                    request,
                    managed.timer,
                    f"credential capture redaction failed: {exc}",
                )
            stop_tasks = []
            eval_runtime = (
                await self._drain_eval_prewarm_task(managed)
                or managed.eval_runtime
            )
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
        try:
            await self._deliver_terminal_result(managed, result)
        finally:
            if runtimes_removed:
                roots_removed = await self._remove_owned_roots(managed)
                if not roots_removed:
                    self._register_cleanup_retry(managed)
                else:
                    self._cleanup_retries.pop(request.session_id, None)
                    self._clear_cleanup_ownership(request.session_id)
            else:
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
        managed.timer.mark("teardown", "started")
        eval_runtime = (
            await self._drain_eval_prewarm_task(managed)
            or managed.eval_runtime
        )
        stop_targets = [
            (eval_runtime, "eval runtime"),
            (managed.runtime, "runtime"),
        ]
        stop_results = await asyncio.gather(
            *(
                self._stop_runtime_best_effort(runtime, request.session_id, label)
                for runtime, label in stop_targets
                if runtime is not None
            ),
            return_exceptions=True,
        )
        runtimes_removed = (
            not managed.runtime_cleanup_blocked
            and all(outcome is True for outcome in stop_results)
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

        await self._finalize_subscription_after_runtime_absence(managed, result=result)

    async def _finalize_subscription_after_runtime_absence(
        self,
        managed: ManagedSession,
        *,
        result: SessionResult | None,
    ) -> None:
        request = managed.request
        try:
            self._start_finalization_deadline(managed)
            self._redact_session_captures(managed)
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
            logger.exception(
                "Subscription finalization failed for session %s",
                request.session_id,
            )
            result = self._error_result(
                request,
                managed.timer,
                f"subscription finalization failed: {exc}",
            )
        finally:
            managed.timer.mark("postrun", "finished")
            managed.timer.mark("return", "finished")

        try:
            await self._deliver_terminal_result(managed, result)
        finally:
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
    ) -> None:
        request = managed.request
        normalized = result.model_copy(
            update={
                "timing": managed.timer.to_session_timing(),
                "node_id": self.node_id,
                "error": result.error or result.trajectory.error,
            }
        )
        normalized = self._redact_in_memory_result(managed, normalized)
        self.session_registry.set_result(request.session_id, normalized)
        await self._export_evolution_event(normalized)
        self.storage.delete_session(request.session_id)
        if await self._push_result(request.callback_url, normalized):
            self.session_registry.clear_result_payload(request.session_id)

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
            runtime_spec = (
                managed.runtime.spec
                if managed.runtime is not None
                else request.runtime
            )
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
                if (
                    managed.log_authority_dir is None
                    or managed.log_authority_identity is None
                ):
                    raise RuntimeError(
                        "managed transcript requires a pinned log authority"
                    )
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
            logger.warning("Eval timed out for session %s: %s", request.session_id, exc)
            if trajectory.status not in ("TIMEOUT", "ERROR"):
                trajectory = trajectory.model_copy(
                    update={"status": "TIMEOUT", "error": f"eval timed out: {exc}"}
                )
        except Exception as exc:
            logger.exception("Eval failed for session %s", request.session_id)
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
            logger.exception(
                "Evaluator %s failed for session %s",
                evaluator_spec.strategy,
                request.session_id,
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
            MANAGED_SUBSCRIPTION_ENV
            if _is_codex_subscription_agent(request.agent)
            else {}
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
        trajectory = result.trajectory.model_copy(
            update={"status": status, "error": error}
        )
        return result.model_copy(
            update={"status": status, "trajectory": trajectory, "error": error}
        )

    async def _push_result(self, callback_url: str | None, result: SessionResult) -> bool:
        """POST the terminal result to the rollout server. Return True on success."""
        if not callback_url:
            return False
        try:
            response = await self._client.post(callback_url, json=result.model_dump(mode="json"))
            response.raise_for_status()
            return True
        except Exception:
            logger.warning(
                "Failed to deliver callback for session %s to %s",
                result.session_id,
                callback_url,
                exc_info=True,
            )
            return False

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
            raise GatewayExecutionTimeout(
                "session transcript finalization timeout"
            ) from exc

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
            except Exception:
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
        except Exception:
            logger.warning(
                "Failed to stop %s for session %s",
                label,
                session_id,
                exc_info=True,
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
                return {
                    redactor.redact(str(key)): redact(item)
                    for key, item in value.items()
                }
            return value

        return SessionResult.model_validate(
            redact(result.model_dump(mode="python"))
        )

    async def _remove_owned_roots(self, managed: ManagedSession) -> bool:
        tasks = [
            self._remove_session_dir_best_effort(
                managed.session_dir,
                managed.session_id,
                managed.session_root_identity,
            )
        ]
        if managed.credential_dir is not None:
            tasks.append(
                self._remove_credential_dir_best_effort(
                    managed.credential_dir,
                    managed.session_id,
                    managed.credential_root_identity,
                )
            )
        if managed.log_authority_dir is not None:
            tasks.append(
                self._remove_log_authority_best_effort(
                    managed.log_authority_dir,
                    managed.session_id,
                    managed.log_authority_identity,
                )
            )
        outcomes = await asyncio.gather(*tasks, return_exceptions=True)
        return all(outcome is not False and not isinstance(outcome, BaseException) for outcome in outcomes)

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
        retries[managed.session_id] = ownership
        self._persist_cleanup_ownership(ownership)

    @staticmethod
    def _cleanup_ownership_for(
        managed: ManagedSession,
        *,
        eval_runtime: BaseRuntime | None = None,
        finalize_subscription: bool = False,
    ) -> CleanupRetryOwnership:
        runtime = managed.runtime
        return CleanupRetryOwnership(
            session_id=managed.session_id,
            session_dir=managed.session_dir,
            session_root_identity=managed.session_root_identity,
            log_authority_dir=managed.log_authority_dir,
            log_authority_identity=managed.log_authority_identity,
            credential_dir=managed.credential_dir,
            credential_root_identity=managed.credential_root_identity,
            runtime_id=(str(getattr(runtime, "runtime_id", "")) or None),
            container_id=getattr(runtime, "container_id", None),
            eval_runtime_id=(
                str(getattr(eval_runtime, "runtime_id", "")) or None
            ),
            eval_container_id=getattr(eval_runtime, "container_id", None),
            runtime=runtime,
            eval_runtime=eval_runtime,
            managed=managed,
            finalize_subscription=finalize_subscription,
        )

    def _cleanup_journal_path(self, session_id: str) -> Path:
        name = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
        return self._cleanup_journal_dir / f"{name}.json"

    def _persist_cleanup_ownership(self, ownership: CleanupRetryOwnership) -> None:
        journal_dir = getattr(self, "_cleanup_journal_dir", None)
        if journal_dir is None:
            return
        journal_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        journal_dir.chmod(0o700)
        payload = {
            "version": 2,
            "session_id": ownership.session_id,
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
                }
            ),
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        destination = self._cleanup_journal_path(ownership.session_id)
        temporary = destination.with_name(
            f".{destination.name}.{os.getpid()}.{id(ownership)}.tmp"
        )
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
        try:
            offset = 0
            while offset < len(encoded):
                written = os.write(descriptor, encoded[offset:])
                if written <= 0:
                    raise RuntimeError("cleanup ownership journal write made no progress")
                offset += written
            os.fchmod(descriptor, 0o600)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, destination)
        directory_fd = os.open(journal_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    def _load_cleanup_retries(self) -> None:
        journal_dir = getattr(self, "_cleanup_journal_dir", None)
        if journal_dir is None or not journal_dir.exists():
            return
        directory_stat = journal_dir.stat(follow_symlinks=False)
        if (
            not stat.S_ISDIR(directory_stat.st_mode)
            or directory_stat.st_uid != os.geteuid()
            or directory_stat.st_mode & 0o077
        ):
            raise RuntimeError("cleanup ownership journal directory is not private")
        for path in sorted(journal_dir.glob("*.json")):
            opened_fd = os.open(
                path,
                os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
            )
            try:
                opened = os.fstat(opened_fd)
                if (
                    not stat.S_ISREG(opened.st_mode)
                    or opened.st_uid != os.geteuid()
                    or opened.st_nlink != 1
                    or opened.st_mode & 0o077
                    or opened.st_size <= 0
                    or opened.st_size > 16 * 1024
                ):
                    raise RuntimeError("cleanup ownership journal file is not private")
                chunks: list[bytes] = []
                remaining = opened.st_size
                while remaining:
                    chunk = os.read(opened_fd, remaining)
                    if not chunk:
                        break
                    chunks.append(chunk)
                    remaining -= len(chunk)
                if remaining or os.read(opened_fd, 1):
                    raise RuntimeError("cleanup ownership journal changed during read")
                raw = b"".join(chunks)
            finally:
                os.close(opened_fd)
            try:
                payload = json.loads(raw.decode("utf-8"))
                ownership = self._cleanup_ownership_from_payload(payload, path)
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
                raise RuntimeError("cleanup ownership journal is invalid") from exc
            self._cleanup_retries[ownership.session_id] = ownership

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
        if version == 2:
            expected_keys.add("log_root")
        if set(payload) != expected_keys:
            raise ValueError("cleanup ownership payload is not closed")
        if version not in {1, 2} or not isinstance(payload["session_id"], str):
            raise ValueError("cleanup ownership identity is invalid")
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
        else:
            credential_dir, credential_identity = root_identity(credential_payload)
        return CleanupRetryOwnership(
            session_id=session_id,
            session_dir=session_dir,
            session_root_identity=session_identity,
            log_authority_dir=log_authority_dir,
            log_authority_identity=log_authority_identity,
            credential_dir=credential_dir,
            credential_root_identity=credential_identity,
            runtime_id=runtime_id,
            container_id=container_id,
            eval_runtime_id=eval_runtime_id,
            eval_container_id=eval_container_id,
            runtime=None,
            eval_runtime=None,
        )

    def _clear_cleanup_ownership(self, session_id: str) -> None:
        journal_dir = getattr(self, "_cleanup_journal_dir", None)
        if journal_dir is None:
            return
        try:
            self._cleanup_journal_path(session_id).unlink()
        except FileNotFoundError:
            return
        try:
            journal_dir.rmdir()
            journal_dir.parent.rmdir()
        except OSError:
            pass

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

    async def _reconcile_cleanup_retries(self) -> None:
        retries = getattr(self, "_cleanup_retries", None)
        if not retries:
            return
        for session_id, ownership in list(retries.items()):
            try:
                await self._reconcile_cleanup_ownership(ownership)
            except Exception:
                logger.warning(
                    "Cleanup reconciliation failed for session %s",
                    session_id,
                    exc_info=True,
                )

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
            if (
                len(recovered_outcomes) != expected_runtime_count
                or not all(outcome is True for outcome in recovered_outcomes)
            ):
                return
            root_tasks = [
                self._remove_session_dir_best_effort(
                    ownership.session_dir,
                    session_id,
                    ownership.session_root_identity,
                )
            ]
            if ownership.credential_dir is not None:
                root_tasks.append(
                    self._remove_credential_dir_best_effort(
                        ownership.credential_dir,
                        session_id,
                        ownership.credential_root_identity,
                    )
                )
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
            if all(
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
        if ownership.finalize_subscription:
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
        except Exception:
            logger.warning(
                "Failed to remove session directory for session %s",
                session_id,
                exc_info=True,
            )
            return False

    async def _remove_credential_dir_best_effort(
        self,
        credential_dir: Path,
        session_id: str,
        credential_root_identity: tuple[int, int, int] | None = None,
    ) -> bool:
        try:
            identity = credential_root_identity or capture_session_root_identity(
                credential_dir
            )
            await asyncio.to_thread(remove_session_tree, credential_dir, identity)
            return True
        except FileNotFoundError:
            return True
        except Exception:
            logger.warning(
                "Failed to remove credential directory for session %s",
                session_id,
                exc_info=True,
            )
            return False

    async def _remove_log_authority_best_effort(
        self,
        log_authority_dir: Path,
        session_id: str,
        log_authority_identity: tuple[int, int, int] | None = None,
    ) -> bool:
        try:
            identity = log_authority_identity or capture_session_root_identity(
                log_authority_dir
            )
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
        except Exception:
            logger.warning(
                "Failed to remove log authority for session %s",
                session_id,
                exc_info=True,
            )
            return False
