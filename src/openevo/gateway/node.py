"""Gateway-node execution lifecycle for dispatched rollout sessions."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
import fcntl
import hashlib
import json
import logging
import math
import os
import re
import secrets
import shlex
import shutil
import stat
import time
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory, mkdtemp
from typing import Any, Awaitable, Callable, cast
from urllib.parse import unquote, urlparse

import httpx

from openevo.config import EvolutionConfig
from openevo.gateway.dispatcher import (
    DispatcherAdmissionLease,
    DispatcherUnavailableError,
    DispatcherSnapshot,
    ManagedSession,
    SessionDispatcher,
    SessionStage,
)
from openevo.gateway.session import SessionRegistry
from openevo.gateway.session_files import (
    CredentialFileIdentity,
    CredentialRedactor,
    HeldCodexCredentialAuthority,
    PreparedCodexCredentialSnapshot,
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
from openevo.harness.capture import canonicalize_capture_mode, transcript_capture_enabled
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
    RUNTIME_SESSION_DIR,
    RuntimeReadbackBudget,
    RuntimePathSecurityError,
    _bounded_public_runtime_readback,
    _cleanup_runtime_readback_temporary_root,
    _create_runtime_readback_temporary_root,
    _has_sealed_session_bind_readback,
    _sealed_session_bind_readback,
    validate_session_bind_path,
)
from openevo.runtime.factory import create_runtime
from openevo.runtime.docker import (
    DockerRuntime,
    verify_managed_runtime_image_admission,
)
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
from openevo.evolution.agent_system import (
    ROOT_AGENT_SYSTEM_FILES,
    normalize_agent_system_target_path,
)
from openevo.evolution.client import EvolutionClient
from openevo.evolution.framework.handlers import PayloadManifestEntry, payload_tree_digest
from openevo.evolution.runtime_injection import (
    RuntimeInjectionPlan,
    build_runtime_injection_plan,
    instruction_with_evolution_context,
    receipt_from_runtime_readback,
)

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
_CLEANUP_JOURNAL_EPOCH_MAX_BYTES = 4096
_CLEANUP_JOURNAL_COMPACT_AT_ROWS = 3584
_CLEANUP_JOURNAL_LOCK_NAME = ".journal.lock"
_CLEANUP_JOURNAL_EPOCH_NAME = ".journal.epoch"
_CLEANUP_JOURNAL_EPOCH_CANDIDATE_NAME = ".journal.epoch.tmp"
_CLEANUP_JOURNAL_RETIREMENT_DIGEST_SEED = hashlib.sha256(
    b"openevo-cleanup-retirement-v1"
).hexdigest()
_CLEANUP_JOURNAL_LOCK_TIMEOUT_SECONDS = 2.0
_CLEANUP_JOURNAL_LOCK_POLL_SECONDS = 0.01
_CLEANUP_JOURNAL_RECORD_RE = re.compile(r"[0-9a-f]{64}\.json")
_CLEANUP_JOURNAL_PENDING_RE = re.compile(r"[0-9a-f]{64}\.pending")
_CLEANUP_JOURNAL_GENERATION_RE = re.compile(r"[0-9a-f]{32}")
_CLEANUP_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
_CANONICAL_EVOLUTION_TARGET_DIR = f"{RUNTIME_SESSION_DIR}/evolution"
_AGENT_SYSTEM_TARGET_READBACK_SCRIPT = r"""
import ctypes
import hashlib
import json
import os
import stat
import struct
import sys

_MARKER = "_OPENEVO_TARGET_READBACK_V1"
_CLOSED_MAX_FILES = 4096
_CLOSED_MAX_NODES = 16384
_CLOSED_MAX_BYTES = 64 * 1024 * 1024
_DIR_FLAGS = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_DIRECTORY
_FILE_FLAGS = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
_IN_MUTATION_MASK = 0x00000002 | 0x00000004 | 0x00000008 | 0x00000040 | 0x00000080 | 0x00000100 | 0x00000200 | 0x00000400 | 0x00000800
_IN_Q_OVERFLOW = 0x00004000
_IN_IGNORED = 0x00008000
_EVENT = struct.Struct("iIII")
_limits = [int(sys.argv[3]), int(sys.argv[4]), int(sys.argv[5])]
_sealed = len(sys.argv) == 7 and sys.argv[6] == "sealed"
_used = [0, 0, 0]
_watch_fd = -1
_watches = set()


def _fail():
    raise RuntimeError("closed runtime target readback")


def _path_dir_identity(value):
    return (value.st_dev, value.st_ino, value.st_mode, value.st_uid, value.st_gid)


def _tree_dir_identity(value):
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_uid,
        value.st_gid,
        value.st_nlink,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _file_identity(value):
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_nlink,
        value.st_uid,
        value.st_gid,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _consume_node():
    _used[1] += 1
    if _used[1] > _limits[1]:
        _fail()


def _consume_file():
    _used[0] += 1
    if _used[0] > _limits[0]:
        _fail()


def _consume_bytes(size):
    if size < 0 or size > _limits[2] - _used[2]:
        _fail()
    _used[2] += size


def _open_mutation_authority():
    global _watch_fd
    libc = ctypes.CDLL(None, use_errno=True)
    init = libc.inotify_init1
    init.argtypes = [ctypes.c_int]
    init.restype = ctypes.c_int
    _watch_fd = init(os.O_NONBLOCK | os.O_CLOEXEC)
    if _watch_fd < 0:
        _fail()


def _watch(directory):
    libc = ctypes.CDLL(None, use_errno=True)
    add = libc.inotify_add_watch
    add.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
    add.restype = ctypes.c_int
    watch = add(
        _watch_fd,
        os.fsencode("/proc/self/fd/" + str(directory)),
        _IN_MUTATION_MASK,
    )
    if watch < 0:
        _fail()
    _watches.add(watch)


def _require_quiet():
    while True:
        try:
            payload = os.read(_watch_fd, 65536)
        except BlockingIOError:
            return
        if not payload:
            _fail()
        offset = 0
        while offset < len(payload):
            if len(payload) - offset < _EVENT.size:
                _fail()
            watch, mask, _cookie, name_size = _EVENT.unpack_from(payload, offset)
            offset += _EVENT.size + name_size
            if offset > len(payload):
                _fail()
            if watch not in _watches or mask & (_IN_MUTATION_MASK | _IN_Q_OVERFLOW | _IN_IGNORED):
                _fail()


def _open_absolute_root(value):
    if not isinstance(value, str) or not value.startswith("/"):
        _fail()
    parts = [part for part in value.split("/") if part]
    if any(part in {".", ".."} for part in parts):
        _fail()
    descriptors = [os.open("/", _DIR_FLAGS)]
    bindings = []
    for part in parts:
        parent = descriptors[-1]
        child = os.open(part, _DIR_FLAGS, dir_fd=parent)
        identity = _path_dir_identity(os.fstat(child))
        if not stat.S_ISDIR(identity[2]):
            _fail()
        bindings.append((parent, part, child, identity))
        descriptors.append(child)
    return descriptors, bindings


def _open_optional_dir(parent, name):
    try:
        descriptor = os.open(name, _DIR_FLAGS, dir_fd=parent)
    except FileNotFoundError:
        return None
    _consume_node()
    identity = _tree_dir_identity(os.fstat(descriptor))
    if not stat.S_ISDIR(identity[2]):
        _fail()
    return descriptor, (parent, name, descriptor, identity)


def _verify_binding(binding):
    parent, name, descriptor, expected = binding
    if _path_dir_identity(os.fstat(descriptor)) != expected:
        _fail()
    observed = os.stat(name, dir_fd=parent, follow_symlinks=False)
    if _path_dir_identity(observed) != expected:
        _fail()


def _verify_tree_binding(binding):
    parent, name, descriptor, expected = binding
    if _tree_dir_identity(os.fstat(descriptor)) != expected:
        _fail()
    observed = os.stat(name, dir_fd=parent, follow_symlinks=False)
    if _tree_dir_identity(observed) != expected:
        _fail()


