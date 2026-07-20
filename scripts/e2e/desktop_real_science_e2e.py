#!/usr/bin/env python3
"""Run the release Desktop sidecar against a real remote science host.

This is maintainer automation, not a user-facing CLI. It starts the packaged
sidecar through the native inherited-listener and credential-frame boundary,
uses only Desktop Local API v1, and writes bounded redacted evidence.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass, field
from email.parser import Parser
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
import signal
import socket
import stat
import subprocess
import sys
from tempfile import TemporaryDirectory, TemporaryFile
import time
from types import ModuleType
from typing import Any, BinaryIO, Iterator, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import OpenerDirector, ProxyHandler, Request, build_opener
from zipfile import BadZipFile, ZipFile


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
NATIVE_PROTOCOL = "openevo-native-sidecar-v1"
DESKTOP_SESSION_HEADER = "X-OpenEvo-Desktop-Session"
NATIVE_CHALLENGE_HEADER = "X-OpenEvo-Native-Challenge"
LISTENER_FD = 3
EXECUTABLE_FD = 4
GUARD_FD_MINIMUM = 64
MAX_NATIVE_FRAME_BYTES = 512
MAX_HTTP_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_EVIDENCE_BYTES = 128 * 1024
MAX_EVIDENCE_ITEMS = 64
REQUIRED_TARGET_IDS = ("agent_system", "skill_bundle", "text_memory")
RUNTIME_CONTEXT_RECEIPT_PREFIX = "Runtime context receipt v3: "
CONTEXT_CANARY_INSTRUCTION = (
    "\n\nOpenEvo E2E context canary v1: when OPENEVO_MEMORY_FILE, "
    "OPENEVO_SKILLS_DIR, and OPENEVO_AGENT_SYSTEM_FILE are set, read the memory file, "
    "every SKILL.md below the skill directory, and the agent-system file before "
    "completing the task. Do not print environment values, filesystem locations, "
    "credentials, or file contents."
)
TERMINAL_OPERATION_STATES = frozenset({"succeeded", "failed", "cancelled"})
TERMINAL_RUN_STATES = frozenset({"succeeded", "failed", "cancelled"})
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
        "outcome",
        "started_at",
        "finished_at",
        "release_assets",
        "sidecar",
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
        "exact_embedded_assets_verified",
        "desktop",
        "source_commit",
        "build_version",
        "openapi_sha256",
        "provider_kind",
        "build_channel",
        "feature_flags",
        "legacy_route_rejected",
        "authenticated_session_probe",
        "unauthenticated_session_rejected",
        "remote",
        "host_sha256",
        "port",
        "user_sha256",
        "host_key_fingerprint_sha256",
        "project",
        "project_id_sha256",
        "execution_mode",
        "capture_mode",
        "token_level_metrics_available",
        "target_ids",
        "method_ids",
        "registry_digest",
        "validation_check_count",
        "sessions",
        "ordinal",
        "run_id_sha256",
        "status",
        "required_relation",
        "required_revision",
        "pinned_revision",
        "id_sha256",
        "generation",
        "manifest_sha256",
        "timeline",
        "logs",
        "count",
        "content_sha256",
        "evidence_truncated",
        "phase_values",
        "status_values",
        "stream_values",
        "level_values",
        "artifacts",
        "artifact_id_sha256",
        "artifact_type",
        "target_id",
        "selected",
        "promoted",
        "produced_revision",
        "release_enabled",
        "source_artifact_count",
        "artifact_count",
        "artifact_evidence_truncated",
        "artifact_inspections",
        "document_count",
        "total_documents",
        "total_utf8_bytes",
        "truncated",
        "runtime_document_sha256",
        "runtime_context_receipt_sha256",
        "context",
        "adapter_count",
        "reuse",
        "successor_generation_delta",
        "session_1_excluded_own_successor",
        "session_2_pinned_session_1_successor",
        "session_1_artifacts_reused",
        "session_2_runtime_injection_verified",
        "session_2_harness_context_consumed",
        "session_2_lineage_verified",
        "reused_artifact_count",
        "successor_revision",
        "cleanup",
        "active_run_cleanup_required",
        "active_run_cancel_requested",
        "active_run_cancelled",
        "active_run_cleanup_succeeded",
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
HOST_KEY_PATTERN = re.compile(r"^SHA256:[A-Za-z0-9+/]{20,88}={0,2}$")


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

    def copy_to(self, destination: Path, *, executable: bool) -> None:
        self.verify_unchanged()
        target_fd = -1
        try:
            target_fd = os.open(
                destination,
                os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                0o500 if executable else 0o400,
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
            os.fchmod(target_fd, 0o500 if executable else 0o400)
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
            raise E2EFailure("native_launch", "sidecar_snapshot_failed") from exc
        finally:
            if target_fd >= 0:
                os.close(target_fd)

    def close(self) -> None:
        os.close(self.descriptor)


@dataclass(frozen=True)
class ReleaseAssets:
    sidecar: Path
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

    def authority(self, path: Path) -> HeldReleaseAsset:
        matches = [authority for authority in self.authorities if authority.path == path]
        if len(matches) != 1:
            raise E2EFailure("release_assets", "release_asset_authority_missing")
        return matches[0]

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
class SessionObservation:
    evidence: dict[str, object]
    run: dict[str, Any]
    context: dict[str, Any]
    artifacts: tuple[dict[str, Any], ...]
    document_sha256_by_target: dict[str, str]
    runtime_context_receipt_sha256: str | None


class LocalApi:
    def __init__(self, base_url: str, session_token: str) -> None:
        self._base_url = base_url.rstrip("/")
        self._session_token = session_token
        self._opener: OpenerDirector = build_opener(ProxyHandler({}))

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
    ) -> dict[str, Any] | None:
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
            with self._opener.open(request, timeout=30) as response:
                status = response.status
                payload = _read_bounded(response)
        except HTTPError as exc:
            status = exc.code
            payload = _read_bounded(exc)
        except (OSError, URLError) as exc:
            raise E2EFailure(stage, "desktop_local_api_unreachable") from exc
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
            query = {"limit": "100", "direction": "asc"}
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


class DesktopScienceWorkflow:
    def __init__(
        self,
        api: LocalApi,
        *,
        host: str,
        port: int,
        user: str,
        host_key_algorithm: str,
        expected_host_key_fingerprint: str,
        codex_model: str,
        task_title: str,
        task_objective: str,
        poll_seconds: float,
        activation_timeout_seconds: float,
        run_timeout_seconds: float,
    ) -> None:
        self._api = api
        self._host = host
        self._port = port
        self._user = user
        self._host_key_algorithm = host_key_algorithm
        self._expected_host_key_fingerprint = expected_host_key_fingerprint
        self._codex_model = codex_model
        self._task_title = task_title
        self._task_objective = task_objective.rstrip() + CONTEXT_CANARY_INSTRUCTION
        self._poll_seconds = poll_seconds
        self._activation_timeout = activation_timeout_seconds
        self._run_timeout = run_timeout_seconds
        self._nonce = secrets.token_hex(12)
        self.profile_id: str | None = None
        self.project_id: str | None = None
        self._method_ids: dict[str, str] = {}
        self._active_run: dict[str, Any] | None = None

    def run(self) -> dict[str, object]:
        profile = self._create_and_confirm_profile()
        project = self._create_and_activate_project(profile)
        capabilities = self._select_and_activate_targets(project)
        project = self._get_project()
        validation = self._validate_project(project)
        if validation.get("registry_digest") != capabilities["registry_digest"]:
            raise E2EFailure("project_validation", "registry_digest_changed")

        first_run = self._create_run(project, ordinal=1)
        first_run = self._wait_run(first_run, ordinal=1)
        first = self._observe_session(first_run, ordinal=1)
        self._assert_successful_session(first, ordinal=1)

        current_project = self._get_project()
        second_run = self._create_run(current_project, ordinal=2)
        second_run = self._wait_run(second_run, ordinal=2)
        second = self._observe_session(second_run, ordinal=2)
        self._assert_successful_session(second, ordinal=2)
        reuse = self._assert_successor_reuse(first, second)

        return {
            "remote": {
                "host_sha256": _digest_text(self._host),
                "port": self._port,
                "user_sha256": _digest_text(self._user),
                "host_key_fingerprint_sha256": _digest_text(self._expected_host_key_fingerprint),
            },
            "project": {
                "project_id_sha256": _digest_text(self.project_id or ""),
                "execution_mode": "codex_subscription_transcript",
                "capture_mode": "transcript",
                "token_level_metrics_available": False,
                "target_ids": list(REQUIRED_TARGET_IDS),
                "method_ids": dict(sorted(self._method_ids.items())),
                "registry_digest": capabilities["registry_digest"],
                "validation_check_count": len(validation.get("checks", [])),
            },
            "sessions": [first.evidence, second.evidence],
            "reuse": reuse,
        }

    def cleanup(self) -> dict[str, bool]:
        run_cleanup = self._cancel_active_run()
        return {
            **run_cleanup,
            "desktop_disconnect_succeeded": self._disconnect(),
        }

    def _cancel_active_run(self) -> dict[str, bool]:
        outcome = {
            "active_run_cleanup_required": False,
            "active_run_cancel_requested": False,
            "active_run_cancelled": False,
            "active_run_cleanup_succeeded": True,
        }
        active = self._active_run
        if active is None:
            return outcome
        outcome["active_run_cleanup_required"] = True
        try:
            run_id = _text(active, "id", "cleanup_run_cancel")
            observed = self._api.request(
                "GET",
                f"/desktop/v1/runs/{run_id}",
                stage="cleanup_run_cancel",
            )
            assert observed is not None
            self._active_run = observed
            if observed.get("status") in TERMINAL_RUN_STATES:
                outcome["active_run_cleanup_required"] = False
                self._active_run = None
                return outcome
            cancelling = self._api.request(
                "POST",
                f"/desktop/v1/runs/{run_id}/cancel",
                stage="cleanup_run_cancel",
                headers={
                    "Idempotency-Key": self._idempotency(f"cancel-{run_id}"),
                    "If-Match": _etag(observed, "cleanup_run_cancel"),
                },
                expected_status=202,
            )
            assert cancelling is not None
            outcome["active_run_cancel_requested"] = True
            self._active_run = cancelling
            deadline = time.monotonic() + 120.0
            while cancelling.get("status") not in TERMINAL_RUN_STATES:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    outcome["active_run_cleanup_succeeded"] = False
                    return outcome
                time.sleep(min(self._poll_seconds, remaining, 2.0))
                cancelling = self._api.request(
                    "GET",
                    f"/desktop/v1/runs/{run_id}",
                    stage="cleanup_run_cancel",
                )
                assert cancelling is not None
                self._active_run = cancelling
            outcome["active_run_cancelled"] = cancelling.get("status") == "cancelled"
            outcome["active_run_cleanup_succeeded"] = outcome["active_run_cancelled"]
            self._active_run = None
            return outcome
        except BaseException:
            outcome["active_run_cleanup_succeeded"] = False
            return outcome

    def _disconnect(self) -> bool:
        if self.profile_id is None:
            return True
        try:
            profile = self._get_profile()
            operation = self._api.request(
                "POST",
                f"/desktop/v1/profiles/{self.profile_id}/disconnect",
                stage="cleanup_disconnect",
                headers={
                    "Idempotency-Key": self._idempotency("disconnect"),
                    "If-Match": _etag(profile, "cleanup_disconnect"),
                },
                expected_status=202,
            )
            assert operation is not None
            operation = self._wait_operation(
                operation,
                stage="cleanup_disconnect",
                timeout_seconds=60,
            )
            return operation.get("state") == "succeeded"
        except BaseException:
            return False

    def _create_and_confirm_profile(self) -> dict[str, Any]:
        profile = self._api.request(
            "POST",
            "/desktop/v1/profiles",
            stage="profile_create",
            headers={"Idempotency-Key": self._idempotency("profile-create")},
            body={
                "name": "OpenEvo real science E2E",
                "host": self._host,
                "port": self._port,
                "user": self._user,
                "authentication_kind": "ssh_agent",
                "proxy": {"http_url": None, "https_url": None, "no_proxy": []},
            },
            expected_status=201,
        )
        assert profile is not None
        self.profile_id = _text(profile, "profile_id", "profile_create")
        operation = self._api.request(
            "POST",
            f"/desktop/v1/profiles/{self.profile_id}/connect",
            stage="profile_connect",
            headers={
                "Idempotency-Key": self._idempotency("profile-connect"),
                "If-Match": _etag(profile, "profile_connect"),
            },
            expected_status=202,
        )
        assert operation is not None
        operation = self._wait_operation(
            operation,
            stage="profile_connect",
            timeout_seconds=60,
        )
        _require_operation_success(operation, "profile_connect")

        state = self._api.request("GET", "/desktop/v1/state", stage="host_key_review")
        assert state is not None
        review = state.get("core", {}).get("host_key_review")
        if not isinstance(review, dict):
            raise E2EFailure("host_key_review", "host_key_review_missing")
        if (
            review.get("algorithm") != self._host_key_algorithm
            or review.get("fingerprint") != self._expected_host_key_fingerprint
        ):
            raise E2EFailure("host_key_review", "host_key_identity_mismatch")

        profile = self._get_profile()
        operation = self._api.request(
            "POST",
            f"/desktop/v1/profiles/{self.profile_id}/host-key/accept",
            stage="host_key_accept",
            headers={
                "Idempotency-Key": self._idempotency("host-key-accept"),
                "If-Match": _etag(profile, "host_key_accept"),
            },
            body={
                "algorithm": self._host_key_algorithm,
                "fingerprint": self._expected_host_key_fingerprint,
            },
            expected_status=202,
        )
        assert operation is not None
        operation = self._wait_operation(
            operation,
            stage="host_key_accept",
            timeout_seconds=60,
        )
        _require_operation_success(operation, "host_key_accept")
        profile = self._get_profile()
        if profile.get("connection_state") != "connected":
            raise E2EFailure("host_key_accept", "profile_not_connected")
        return profile

    def _create_and_activate_project(self, profile: dict[str, Any]) -> dict[str, Any]:
        project = self._api.request(
            "POST",
            "/desktop/v1/projects",
            stage="project_create",
            headers={"Idempotency-Key": self._idempotency("project-create")},
            body={
                "name": "OpenEvo real science E2E",
                "profile_id": _text(profile, "profile_id", "project_create"),
                "task": {
                    "title": self._task_title,
                    "objective": self._task_objective,
                },
                "source": {"kind": "scratch", "display_name": "E2E scratch workspace"},
                "execution": {
                    "mode": "codex_subscription_transcript",
                    "capture_mode": "transcript",
                    "token_level_metrics_available": False,
                    "codex_model": self._codex_model,
                },
                "evolution": {
                    "targets": {
                        target_id: {
                            "enabled": False,
                            "method": None,
                            "config": {},
                        }
                        for target_id in REQUIRED_TARGET_IDS
                    }
                },
            },
            expected_status=201,
        )
        assert project is not None
        self.project_id = _text(project, "project_id", "project_create")
        return self._activate_project(project, stage="project_bootstrap_activate")

    def _activate_project(
        self,
        project: dict[str, Any],
        *,
        stage: str,
    ) -> dict[str, Any]:
        operation = self._api.request(
            "POST",
            f"/desktop/v1/projects/{self.project_id}/activate",
            stage=stage,
            headers={
                "Idempotency-Key": self._idempotency(stage),
                "If-Match": _etag(project, stage),
            },
            expected_status=202,
        )
        assert operation is not None
        operation = self._wait_operation(
            operation,
            stage=stage,
            timeout_seconds=self._activation_timeout,
        )
        _require_operation_success(operation, stage)
        project = self._get_project()
        remote = project.get("remote")
        if project.get("state") != "active" or not isinstance(remote, dict):
            raise E2EFailure(stage, "project_not_active")
        if remote.get("status") != "ready" or not isinstance(remote.get("active_revision"), dict):
            raise E2EFailure(stage, "remote_project_not_ready")
        return project

    def _select_and_activate_targets(self, project: dict[str, Any]) -> dict[str, Any]:
        capabilities = self._api.request(
            "GET",
            f"/desktop/v1/projects/{self.project_id}/capabilities",
            stage="project_capabilities",
        )
        assert capabilities is not None
        body = capabilities.get("capabilities")
        if not isinstance(body, dict) or not _is_sha256(body.get("registry_digest")):
            raise E2EFailure("project_capabilities", "invalid_registry_digest")
        targets = body.get("targets")
        if not isinstance(targets, list):
            raise E2EFailure("project_capabilities", "invalid_target_inventory")
        if capabilities.get("project_etag") != project.get("etag"):
            raise E2EFailure("project_capabilities", "project_etag_mismatch")
        target_map = {item.get("target_id"): item for item in targets if isinstance(item, dict)}
        selections: dict[str, dict[str, object]] = {}
        for target_id in REQUIRED_TARGET_IDS:
            target = target_map.get(target_id)
            if not isinstance(target, dict):
                raise E2EFailure("project_capabilities", "required_target_not_supported")
            if target_id == "agent_system":
                resolvers = target.get("selection_resolvers")
                auto_resolvers = (
                    [
                        resolver
                        for resolver in resolvers
                        if isinstance(resolver, dict) and resolver.get("selection_value") == "auto"
                    ]
                    if isinstance(resolvers, list)
                    else []
                )
                if len(auto_resolvers) != 1:
                    raise E2EFailure("project_capabilities", "agent_system_auto_not_supported")
                resolved_methods = auto_resolvers[0].get("resolved_methods")
                accepted_methods = target.get("accepted_methods")
                accepted_by_id = (
                    {
                        method.get("method_id"): method
                        for method in accepted_methods
                        if isinstance(method, dict) and isinstance(method.get("method_id"), str)
                    }
                    if isinstance(accepted_methods, list)
                    else {}
                )
                if (
                    not isinstance(resolved_methods, list)
                    or not resolved_methods
                    or any(
                        not isinstance(method, dict)
                        or not isinstance(method.get("method_id"), str)
                        or not _is_sha256(method.get("implementation_identity_digest"))
                        or not isinstance(method.get("support"), dict)
                        or method["support"].get("overall") != "supported"
                        or not isinstance(
                            (accepted_method := accepted_by_id.get(method["method_id"])),
                            dict,
                        )
                        or accepted_method.get("implementation_identity_digest")
                        != method["implementation_identity_digest"]
                        or accepted_method.get("support") != method["support"]
                        for method in resolved_methods
                    )
                ):
                    raise E2EFailure("project_capabilities", "agent_system_auto_not_supported")
                self._method_ids[target_id] = "auto"
                selections[target_id] = {
                    "enabled": True,
                    "method": "auto",
                    "config": {},
                }
                continue
            visible_methods = target.get("methods")
            if not isinstance(visible_methods, list):
                raise E2EFailure("project_capabilities", "invalid_method_inventory")
            supported = [
                method
                for method in visible_methods
                if isinstance(method, dict)
                and isinstance(method.get("support"), dict)
                and method["support"].get("overall") == "supported"
                and isinstance(method.get("method_id"), str)
                and _is_sha256(method.get("implementation_identity_digest"))
            ]
            stable = [method for method in supported if method.get("maturity") == "stable"]
            effective_default = target.get("effective_default_method_id")
            selected = next(
                (method for method in supported if method["method_id"] == effective_default),
                None,
            )
            if selected is None:
                selected = min(
                    stable or supported,
                    key=lambda method: method["method_id"],
                    default=None,
                )
            if selected is None:
                raise E2EFailure("project_capabilities", "stable_method_not_supported")
            try:
                default_config = json.loads(selected["default_config_json"])
            except (KeyError, TypeError, json.JSONDecodeError) as exc:
                raise E2EFailure("project_capabilities", "invalid_method_default_config") from exc
            if not isinstance(default_config, dict):
                raise E2EFailure("project_capabilities", "invalid_method_default_config")
            method_id = str(selected["method_id"])
            self._method_ids[target_id] = method_id
            selections[target_id] = {
                "enabled": True,
                "method": method_id,
                "config": default_config,
            }
        patched = self._api.request(
            "PATCH",
            f"/desktop/v1/projects/{self.project_id}",
            stage="project_target_selection",
            headers={"If-Match": _etag(project, "project_target_selection")},
            body={"evolution": {"targets": selections}},
        )
        assert patched is not None
        activated = self._activate_project(patched, stage="project_target_activate")
        if _nested(activated, "remote", "registry_digest") != body["registry_digest"]:
            raise E2EFailure("project_target_activate", "registry_digest_changed")
        return body

    def _validate_project(self, project: dict[str, Any]) -> dict[str, Any]:
        validation = self._api.request(
            "POST",
            f"/desktop/v1/projects/{self.project_id}/validate",
            stage="project_validation",
            headers={
                "Idempotency-Key": self._idempotency("project-validation"),
                "If-Match": _etag(project, "project_validation"),
            },
        )
        assert validation is not None
        if validation.get("valid") is not True or not _is_sha256(
            validation.get("registry_digest")
        ):
            raise E2EFailure("project_validation", "project_invalid")
        if not isinstance(validation.get("checks"), list):
            raise E2EFailure("project_validation", "invalid_validation_checks")
        return validation

    def _create_run(self, project: dict[str, Any], *, ordinal: int) -> dict[str, Any]:
        run = self._api.request(
            "POST",
            "/desktop/v1/runs",
            stage=f"session_{ordinal}_create",
            headers={
                "Idempotency-Key": self._idempotency(f"session-{ordinal}-create"),
                "If-Match": _etag(project, f"session_{ordinal}_create"),
            },
            body={"project_id": self.project_id or ""},
            expected_status=202,
        )
        assert run is not None
        _text(run, "id", f"session_{ordinal}_create")
        self._active_run = run
        return run

    def _wait_run(self, run: dict[str, Any], *, ordinal: int) -> dict[str, Any]:
        run_id = _text(run, "id", f"session_{ordinal}_poll")
        deadline = time.monotonic() + self._run_timeout
        while run.get("status") not in TERMINAL_RUN_STATES:
            if time.monotonic() >= deadline:
                raise E2EFailure(f"session_{ordinal}_poll", "run_timeout")
            time.sleep(self._poll_seconds)
            observed = self._api.request(
                "GET",
                f"/desktop/v1/runs/{run_id}",
                stage=f"session_{ordinal}_poll",
            )
            assert observed is not None
            run = observed
            self._active_run = run
        self._active_run = None
        return run

    def _observe_session(self, run: dict[str, Any], *, ordinal: int) -> SessionObservation:
        run_id = _text(run, "id", f"session_{ordinal}_observe")
        timeline = self._api.page(
            f"/desktop/v1/runs/{run_id}/timeline",
            stage=f"session_{ordinal}_timeline",
        )
        logs = self._api.page(
            f"/desktop/v1/runs/{run_id}/logs",
            stage=f"session_{ordinal}_logs",
        )
        artifacts = self._api.page(
            f"/desktop/v1/runs/{run_id}/artifacts",
            stage=f"session_{ordinal}_artifacts",
        )
        context = self._api.request(
            "GET",
            f"/desktop/v1/runs/{run_id}/context",
            stage=f"session_{ordinal}_context",
        )
        assert context is not None
        inspections: dict[str, dict[str, object]] = {}
        document_sha256_by_target: dict[str, str] = {}
        for target_id in REQUIRED_TARGET_IDS:
            target_artifacts = [
                artifact for artifact in artifacts if artifact.get("target_id") == target_id
            ]
            if len(target_artifacts) != 1:
                continue
            target_artifact = target_artifacts[0]
            artifact_id = _text(
                target_artifact,
                "id",
                f"session_{ordinal}_artifact_inspection",
            )
            detail = self._api.request(
                "GET",
                f"/desktop/v1/artifacts/{artifact_id}",
                stage=f"session_{ordinal}_artifact_detail",
            )
            content = self._api.request(
                "GET",
                f"/desktop/v1/artifacts/{artifact_id}/content",
                stage=f"session_{ordinal}_artifact_content",
            )
            assert detail is not None and content is not None
            if detail.get("id") != artifact_id or content.get("artifact_id") != artifact_id:
                raise E2EFailure(
                    f"session_{ordinal}_artifact_content",
                    "artifact_identity_mismatch",
                )
            document_sha256 = _required_target_document_sha256(
                content,
                target_id=target_id,
                stage=f"session_{ordinal}_artifact_content",
            )
            document_sha256_by_target[target_id] = document_sha256
            inspections[target_id] = {
                "artifact_id_sha256": _digest_text(artifact_id),
                "document_count": len(content.get("documents", []))
                if isinstance(content.get("documents"), list)
                else -1,
                "total_documents": content.get("total_documents"),
                "total_utf8_bytes": content.get("total_utf8_bytes"),
                "truncated": content.get("truncated"),
                "runtime_document_sha256": document_sha256,
            }

        receipt_digests = [
            message.removeprefix(RUNTIME_CONTEXT_RECEIPT_PREFIX)
            for item in logs
            if isinstance((message := item.get("message")), str)
            and message.startswith(RUNTIME_CONTEXT_RECEIPT_PREFIX)
            and _is_sha256(message.removeprefix(RUNTIME_CONTEXT_RECEIPT_PREFIX))
        ]
        if len(receipt_digests) > 1:
            raise E2EFailure(f"session_{ordinal}_logs", "multiple_runtime_context_receipts")
        runtime_context_receipt_sha256 = receipt_digests[0] if receipt_digests else None

        evidence = {
            "ordinal": ordinal,
            "run_id_sha256": _digest_text(run_id),
            "status": run.get("status"),
            "required_relation": _nested(run, "required_revision", "relation"),
            "required_revision": _revision_evidence(
                _nested(run, "required_revision", "revision"),
                f"session_{ordinal}_required_revision",
            ),
            "pinned_revision": _revision_evidence(
                run.get("pinned_revision"),
                f"session_{ordinal}_pinned_revision",
            ),
            "timeline": _event_inventory(timeline, ("phase", "status")),
            "logs": _event_inventory(logs, ("stream", "level")),
            "artifacts": [
                _artifact_evidence(item, f"session_{ordinal}_artifacts")
                for item in artifacts[:MAX_EVIDENCE_ITEMS]
            ],
            "artifact_count": len(artifacts),
            "artifact_evidence_truncated": len(artifacts) > MAX_EVIDENCE_ITEMS,
            "artifact_inspections": inspections,
            "runtime_context_receipt_sha256": runtime_context_receipt_sha256,
            "context": {
                "status": context.get("status"),
                "capture_mode": context.get("capture_mode"),
                "token_level_metrics_available": context.get("token_level_metrics_available"),
                "artifact_count": len(context.get("artifacts", []))
                if isinstance(context.get("artifacts"), list)
                else -1,
                "adapter_count": len(context.get("adapters", []))
                if isinstance(context.get("adapters"), list)
                else -1,
            },
        }
        return SessionObservation(
            evidence,
            run,
            context,
            tuple(artifacts),
            document_sha256_by_target,
            runtime_context_receipt_sha256,
        )

    def _assert_successful_session(
        self,
        observation: SessionObservation,
        *,
        ordinal: int,
    ) -> None:
        run = observation.run
        if run.get("status") != "succeeded":
            error = run.get("current_error")
            code = error.get("code") if isinstance(error, dict) else "run_not_succeeded"
            raise E2EFailure(f"session_{ordinal}_terminal", _safe_code(code))
        if not isinstance(run.get("pinned_revision"), dict):
            raise E2EFailure(f"session_{ordinal}_terminal", "revision_not_pinned")
        if (
            observation.context.get("capture_mode") != "transcript"
            or observation.context.get("token_level_metrics_available") is not False
        ):
            raise E2EFailure(f"session_{ordinal}_context", "capture_contract_mismatch")
        timeline_evidence = observation.evidence.get("timeline")
        phases = (
            set(timeline_evidence.get("phase_values", []))
            if isinstance(timeline_evidence, dict)
            else set()
        )
        if not {"execution", "evolution", "revision", "terminal"}.issubset(phases):
            raise E2EFailure(f"session_{ordinal}_timeline", "terminal_evidence_missing")
        logs_evidence = observation.evidence.get("logs")
        if not isinstance(logs_evidence, dict) or logs_evidence.get("count", 0) < 1:
            raise E2EFailure(f"session_{ordinal}_logs", "logs_missing")
        artifacts_by_target = {
            target_id: [
                artifact
                for artifact in observation.artifacts
                if artifact.get("target_id") == target_id
            ]
            for target_id in REQUIRED_TARGET_IDS
        }
        if any(len(items) != 1 for items in artifacts_by_target.values()):
            raise E2EFailure(
                f"session_{ordinal}_artifacts", "required_target_artifact_set_invalid"
            )
        if set(observation.document_sha256_by_target) != set(REQUIRED_TARGET_IDS):
            raise E2EFailure(f"session_{ordinal}_artifacts", "artifact_inspection_incomplete")
        revisions = {
            json.dumps(artifact.get("produced_revision"), sort_keys=True)
            for items in artifacts_by_target.values()
            for artifact in items
        }
        if len(revisions) != 1:
            raise E2EFailure(f"session_{ordinal}_artifacts", "output_revision_inconsistent")

    def _assert_successor_reuse(
        self,
        first: SessionObservation,
        second: SessionObservation,
    ) -> dict[str, object]:
        first_pin = _revision(first.run.get("pinned_revision"), "reuse_first_pin")
        second_pin = _revision(second.run.get("pinned_revision"), "reuse_second_pin")
        first_outputs = {
            target_id: next(
                (
                    artifact
                    for artifact in first.artifacts
                    if artifact.get("target_id") == target_id
                ),
                None,
            )
            for target_id in REQUIRED_TARGET_IDS
        }
        if any(artifact is None for artifact in first_outputs.values()):
            raise E2EFailure("successor_reuse", "first_session_output_missing")
        successor = _revision(
            first_outputs[REQUIRED_TARGET_IDS[0]].get("produced_revision"),
            "reuse_successor_revision",
        )
        if any(
            artifact.get("produced_revision") != successor for artifact in first_outputs.values()
        ):
            raise E2EFailure("successor_reuse", "output_revision_inconsistent")
        if any(
            artifact.get("selected") is not True or artifact.get("release_enabled") is not True
            for artifact in first_outputs.values()
        ):
            raise E2EFailure("successor_reuse", "successor_artifact_not_selected")
        if successor["generation"] != first_pin["generation"] + 1:
            raise E2EFailure("successor_reuse", "successor_generation_invalid")
        required = _revision(
            _nested(second.run, "required_revision", "revision"),
            "reuse_second_required",
        )
        if second_pin != successor or required != successor:
            raise E2EFailure("successor_reuse", "second_session_did_not_pin_successor")
        first_artifact_ids = {
            target_id: _text(artifact, "id", "successor_reuse")
            for target_id, artifact in first_outputs.items()
        }
        first_context_artifacts = first.context.get("artifacts")
        if not isinstance(first_context_artifacts, list):
            raise E2EFailure("successor_reuse", "first_context_invalid")
        own_output_ids = set(first_artifact_ids.values())
        if any(
            isinstance(item, dict)
            and (item.get("artifact_id") in own_output_ids or item.get("revision") == successor)
            for item in first_context_artifacts
        ):
            raise E2EFailure("successor_reuse", "first_session_consumed_own_successor")
        context_artifacts = second.context.get("artifacts")
        if not isinstance(context_artifacts, list):
            raise E2EFailure("successor_reuse", "second_context_invalid")
        reused = {
            target_id: item
            for target_id, artifact_id in first_artifact_ids.items()
            for item in context_artifacts
            if isinstance(item, dict)
            and item.get("artifact_id") == artifact_id
            and item.get("target_id") == target_id
            and item.get("artifact_type") == target_id
            and item.get("revision") == successor
        }
        if set(reused) != set(REQUIRED_TARGET_IDS):
            raise E2EFailure("successor_reuse", "first_artifact_not_in_second_context")
        second_outputs = {
            target_id: next(
                (
                    artifact
                    for artifact in second.artifacts
                    if artifact.get("target_id") == target_id
                ),
                None,
            )
            for target_id in REQUIRED_TARGET_IDS
        }
        for target_id, artifact in second_outputs.items():
            lineage = artifact.get("lineage") if isinstance(artifact, dict) else None
            source_artifact_ids = (
                lineage.get("source_artifact_ids") if isinstance(lineage, dict) else None
            )
            if (
                not isinstance(source_artifact_ids, list)
                or first_artifact_ids[target_id] not in source_artifact_ids
            ):
                raise E2EFailure("successor_reuse", "successor_lineage_missing")
        receipt_sha256 = second.runtime_context_receipt_sha256
        if not _is_sha256(receipt_sha256):
            raise E2EFailure("successor_reuse", "runtime_context_receipt_mismatch")
        return {
            "successor_generation_delta": 1,
            "session_1_excluded_own_successor": True,
            "session_2_pinned_session_1_successor": True,
            "session_1_artifacts_reused": True,
            "session_2_runtime_injection_verified": True,
            "session_2_harness_context_consumed": True,
            "session_2_lineage_verified": True,
            "runtime_context_receipt_sha256": receipt_sha256,
            "reused_artifact_count": len(reused),
            "successor_revision": _revision_evidence(successor, "successor_reuse"),
        }

    def _wait_operation(
        self,
        operation: dict[str, Any],
        *,
        stage: str,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        operation_id = _text(operation, "operation_id", stage)
        deadline = time.monotonic() + timeout_seconds
        while operation.get("state") not in TERMINAL_OPERATION_STATES:
            if time.monotonic() >= deadline:
                raise E2EFailure(stage, "operation_timeout")
            time.sleep(self._poll_seconds)
            observed = self._api.request(
                "GET",
                f"/desktop/v1/operations/{operation_id}",
                stage=stage,
            )
            assert observed is not None
            operation = observed
        return operation

    def _get_profile(self) -> dict[str, Any]:
        profile = self._api.request(
            "GET",
            f"/desktop/v1/profiles/{self.profile_id}",
            stage="profile_read",
        )
        assert profile is not None
        return profile

    def _get_project(self) -> dict[str, Any]:
        project = self._api.request(
            "GET",
            f"/desktop/v1/projects/{self.project_id}",
            stage="project_read",
        )
        assert project is not None
        return project

    def _idempotency(self, action: str) -> str:
        return f"real-science-e2e-{self._nonce}-{action}"


def _build_assets(
    root: Path,
    core_wheel: Path,
    framework_lock: Path,
    managed_runtime_archive: Path,
    daemon_bundle: Path,
    daemon_manifest: Path,
    *,
    timeout_seconds: float,
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
            env=_build_environment(),
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
        )
        if returncode != 0:
            raise E2EFailure("release_assets", "sidecar_build_failed")
        build_log.seek(0)
        lines = build_log.read().decode("utf-8", errors="replace").splitlines()
    if not lines:
        raise E2EFailure("release_assets", "sidecar_build_output_missing")
    sidecar = Path(lines[-1].strip())
    return _inspect_release_assets(
        sidecar,
        core_wheel,
        framework_lock,
        managed_runtime_archive,
        daemon_bundle,
        daemon_manifest,
    )


def _inspect_release_assets(
    sidecar: Path,
    wheel: Path,
    lock: Path,
    managed_runtime_archive: Path,
    daemon_bundle: Path,
    daemon_manifest: Path,
) -> ReleaseAssets:
    inputs = (
        (sidecar, "packaged_sidecar_invalid"),
        (wheel, "core_wheel_invalid"),
        (lock, "framework_lock_invalid"),
        (managed_runtime_archive, "managed_runtime_archive_invalid"),
        (daemon_bundle, "daemon_bundle_invalid"),
        (daemon_manifest, "daemon_manifest_invalid"),
    )
    for item, code in inputs:
        if item.is_symlink() or not item.is_file() or not stat.S_ISREG(item.stat().st_mode):
            raise E2EFailure("release_assets", code)
    if not os.access(sidecar, os.X_OK):
        raise E2EFailure("release_assets", "packaged_sidecar_not_executable")
    authorities: list[HeldReleaseAsset] = []
    try:
        authorities.extend(HeldReleaseAsset.open(path) for path, _code in inputs)
        authority_by_path = {authority.path: authority for authority in authorities}
        name, version, wheel_digest = _validate_wheel_lock(wheel, lock)
        if wheel_digest != authority_by_path[wheel].sha256:
            raise E2EFailure("release_assets", "framework_lock_wheel_mismatch")
        builder = _load_sidecar_builder()
        runtime_size, runtime_digest = builder._validate_managed_runtime_archive(
            managed_runtime_archive
        )
        if (
            runtime_size != authority_by_path[managed_runtime_archive].byte_size
            or runtime_digest != authority_by_path[managed_runtime_archive].sha256
        ):
            raise E2EFailure("release_assets", "managed_runtime_archive_changed")
        builder._validate_fd_bound_bootloader(sidecar)
        builder._validate_embedded_core_wheel(sidecar, wheel)
        builder._validate_embedded_core_framework_lock(
            sidecar,
            wheel,
            lock,
            version=version,
        )
        builder._validate_embedded_managed_runtime_archive(
            sidecar,
            managed_runtime_archive,
        )
        bundle_source, manifest_source, daemon_identity = builder._open_daemon_release_input_pair(
            daemon_bundle,
            daemon_manifest,
            repo=REPOSITORY_ROOT,
        )
        try:
            builder._validate_daemon_manifest_core(
                daemon_identity,
                wheel=wheel,
                framework_lock=lock,
                version=version,
            )
            builder._validate_embedded_daemon_release_inputs(
                sidecar,
                bundle_source,
                manifest_source,
            )
        finally:
            manifest_source.close()
            bundle_source.close()
        for authority in authorities:
            authority.verify_unchanged()
        evidence = {
            "sidecar": authority_by_path[sidecar].evidence(),
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
            "managed_runtime_archive": {
                "sha256": runtime_digest,
                "byte_size": runtime_size,
            },
            "daemon_bundle": authority_by_path[daemon_bundle].evidence(),
            "daemon_manifest": authority_by_path[daemon_manifest].evidence(),
            "exact_embedded_assets_verified": True,
        }
    except E2EFailure:
        for authority in authorities:
            authority.close()
        raise
    except Exception as exc:
        for authority in authorities:
            authority.close()
        raise E2EFailure("release_assets", "packaged_assets_not_exact") from exc
    return ReleaseAssets(
        sidecar=sidecar,
        wheel=wheel,
        framework_lock=lock,
        managed_runtime_archive=managed_runtime_archive,
        daemon_bundle=daemon_bundle,
        daemon_manifest=daemon_manifest,
        authorities=tuple(authorities),
        evidence=evidence,
    )


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


def _launch_sidecar(assets: ReleaseAssets, root: Path) -> NativeSidecar:
    if os.name != "posix":
        raise E2EFailure("native_launch", "posix_process_boundary_required")
    root.mkdir(parents=True, exist_ok=True)
    launch_path = root / "openevo-desktop-sidecar"
    assets.authority(assets.sidecar).copy_to(launch_path, executable=True)
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
        "--desktop-config-root",
        str(root / "state"),
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
    )
    try:
        _wait_sidecar_ready(native)
    except BaseException:
        native.terminate()
        raise
    return native


def _wait_sidecar_ready(native: NativeSidecar) -> None:
    api = LocalApi(native.base_url, native.credentials.session_token)
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if _process_exited_without_reap(native.process):
            raise E2EFailure("native_launch", "sidecar_exited_before_readiness")
        challenge = secrets.token_hex(32)
        try:
            health = api.request(
                "GET",
                "/health",
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
    if (
        not isinstance(release_contract, dict)
        or set(release_contract)
        != {
            "accepted_openapi_digests",
            "allowed_provider_kinds",
            "required_feature_flags",
            "schema_version",
        }
        or release_contract.get("schema_version") != "1"
        or release_contract.get("allowed_provider_kinds") != ["desktop_sidecar"]
        or not isinstance(release_contract.get("accepted_openapi_digests"), list)
        or len(release_contract["accepted_openapi_digests"]) != 1
        or not _is_sha256(release_contract["accepted_openapi_digests"][0])
        or not isinstance(release_contract.get("required_feature_flags"), list)
        or not all(
            isinstance(flag, str) and flag for flag in release_contract["required_feature_flags"]
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
        "openapi_sha256",
        "build_version",
        "source_commit",
        "build_channel",
        "provider_kind",
        "feature_flags",
    }:
        raise E2EFailure("desktop_version", "desktop_contract_invalid")
    required = {
        "schema_version": "1",
        "api_name": "openevo-desktop-local-api",
        "preferred_major": 1,
        "provider_kind": "desktop_sidecar",
        "build_channel": "release",
    }
    if any(version.get(key) != value for key, value in required.items()):
        raise E2EFailure("desktop_version", "not_release_desktop_sidecar")
    if (
        version.get("supported_majors") != [1]
        or version.get("openapi_sha256") != release_contract["accepted_openapi_digests"][0]
        or version.get("feature_flags") != release_contract["required_feature_flags"]
    ):
        raise E2EFailure("desktop_version", "desktop_contract_invalid")
    source_commit = version.get("source_commit")
    build_version = version.get("build_version")
    if (
        not isinstance(source_commit, str)
        or re.fullmatch(r"[0-9a-f]{7,40}", source_commit) is None
        or not isinstance(build_version, str)
        or not 0 < len(build_version) <= 512
        or any(character in build_version for character in ("\x00", "\r", "\n"))
        or build_version.startswith("/")
        or ABSOLUTE_WINDOWS_PATH.match(build_version)
    ):
        raise E2EFailure("desktop_version", "desktop_build_identity_invalid")
    api.request(
        "GET",
        "/openevo-api/desktop/shell",
        stage="desktop_legacy_route",
        expected_status=404,
        authenticated=False,
    )
    api.request(
        "GET",
        "/openevo-native/session",
        stage="desktop_session_probe",
        expected_status=204,
    )
    api.request(
        "GET",
        "/openevo-native/session",
        stage="desktop_session_probe_unauthenticated",
        expected_status=403,
        authenticated=False,
        expected_empty_body=True,
    )
    return {
        "source_commit": source_commit,
        "build_version": build_version,
        "openapi_sha256": version["openapi_sha256"],
        "provider_kind": version["provider_kind"],
        "build_channel": version["build_channel"],
        "feature_flags": sorted(version.get("feature_flags", [])),
        "legacy_route_rejected": True,
        "authenticated_session_probe": True,
        "unauthenticated_session_rejected": True,
    }


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
    try:
        contract_payload = json.loads(contract.read_text(encoding="utf-8"))
        launcher_text = launcher.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise E2EFailure("structural_check", "release_sources_unavailable") from exc
    if (
        set(contract_payload)
        != {
            "accepted_openapi_digests",
            "allowed_provider_kinds",
            "required_feature_flags",
            "schema_version",
        }
        or contract_payload.get("schema_version") != "1"
        or contract_payload.get("allowed_provider_kinds") != ["desktop_sidecar"]
        or len(contract_payload.get("accepted_openapi_digests", [])) != 1
        or not _is_sha256(contract_payload["accepted_openapi_digests"][0])
        or not contract_payload.get("required_feature_flags")
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
    return {
        "artifact_id_sha256": _digest_text(_text(artifact, "id", stage)),
        "artifact_type": artifact.get("artifact_type"),
        "target_id": artifact.get("target_id"),
        "content_sha256": artifact.get("content_sha256"),
        "byte_size": artifact.get("byte_size"),
        "selected": artifact.get("selected"),
        "promoted": artifact.get("promoted"),
        "release_enabled": artifact.get("release_enabled"),
        "source_artifact_count": len(source_artifact_ids)
        if isinstance(source_artifact_ids, list)
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
    ]
    if len(complete) != len(documents):
        raise E2EFailure(stage, "artifact_document_not_complete")
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
    try:
        result = os.waitid(
            os.P_PID,
            process.pid,
            os.WEXITED | os.WNOHANG | os.WNOWAIT,
        )
    except ChildProcessError:
        return True
    return result is not None


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
) -> int:
    if not _wait_exited_without_reap(process, timeout_seconds=timeout_seconds):
        _terminate_process_group(
            process,
            process_group_id=process_group_id,
            graceful_timeout_seconds=0,
        )
        raise E2EFailure("release_assets", "sidecar_build_timeout")
    if not _terminate_process_group(
        process,
        process_group_id=process_group_id,
        graceful_timeout_seconds=0,
    ):
        raise E2EFailure("release_assets", "build_process_group_cleanup_failed")
    if process.returncode is None:
        raise E2EFailure("release_assets", "build_process_status_missing")
    return process.returncode


def _terminate_process_group(
    process: subprocess.Popen[Any],
    *,
    process_group_id: int,
    graceful_timeout_seconds: float,
) -> bool:
    if process_group_id <= 0 or process_group_id != process.pid or process.returncode is not None:
        return False
    try:
        os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
    except ChildProcessError:
        return False
    graceful = _process_exited_without_reap(process)
    if not graceful:
        try:
            os.killpg(process_group_id, signal.SIGTERM)
        except ProcessLookupError:
            graceful = True
        else:
            graceful = _wait_exited_without_reap(
                process,
                timeout_seconds=graceful_timeout_seconds,
            )
    # The unreaped group leader keeps the captured PGID authoritative while
    # any descendants are force-closed.
    try:
        os.killpg(process_group_id, signal.SIGKILL)
    except ProcessLookupError:
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


def _validate_runtime_arguments(args: argparse.Namespace) -> None:
    if args.structural_check:
        return
    if (args.core_wheel is None) != (args.framework_lock is None):
        raise E2EFailure("arguments", "core_release_pair_required")
    if args.core_wheel is None or args.framework_lock is None:
        raise E2EFailure("arguments", "core_release_pair_required")
    if args.sidecar is not None and (
        args.core_wheel is None or args.framework_lock is None
    ):
        raise E2EFailure("arguments", "release_asset_triplet_required")
    if args.daemon_bundle is None or args.daemon_manifest is None:
        raise E2EFailure("arguments", "daemon_release_pair_required")
    if args.managed_runtime_archive is None:
        raise E2EFailure("arguments", "managed_runtime_archive_required")
    if not args.host or not args.user or not args.expected_host_key_fingerprint:
        raise E2EFailure("arguments", "remote_identity_required")
    if not 1 <= args.port <= 65_535:
        raise E2EFailure("arguments", "remote_port_invalid")
    if HOST_KEY_PATTERN.fullmatch(args.expected_host_key_fingerprint) is None:
        raise E2EFailure("arguments", "host_key_fingerprint_invalid")
    if not os.environ.get("SSH_AUTH_SOCK"):
        raise E2EFailure("arguments", "ssh_agent_unavailable")


def _positive_finite_seconds(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a finite positive number") from exc
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("must be a finite positive number")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host")
    parser.add_argument("--port", type=int, default=22)
    parser.add_argument("--user")
    parser.add_argument(
        "--expected-host-key-fingerprint",
        help="Exact SHA256 host-key fingerprint reviewed out of band.",
    )
    parser.add_argument(
        "--host-key-algorithm",
        choices=("ssh-ed25519", "ecdsa-sha2-nistp256", "rsa-sha2-512"),
        default="ssh-ed25519",
    )
    parser.add_argument("--sidecar", type=Path)
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
    parser.add_argument("--codex-model", default="gpt-5.3-codex-spark")
    parser.add_argument("--task-title", default="Release Desktop science E2E")
    parser.add_argument(
        "--task-objective",
        default=(
            "Inspect the scratch workspace, record one concise scientific observation, "
            "and complete without requesting user input."
        ),
    )
    parser.add_argument("--poll-seconds", type=_positive_finite_seconds, default=2.0)
    parser.add_argument(
        "--activation-timeout-seconds",
        type=_positive_finite_seconds,
        default=1200.0,
    )
    parser.add_argument(
        "--run-timeout-seconds",
        type=_positive_finite_seconds,
        default=7200.0,
    )
    parser.add_argument(
        "--build-timeout-seconds",
        type=_positive_finite_seconds,
        default=1800.0,
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

    signal.signal(signal.SIGTERM, interrupt_for_cleanup)

    started_at = _utc_now()
    evidence: dict[str, object] = {
        "schema_version": "1",
        "kind": "openevo_desktop_real_science_e2e",
        "issue": 163,
        "real_process_boundary": True,
        "outcome": "failed",
        "started_at": started_at,
    }
    private_values = [os.environ.get("SSH_AUTH_SOCK", "")]
    native: NativeSidecar | None = None
    assets: ReleaseAssets | None = None
    workflow: DesktopScienceWorkflow | None = None
    cleanup = {
        "active_run_cleanup_required": False,
        "active_run_cancel_requested": False,
        "active_run_cancelled": False,
        "active_run_cleanup_succeeded": True,
        "desktop_disconnect_succeeded": False,
        "sidecar_shutdown_succeeded": False,
        "core_ownership_release_requested": False,
    }
    exit_code = 1
    evidence_write_failed = False
    with TemporaryDirectory(prefix="openevo-desktop-real-e2e-") as temporary:
        root = Path(temporary)
        try:
            if args.sidecar is None:
                assets = _build_assets(
                    root / "build",
                    args.core_wheel,
                    args.framework_lock,
                    args.managed_runtime_archive,
                    args.daemon_bundle,
                    args.daemon_manifest,
                    timeout_seconds=args.build_timeout_seconds,
                )
            else:
                assets = _inspect_release_assets(
                    args.sidecar,
                    args.core_wheel,
                    args.framework_lock,
                    args.managed_runtime_archive,
                    args.daemon_bundle,
                    args.daemon_manifest,
                )
            evidence["release_assets"] = assets.evidence
            native = _launch_sidecar(assets, root / "native")
            private_values.extend(native.credentials.private_values())
            api = LocalApi(native.base_url, native.credentials.session_token)
            evidence["desktop"] = _release_identity(api)
            workflow = DesktopScienceWorkflow(
                api,
                host=args.host,
                port=args.port,
                user=args.user,
                host_key_algorithm=args.host_key_algorithm,
                expected_host_key_fingerprint=args.expected_host_key_fingerprint,
                codex_model=args.codex_model,
                task_title=args.task_title,
                task_objective=args.task_objective,
                poll_seconds=args.poll_seconds,
                activation_timeout_seconds=args.activation_timeout_seconds,
                run_timeout_seconds=args.run_timeout_seconds,
            )
            evidence.update(workflow.run())
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
            if workflow is not None:
                cleanup.update(workflow.cleanup())
            if native is not None:
                cleanup["core_ownership_release_requested"] = True
                cleanup["sidecar_shutdown_succeeded"] = native.terminate()
            if assets is not None:
                assets.close()
            evidence["cleanup"] = cleanup
            evidence["finished_at"] = _utc_now()
            cleanup_complete = (
                cleanup["active_run_cleanup_succeeded"]
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
        print("Desktop real-science E2E passed; bounded evidence written.")
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
