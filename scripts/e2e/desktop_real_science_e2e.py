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
import os
from pathlib import Path
import re
import secrets
import shutil
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
class ReleaseAssets:
    sidecar: Path
    wheel: Path
    framework_lock: Path
    evidence: dict[str, object]


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
    base_url: str
    credentials: NativeCredentials = field(repr=False)
    process_log: BinaryIO = field(repr=False)

    def terminate(self) -> bool:
        graceful = True
        if self.process.poll() is None:
            try:
                os.killpg(self.process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                self.process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                graceful = False
                try:
                    os.killpg(self.process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                self.process.wait(timeout=10)
        self.process_log.close()
        return graceful and self.process.poll() is not None


@dataclass(frozen=True)
class SessionObservation:
    evidence: dict[str, object]
    run: dict[str, Any]
    context: dict[str, Any]
    artifacts: tuple[dict[str, Any], ...]


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
        target_id: str,
        method_id: str,
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
        self._target_id = target_id
        self._method_id = method_id
        self._task_title = task_title
        self._task_objective = task_objective
        self._poll_seconds = poll_seconds
        self._activation_timeout = activation_timeout_seconds
        self._run_timeout = run_timeout_seconds
        self._nonce = secrets.token_hex(12)
        self.profile_id: str | None = None
        self.project_id: str | None = None

    def run(self) -> dict[str, object]:
        profile = self._create_and_confirm_profile()
        project = self._create_and_activate_project(profile)
        capabilities = self._assert_supported_target(project)
        validation = self._validate_project(project)

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
                "host_key_fingerprint_sha256": _digest_text(
                    self._expected_host_key_fingerprint
                ),
            },
            "project": {
                "project_id_sha256": _digest_text(self.project_id or ""),
                "execution_mode": "codex_subscription_transcript",
                "capture_mode": "transcript",
                "token_level_metrics_available": False,
                "target_id": self._target_id,
                "method_id": self._method_id,
                "registry_digest": capabilities["registry_digest"],
                "validation_check_count": len(validation.get("checks", [])),
            },
            "sessions": [first.evidence, second.evidence],
            "reuse": reuse,
        }

    def cleanup(self) -> bool:
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
        except Exception:
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
                        self._target_id: {
                            "enabled": True,
                            "method": self._method_id,
                            "config": {},
                        }
                    }
                },
            },
            expected_status=201,
        )
        assert project is not None
        self.project_id = _text(project, "project_id", "project_create")
        operation = self._api.request(
            "POST",
            f"/desktop/v1/projects/{self.project_id}/activate",
            stage="project_activate",
            headers={
                "Idempotency-Key": self._idempotency("project-activate"),
                "If-Match": _etag(project, "project_activate"),
            },
            expected_status=202,
        )
        assert operation is not None
        operation = self._wait_operation(
            operation,
            stage="project_activate",
            timeout_seconds=self._activation_timeout,
        )
        _require_operation_success(operation, "project_activate")
        project = self._get_project()
        remote = project.get("remote")
        if project.get("state") != "active" or not isinstance(remote, dict):
            raise E2EFailure("project_activate", "project_not_active")
        if remote.get("status") != "ready" or not isinstance(
            remote.get("active_revision"), dict
        ):
            raise E2EFailure("project_activate", "remote_project_not_ready")
        return project

    def _assert_supported_target(self, project: dict[str, Any]) -> dict[str, Any]:
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
        target = next(
            (
                item
                for item in targets
                if isinstance(item, dict) and item.get("target_id") == self._target_id
            ),
            None,
        )
        if target is None:
            raise E2EFailure("project_capabilities", "target_not_supported")
        methods = [
            item
            for key in ("methods", "accepted_methods")
            for item in target.get(key, [])
            if isinstance(item, dict) and item.get("method_id") == self._method_id
        ]
        if not methods or not any(
            isinstance(item.get("support"), dict)
            and item["support"].get("overall") == "supported"
            for item in methods
        ):
            raise E2EFailure("project_capabilities", "method_not_supported")
        if capabilities.get("project_etag") != project.get("etag"):
            raise E2EFailure("project_capabilities", "project_etag_mismatch")
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
        target_artifact = next(
            (artifact for artifact in artifacts if artifact.get("target_id") == self._target_id),
            None,
        )
        inspection: dict[str, object] | None = None
        if target_artifact is not None:
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
            inspection = {
                "artifact_id_sha256": _digest_text(artifact_id),
                "document_count": len(content.get("documents", []))
                if isinstance(content.get("documents"), list)
                else -1,
                "total_documents": content.get("total_documents"),
                "total_utf8_bytes": content.get("total_utf8_bytes"),
                "truncated": content.get("truncated"),
            }

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
            "artifact_inspection": inspection,
            "context": {
                "status": context.get("status"),
                "capture_mode": context.get("capture_mode"),
                "token_level_metrics_available": context.get(
                    "token_level_metrics_available"
                ),
                "artifact_count": len(context.get("artifacts", []))
                if isinstance(context.get("artifacts"), list)
                else -1,
                "adapter_count": len(context.get("adapters", []))
                if isinstance(context.get("adapters"), list)
                else -1,
            },
        }
        return SessionObservation(evidence, run, context, tuple(artifacts))

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
        if observation.context.get("capture_mode") != "transcript" or observation.context.get(
            "token_level_metrics_available"
        ) is not False:
            raise E2EFailure(f"session_{ordinal}_context", "capture_contract_mismatch")
        timeline_evidence = observation.evidence.get("timeline")
        phases = (
            set(timeline_evidence.get("phase_values", []))
            if isinstance(timeline_evidence, dict)
            else set()
        )
        if not {"evolution", "revision", "terminal"}.issubset(phases):
            raise E2EFailure(f"session_{ordinal}_timeline", "terminal_evidence_missing")
        logs_evidence = observation.evidence.get("logs")
        if not isinstance(logs_evidence, dict) or logs_evidence.get("count", 0) < 1:
            raise E2EFailure(f"session_{ordinal}_logs", "logs_missing")
        if not any(artifact.get("target_id") == self._target_id for artifact in observation.artifacts):
            raise E2EFailure(f"session_{ordinal}_artifacts", "target_artifact_missing")
        if observation.evidence.get("artifact_inspection") is None:
            raise E2EFailure(f"session_{ordinal}_artifacts", "artifact_inspection_missing")

    def _assert_successor_reuse(
        self,
        first: SessionObservation,
        second: SessionObservation,
    ) -> dict[str, object]:
        first_pin = _revision(first.run.get("pinned_revision"), "reuse_first_pin")
        second_pin = _revision(second.run.get("pinned_revision"), "reuse_second_pin")
        first_outputs = [
            artifact
            for artifact in first.artifacts
            if artifact.get("target_id") == self._target_id
        ]
        if not first_outputs:
            raise E2EFailure("successor_reuse", "first_session_output_missing")
        successor = _revision(
            first_outputs[0].get("produced_revision"),
            "reuse_successor_revision",
        )
        if any(artifact.get("produced_revision") != successor for artifact in first_outputs):
            raise E2EFailure("successor_reuse", "output_revision_inconsistent")
        if successor["generation"] != first_pin["generation"] + 1:
            raise E2EFailure("successor_reuse", "successor_generation_invalid")
        required = _revision(
            _nested(second.run, "required_revision", "revision"),
            "reuse_second_required",
        )
        if second_pin != successor or required != successor:
            raise E2EFailure("successor_reuse", "second_session_did_not_pin_successor")
        first_artifact_ids = {
            _text(artifact, "id", "successor_reuse") for artifact in first_outputs
        }
        context_artifacts = second.context.get("artifacts")
        if not isinstance(context_artifacts, list):
            raise E2EFailure("successor_reuse", "second_context_invalid")
        reused = [
            item
            for item in context_artifacts
            if isinstance(item, dict)
            and item.get("artifact_id") in first_artifact_ids
            and item.get("target_id") == self._target_id
            and item.get("revision") == successor
        ]
        if not reused:
            raise E2EFailure("successor_reuse", "first_artifact_not_in_second_context")
        return {
            "successor_generation_delta": 1,
            "session_2_pinned_session_1_successor": True,
            "session_1_artifact_reused": True,
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


def _build_assets(root: Path) -> ReleaseAssets:
    root.mkdir(parents=True, exist_ok=True)
    output = root / "core-release-assets"
    command = [
        sys.executable,
        str(REPOSITORY_ROOT / "desktop/packaging/build_sidecar.py"),
        "--core-wheel-output-dir",
        str(output),
    ]
    with TemporaryFile(mode="w+b") as build_log:
        result = subprocess.run(
            command,
            cwd=REPOSITORY_ROOT,
            stdin=subprocess.DEVNULL,
            stdout=build_log,
            stderr=subprocess.STDOUT,
            check=False,
            env=_build_environment(),
        )
        if result.returncode != 0:
            raise E2EFailure("release_assets", "sidecar_build_failed")
        build_log.seek(0)
        lines = build_log.read().decode("utf-8", errors="replace").splitlines()
    if not lines:
        raise E2EFailure("release_assets", "sidecar_build_output_missing")
    sidecar = Path(lines[-1].strip())
    wheels = sorted(output.glob("*.whl"))
    lock = output / "framework-lock.json"
    if len(wheels) != 1:
        raise E2EFailure("release_assets", "built_wheel_inventory_invalid")
    return _inspect_release_assets(sidecar, wheels[0], lock)


def _inspect_release_assets(sidecar: Path, wheel: Path, lock: Path) -> ReleaseAssets:
    for item, code in (
        (sidecar, "packaged_sidecar_invalid"),
        (wheel, "core_wheel_invalid"),
        (lock, "framework_lock_invalid"),
    ):
        if item.is_symlink() or not item.is_file() or not stat.S_ISREG(item.stat().st_mode):
            raise E2EFailure("release_assets", code)
    if not os.access(sidecar, os.X_OK):
        raise E2EFailure("release_assets", "packaged_sidecar_not_executable")
    name, version, wheel_digest = _validate_wheel_lock(wheel, lock)
    builder = _load_sidecar_builder()
    try:
        builder._validate_fd_bound_bootloader(sidecar)
        builder._validate_embedded_core_wheel(sidecar, wheel)
        builder._validate_embedded_core_framework_lock(
            sidecar,
            wheel,
            lock,
            version=version,
        )
    except Exception as exc:
        raise E2EFailure("release_assets", "packaged_assets_not_exact") from exc
    return ReleaseAssets(
        sidecar=sidecar,
        wheel=wheel,
        framework_lock=lock,
        evidence={
            "sidecar": _file_evidence(sidecar),
            "core_wheel": {
                **_file_evidence(wheel),
                "filename": wheel.name,
                "distribution": name,
                "version": version,
            },
            "framework_lock": {
                **_file_evidence(lock),
                "distribution_digest": wheel_digest,
            },
            "exact_embedded_assets_verified": True,
        },
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


def _launch_sidecar(sidecar: Path, root: Path) -> NativeSidecar:
    if os.name != "posix":
        raise E2EFailure("native_launch", "posix_process_boundary_required")
    root.mkdir(parents=True, exist_ok=True)
    launch_path = root / "openevo-desktop-sidecar"
    shutil.copyfile(sidecar, launch_path)
    launch_path.chmod(0o500)
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
    except BaseException:
        process_log.close()
        raise
    finally:
        os.close(log_guard)
        os.close(executable_fd)
        listener.close()
    if process.stdin is None:
        process_log.close()
        raise E2EFailure("native_launch", "credential_channel_missing")
    try:
        process.stdin.write(credentials.frame())
        process.stdin.close()
        process.stdin = None
    except OSError as exc:
        _terminate_process(process)
        process_log.close()
        raise E2EFailure("native_launch", "credential_frame_delivery_failed") from exc
    native = NativeSidecar(
        process=process,
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
        if native.process.poll() is not None:
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
        domain = (
            f"{NATIVE_PROTOCOL}\0{native.credentials.instance_id}\0{challenge}"
        ).encode("ascii")
        expected = hmac.new(
            native.credentials.readiness_key,
            domain,
            hashlib.sha256,
        ).hexdigest()
        if (
            health.get("status") == "ok"
            and health.get("protocol") == NATIVE_PROTOCOL
            and health.get("instance_id") == native.credentials.instance_id
            and hmac.compare_digest(str(health.get("instance_proof", "")), expected)
        ):
            return
        time.sleep(0.25)
    raise E2EFailure("native_launch", "sidecar_readiness_timeout")


def _release_identity(api: LocalApi) -> dict[str, object]:
    version = api.request(
        "GET",
        "/version",
        stage="desktop_version",
        authenticated=False,
    )
    assert version is not None
    required = {
        "api_name": "openevo-desktop-local-api",
        "preferred_major": 1,
        "provider_kind": "desktop_sidecar",
        "build_channel": "release",
    }
    if any(version.get(key) != value for key, value in required.items()):
        raise E2EFailure("desktop_version", "not_release_desktop_sidecar")
    if version.get("supported_majors") != [1] or not _is_sha256(
        version.get("openapi_sha256")
    ):
        raise E2EFailure("desktop_version", "desktop_contract_invalid")
    source_commit = version.get("source_commit")
    build_version = version.get("build_version")
    if (
        not isinstance(source_commit, str)
        or re.fullmatch(r"[0-9a-f]{7,40}", source_commit) is None
        or not isinstance(build_version, str)
        or not build_version
    ):
        raise E2EFailure("desktop_version", "desktop_build_identity_invalid")
    return {
        "source_commit": source_commit,
        "build_version": build_version,
        "openapi_sha256": version["openapi_sha256"],
        "provider_kind": version["provider_kind"],
        "build_channel": version["build_channel"],
        "feature_flags": sorted(version.get("feature_flags", [])),
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
                if lowered in FORBIDDEN_EVIDENCE_KEYS or lowered.endswith("_path"):
                    raise E2EFailure("evidence", "forbidden_evidence_field")
                visit(child)
            return
        if isinstance(item, list):
            for child in item:
                visit(child)
            return
        if isinstance(item, str):
            if item.startswith("/") or ABSOLUTE_WINDOWS_PATH.match(item):
                raise E2EFailure("evidence", "host_path_in_evidence")
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
    if contract_payload.get("allowed_provider_kinds") != ["desktop_sidecar"]:
        raise E2EFailure("structural_check", "release_provider_policy_invalid")
    required = (
        'parser.add_argument("--listener-fd", type=int, required=True)',
        'parser.add_argument("--native-instance-stdin", action="store_true", required=True)',
        "_read_native_instance_frame()",
        "uvicorn.Server(config).run(sockets=[listener])",
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
            {
                str(item[field_name])
                for item in items
                if isinstance(item.get(field_name), str)
            }
        )
    return evidence


def _artifact_evidence(artifact: Mapping[str, object], stage: str) -> dict[str, object]:
    return {
        "artifact_id_sha256": _digest_text(_text(artifact, "id", stage)),
        "artifact_type": artifact.get("artifact_type"),
        "target_id": artifact.get("target_id"),
        "content_sha256": artifact.get("content_sha256"),
        "byte_size": artifact.get("byte_size"),
        "selected": artifact.get("selected"),
        "promoted": artifact.get("promoted"),
        "produced_revision": _revision_evidence(
            artifact.get("produced_revision"), stage
        ),
    }


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
        json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
        + "\n"
    ).encode("utf-8")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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


def _terminate_process(process: subprocess.Popen[Any]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait(timeout=10)


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
    external = (args.sidecar, args.core_wheel, args.framework_lock)
    if any(item is not None for item in external) and not all(
        item is not None for item in external
    ):
        raise E2EFailure("arguments", "release_asset_triplet_required")
    if args.structural_check:
        return
    if not args.host or not args.user or not args.expected_host_key_fingerprint:
        raise E2EFailure("arguments", "remote_identity_required")
    if HOST_KEY_PATTERN.fullmatch(args.expected_host_key_fingerprint) is None:
        raise E2EFailure("arguments", "host_key_fingerprint_invalid")
    if not os.environ.get("SSH_AUTH_SOCK"):
        raise E2EFailure("arguments", "ssh_agent_unavailable")


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
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("desktop-real-science-e2e-evidence.json"),
    )
    parser.add_argument("--codex-model", default="gpt-5")
    parser.add_argument(
        "--target-id",
        choices=("text_memory", "skill_bundle", "agent_system"),
        default="text_memory",
    )
    parser.add_argument("--method-id", default="text_memory_expel_reflector")
    parser.add_argument("--task-title", default="Release Desktop science E2E")
    parser.add_argument(
        "--task-objective",
        default=(
            "Inspect the scratch workspace, record one concise scientific observation, "
            "and complete without requesting user input."
        ),
    )
    parser.add_argument("--poll-seconds", type=float, default=2.0)
    parser.add_argument("--activation-timeout-seconds", type=float, default=1200.0)
    parser.add_argument("--run-timeout-seconds", type=float, default=7200.0)
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
    workflow: DesktopScienceWorkflow | None = None
    cleanup = {
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
                assets = _build_assets(root / "build")
            else:
                assets = _inspect_release_assets(
                    args.sidecar,
                    args.core_wheel,
                    args.framework_lock,
                )
            evidence["release_assets"] = assets.evidence
            native = _launch_sidecar(assets.sidecar, root / "native")
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
                target_id=args.target_id,
                method_id=args.method_id,
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
                cleanup["desktop_disconnect_succeeded"] = workflow.cleanup()
            if native is not None:
                cleanup["core_ownership_release_requested"] = True
                cleanup["sidecar_shutdown_succeeded"] = native.terminate()
            evidence["cleanup"] = cleanup
            evidence["finished_at"] = _utc_now()
            if evidence.get("outcome") == "passed" and not all(cleanup.values()):
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

    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


if __name__ == "__main__":
    raise SystemExit(main())