def _present_names(parent, names):
    present = []
    for name in names:
        try:
            os.stat(name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            continue
        _consume_node()
        present.append(name)
    return present


def _target_names(parent):
    names = []
    for name in os.listdir(parent):
        _consume_node()
        if not name.endswith(".md"):
            continue
        try:
            encoded = name.encode("utf-8")
        except UnicodeError:
            _fail()
        if not encoded or len(encoded) > 256 or name in {".", ".."} or "/" in name:
            _fail()
        names.append(name)
    return sorted(names)


def _require_missing(parent, name):
    try:
        os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return
    _fail()


def _scan_file(parent, name, relative_path, budget):
    del budget
    named_before = os.stat(name, dir_fd=parent, follow_symlinks=False)
    if (
        not stat.S_ISREG(named_before.st_mode)
        or named_before.st_nlink != 1
        or named_before.st_size < 0
        or named_before.st_size > _limits[2] - _used[2]
    ):
        _fail()
    _consume_file()
    descriptor = os.open(name, _FILE_FLAGS, dir_fd=parent)
    try:
        opened_before = os.fstat(descriptor)
        if _file_identity(opened_before) != _file_identity(named_before):
            _fail()
        remaining = opened_before.st_size
        digest = hashlib.sha256()
        while remaining:
            chunk = os.read(descriptor, min(65536, remaining))
            if not chunk:
                _fail()
            _consume_bytes(len(chunk))
            digest.update(chunk)
            remaining -= len(chunk)
        opened_after = os.fstat(descriptor)
        named_after = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if (
            _file_identity(opened_after) != _file_identity(opened_before)
            or _file_identity(named_after) != _file_identity(opened_before)
        ):
            _fail()
    finally:
        os.close(descriptor)
    return {
        "relative_path": relative_path,
        "size_bytes": opened_before.st_size,
        "sha256": digest.hexdigest(),
    }


def _main():
    if (
        len(sys.argv) != 7
        or sys.argv[6] not in {"compat", "sealed"}
        or not 0 <= _limits[0] <= _CLOSED_MAX_FILES
        or not 0 <= _limits[1] <= _CLOSED_MAX_NODES
        or not 0 <= _limits[2] <= _CLOSED_MAX_BYTES
    ):
        _fail()
    root_files = json.loads(sys.argv[2])
    if (
        not isinstance(root_files, list)
        or len(root_files) != len(set(root_files))
        or not all(isinstance(value, str) and value for value in root_files)
    ):
        _fail()
    descriptors, bindings = _open_absolute_root(sys.argv[1])
    optional_descriptors = []
    try:
        if _sealed:
            _open_mutation_authority()
        root = descriptors[-1]
        if _sealed:
            _watch(root)
        root_identity = _tree_dir_identity(os.fstat(root))
        root_before = _present_names(root, root_files)
        openhands = _open_optional_dir(root, ".openhands")
        microagents = None
        microagent_names = []
        if openhands is not None:
            optional_descriptors.append(openhands[0])
            if _sealed:
                _watch(openhands[0])
            microagents = _open_optional_dir(openhands[0], "microagents")
            if microagents is not None:
                optional_descriptors.append(microagents[0])
                if _sealed:
                    _watch(microagents[0])
                microagent_names = _target_names(microagents[0])

        files = [_scan_file(root, name, name, None) for name in root_before]
        if microagents is not None:
            files.extend(
                _scan_file(
                    microagents[0],
                    name,
                    ".openhands/microagents/" + name,
                    None,
                )
                for name in microagent_names
            )

        if _present_names(root, root_files) != root_before:
            _fail()
        if _tree_dir_identity(os.fstat(root)) != root_identity:
            _fail()
        if openhands is None:
            _require_missing(root, ".openhands")
        else:
            if microagents is None:
                _require_missing(openhands[0], "microagents")
            else:
                if _target_names(microagents[0]) != microagent_names:
                    _fail()
                _verify_tree_binding(microagents[1])
            _verify_tree_binding(openhands[1])
        for binding in reversed(bindings):
            _verify_binding(binding)
        if _sealed:
            _require_quiet()
        files.sort(key=lambda item: item["relative_path"])
        sys.stdout.write(
            json.dumps(
                {
                    "schema_version": "1",
                    "files": files,
                    "consumed": {
                        "files": _used[0],
                        "nodes": _used[1],
                        "bytes": _used[2],
                    },
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )
    finally:
        if _watch_fd >= 0:
            os.close(_watch_fd)
        for descriptor in reversed(optional_descriptors):
            os.close(descriptor)
        for descriptor in reversed(descriptors):
            os.close(descriptor)


try:
    _main()
except BaseException:
    sys.stderr.write(_MARKER + " failed\n")
    raise SystemExit(74)
"""
_RECOVERY_PHASE_RUNTIME_ACTIVE = "runtime_active"
_RECOVERY_PHASE_TERMINAL_FINALIZATION = "terminal_finalization"
_RECOVERY_PHASE_TERMINAL_DELIVERY = "terminal_delivery"
_RECOVERY_PHASES = {
    _RECOVERY_PHASE_RUNTIME_ACTIVE,
    _RECOVERY_PHASE_TERMINAL_FINALIZATION,
    _RECOVERY_PHASE_TERMINAL_DELIVERY,
}
_RECOVERY_PHASE_ORDER = {
    _RECOVERY_PHASE_RUNTIME_ACTIVE: 0,
    _RECOVERY_PHASE_TERMINAL_FINALIZATION: 1,
    _RECOVERY_PHASE_TERMINAL_DELIVERY: 2,
}
_UNSET = object()
_SHA256_RE = re.compile(r"[0-9a-f]{64}")


class GatewayExecutionTimeout(TimeoutError):
    """Raised when a session exhausts its shared gateway execution budget."""


class CancelAuthorityPersistenceError(RuntimeError):
    """Raised when cancellation cannot be made durable before runtime effects."""


class GatewayReadinessError(RuntimeError):
    """Stable admission failure for managed credential readiness or publication."""


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
class _StagedEvolutionArtifact:
    artifact_id: str
    artifact_type: str
    content_sha256: str
    staged_sha256: str


@dataclass(frozen=True, slots=True)
class _SelectedEvolutionArtifact:
    artifact_id: str
    artifact_type: str
    content_sha256: str
    payload_entries: tuple[PayloadManifestEntry, ...]


@dataclass(frozen=True, slots=True)
class _EvolutionStagingResult:
    env: dict[str, str]
    artifacts: tuple[_StagedEvolutionArtifact, ...]
    staged_tree_sha256: str
    injection_plan: RuntimeInjectionPlan | None = None


@dataclass(frozen=True, slots=True)
class _EvolutionInjection:
    env: dict[str, str]
    staged: _EvolutionStagingResult


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
    revision: int = 0
    generation: str | None = None
    epoch: int | None = None
    epoch_token: str | None = None
    eval_runtime: BaseRuntime | None = None
    managed: ManagedSession | None = None
    finalize_subscription: bool = False
    finalization_state: SubscriptionFinalizationState | None = None
    delivery_state: TerminalDeliveryState | None = None


@dataclass(frozen=True, slots=True)
class _CleanupJournalTombstone:
    session_id: str
    generation: str
    revision: int
    epoch: int | None
    epoch_token: str | None
    retired_epoch: int | None
    retired_epoch_token: str | None
    result_digest: str | None
    export_succeeded: bool | None
    callback_succeeded: bool | None


@dataclass(frozen=True, slots=True)
class _CleanupJournalEpoch:
    epoch: int
    token: str
    previous_token: str | None
    retired_count: int
    retirement_digest: str


@dataclass(slots=True)
class _CleanupJournalAuthority:
    """Held no-follow authority for one immutable cleanup journal root."""

    path: Path
    ancestor_fds: list[int]
    ancestor_identities: tuple[tuple[int, int, int, int], ...]
    root_fd: int
    root_identity: tuple[int, int, int, int]
    marker_version: int

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
    staged = await _stage_evolution_context_files(
        runtime=runtime,
        context=context,
        host_dir=host_dir,
        target_dir=target_dir,
        expected_artifact_ids=None,
        instruction=None,
        revision_id=None,
    )
    return dict(staged.env)


async def _stage_evolution_context_files(
    *,
    runtime: BaseRuntime,
    context: dict,
    host_dir: Path,
    target_dir: str,
    expected_artifact_ids: tuple[str, ...] | None,
    instruction: str | None,
    revision_id: str | None,
) -> _EvolutionStagingResult:
    del host_dir  # Staging must stay outside the agent-writable session bind.
    selected = _selected_evolution_artifacts(
        context,
        expected_artifact_ids=expected_artifact_ids,
    )
    selected_by_id = {item.artifact_id: item for item in selected}
    memory_items = _selected_text_items(
        context,
        "memory",
        "text_memory",
        selected_by_id=selected_by_id,
    )
    agent_system_items = _selected_text_items(
        context,
        "agent_system",
        "agent_system",
        selected_by_id=selected_by_id,
    )
    injection_plan = None
    if expected_artifact_ids:
        if instruction is None or revision_id is None:
            raise ValueError("exact evolution staging requires instruction and revision authority")
        injection_plan = build_runtime_injection_plan(
            context=context,
            revision_id=revision_id,
            instruction=instruction,
            expected_artifact_ids=expected_artifact_ids,
        )
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
        instruction_path = evolution_dir / "instruction.txt"

        skill_digests = _stage_evolution_skill_bundles(
            context,
            skills_dir,
            selected_by_id=selected_by_id,
        )
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
                    target_payloads=(
                        injection_plan.agent_system_targets if injection_plan is not None else None
                    ),
                )
            )

        memory_text = _memory_rendered_text(context)
        if injection_plan is None:
            context_path.write_text(
                json.dumps(_runtime_context_document(context), indent=2, sort_keys=True),
                encoding="utf-8",
            )
            memory_path.write_text(memory_text, encoding="utf-8")
            agent_system_path.write_text(agent_system_text, encoding="utf-8")
            adapters_path.write_text(
                json.dumps(
                    context.get("adapter_merge_spec") or {},
                    indent=2,
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
        else:
            for filename, payload in injection_plan.canonical_files.items():
                (evolution_dir / filename).write_bytes(payload)

        await runtime.upload_file(str(context_path), f"{target_dir}/context.json")
        await runtime.upload_file(str(memory_path), f"{target_dir}/memory.md")
        if agent_system_text:
            await runtime.upload_file(
                str(agent_system_path),
                f"{target_dir}/agent_system.md",
            )
        if injection_plan is not None:
            await runtime.upload_file(
                str(instruction_path),
                f"{target_dir}/instruction.txt",
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
    staged_artifacts: list[_StagedEvolutionArtifact] = []
    for item in selected:
        if item.artifact_type == "text_memory":
            staged_sha256 = _sha256_text(memory_items[item.artifact_id])
        elif item.artifact_type == "agent_system":
            staged_sha256 = _sha256_text(agent_system_items[item.artifact_id])
        elif item.artifact_type == "skill_bundle":
            staged_sha256 = skill_digests[item.artifact_id]
        elif item.artifact_type == "parametric_memory":
            staged_sha256 = _sha256_text(
                json.dumps(
                    context.get("adapter_merge_spec") or {},
                    ensure_ascii=True,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
        else:
            raise ValueError("selected evolution artifact type is unsupported")
        staged_artifacts.append(
            _StagedEvolutionArtifact(
                artifact_id=item.artifact_id,
                artifact_type=item.artifact_type,
                content_sha256=item.content_sha256,
                staged_sha256=staged_sha256,
            )
        )
    staged_tree_sha256 = _canonical_sha256(
        {
            "artifacts": [
                {
                    "artifact_id": item.artifact_id,
                    "artifact_type": item.artifact_type,
                    "content_sha256": item.content_sha256,
                    "staged_sha256": item.staged_sha256,
                }
                for item in staged_artifacts
            ],
            "canonical_files": {
                "agent_system_sha256": _sha256_text(agent_system_text),
                "memory_sha256": _sha256_text(memory_text),
            },
        }
    )
    return _EvolutionStagingResult(
        env=env,
        artifacts=tuple(staged_artifacts),
        staged_tree_sha256=staged_tree_sha256,
        injection_plan=injection_plan,
    )


def _selected_evolution_artifacts(
    context: dict,
    *,
    expected_artifact_ids: tuple[str, ...] | None,
) -> tuple[_SelectedEvolutionArtifact, ...]:
    selection = context.get("selection")
    if expected_artifact_ids is None and selection in (None, {}):
        return ()
    if not isinstance(selection, dict) or set(selection) != {
        "artifact_ids",
        "artifacts",
        "reasons",
    }:
        raise ValueError("evolution context selection contract is invalid")
    artifact_ids = selection.get("artifact_ids")
    artifacts = selection.get("artifacts")
    reasons = selection.get("reasons")
    if (
        not isinstance(artifact_ids, list)
        or not isinstance(artifacts, list)
        or not isinstance(reasons, list)
        or not all(isinstance(reason, str) for reason in reasons)
        or len(artifact_ids) > 256
        or not all(
            isinstance(artifact_id, str) and 0 < len(artifact_id.encode("utf-8")) <= 256
            for artifact_id in artifact_ids
        )
        or len(artifact_ids) != len(set(artifact_ids))
    ):
        raise ValueError("evolution context selection identity is invalid")
    if expected_artifact_ids is not None and tuple(artifact_ids) != expected_artifact_ids:
        raise ValueError("resolved evolution context differs from admission")
    selected_by_id: dict[str, _SelectedEvolutionArtifact] = {}
    for value in artifacts:
        if not isinstance(value, dict) or set(value) != {
            "artifact_id",
            "artifact_type",
            "content_sha256",
            "payload_entries",
        }:
            raise ValueError("evolution context artifact inventory is invalid")
        artifact_id = value.get("artifact_id")
        artifact_type = value.get("artifact_type")
        content_sha256 = value.get("content_sha256")
        raw_entries = value.get("payload_entries")
        if (
            not isinstance(artifact_id, str)
            or not isinstance(artifact_type, str)
            or artifact_type
            not in {"agent_system", "parametric_memory", "skill_bundle", "text_memory"}
            or not isinstance(content_sha256, str)
            or _SHA256_RE.fullmatch(content_sha256) is None
            or not isinstance(raw_entries, list)
            or not raw_entries
        ):
            raise ValueError("evolution context artifact inventory is invalid")
        entries = tuple(PayloadManifestEntry.model_validate(entry) for entry in raw_entries)
        if payload_tree_digest(entries) != content_sha256:
            raise ValueError("evolution context artifact digest is invalid")
        if artifact_id in selected_by_id:
            raise ValueError("evolution context artifact inventory is duplicated")
        selected_by_id[artifact_id] = _SelectedEvolutionArtifact(
            artifact_id=artifact_id,
            artifact_type=artifact_type,
            content_sha256=content_sha256,
            payload_entries=entries,
        )
    if len(selected_by_id) != len(artifact_ids) or set(selected_by_id) != set(artifact_ids):
        raise ValueError("evolution context artifact inventory is incomplete")
    return tuple(selected_by_id[artifact_id] for artifact_id in artifact_ids)


def _selected_text_items(
    context: dict,
    section_name: str,
    artifact_type: str,
    *,
    selected_by_id: dict[str, _SelectedEvolutionArtifact],
) -> dict[str, str]:
    expected_ids = {
        artifact_id
        for artifact_id, item in selected_by_id.items()
        if item.artifact_type == artifact_type
    }
    if not expected_ids:
        return {}
    section = context.get(section_name)
    if not isinstance(section, dict):
        raise ValueError(f"evolution {section_name} selection is invalid")
    artifact_ids = section.get("artifact_ids")
    item_key = "targets" if section_name == "agent_system" else "items"
    values = section.get(item_key)
    rendered = section.get("rendered_text")
    if (
        not isinstance(artifact_ids, list)
        or set(artifact_ids) != expected_ids
        or len(artifact_ids) != len(expected_ids)
        or not isinstance(values, list)
        or not isinstance(rendered, str)
    ):
        raise ValueError(f"evolution {section_name} selection is incomplete")
    text_by_id: dict[str, str] = {}
    ordered_parts: list[str] = []
    for value in values:
        if not isinstance(value, dict):
            raise ValueError(f"evolution {section_name} item is invalid")
        artifact_id = value.get("artifact_id")
        text = value.get("rendered_text")
        if (
            not isinstance(artifact_id, str)
            or artifact_id not in expected_ids
            or artifact_id in text_by_id
            or not isinstance(text, str)
            or not text
        ):
            raise ValueError(f"evolution {section_name} item is invalid")
        text_by_id[artifact_id] = text
        ordered_parts.append(text)
    if set(text_by_id) != expected_ids or "\n\n".join(ordered_parts) != rendered:
        raise ValueError(f"evolution {section_name} rendered content is inconsistent")
    return text_by_id


def _memory_rendered_text(context: dict) -> str:
    memory = context.get("memory") or {}
    if not isinstance(memory, dict):
        raise ValueError("evolution memory context must be an object")
    rendered = memory.get("rendered_text") or ""
    if not isinstance(rendered, str):
        raise ValueError("evolution memory content must be text")
    return rendered


def _runtime_context_document(context: dict) -> dict[str, Any]:
    document = json.loads(json.dumps(context, ensure_ascii=True, allow_nan=False))
    for skill in document.get("skills", []):
        if isinstance(skill, dict):
            skill.pop("uri", None)
    adapter_spec = document.get("adapter_merge_spec")
    if isinstance(adapter_spec, dict):
        for adapter in adapter_spec.get("adapters", []):
            if isinstance(adapter, dict):
                adapter.pop("uri", None)
    return cast(dict[str, Any], document)


def _stage_evolution_skill_bundles(
    context: dict,
    skills_dir: Path,
    *,
    selected_by_id: dict[str, _SelectedEvolutionArtifact],
) -> dict[str, str]:
    skills = context.get("skills") or []
    if not isinstance(skills, list):
        raise ValueError("evolution skill selection must be a list")
    staged: dict[str, str] = {}
    for index, skill in enumerate(skills):
        if not isinstance(skill, dict):
            raise ValueError("evolution skill selection entry must be an object")
        artifact_id = skill.get("artifact_id")
        if not isinstance(artifact_id, str) or not artifact_id:
            raise ValueError("evolution skill selection has no artifact identity")
        selected = selected_by_id.get(artifact_id)
        if selected_by_id and (selected is None or selected.artifact_type != "skill_bundle"):
            raise ValueError("evolution skill selection differs from context inventory")
        source = _artifact_file_uri_path(skill.get("uri"))
        if source is None:
            raise ValueError("skill artifact URI is not a file:// URI")
        _require_regular_payload_tree(source)
        dest = skills_dir / _safe_skill_dir_name(skill, index)
        if source.is_dir():
            shutil.copytree(source, dest, symlinks=True)
        else:
            dest.mkdir(parents=True, exist_ok=False)
            relative_target = (
                selected.payload_entries[0].relative_path
                if selected is not None and len(selected.payload_entries) == 1
                else source.name
            )
            target = dest / Path(*PurePosixPath(relative_target).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        if selected is not None:
            digest = _verify_staged_payload(dest, selected.payload_entries)
            if digest != selected.content_sha256:
                raise ValueError("staged skill payload digest differs from context authority")
        else:
            digest = _directory_content_sha256(dest)
        staged[artifact_id] = digest
    expected_skill_ids = {
        artifact_id
        for artifact_id, item in selected_by_id.items()
        if item.artifact_type == "skill_bundle"
    }
    if set(staged) != expected_skill_ids and selected_by_id:
        raise ValueError("evolution skill payload was not staged exactly")
    return staged


async def _stage_evolution_agent_system(
    *,
    runtime: BaseRuntime,
    context: dict,
    targets_dir: Path,
    rendered: str,
    target_payloads: Mapping[str, bytes] | None = None,
) -> dict[str, str]:
    agent_system = context.get("agent_system") or {}
    if not isinstance(agent_system, dict):
        return {}

    target_specs = (
        [
            {"target_path": target_path, "rendered_text": payload.decode("utf-8")}
            for target_path, payload in target_payloads.items()
        ]
        if target_payloads is not None
        else _agent_system_target_specs(agent_system, rendered)
    )
    target_root = _agent_system_target_root(runtime)
    remote_targets: list[str] = []
    agents_md_target: str | None = None
    for spec in target_specs:
        target_path = PurePosixPath(normalize_agent_system_target_path(spec.get("target_path")))

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
                raise ValueError("evolution agent-system target must be an object")
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


def _require_regular_payload_tree(path: Path) -> None:
    try:
        root_stat = path.lstat()
    except OSError as exc:
        raise ValueError("evolution payload is unavailable") from exc
    if stat.S_ISLNK(root_stat.st_mode):
        raise ValueError("evolution payload must not contain symlinks")
    if stat.S_ISREG(root_stat.st_mode):
        return
    if not stat.S_ISDIR(root_stat.st_mode):
        raise ValueError("evolution payload root must be a regular file or directory")
    for root, directories, files in os.walk(path, followlinks=False):
        for name in [*directories, *files]:
            item = Path(root) / name
            item_stat = item.lstat()
            if stat.S_ISLNK(item_stat.st_mode):
                raise ValueError("evolution payload must not contain symlinks")
            if name in files and not stat.S_ISREG(item_stat.st_mode):
                raise ValueError("evolution payload contains a non-regular file")


def _verify_staged_payload(
    root: Path,
    expected_entries: tuple[PayloadManifestEntry, ...],
) -> str:
    _require_regular_payload_tree(root)
    expected = {entry.relative_path: entry for entry in expected_entries}
    observed_paths = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and not path.is_symlink()
    }
    if observed_paths != set(expected):
        raise ValueError("staged evolution payload inventory differs from context authority")
    observed: list[PayloadManifestEntry] = []
    for relative_path in sorted(observed_paths):
        path = root / Path(*PurePosixPath(relative_path).parts)
        payload = path.read_bytes()
        authority = expected[relative_path]
        digest = hashlib.sha256(payload).hexdigest()
        if len(payload) != authority.size_bytes or digest != authority.sha256:
            raise ValueError("staged evolution payload content differs from context authority")
        observed.append(
            PayloadManifestEntry(
                relative_path=relative_path,
                media_type=authority.media_type,
                size_bytes=len(payload),
                sha256=digest,
            )
        )
    return payload_tree_digest(tuple(observed))


def _directory_content_sha256(root: Path) -> str:
    _require_regular_payload_tree(root)
    inventory = []
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        payload = path.read_bytes()
        inventory.append(
            {
                "relative_path": path.relative_to(root).as_posix(),
                "size_bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        )
    if not inventory:
        raise ValueError("staged evolution payload is empty")
    return _canonical_sha256({"entries": inventory})


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical_sha256(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _instruction_with_evolution_context(instruction: str, context: dict) -> str:
    return instruction_with_evolution_context(instruction, context)


def _existing_evolution_metadata(metadata: dict) -> dict:
    existing = metadata.get("evolution")
    if isinstance(existing, dict):
        return dict(existing)
    return {}


def _admitted_context_artifact_ids(
    metadata: dict[str, object],
) -> tuple[str, ...] | None:
    evolution = metadata.get("evolution")
    if not isinstance(evolution, dict) or "context_artifact_ids" not in evolution:
        return None
    artifact_ids = evolution.get("context_artifact_ids")
    if (
        not isinstance(artifact_ids, list)
        or len(artifact_ids) > 256
        or not all(
            isinstance(artifact_id, str) and 0 < len(artifact_id.encode("utf-8")) <= 256
            for artifact_id in artifact_ids
        )
        or len(artifact_ids) != len(set(artifact_ids))
    ):
        raise ValueError("admitted evolution context artifact identity is invalid")
    return tuple(artifact_ids)


def _admitted_revision_id(
    metadata: dict[str, object],
    *,
    required: bool,
) -> str | None:
    openevo = metadata.get("openevo")
    revision_id = openevo.get("revision_id") if isinstance(openevo, dict) else None
    if revision_id is None and not required:
        return None
    if (
        not isinstance(revision_id, str)
        or not revision_id
        or len(revision_id.encode("utf-8")) > 256
    ):
        raise ValueError("admitted evolution revision identity is invalid")
    return revision_id


async def _runtime_injection_receipt_from_readback(
    *,
    runtime: BaseRuntime,
    target_dir: str,
    plan: RuntimeInjectionPlan,
) -> dict[str, object]:
    budget = RuntimeReadbackBudget()
    temporary = _create_runtime_readback_temporary_root()
    try:
        readback_root = temporary.path
        sealed = (
            target_dir == _CANONICAL_EVOLUTION_TARGET_DIR
            and _has_sealed_session_bind_readback(runtime)
        )
        if sealed:
            readback = await _sealed_session_bind_readback(
                runtime,
                target_dir,
                readback_root / "evolution",
                budget=budget,
                expected_directory=True,
            )
            files = [
                {
                    "relative_path": f"evolution/{entry.relative_path}",
                    "size_bytes": entry.size_bytes,
                    "sha256": entry.sha256,
                }
                for entry in readback.files
            ]
        else:
            readback = await _bounded_public_runtime_readback(
                runtime,
                target_dir,
                readback_root / "evolution",
                budget=budget,
                relative_prefix="evolution",
                temporary_root=temporary,
            )
            files = [
                {
                    "relative_path": entry.relative_path,
                    "size_bytes": entry.size_bytes,
                    "sha256": entry.sha256,
                }
                for entry in readback.files
            ]
        if plan.agent_system_targets:
            files.extend(
                await _runtime_agent_system_target_inventory(
                    runtime,
                    budget=budget,
                    sealed=sealed,
                )
            )
            files.sort(key=lambda item: str(item["relative_path"]))
        return receipt_from_runtime_readback(plan.authority, files)
    finally:
        await _cleanup_runtime_readback_temporary_root(temporary)


async def _runtime_agent_system_target_inventory(
    runtime: BaseRuntime,
    *,
    budget: RuntimeReadbackBudget,
    sealed: bool = True,
) -> list[dict[str, object]]:
    target_root = _agent_system_target_root(runtime)
    if not target_root.is_absolute():
        raise ValueError("runtime agent-system target root must be absolute")
    root_files = json.dumps(sorted(ROOT_AGENT_SYSTEM_FILES), separators=(",", ":"))
    command = " ".join(
        (
            "/usr/bin/python3",
            "-I",
            "-S",
            "-c",
            shlex.quote(_AGENT_SYSTEM_TARGET_READBACK_SCRIPT),
            shlex.quote(target_root.as_posix()),
            shlex.quote(root_files),
            str(budget.remaining_files),
            str(budget.remaining_nodes),
            str(budget.remaining_bytes),
            "sealed" if sealed else "compat",
        )
    )
    try:
        result = await runtime.exec(command, timeout_sec=30.0)
    except BaseException:
        budget.exhaust()
        raise
    if (
        result is None
        or getattr(result, "return_code", None) != 0
        or not isinstance(getattr(result, "stdout", None), str)
    ):
        budget.exhaust()
        raise ValueError("runtime agent-system target readback failed")
    payload = result.stdout
    if len(payload.encode("utf-8")) > 2 * 1024 * 1024:
        budget.exhaust()
        raise ValueError("runtime agent-system target inventory exceeds its byte bound")
    try:
        document = json.loads(payload)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        budget.exhaust()
        raise ValueError("runtime agent-system target inventory is invalid") from exc
    if not isinstance(document, dict) or set(document) != {
        "schema_version",
        "files",
        "consumed",
    }:
        budget.exhaust()
        raise ValueError("runtime agent-system target inventory is invalid")
    values = document.get("files")
    consumed = document.get("consumed")
    if (
        document.get("schema_version") != "1"
        or not isinstance(values, list)
        or not isinstance(consumed, dict)
        or set(consumed) != {"files", "nodes", "bytes"}
        or any(
            not isinstance(value, int) or isinstance(value, bool)
            for value in consumed.values()
        )
    ):
        budget.exhaust()
        raise ValueError("runtime agent-system target inventory is invalid")
    files: list[dict[str, object]] = []
    for value in values:
        if not isinstance(value, dict) or set(value) != {
            "relative_path",
            "size_bytes",
            "sha256",
        }:
            budget.exhaust()
            raise ValueError("runtime agent-system target inventory is invalid")
        relative_path = value.get("relative_path")
        size_bytes = value.get("size_bytes")
        digest = value.get("sha256")
        if (
            not isinstance(relative_path, str)
            or not isinstance(size_bytes, int)
            or isinstance(size_bytes, bool)
            or size_bytes < 0
            or not isinstance(digest, str)
            or _SHA256_RE.fullmatch(digest) is None
        ):
            budget.exhaust()
            raise ValueError("runtime agent-system target inventory is invalid")
        try:
            normalized = normalize_agent_system_target_path(relative_path)
        except ValueError as exc:
            budget.exhaust()
            raise ValueError("runtime agent-system target inventory is invalid") from exc
        if normalized != relative_path:
            budget.exhaust()
            raise ValueError("runtime agent-system target inventory is not canonical")
        files.append(
            {
                "relative_path": f"agent_system_targets/{relative_path}",
                "size_bytes": size_bytes,
                "sha256": digest,
            }
        )
    if (
        consumed["files"] != len(files)
        or consumed["bytes"] != sum(int(item["size_bytes"]) for item in files)
    ):
        budget.exhaust()
        raise ValueError("runtime agent-system target inventory is invalid")
    try:
        budget.consume_report(
            files=consumed["files"],
            nodes=consumed["nodes"],
            bytes_read=consumed["bytes"],
        )
    except RuntimePathSecurityError as exc:
        raise ValueError("runtime agent-system target inventory exceeds its budget") from exc
    return files


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
        credential_authority: (
            HeldCodexCredentialAuthority | PreparedCodexCredentialSnapshot | None
        ) = None,
        managed_image_authority_verifier: (
            Callable[[RuntimeSpec], Awaitable[None]] | None
        ) = None,
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
        self._credential_authority = credential_authority
        self._managed_image_authority_verifier = (
            managed_image_authority_verifier
            or verify_managed_runtime_image_admission
        )
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
        self._canonicalize_request_capture_mode(request)
        session_id = request.session_id
        if self.session_registry.get(session_id) is not None:
            raise ValueError(
                f"session {session_id} already exists; rollout session IDs are single-use"
            )

        runtime_spec = self._resolve_runtime_spec(request)
        self._validate_subscription_admission(request, runtime_spec, None)
        prepared_credential: PreparedCodexCredentialSnapshot | None = None
        if _is_codex_subscription_agent(request.agent):
            try:
                prepared_credential = self._prepare_codex_subscription_auth(request)
            except SessionFileSecurityError as exc:
                raise GatewayReadinessError(
                    "managed subscription credential readiness failed"
                ) from exc
            try:
                await self._managed_image_authority_verifier(runtime_spec)
            except (OSError, RuntimeError, ValueError) as exc:
                prepared_credential.close()
                raise GatewayReadinessError(
                    "managed runtime image authority was not ready for admission"
                ) from exc
            except BaseException:
                prepared_credential.close()
                raise

        dispatcher_admission: DispatcherAdmissionLease | None = None
        try:
            dispatcher_admission = await self._dispatcher.reserve_admission()
        except DispatcherUnavailableError as exc:
            if prepared_credential is not None:
                prepared_credential.close()
            raise GatewayReadinessError(
                "gateway dispatcher was not ready for admission"
            ) from exc

        session_dir: Path | None = None
        session_root_identity: tuple[int, int, int] | None = None
        log_authority_dir: Path | None = None
        log_authority_identity: tuple[int, int, int] | None = None
        managed: ManagedSession | None = None
        try:
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
            self._validate_subscription_admission(request, runtime_spec, session_dir)
            self._validate_runtime_admission(runtime_spec, managed)
            if prepared_credential is not None:
                try:
                    self._publish_prepared_codex_subscription_auth(
                        managed,
                        prepared_credential,
                    )
                except Exception as exc:
                    raise GatewayReadinessError(
                        "managed subscription credential publication failed"
                    ) from exc

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
            self._persist_cleanup_ownership(self._cleanup_ownership_for(managed))
            try:
                await self._dispatcher.enqueue(
                    managed,
                    admission=dispatcher_admission,
                )
                dispatcher_admission = None
            except DispatcherUnavailableError as exc:
                raise GatewayReadinessError(
                    "gateway dispatcher was not ready for admission"
                ) from exc
        except BaseException as admission_error:
            try:
                self.storage.delete_session(session_id)
            except Exception as exc:
                self._log_credential_safe_exception(
                    managed,
                    "Failed to roll back session storage",
                    exc,
                    session_id=session_id,
                    level=logging.WARNING,
                )
            try:
                self.session_registry.remove(session_id)
            except Exception as exc:
                self._log_credential_safe_exception(
                    managed,
                    "Failed to roll back session registry",
                    exc,
                    session_id=session_id,
                    level=logging.WARNING,
                )
            cleanup_complete = True
            if session_dir is not None:
                try:
                    cleaned = await self._remove_session_dir_best_effort(
                        session_dir,
                        session_id,
                        session_root_identity,
                    )
                except Exception as exc:
                    cleaned = False
                    self._log_credential_safe_exception(
                        managed,
                        "Failed to roll back session root",
                        exc,
                        session_id=session_id,
                        level=logging.WARNING,
                    )
                cleanup_complete = cleaned and cleanup_complete
            if log_authority_dir is not None:
                try:
                    cleaned = await self._remove_log_authority_best_effort(
                        log_authority_dir,
                        session_id,
                        log_authority_identity,
                    )
                except Exception as exc:
                    cleaned = False
                    self._log_credential_safe_exception(
                        managed,
                        "Failed to roll back log authority",
                        exc,
                        session_id=session_id,
                        level=logging.WARNING,
                    )
                cleanup_complete = cleaned and cleanup_complete
            if managed is not None and managed.credential_dir is not None:
                try:
                    cleaned = await self._remove_credential_dir_best_effort(
                        managed.credential_dir,
                        session_id,
                        managed.credential_root_identity,
                        managed.credential_auth_identity,
                    )
                except Exception as exc:
                    cleaned = False
                    self._log_credential_safe_exception(
                        managed,
                        "Failed to roll back credential authority",
                        exc,
                        session_id=session_id,
                        level=logging.WARNING,
                    )
                cleanup_complete = cleaned and cleanup_complete
            if managed is not None and cleanup_complete:
                try:
                    self._retire_cleanup_ownership(self._cleanup_ownership_for(managed))
                except Exception as exc:
                    self._log_credential_safe_exception(
                        managed,
                        "Failed to retire rolled-back session ownership",
                        exc,
                        level=logging.WARNING,
                    )
            if isinstance(admission_error, asyncio.CancelledError):
                raise
            if isinstance(admission_error, GatewayReadinessError):
                raise
            if isinstance(admission_error, Exception):
                raise GatewayReadinessError(
                    "gateway session publication was not ready for admission"
                ) from admission_error
            raise
        finally:
            if dispatcher_admission is not None:
                await self._dispatcher.release_admission(dispatcher_admission)
            if prepared_credential is not None:
                prepared_credential.close()

    async def cancel(self, session_id: str) -> bool:
        def persist_cancel_authority(managed: ManagedSession) -> None:
            if not _is_codex_subscription_agent(managed.request.agent):
                return
            try:
                self._persist_subscription_finalization_authority(
                    managed,
                    cancel_requested=True,
                )
            except BaseException as exc:
                raise CancelAuthorityPersistenceError(
                    f"cancel authority persistence failed for session {session_id}"
                ) from exc

        return await self._dispatcher.cancel(
            session_id,
            before_cancel=persist_cancel_authority,
        )

    async def cancel_and_wait(self, session_id: str) -> bool:
        cancelled = await self.cancel(session_id)
        if cancelled:
            await self._dispatcher.wait_terminated(session_id)
        return cancelled

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
            if _is_codex_subscription_agent(request.agent) and (
                managed.credential_mount is None or managed.credential_redactor is None
            ):
                raise RuntimeError(
                    "subscription credential was not committed before session admission"
                )
            if _is_codex_subscription_agent(request.agent):
                runtime_spec = runtime_spec.model_copy(
                    update={
                        "env": {
                            **runtime_spec.env,
                            **MANAGED_SUBSCRIPTION_ENV,
                        }
                    }
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
        prepared_snapshot: PreparedCodexCredentialSnapshot | None = None,
        on_identity: Callable[[CredentialFileIdentity], None] | None = None,
    ) -> StagedCodexCredential:
        if not _is_codex_subscription_agent(request.agent):
            raise RuntimeError("credential staging requires a Codex subscription agent")

        identity = credential_root_identity or capture_session_root_identity(credential_dir)
        try:
            return stage_codex_subscription_auth(
                source=Path.home() / ".codex" / "auth.json",
                source_authority=(
                    None
                    if prepared_snapshot is not None
                    else getattr(self, "_credential_authority", None)
                ),
                prepared_snapshot=prepared_snapshot,
                session_dir=credential_dir,
                session_identity=identity,
                target_home_parts=(),
                on_identity=on_identity,
            )
        except SessionFileSecurityError:
            raise

    def _publish_prepared_codex_subscription_auth(
        self,
        managed: ManagedSession,
        prepared_snapshot: PreparedCodexCredentialSnapshot,
    ) -> None:
        request = managed.request
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
            credential_dir,
            managed.credential_root_identity,
            prepared_snapshot=prepared_snapshot,
            on_identity=persist_auth_identity,
        )
        managed.credential_redactor = staged_credential.redactor
        managed.credential_auth_identity = staged_credential.auth_identity
        managed.credential_mount = ManagedCredentialMount(
            root=credential_dir,
            root_identity=managed.credential_root_identity,
            auth_identity=staged_credential.auth_identity,
        )
        self._persist_cleanup_ownership(self._cleanup_ownership_for(managed))

    def _prepare_codex_subscription_auth(
        self,
        request: SessionDispatchRequest,
    ) -> PreparedCodexCredentialSnapshot:
        if not _is_codex_subscription_agent(request.agent):
            raise RuntimeError("credential preparation requires a Codex subscription agent")
        authority = getattr(self, "_credential_authority", None)
        if authority is not None:
            return authority.prepare_snapshot()
        if getattr(self, "_internal_headers", None):
            raise SessionFileSecurityError("release Gateway credential authority is unavailable")
        temporary_authority = HeldCodexCredentialAuthority.open(
            Path.home() / ".codex" / "auth.json"
        )
        try:
            return temporary_authority.prepare_snapshot()
        finally:
            temporary_authority.close()

    def verify_credential_authority(self) -> None:
        authority = getattr(self, "_credential_authority", None)
        if authority is None:
            if getattr(self, "_internal_headers", None):
                raise SessionFileSecurityError(
                    "release Gateway credential authority is unavailable"
                )
            return
        authority.verify()

    @staticmethod
    def _canonicalize_request_capture_mode(request: SessionDispatchRequest) -> None:
        canonicalize_capture_mode(request.agent.settings)

    @staticmethod
    def _validate_subscription_admission(
        request: SessionDispatchRequest,
        runtime_spec: RuntimeSpec,
        session_dir: Path | None,
    ) -> None:
        if not _is_subscription_agent(request.agent):
            return
        GatewayNodeManager._canonicalize_request_capture_mode(request)
        if request.agent.harness != "codex":
            raise RuntimeError(
                "managed subscription execution currently requires the Codex harness"
            )
        capture_mode = request.agent.settings.get("capture_mode")
        if not transcript_capture_enabled(capture_mode):
            raise RuntimeError("subscription execution requires transcript capture")
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

        auth_path = Path(os.path.abspath(Path.home() / ".codex" / "auth.json"))
        protected_paths = (
            (auth_path,) if session_dir is None else (auth_path, session_dir.resolve())
        )
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

            resolved_evolution = await self._resolve_and_inject_evolution_context(
                managed,
                harness,
            )
            evolution_injection = (
                resolved_evolution if isinstance(resolved_evolution, _EvolutionInjection) else None
            )
            evolution_env = (
                evolution_injection.env if evolution_injection is not None else resolved_evolution
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
            if (
                evolution_injection is not None
                and evolution_injection.staged.injection_plan is not None
            ):
                receipt = await self._await_with_budget(
                    _runtime_injection_receipt_from_readback(
                        runtime=runtime,
                        target_dir=self.evolution.context.target_dir,
                        plan=evolution_injection.staged.injection_plan,
                    ),
                    managed,
                )
                self._publish_runtime_injection_receipt(managed, receipt)
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
    ) -> _EvolutionInjection | dict[str, str]:
        request = managed.request
        if self.evolution is None or not self.evolution.enabled or self.evolution_client is None:
            return {}
        if managed.runtime is None:
            return {}
        expected_artifact_ids = _admitted_context_artifact_ids(request.metadata)
        revision_id = _admitted_revision_id(
            request.metadata,
            required=bool(expected_artifact_ids),
        )
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
            staged = await self._await_with_budget(
                _stage_evolution_context_files(
                    runtime=managed.runtime,
                    context=context,
                    host_dir=managed.session_dir,
                    target_dir=self.evolution.context.target_dir,
                    expected_artifact_ids=expected_artifact_ids,
                    instruction=request.instruction,
                    revision_id=revision_id,
                ),
                managed,
            )
            request.instruction = (
                staged.injection_plan.effective_instruction
                if staged.injection_plan is not None
                else _instruction_with_evolution_context(request.instruction, context)
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
            if staged.injection_plan is not None:
                return _EvolutionInjection(env=dict(staged.env), staged=staged)
            return dict(staged.env)
        except Exception as exc:
            if expected_artifact_ids is not None or not self.evolution.context.fail_open:
                raise
            request.metadata["evolution"] = {
                **_existing_evolution_metadata(request.metadata),
                "context_injected": False,
                "error": "context_resolution_failed",
            }
            self._log_credential_safe_exception(
                managed,
                "Evolution context resolution failed",
                exc,
                level=logging.WARNING,
            )
            return {}

    def _publish_runtime_injection_receipt(
        self,
        managed: ManagedSession,
        receipt: dict[str, object],
    ) -> None:
        request = managed.request
        evolution_metadata = _existing_evolution_metadata(request.metadata)
        if evolution_metadata.get("context_id") != receipt.get("context_id"):
            raise ValueError("runtime injection receipt context changed before publication")
        evolution_metadata["runtime_injection_receipt"] = receipt
        request.metadata["evolution"] = evolution_metadata
        session_registry = getattr(self, "session_registry", None)
        if session_registry is not None:
            session_registry.update_metadata(
                request.session_id,
                {"evolution": evolution_metadata},
            )

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
        cancel_requested: bool | object = _UNSET,
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
                cancel_requested=cancel_requested,
            ),
        )
        updated = self._persist_cleanup_ownership(updated)
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

    async def _drain_and_stop_postrun_runtimes(
        self,
        managed: ManagedSession,
        *,
        subscription_retry: bool,
    ) -> tuple[bool, list[BaseException]]:
        """Drain prewarm and attempt every runtime stop despite cancellation."""

        errors: list[BaseException] = []
        eval_runtime = managed.eval_runtime
        drained, drain_errors = await self._await_cleanup_to_completion(
            self._drain_eval_prewarm_task(managed)
        )
        errors.extend(drain_errors)
        if drained is not None:
            eval_runtime = drained

        targets = [
            (eval_runtime, "eval runtime"),
            (managed.runtime, "runtime"),
        ]
        runtimes_removed = not managed.runtime_cleanup_blocked
        if subscription_retry:
            stopped, stop_errors = await self._await_cleanup_to_completion(
                self._stop_subscription_runtimes_with_retry(
                    targets,
                    managed.session_id,
                )
            )
            errors.extend(stop_errors)
            runtimes_removed = runtimes_removed and stopped is True
        else:
            stop_results, stop_errors = await self._await_cleanup_to_completion(
                asyncio.gather(
                    *(
                        self._stop_runtime_best_effort(
                            runtime,
                            managed.session_id,
                            label,
                        )
                        for runtime, label in targets
                        if runtime is not None
                    ),
                    return_exceptions=True,
                )
            )
            errors.extend(stop_errors)
            if isinstance(stop_results, list):
                for outcome in stop_results:
                    if isinstance(outcome, BaseException):
                        errors.append(outcome)
                runtimes_removed = runtimes_removed and all(
                    outcome is True for outcome in stop_results
                )
            else:
                runtimes_removed = False
        return runtimes_removed, errors

    @staticmethod
    async def _await_cleanup_to_completion(awaitable) -> tuple[Any, list[BaseException]]:
        """Keep one cleanup operation alive while preserving caller cancellation."""

        future = asyncio.ensure_future(awaitable)
        errors: list[BaseException] = []
        while True:
            try:
                return await asyncio.shield(future), errors
            except asyncio.CancelledError as exc:
                errors.append(exc)
                if not future.done():
                    continue
                try:
                    return future.result(), errors
                except asyncio.CancelledError:
                    return None, errors
                except BaseException as operation_error:
                    errors.append(operation_error)
                    return None, errors
            except BaseException as exc:
                errors.append(exc)
                return None, errors

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
        runtimes_removed = False
        primary_error: BaseException | None = None
        teardown_errors: list[BaseException] = []
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
        except BaseException as exc:
            primary_error = exc
        finally:
            managed.timer.mark("postrun", "finished")
            managed.timer.mark("teardown", "started")
            try:
                await self._run_postrun_steps(managed)
            except BaseException as exc:
                teardown_errors.append(exc)
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
            except BaseException as exc:
                teardown_errors.append(exc)
            runtimes_removed, stop_errors = await self._drain_and_stop_postrun_runtimes(
                managed,
                subscription_retry=False,
            )
            teardown_errors.extend(stop_errors)
            managed.timer.mark("teardown", "finished")
            managed.timer.mark("return", "finished")

        if primary_error is not None or teardown_errors:
            try:
                self._register_cleanup_retry(managed, eval_runtime=managed.eval_runtime)
            except BaseException as exc:
                teardown_errors.append(exc)
            failures: list[BaseException] = []
            if primary_error is not None:
                failures.append(primary_error)
            failures.extend(teardown_errors)
            if len(failures) == 1:
                raise failures[0]
            raise BaseExceptionGroup("post-run failed and cleanup did not complete", failures)

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
                    ownership = self._cleanup_retries[request.session_id]
                    self._retire_cleanup_ownership(ownership)
                    self._cleanup_retries.pop(request.session_id, None)
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
        eval_runtime = managed.eval_runtime
        runtimes_removed = False
        managed.timer.mark("postrun", "started")
        try:
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
        finally:
            managed.timer.mark("teardown", "started")
            try:
                self._register_cleanup_retry(
                    managed,
                    eval_runtime=managed.eval_runtime,
                    finalize_subscription=True,
                )
            finally:
                runtimes_removed, teardown_errors = await self._drain_and_stop_postrun_runtimes(
                    managed,
                    subscription_retry=True,
                )
                managed.timer.mark("teardown", "finished")
                if runtimes_removed:
                    self._record_cleanup_runtimes_absent(managed)
                else:
                    self._register_cleanup_retry(
                        managed,
                        eval_runtime=eval_runtime or managed.eval_runtime,
                        finalize_subscription=True,
                    )
                    logger.error(
                        "Retaining subscription cleanup ownership because runtime absence "
                        "was not proven for session %s",
                        request.session_id,
                    )
                if teardown_errors:
                    logger.error(
                        "Subscription teardown retained cleanup retry after %d base error(s) "
                        "for session %s",
                        len(teardown_errors),
                        request.session_id,
                    )

        if not runtimes_removed:
            return
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
            if result is None and managed.agent_result is not None:
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
                ownership = self._cleanup_retries[request.session_id]
                self._retire_cleanup_ownership(ownership)
                self._cleanup_retries.pop(request.session_id, None)
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
        ownership = self._persist_cleanup_ownership(ownership)
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
        updated = self._persist_cleanup_ownership(updated)
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
        updated = self._persist_cleanup_ownership(updated)
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
        updated_ownership = self._persist_cleanup_ownership(updated_ownership)
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
        cancel_requested: bool | object = _UNSET,
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
            cancel_requested=(
                managed.cancel_requested
                if cancel_requested is _UNSET
                else cast(bool, cancel_requested)
            ),
            timer_marks=dict(managed.timer._marks),
        )

    def _cleanup_ownership_for(
        self,
        managed: ManagedSession,
        *,
        eval_runtime: BaseRuntime | None = None,
        finalize_subscription: bool = False,
    ) -> CleanupRetryOwnership:
        runtime = managed.runtime
        finalization_state = (
            self._subscription_finalization_state(managed) if finalize_subscription else None
        )
        epoch = managed.cleanup_journal_epoch
        epoch_token = managed.cleanup_journal_epoch_token
        if (
            epoch is None
            and epoch_token is None
            and getattr(self, "_cleanup_journal_dir", None) is not None
        ):
            current_epoch = self._capture_cleanup_journal_creation_epoch()
            epoch = current_epoch.epoch
            epoch_token = current_epoch.token
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
            revision=managed.cleanup_journal_revision,
            generation=managed.cleanup_journal_generation,
            epoch=epoch,
            epoch_token=epoch_token,
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

    @staticmethod
    def _cleanup_journal_epoch_payload(epoch: _CleanupJournalEpoch) -> bytes:
        payload = {
            "version": 1,
            "epoch": epoch.epoch,
            "token": epoch.token,
            "previous_token": epoch.previous_token,
            "retired_count": epoch.retired_count,
            "retirement_digest": epoch.retirement_digest,
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(encoded) > _CLEANUP_JOURNAL_EPOCH_MAX_BYTES:
            raise RuntimeError("cleanup journal epoch exceeds its byte limit")
        return encoded

    @staticmethod
    def _cleanup_journal_epoch_from_payload(payload: object) -> _CleanupJournalEpoch:
        if not isinstance(payload, dict) or set(payload) != {
            "version",
            "epoch",
            "token",
            "previous_token",
            "retired_count",
            "retirement_digest",
        }:
            raise ValueError("cleanup journal epoch payload is not closed")
        epoch = payload["epoch"]
        token = payload["token"]
        previous_token = payload["previous_token"]
        retired_count = payload["retired_count"]
        retirement_digest = payload["retirement_digest"]
        if (
            payload["version"] != 1
            or isinstance(epoch, bool)
            or not isinstance(epoch, int)
            or epoch < 0
            or not isinstance(token, str)
            or _CLEANUP_JOURNAL_GENERATION_RE.fullmatch(token) is None
            or (
                previous_token is not None
                and (
                    not isinstance(previous_token, str)
                    or _CLEANUP_JOURNAL_GENERATION_RE.fullmatch(previous_token) is None
                )
            )
            or (epoch == 0) != (previous_token is None)
            or isinstance(retired_count, bool)
            or not isinstance(retired_count, int)
            or retired_count < 0
            or not isinstance(retirement_digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", retirement_digest) is None
            or (
                retired_count == 0 and retirement_digest != _CLEANUP_JOURNAL_RETIREMENT_DIGEST_SEED
            )
            or (epoch == 0) != (retired_count == 0)
            or (retired_count > 0 and retirement_digest == _CLEANUP_JOURNAL_RETIREMENT_DIGEST_SEED)
        ):
            raise ValueError("cleanup journal epoch identity is invalid")
        return _CleanupJournalEpoch(
            epoch=epoch,
            token=token,
            previous_token=previous_token,
            retired_count=retired_count,
            retirement_digest=retirement_digest,
        )

    @staticmethod
    def _cleanup_journal_root_marker_payload(
        *,
        path: Path,
        ancestor_identities: tuple[tuple[int, int, int, int], ...],
        root_identity: tuple[int, int, int, int],
        version: int,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "version": version,
            "path": str(path),
            "ancestor_identities": [list(item) for item in ancestor_identities],
            "root_identity": list(root_identity),
        }
        if version == 2:
            payload["epoch_required"] = True
        return payload

    @staticmethod
    def _encode_cleanup_journal_root_marker(payload: dict[str, Any]) -> bytes:
        encoded = json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(encoded) > _CLEANUP_JOURNAL_ROOT_MARKER_MAX_BYTES:
            raise RuntimeError("cleanup journal root identity marker exceeds its limit")
        return encoded

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
                initial_epoch = _CleanupJournalEpoch(
                    epoch=0,
                    token=secrets.token_hex(16),
                    previous_token=None,
                    retired_count=0,
                    retirement_digest=_CLEANUP_JOURNAL_RETIREMENT_DIGEST_SEED,
                )
                epoch_fd = os.open(
                    _CLEANUP_JOURNAL_EPOCH_NAME,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=root_fd,
                )
                try:
                    self._write_all(
                        epoch_fd,
                        self._cleanup_journal_epoch_payload(initial_epoch),
                        label="cleanup journal epoch",
                    )
                    os.fchmod(epoch_fd, 0o600)
                    os.fsync(epoch_fd)
                finally:
                    os.close(epoch_fd)
                os.fsync(root_fd)
                marker_payload = self._cleanup_journal_root_marker_payload(
                    path=path,
                    ancestor_identities=tuple(ancestor_identities),
                    root_identity=root_identity,
                    version=2,
                )
                marker_bytes = self._encode_cleanup_journal_root_marker(marker_payload)
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
                os.fsync(parent_fd)
                marker_version = 2
            elif root_before is None or not marker_exists:
                raise RuntimeError("cleanup journal root identity authority is incomplete")
            else:
                marker_payload = self._read_cleanup_journal_root_marker(
                    parent_fd,
                    marker_name,
                )
                marker_version = (
                    marker_payload.get("version") if isinstance(marker_payload, dict) else None
                )
                expected_marker_keys = {
                    "version",
                    "path",
                    "ancestor_identities",
                    "root_identity",
                }
                if marker_version == 2:
                    expected_marker_keys.add("epoch_required")
                if (
                    not isinstance(marker_payload, dict)
                    or marker_version not in {1, 2}
                    or set(marker_payload) != expected_marker_keys
                    or (marker_version == 2 and marker_payload["epoch_required"] is not True)
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
                if marker_version == 2:
                    try:
                        os.stat(
                            _CLEANUP_JOURNAL_EPOCH_NAME,
                            dir_fd=root_fd,
                            follow_symlinks=False,
                        )
                    except FileNotFoundError as exc:
                        raise RuntimeError("cleanup journal epoch authority is missing") from exc

            return _CleanupJournalAuthority(
                path=path,
                ancestor_fds=ancestor_fds,
                ancestor_identities=tuple(ancestor_identities),
                root_fd=root_fd,
                root_identity=root_identity,
                marker_version=marker_version,
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

    def _acquire_cleanup_journal_lock(
        self,
        authority: _CleanupJournalAuthority,
    ) -> int:
        """Acquire the bounded process lock bound to one held journal root."""

        self._verify_cleanup_journal_authority(authority)
        created = False
        try:
            descriptor = os.open(
                _CLEANUP_JOURNAL_LOCK_NAME,
                os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                0o600,
                dir_fd=authority.root_fd,
            )
            created = True
        except FileExistsError:
            descriptor = os.open(
                _CLEANUP_JOURNAL_LOCK_NAME,
                os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
                dir_fd=authority.root_fd,
            )
        try:
            opened = os.fstat(descriptor)
            rebound = os.stat(
                _CLEANUP_JOURNAL_LOCK_NAME,
                dir_fd=authority.root_fd,
                follow_symlinks=False,
            )
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_uid != os.geteuid()
                or opened.st_nlink != 1
                or stat.S_IMODE(opened.st_mode) != 0o600
                or self._cleanup_journal_identity(opened)
                != self._cleanup_journal_identity(rebound)
            ):
                raise RuntimeError("cleanup journal process lock is invalid")
            if created:
                os.fsync(descriptor)
                self._fsync_cleanup_journal_directory(authority.root_fd)

            deadline = time.monotonic() + _CLEANUP_JOURNAL_LOCK_TIMEOUT_SECONDS
            while True:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except InterruptedError:
                    continue
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise RuntimeError("cleanup journal process lock timed out")
                    time.sleep(_CLEANUP_JOURNAL_LOCK_POLL_SECONDS)

            locked = os.fstat(descriptor)
            rebound = os.stat(
                _CLEANUP_JOURNAL_LOCK_NAME,
                dir_fd=authority.root_fd,
                follow_symlinks=False,
            )
            if (
                self._cleanup_journal_identity(locked) != self._cleanup_journal_identity(opened)
                or self._cleanup_journal_identity(rebound)
                != self._cleanup_journal_identity(opened)
                or locked.st_nlink != 1
                or stat.S_IMODE(locked.st_mode) != 0o600
            ):
                raise RuntimeError("cleanup journal process lock changed during acquisition")
            self._verify_cleanup_journal_authority(authority)
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    @staticmethod
    def _release_cleanup_journal_lock(descriptor: int) -> None:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)

    def _verify_cleanup_journal_lock(
        self,
        authority: _CleanupJournalAuthority,
        descriptor: int,
    ) -> None:
        opened = os.fstat(descriptor)
        rebound = os.stat(
            _CLEANUP_JOURNAL_LOCK_NAME,
            dir_fd=authority.root_fd,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_uid != os.geteuid()
            or opened.st_nlink != 1
            or stat.S_IMODE(opened.st_mode) != 0o600
            or self._cleanup_journal_identity(opened) != self._cleanup_journal_identity(rebound)
        ):
            raise RuntimeError("cleanup journal process lock identity changed")

    def _seal_cleanup_journal_epoch_requirement(
        self,
        authority: _CleanupJournalAuthority,
    ) -> None:
        if authority.marker_version == 2:
            return
        parent_fd = authority.ancestor_fds[-1]
        marker_name = self._cleanup_journal_marker_name(authority.path)
        candidate_name = f"{marker_name}.v2.tmp"
        payload = self._cleanup_journal_root_marker_payload(
            path=authority.path,
            ancestor_identities=authority.ancestor_identities,
            root_identity=authority.root_identity,
            version=2,
        )
        encoded = self._encode_cleanup_journal_root_marker(payload)
        descriptor = -1
        try:
            try:
                descriptor = os.open(
                    candidate_name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=parent_fd,
                )
                self._write_all(
                    descriptor,
                    encoded,
                    label="cleanup journal epoch root marker",
                )
                os.fchmod(descriptor, 0o600)
                os.fsync(descriptor)
                os.close(descriptor)
                descriptor = -1
            except FileExistsError:
                existing = self._read_cleanup_journal_root_marker(
                    parent_fd,
                    candidate_name,
                )
                if existing != payload:
                    raise RuntimeError("cleanup journal epoch root marker candidate is invalid")
            os.replace(
                candidate_name,
                marker_name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
            )
            os.fsync(parent_fd)
            authority.marker_version = 2
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    def _read_cleanup_journal_epoch(
        self,
        authority: _CleanupJournalAuthority,
        name: str = _CLEANUP_JOURNAL_EPOCH_NAME,
        *,
        allow_missing: bool = False,
    ) -> _CleanupJournalEpoch | None:
        raw = self._read_private_cleanup_file(
            name,
            root_fd=authority.root_fd,
            max_bytes=_CLEANUP_JOURNAL_EPOCH_MAX_BYTES,
            allow_missing=allow_missing,
        )
        if raw is None:
            return None
        try:
            payload = json.loads(raw.decode("utf-8"))
            return self._cleanup_journal_epoch_from_payload(payload)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise RuntimeError("cleanup journal epoch authority is invalid") from exc

    def _ensure_cleanup_journal_epoch(
        self,
        authority: _CleanupJournalAuthority,
    ) -> _CleanupJournalEpoch:
        current = self._read_cleanup_journal_epoch(authority, allow_missing=True)
        if current is None:
            if authority.marker_version == 2:
                raise RuntimeError("cleanup journal epoch authority is missing")
            current = _CleanupJournalEpoch(
                epoch=0,
                token=secrets.token_hex(16),
                previous_token=None,
                retired_count=0,
                retirement_digest=_CLEANUP_JOURNAL_RETIREMENT_DIGEST_SEED,
            )
            descriptor = os.open(
                _CLEANUP_JOURNAL_EPOCH_NAME,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                0o600,
                dir_fd=authority.root_fd,
            )
            try:
                self._write_all(
                    descriptor,
                    self._cleanup_journal_epoch_payload(current),
                    label="cleanup journal epoch",
                )
                os.fchmod(descriptor, 0o600)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            self._fsync_cleanup_journal_directory(authority.root_fd)

        candidate = self._read_cleanup_journal_epoch(
            authority,
            _CLEANUP_JOURNAL_EPOCH_CANDIDATE_NAME,
            allow_missing=True,
        )
        if candidate is not None:
            if (
                candidate.epoch != current.epoch + 1
                or candidate.previous_token != current.token
                or candidate.retired_count <= current.retired_count
                or candidate.retirement_digest == current.retirement_digest
            ):
                raise RuntimeError("cleanup journal epoch candidate is invalid")
            os.unlink(_CLEANUP_JOURNAL_EPOCH_CANDIDATE_NAME, dir_fd=authority.root_fd)
            self._fsync_cleanup_journal_directory(authority.root_fd)

        self._seal_cleanup_journal_epoch_requirement(authority)
        self._verify_cleanup_journal_authority(authority)
        return current

    def _capture_cleanup_journal_creation_epoch(self) -> _CleanupJournalEpoch:
        authority = self._open_cleanup_journal_authority(initialize=True)
        if authority is None:
            raise RuntimeError("cleanup journal root identity authority is unavailable")
        lock_fd = -1
        try:
            lock_fd = self._acquire_cleanup_journal_lock(authority)
            epoch = self._ensure_cleanup_journal_epoch(authority)
            self._verify_cleanup_journal_lock(authority, lock_fd)
            return epoch
        finally:
            if lock_fd >= 0:
                self._release_cleanup_journal_lock(lock_fd)
            authority.close()

    def _cleanup_journal_path(self, session_id: str) -> Path:
        return self._cleanup_journal_dir / self._cleanup_journal_name(session_id)

    @staticmethod
    def _cleanup_journal_name(session_id: str) -> str:
        name = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
        return f"{name}.json"

    @staticmethod
    def _cleanup_ownership_payload(ownership: CleanupRetryOwnership) -> dict[str, Any]:
        state = ownership.finalization_state
        delivery = ownership.delivery_state
        return {
            "version": 9,
            "kind": "active",
            "epoch": ownership.epoch,
            "epoch_token": ownership.epoch_token,
            "generation": ownership.generation,
            "revision": ownership.revision,
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

    @staticmethod
    def _cleanup_tombstone_payload(
        ownership: CleanupRetryOwnership,
        *,
        generation: str,
        retirement_epoch: _CleanupJournalEpoch,
    ) -> dict[str, Any]:
        delivery = ownership.delivery_state
        return {
            "version": 9,
            "kind": "retired",
            "session_id": ownership.session_id,
            "epoch": ownership.epoch,
            "epoch_token": ownership.epoch_token,
            "retired_epoch": retirement_epoch.epoch,
            "retired_epoch_token": retirement_epoch.token,
            "generation": generation,
            "revision": ownership.revision + 1,
            "terminal_delivery": (
                None
                if delivery is None
                else {
                    "result_digest": delivery.result_digest,
                    "export_succeeded": delivery.export_succeeded,
                    "callback_succeeded": delivery.callback_succeeded,
                }
            ),
        }

    @staticmethod
    def _merge_cleanup_journal_cancel_authority(
        previous: CleanupRetryOwnership | None,
        candidate: CleanupRetryOwnership,
    ) -> CleanupRetryOwnership:
        if (
            previous is None
            or previous.finalization_state is None
            or not previous.finalization_state.cancel_requested
            or candidate.finalization_state is None
            or candidate.finalization_state.cancel_requested
        ):
            return candidate
        return replace(
            candidate,
            finalization_state=replace(
                candidate.finalization_state,
                cancel_requested=True,
            ),
        )

    @staticmethod
    def _validate_cleanup_journal_transition(
        previous: CleanupRetryOwnership | None,
        candidate: CleanupRetryOwnership,
    ) -> None:
        expected_revision = 0 if previous is None else previous.revision
        if candidate.revision != expected_revision:
            raise RuntimeError(
                "cleanup journal revision compare-and-swap failed: "
                f"expected {expected_revision}, got {candidate.revision}"
            )
        if previous is not None and candidate.generation != previous.generation:
            raise RuntimeError("cleanup journal generation compare-and-swap failed")
        if previous is None and candidate.generation is not None:
            raise RuntimeError("cleanup journal generation cannot precede its authority")
        if (
            previous is not None
            and previous.epoch is not None
            and (
                candidate.epoch != previous.epoch or candidate.epoch_token != previous.epoch_token
            )
        ):
            raise RuntimeError("cleanup journal epoch compare-and-swap failed")
        if candidate.phase not in _RECOVERY_PHASE_ORDER:
            raise RuntimeError("cleanup journal candidate phase is invalid")
        if previous is None:
            return
        if previous.session_id != candidate.session_id:
            raise RuntimeError("cleanup journal session identity changed")
        if (
            previous.session_dir != candidate.session_dir
            or previous.session_root_identity != candidate.session_root_identity
            or previous.log_authority_dir != candidate.log_authority_dir
            or previous.log_authority_identity != candidate.log_authority_identity
        ):
            raise RuntimeError("cleanup journal root authority changed")
        if previous.credential_dir is not None and (
            previous.credential_dir != candidate.credential_dir
            or previous.credential_root_identity != candidate.credential_root_identity
        ):
            raise RuntimeError("cleanup journal credential authority changed")
        previous_phase = previous.phase
        if previous_phase not in _RECOVERY_PHASE_ORDER:
            raise RuntimeError("cleanup journal authoritative phase is invalid")
        if _RECOVERY_PHASE_ORDER[candidate.phase] < _RECOVERY_PHASE_ORDER[previous_phase]:
            raise RuntimeError("cleanup journal phase cannot regress")

        previous_finalization = previous.finalization_state
        candidate_finalization = candidate.finalization_state
        candidate_delivery = candidate.delivery_state
        if previous_finalization is not None:
            if candidate_finalization is None:
                raise RuntimeError(
                    "cleanup journal transition cannot discard finalization authority"
                )
            if previous_finalization.request != candidate_finalization.request:
                raise RuntimeError("cleanup journal finalization request identity changed")
            if (
                previous_finalization.cancel_requested
                and not candidate_finalization.cancel_requested
            ):
                raise RuntimeError("cleanup journal transition cannot discard cancel authority")
            for label, old, new in (
                (
                    "agent result",
                    previous_finalization.agent_result,
                    candidate_finalization.agent_result,
                ),
                (
                    "final result",
                    previous_finalization.final_result,
                    candidate_finalization.final_result,
                ),
                (
                    "pending status",
                    previous_finalization.pending_status,
                    candidate_finalization.pending_status,
                ),
                (
                    "pending error",
                    previous_finalization.pending_error,
                    candidate_finalization.pending_error,
                ),
            ):
                if old is not None and new != old:
                    if (
                        label == "final result"
                        and candidate_delivery is not None
                        and new == candidate_delivery.result
                    ):
                        continue
                    raise RuntimeError(
                        f"cleanup journal transition cannot discard or change {label}"
                    )

        previous_delivery = previous.delivery_state
        if previous_delivery is not None:
            if candidate_delivery is None:
                raise RuntimeError("cleanup journal transition cannot discard terminal delivery")
            if (
                previous_delivery.result != candidate_delivery.result
                or previous_delivery.result_digest != candidate_delivery.result_digest
                or previous_delivery.callback_url != candidate_delivery.callback_url
                or previous_delivery.export_required != candidate_delivery.export_required
                or previous_delivery.export_authority != candidate_delivery.export_authority
                or previous_delivery.callback_required != candidate_delivery.callback_required
            ):
                raise RuntimeError("cleanup journal terminal delivery identity changed")
            if previous_delivery.export_succeeded and not candidate_delivery.export_succeeded:
                raise RuntimeError("cleanup journal export proof cannot regress")
            if previous_delivery.callback_succeeded and not candidate_delivery.callback_succeeded:
                raise RuntimeError("cleanup journal callback proof cannot regress")

    def _persist_cleanup_ownership(
        self,
        ownership: CleanupRetryOwnership,
    ) -> CleanupRetryOwnership:
        journal_dir = getattr(self, "_cleanup_journal_dir", None)
        if journal_dir is None:
            return ownership
        authority = self._open_cleanup_journal_authority(initialize=True)
        if authority is None:
            raise RuntimeError("cleanup journal root identity authority is unavailable")
        try:
            self._verify_cleanup_journal_authority(authority)
        except Exception:
            authority.close()
            raise
        lock_fd = -1
        destination = self._cleanup_journal_name(ownership.session_id)
        try:
            lock_fd = self._acquire_cleanup_journal_lock(authority)
            current_epoch = self._ensure_cleanup_journal_epoch(authority)
            previous = self._read_private_cleanup_file(
                destination,
                root_fd=authority.root_fd,
                max_bytes=_CLEANUP_JOURNAL_MAX_BYTES,
                allow_missing=True,
            )
            previous_ownership = None
            if previous is not None:
                try:
                    previous_payload = json.loads(previous.decode("utf-8"))
                    previous_record = self._cleanup_journal_record_from_payload(
                        previous_payload,
                        Path(destination),
                    )
                    if isinstance(previous_record, _CleanupJournalTombstone):
                        raise RuntimeError("cleanup ownership generation is already retired")
                    previous_ownership = previous_record
                except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
                    raise RuntimeError("cleanup ownership journal is invalid") from exc
            if previous_ownership is None:
                if (
                    ownership.epoch != current_epoch.epoch
                    or ownership.epoch_token != current_epoch.token
                ):
                    raise RuntimeError("cleanup journal creation epoch is stale")
                inventory = self._inventory_cleanup_journal_locked(authority)
                if len(inventory) >= _CLEANUP_JOURNAL_COMPACT_AT_ROWS:
                    records = self._parse_cleanup_journal_inventory_locked(
                        authority,
                        inventory,
                    )
                    current_epoch = self._compact_cleanup_journal_locked(
                        authority=authority,
                        lock_fd=lock_fd,
                        epoch=current_epoch,
                        records=records,
                    )
                    inventory = [
                        (name, expected)
                        for name, expected, record in records
                        if isinstance(record, CleanupRetryOwnership)
                    ]
                    ownership = replace(
                        ownership,
                        epoch=current_epoch.epoch,
                        epoch_token=current_epoch.token,
                    )
                if len(inventory) >= _CLEANUP_JOURNAL_MAX_ROWS:
                    raise RuntimeError("cleanup journal capacity is occupied by active records")
            elif previous_ownership.epoch is None:
                ownership = replace(
                    ownership,
                    epoch=current_epoch.epoch,
                    epoch_token=current_epoch.token,
                )
            ownership = self._merge_cleanup_journal_cancel_authority(
                previous_ownership,
                ownership,
            )
            self._validate_cleanup_journal_transition(previous_ownership, ownership)
            ownership = replace(
                ownership,
                revision=(0 if previous_ownership is None else previous_ownership.revision) + 1,
                generation=(
                    secrets.token_hex(16)
                    if previous_ownership is None or previous_ownership.generation is None
                    else previous_ownership.generation
                ),
            )
            payload = self._cleanup_ownership_payload(ownership)
            try:
                self._cleanup_ownership_from_payload(payload, Path(destination))
            except ValueError as exc:
                raise RuntimeError("cleanup ownership journal candidate is invalid") from exc
            encoded = json.dumps(
                payload,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            if len(encoded) > _CLEANUP_JOURNAL_MAX_BYTES:
                raise RuntimeError("cleanup ownership journal exceeds the byte limit")
            self._commit_cleanup_journal_record(
                authority=authority,
                lock_fd=lock_fd,
                destination=destination,
                previous=previous,
                encoded=encoded,
                session_id=ownership.session_id,
                operation_id=id(ownership),
            )
            if ownership.managed is not None:
                ownership.managed.cleanup_journal_revision = ownership.revision
                ownership.managed.cleanup_journal_generation = ownership.generation
                ownership.managed.cleanup_journal_epoch = ownership.epoch
                ownership.managed.cleanup_journal_epoch_token = ownership.epoch_token
            return ownership
        finally:
            if lock_fd >= 0:
                self._release_cleanup_journal_lock(lock_fd)
            authority.close()

    def _commit_cleanup_journal_record(
        self,
        *,
        authority: _CleanupJournalAuthority,
        lock_fd: int,
        destination: str,
        previous: bytes | None,
        encoded: bytes,
        session_id: str,
        operation_id: int,
    ) -> None:
        temporary = f".{destination}.{os.getpid()}.{operation_id}.tmp"
        pending = f"{destination.removesuffix('.json')}.pending"
        rollback = f".{destination}.rollback.tmp"
        replaced = False
        descriptor = -1
        try:
            self._ensure_cleanup_pending_marker(pending, authority.root_fd)
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                0o600,
                dir_fd=authority.root_fd,
            )
            self._write_all(descriptor, encoded, label="cleanup ownership journal")
            os.fchmod(descriptor, 0o600)
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1
            os.replace(
                temporary,
                destination,
                src_dir_fd=authority.root_fd,
                dst_dir_fd=authority.root_fd,
            )
            replaced = True
            self._fsync_cleanup_journal_directory(authority.root_fd)
            os.unlink(pending, dir_fd=authority.root_fd)
            self._fsync_cleanup_journal_directory(authority.root_fd)
            self._verify_cleanup_journal_authority(authority)
            self._verify_cleanup_journal_lock(authority, lock_fd)
        except Exception:
            if descriptor >= 0:
                os.close(descriptor)
                descriptor = -1
            if replaced:
                try:
                    if previous is None:
                        try:
                            os.unlink(destination, dir_fd=authority.root_fd)
                        except FileNotFoundError:
                            pass
                    else:
                        rollback_fd = os.open(
                            rollback,
                            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                            0o600,
                            dir_fd=authority.root_fd,
                        )
                        try:
                            self._write_all(
                                rollback_fd,
                                previous,
                                label="cleanup ownership journal rollback",
                            )
                            os.fsync(rollback_fd)
                        finally:
                            os.close(rollback_fd)
                        os.replace(
                            rollback,
                            destination,
                            src_dir_fd=authority.root_fd,
                            dst_dir_fd=authority.root_fd,
                        )
                    self._fsync_cleanup_journal_directory(authority.root_fd)
                except Exception:
                    logger.error(
                        "Cleanup ownership journal rollback could not be proven for %s",
                        session_id,
                    )
            try:
                self._ensure_cleanup_pending_marker(pending, authority.root_fd)
            except OSError:
                logger.error(
                    "Cleanup ownership journal pending marker could not be proven for %s",
                    session_id,
                )
            raise
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            for leftover in (temporary, rollback):
                try:
                    os.unlink(leftover, dir_fd=authority.root_fd)
                except FileNotFoundError:
                    pass

    @staticmethod
    def _fsync_cleanup_journal_directory(root_fd: int) -> None:
        os.fsync(root_fd)

    @classmethod
    def _ensure_cleanup_pending_marker(
        cls,
        pending: str,
        root_fd: int,
    ) -> None:
        descriptor = -1
        try:
            try:
                descriptor = os.open(
                    pending,
                    os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=root_fd,
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
                    dir_fd=root_fd,
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
        cls._fsync_cleanup_journal_directory(root_fd)

    @staticmethod
    def _read_private_cleanup_file(
        name: str,
        *,
        root_fd: int,
        max_bytes: int,
        allow_missing: bool,
    ) -> bytes | None:
        try:
            descriptor = os.open(
                name,
                os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
                dir_fd=root_fd,
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

    def _inventory_cleanup_journal_locked(
        self,
        authority: _CleanupJournalAuthority,
    ) -> list[tuple[str, tuple[int, int, int, int, int, int]]]:
        records: list[tuple[str, tuple[int, int, int, int, int, int]]] = []
        metadata_bytes = 0
        total_bytes = 0
        with os.scandir(authority.root_fd) as entries:
            for entry in entries:
                name = entry.name
                filename_bytes = os.fsencode(name)
                if len(filename_bytes) > _CLEANUP_JOURNAL_MAX_FILENAME_BYTES:
                    raise RuntimeError("cleanup ownership journal exceeds the filename budget")
                metadata_bytes += len(filename_bytes) + 6 * 8
                if metadata_bytes > _CLEANUP_JOURNAL_MAX_METADATA_BYTES:
                    raise RuntimeError("cleanup ownership journal exceeds the metadata budget")
                if name in {_CLEANUP_JOURNAL_LOCK_NAME, _CLEANUP_JOURNAL_EPOCH_NAME}:
                    continue
                if name == _CLEANUP_JOURNAL_EPOCH_CANDIDATE_NAME:
                    raise RuntimeError("cleanup journal has an incomplete epoch rotation")
                if _CLEANUP_JOURNAL_PENDING_RE.fullmatch(name) is not None:
                    raise RuntimeError("cleanup ownership journal has an incomplete update")
                if _CLEANUP_JOURNAL_RECORD_RE.fullmatch(name) is None:
                    raise RuntimeError("cleanup ownership journal filename metadata is invalid")
                if len(records) >= _CLEANUP_JOURNAL_MAX_ROWS:
                    raise RuntimeError("cleanup ownership journal exceeds the row budget")
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
        return records

    def _parse_cleanup_journal_inventory_locked(
        self,
        authority: _CleanupJournalAuthority,
        inventory: list[tuple[str, tuple[int, int, int, int, int, int]]],
    ) -> list[
        tuple[
            str,
            tuple[int, int, int, int, int, int],
            CleanupRetryOwnership | _CleanupJournalTombstone,
        ]
    ]:
        parsed = []
        session_ids: set[str] = set()
        for name, expected in sorted(inventory):
            raw = self._read_cleanup_journal_record(authority, name, expected)
            try:
                payload = json.loads(raw.decode("utf-8"))
                record = self._cleanup_journal_record_from_payload(
                    payload,
                    authority.path / name,
                )
            except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
                raise RuntimeError("cleanup ownership journal is invalid") from exc
            if record.session_id in session_ids:
                raise RuntimeError("cleanup ownership journal session identity is duplicated")
            session_ids.add(record.session_id)
            parsed.append((name, expected, record))
        self._verify_cleanup_journal_authority(authority)
        return parsed

    def _cleanup_journal_compaction_checkpoint(self, label: str) -> None:
        del label

    def _compact_cleanup_journal_locked(
        self,
        *,
        authority: _CleanupJournalAuthority,
        lock_fd: int,
        epoch: _CleanupJournalEpoch,
        records: list[
            tuple[
                str,
                tuple[int, int, int, int, int, int],
                CleanupRetryOwnership | _CleanupJournalTombstone,
            ]
        ],
    ) -> _CleanupJournalEpoch:
        retired = [item for item in records if isinstance(item[2], _CleanupJournalTombstone)]
        if not retired:
            return epoch
        unsummarized = []
        for item in retired:
            tombstone = item[2]
            assert isinstance(tombstone, _CleanupJournalTombstone)
            retired_epoch = tombstone.retired_epoch
            if retired_epoch is None:
                retired_epoch = 0
            if retired_epoch > epoch.epoch:
                raise RuntimeError("cleanup tombstone retirement epoch is from the future")
            if retired_epoch == epoch.epoch:
                if (
                    tombstone.retired_epoch_token is not None
                    and tombstone.retired_epoch_token != epoch.token
                ):
                    raise RuntimeError("cleanup tombstone retirement epoch token is invalid")
                unsummarized.append(item)

        next_epoch = epoch
        if unsummarized:
            retirement_digest = epoch.retirement_digest
            for name, expected, _ in unsummarized:
                raw = self._read_cleanup_journal_record(authority, name, expected)
                digest = hashlib.sha256()
                digest.update(b"openevo-cleanup-retirement-entry-v1\0")
                digest.update(bytes.fromhex(retirement_digest))
                encoded_name = name.encode("ascii")
                digest.update(len(encoded_name).to_bytes(4, "big"))
                digest.update(encoded_name)
                digest.update(len(raw).to_bytes(8, "big"))
                digest.update(raw)
                retirement_digest = digest.hexdigest()
            next_epoch = _CleanupJournalEpoch(
                epoch=epoch.epoch + 1,
                token=secrets.token_hex(16),
                previous_token=epoch.token,
                retired_count=epoch.retired_count + len(unsummarized),
                retirement_digest=retirement_digest,
            )
            descriptor = os.open(
                _CLEANUP_JOURNAL_EPOCH_CANDIDATE_NAME,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                0o600,
                dir_fd=authority.root_fd,
            )
            try:
                self._write_all(
                    descriptor,
                    self._cleanup_journal_epoch_payload(next_epoch),
                    label="cleanup journal epoch candidate",
                )
                os.fchmod(descriptor, 0o600)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            self._cleanup_journal_compaction_checkpoint("epoch_candidate_fsynced")
            os.replace(
                _CLEANUP_JOURNAL_EPOCH_CANDIDATE_NAME,
                _CLEANUP_JOURNAL_EPOCH_NAME,
                src_dir_fd=authority.root_fd,
                dst_dir_fd=authority.root_fd,
            )
            self._cleanup_journal_compaction_checkpoint("epoch_replaced")
            self._fsync_cleanup_journal_directory(authority.root_fd)
            self._cleanup_journal_compaction_checkpoint("epoch_directory_fsynced")

        for name, expected, tombstone in retired:
            raw = self._read_cleanup_journal_record(authority, name, expected)
            try:
                current = self._cleanup_journal_record_from_payload(
                    json.loads(raw.decode("utf-8")),
                    authority.path / name,
                )
            except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
                raise RuntimeError(
                    "cleanup ownership tombstone changed before compaction"
                ) from exc
            if current != tombstone:
                raise RuntimeError("cleanup ownership tombstone changed before compaction")
            os.unlink(name, dir_fd=authority.root_fd)
            self._cleanup_journal_compaction_checkpoint("tombstone_unlinked")
        self._fsync_cleanup_journal_directory(authority.root_fd)
        self._cleanup_journal_compaction_checkpoint("tombstones_directory_fsynced")
        self._verify_cleanup_journal_authority(authority)
        self._verify_cleanup_journal_lock(authority, lock_fd)
        return next_epoch

    def _compact_cleanup_journal(self) -> None:
        authority = self._open_cleanup_journal_authority(initialize=False)
        if authority is None:
            return
        lock_fd = -1
        try:
            lock_fd = self._acquire_cleanup_journal_lock(authority)
            epoch = self._ensure_cleanup_journal_epoch(authority)
            inventory = self._inventory_cleanup_journal_locked(authority)
            records = self._parse_cleanup_journal_inventory_locked(authority, inventory)
            self._compact_cleanup_journal_locked(
                authority=authority,
                lock_fd=lock_fd,
                epoch=epoch,
                records=records,
            )
        finally:
            if lock_fd >= 0:
                self._release_cleanup_journal_lock(lock_fd)
            authority.close()

    def _load_cleanup_retries(self) -> None:
        authority = self._open_cleanup_journal_authority(initialize=False)
        if authority is None:
            return
        lock_fd = -1
        try:
            lock_fd = self._acquire_cleanup_journal_lock(authority)
            epoch = self._ensure_cleanup_journal_epoch(authority)
            inventory = self._inventory_cleanup_journal_locked(authority)
            records = self._parse_cleanup_journal_inventory_locked(authority, inventory)
            recovered: dict[str, CleanupRetryOwnership] = {}
            for _, _, record in records:
                if record.session_id in self._cleanup_retries:
                    raise RuntimeError("cleanup ownership journal session identity is duplicated")
                if isinstance(record, CleanupRetryOwnership):
                    recovered[record.session_id] = record
            self._compact_cleanup_journal_locked(
                authority=authority,
                lock_fd=lock_fd,
                epoch=epoch,
                records=records,
            )
            self._verify_cleanup_journal_authority(authority)
            self._verify_cleanup_journal_lock(authority, lock_fd)
            self._cleanup_retries.update(recovered)
        finally:
            if lock_fd >= 0:
                self._release_cleanup_journal_lock(lock_fd)
            authority.close()

    @staticmethod
    def _cleanup_journal_record_from_payload(
        payload: object,
        path: Path,
    ) -> CleanupRetryOwnership | _CleanupJournalTombstone:
        if not isinstance(payload, dict):
            raise ValueError("cleanup ownership payload is not closed")
        version = payload.get("version")
        if version not in {8, 9} or payload.get("kind") != "retired":
            return GatewayNodeManager._cleanup_ownership_from_payload(payload, path)
        expected_keys = {
            "version",
            "kind",
            "session_id",
            "generation",
            "revision",
            "terminal_delivery",
        }
        if version == 9:
            expected_keys.update({"epoch", "epoch_token", "retired_epoch", "retired_epoch_token"})
        if set(payload) != expected_keys:
            raise ValueError("cleanup tombstone payload is not closed")
        session_id = payload["session_id"]
        generation = payload["generation"]
        revision = payload["revision"]
        epoch = payload.get("epoch")
        epoch_token = payload.get("epoch_token")
        retired_epoch = payload.get("retired_epoch")
        retired_epoch_token = payload.get("retired_epoch_token")
        if (
            not isinstance(session_id, str)
            or not isinstance(generation, str)
            or _CLEANUP_JOURNAL_GENERATION_RE.fullmatch(generation) is None
            or isinstance(revision, bool)
            or not isinstance(revision, int)
            or revision < 1
            or (
                version == 9
                and (
                    isinstance(epoch, bool)
                    or not isinstance(epoch, int)
                    or epoch < 0
                    or not isinstance(epoch_token, str)
                    or _CLEANUP_JOURNAL_GENERATION_RE.fullmatch(epoch_token) is None
                    or isinstance(retired_epoch, bool)
                    or not isinstance(retired_epoch, int)
                    or retired_epoch < epoch
                    or not isinstance(retired_epoch_token, str)
                    or _CLEANUP_JOURNAL_GENERATION_RE.fullmatch(retired_epoch_token) is None
                )
            )
        ):
            raise ValueError("cleanup tombstone identity is invalid")
        expected_name = f"{hashlib.sha256(session_id.encode('utf-8')).hexdigest()}.json"
        if path.name != expected_name:
            raise ValueError("cleanup tombstone filename does not match session identity")
        delivery = payload["terminal_delivery"]
        if delivery is None:
            result_digest = None
            export_succeeded = None
            callback_succeeded = None
        else:
            if (
                not isinstance(delivery, dict)
                or set(delivery) != {"result_digest", "export_succeeded", "callback_succeeded"}
                or not isinstance(delivery["result_digest"], str)
                or re.fullmatch(r"[0-9a-f]{64}", delivery["result_digest"]) is None
                or delivery["export_succeeded"] is not True
                or delivery["callback_succeeded"] is not True
            ):
                raise ValueError("cleanup tombstone terminal proof is invalid")
            result_digest = delivery["result_digest"]
            export_succeeded = True
            callback_succeeded = True
        return _CleanupJournalTombstone(
            session_id=session_id,
            generation=generation,
            revision=revision,
            epoch=epoch,
            epoch_token=epoch_token,
            retired_epoch=retired_epoch,
            retired_epoch_token=retired_epoch_token,
            result_digest=result_digest,
            export_succeeded=export_succeeded,
            callback_succeeded=callback_succeeded,
        )

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
        if version in {2, 3, 4, 5, 6, 7}:
            expected_keys.add("log_root")
        if version in {3, 4, 5, 6, 7}:
            expected_keys.add("subscription_finalization")
        if version in {4, 5, 6, 7}:
            expected_keys.add("terminal_delivery")
        if version in {5, 6, 7}:
            expected_keys.add("phase")
        if version == 7:
            expected_keys.add("revision")
        if version in {8, 9}:
            expected_keys.update({"kind", "generation", "revision", "phase"})
            expected_keys.update({"log_root", "subscription_finalization", "terminal_delivery"})
        if version == 9:
            expected_keys.update({"epoch", "epoch_token"})
        if set(payload) != expected_keys:
            raise ValueError("cleanup ownership payload is not closed")
        if version not in {1, 2, 3, 4, 5, 6, 7, 8, 9} or not isinstance(
            payload["session_id"], str
        ):
            raise ValueError("cleanup ownership identity is invalid")
        generation = payload.get("generation")
        if version in {8, 9} and (
            payload["kind"] != "active"
            or not isinstance(generation, str)
            or _CLEANUP_JOURNAL_GENERATION_RE.fullmatch(generation) is None
        ):
            raise ValueError("cleanup ownership generation is invalid")
        epoch = payload.get("epoch")
        epoch_token = payload.get("epoch_token")
        if version == 9 and (
            isinstance(epoch, bool)
            or not isinstance(epoch, int)
            or epoch < 0
            or not isinstance(epoch_token, str)
            or _CLEANUP_JOURNAL_GENERATION_RE.fullmatch(epoch_token) is None
        ):
            raise ValueError("cleanup ownership epoch is invalid")
        revision = payload.get("revision", 0)
        if version in {7, 8, 9} and (
            isinstance(revision, bool) or not isinstance(revision, int) or revision < 1
        ):
            raise ValueError("cleanup ownership revision is invalid")
        phase = payload.get("phase")
        if version in {5, 6, 7, 8, 9} and phase not in _RECOVERY_PHASES:
            raise ValueError("cleanup recovery phase is invalid")
        if version not in {5, 6, 7, 8, 9}:
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
            if version in {6, 7, 8, 9}:
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
            if version in {5, 6, 7, 8, 9}:
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
            if version in {5, 6, 7, 8, 9} and export_required != (export_authority is not None):
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
            revision=revision,
            generation=generation,
            epoch=epoch,
            epoch_token=epoch_token,
            eval_runtime=None,
            finalize_subscription=finalization_state is not None,
            finalization_state=finalization_state,
            delivery_state=delivery_state,
        )

    def _retire_cleanup_ownership(
        self,
        ownership: CleanupRetryOwnership,
    ) -> _CleanupJournalTombstone:
        if getattr(self, "_cleanup_journal_dir", None) is None:
            delivery = ownership.delivery_state
            return _CleanupJournalTombstone(
                session_id=ownership.session_id,
                generation=ownership.generation or secrets.token_hex(16),
                revision=ownership.revision + 1,
                epoch=ownership.epoch,
                epoch_token=ownership.epoch_token,
                retired_epoch=ownership.epoch,
                retired_epoch_token=ownership.epoch_token,
                result_digest=None if delivery is None else delivery.result_digest,
                export_succeeded=None if delivery is None else delivery.export_succeeded,
                callback_succeeded=None if delivery is None else delivery.callback_succeeded,
            )
        authority = self._open_cleanup_journal_authority(initialize=True)
        if authority is None:
            raise RuntimeError("cleanup journal root identity authority is unavailable")
        lock_fd = -1
        destination = self._cleanup_journal_name(ownership.session_id)
        try:
            lock_fd = self._acquire_cleanup_journal_lock(authority)
            current_epoch = self._ensure_cleanup_journal_epoch(authority)
            previous = self._read_private_cleanup_file(
                destination,
                root_fd=authority.root_fd,
                max_bytes=_CLEANUP_JOURNAL_MAX_BYTES,
                allow_missing=True,
            )
            authoritative: CleanupRetryOwnership | None = None
            if previous is not None:
                try:
                    payload = json.loads(previous.decode("utf-8"))
                    record = self._cleanup_journal_record_from_payload(
                        payload,
                        Path(destination),
                    )
                except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
                    raise RuntimeError("cleanup ownership journal is invalid") from exc
                if isinstance(record, _CleanupJournalTombstone):
                    expected_generation = ownership.generation
                    expected_revision = ownership.revision + 1
                    if (
                        expected_generation is not None
                        and record.generation == expected_generation
                        and record.revision == expected_revision
                        and (
                            ownership.epoch is None
                            or (
                                record.epoch == ownership.epoch
                                and record.epoch_token == ownership.epoch_token
                            )
                        )
                    ):
                        self._verify_cleanup_journal_authority(authority)
                        self._verify_cleanup_journal_lock(authority, lock_fd)
                        return record
                    raise RuntimeError("cleanup ownership generation is already retired")
                authoritative = record

            if authoritative is None:
                if (
                    ownership.revision != 0
                    or ownership.generation is not None
                    or ownership.epoch != current_epoch.epoch
                    or ownership.epoch_token != current_epoch.token
                ):
                    raise RuntimeError("cleanup journal retirement compare-and-swap failed")
                inventory = self._inventory_cleanup_journal_locked(authority)
                if len(inventory) >= _CLEANUP_JOURNAL_COMPACT_AT_ROWS:
                    records = self._parse_cleanup_journal_inventory_locked(
                        authority,
                        inventory,
                    )
                    current_epoch = self._compact_cleanup_journal_locked(
                        authority=authority,
                        lock_fd=lock_fd,
                        epoch=current_epoch,
                        records=records,
                    )
                    inventory = [
                        (name, expected)
                        for name, expected, record in records
                        if isinstance(record, CleanupRetryOwnership)
                    ]
                    ownership = replace(
                        ownership,
                        epoch=current_epoch.epoch,
                        epoch_token=current_epoch.token,
                    )
                if len(inventory) >= _CLEANUP_JOURNAL_MAX_ROWS:
                    raise RuntimeError("cleanup journal capacity is occupied by active records")
                authoritative = ownership
            elif (
                authoritative.session_id != ownership.session_id
                or authoritative.revision != ownership.revision
                or authoritative.generation != ownership.generation
                or (
                    authoritative.epoch is not None
                    and (
                        authoritative.epoch != ownership.epoch
                        or authoritative.epoch_token != ownership.epoch_token
                    )
                )
            ):
                raise RuntimeError("cleanup journal retirement compare-and-swap failed")

            if authoritative.epoch is None:
                authoritative = replace(
                    authoritative,
                    epoch=current_epoch.epoch,
                    epoch_token=current_epoch.token,
                )

            generation = authoritative.generation or secrets.token_hex(16)
            tombstone_payload = self._cleanup_tombstone_payload(
                authoritative,
                generation=generation,
                retirement_epoch=current_epoch,
            )
            try:
                tombstone = self._cleanup_journal_record_from_payload(
                    tombstone_payload,
                    Path(destination),
                )
            except ValueError as exc:
                raise RuntimeError("cleanup ownership tombstone is invalid") from exc
            if not isinstance(tombstone, _CleanupJournalTombstone):
                raise RuntimeError("cleanup ownership tombstone is invalid")
            encoded = json.dumps(
                tombstone_payload,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            if len(encoded) > _CLEANUP_JOURNAL_MAX_BYTES:
                raise RuntimeError("cleanup ownership tombstone exceeds the byte limit")
            self._commit_cleanup_journal_record(
                authority=authority,
                lock_fd=lock_fd,
                destination=destination,
                previous=previous,
                encoded=encoded,
                session_id=ownership.session_id,
                operation_id=id(ownership),
            )
            if ownership.managed is not None:
                ownership.managed.cleanup_journal_revision = tombstone.revision
                ownership.managed.cleanup_journal_generation = tombstone.generation
                ownership.managed.cleanup_journal_epoch = tombstone.epoch
                ownership.managed.cleanup_journal_epoch_token = tombstone.epoch_token
            return tombstone
        finally:
            if lock_fd >= 0:
                self._release_cleanup_journal_lock(lock_fd)
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
            cleanup_journal_revision=ownership.revision,
            cleanup_journal_generation=ownership.generation,
            cleanup_journal_epoch=ownership.epoch,
            cleanup_journal_epoch_token=ownership.epoch_token,
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
            ownership = self._record_cleanup_ownership_runtimes_absent(ownership)
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
                current = retries[session_id]
                self._retire_cleanup_ownership(current)
                retries.pop(session_id, None)
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
            current = retries[session_id]
            self._retire_cleanup_ownership(current)
            retries.pop(session_id, None)

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
