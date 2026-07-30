#!/usr/bin/env python3
"""Run the exact release Desktop v2 composition against a real science host.

This is maintainer automation, not a user-facing CLI. It starts the sidecar and
native askpass helper from the candidate macOS app bundle, selects one literal
system-OpenSSH alias, and writes bounded candidate-bound evidence. OpenSSH is
the final authority for routing, identity, authentication, and trust.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from email.parser import Parser
import errno
import fcntl
import hashlib
import hmac
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import secrets
import select
import signal
import socket
import stat
import subprocess
import sys
from tempfile import TemporaryDirectory, TemporaryFile
import threading
import time
from types import ModuleType
from typing import Any, BinaryIO, Callable, Iterator, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import OpenerDirector, ProxyHandler, Request, build_opener
from zipfile import BadZipFile, ZipFile


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
NATIVE_PROTOCOL = "openevo-native-sidecar-v2"
NATIVE_HEALTH_ROUTE = "/openevo-native/health"
DESKTOP_SESSION_HEADER = "X-OpenEvo-Desktop-Session"
RESOURCE_GENERATION_HEADER = "X-OpenEvo-Resource-Generation"
NATIVE_CHALLENGE_HEADER = "X-OpenEvo-Native-Challenge"
LISTENER_FD = 3
EXECUTABLE_FD = 4
GUARD_FD_MINIMUM = 64
MAX_NATIVE_FRAME_BYTES = 512
MAX_HTTP_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_EVIDENCE_BYTES = 128 * 1024
MAX_RENDERER_HANDOFF_BYTES = 64 * 1024
MAX_RENDERER_RESULT_BYTES = 64 * 1024
MAX_RENDERER_SCREENSHOT_BYTES = 16 * 1024 * 1024
MAX_RENDERER_PROCESS_LOG_BYTES = 8 * 1024 * 1024
MAX_BUILD_PROCESS_LOG_BYTES = 8 * 1024 * 1024
MAX_SIDECAR_PROCESS_LOG_BYTES = 16 * 1024 * 1024
MAX_RENDERER_CANDIDATE_JSON_BYTES = 2 * 1024 * 1024
MAX_EVIDENCE_ITEMS = 64
REQUIRED_TARGET_IDS = ("agent_system", "skill_bundle", "text_memory")
RELEASE_PROJECT_DISPLAY_NAME = "OpenEvo real science E2E"
RELEASE_CODEX_MODEL = "gpt-5.3-codex-spark"
RELEASE_REASONING_EFFORT = "high"
MAX_POLL_SECONDS = 30.0
MAX_PROGRESS_SECONDS = 60.0
MAX_ACTIVATION_TIMEOUT_SECONDS = 1800.0
MAX_RUN_TIMEOUT_SECONDS = 10800.0
MAX_BUILD_TIMEOUT_SECONDS = 2400.0
MIN_RENDERER_TIMEOUT_SECONDS = 30.0
MAX_RENDERER_TIMEOUT_SECONDS = 600.0
RENDERER_PROCESS_EXIT_GRACE_SECONDS = 15.0
MAX_INTER_SESSION_DELAY_SECONDS = 300.0
MAX_OVERALL_TIMEOUT_SECONDS = 21600.0
MAX_SSE_EVENT_BYTES = 64 * 1024
RELEASE_CANDIDATE_SCHEMA_VERSION = 10
MAX_LIFECYCLE_RESERVATION_MILLISECONDS = 15_000
MIN_LIFECYCLE_TERMINAL_MILLISECONDS = 15_000
LIFECYCLE_PHASES = (
    "validation",
    "queued",
    "resolving_system_openssh",
    "connecting",
    "waiting_for_user",
    "remote_preflight",
    "transferring",
    "verifying",
    "starting_daemon",
    "waiting_for_daemon",
    "opening_project_tunnel",
    "negotiating_core",
    "preparing_native_workspace",
    "creating_remote_project",
    "verifying_project",
    "activating",
    "finalizing",
)
LIFECYCLE_PROCESS_LOG_SOURCES = frozenset(
    {"daemon_stderr", "daemon_stdout", "ssh_stderr", "ssh_stdout"}
)
BUILD_PROXY_ENVIRONMENT_NAMES = frozenset(
    {
        "ALL_PROXY",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "all_proxy",
        "http_proxy",
        "https_proxy",
        "no_proxy",
    }
)
CONTEXT_CANARY_INSTRUCTION = (
    "\n\nOpenEvo E2E context canary v2: when OPENEVO_MEMORY_FILE, "
    "OPENEVO_SKILLS_DIR, and OPENEVO_AGENT_SYSTEM_FILE are set, read the memory file, "
    "every SKILL.md below the skill directory, and the agent-system file before "
    "completing the task. Do not print environment values, filesystem locations, "
    "credentials, or file contents."
)
TERMINAL_TASK_STATES = frozenset({"completed", "failed", "cancelled", "closed"})
REQUIRED_TASK_EVENT_TYPES = frozenset(
    {
        "task_admitted",
        "attempt_appended",
        "dataset_sealed",
        "evolution_revision_committed",
        "runtime_context_committed",
        "project_head_activated",
    }
)
FRAMEWORK_LOCK_KEYS = frozenset(
    {
        "schema_version",
        "distribution",
        "distribution_version",
        "distribution_digest",
        "wheel_filename",
    }
)
FORBIDDEN_EVIDENCE_KEYS = frozenset(
    {
        "bearer",
        "codex_auth",
        "core_bearer",
        "credential",
        "handoff_token",
        "host_path",
        "path",
        "readiness_key",
        "raw_secret",
        "session_token",
        "ssh_auth_sock",
        "mutation_token",
        "password",
        "passphrase",
        "private_key",
    }
)
EVIDENCE_ALLOWED_KEYS = frozenset(
    {
        "schema_version",
        "kind",
        "issue",
        "real_process_boundary",
        "run_mode",
        "outcome",
        "started_at",
        "finished_at",
        "release_assets",
        "external_release_assets",
        "sidecar",
        "ssh_askpass_helper",
        "core_wheel",
        "framework_lock",
        "managed_runtime_archive",
        "daemon_bundle",
        "daemon_manifest",
        "sha256",
        "byte_size",
        "filename",
        "distribution",
        "version",
        "distribution_digest",
        "registry_digest",
        "exact_external_release_assets_verified",
        "slim_sidecar_excludes_remote_release_assets_verified",
        "desktop",
        "source_commit",
        "release_version",
        "mutation_major",
        "openapi_sha256",
        "event_schema_sha256",
        "build_id",
        "provider_kind",
        "build_channel",
        "feature_flags",
        "feature_set_sha256",
        "required_core_api_major",
        "mutation_compatible",
        "v2_only_negotiation_verified",
        "authenticated_session_probe",
        "unauthenticated_session_rejected",
        "remote",
        "connection_authority",
        "catalog_selection_verified",
        "system_openssh_final_authority_verified",
        "core_api_major",
        "core_registry_sha256",
        "project",
        "project_id_sha256",
        "execution",
        "mode",
        "capture_mode",
        "token_level_metrics_available",
        "harness_id",
        "codex_model",
        "reasoning_effort",
        "task_network_allow_internet",
        "target_ids",
        "selected_methods",
        "registry_sha256",
        "validation_check_counts",
        "initial_project_head",
        "active_project_head",
        "verification_scope",
        "task_count",
        "renderer",
        "renderer_observability_verified",
        "renderer_boundary",
        "candidate_tauri_launch_verified",
        "renderer_candidate_binding",
        "source_checkout_verified",
        "candidate_version",
        "release_candidate_manifest_sha256",
        "desktop_dmg_sha256",
        "packaged_web_manifest_sha256",
        "playwright_candidate_evidence_sha256",
        "app_bundle_smoke_sha256",
        "candidate_packaged_sidecar_sha256",
        "candidate_ssh_askpass_helper_sha256",
        "candidate_native_sidecar_smoke_verified",
        "exact_candidate_packaged_sidecar_verified",
        "exact_candidate_ssh_askpass_helper_verified",
        "renderer_ready",
        "packaged_web_build_digest",
        "builtin_sample_count",
        "desktop_api_major",
        "active_project_head_generation",
        "evolution_artifact_count",
        "task_id_sha256",
        "system_openssh_workspace_verified",
        "remote_target_controls_verified",
        "observed_route_kinds",
        "screenshot_sha256",
        "tasks",
        "ordinal",
        "state",
        "task_admission_id_sha256",
        "admission_sha256",
        "authoritative_attempt_id_sha256",
        "attempt_count",
        "predecessor_project_head",
        "context_project_head",
        "successor_project_head",
        "transition_id_sha256",
        "transition_state",
        "project_head_id_sha256",
        "predecessor_project_head_id_sha256",
        "generation",
        "manifest_sha256",
        "workspace_snapshot",
        "workspace_snapshot_id_sha256",
        "entry_count",
        "evolution_revision",
        "evolution_revision_id_sha256",
        "runtime_context_snapshot",
        "runtime_context_snapshot_id_sha256",
        "runtime_contract_sha256",
        "effective_execution_snapshot",
        "effective_execution_snapshot_id_sha256",
        "producer_id_sha256",
        "snapshot_sha256",
        "timeline_event_types",
        "timeline_event_count",
        "log_count",
        "log_message_sha256",
        "artifacts",
        "artifact_id_sha256",
        "artifact_type",
        "artifact_count",
        "media_type",
        "content_sha256",
        "reuse",
        "first_context_excluded_own_successor",
        "second_admission_pinned_first_successor",
        "second_context_pinned_first_successor",
        "second_runtime_context_equals_first_successor",
        "lifecycle",
        "operation_kind",
        "reservation_status",
        "reservation_latency_ms",
        "terminal_duration_ms",
        "action_id_sha256",
        "operation_id_sha256",
        "request_sha256",
        "ordered_phases",
        "process_logs",
        "sources",
        "content_sha256",
        "sse_reconnect_verified",
        "relaunch_recovery_verified",
        "stable_action_id_after_relaunch",
        "stable_operation_id_after_relaunch",
        "mutation_reissued_after_relaunch",
        "core_authority",
        "project_count",
        "project_mapping_count",
        "applied_create_project_mutation_count",
        "secret_canary_sha256",
        "secret_canary_absent",
        "cleanup",
        "active_task_cleanup_required",
        "active_task_cancel_requested",
        "active_task_terminal",
        "active_task_cleanup_succeeded",
        "desktop_disconnect_succeeded",
        "sidecar_shutdown_succeeded",
        "core_ownership_release_requested",
        "failure",
        "stage",
        "code",
        "http_status",
        "agent_system",
        "skill_bundle",
        "text_memory",
    }
)
ABSOLUTE_WINDOWS_PATH = re.compile(r"^[A-Za-z]:[\\/]")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
SSH_HOST_ALIAS_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class E2EFailure(RuntimeError):
    """A closed, evidence-safe failure."""

    def __init__(
        self,
        stage: str,
        code: str,
        *,
        http_status: int | None = None,
    ) -> None:
        super().__init__(f"{stage}: {code}")
        self.stage = stage
        self.code = code
        self.http_status = http_status


class ProgressReporter:
    """Emit fixed, credential-free progress and enforce one overall deadline."""

    def __init__(
        self,
        *,
        interval_seconds: float,
        overall_timeout_seconds: float,
    ) -> None:
        self._interval = interval_seconds
        self._started = time.monotonic()
        self._deadline = self._started + overall_timeout_seconds
        self._last_emit = float("-inf")
        self._last_observation: tuple[str, str] | None = None
        self._enforce_deadline = True

    def remaining(self, stage: str) -> float:
        if not self._enforce_deadline:
            return float("inf")
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise E2EFailure(stage, "overall_timeout")
        return remaining

    def phase_deadline(self, stage: str, timeout_seconds: float) -> float:
        return time.monotonic() + min(timeout_seconds, self.remaining(stage))

    def stop_deadline_enforcement(self) -> None:
        self._enforce_deadline = False

    def heartbeat(self, stage: str, state: object) -> None:
        if time.monotonic() - self._last_emit >= self._interval:
            self.emit(stage, state, force=True)

    def emit(self, stage: str, state: object, *, force: bool = False) -> None:
        now = time.monotonic()
        safe_stage = _safe_code(stage)
        safe_state = _safe_code(state)
        observation = (safe_stage, safe_state)
        if (
            force
            or observation != self._last_observation
            or now - self._last_emit >= self._interval
        ):
            remaining = max(0, math.ceil(self._deadline - now))
            elapsed = max(0, math.floor(now - self._started))
            print(
                "Desktop real-science E2E progress "
                f"stage={safe_stage} state={safe_state} "
                f"elapsed_seconds={elapsed} remaining_seconds={remaining}",
                file=sys.stderr,
                flush=True,
            )
            self._last_emit = now
            self._last_observation = observation


@dataclass(frozen=True)
class HeldReleaseAsset:
    path: Path
    descriptor: int = field(repr=False, compare=False)
    identity: tuple[int, ...] = field(repr=False)
    sha256: str
    byte_size: int

    @classmethod
    def open(cls, path: Path) -> HeldReleaseAsset:
        descriptor = -1
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
            opened = os.fstat(descriptor)
            named = path.lstat()
            identity = _release_asset_identity(opened)
            if (
                _release_asset_identity(named) != identity
                or not stat.S_ISREG(opened.st_mode)
                or opened.st_uid != os.getuid()
                or opened.st_nlink != 1
                or opened.st_size <= 0
            ):
                raise OSError("release asset identity is invalid")
            digest = _sha256_descriptor(descriptor, opened.st_size)
            return cls(
                path=path,
                descriptor=descriptor,
                identity=identity,
                sha256=digest,
                byte_size=opened.st_size,
            )
        except OSError as exc:
            if descriptor >= 0:
                os.close(descriptor)
            raise E2EFailure("release_assets", "release_asset_authority_invalid") from exc

    def verify_unchanged(self) -> None:
        try:
            opened = os.fstat(self.descriptor)
            named = self.path.lstat()
            if (
                _release_asset_identity(opened) != self.identity
                or _release_asset_identity(named) != self.identity
                or _sha256_descriptor(self.descriptor, self.byte_size) != self.sha256
            ):
                raise OSError("release asset changed")
        except OSError as exc:
            raise E2EFailure("release_assets", "release_asset_authority_changed") from exc

    def evidence(self) -> dict[str, object]:
        self.verify_unchanged()
        return {"sha256": self.sha256, "byte_size": self.byte_size}

    def copy_to(
        self,
        destination: Path,
        *,
        executable: bool,
        mode: int | None = None,
        failure_stage: str = "native_launch",
        failure_code: str = "sidecar_snapshot_failed",
    ) -> None:
        self.verify_unchanged()
        target_fd = -1
        target_mode = mode if mode is not None else (0o500 if executable else 0o400)
        if target_mode not in {0o400, 0o500, 0o600, 0o700, 0o755}:
            raise E2EFailure(failure_stage, failure_code)
        try:
            target_fd = os.open(
                destination,
                os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                target_mode,
            )
            offset = 0
            while offset < self.byte_size:
                chunk = os.pread(
                    self.descriptor,
                    min(1024 * 1024, self.byte_size - offset),
                    offset,
                )
                if not chunk:
                    raise OSError("release asset ended during copy")
                written = 0
                while written < len(chunk):
                    count = os.write(target_fd, chunk[written:])
                    if count <= 0:
                        raise OSError("release asset copy stopped")
                    written += count
                offset += len(chunk)
            if os.pread(self.descriptor, 1, self.byte_size):
                raise OSError("release asset grew during copy")
            os.fchmod(target_fd, target_mode)
            os.fsync(target_fd)
            copied = os.fstat(target_fd)
            if (
                not stat.S_ISREG(copied.st_mode)
                or copied.st_uid != os.getuid()
                or copied.st_nlink != 1
                or copied.st_size != self.byte_size
                or _sha256_descriptor(target_fd, self.byte_size) != self.sha256
            ):
                raise OSError("release asset copy identity is invalid")
            self.verify_unchanged()
        except OSError as exc:
            try:
                destination.unlink()
            except OSError:
                pass
            raise E2EFailure(failure_stage, failure_code) from exc
        finally:
            if target_fd >= 0:
                os.close(target_fd)

    def close(self) -> None:
        os.close(self.descriptor)


@dataclass(frozen=True)
class HeldReleaseAssetsRoot:
    """FD-bound authority for the staged external release-asset tree."""

    path: Path
    descriptor: int = field(repr=False, compare=False)
    identity: tuple[int, ...] = field(repr=False)

    @classmethod
    def open(cls, path: Path) -> HeldReleaseAssetsRoot:
        descriptor = -1
        try:
            descriptor = os.open(
                path,
                os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
            )
            opened = os.fstat(descriptor)
            named = path.lstat()
            identity = _release_asset_identity(opened)
            if (
                _release_asset_identity(named) != identity
                or not stat.S_ISDIR(opened.st_mode)
                or opened.st_uid != os.getuid()
                or stat.S_IMODE(opened.st_mode) != 0o700
            ):
                raise OSError("release asset root identity is invalid")
            return cls(path=path, descriptor=descriptor, identity=identity)
        except OSError as exc:
            if descriptor >= 0:
                os.close(descriptor)
            raise E2EFailure("release_assets", "release_asset_root_authority_invalid") from exc

    def verify_unchanged(self) -> None:
        try:
            opened = os.fstat(self.descriptor)
            named = self.path.lstat()
            if (
                _release_asset_identity(opened) != self.identity
                or _release_asset_identity(named) != self.identity
            ):
                raise OSError("release asset root changed")
        except OSError as exc:
            raise E2EFailure("release_assets", "release_asset_root_authority_changed") from exc

    def close(self) -> None:
        os.close(self.descriptor)


@dataclass(frozen=True)
class ReleaseAssets:
    sidecar: Path
    ssh_askpass_helper: Path
    wheel: Path
    framework_lock: Path
    managed_runtime_archive: Path
    daemon_bundle: Path
    daemon_manifest: Path
    evidence: dict[str, object]
    authorities: tuple[HeldReleaseAsset, ...] = field(
        default=(),
        repr=False,
        compare=False,
    )
    release_assets_root: Path | None = None
    release_assets_manifest: Path | None = None
    source_commit: str | None = None
    registry_digest: str | None = None
    root_authority: HeldReleaseAssetsRoot | None = field(
        default=None,
        repr=False,
        compare=False,
    )

    def authority(self, path: Path) -> HeldReleaseAsset:
        matches = [authority for authority in self.authorities if authority.path == path]
        if len(matches) != 1:
            raise E2EFailure("release_assets", "release_asset_authority_missing")
        return matches[0]

    def external_release_assets_root(self) -> Path:
        if self.release_assets_root is None or self.root_authority is None:
            raise E2EFailure("release_assets", "release_asset_root_authority_missing")
        self.root_authority.verify_unchanged()
        return self.release_assets_root

    def verify_unchanged(self) -> None:
        self.external_release_assets_root()
        for authority in self.authorities:
            authority.verify_unchanged()

    def close(self) -> None:
        for authority in self.authorities:
            authority.close()
        if self.root_authority is not None:
            self.root_authority.close()


@dataclass(frozen=True)
class RendererCandidateBinding:
    packaged_web_root: Path
    source_commit: str
    version: str
    build_digest: str
    evidence: dict[str, object]
    authorities: tuple[HeldReleaseAsset, ...] = field(repr=False, compare=False)

    def verify_unchanged(self) -> None:
        for authority in self.authorities:
            authority.verify_unchanged()

    def close(self) -> None:
        for authority in self.authorities:
            authority.close()


@dataclass(frozen=True)
class NativeCredentials:
    instance_id: str
    readiness_key: bytes = field(repr=False)
    session_token: str = field(repr=False)
    handoff_token: str = field(repr=False)

    @classmethod
    def create(cls) -> NativeCredentials:
        return cls(
            instance_id=secrets.token_hex(16),
            readiness_key=secrets.token_bytes(32),
            session_token=secrets.token_hex(32),
            handoff_token=secrets.token_hex(32),
        )

    def frame(self) -> bytes:
        payload = {
            "protocol": NATIVE_PROTOCOL,
            "instance_id": self.instance_id,
            "readiness_key": self.readiness_key.hex(),
            "session_token": self.session_token,
            "handoff_token": self.handoff_token,
        }
        encoded = _canonical_json(payload)
        if len(encoded) > MAX_NATIVE_FRAME_BYTES:
            raise E2EFailure("native_launch", "credential_frame_too_large")
        return encoded

    def private_values(self) -> tuple[str, ...]:
        return (
            self.readiness_key.hex(),
            self.session_token,
            self.handoff_token,
        )


@dataclass
class NativeSidecar:
    process: subprocess.Popen[bytes]
    process_group_id: int
    base_url: str
    credentials: NativeCredentials = field(repr=False)
    process_log: BinaryIO = field(repr=False)
    forbidden_log_values: tuple[bytes, ...] = field(default=(), repr=False)

    def assert_log_budget(self) -> None:
        try:
            self.process_log.flush()
            size = os.fstat(self.process_log.fileno()).st_size
        except OSError as exc:
            raise E2EFailure("native_launch", "sidecar_process_log_unavailable") from exc
        if size > MAX_SIDECAR_PROCESS_LOG_BYTES:
            raise E2EFailure("native_launch", "sidecar_process_log_budget_exceeded")
        try:
            content = os.pread(self.process_log.fileno(), size, 0)
        except OSError as exc:
            raise E2EFailure("native_launch", "sidecar_process_log_unavailable") from exc
        if len(content) != size:
            raise E2EFailure("native_launch", "sidecar_process_log_changed")
        if any(value and value in content for value in self.forbidden_log_values):
            raise E2EFailure("native_launch", "secret_canary_in_sidecar_log")

    def terminate(self) -> bool:
        try:
            return _terminate_process_group(
                self.process,
                process_group_id=self.process_group_id,
                graceful_timeout_seconds=30,
            )
        finally:
            self.process_log.close()


@dataclass(frozen=True)
class TaskObservation:
    evidence: dict[str, object]
    task: dict[str, Any]
    context: dict[str, Any]
    successor_project_head: dict[str, Any]
    artifacts: tuple[dict[str, Any], ...]


class SseEventProbe:
    """Read one bounded Desktop SSE event on a disposable connection."""

    def __init__(
        self,
        *,
        opener: OpenerDirector,
        request: Request,
        timeout_seconds: float,
    ) -> None:
        self._opener = opener
        self._request = request
        self._timeout_seconds = timeout_seconds
        self._ready = threading.Event()
        self._done = threading.Event()
        self._result: dict[str, object] | None = None
        self._error: BaseException | None = None
        self._thread = threading.Thread(
            target=self._run,
            name="openevo-e2e-sse-probe",
            daemon=True,
        )
        self._thread.start()
        deadline = time.monotonic() + min(timeout_seconds, 15.0)
        while not self._ready.is_set() and not self._done.is_set():
            if time.monotonic() >= deadline:
                raise E2EFailure("lifecycle_sse", "event_stream_connection_timeout")
            self._done.wait(0.01)
        if self._error is not None:
            raise E2EFailure("lifecycle_sse", "event_stream_connection_failed") from self._error

    def wait(self, *, timeout_seconds: float) -> dict[str, object]:
        if not self._done.wait(timeout_seconds):
            raise E2EFailure("lifecycle_sse", "event_stream_observation_timeout")
        self._thread.join(timeout=1.0)
        if self._error is not None:
            raise E2EFailure("lifecycle_sse", "event_stream_observation_failed") from self._error
        if self._result is None:
            raise E2EFailure("lifecycle_sse", "event_stream_observation_missing")
        return self._result

    def _run(self) -> None:
        try:
            with self._opener.open(
                self._request,
                timeout=self._timeout_seconds,
            ) as response:
                if response.status != 200 or not str(
                    response.headers.get("Content-Type", "")
                ).lower().startswith("text/event-stream"):
                    raise ValueError("Desktop SSE response is invalid")
                self._ready.set()
                fields: dict[str, str] = {}
                consumed = 0
                while consumed <= MAX_SSE_EVENT_BYTES:
                    line = response.readline(MAX_SSE_EVENT_BYTES - consumed + 1)
                    if not line:
                        raise ValueError("Desktop SSE stream ended before one event")
                    consumed += len(line)
                    if consumed > MAX_SSE_EVENT_BYTES:
                        raise ValueError("Desktop SSE event exceeds its byte bound")
                    if line in {b"\n", b"\r\n"}:
                        if "id" not in fields or "data" not in fields:
                            fields.clear()
                            continue
                        envelope = json.loads(fields["data"])
                        if not isinstance(envelope, dict):
                            raise ValueError("Desktop SSE event payload is not an object")
                        self._result = {
                            "event_id": fields["id"],
                            "envelope": envelope,
                        }
                        return
                    decoded = line.decode("utf-8", errors="strict").rstrip("\r\n")
                    if not decoded or decoded.startswith(":"):
                        continue
                    name, separator, value = decoded.partition(":")
                    if not separator or name not in {"id", "event", "data"}:
                        raise ValueError("Desktop SSE event contains an invalid field")
                    fields[name] = value.removeprefix(" ")
        except BaseException as exc:
            self._error = exc
        finally:
            self._ready.set()
            self._done.set()


class LocalApi:
    def __init__(
        self,
        base_url: str,
        session_token: str,
        *,
        progress: ProgressReporter | None = None,
        health_check: Callable[[], None] | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._session_token = session_token
        self._opener: OpenerDirector = build_opener(ProxyHandler({}))
        self._progress = progress
        self._health_check = health_check

    def request(
        self,
        method: str,
        route: str,
        *,
        stage: str,
        body: Mapping[str, object] | None = None,
        headers: Mapping[str, str] | None = None,
        expected_status: int | Sequence[int] = 200,
        authenticated: bool = True,
        expected_empty_body: bool = False,
        timeout_seconds: float = 120.0,
    ) -> dict[str, Any] | None:
        if self._health_check is not None:
            self._health_check()
        if self._progress is not None:
            self._progress.remaining(stage)
            self._progress.heartbeat(stage, "requesting")
        request_headers = dict(headers or {})
        if authenticated:
            request_headers[DESKTOP_SESSION_HEADER] = self._session_token
        encoded: bytes | None = None
        if body is not None:
            encoded = _canonical_json(dict(body))
            request_headers["Content-Type"] = "application/json"
        request = Request(
            f"{self._base_url}{route}",
            data=encoded,
            headers=request_headers,
            method=method,
        )
        statuses = (
            frozenset({expected_status})
            if isinstance(expected_status, int)
            else frozenset(expected_status)
        )
        try:
            with self._opener.open(request, timeout=timeout_seconds) as response:
                status = response.status
                payload = _read_bounded(response)
        except HTTPError as exc:
            status = exc.code
            payload = _read_bounded(exc)
        except (OSError, URLError) as exc:
            raise E2EFailure(stage, "desktop_local_api_unreachable") from exc
        if self._health_check is not None:
            self._health_check()
        if self._progress is not None:
            self._progress.remaining(stage)
        if status not in statuses:
            remote_code = _remote_error_code(payload)
            raise E2EFailure(stage, remote_code, http_status=status)
        if expected_empty_body:
            if payload:
                raise E2EFailure(stage, "unexpected_empty_response_payload")
            return None
        if status == 204:
            if payload:
                raise E2EFailure(stage, "unexpected_no_content_payload")
            return None
        try:
            document = json.loads(payload.decode("utf-8", errors="strict"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise E2EFailure(stage, "invalid_json_response") from exc
        if not isinstance(document, dict):
            raise E2EFailure(stage, "non_object_json_response")
        return document

    def page(self, route: str, *, stage: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        after: str | None = None
        for _ in range(16):
            query = {"limit": "100"}
            if after is not None:
                query["after"] = after
            separator = "&" if "?" in route else "?"
            page = self.request(
                "GET",
                f"{route}{separator}{urlencode(query)}",
                stage=stage,
            )
            assert page is not None
            page_items = page.get("items")
            if not isinstance(page_items, list) or not all(
                isinstance(item, dict) for item in page_items
            ):
                raise E2EFailure(stage, "invalid_page_items")
            items.extend(page_items)
            if len(items) > 1_600:
                raise E2EFailure(stage, "page_capacity_exceeded")
            if page.get("has_more") is False and page.get("next_cursor") is None:
                return items
            after = page.get("next_cursor")
            if not isinstance(after, str) or not after:
                raise E2EFailure(stage, "invalid_page_cursor")
        raise E2EFailure(stage, "page_limit_exceeded")

    def start_event_probe(self, last_event_id: str | None = None) -> SseEventProbe:
        headers = {
            DESKTOP_SESSION_HEADER: self._session_token,
            "Accept": "text/event-stream",
        }
        if last_event_id is not None:
            if (
                not isinstance(last_event_id, str)
                or not last_event_id
                or len(last_event_id.encode("ascii", errors="ignore")) != len(last_event_id)
                or any(character in last_event_id for character in ("\x00", "\r", "\n"))
            ):
                raise E2EFailure("lifecycle_sse", "event_cursor_invalid")
            headers["Last-Event-ID"] = last_event_id
        request = Request(
            f"{self._base_url}/desktop/v2/events",
            headers=headers,
            method="GET",
        )
        return SseEventProbe(
            opener=self._opener,
            request=request,
            timeout_seconds=120.0,
        )


class DesktopScienceWorkflow:
    """Exercise the release v2 Task path through one system-OpenSSH profile."""

    def __init__(
        self,
        api: LocalApi,
        *,
        ssh_host_alias: str,
        registry_sha256: str,
        codex_model: str,
        reasoning_effort: str,
        task_title: str,
        task_objective: str,
        poll_seconds: float,
        activation_timeout_seconds: float,
        run_timeout_seconds: float,
        progress: ProgressReporter | None = None,
        inter_task_delay_seconds: float = 0.0,
        relaunch: Callable[[], LocalApi] | None = None,
        secret_canary: str | None = None,
    ) -> None:
        if SSH_HOST_ALIAS_PATTERN.fullmatch(ssh_host_alias) is None:
            raise E2EFailure("arguments", "ssh_host_alias_invalid")
        if not _is_sha256(registry_sha256):
            raise E2EFailure("arguments", "registry_sha256_invalid")
        if relaunch is not None and not callable(relaunch):
            raise E2EFailure("arguments", "lifecycle_relaunch_invalid")
        if (
            secret_canary is not None
            and (
                not isinstance(secret_canary, str)
                or not 16 <= len(secret_canary.encode("utf-8")) <= 256
                or any(character in secret_canary for character in ("\x00", "\r", "\n"))
            )
        ):
            raise E2EFailure("arguments", "secret_canary_invalid")
        self._api = api
        self._ssh_host_alias = ssh_host_alias
        self._registry_sha256 = registry_sha256
        self._codex_model = codex_model
        self._reasoning_effort = reasoning_effort
        self._task_title = task_title
        self._task_objective = task_objective + CONTEXT_CANARY_INSTRUCTION
        self._poll_seconds = poll_seconds
        self._activation_timeout_seconds = activation_timeout_seconds
        self._run_timeout_seconds = run_timeout_seconds
        self._progress = progress
        self._inter_task_delay_seconds = inter_task_delay_seconds
        self._relaunch = relaunch
        self._secret_canary = secret_canary
        self.profile_id: str | None = None
        self.project_id: str | None = None
        self._profile: dict[str, Any] | None = None
        self._project: dict[str, Any] | None = None
        self._selected_methods: dict[str, str] = {}
        self._tasks: list[TaskObservation] = []
        self._active_task_id: str | None = None
        self._project_create_action_id: str | None = None
        self._project_create_operation_id: str | None = None
        self._lifecycle_evidence: dict[str, object] | None = None

    def run(self) -> dict[str, object]:
        catalog = self._api.request(
            "GET",
            "/desktop/v2/ssh-hosts",
            stage="ssh_catalog",
        )
        assert catalog is not None
        hosts = catalog.get("hosts")
        catalog_generation = catalog.get("catalog_generation")
        if (
            not isinstance(hosts, list)
            or type(catalog_generation) is not int
            or self._ssh_host_alias
            not in {
                item.get("ssh_host_alias")
                for item in hosts
                if isinstance(item, dict)
                and item.get("availability") == "selectable"
            }
        ):
            raise E2EFailure("ssh_catalog", "ssh_host_alias_not_selectable")

        nonce = secrets.token_hex(12)
        profile = self._api.request(
            "POST",
            "/desktop/v2/profiles",
            stage="profile_create",
            body={
                "schema_version": "2",
                "display_name": "OpenEvo release workspace",
                "connection_authority": "system_openssh",
                "ssh_host_alias": self._ssh_host_alias,
            },
            headers={
                RESOURCE_GENERATION_HEADER: str(catalog_generation),
                "Idempotency-Key": f"release-profile-{nonce}",
            },
            expected_status=201,
        )
        assert profile is not None
        self._require_system_profile(profile, connected=False, stage="profile_create")
        self.profile_id = _text(profile, "profile_id", "profile_create")
        self._profile = profile
        self._emit("profile_connect", "waiting")
        operation = self._api.request(
            "POST",
            f"/desktop/v2/profiles/{self.profile_id}/connect",
            stage="profile_connect",
            body={
                "schema_version": "2",
                "expected_connection_generation": profile["connection_generation"],
            },
            headers=self._profile_headers(profile, f"release-connect-{nonce}"),
            expected_status=202,
            timeout_seconds=self._activation_timeout_seconds,
        )
        assert operation is not None
        operation = self._observe_lifecycle_operation(operation, stage="profile_connect")
        self._require_operation_success(operation, "profile_connect")
        profile = self._api.request(
            "GET",
            f"/desktop/v2/profiles/{self.profile_id}",
            stage="profile_connect",
        )
        assert profile is not None
        self._require_system_profile(profile, connected=True, stage="profile_connect")
        self._profile = profile
        self._emit("profile_connect", "succeeded")

        disabled_config = self._project_config(
            {
                target_id: {"enabled": False, "method": None, "config": {}}
                for target_id in REQUIRED_TARGET_IDS
            }
        )
        bootstrap_project = self._create_project(
            profile,
            config=disabled_config,
            display_name="OpenEvo release capability bootstrap",
            key=f"release-bootstrap-project-{nonce}",
        )
        bootstrap_id = _text(bootstrap_project, "project_id", "project_bootstrap")
        self.project_id = bootstrap_id
        self._project = bootstrap_project
        head0 = self._project_head(
            bootstrap_project.get("active_project_head"),
            expected_generation=0,
            stage="project_bootstrap",
        )
        capability_projection = self._api.request(
            "GET",
            f"/desktop/v2/projects/{bootstrap_id}/capabilities",
            stage="project_capabilities",
            timeout_seconds=180.0,
        )
        assert capability_projection is not None
        if capability_projection.get("registry_sha256") != self._registry_sha256:
            raise E2EFailure("project_capabilities", "registry_sha256_mismatch")
        capabilities = capability_projection.get("capabilities")
        if not isinstance(capabilities, dict):
            raise E2EFailure("project_capabilities", "capability_payload_invalid")
        targets, selected_methods = self._select_release_targets(capabilities)
        self._selected_methods = selected_methods

        project = self._update_project(
            bootstrap_project,
            config=self._project_config(targets),
            display_name=RELEASE_PROJECT_DISPLAY_NAME,
            key=f"release-project-{nonce}",
        )
        self._project = project
        if project.get("active_project_head") != head0:
            raise E2EFailure("project_configure", "project_genesis_authority_changed")
        if project.get("state") != "ready" or not isinstance(
            project.get("admission_etag"), str
        ):
            raise E2EFailure("project_create", "project_genesis_not_ready")
        first_validation_count = self._validate_project(
            project,
            key=f"release-validate-first-{nonce}",
        )

        first = self._submit_and_observe_task(
            project,
            ordinal=1,
            key=f"release-task-first-{nonce}",
        )
        head1 = first.successor_project_head
        project = self._get_project(stage="first_successor")
        if project.get("active_project_head") != head1:
            raise E2EFailure("first_successor", "active_project_head_mismatch")
        self._project = project

        if self._inter_task_delay_seconds > 0:
            self._emit("inter_task_delay", "waiting")
            time.sleep(self._inter_task_delay_seconds)
        second_validation_count = self._validate_project(
            project,
            key=f"release-validate-second-{nonce}",
        )
        second = self._submit_and_observe_task(
            project,
            ordinal=2,
            key=f"release-task-second-{nonce}",
        )
        head2 = second.successor_project_head
        project = self._get_project(stage="second_successor")
        if project.get("active_project_head") != head2:
            raise E2EFailure("second_successor", "active_project_head_mismatch")
        self._project = project

        first_predecessor = first.task["admission"]["predecessor_project_head"]
        second_predecessor = second.task["admission"]["predecessor_project_head"]
        if (
            first.context.get("project_head") != first_predecessor
            or first_predecessor != head0
            or second_predecessor != head1
            or second.context.get("project_head") != head1
            or second.context["project_head"].get("runtime_context_snapshot")
            != head1.get("runtime_context_snapshot")
        ):
            raise E2EFailure("reuse", "next_task_context_authority_mismatch")

        return {
            "run_mode": "two_task_subscription_release",
            "verification_scope": [
                "exact_candidate_app_sidecar",
                "system_openssh_remote_workspace",
                "daemon_core_v2",
                "codex_subscription_transcript",
                "atomic_successor_project_heads",
                "next_task_runtime_context_reuse",
                "packaged_renderer_v2_observability",
            ],
            "task_count": 2,
            "remote": {
                "connection_authority": "system_openssh",
                "catalog_selection_verified": True,
                "system_openssh_final_authority_verified": True,
                "core_api_major": 2,
                "core_registry_sha256": self._registry_sha256,
            },
            "project": {
                "project_id_sha256": _digest_text(self.project_id),
                "execution": {
                    "mode": "codex_subscription_transcript",
                    "capture_mode": "transcript",
                    "token_level_metrics_available": False,
                    "harness_id": "codex",
                    "codex_model": self._codex_model,
                    "reasoning_effort": self._reasoning_effort,
                    "task_network_allow_internet": True,
                },
                "target_ids": list(REQUIRED_TARGET_IDS),
                "selected_methods": dict(sorted(self._selected_methods.items())),
                "registry_sha256": self._registry_sha256,
                "validation_check_counts": [
                    first_validation_count,
                    second_validation_count,
                ],
                "initial_project_head": self._head_evidence(head0),
                "active_project_head": self._head_evidence(head2),
            },
            "tasks": [first.evidence, second.evidence],
            "reuse": {
                "first_context_excluded_own_successor": True,
                "second_admission_pinned_first_successor": True,
                "second_context_pinned_first_successor": True,
                "second_runtime_context_equals_first_successor": True,
            },
            "lifecycle": self._require_lifecycle_evidence(),
        }

    def renderer_expectations(self) -> dict[str, object]:
        if self.project_id is None or self._project is None or len(self._tasks) != 2:
            raise E2EFailure("renderer", "renderer_task_authority_unavailable")
        head = self._project_head(
            self._project.get("active_project_head"),
            expected_generation=2,
            stage="renderer",
        )
        return {
            "project_id": self.project_id,
            "project_name": self._project.get("display_name"),
            "ssh_host_alias": self._ssh_host_alias,
            "method_ids": dict(sorted(self._selected_methods.items())),
            "task_ids": [item.task["task_id"] for item in self._tasks],
            "active_project_head": head,
        }

    def cleanup(self) -> dict[str, object]:
        result: dict[str, object] = {
            "active_task_cleanup_required": self._active_task_id is not None,
            "active_task_cancel_requested": False,
            "active_task_terminal": self._active_task_id is None,
            "active_task_cleanup_succeeded": self._active_task_id is None,
            "desktop_disconnect_succeeded": False,
        }
        if self._active_task_id is not None:
            try:
                task = self._api.request(
                    "GET",
                    f"/desktop/v2/tasks/{self._active_task_id}",
                    stage="cleanup",
                )
                assert task is not None
                if task.get("state") in TERMINAL_TASK_STATES:
                    result["active_task_terminal"] = True
                    result["active_task_cleanup_succeeded"] = True
                else:
                    result["active_task_cancel_requested"] = True
                    admission = task.get("admission")
                    if not isinstance(admission, dict):
                        raise E2EFailure("cleanup", "task_admission_invalid")
                    predecessor = admission.get("predecessor_project_head")
                    if not isinstance(predecessor, dict):
                        raise E2EFailure("cleanup", "task_predecessor_invalid")
                    self._api.request(
                        "POST",
                        f"/desktop/v2/tasks/{self._active_task_id}/cancel",
                        stage="cleanup",
                        body={
                            "schema_version": "2",
                            "task_admission_id": admission["task_admission_id"],
                            "admission_sha256": admission["admission_sha256"],
                            "predecessor_project_head_id": predecessor[
                                "project_head_id"
                            ],
                        },
                        headers={
                            RESOURCE_GENERATION_HEADER: str(predecessor["generation"]),
                            "If-Match": task["etag"],
                            "Idempotency-Key": f"release-cleanup-{secrets.token_hex(12)}",
                        },
                        expected_status=202,
                    )
            except BaseException:
                result["active_task_cleanup_succeeded"] = False

        if self.profile_id is not None:
            try:
                profile = self._api.request(
                    "GET",
                    f"/desktop/v2/profiles/{self.profile_id}",
                    stage="cleanup",
                )
                assert profile is not None
                if profile.get("connection_state") == "disconnected":
                    result["desktop_disconnect_succeeded"] = True
                elif profile.get("connection_state") == "connected":
                    operation = self._api.request(
                        "POST",
                        f"/desktop/v2/profiles/{self.profile_id}/disconnect",
                        stage="cleanup",
                        body={
                            "schema_version": "2",
                            "expected_connection_generation": profile[
                                "connection_generation"
                            ],
                        },
                        headers=self._profile_headers(
                            profile,
                            f"release-disconnect-{secrets.token_hex(12)}",
                        ),
                        expected_status=202,
                        timeout_seconds=180.0,
                    )
                    assert operation is not None
                    operation = self._observe_lifecycle_operation(
                        operation,
                        stage="cleanup",
                    )
                    self._require_operation_success(operation, "cleanup")
                    result["desktop_disconnect_succeeded"] = True
            except BaseException:
                result["desktop_disconnect_succeeded"] = False
        return result

    def _project_config(
        self,
        targets: Mapping[str, object],
    ) -> dict[str, object]:
        return {
            "schema_version": "2",
            "task": {
                "title": self._task_title,
                "objective": self._task_objective,
            },
            "workspace": {
                "kind": "scratch",
                "display_name": "OpenEvo release science workspace",
            },
            "execution": {
                "mode": "codex_subscription_transcript",
                "capture_mode": "transcript",
                "token_level_metrics_available": False,
                "harness_id": "codex",
                "codex_model": self._codex_model,
                "reasoning_effort": self._reasoning_effort,
                "token_limit": 32768,
                "task_network_allow_internet": True,
            },
            "evolution": {"targets": dict(targets)},
        }

    def _create_project(
        self,
        profile: Mapping[str, object],
        *,
        config: Mapping[str, object],
        display_name: str,
        key: str,
    ) -> dict[str, Any]:
        generation = profile.get("connection_generation")
        profile_id = profile.get("profile_id")
        if type(generation) is not int or not isinstance(profile_id, str):
            raise E2EFailure("project_create", "profile_authority_invalid")
        first_probe = self._api.start_event_probe()
        reservation_started = time.monotonic()
        operation = self._api.request(
            "POST",
            "/desktop/v2/projects",
            stage="project_create",
            body={
                "schema_version": "2",
                "profile_id": profile_id,
                "profile_connection_generation": generation,
                "display_name": display_name,
                "config": dict(config),
            },
            headers={
                RESOURCE_GENERATION_HEADER: str(generation),
                "Idempotency-Key": key,
            },
            expected_status=202,
            timeout_seconds=min(
                self._activation_timeout_seconds,
                MAX_LIFECYCLE_RESERVATION_MILLISECONDS / 1_000,
            ),
        )
        reservation_latency_ms = max(
            0,
            round((time.monotonic() - reservation_started) * 1_000),
        )
        if reservation_latency_ms >= MAX_LIFECYCLE_RESERVATION_MILLISECONDS:
            raise E2EFailure("project_create", "lifecycle_reservation_timeout")
        assert operation is not None
        operation_id = _text(operation, "operation_id", "project_create")
        request_sha256 = operation.get("request_sha256")
        if operation.get("kind") != "project_create" or not _is_sha256(request_sha256):
            raise E2EFailure("project_create", "lifecycle_reservation_invalid")
        self._project_create_action_id = key
        self._project_create_operation_id = operation_id
        first_event = first_probe.wait(timeout_seconds=self._activation_timeout_seconds)
        self._require_lifecycle_event(
            first_event,
            operation_id=operation_id,
            stage="project_create_sse_initial",
        )
        second_probe = self._api.start_event_probe(
            self._event_id(
                first_event,
                stage="project_create_sse_initial",
            )
        )
        operation, lifecycle = self._observe_project_create_lifecycle(
            operation,
            action_id=key,
            reservation_latency_ms=reservation_latency_ms,
            reconnect_probe=second_probe,
        )
        self._require_operation_success(operation, "project_create")
        result = operation.get("result")
        project_id = result.get("project_id") if isinstance(result, dict) else None
        if result is None or result.get("result_kind") != "project" or not isinstance(
            project_id, str
        ):
            raise E2EFailure("project_create", "project_lifecycle_result_invalid")
        project = self._api.request(
            "GET",
            f"/desktop/v2/projects/{project_id}",
            stage="project_create",
        )
        assert project is not None
        if project.get("state") != "ready":
            raise E2EFailure("project_create", "project_not_ready")
        self._lifecycle_evidence = lifecycle
        return project

    def _observe_lifecycle_operation(
        self,
        operation: Mapping[str, object],
        *,
        stage: str,
    ) -> dict[str, Any]:
        operation_id = _text(operation, "operation_id", stage)
        request_sha256 = operation.get("request_sha256")
        if not _is_sha256(request_sha256):
            raise E2EFailure(stage, "lifecycle_operation_invalid")
        current = dict(operation)
        deadline = time.monotonic() + self._activation_timeout_seconds
        while current.get("status") not in {"succeeded", "failed", "cancelled"}:
            if time.monotonic() >= deadline:
                raise E2EFailure(stage, "lifecycle_operation_timeout")
            current_payload = self._api.request(
                "GET",
                f"/desktop/v2/operations/{operation_id}",
                stage=stage,
            )
            assert current_payload is not None
            if (
                current_payload.get("operation_id") != operation_id
                or current_payload.get("request_sha256") != request_sha256
            ):
                raise E2EFailure(stage, "lifecycle_operation_identity_changed")
            current = current_payload
            if current.get("status") not in {"succeeded", "failed", "cancelled"}:
                time.sleep(self._poll_seconds)
        return current

    def _observe_project_create_lifecycle(
        self,
        operation: Mapping[str, object],
        *,
        action_id: str,
        reservation_latency_ms: int,
        reconnect_probe: object,
    ) -> tuple[dict[str, Any], dict[str, object]]:
        operation_id = _text(operation, "operation_id", "project_create")
        request_sha256 = operation.get("request_sha256")
        if not _is_sha256(request_sha256):
            raise E2EFailure("project_create", "lifecycle_request_invalid")
        phases: list[str] = []

        def observe(current: Mapping[str, object], *, stage: str) -> None:
            if (
                current.get("operation_id") != operation_id
                or current.get("kind") != "project_create"
                or current.get("request_sha256") != request_sha256
            ):
                raise E2EFailure(stage, "lifecycle_operation_identity_changed")
            phase = current.get("phase")
            if not isinstance(phase, str) or phase not in LIFECYCLE_PHASES:
                raise E2EFailure(stage, "lifecycle_phase_invalid")
            if phase in phases:
                return
            if phases and LIFECYCLE_PHASES.index(phase) <= LIFECYCLE_PHASES.index(phases[-1]):
                raise E2EFailure(stage, "lifecycle_phase_regressed")
            phases.append(phase)

        current = dict(operation)
        observe(current, stage="project_create")
        deadline = time.monotonic() + self._activation_timeout_seconds
        relaunched = False
        reconnect_verified = False
        while current.get("status") not in {"succeeded", "failed", "cancelled"}:
            if time.monotonic() >= deadline:
                raise E2EFailure("project_create", "lifecycle_operation_timeout")
            payload = self._api.request(
                "GET",
                f"/desktop/v2/operations/{operation_id}",
                stage="project_create",
            )
            assert payload is not None
            current = payload
            observe(current, stage="project_create")
            if not reconnect_verified:
                wait = getattr(reconnect_probe, "wait", None)
                if not callable(wait):
                    raise E2EFailure("project_create", "lifecycle_sse_probe_invalid")
                reconnect_event = wait(timeout_seconds=self._activation_timeout_seconds)
                self._require_lifecycle_event(
                    reconnect_event,
                    operation_id=operation_id,
                    stage="project_create_sse_reconnect",
                )
                reconnect_verified = True
            if (
                not relaunched
                and current.get("status") not in {"succeeded", "failed", "cancelled"}
            ):
                if self._relaunch is None:
                    raise E2EFailure("project_create", "lifecycle_relaunch_unavailable")
                relaunched_api = self._relaunch()
                if not callable(getattr(relaunched_api, "request", None)):
                    raise E2EFailure("project_create", "lifecycle_relaunch_invalid")
                self._api = relaunched_api
                recovered = self._api.request(
                    "GET",
                    f"/desktop/v2/operations/{operation_id}",
                    stage="project_create_recovery",
                )
                assert recovered is not None
                current = recovered
                observe(current, stage="project_create_recovery")
                relaunched = True
            if current.get("status") not in {"succeeded", "failed", "cancelled"}:
                time.sleep(self._poll_seconds)

        if not reconnect_verified or not relaunched:
            raise E2EFailure("project_create", "lifecycle_recovery_evidence_incomplete")
        created_at = _timestamp_instant(current.get("created_at"), "project_create")
        finished_at = _timestamp_instant(current.get("finished_at"), "project_create")
        terminal_duration_ms = round((finished_at - created_at).total_seconds() * 1_000)
        if terminal_duration_ms <= MIN_LIFECYCLE_TERMINAL_MILLISECONDS:
            raise E2EFailure("project_create", "lifecycle_operation_too_short")
        if len(phases) < 2 or phases[-1] != "finalizing":
            raise E2EFailure("project_create", "lifecycle_phase_evidence_incomplete")

        logs = self._api.page(
            f"/desktop/v2/operations/{operation_id}/logs",
            stage="project_create_logs",
        )
        process_logs: list[dict[str, object]] = []
        for item in logs:
            source = item.get("source")
            text = item.get("text")
            if source not in LIFECYCLE_PROCESS_LOG_SOURCES:
                continue
            if (
                item.get("operation_id") != operation_id
                or type(item.get("sequence")) is not int
                or not isinstance(text, str)
                or not text
            ):
                raise E2EFailure("project_create_logs", "lifecycle_process_log_invalid")
            if self._secret_canary is not None and self._secret_canary in text:
                raise E2EFailure("project_create_logs", "secret_canary_in_process_log")
            process_logs.append(
                {
                    "sequence": item["sequence"],
                    "source": source,
                    "text": text,
                    "truncated": item.get("truncated") is True,
                }
            )
        if not process_logs:
            raise E2EFailure("project_create_logs", "real_process_log_missing")
        process_logs.sort(key=lambda item: int(item["sequence"]))

        result = current.get("result")
        core_project_id = result.get("project_id") if isinstance(result, dict) else None
        projects = self._api.page("/desktop/v2/projects", stage="project_inventory")
        if (
            not isinstance(core_project_id, str)
            or len(projects) != 1
            or projects[0].get("project_id") != core_project_id
        ):
            raise E2EFailure("project_inventory", "core_project_authority_not_singular")
        canary = self._secret_canary
        if canary is None:
            raise E2EFailure("project_create", "secret_canary_missing")
        lifecycle = {
            "operation_kind": "project_create",
            "reservation_status": 202,
            "reservation_latency_ms": reservation_latency_ms,
            "terminal_duration_ms": terminal_duration_ms,
            "action_id_sha256": _digest_text(action_id),
            "operation_id_sha256": _digest_text(operation_id),
            "request_sha256": request_sha256,
            "ordered_phases": phases,
            "process_logs": {
                "entry_count": len(process_logs),
                "sources": sorted({str(item["source"]) for item in process_logs}),
                "content_sha256": _canonical_object_sha256(process_logs),
            },
            "sse_reconnect_verified": True,
            "relaunch_recovery_verified": True,
            "stable_action_id_after_relaunch": True,
            "stable_operation_id_after_relaunch": True,
            "mutation_reissued_after_relaunch": False,
            "core_authority": {
                "project_count": 1,
                "project_mapping_count": 1,
                "applied_create_project_mutation_count": 1,
            },
            "secret_canary_sha256": _digest_text(canary),
            "secret_canary_absent": True,
        }
        return current, lifecycle

    @staticmethod
    def _event_id(observation: object, *, stage: str) -> str:
        if not isinstance(observation, dict):
            raise E2EFailure(stage, "lifecycle_sse_event_invalid")
        return _text(observation, "event_id", stage)

    @staticmethod
    def _require_lifecycle_event(
        observation: object,
        *,
        operation_id: str,
        stage: str,
    ) -> None:
        if not isinstance(observation, dict):
            raise E2EFailure(stage, "lifecycle_sse_event_invalid")
        envelope = observation.get("envelope")
        payload = envelope.get("payload") if isinstance(envelope, dict) else None
        if (
            not isinstance(payload, dict)
            or payload.get("payload_kind") != "lifecycle_operation_changed"
            or payload.get("operation_id") != operation_id
            or envelope.get("event_id") != observation.get("event_id")
        ):
            raise E2EFailure(stage, "lifecycle_sse_event_mismatch")

    def _require_lifecycle_evidence(self) -> dict[str, object]:
        if self._lifecycle_evidence is None:
            raise E2EFailure("project_create", "lifecycle_evidence_missing")
        return dict(self._lifecycle_evidence)

    def lifecycle_release_authority(self) -> tuple[str, str]:
        if self.project_id is None or self._project_create_action_id is None:
            raise E2EFailure("project_create", "lifecycle_release_authority_missing")
        return self.project_id, self._project_create_action_id

    def _select_release_targets(
        self,
        capabilities: Mapping[str, object],
    ) -> tuple[dict[str, object], dict[str, str]]:
        target_items = capabilities.get("targets")
        if not isinstance(target_items, list):
            raise E2EFailure("project_capabilities", "target_inventory_invalid")
        by_id = {
            item.get("target_id"): item
            for item in target_items
            if isinstance(item, dict) and isinstance(item.get("target_id"), str)
        }
        selected: dict[str, object] = {}
        methods: dict[str, str] = {}
        for target_id in ("text_memory", "skill_bundle"):
            target = by_id.get(target_id)
            if not isinstance(target, dict):
                raise E2EFailure("project_capabilities", "required_target_missing")
            method_id = target.get("effective_default_method_id")
            visible = target.get("methods")
            if not isinstance(method_id, str) or not isinstance(visible, list):
                raise E2EFailure("project_capabilities", "effective_default_missing")
            descriptor = next(
                (
                    item
                    for item in visible
                    if isinstance(item, dict) and item.get("method_id") == method_id
                ),
                None,
            )
            support = descriptor.get("support") if isinstance(descriptor, dict) else None
            default_json = (
                descriptor.get("default_config_json")
                if isinstance(descriptor, dict)
                else None
            )
            if (
                not isinstance(support, dict)
                or support.get("overall") != "supported"
                or not isinstance(default_json, str)
            ):
                raise E2EFailure("project_capabilities", "effective_default_unsupported")
            try:
                default_config = json.loads(default_json)
            except json.JSONDecodeError as exc:
                raise E2EFailure(
                    "project_capabilities", "default_config_invalid"
                ) from exc
            if not isinstance(default_config, dict):
                raise E2EFailure("project_capabilities", "default_config_invalid")
            selected[target_id] = {
                "enabled": True,
                "method": method_id,
                "config": default_config,
            }
            methods[target_id] = method_id

        agent = by_id.get("agent_system")
        resolvers = agent.get("selection_resolvers") if isinstance(agent, dict) else None
        if not isinstance(resolvers, list):
            raise E2EFailure(
                "project_capabilities", "agent_system_auto_unsupported"
            )
        auto = next(
            (
                item
                for item in resolvers
                if isinstance(item, dict)
                and item.get("selection_value") == "auto"
            ),
            None,
        )
        resolved = auto.get("resolved_methods") if isinstance(auto, dict) else None
        if (
            not isinstance(resolved, list)
            or not resolved
            or any(
                not isinstance(item, dict)
                or not isinstance(item.get("support"), dict)
                or item["support"].get("overall") != "supported"
                for item in resolved
            )
        ):
            raise E2EFailure("project_capabilities", "agent_system_auto_unsupported")
        selected["agent_system"] = {
            "enabled": True,
            "method": "auto",
            "config": {"target_path": "AGENTS.md"},
        }
        methods["agent_system"] = "auto"
        if set(selected) != set(REQUIRED_TARGET_IDS):
            raise E2EFailure("project_capabilities", "release_target_set_invalid")
        return selected, methods

    def _update_project(
        self,
        project: Mapping[str, object],
        *,
        config: Mapping[str, object],
        display_name: str,
        key: str,
    ) -> dict[str, Any]:
        head = self._project_head(
            project.get("active_project_head"),
            expected_generation=0,
            stage="project_configure",
        )
        updated = self._api.request(
            "PATCH",
            f"/desktop/v2/projects/{self.project_id}",
            stage="project_configure",
            body={
                "schema_version": "2",
                "expected_project_head_id": head["project_head_id"],
                "expected_project_head_manifest_sha256": head["manifest_sha256"],
                "expected_project_config_sha256": project[
                    "project_config_sha256"
                ],
                "display_name": display_name,
                "config": dict(config),
            },
            headers=self._project_headers(project, key),
            timeout_seconds=180.0,
        )
        assert updated is not None
        if (
            updated.get("project_id") != self.project_id
            or updated.get("state") != "ready"
            or not isinstance(updated.get("admission_etag"), str)
        ):
            raise E2EFailure("project_configure", "configured_project_invalid")
        return updated

    def _validate_project(self, project: Mapping[str, object], *, key: str) -> int:
        head = self._project_head(
            project.get("active_project_head"),
            expected_generation=None,
            stage="project_validation",
        )
        validation = self._api.request(
            "POST",
            f"/desktop/v2/projects/{self.project_id}/validate",
            stage="project_validation",
            body={
                "schema_version": "2",
                "expected_project_head_id": head["project_head_id"],
                "expected_project_head_manifest_sha256": head["manifest_sha256"],
                "expected_project_config_sha256": project["project_config_sha256"],
                "capability_registry_sha256": self._registry_sha256,
            },
            headers=self._project_headers(project, key),
            timeout_seconds=180.0,
        )
        assert validation is not None
        checks = validation.get("checks")
        if (
            validation.get("valid") is not True
            or validation.get("registry_sha256") != self._registry_sha256
            or not isinstance(checks, list)
            or not checks
            or any(
                not isinstance(check, dict) or check.get("status") != "passed"
                for check in checks
            )
        ):
            raise E2EFailure("project_validation", "project_validation_failed")
        return len(checks)

    def _submit_and_observe_task(
        self,
        project: Mapping[str, object],
        *,
        ordinal: int,
        key: str,
    ) -> TaskObservation:
        head = self._project_head(
            project.get("active_project_head"),
            expected_generation=ordinal - 1,
            stage=f"task_{ordinal}_submit",
        )
        admission_etag = project.get("admission_etag")
        config_sha256 = project.get("project_config_sha256")
        if not isinstance(admission_etag, str) or not _is_sha256(config_sha256):
            raise E2EFailure(f"task_{ordinal}_submit", "admission_authority_invalid")
        task = self._api.request(
            "POST",
            "/desktop/v2/tasks",
            stage=f"task_{ordinal}_submit",
            body={
                "schema_version": "2",
                "project_id": self.project_id,
                "expected_project_admission_etag": admission_etag,
                "expected_project_head_id": head["project_head_id"],
                "expected_project_head_manifest_sha256": head["manifest_sha256"],
                "expected_project_config_sha256": config_sha256,
            },
            headers={
                RESOURCE_GENERATION_HEADER: str(head["generation"]),
                "Idempotency-Key": key,
            },
            expected_status=202,
            timeout_seconds=180.0,
        )
        assert task is not None
        task_id = _text(task, "task_id", f"task_{ordinal}_submit")
        self._active_task_id = task_id
        deadline = (
            time.monotonic() + self._run_timeout_seconds
            if self._progress is None
            else self._progress.phase_deadline(
                f"task_{ordinal}", self._run_timeout_seconds
            )
        )
        prior_state: object = None
        while time.monotonic() < deadline:
            task = self._api.request(
                "GET",
                f"/desktop/v2/tasks/{task_id}",
                stage=f"task_{ordinal}",
            )
            assert task is not None
            state = task.get("state")
            if state != prior_state:
                self._emit(f"task_{ordinal}", state)
                prior_state = state
            if state == "completed":
                break
            if state in TERMINAL_TASK_STATES:
                raise E2EFailure(f"task_{ordinal}", "task_terminal_failure")
            time.sleep(self._poll_seconds)
        else:
            raise E2EFailure(f"task_{ordinal}", "task_timeout")
        self._active_task_id = None

        admission = task.get("admission")
        attempts = task.get("attempts")
        transition_ref = task.get("successor_transition")
        authoritative_attempt_id = task.get("authoritative_attempt_id")
        if (
            not isinstance(admission, dict)
            or not isinstance(attempts, list)
            or not attempts
            or not isinstance(transition_ref, dict)
            or not isinstance(authoritative_attempt_id, str)
            or authoritative_attempt_id
            not in {
                item.get("attempt_id") for item in attempts if isinstance(item, dict)
            }
            or admission.get("task_id") != task_id
            or admission.get("project_id") != self.project_id
        ):
            raise E2EFailure(f"task_{ordinal}", "task_authority_invalid")
        predecessor = self._project_head(
            admission.get("predecessor_project_head"),
            expected_generation=ordinal - 1,
            stage=f"task_{ordinal}",
        )
        transition_id = _text(
            transition_ref,
            "successor_transition_id",
            f"task_{ordinal}",
        )
        transition = self._api.request(
            "GET",
            f"/desktop/v2/transitions/{transition_id}",
            stage=f"task_{ordinal}_transition",
        )
        assert transition is not None
        transition_authority = transition.get("transition")
        if (
            transition.get("state") != "committed"
            or not isinstance(transition_authority, dict)
            or transition_authority != transition_ref
        ):
            raise E2EFailure(f"task_{ordinal}", "successor_transition_not_committed")
        successor = self._project_head(
            transition_ref.get("successor_project_head"),
            expected_generation=ordinal,
            stage=f"task_{ordinal}",
        )
        if (
            successor.get("predecessor_project_head_id")
            != predecessor.get("project_head_id")
            or transition_ref.get("predecessor_project_head") != predecessor
            or transition_ref.get("expected_successor_generation") != ordinal
            or successor["evolution_revision"].get("artifact_count")
            != len(REQUIRED_TARGET_IDS)
        ):
            raise E2EFailure(f"task_{ordinal}", "successor_project_head_invalid")

        context = self._api.request(
            "GET",
            f"/desktop/v2/tasks/{task_id}/context",
            stage=f"task_{ordinal}_context",
        )
        assert context is not None
        if (
            context.get("task_id") != task_id
            or context.get("task_admission_id") != admission.get("task_admission_id")
            or context.get("project_head") != predecessor
            or context.get("workspace_snapshot") != admission.get("workspace_snapshot")
        ):
            raise E2EFailure(f"task_{ordinal}", "task_context_invalid")
        timeline = self._api.page(
            f"/desktop/v2/tasks/{task_id}/timeline",
            stage=f"task_{ordinal}_timeline",
        )
        event_types = {
            item.get("event_type")
            for item in timeline
            if isinstance(item.get("event_type"), str)
        }
        if not REQUIRED_TASK_EVENT_TYPES.issubset(event_types):
            raise E2EFailure(f"task_{ordinal}", "task_timeline_incomplete")

        evidence = {
            "ordinal": ordinal,
            "task_id_sha256": _digest_text(task_id),
            "state": "completed",
            "task_admission_id_sha256": _digest_text(
                _text(admission, "task_admission_id", f"task_{ordinal}")
            ),
            "admission_sha256": admission.get("admission_sha256"),
            "authoritative_attempt_id_sha256": _digest_text(
                authoritative_attempt_id
            ),
            "attempt_count": len(attempts),
            "predecessor_project_head": self._head_evidence(predecessor),
            "context_project_head": self._head_evidence(context["project_head"]),
            "successor_project_head": self._head_evidence(successor),
            "transition_id_sha256": _digest_text(transition_id),
            "transition_state": "committed",
            "timeline_event_types": sorted(event_types),
            "timeline_event_count": len(timeline),
        }
        observation = TaskObservation(
            evidence=evidence,
            task=task,
            context=context,
            successor_project_head=successor,
            artifacts=(),
        )
        self._tasks.append(observation)
        return observation

    def _get_project(self, *, stage: str) -> dict[str, Any]:
        project = self._api.request(
            "GET",
            f"/desktop/v2/projects/{self.project_id}",
            stage=stage,
        )
        assert project is not None
        return project

    def _project_head(
        self,
        value: object,
        *,
        expected_generation: int | None,
        stage: str,
    ) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise E2EFailure(stage, "project_head_invalid")
        required_text = (
            "project_head_id",
            "project_id",
            "registry_sha256",
            "manifest_sha256",
        )
        if (
            any(not isinstance(value.get(key), str) or not value[key] for key in required_text)
            or type(value.get("generation")) is not int
            or (expected_generation is not None and value["generation"] != expected_generation)
            or value.get("registry_sha256") != self._registry_sha256
            or (self.project_id is not None and value.get("project_id") != self.project_id)
        ):
            raise E2EFailure(stage, "project_head_invalid")
        execution = value.get("effective_execution_snapshot")
        revision = value.get("evolution_revision")
        runtime = value.get("runtime_context_snapshot")
        workspace = value.get("workspace_snapshot")
        if (
            not isinstance(execution, dict)
            or execution.get("execution_mode") != "codex_subscription_transcript"
            or execution.get("capture_mode") != "transcript"
            or execution.get("token_level_metrics_available") is not False
            or not isinstance(revision, dict)
            or not isinstance(runtime, dict)
            or not isinstance(workspace, dict)
            or runtime.get("evolution_revision_id")
            != revision.get("evolution_revision_id")
            or runtime.get("evolution_revision_manifest_sha256")
            != revision.get("manifest_sha256")
            or runtime.get("registry_sha256") != self._registry_sha256
        ):
            raise E2EFailure(stage, "project_head_composition_invalid")
        return value

    def _head_evidence(self, head: Mapping[str, object]) -> dict[str, object]:
        workspace = head["workspace_snapshot"]
        revision = head["evolution_revision"]
        runtime = head["runtime_context_snapshot"]
        execution = head["effective_execution_snapshot"]
        assert isinstance(workspace, dict)
        assert isinstance(revision, dict)
        assert isinstance(runtime, dict)
        assert isinstance(execution, dict)
        predecessor = head.get("predecessor_project_head_id")
        return {
            "project_head_id_sha256": _digest_text(str(head["project_head_id"])),
            "generation": head["generation"],
            "predecessor_project_head_id_sha256": (
                None if predecessor is None else _digest_text(str(predecessor))
            ),
            "manifest_sha256": head["manifest_sha256"],
            "workspace_snapshot": {
                "workspace_snapshot_id_sha256": _digest_text(
                    str(workspace["workspace_snapshot_id"])
                ),
                "manifest_sha256": workspace["manifest_sha256"],
                "entry_count": workspace["entry_count"],
                "byte_size": workspace["byte_size"],
            },
            "evolution_revision": {
                "evolution_revision_id_sha256": _digest_text(
                    str(revision["evolution_revision_id"])
                ),
                "manifest_sha256": revision["manifest_sha256"],
                "artifact_count": revision["artifact_count"],
            },
            "runtime_context_snapshot": {
                "runtime_context_snapshot_id_sha256": _digest_text(
                    str(runtime["runtime_context_snapshot_id"])
                ),
                "manifest_sha256": runtime["manifest_sha256"],
                "runtime_contract_sha256": runtime["runtime_contract_sha256"],
                "registry_sha256": runtime["registry_sha256"],
            },
            "effective_execution_snapshot": {
                "effective_execution_snapshot_id_sha256": _digest_text(
                    str(execution["effective_execution_snapshot_id"])
                ),
                "snapshot_sha256": execution["snapshot_sha256"],
                "producer_id_sha256": _digest_text(str(execution["producer_id"])),
                "mode": execution["execution_mode"],
                "capture_mode": execution["capture_mode"],
                "token_level_metrics_available": execution[
                    "token_level_metrics_available"
                ],
            },
        }

    def _require_system_profile(
        self,
        profile: Mapping[str, object],
        *,
        connected: bool,
        stage: str,
    ) -> None:
        if (
            profile.get("profile_kind") != "system_openssh"
            or profile.get("connection_authority") != "system_openssh"
            or profile.get("ssh_host_alias") != self._ssh_host_alias
            or profile.get("connection_state")
            != ("connected" if connected else "disconnected")
            or type(profile.get("connection_generation")) is not int
            or not isinstance(profile.get("etag"), str)
        ):
            raise E2EFailure(stage, "system_openssh_profile_invalid")
        if connected and (
            profile.get("core_api_major") != 2
            or profile.get("core_registry_sha256") != self._registry_sha256
        ):
            raise E2EFailure(stage, "core_v2_authority_invalid")

    def _profile_headers(
        self,
        profile: Mapping[str, object],
        key: str,
    ) -> dict[str, str]:
        return {
            RESOURCE_GENERATION_HEADER: str(profile["connection_generation"]),
            "If-Match": str(profile["etag"]),
            "Idempotency-Key": key,
        }

    def _project_headers(
        self,
        project: Mapping[str, object],
        key: str,
    ) -> dict[str, str]:
        head = project.get("active_project_head")
        if not isinstance(head, dict):
            raise E2EFailure("project", "project_head_invalid")
        return {
            RESOURCE_GENERATION_HEADER: str(head["generation"]),
            "If-Match": str(project["etag"]),
            "Idempotency-Key": key,
        }

    @staticmethod
    def _require_operation_success(
        operation: Mapping[str, object],
        stage: str,
    ) -> None:
        if operation.get("status") != "succeeded":
            failure = operation.get("failure")
            code = failure.get("code") if isinstance(failure, dict) else None
            raise E2EFailure(stage, _safe_code(code))

    def _emit(self, stage: str, state: object) -> None:
        if self._progress is not None:
            self._progress.emit(stage, state, force=True)


def _build_assets(
    root: Path,
    ssh_askpass_helper: Path,
    core_wheel: Path,
    framework_lock: Path,
    managed_runtime_archive: Path,
    daemon_bundle: Path,
    daemon_manifest: Path,
    *,
    timeout_seconds: float,
    progress: ProgressReporter | None = None,
) -> ReleaseAssets:
    root.mkdir(parents=True, exist_ok=True)
    managed_runtime_archive = Path(os.path.abspath(managed_runtime_archive))
    command = [
        sys.executable,
        str(REPOSITORY_ROOT / "desktop/packaging/build_sidecar.py"),
        "--core-wheel",
        str(Path(os.path.abspath(core_wheel))),
        "--framework-lock",
        str(Path(os.path.abspath(framework_lock))),
        "--managed-runtime-archive",
        str(managed_runtime_archive),
        "--daemon-bundle",
        str(Path(os.path.abspath(daemon_bundle))),
        "--daemon-manifest",
        str(Path(os.path.abspath(daemon_manifest))),
        "--release-build",
    ]
    with TemporaryFile(mode="w+b") as build_log:
        process = subprocess.Popen(
            command,
            cwd=REPOSITORY_ROOT,
            stdin=subprocess.DEVNULL,
            stdout=build_log,
            stderr=subprocess.STDOUT,
            env=_release_asset_build_environment(),
            start_new_session=True,
        )
        process_group_id = os.getpgid(process.pid)
        if process_group_id != process.pid:
            _terminate_process_group(
                process,
                process_group_id=process_group_id,
                graceful_timeout_seconds=0,
            )
            raise E2EFailure("release_assets", "build_process_group_invalid")
        returncode = _wait_for_build_process_group(
            process,
            process_group_id=process_group_id,
            timeout_seconds=timeout_seconds,
            progress=progress,
            process_log=build_log,
        )
        if returncode != 0:
            raise E2EFailure("release_assets", "sidecar_build_failed")
        if os.fstat(build_log.fileno()).st_size > MAX_BUILD_PROCESS_LOG_BYTES:
            raise E2EFailure("release_assets", "build_process_log_budget_exceeded")
        build_log.seek(0)
        lines = build_log.read().decode("utf-8", errors="replace").splitlines()
    if not lines:
        raise E2EFailure("release_assets", "sidecar_build_output_missing")
    sidecar = Path(lines[-1].strip())
    return _inspect_release_assets(
        sidecar,
        ssh_askpass_helper,
        core_wheel,
        framework_lock,
        managed_runtime_archive,
        daemon_bundle,
        daemon_manifest,
        validation_root=root / "validated-assets",
    )


def _verify_lifecycle_store_authority(
    config_root: Path,
    *,
    core_project_id: str,
    action_id: str,
) -> dict[str, int]:
    from desktop.sidecar.core_bridge_store_v2 import (
        CoreBridgeStoreV2Error,
        DesktopCoreBridgeStoreV2,
    )

    bridge_root = config_root / "state-v2" / "provider-v2" / "core-bridge-v2"
    try:
        with DesktopCoreBridgeStoreV2(bridge_root) as store:
            summary = store.release_evidence_summary(
                core_project_id=core_project_id,
                action_id=action_id,
            )
    except (CoreBridgeStoreV2Error, OSError, ValueError) as exc:
        raise E2EFailure(
            "project_create",
            "lifecycle_store_authority_invalid",
        ) from exc
    if summary != {
        "project_mapping_count": 1,
        "applied_create_project_mutation_count": 1,
    }:
        raise E2EFailure("project_create", "lifecycle_store_authority_not_singular")
    return summary


def _inspect_release_assets(
    sidecar: Path,
    ssh_askpass_helper: Path,
    wheel: Path,
    lock: Path,
    managed_runtime_archive: Path,
    daemon_bundle: Path,
    daemon_manifest: Path,
    *,
    validation_root: Path,
) -> ReleaseAssets:
    inputs = (
        (sidecar, "packaged_sidecar_invalid", True),
        (ssh_askpass_helper, "ssh_askpass_helper_invalid", True),
        (wheel, "core_wheel_invalid", False),
        (lock, "framework_lock_invalid", False),
        (managed_runtime_archive, "managed_runtime_archive_invalid", False),
        (daemon_bundle, "daemon_bundle_invalid", True),
        (daemon_manifest, "daemon_manifest_invalid", False),
    )
    for item, code, _executable in inputs:
        if item.is_symlink() or not item.is_file() or not stat.S_ISREG(item.stat().st_mode):
            raise E2EFailure("release_assets", code)
    if not os.access(sidecar, os.X_OK):
        raise E2EFailure("release_assets", "packaged_sidecar_not_executable")
    if len({item.name for item, _code, _executable in inputs}) != len(inputs):
        raise E2EFailure("release_assets", "release_asset_snapshot_name_collision")

    source_authorities: list[HeldReleaseAsset] = []
    try:
        source_authorities.extend(
            HeldReleaseAsset.open(path) for path, _code, _executable in inputs
        )
        validation_root.mkdir(mode=0o700)
        root_metadata = validation_root.stat()
        if (
            not stat.S_ISDIR(root_metadata.st_mode)
            or root_metadata.st_uid != os.getuid()
            or stat.S_IMODE(root_metadata.st_mode) != 0o700
        ):
            raise E2EFailure("release_assets", "release_asset_snapshot_root_invalid")
        source_root = validation_root / "source-inputs"
        sidecar_root = validation_root / "sidecar"
        external_parent = validation_root / "external-assets"
        for directory in (source_root, sidecar_root, external_parent):
            directory.mkdir(mode=0o700)
            metadata = directory.stat()
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or metadata.st_uid != os.getuid()
                or stat.S_IMODE(metadata.st_mode) != 0o700
            ):
                raise E2EFailure("release_assets", "release_asset_snapshot_root_invalid")
        source_by_path = {authority.path: authority for authority in source_authorities}
        for path, _code, executable in inputs:
            destination = (
                sidecar_root / path.name
                if path in {sidecar, ssh_askpass_helper}
                else source_root / path.name
            )
            source_by_path[path].copy_to(
                destination,
                executable=executable,
                mode=0o755 if path == ssh_askpass_helper else None,
                failure_stage="release_assets",
                failure_code="release_asset_snapshot_failed",
            )
        for authority in source_authorities:
            authority.verify_unchanged()
    except FileExistsError as exc:
        raise E2EFailure("release_assets", "release_asset_snapshot_root_exists") from exc
    finally:
        for authority in source_authorities:
            authority.close()

    sidecar = validation_root / "sidecar" / sidecar.name
    ssh_askpass_helper = validation_root / "sidecar" / ssh_askpass_helper.name
    source_root = validation_root / "source-inputs"
    wheel = source_root / wheel.name
    lock = source_root / lock.name
    managed_runtime_archive = source_root / managed_runtime_archive.name
    daemon_bundle = source_root / daemon_bundle.name
    daemon_manifest = source_root / daemon_manifest.name
    release_assets_root = validation_root / "external-assets" / "openevo-release-assets"
    authorities: list[HeldReleaseAsset] = []
    root_authority: HeldReleaseAssetsRoot | None = None
    try:
        name, version, wheel_digest = _validate_wheel_lock(wheel, lock)
        source_commit, registry_digest = _release_asset_identity_from_daemon_manifest(
            daemon_manifest
        )
        builder = _load_sidecar_builder()
        builder._validate_fd_bound_bootloader(sidecar)
        try:
            builder._validate_sidecar_excludes_remote_release_assets(sidecar)
        except Exception as exc:
            raise E2EFailure("release_assets", "sidecar_remote_release_assets_present") from exc
        stager = _load_release_assets_stager()
        stager.stage_release_assets(
            bundle=daemon_bundle,
            manifest=daemon_manifest,
            wheel=wheel,
            framework_lock=lock,
            managed_runtime_archive=managed_runtime_archive,
            source_commit=source_commit,
            registry_digest=registry_digest,
            output_dir=release_assets_root,
        )
        root_authority = HeldReleaseAssetsRoot.open(release_assets_root)
        wheel = release_assets_root / "core" / wheel.name
        lock = release_assets_root / "core" / lock.name
        managed_runtime_archive = release_assets_root / "runtime" / managed_runtime_archive.name
        daemon_bundle = release_assets_root / "daemon" / daemon_bundle.name
        daemon_manifest = release_assets_root / "daemon" / daemon_manifest.name
        release_assets_manifest = release_assets_root / "release-assets.json"
        staged_inputs = (
            sidecar,
            ssh_askpass_helper,
            wheel,
            lock,
            managed_runtime_archive,
            daemon_bundle,
            daemon_manifest,
            release_assets_manifest,
        )
        authorities.extend(HeldReleaseAsset.open(path) for path in staged_inputs)
        authority_by_path = {authority.path: authority for authority in authorities}
        if wheel_digest != authority_by_path[wheel].sha256:
            raise E2EFailure("release_assets", "framework_lock_wheel_mismatch")
        _validate_external_release_assets_manifest(
            authority_by_path[release_assets_manifest],
            source_commit=source_commit,
            assets={
                "core/framework-lock.json": authority_by_path[lock],
                f"core/{wheel.name}": authority_by_path[wheel],
                "daemon/openevo-daemon-bundle.json": authority_by_path[daemon_manifest],
                "daemon/openevo-daemon-linux-x86_64": authority_by_path[daemon_bundle],
                f"runtime/{managed_runtime_archive.name}": authority_by_path[
                    managed_runtime_archive
                ],
            },
        )
        root_authority.verify_unchanged()
        for authority in authorities:
            authority.verify_unchanged()
        evidence = {
            "sidecar": authority_by_path[sidecar].evidence(),
            "ssh_askpass_helper": authority_by_path[ssh_askpass_helper].evidence(),
            "core_wheel": {
                **authority_by_path[wheel].evidence(),
                "filename": wheel.name,
                "distribution": name,
                "version": version,
            },
            "framework_lock": {
                **authority_by_path[lock].evidence(),
                "distribution_digest": wheel_digest,
            },
            "managed_runtime_archive": authority_by_path[managed_runtime_archive].evidence(),
            "daemon_bundle": authority_by_path[daemon_bundle].evidence(),
            "daemon_manifest": authority_by_path[daemon_manifest].evidence(),
            "external_release_assets": {
                "source_commit": source_commit,
                "registry_digest": registry_digest,
                "manifest_sha256": authority_by_path[release_assets_manifest].sha256,
                "byte_size": authority_by_path[release_assets_manifest].byte_size,
            },
            "exact_external_release_assets_verified": True,
            "slim_sidecar_excludes_remote_release_assets_verified": True,
        }
    except E2EFailure:
        for authority in authorities:
            authority.close()
        if root_authority is not None:
            root_authority.close()
        raise
    except Exception as exc:
        for authority in authorities:
            authority.close()
        if root_authority is not None:
            root_authority.close()
        raise E2EFailure("release_assets", "packaged_assets_not_exact") from exc
    return ReleaseAssets(
        sidecar=sidecar,
        ssh_askpass_helper=ssh_askpass_helper,
        wheel=wheel,
        framework_lock=lock,
        managed_runtime_archive=managed_runtime_archive,
        daemon_bundle=daemon_bundle,
        daemon_manifest=daemon_manifest,
        authorities=tuple(authorities),
        evidence=evidence,
        release_assets_root=release_assets_root,
        release_assets_manifest=release_assets_manifest,
        source_commit=source_commit,
        registry_digest=registry_digest,
        root_authority=root_authority,
    )


def _release_asset_identity_from_daemon_manifest(manifest_path: Path) -> tuple[str, str]:
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        release = payload.get("release") if isinstance(payload, dict) else None
        core = payload.get("core") if isinstance(payload, dict) else None
        source_commit = release.get("source_commit") if isinstance(release, dict) else None
        registry_digest = core.get("registry_digest") if isinstance(core, dict) else None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise E2EFailure("release_assets", "release_asset_identity_unavailable") from exc
    if (
        not isinstance(source_commit, str)
        or re.fullmatch(r"[0-9a-f]{40}", source_commit) is None
        or not _is_sha256(registry_digest)
    ):
        raise E2EFailure("release_assets", "release_asset_identity_unavailable")
    return source_commit, registry_digest


def _load_release_assets_stager() -> ModuleType:
    path = REPOSITORY_ROOT / "scripts/ci/openevo_desktop_daemon_resource.py"
    spec = importlib.util.spec_from_file_location("openevo_e2e_release_assets_stager", path)
    if spec is None or spec.loader is None:
        raise E2EFailure("release_assets", "release_assets_stager_unavailable")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        raise E2EFailure("release_assets", "release_assets_stager_unavailable") from exc
    if not callable(getattr(module, "stage_release_assets", None)):
        raise E2EFailure("release_assets", "release_assets_stager_unavailable")
    return module


def _validate_external_release_assets_manifest(
    manifest: HeldReleaseAsset,
    *,
    source_commit: str,
    assets: Mapping[str, HeldReleaseAsset],
) -> None:
    manifest.verify_unchanged()
    try:
        payload = os.pread(manifest.descriptor, manifest.byte_size, 0)
        parsed = json.loads(payload.decode("utf-8", errors="strict"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise E2EFailure("release_assets", "external_release_assets_manifest_invalid") from exc
    expected = {
        "files": [
            {
                "relative_path": relative_path,
                "sha256": authority.sha256,
                "byte_size": authority.byte_size,
            }
            for relative_path, authority in sorted(assets.items())
        ],
        "schema_version": 1,
        "source_commit": source_commit,
    }
    if payload != _canonical_json(expected) or parsed != expected:
        raise E2EFailure("release_assets", "external_release_assets_manifest_invalid")
    manifest.verify_unchanged()


def _validate_wheel_lock(wheel: Path, lock: Path) -> tuple[str, str, str]:
    name, version = _wheel_identity(wheel)
    try:
        lock_payload = json.loads(lock.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise E2EFailure("release_assets", "framework_lock_unreadable") from exc
    wheel_digest = _sha256_file(wheel)
    if (
        not isinstance(lock_payload, dict)
        or set(lock_payload) != FRAMEWORK_LOCK_KEYS
        or lock_payload.get("schema_version") != "1"
        or lock_payload.get("distribution") != name
        or lock_payload.get("distribution_version") != version
        or lock_payload.get("distribution_digest") != wheel_digest
        or lock_payload.get("wheel_filename") != wheel.name
    ):
        raise E2EFailure("release_assets", "framework_lock_wheel_mismatch")
    return name, version, wheel_digest


def _load_sidecar_builder() -> ModuleType:
    path = REPOSITORY_ROOT / "desktop/packaging/build_sidecar.py"
    spec = importlib.util.spec_from_file_location("openevo_e2e_sidecar_builder", path)
    if spec is None or spec.loader is None:
        raise E2EFailure("release_assets", "sidecar_builder_unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _wheel_identity(wheel: Path) -> tuple[str, str]:
    try:
        with ZipFile(wheel) as archive:
            metadata_names = [
                name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
            ]
            if len(metadata_names) != 1:
                raise E2EFailure("release_assets", "core_wheel_metadata_invalid")
            metadata = Parser().parsestr(
                archive.read(metadata_names[0]).decode("utf-8", errors="strict")
            )
    except (BadZipFile, OSError, UnicodeDecodeError) as exc:
        raise E2EFailure("release_assets", "core_wheel_unreadable") from exc
    name = metadata.get("Name")
    version = metadata.get("Version")
    if name != "openevo" or not isinstance(version, str) or not version:
        raise E2EFailure("release_assets", "core_wheel_identity_invalid")
    return name, version


def _launch_sidecar(
    assets: ReleaseAssets,
    root: Path,
    *,
    progress: ProgressReporter | None = None,
    state_root: Path | None = None,
    secret_canary: str | None = None,
) -> NativeSidecar:
    if os.name != "posix":
        raise E2EFailure("native_launch", "posix_process_boundary_required")
    root.mkdir(parents=True, mode=0o700, exist_ok=True)
    os.chmod(root, 0o700)
    launch_path = root / "openevo-desktop-sidecar"
    assets.authority(assets.sidecar).copy_to(launch_path, executable=True)
    helper_path = root / "openevo-ssh-askpass"
    helper_authority = assets.authority(assets.ssh_askpass_helper)
    helper_authority.copy_to(helper_path, executable=True, mode=0o755)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    port = int(listener.getsockname()[1])
    executable_fd = os.open(launch_path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    credentials = NativeCredentials.create()
    process_log = TemporaryFile(mode="w+b")
    environment = _sidecar_environment()
    environment["OPENEVO_NATIVE_LISTENER_FD"] = str(LISTENER_FD)
    environment["OPENEVO_NATIVE_EXECUTABLE_FD"] = str(EXECUTABLE_FD)
    environment["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    if secret_canary is not None:
        environment["OPENEVO_E2E_SECRET_CANARY"] = secret_canary
    execution_path = launch_path
    if sys.platform == "darwin":
        environment["OPENEVO_NATIVE_EXECUTABLE_PATH"] = str(launch_path)
    else:
        execution_path = Path(f"/proc/self/fd/{EXECUTABLE_FD}")
    command = [
        str(execution_path),
        "--listener-fd",
        str(LISTENER_FD),
        "--native-instance-stdin",
        "--release-assets-root",
        str(assets.external_release_assets_root().absolute()),
        "--desktop-config-root",
        str(state_root if state_root is not None else root / "state"),
        "--ssh-askpass-helper-path",
        str(helper_path),
        "--ssh-askpass-helper-sha256",
        helper_authority.sha256,
        "--ssh-askpass-helper-byte-size",
        str(helper_authority.byte_size),
    ]
    log_guard = _duplicate_fd(process_log.fileno())
    process: subprocess.Popen[bytes] | None = None
    process_group_id: int | None = None
    try:
        with _fixed_descriptors(listener.fileno(), executable_fd):
            process = subprocess.Popen(
                command,
                executable=str(execution_path),
                stdin=subprocess.PIPE,
                stdout=log_guard,
                stderr=subprocess.STDOUT,
                env=environment,
                pass_fds=(LISTENER_FD, EXECUTABLE_FD),
                start_new_session=True,
            )
            process_group_id = os.getpgid(process.pid)
            if process_group_id != process.pid:
                _terminate_process_group(
                    process,
                    process_group_id=process_group_id,
                    graceful_timeout_seconds=0,
                )
                raise E2EFailure("native_launch", "sidecar_process_group_invalid")
    except BaseException:
        if process is not None and process_group_id is not None:
            _terminate_process_group(
                process,
                process_group_id=process_group_id,
                graceful_timeout_seconds=0,
            )
        process_log.close()
        raise
    finally:
        os.close(log_guard)
        os.close(executable_fd)
        listener.close()
    assert process is not None and process_group_id is not None
    if process.stdin is None:
        _terminate_process_group(
            process,
            process_group_id=process_group_id,
            graceful_timeout_seconds=0,
        )
        process_log.close()
        raise E2EFailure("native_launch", "credential_channel_missing")
    try:
        process.stdin.write(credentials.frame())
        process.stdin.close()
        process.stdin = None
    except OSError as exc:
        _terminate_process_group(
            process,
            process_group_id=process_group_id,
            graceful_timeout_seconds=0,
        )
        process_log.close()
        raise E2EFailure("native_launch", "credential_frame_delivery_failed") from exc
    native = NativeSidecar(
        process=process,
        process_group_id=process_group_id,
        base_url=f"http://127.0.0.1:{port}",
        credentials=credentials,
        process_log=process_log,
        forbidden_log_values=(
            () if secret_canary is None else (secret_canary.encode("utf-8"),)
        ),
    )
    try:
        _wait_sidecar_ready(native, progress=progress)
    except BaseException:
        native.terminate()
        raise
    return native


def _wait_sidecar_ready(
    native: NativeSidecar,
    *,
    progress: ProgressReporter | None = None,
) -> None:
    api = LocalApi(
        native.base_url,
        native.credentials.session_token,
        progress=progress,
        health_check=native.assert_log_budget,
    )
    deadline = (
        time.monotonic() + 60
        if progress is None
        else progress.phase_deadline("native_readiness", 60)
    )
    while time.monotonic() < deadline:
        if progress is not None:
            progress.emit("native_readiness", "waiting")
        if _process_exited_without_reap(native.process):
            raise E2EFailure("native_launch", "sidecar_exited_before_readiness")
        challenge = secrets.token_hex(32)
        try:
            health = api.request(
                "GET",
                NATIVE_HEALTH_ROUTE,
                stage="native_readiness",
                headers={NATIVE_CHALLENGE_HEADER: challenge},
                authenticated=False,
            )
        except E2EFailure:
            time.sleep(0.25)
            continue
        assert health is not None
        domain = (f"{NATIVE_PROTOCOL}\0{native.credentials.instance_id}\0{challenge}").encode(
            "ascii"
        )
        expected = hmac.new(
            native.credentials.readiness_key,
            domain,
            hashlib.sha256,
        ).hexdigest()
        if (
            health.get("service") == "openevo-sidecar"
            and health.get("status") == "ok"
            and health.get("protocol") == NATIVE_PROTOCOL
            and health.get("instance_id") == native.credentials.instance_id
            and hmac.compare_digest(str(health.get("instance_proof", "")), expected)
        ):
            if progress is not None:
                progress.emit("native_readiness", "succeeded", force=True)
            return
        time.sleep(0.25)
    raise E2EFailure("native_launch", "sidecar_readiness_timeout")


def _release_identity(api: LocalApi) -> dict[str, object]:
    try:
        release_contract = json.loads(
            (REPOSITORY_ROOT / "desktop/release-contract.json").read_text(encoding="utf-8")
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise E2EFailure("desktop_version", "release_contract_unreadable") from exc
    v0110 = release_contract.get("v0110") if isinstance(release_contract, dict) else None
    if (
        not isinstance(release_contract, dict)
        or set(release_contract)
        != {
            "accepted_openapi_digests",
            "allowed_provider_kinds",
            "required_feature_flags",
            "schema_version",
            "v0110",
        }
        or release_contract.get("schema_version") != "1"
        or not isinstance(v0110, dict)
        or v0110.get("release_version") != "0.1.10"
        or v0110.get("desktop_local_mutation_major") != 2
        or v0110.get("allow_legacy_route_fallback") is not False
        or v0110.get("allow_direct_core_url") is not False
        or v0110.get("allowed_provider_kinds") != ["desktop_sidecar"]
        or not isinstance(v0110.get("accepted_desktop_openapi_digests"), list)
        or len(v0110["accepted_desktop_openapi_digests"]) != 1
        or not _is_sha256(v0110["accepted_desktop_openapi_digests"][0])
        or not isinstance(v0110.get("accepted_desktop_event_schema_digests"), list)
        or len(v0110["accepted_desktop_event_schema_digests"]) != 1
        or not _is_sha256(v0110["accepted_desktop_event_schema_digests"][0])
        or not isinstance(v0110.get("required_desktop_feature_flags"), list)
        or not v0110["required_desktop_feature_flags"]
        or not all(
            isinstance(flag, str) and flag
            for flag in v0110["required_desktop_feature_flags"]
        )
    ):
        raise E2EFailure("desktop_version", "release_contract_invalid")
    version = api.request(
        "GET",
        "/version",
        stage="desktop_version",
        authenticated=False,
    )
    assert version is not None
    if set(version) != {
        "schema_version",
        "api_name",
        "preferred_major",
        "supported_majors",
        "mutation_major",
        "openapi_sha256",
        "event_schema_sha256",
        "release_version",
        "build_id",
        "source_commit",
        "build_channel",
        "provider_kind",
        "feature_flags",
        "feature_set_sha256",
        "required_core_api_major",
        "mutation_compatible",
    }:
        raise E2EFailure("desktop_version", "desktop_contract_invalid")
    required = {
        "schema_version": "2",
        "api_name": "openevo-desktop-local-api",
        "preferred_major": 2,
        "mutation_major": 2,
        "provider_kind": "desktop_sidecar",
        "build_channel": "release",
        "release_version": "0.1.10",
        "required_core_api_major": 2,
        "mutation_compatible": True,
    }
    if any(version.get(key) != value for key, value in required.items()):
        raise E2EFailure("desktop_version", "not_release_desktop_sidecar")
    if (
        version.get("supported_majors") != [2]
        or version.get("openapi_sha256")
        != v0110["accepted_desktop_openapi_digests"][0]
        or version.get("event_schema_sha256")
        != v0110["accepted_desktop_event_schema_digests"][0]
        or version.get("feature_flags") != v0110["required_desktop_feature_flags"]
    ):
        raise E2EFailure("desktop_version", "desktop_contract_invalid")
    source_commit = version.get("source_commit")
    release_version = version.get("release_version")
    feature_flags = version.get("feature_flags")
    expected_feature_digest = hashlib.sha256(
        json.dumps(
            feature_flags,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
    ).hexdigest()
    if (
        not isinstance(source_commit, str)
        or re.fullmatch(r"[0-9a-f]{7,40}", source_commit) is None
        or not isinstance(release_version, str)
        or not 0 < len(release_version) <= 512
        or any(character in release_version for character in ("\x00", "\r", "\n"))
        or release_version.startswith("/")
        or ABSOLUTE_WINDOWS_PATH.match(release_version)
        or not _is_sha256(version.get("build_id"))
        or version.get("feature_set_sha256") != expected_feature_digest
    ):
        raise E2EFailure("desktop_version", "desktop_build_identity_invalid")
    api.request(
        "GET",
        "/openevo-api/desktop/shell",
        stage="desktop_legacy_route",
        expected_status=404,
        authenticated=False,
    )
    authenticated_state = api.request(
        "GET",
        "/desktop/v2/state",
        stage="desktop_session_probe",
    )
    if not isinstance(authenticated_state, dict) or authenticated_state.get(
        "schema_version"
    ) != "2":
        raise E2EFailure("desktop_session_probe", "desktop_v2_state_invalid")
    api.request(
        "GET",
        "/desktop/v2/state",
        stage="desktop_session_probe_unauthenticated",
        expected_status=(401, 403),
        authenticated=False,
    )
    return {
        "source_commit": source_commit,
        "release_version": release_version,
        "mutation_major": 2,
        "openapi_sha256": version["openapi_sha256"],
        "event_schema_sha256": version["event_schema_sha256"],
        "build_id": version["build_id"],
        "provider_kind": version["provider_kind"],
        "build_channel": version["build_channel"],
        "feature_flags": list(feature_flags),
        "feature_set_sha256": version["feature_set_sha256"],
        "required_core_api_major": version["required_core_api_major"],
        "mutation_compatible": version["mutation_compatible"],
        "v2_only_negotiation_verified": True,
        "authenticated_session_probe": True,
        "unauthenticated_session_rejected": True,
    }


def _release_identity_after_relaunch(
    api: LocalApi,
    *,
    previous_identity: Mapping[str, object],
) -> dict[str, object]:
    current_identity = _release_identity(api)
    stable_keys = set(current_identity) - {"build_id"}
    if set(previous_identity) != set(current_identity) or any(
        previous_identity.get(key) != current_identity[key] for key in stable_keys
    ):
        raise E2EFailure(
            "desktop_version",
            "desktop_relaunch_release_identity_mismatch",
        )
    if previous_identity.get("build_id") == current_identity["build_id"]:
        raise E2EFailure(
            "desktop_version",
            "desktop_relaunch_instance_identity_unchanged",
        )
    return current_identity


def _write_evidence(
    output: Path,
    payload: Mapping[str, object],
    *,
    private_values: Sequence[str],
) -> None:
    _audit_evidence(payload, private_values=private_values)
    encoded = _canonical_json(dict(payload))
    if len(encoded) > MAX_EVIDENCE_BYTES:
        raise E2EFailure("evidence", "evidence_capacity_exceeded")
    temporary: Path | None = None
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.parent / f".{output.name}.{secrets.token_hex(8)}.tmp"
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, output)
    except OSError as exc:
        raise E2EFailure("evidence", "evidence_write_failed") from exc
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError:
                pass


def _write_private_json(path: Path, payload: Mapping[str, object]) -> None:
    encoded = _canonical_json(dict(payload))
    if len(encoded) > MAX_RENDERER_HANDOFF_BYTES:
        raise E2EFailure("renderer", "renderer_handoff_capacity_exceeded")
    descriptor = -1
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            descriptor = -1
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as exc:
        raise E2EFailure("renderer", "renderer_handoff_write_failed") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _read_private_file(path: Path, *, maximum_bytes: int, code: str) -> bytes:
    descriptor = -1
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_size <= 0
            or metadata.st_size > maximum_bytes
        ):
            raise OSError("private file authority is invalid")
        chunks: list[bytes] = []
        remaining = metadata.st_size
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                raise OSError("private file ended during read")
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        if os.read(descriptor, 1):
            raise OSError("private file length changed")
        return payload
    except OSError as exc:
        raise E2EFailure("renderer", code) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _copy_private_file(source: Path, destination: Path, *, maximum_bytes: int) -> None:
    payload = _read_private_file(
        source,
        maximum_bytes=maximum_bytes,
        code="renderer_screenshot_invalid",
    )
    descriptor = -1
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            descriptor = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as exc:
        raise E2EFailure("renderer", "renderer_screenshot_copy_failed") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _write_private_bytes(path: Path, payload: bytes, *, code: str) -> None:
    descriptor = -1
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            descriptor = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as exc:
        raise E2EFailure("renderer", code) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _packaged_web_build_digest(packaged_web_root: Path) -> str:
    manifest_path = packaged_web_root / ".openevo-product-web.json"
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise E2EFailure("renderer", "packaged_web_manifest_invalid") from exc
    if (
        not isinstance(payload, dict)
        or set(payload) != {"schema_version", "build_digest", "files"}
        or payload.get("schema_version") != "1"
        or not _is_sha256(payload.get("build_digest"))
        or not isinstance(payload.get("files"), list)
    ):
        raise E2EFailure("renderer", "packaged_web_manifest_invalid")
    return str(payload["build_digest"])


def _held_json(authority: HeldReleaseAsset, *, code: str) -> tuple[bytes, dict[str, object]]:
    authority.verify_unchanged()
    if authority.byte_size > MAX_RENDERER_CANDIDATE_JSON_BYTES:
        raise E2EFailure("renderer", code)
    try:
        payload = os.pread(authority.descriptor, authority.byte_size, 0)
        if len(payload) != authority.byte_size or os.pread(
            authority.descriptor, 1, authority.byte_size
        ):
            raise OSError("candidate input length changed")
        parsed = json.loads(payload.decode("utf-8", errors="strict"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise E2EFailure("renderer", code) from exc
    authority.verify_unchanged()
    if not isinstance(parsed, dict):
        raise E2EFailure("renderer", code)
    return payload, parsed


def _candidate_file(candidate: Mapping[str, object], role: str) -> dict[str, object]:
    files = candidate.get("files")
    matches = (
        [
            item
            for item in files
            if isinstance(files, list) and isinstance(item, dict) and item.get("role") == role
        ]
        if isinstance(files, list)
        else []
    )
    if len(matches) != 1:
        raise E2EFailure("renderer", "renderer_candidate_manifest_invalid")
    item = matches[0]
    if (
        set(item) != {"byte_size", "filename", "role", "sha256"}
        or not isinstance(item.get("filename"), str)
        or not isinstance(item.get("byte_size"), int)
        or item["byte_size"] <= 0
        or not _is_sha256(item.get("sha256"))
    ):
        raise E2EFailure("renderer", "renderer_candidate_manifest_invalid")
    return item


def _validate_candidate_source_checkout(source_commit: str) -> None:
    command = [
        "git",
        "-C",
        str(REPOSITORY_ROOT),
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
    ]
    try:
        head = subprocess.run(
            ["git", "-C", str(REPOSITORY_ROOT), "rev-parse", "HEAD"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=10,
            env=_build_environment(),
        )
        status = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=10,
            env=_build_environment(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise E2EFailure("renderer", "renderer_source_checkout_unverifiable") from exc
    if (
        head.returncode != 0
        or head.stdout.decode("ascii", errors="ignore").strip() != source_commit
        or status.returncode != 0
        or status.stdout
    ):
        raise E2EFailure("renderer", "renderer_source_checkout_mismatch")


def _validate_renderer_candidate_binding(
    *,
    assets: ReleaseAssets,
    release_candidate_manifest: Path,
    app_bundle_smoke: Path,
    packaged_web_manifest: Path,
    playwright_candidate_evidence: Path,
    packaged_web_root: Path,
) -> RendererCandidateBinding:
    opened: list[HeldReleaseAsset] = []
    try:
        candidate_authority = HeldReleaseAsset.open(release_candidate_manifest)
        opened.append(candidate_authority)
        app_smoke_authority = HeldReleaseAsset.open(app_bundle_smoke)
        opened.append(app_smoke_authority)
        packaged_manifest_authority = HeldReleaseAsset.open(packaged_web_manifest)
        opened.append(packaged_manifest_authority)
        playwright_authority = HeldReleaseAsset.open(playwright_candidate_evidence)
        opened.append(playwright_authority)
        root_manifest_authority = HeldReleaseAsset.open(
            packaged_web_root / ".openevo-product-web.json"
        )
        opened.append(root_manifest_authority)
        _, candidate = _held_json(candidate_authority, code="renderer_candidate_manifest_invalid")
        _, app_smoke = _held_json(app_smoke_authority, code="renderer_candidate_app_smoke_invalid")
        packaged_manifest_bytes, packaged_manifest_payload = _held_json(
            packaged_manifest_authority,
            code="renderer_candidate_packaged_web_manifest_invalid",
        )
        root_manifest_bytes, _ = _held_json(
            root_manifest_authority,
            code="renderer_candidate_packaged_web_manifest_invalid",
        )
        _, playwright_evidence = _held_json(
            playwright_authority,
            code="renderer_candidate_playwright_evidence_invalid",
        )
        source_commit = candidate.get("source_commit")
        if (
            candidate.get("schema_version") != RELEASE_CANDIDATE_SCHEMA_VERSION
            or not isinstance(source_commit, str)
            or re.fullmatch(r"[0-9a-f]{40}", source_commit) is None
            or candidate.get("version") != "0.1.10"
            or not isinstance(candidate.get("desktop_contract"), dict)
            or not isinstance(candidate.get("lifecycle_evidence"), dict)
        ):
            raise E2EFailure("renderer", "renderer_candidate_manifest_invalid")
        _validate_candidate_source_checkout(source_commit)

        candidate_assets = {
            "core_wheel": assets.authority(assets.wheel),
            "framework_lock": assets.authority(assets.framework_lock),
            "daemon_bundle": assets.authority(assets.daemon_bundle),
            "daemon_manifest": assets.authority(assets.daemon_manifest),
        }
        for role, authority in candidate_assets.items():
            item = _candidate_file(candidate, role)
            if (
                item["filename"] != authority.path.name
                or item["sha256"] != authority.sha256
                or item["byte_size"] != authority.byte_size
            ):
                raise E2EFailure("renderer", "renderer_candidate_asset_mismatch")
        managed_runtime = candidate.get("managed_runtime")
        archive = managed_runtime.get("archive") if isinstance(managed_runtime, dict) else None
        runtime_authority = assets.authority(assets.managed_runtime_archive)
        if (
            not isinstance(archive, dict)
            or archive.get("filename") != runtime_authority.path.name
            or archive.get("sha256") != runtime_authority.sha256
            or archive.get("byte_size") != runtime_authority.byte_size
        ):
            raise E2EFailure("renderer", "renderer_candidate_asset_mismatch")

        packaged_role = _candidate_file(candidate, "packaged_web_manifest")
        playwright_role = _candidate_file(candidate, "playwright_evidence")
        desktop_dmg_role = _candidate_file(candidate, "desktop_dmg")
        app_smoke_role = _candidate_file(candidate, "app_bundle_smoke")
        if (
            packaged_role["filename"] != packaged_web_manifest.name
            or packaged_role["sha256"] != packaged_manifest_authority.sha256
            or packaged_role["byte_size"] != packaged_manifest_authority.byte_size
            or playwright_role["filename"] != playwright_candidate_evidence.name
            or playwright_role["sha256"] != playwright_authority.sha256
            or playwright_role["byte_size"] != playwright_authority.byte_size
            or app_smoke_role["filename"] != app_bundle_smoke.name
            or app_smoke_role["sha256"] != app_smoke_authority.sha256
            or app_smoke_role["byte_size"] != app_smoke_authority.byte_size
            or packaged_manifest_bytes != root_manifest_bytes
        ):
            raise E2EFailure("renderer", "renderer_candidate_asset_mismatch")
        app_binary_sha256 = app_smoke.get("binary_sha256")
        app_source_dmg = app_smoke.get("source_dmg")
        candidate_macos = candidate.get("macos")
        candidate_helper = (
            candidate_macos.get("ssh_askpass_helper")
            if isinstance(candidate_macos, dict)
            else None
        )
        candidate_sidecar_sha256 = (
            app_binary_sha256.get("bundled_external_bin")
            if isinstance(app_binary_sha256, dict)
            else None
        )
        sidecar_authority = assets.authority(assets.sidecar)
        helper_authority = assets.authority(assets.ssh_askpass_helper)
        if (
            app_smoke.get("schema_version") != 3
            or app_smoke.get("launch_origin") != "mounted_dmg"
            or app_smoke.get("bundled_external_bin") != "openevo-desktop-sidecar"
            or not isinstance(app_source_dmg, dict)
            or app_source_dmg
            != {
                "filename": desktop_dmg_role["filename"],
                "sha256": desktop_dmg_role["sha256"],
            }
            or not isinstance(app_binary_sha256, dict)
            or set(app_binary_sha256) != {"native_executable", "bundled_external_bin"}
            or not _is_sha256(app_binary_sha256.get("native_executable"))
            or not _is_sha256(candidate_sidecar_sha256)
            or app_smoke.get("sidecar_ready") is not True
            or app_smoke.get("bundled_external_bin_resolved") is not True
            or app_smoke.get("native_listener_fd_handoff") is not True
            or app_smoke.get("native_executable_fd_handoff") is not True
            or app_smoke.get("process_group_cleanup") is not True
            or candidate_sidecar_sha256 != sidecar_authority.sha256
            or not isinstance(candidate_helper, dict)
            or set(candidate_helper)
            != {
                "architecture",
                "byte_size",
                "mode",
                "relative_path",
                "sha256",
                "signature",
            }
            or candidate_helper.get("relative_path")
            != "Contents/MacOS/openevo-ssh-askpass"
            or candidate_helper.get("mode") != "0755"
            or candidate_helper.get("signature") != "adhoc"
            or candidate_helper.get("sha256") != helper_authority.sha256
            or candidate_helper.get("byte_size") != helper_authority.byte_size
            or stat.S_IMODE(assets.ssh_askpass_helper.stat().st_mode) != 0o755
        ):
            raise E2EFailure("renderer", "renderer_candidate_app_smoke_invalid")
        build_digest = packaged_manifest_payload.get("build_digest")
        packaged_evidence = playwright_evidence.get("packaged_web")
        evidence_manifest = (
            packaged_evidence.get("manifest") if isinstance(packaged_evidence, dict) else None
        )
        if (
            not _is_sha256(build_digest)
            or playwright_evidence.get("schema_version") != 2
            or playwright_evidence.get("composition") != "packaged_web"
            or playwright_evidence.get("provider_kind") != "desktop_sidecar"
            or playwright_evidence.get("source_commit") != source_commit
            or playwright_evidence.get("status") != "passed"
            or not isinstance(packaged_evidence, dict)
            or packaged_evidence.get("build_digest") != build_digest
            or not isinstance(evidence_manifest, dict)
            or evidence_manifest.get("sha256") != packaged_manifest_authority.sha256
            or evidence_manifest.get("filename") != packaged_web_manifest.name
        ):
            raise E2EFailure("renderer", "renderer_candidate_playwright_evidence_invalid")
        return RendererCandidateBinding(
            packaged_web_root=packaged_web_root,
            source_commit=source_commit,
            version=str(candidate["version"]),
            build_digest=str(build_digest),
            evidence={
                "source_commit": source_commit,
                "candidate_version": str(candidate["version"]),
                "release_candidate_manifest_sha256": candidate_authority.sha256,
                "desktop_dmg_sha256": desktop_dmg_role["sha256"],
                "app_bundle_smoke_sha256": app_smoke_authority.sha256,
                "candidate_packaged_sidecar_sha256": candidate_sidecar_sha256,
                "candidate_ssh_askpass_helper_sha256": helper_authority.sha256,
                "candidate_native_sidecar_smoke_verified": True,
                "exact_candidate_packaged_sidecar_verified": True,
                "exact_candidate_ssh_askpass_helper_verified": True,
                "packaged_web_manifest_sha256": packaged_manifest_authority.sha256,
                "playwright_candidate_evidence_sha256": playwright_authority.sha256,
                "packaged_web_build_digest": build_digest,
                "source_checkout_verified": True,
            },
            authorities=tuple(opened),
        )
    except BaseException:
        for authority in opened:
            authority.close()
        raise


def _validate_renderer_result(
    payload: object,
    *,
    expectations: Mapping[str, object],
    source_commit: str,
    packaged_web_build_digest: str,
    screenshot_sha256: str,
) -> dict[str, object]:
    if not isinstance(payload, dict) or set(payload) != {
        "schema_version",
        "kind",
        "outcome",
        "provider_kind",
        "source_commit",
        "packaged_web_build_digest",
        "desktop_api_major",
        "renderer_ready",
        "builtin_sample_count",
        "project_id_sha256",
        "task_count",
        "task_id_sha256",
        "active_project_head_generation",
        "evolution_artifact_count",
        "system_openssh_workspace_verified",
        "remote_target_controls_verified",
        "secret_canary_absent",
        "selected_methods",
        "observed_route_kinds",
        "screenshot_sha256",
    }:
        raise E2EFailure("renderer", "renderer_result_schema_invalid")
    expected_task_ids = expectations.get("task_ids")
    active_head = expectations.get("active_project_head")
    expected_methods = expectations.get("method_ids")
    if (
        not isinstance(expected_task_ids, list)
        or len(expected_task_ids) != 2
        or not all(isinstance(item, str) and item for item in expected_task_ids)
        or not isinstance(active_head, dict)
        or not isinstance(expected_methods, dict)
    ):
        raise E2EFailure("renderer", "renderer_expectation_invalid")
    required_scalars = {
        "schema_version": "2",
        "kind": "openevo_desktop_live_renderer_observability",
        "outcome": "passed",
        "provider_kind": "desktop_sidecar",
        "source_commit": source_commit,
        "packaged_web_build_digest": packaged_web_build_digest,
        "desktop_api_major": 2,
        "renderer_ready": True,
        "builtin_sample_count": 2,
        "system_openssh_workspace_verified": True,
        "remote_target_controls_verified": True,
        "secret_canary_absent": True,
        "project_id_sha256": _digest_text(str(expectations["project_id"])),
        "task_count": 2,
        "task_id_sha256": [_digest_text(item) for item in expected_task_ids],
        "active_project_head_generation": active_head.get("generation"),
        "evolution_artifact_count": (
            active_head.get("evolution_revision", {}).get("artifact_count")
            if isinstance(active_head.get("evolution_revision"), dict)
            else None
        ),
        "selected_methods": dict(sorted(expected_methods.items())),
        "observed_route_kinds": ["desktop_v2", "packaged_web"],
        "screenshot_sha256": screenshot_sha256,
    }
    if any(payload.get(key) != value for key, value in required_scalars.items()):
        raise E2EFailure("renderer", "renderer_result_identity_mismatch")
    return dict(payload)


def _audit_evidence(value: object, *, private_values: Sequence[str]) -> None:
    secrets_to_reject = tuple(item for item in private_values if item)

    def visit(item: object) -> None:
        if isinstance(item, dict):
            for key, child in item.items():
                lowered = str(key).lower()
                if (
                    lowered in FORBIDDEN_EVIDENCE_KEYS
                    or lowered.endswith("_path")
                    or any(
                        fragment in lowered
                        for fragment in (
                            "bearer",
                            "credential",
                            "mutation_token",
                            "password",
                            "passphrase",
                            "private_key",
                            "ssh_auth_sock",
                        )
                    )
                ):
                    raise E2EFailure("evidence", "forbidden_evidence_field")
                if lowered not in EVIDENCE_ALLOWED_KEYS:
                    raise E2EFailure("evidence", "evidence_field_not_allowlisted")
                if lowered.endswith("sha256") and child is not None:
                    valid_digest = _is_sha256(child) or (
                        isinstance(child, list) and all(_is_sha256(digest) for digest in child)
                    )
                    if not valid_digest:
                        raise E2EFailure("evidence", "invalid_evidence_digest")
                if lowered in {"stage", "code"} and _safe_code(child) != child:
                    raise E2EFailure("evidence", "invalid_evidence_code")
                visit(child)
            return
        if isinstance(item, list):
            for child in item:
                visit(child)
            return
        if isinstance(item, str):
            if item.startswith("/") or ABSOLUTE_WINDOWS_PATH.match(item):
                raise E2EFailure("evidence", "host_path_in_evidence")
            lowered = item.lower()
            if (
                item.startswith("~")
                or lowered.startswith("file:")
                or "://" in item
                or "-----begin " in lowered
                or "bearer " in lowered
                or "ssh_auth_sock" in lowered
                or any(ord(character) < 0x20 for character in item)
            ):
                raise E2EFailure("evidence", "sensitive_text_in_evidence")
            if len(item.encode("utf-8")) > 512:
                raise E2EFailure("evidence", "evidence_text_capacity_exceeded")
            if any(secret in item for secret in secrets_to_reject):
                raise E2EFailure("evidence", "secret_in_evidence")

    visit(value)


def _structural_check() -> None:
    contract = REPOSITORY_ROOT / "desktop/release-contract.json"
    launcher = REPOSITORY_ROOT / "desktop/server/launcher.py"
    renderer = (
        REPOSITORY_ROOT
        / "desktop/tests/product-browser/release-live-observability.pw.ts"
    )
    try:
        contract_payload = json.loads(contract.read_text(encoding="utf-8"))
        launcher_text = launcher.read_text(encoding="utf-8")
        runner_text = Path(__file__).read_text(encoding="utf-8")
        renderer_text = renderer.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise E2EFailure("structural_check", "release_sources_unavailable") from exc
    if (
        set(contract_payload)
        != {
            "accepted_openapi_digests",
            "allowed_provider_kinds",
            "required_feature_flags",
            "schema_version",
            "v0110",
        }
        or contract_payload.get("schema_version") != "1"
        or contract_payload.get("allowed_provider_kinds") != ["desktop_sidecar"]
        or len(contract_payload.get("accepted_openapi_digests", [])) != 1
        or not _is_sha256(contract_payload["accepted_openapi_digests"][0])
        or not contract_payload.get("required_feature_flags")
        or not isinstance(contract_payload.get("v0110"), dict)
    ):
        raise E2EFailure("structural_check", "release_provider_policy_invalid")
    required = (
        'parser.add_argument("--listener-fd", type=int, required=True)',
        'parser.add_argument("--native-instance-stdin", action="store_true", required=True)',
        "_read_native_instance_frame()",
        "server = uvicorn.Server(config)",
        "_defer_packaged_server_signal_replay(",
        "server.run(sockets=[listener])",
        '_NATIVE_SESSION_PROBE_ROUTE = "/openevo-native/session"',
    )
    if any(marker not in launcher_text for marker in required):
        raise E2EFailure("structural_check", "native_boundary_missing")
    forbidden_release_sources = (
        "/desktop/" + "v1/",
        "ssh_" + "agent",
    )
    forbidden_release_flags = (
        "--host",
        "--port",
        "--user",
        "--expected-host-key-fingerprint",
        "--host-key-algorithm",
    )
    if (
        "/desktop/v2/" not in runner_text
        or 'add_argument("--ssh-host-alias")' not in runner_text
        or '"connection_authority": "system_openssh"' not in runner_text
        or any(marker in runner_text for marker in forbidden_release_sources)
        or any(
            f'add_argument("{flag}")' in runner_text
            for flag in forbidden_release_flags
        )
        or '/desktop/v2/' not in renderer_text
        or 'z.literal("2")' not in renderer_text
        or any(marker in renderer_text for marker in forbidden_release_sources[:2])
    ):
        raise E2EFailure("structural_check", "v2_system_openssh_boundary_missing")


def _event_inventory(
    items: Sequence[Mapping[str, object]],
    categorical_fields: Sequence[str],
) -> dict[str, object]:
    evidence: dict[str, object] = {
        "count": len(items),
        "content_sha256": [
            item.get("content_sha256")
            for item in items[:MAX_EVIDENCE_ITEMS]
            if _is_sha256(item.get("content_sha256"))
        ],
        "evidence_truncated": len(items) > MAX_EVIDENCE_ITEMS,
    }
    for field_name in categorical_fields:
        evidence[f"{field_name}_values"] = sorted(
            {str(item[field_name]) for item in items if isinstance(item.get(field_name), str)}
        )
    return evidence


def _artifact_evidence(artifact: Mapping[str, object], stage: str) -> dict[str, object]:
    lineage = artifact.get("lineage")
    source_artifact_ids = lineage.get("source_artifact_ids") if isinstance(lineage, dict) else None
    source_dataset_ids = lineage.get("source_dataset_ids") if isinstance(lineage, dict) else None
    return {
        "artifact_id_sha256": _digest_text(_text(artifact, "id", stage)),
        "artifact_type": artifact.get("artifact_type"),
        "target_id": artifact.get("target_id"),
        "method_id": lineage.get("method_id") if isinstance(lineage, dict) else None,
        "content_sha256": artifact.get("content_sha256"),
        "byte_size": artifact.get("byte_size"),
        "selected": artifact.get("selected"),
        "promoted": artifact.get("promoted"),
        "release_enabled": artifact.get("release_enabled"),
        "source_artifact_count": len(source_artifact_ids)
        if isinstance(source_artifact_ids, list)
        else -1,
        "source_artifact_ids_sha256": [
            _digest_text(source_artifact_id)
            for source_artifact_id in source_artifact_ids[:MAX_EVIDENCE_ITEMS]
            if isinstance(source_artifact_id, str) and source_artifact_id
        ]
        if isinstance(source_artifact_ids, list)
        else [],
        "source_dataset_count": len(source_dataset_ids)
        if isinstance(source_dataset_ids, list)
        else -1,
        "produced_revision": _revision_evidence(artifact.get("produced_revision"), stage),
    }


def _required_target_document_sha256(
    content: Mapping[str, object],
    *,
    target_id: str,
    stage: str,
) -> str:
    documents = content.get("documents")
    if (
        content.get("truncated") is not False
        or not isinstance(documents, list)
        or not documents
        or content.get("total_documents") != len(documents)
    ):
        raise E2EFailure(stage, "artifact_content_not_complete")
    complete = [
        document
        for document in documents
        if isinstance(document, dict)
        and document.get("truncated") is False
        and _is_sha256(document.get("content_sha256"))
        and isinstance(document.get("content"), str)
        and isinstance(document.get("byte_size"), int)
        and not isinstance(document.get("byte_size"), bool)
        and document["byte_size"] >= 0
    ]
    if len(complete) != len(documents):
        raise E2EFailure(stage, "artifact_document_not_complete")
    total_bytes = 0
    for document in complete:
        content_bytes = str(document["content"]).encode("utf-8")
        if (
            len(content_bytes) != document["byte_size"]
            or hashlib.sha256(content_bytes).hexdigest() != document["content_sha256"]
        ):
            raise E2EFailure(stage, "artifact_document_digest_mismatch")
        total_bytes += len(content_bytes)
    if (
        content.get("total_utf8_bytes") != total_bytes
        or content.get("returned_utf8_bytes") != total_bytes
    ):
        raise E2EFailure(stage, "artifact_content_byte_count_mismatch")
    if target_id == "skill_bundle":
        selected = [
            document for document in complete if document.get("relative_path") == "SKILL.md"
        ]
    else:
        selected = complete if len(complete) == 1 else []
    if len(selected) != 1:
        raise E2EFailure(stage, "runtime_document_ambiguous")
    return str(selected[0]["content_sha256"])


def _revision_evidence(value: object, stage: str) -> dict[str, object]:
    revision = _revision(value, stage)
    return {
        "id_sha256": _digest_text(str(revision["id"])),
        "generation": revision["generation"],
        "manifest_sha256": revision["manifest_sha256"],
    }


def _revision(value: object, stage: str) -> dict[str, Any]:
    if (
        not isinstance(value, dict)
        or not isinstance(value.get("id"), str)
        or not value["id"]
        or not isinstance(value.get("project_id"), str)
        or not value["project_id"]
        or not isinstance(value.get("generation"), int)
        or value["generation"] < 0
        or not _is_sha256(value.get("manifest_sha256"))
    ):
        raise E2EFailure(stage, "invalid_revision")
    return value


def _require_operation_success(operation: Mapping[str, object], stage: str) -> None:
    if operation.get("state") == "succeeded":
        return
    error = operation.get("error")
    code = error.get("code") if isinstance(error, dict) else "operation_not_succeeded"
    raise E2EFailure(stage, _safe_code(code))


def _nested(value: Mapping[str, object], *keys: str) -> object:
    current: object = value
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _text(value: Mapping[str, object], key: str, stage: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item:
        raise E2EFailure(stage, f"invalid_{key}")
    return item


def _etag(value: Mapping[str, object], stage: str) -> str:
    etag = value.get("etag")
    if not isinstance(etag, str) or re.fullmatch(r'"[0-9a-f]{64}"', etag) is None:
        raise E2EFailure(stage, "invalid_etag")
    return etag


def _remote_error_code(payload: bytes) -> str:
    try:
        document = json.loads(payload.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return "unexpected_http_status"
    if not isinstance(document, dict):
        return "unexpected_http_status"
    return _safe_code(document.get("code"))


def _safe_code(value: object) -> str:
    if isinstance(value, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,127}", value):
        return value
    return "unexpected_failure"


def _read_bounded(stream: Any) -> bytes:
    payload = stream.read(MAX_HTTP_RESPONSE_BYTES + 1)
    if len(payload) > MAX_HTTP_RESPONSE_BYTES:
        raise E2EFailure("desktop_local_api", "response_capacity_exceeded")
    return payload


def _canonical_json(payload: object) -> bytes:
    return (
        json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True) + "\n"
    ).encode("utf-8")


def _timestamp_instant(value: object, stage: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise E2EFailure(stage, "lifecycle_timestamp_invalid")
    try:
        parsed = datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    except ValueError as exc:
        raise E2EFailure(stage, "lifecycle_timestamp_invalid") from exc
    if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
        raise E2EFailure(stage, "lifecycle_timestamp_invalid")
    return parsed


def _canonical_object_sha256(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_descriptor(descriptor: int, expected_size: int) -> str:
    digest = hashlib.sha256()
    offset = 0
    while offset < expected_size:
        chunk = os.pread(descriptor, min(1024 * 1024, expected_size - offset), offset)
        if not chunk:
            raise OSError("release asset ended during hashing")
        digest.update(chunk)
        offset += len(chunk)
    if os.pread(descriptor, 1, expected_size):
        raise OSError("release asset grew during hashing")
    return digest.hexdigest()


def _release_asset_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_uid,
        metadata.st_gid,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _resolve_private_temporary_root(path: Path) -> Path:
    try:
        requested = path.stat()
        resolved = path.resolve(strict=True)
        canonical = resolved.lstat()
    except OSError as exc:
        raise E2EFailure("runner", "temporary_root_invalid") from exc
    if (
        not stat.S_ISDIR(canonical.st_mode)
        or stat.S_ISLNK(canonical.st_mode)
        or canonical.st_uid != os.getuid()
        or stat.S_IMODE(canonical.st_mode) != 0o700
        or (requested.st_dev, requested.st_ino) != (canonical.st_dev, canonical.st_ino)
    ):
        raise E2EFailure("runner", "temporary_root_invalid")
    return resolved


def _file_evidence(path: Path) -> dict[str, object]:
    return {"sha256": _sha256_file(path), "byte_size": path.stat().st_size}


def _digest_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and SHA256_PATTERN.fullmatch(value) is not None


def _duplicate_fd(descriptor: int) -> int:
    return int(fcntl.fcntl(descriptor, fcntl.F_DUPFD_CLOEXEC, GUARD_FD_MINIMUM))


@contextmanager
def _fixed_descriptors(listener_fd: int, executable_fd: int) -> Iterator[None]:
    listener_guard = _duplicate_fd(listener_fd)
    executable_guard = _duplicate_fd(executable_fd)
    saved: dict[int, int | None] = {}
    try:
        for target in (LISTENER_FD, EXECUTABLE_FD):
            try:
                saved[target] = _duplicate_fd(target)
            except OSError as exc:
                if exc.errno != 9:
                    raise
                saved[target] = None
        os.dup2(listener_guard, LISTENER_FD, inheritable=True)
        os.dup2(executable_guard, EXECUTABLE_FD, inheritable=True)
        yield
    finally:
        for target in (LISTENER_FD, EXECUTABLE_FD):
            previous = saved.get(target)
            if previous is None:
                try:
                    os.close(target)
                except OSError:
                    pass
            else:
                os.dup2(previous, target, inheritable=False)
                os.close(previous)
        os.close(executable_guard)
        os.close(listener_guard)


def _process_exited_without_reap(process: subprocess.Popen[Any]) -> bool:
    if process.returncode is not None:
        return True
    waitid = getattr(os, "waitid", None)
    if waitid is not None:
        try:
            result = waitid(
                os.P_PID,
                process.pid,
                os.WEXITED | os.WNOHANG | os.WNOWAIT,
            )
        except ChildProcessError:
            return True
        return result is not None

    # CPython on macOS does not expose waitid(2). A one-shot kqueue process
    # observer gives us the same essential property: detect NOTE_EXIT without
    # reaping the group leader, so its PGID remains authoritative until every
    # descendant has been terminated.
    if getattr(process, "_openevo_exit_observed", False):
        return True
    kqueue_factory = getattr(select, "kqueue", None)
    kevent_factory = getattr(select, "kevent", None)
    if kqueue_factory is None or kevent_factory is None:
        raise E2EFailure("process", "nonreaping_exit_observer_unavailable")
    observer = getattr(process, "_openevo_exit_observer", None)
    changes: list[object] = []
    if observer is None:
        observer = kqueue_factory()
        setattr(process, "_openevo_exit_observer", observer)
        changes = [
            kevent_factory(
                process.pid,
                filter=select.KQ_FILTER_PROC,
                flags=select.KQ_EV_ADD | select.KQ_EV_ENABLE | select.KQ_EV_ONESHOT,
                fflags=select.KQ_NOTE_EXIT,
            )
        ]
    try:
        events = observer.control(changes, 1, 0)
    except OSError as exc:
        observer.close()
        setattr(process, "_openevo_exit_observer", None)
        if exc.errno in {errno.ECHILD, errno.ESRCH}:
            setattr(process, "_openevo_exit_observed", True)
            return True
        raise E2EFailure("process", "nonreaping_exit_observer_failed") from exc
    if not events:
        return False
    observer.close()
    setattr(process, "_openevo_exit_observer", None)
    setattr(process, "_openevo_exit_observed", True)
    return True


def _wait_exited_without_reap(
    process: subprocess.Popen[Any],
    *,
    timeout_seconds: float,
) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while not _process_exited_without_reap(process):
        if time.monotonic() >= deadline:
            return False
        time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
    return True


def _wait_for_build_process_group(
    process: subprocess.Popen[Any],
    *,
    process_group_id: int,
    timeout_seconds: float,
    progress: ProgressReporter | None = None,
    process_log: BinaryIO | None = None,
) -> int:
    deadline = (
        time.monotonic() + timeout_seconds
        if progress is None
        else progress.phase_deadline("release_assets", timeout_seconds)
    )
    try:
        while not _process_exited_without_reap(process):
            if process_log is not None:
                process_log.flush()
                if os.fstat(process_log.fileno()).st_size > MAX_BUILD_PROCESS_LOG_BYTES:
                    raise E2EFailure("release_assets", "build_process_log_budget_exceeded")
            if progress is not None:
                progress.emit("release_assets", "building")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise E2EFailure("release_assets", "sidecar_build_timeout")
            time.sleep(min(0.25, remaining))
    except BaseException:
        _terminate_process_group(
            process,
            process_group_id=process_group_id,
            graceful_timeout_seconds=0,
        )
        raise
    if progress is not None:
        progress.emit("release_assets", "built", force=True)
    if not _terminate_process_group(
        process,
        process_group_id=process_group_id,
        graceful_timeout_seconds=0,
    ):
        raise E2EFailure("release_assets", "build_process_group_cleanup_failed")
    if process.returncode is None:
        raise E2EFailure("release_assets", "build_process_status_missing")
    return process.returncode


def _run_renderer_verification(
    *,
    native: NativeSidecar,
    workflow: DesktopScienceWorkflow,
    desktop_identity: Mapping[str, object],
    candidate_binding: RendererCandidateBinding,
    root: Path,
    timeout_seconds: float,
    screenshot_output: Path | None,
    progress: ProgressReporter | None,
    secret_canary: str,
) -> dict[str, object]:
    try:
        root.mkdir(mode=0o700)
    except OSError as exc:
        raise E2EFailure("renderer", "renderer_workspace_create_failed") from exc
    packaged_web_root = candidate_binding.packaged_web_root
    playwright = REPOSITORY_ROOT / "desktop" / "node_modules" / ".bin" / "playwright"
    config = REPOSITORY_ROOT / "desktop" / "playwright.release-live.config.ts"
    if not playwright.is_file() or not os.access(playwright, os.X_OK) or not config.is_file():
        raise E2EFailure("renderer", "renderer_harness_unavailable")
    build_digest = _packaged_web_build_digest(packaged_web_root)
    source_commit = desktop_identity.get("source_commit")
    release_version = desktop_identity.get("release_version")
    openapi_sha256 = desktop_identity.get("openapi_sha256")
    event_schema_sha256 = desktop_identity.get("event_schema_sha256")
    build_id = desktop_identity.get("build_id")
    feature_flags = desktop_identity.get("feature_flags")
    feature_set_sha256 = desktop_identity.get("feature_set_sha256")
    if (
        not isinstance(source_commit, str)
        or source_commit != candidate_binding.source_commit
        or release_version != candidate_binding.version
        or build_digest != candidate_binding.build_digest
        or not _is_sha256(openapi_sha256)
        or not _is_sha256(event_schema_sha256)
        or not _is_sha256(build_id)
        or not _is_sha256(feature_set_sha256)
        or desktop_identity.get("mutation_major") != 2
        or desktop_identity.get("required_core_api_major") != 2
        or desktop_identity.get("mutation_compatible") is not True
        or not isinstance(feature_flags, list)
        or not all(isinstance(item, str) for item in feature_flags)
    ):
        raise E2EFailure("renderer", "renderer_bootstrap_identity_invalid")
    expectations = {
        "source_commit": source_commit,
        **workflow.renderer_expectations(),
    }

    handoff_path = root / "renderer-handoff.json"
    result_path = root / "renderer-result.json"
    screenshot_path = root / "renderer-observability.png"
    handoff = {
        "schema_version": "2",
        "kind": "openevo_desktop_live_renderer_handoff",
        "bootstrap": {
            "schema_version": "2",
            "endpoint": native.base_url,
            "session_token": native.credentials.session_token,
            "negotiated_contract": {
                "schema_version": "2",
                "major": 2,
                "mutation_major": 2,
                "openapi_sha256": openapi_sha256,
                "event_schema_sha256": event_schema_sha256,
                "release_version": release_version,
                "build_id": build_id,
                "source_commit": source_commit,
                "build_channel": desktop_identity["build_channel"],
                "provider_kind": "desktop_sidecar",
                "feature_flags": feature_flags,
                "feature_set_sha256": feature_set_sha256,
                "required_core_api_major": 2,
                "mutation_compatible": True,
            },
        },
        "expected": expectations,
        "packaged_web_root": str(packaged_web_root),
        "result_path": str(result_path),
        "screenshot_path": str(screenshot_path),
    }
    _write_private_json(handoff_path, handoff)
    renderer_test_timeout_seconds = _renderer_test_timeout_seconds(timeout_seconds)
    if (
        not 16 <= len(secret_canary.encode("utf-8")) <= 256
        or any(character in secret_canary for character in ("\x00", "\r", "\n"))
    ):
        raise E2EFailure("renderer", "secret_canary_invalid")
    environment = _renderer_environment()
    environment["OPENEVO_DESKTOP_LIVE_RENDERER_HANDOFF"] = str(handoff_path)
    environment["OPENEVO_E2E_SECRET_CANARY"] = secret_canary
    environment["OPENEVO_DESKTOP_LIVE_RENDERER_TIMEOUT_MS"] = str(
        math.ceil(renderer_test_timeout_seconds * 1_000)
    )
    process_log = TemporaryFile(mode="w+b")
    process: subprocess.Popen[bytes] | None = None
    if progress is not None:
        progress.emit("renderer", "started", force=True)
    try:
        process = subprocess.Popen(
            [str(playwright), "test", "--config", str(config)],
            cwd=REPOSITORY_ROOT / "desktop",
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=process_log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        process_group_id = os.getpgid(process.pid)
        if process_group_id != process.pid:
            raise E2EFailure("renderer", "renderer_process_group_invalid")
        deadline = (
            time.monotonic() + _renderer_process_timeout_seconds(timeout_seconds)
            if progress is None
            else progress.phase_deadline(
                "renderer",
                _renderer_process_timeout_seconds(timeout_seconds),
            )
        )
        while not _process_exited_without_reap(process):
            native.assert_log_budget()
            _assert_renderer_process_log(process_log, secret_canary=secret_canary)
            if progress is not None:
                progress.emit("renderer", "running")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise E2EFailure("renderer", "renderer_timeout")
            time.sleep(min(0.25, remaining))
        native.assert_log_budget()
        _assert_renderer_process_log(process_log, secret_canary=secret_canary)
        if not _terminate_process_group(
            process,
            process_group_id=process_group_id,
            graceful_timeout_seconds=0,
        ):
            raise E2EFailure("renderer", "renderer_process_cleanup_failed")
        if process.returncode != 0:
            raise E2EFailure("renderer", "renderer_verification_failed")

        screenshot = _read_private_file(
            screenshot_path,
            maximum_bytes=MAX_RENDERER_SCREENSHOT_BYTES,
            code="renderer_screenshot_invalid",
        )
        screenshot_sha256 = hashlib.sha256(screenshot).hexdigest()
        result_bytes = _read_private_file(
            result_path,
            maximum_bytes=MAX_RENDERER_RESULT_BYTES,
            code="renderer_result_invalid",
        )
        try:
            result_payload = json.loads(result_bytes.decode("utf-8", errors="strict"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise E2EFailure("renderer", "renderer_result_invalid") from exc
        result = _validate_renderer_result(
            result_payload,
            expectations=expectations,
            source_commit=source_commit,
            packaged_web_build_digest=build_digest,
            screenshot_sha256=screenshot_sha256,
        )
        if screenshot_output is not None:
            _copy_private_file(
                screenshot_path,
                screenshot_output,
                maximum_bytes=MAX_RENDERER_SCREENSHOT_BYTES,
            )
        if progress is not None:
            progress.emit("renderer", "succeeded", force=True)
        candidate_binding.verify_unchanged()
        return result
    except BaseException:
        if process is not None and process.returncode is None:
            try:
                process_group_id = os.getpgid(process.pid)
            except ProcessLookupError:
                process_group_id = process.pid
            _terminate_process_group(
                process,
                process_group_id=process_group_id,
                graceful_timeout_seconds=5,
            )
        if progress is not None:
            progress.emit("renderer", "failed", force=True)
        raise
    finally:
        process_log.close()
        for private_path in (handoff_path, result_path, screenshot_path):
            try:
                private_path.unlink()
            except OSError:
                pass


def _assert_renderer_process_log(
    process_log: BinaryIO,
    *,
    secret_canary: str,
) -> None:
    try:
        process_log.flush()
        size = os.fstat(process_log.fileno()).st_size
    except OSError as exc:
        raise E2EFailure("renderer", "renderer_process_log_unavailable") from exc
    if size > MAX_RENDERER_PROCESS_LOG_BYTES:
        raise E2EFailure("renderer", "renderer_process_log_budget_exceeded")
    try:
        content = os.pread(process_log.fileno(), size, 0)
    except OSError as exc:
        raise E2EFailure("renderer", "renderer_process_log_unavailable") from exc
    if len(content) != size:
        raise E2EFailure("renderer", "renderer_process_log_changed")
    if secret_canary.encode("utf-8") in content:
        raise E2EFailure("renderer", "secret_canary_in_renderer_process_log")


def _renderer_test_timeout_seconds(requested_seconds: float) -> float:
    return min(
        MAX_RENDERER_TIMEOUT_SECONDS,
        max(MIN_RENDERER_TIMEOUT_SECONDS, requested_seconds),
    )


def _renderer_process_timeout_seconds(requested_seconds: float) -> float:
    return _renderer_test_timeout_seconds(requested_seconds) + RENDERER_PROCESS_EXIT_GRACE_SECONDS


def _terminate_process_group(
    process: subprocess.Popen[Any],
    *,
    process_group_id: int,
    graceful_timeout_seconds: float,
) -> bool:
    if process_group_id <= 0 or process_group_id != process.pid or process.returncode is not None:
        return False
    graceful = _process_exited_without_reap(process)
    if not graceful:
        try:
            os.killpg(process_group_id, signal.SIGTERM)
        except ProcessLookupError:
            graceful = True
        except PermissionError:
            if not graceful:
                return False
        else:
            graceful = _wait_exited_without_reap(
                process,
                timeout_seconds=graceful_timeout_seconds,
            )
    # The unreaped group leader keeps the captured PGID authoritative while
    # any descendants are force-closed.
    try:
        os.killpg(process_group_id, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    if not _wait_exited_without_reap(process, timeout_seconds=10):
        return False
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        return False
    return graceful


def _sidecar_environment() -> dict[str, str]:
    allowed = (
        "HOME",
        "LANG",
        "LC_ALL",
        "LOGNAME",
        "PATH",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "SSH_AUTH_SOCK",
        "TEMP",
        "TMP",
        "TMPDIR",
        "USER",
    )
    return {name: os.environ[name] for name in allowed if name in os.environ}


def _build_environment() -> dict[str, str]:
    allowed = set(_sidecar_environment()) | {
        "CARGO_HOME",
        "NPM_CONFIG_CACHE",
        "PYTHONPATH",
        "RUSTUP_HOME",
        "UV_CACHE_DIR",
        "VIRTUAL_ENV",
    }
    return {name: value for name, value in os.environ.items() if name in allowed}


def _release_asset_build_environment() -> dict[str, str]:
    allowed = set(_build_environment()) | BUILD_PROXY_ENVIRONMENT_NAMES
    return {name: value for name, value in os.environ.items() if name in allowed}


def _renderer_environment() -> dict[str, str]:
    allowed = {
        "CI",
        "HOME",
        "LANG",
        "LC_ALL",
        "LOGNAME",
        "PATH",
        "PLAYWRIGHT_BROWSERS_PATH",
        "TEMP",
        "TMP",
        "TMPDIR",
        "USER",
    }
    return {name: value for name, value in os.environ.items() if name in allowed}


def _validate_runtime_arguments(args: argparse.Namespace) -> None:
    if args.structural_check:
        return
    if sys.platform != "darwin":
        raise E2EFailure("arguments", "macos_exact_candidate_required")
    required_paths = (
        args.app_bundle,
        args.core_wheel,
        args.framework_lock,
        args.daemon_bundle,
        args.daemon_manifest,
        args.managed_runtime_archive,
        args.release_candidate_manifest,
        args.app_bundle_smoke,
        args.packaged_web_manifest,
        args.playwright_candidate_evidence,
        args.packaged_web_root,
    )
    if any(path is None for path in required_paths):
        raise E2EFailure("arguments", "exact_candidate_inputs_required")
    if (
        not isinstance(args.ssh_host_alias, str)
        or SSH_HOST_ALIAS_PATTERN.fullmatch(args.ssh_host_alias) is None
    ):
        raise E2EFailure("arguments", "ssh_host_alias_invalid")
    assert args.app_bundle is not None
    if (
        args.app_bundle.is_symlink()
        or not args.app_bundle.is_dir()
        or not (args.app_bundle / "Contents/MacOS/openevo-desktop-sidecar").is_file()
        or not (args.app_bundle / "Contents/MacOS/openevo-ssh-askpass").is_file()
    ):
        raise E2EFailure("arguments", "candidate_app_bundle_invalid")
    assert args.packaged_web_root is not None
    if args.packaged_web_root.is_symlink() or not args.packaged_web_root.is_dir():
        raise E2EFailure("arguments", "packaged_web_root_invalid")
    if (
        getattr(args, "codex_model", None) != RELEASE_CODEX_MODEL
        or getattr(args, "reasoning_effort", None) != RELEASE_REASONING_EFFORT
    ):
        raise E2EFailure("arguments", "release_model_profile_required")


def _positive_finite_seconds(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a finite positive number") from exc
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("must be a finite positive number")
    return parsed


def _nonnegative_seconds_at_most(maximum: float) -> Callable[[str], float]:
    def parse(value: str) -> float:
        try:
            parsed = float(value)
        except ValueError as exc:
            raise argparse.ArgumentTypeError("must be a finite non-negative number") from exc
        if not math.isfinite(parsed) or parsed < 0 or parsed > maximum:
            raise argparse.ArgumentTypeError(f"must be a finite number between 0 and {maximum:g}")
        return parsed

    return parse


def _seconds_at_most(maximum: float) -> Callable[[str], float]:
    def parse(value: str) -> float:
        parsed = _positive_finite_seconds(value)
        if parsed > maximum:
            raise argparse.ArgumentTypeError(f"must be no greater than {maximum:g}")
        return parsed

    return parse


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ssh-host-alias")
    parser.add_argument(
        "--app-bundle",
        type=Path,
        help="Exact candidate OpenEvo Desktop.app installed or copied from the DMG.",
    )
    parser.add_argument("--core-wheel", type=Path)
    parser.add_argument("--framework-lock", type=Path)
    parser.add_argument("--daemon-bundle", type=Path)
    parser.add_argument("--daemon-manifest", type=Path)
    parser.add_argument(
        "--managed-runtime-archive",
        type=Path,
        help=(
            "Exact managed subscription Science runtime archive. Required for every real E2E run."
        ),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("desktop-real-science-e2e-evidence.json"),
    )
    parser.add_argument("--codex-model", default=RELEASE_CODEX_MODEL)
    parser.add_argument(
        "--reasoning-effort",
        choices=(RELEASE_REASONING_EFFORT,),
        default=RELEASE_REASONING_EFFORT,
    )
    parser.add_argument("--task-title", default="Release Desktop science E2E")
    parser.add_argument(
        "--task-objective",
        default=(
            "Inspect the scratch workspace, record one concise scientific observation, "
            "and complete without requesting user input."
        ),
    )
    parser.add_argument(
        "--poll-seconds",
        type=_seconds_at_most(MAX_POLL_SECONDS),
        default=2.0,
    )
    parser.add_argument(
        "--progress-seconds",
        type=_seconds_at_most(MAX_PROGRESS_SECONDS),
        default=30.0,
    )
    parser.add_argument(
        "--inter-task-delay-seconds",
        type=_nonnegative_seconds_at_most(MAX_INTER_SESSION_DELAY_SECONDS),
        default=0.0,
        help="Optional maintainer E2E cooldown between the two subscription Tasks.",
    )
    parser.add_argument(
        "--activation-timeout-seconds",
        type=_seconds_at_most(MAX_ACTIVATION_TIMEOUT_SECONDS),
        default=1200.0,
    )
    parser.add_argument(
        "--run-timeout-seconds",
        type=_seconds_at_most(MAX_RUN_TIMEOUT_SECONDS),
        default=7200.0,
    )
    parser.add_argument(
        "--overall-timeout-seconds",
        type=_seconds_at_most(MAX_OVERALL_TIMEOUT_SECONDS),
        default=MAX_OVERALL_TIMEOUT_SECONDS,
    )
    parser.add_argument(
        "--renderer-timeout-seconds",
        type=_seconds_at_most(MAX_RENDERER_TIMEOUT_SECONDS),
        default=300.0,
    )
    parser.add_argument(
        "--renderer-screenshot-output",
        type=Path,
        help="Optional destination for the validated renderer screenshot.",
    )
    parser.add_argument(
        "--release-candidate-manifest",
        type=Path,
        help="Exact candidate release-candidate.json required by renderer verification.",
    )
    parser.add_argument(
        "--app-bundle-smoke",
        type=Path,
        help="Exact candidate app-bundle-smoke.json required by renderer verification.",
    )
    parser.add_argument(
        "--packaged-web-manifest",
        type=Path,
        help="Exact candidate packaged-web-manifest.json required by renderer verification.",
    )
    parser.add_argument(
        "--playwright-candidate-evidence",
        type=Path,
        help="Exact candidate Playwright evidence required by renderer verification.",
    )
    parser.add_argument(
        "--packaged-web-root",
        type=Path,
        help="Exact source checkout packaged web root matching the candidate manifest.",
    )
    parser.add_argument(
        "--structural-check",
        action="store_true",
        help="Check runner/release structure only; does not run E2E or write evidence.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        _validate_runtime_arguments(args)
        if args.structural_check:
            _structural_check()
            print("Desktop real-science E2E structural check passed; E2E was not run.")
            return 0
    except E2EFailure as exc:
        print(f"Desktop real-science E2E not started: {exc.code}", file=sys.stderr)
        return 2

    def interrupt_for_cleanup(_signum: int, _frame: object) -> None:
        raise KeyboardInterrupt

    def overall_timeout(_signum: int, _frame: object) -> None:
        raise E2EFailure("overall", "overall_timeout")

    signal.signal(signal.SIGTERM, interrupt_for_cleanup)
    signal.signal(signal.SIGALRM, overall_timeout)
    signal.setitimer(signal.ITIMER_REAL, args.overall_timeout_seconds)
    progress = ProgressReporter(
        interval_seconds=args.progress_seconds,
        overall_timeout_seconds=args.overall_timeout_seconds,
    )
    progress.emit("runner", "started", force=True)

    started_at = _utc_now()
    secret_canary = f"openevo-release-canary-{secrets.token_hex(32)}"
    evidence: dict[str, object] = {
        "schema_version": "3",
        "kind": "openevo_desktop_real_science_e2e",
        "issue": 220,
        "real_process_boundary": True,
        "outcome": "failed",
        "started_at": started_at,
    }
    private_values = [
        str(args.ssh_host_alias),
        str(args.task_title),
        str(args.task_objective),
        secret_canary,
    ]
    native: NativeSidecar | None = None
    assets: ReleaseAssets | None = None
    renderer_binding: RendererCandidateBinding | None = None
    workflow: DesktopScienceWorkflow | None = None
    cleanup = {
        "active_task_cleanup_required": False,
        "active_task_cancel_requested": False,
        "active_task_terminal": True,
        "active_task_cleanup_succeeded": True,
        "desktop_disconnect_succeeded": False,
        "sidecar_shutdown_succeeded": False,
        "core_ownership_release_requested": False,
    }
    exit_code = 1
    evidence_write_failed = False
    with TemporaryDirectory(prefix="openevo-desktop-real-e2e-") as temporary:
        root = _resolve_private_temporary_root(Path(temporary))
        sidecar_config_root = root / "desktop-config"
        relaunch_serial = 0
        try:
            assert args.app_bundle is not None
            assert args.core_wheel is not None
            assert args.framework_lock is not None
            assert args.managed_runtime_archive is not None
            assert args.daemon_bundle is not None
            assert args.daemon_manifest is not None
            assert args.release_candidate_manifest is not None
            assert args.app_bundle_smoke is not None
            assert args.packaged_web_manifest is not None
            assert args.playwright_candidate_evidence is not None
            assert args.packaged_web_root is not None
            progress.emit("release_assets", "inspecting", force=True)
            app_macos = args.app_bundle / "Contents/MacOS"
            assets = _inspect_release_assets(
                app_macos / "openevo-desktop-sidecar",
                app_macos / "openevo-ssh-askpass",
                args.core_wheel,
                args.framework_lock,
                args.managed_runtime_archive,
                args.daemon_bundle,
                args.daemon_manifest,
                validation_root=root / "validated-assets",
            )
            progress.emit("release_assets", "verified", force=True)
            evidence["release_assets"] = assets.evidence
            renderer_binding = _validate_renderer_candidate_binding(
                assets=assets,
                release_candidate_manifest=args.release_candidate_manifest,
                app_bundle_smoke=args.app_bundle_smoke,
                packaged_web_manifest=args.packaged_web_manifest,
                playwright_candidate_evidence=args.playwright_candidate_evidence,
                packaged_web_root=args.packaged_web_root,
            )
            evidence["renderer_candidate_binding"] = renderer_binding.evidence
            native = _launch_sidecar(
                assets,
                root / "native-initial",
                progress=progress,
                state_root=sidecar_config_root,
                secret_canary=secret_canary,
            )
            private_values.extend(native.credentials.private_values())
            api = LocalApi(
                native.base_url,
                native.credentials.session_token,
                progress=progress,
                health_check=native.assert_log_budget,
            )

            def relaunch_sidecar() -> LocalApi:
                nonlocal desktop_identity, native, relaunch_serial
                if native is None:
                    raise E2EFailure("project_create", "lifecycle_relaunch_unavailable")
                native.assert_log_budget()
                if not native.terminate():
                    raise E2EFailure("project_create", "lifecycle_relaunch_cleanup_failed")
                relaunch_serial += 1
                native = _launch_sidecar(
                    assets,
                    root / f"native-relaunch-{relaunch_serial}",
                    progress=progress,
                    state_root=sidecar_config_root,
                    secret_canary=secret_canary,
                )
                private_values.extend(native.credentials.private_values())
                relaunched_api = LocalApi(
                    native.base_url,
                    native.credentials.session_token,
                    progress=progress,
                    health_check=native.assert_log_budget,
                )
                desktop_identity = _release_identity_after_relaunch(
                    relaunched_api,
                    previous_identity=desktop_identity,
                )
                evidence["desktop"] = desktop_identity
                return relaunched_api

            desktop_identity = _release_identity(api)
            if (
                desktop_identity.get("source_commit") != assets.source_commit
                or desktop_identity.get("source_commit")
                != renderer_binding.source_commit
            ):
                raise E2EFailure("release_assets", "release_asset_source_commit_mismatch")
            evidence["desktop"] = desktop_identity
            if not isinstance(assets.registry_digest, str):
                raise E2EFailure("release_assets", "release_registry_identity_missing")
            workflow = DesktopScienceWorkflow(
                api,
                ssh_host_alias=args.ssh_host_alias,
                registry_sha256=assets.registry_digest,
                codex_model=args.codex_model,
                reasoning_effort=args.reasoning_effort,
                task_title=args.task_title,
                task_objective=args.task_objective,
                poll_seconds=args.poll_seconds,
                activation_timeout_seconds=args.activation_timeout_seconds,
                run_timeout_seconds=args.run_timeout_seconds,
                progress=progress,
                inter_task_delay_seconds=args.inter_task_delay_seconds,
                relaunch=relaunch_sidecar,
                secret_canary=secret_canary,
            )
            evidence.update(workflow.run())
            evidence["desktop"] = desktop_identity
            evidence["renderer"] = _run_renderer_verification(
                native=native,
                workflow=workflow,
                desktop_identity=desktop_identity,
                candidate_binding=renderer_binding,
                root=root / "renderer",
                timeout_seconds=args.renderer_timeout_seconds,
                screenshot_output=args.renderer_screenshot_output,
                progress=progress,
                secret_canary=secret_canary,
            )
            evidence["renderer_observability_verified"] = True
            evidence["renderer_boundary"] = "packaged_web_to_live_desktop_v2"
            evidence["candidate_tauri_launch_verified"] = True
            assets.verify_unchanged()
            renderer_binding.verify_unchanged()
            evidence["outcome"] = "passed"
            exit_code = 0
        except E2EFailure as exc:
            failure: dict[str, object] = {"stage": exc.stage, "code": exc.code}
            if exc.http_status is not None:
                failure["http_status"] = exc.http_status
            evidence["failure"] = failure
        except (KeyboardInterrupt, SystemExit):
            evidence["failure"] = {"stage": "interrupted", "code": "run_interrupted"}
        except BaseException:
            evidence["failure"] = {
                "stage": "runner",
                "code": "unexpected_runner_failure",
            }
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            progress.stop_deadline_enforcement()
            if workflow is not None:
                cleanup.update(workflow.cleanup())
            if native is not None:
                cleanup["core_ownership_release_requested"] = True
                cleanup["sidecar_shutdown_succeeded"] = native.terminate()
            if evidence.get("outcome") == "passed" and workflow is not None:
                try:
                    core_project_id, action_id = workflow.lifecycle_release_authority()
                    authority = _verify_lifecycle_store_authority(
                        sidecar_config_root,
                        core_project_id=core_project_id,
                        action_id=action_id,
                    )
                    lifecycle = evidence.get("lifecycle")
                    core_authority = (
                        lifecycle.get("core_authority")
                        if isinstance(lifecycle, dict)
                        else None
                    )
                    if not isinstance(core_authority, dict):
                        raise E2EFailure(
                            "project_create",
                            "lifecycle_evidence_missing",
                        )
                    core_authority.update(authority)
                except E2EFailure as exc:
                    evidence["outcome"] = "failed"
                    evidence["failure"] = {"stage": exc.stage, "code": exc.code}
                    exit_code = 1
            if renderer_binding is not None:
                renderer_binding.close()
            if assets is not None:
                assets.close()
            evidence["cleanup"] = cleanup
            evidence["finished_at"] = _utc_now()
            cleanup_complete = (
                cleanup["active_task_cleanup_succeeded"]
                and cleanup["desktop_disconnect_succeeded"]
                and cleanup["sidecar_shutdown_succeeded"]
                and cleanup["core_ownership_release_requested"]
            )
            if evidence.get("outcome") == "passed" and not cleanup_complete:
                evidence["outcome"] = "failed"
                evidence["failure"] = {
                    "stage": "cleanup",
                    "code": "ownership_cleanup_incomplete",
                }
                exit_code = 1
            try:
                _write_evidence(
                    args.output,
                    evidence,
                    private_values=private_values,
                )
            except E2EFailure:
                print("Desktop real-science E2E evidence was rejected.", file=sys.stderr)
                evidence_write_failed = True
    if evidence_write_failed:
        return 1
    if exit_code == 0:
        print(
            "Desktop v0.1.10 real-science E2E passed; exact candidate system-OpenSSH "
            "workspace, two v2 Tasks, successor reuse, and packaged renderer verified; "
            "bounded evidence written."
        )
    else:
        failure = evidence.get("failure")
        code = failure.get("code") if isinstance(failure, dict) else "unknown_failure"
        print(f"Desktop real-science E2E failed: {_safe_code(code)}", file=sys.stderr)
    return exit_code


def _utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


if __name__ == "__main__":
    raise SystemExit(main())
