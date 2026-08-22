#!/usr/bin/env python3
"""Development-only Desktop Local API v2 adapter for ``live_agent_daemon.py``.

This module is intentionally kept under ``scripts/dev``.  It projects the small,
working development daemon into renderer-safe Desktop v2 models; it is not a
release Daemon implementation and is never packaged.
"""

from __future__ import annotations

import hashlib
import argparse
import json
import logging
import os
import secrets
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping
from urllib.parse import quote


# This development entry point is launched from ``desktop/``.  Keep repository-local
# packages importable without requiring Desktop itself to become an installable product.
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError, ResponseValidationError
from pydantic import ValidationError
from fastapi.responses import JSONResponse, Response, StreamingResponse

from desktop.server.browser_host import install_browser_host_routes
from desktop.server.app import create_desktop_app
from desktop.sidecar.contracts.v2 import models as m
from desktop.sidecar.contracts.v2.app import create_desktop_local_v2_contract_app
from desktop.sidecar.event_broker_v2 import DesktopEventBrokerV2
from openevo.backend.contracts.v2 import models as core


OPENAPI_SHA256 = "fe4ac8415f20e584bf0f9b3240d52ec98bc61366d587a09b91d14b4ae29541af"
EVENT_SCHEMA_SHA256 = "515b6d90e9ebdf3f5b4f7c4a57a1924dc85011536d9396b1ab3a5dc73fc48b6b"
RELEASE_VERSION = "0.1.10-dev-agent-web-v1"
FEATURES = [
    "development_agent_bridge_v2",
    "event_replay_v2",
    "mutation_idempotency_v2",
]
PROFILE_ID = "development-agent-profile"
ETAG = f'"{"b" * 64}"'
DIGEST = "a" * 64
MAX_DEVELOPMENT_DAEMON_STATE_BYTES = 64 * 1024 * 1024
MAX_DEVELOPMENT_PROXY_REQUEST_BYTES = 64 * 1024 * 1024
MAX_DEVELOPMENT_PROXY_RESPONSE_BYTES = 64 * 1024 * 1024
STATE_CACHE_SECONDS = 1.0
STATE_EVENT_POLL_SECONDS = 0.25
LOGGER = logging.getLogger("openevo.development_agent_web_layer")


def _canonical_digest(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class DevelopmentDaemonClient:
    def __init__(self, endpoint: str, token: str) -> None:
        self._endpoint = endpoint.rstrip("/") + "/openevo-dev-agent/v1"
        self._token = token

    def request(self, path: str, *, method: str = "GET", body: object | None = None) -> object:
        data = None if body is None else json.dumps(body, separators=(",", ":")).encode()
        headers = {"Authorization": f"Bearer {self._token}"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self._endpoint + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                declared_length = response.headers.get("Content-Length")
                if declared_length is not None and int(declared_length) > MAX_DEVELOPMENT_DAEMON_STATE_BYTES:
                    raise HTTPException(status_code=503, detail="development daemon response exceeds the bounded bridge limit")
                payload = response.read(MAX_DEVELOPMENT_DAEMON_STATE_BYTES + 1)
        except (OSError, urllib.error.HTTPError) as exc:
            raise HTTPException(status_code=503, detail="development daemon is unavailable") from exc
        if len(payload) > MAX_DEVELOPMENT_DAEMON_STATE_BYTES:
            raise HTTPException(status_code=503, detail="development daemon response exceeds the bounded bridge limit")
        try:
            return json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=503, detail="development daemon returned invalid JSON") from exc

    def proxy(
        self,
        path: str,
        *,
        query: str,
        method: str,
        body: bytes,
        content_type: str | None,
    ) -> tuple[int, bytes, Mapping[str, str]]:
        segments = path.split("/")
        if not path or any(segment in {"", ".", ".."} for segment in segments):
            raise HTTPException(status_code=404, detail="development daemon route not found")
        url = self._endpoint + "/" + quote(path, safe="/-._~")
        if query:
            url += "?" + query
        headers = {"Authorization": f"Bearer {self._token}"}
        if content_type:
            headers["Content-Type"] = content_type
        upstream = urllib.request.Request(
            url,
            data=body if method in {"POST", "PUT", "PATCH"} else None,
            headers=headers,
            method=method,
        )
        try:
            response = urllib.request.urlopen(upstream, timeout=65)
        except urllib.error.HTTPError as exc:
            response = exc
        except OSError as exc:
            raise HTTPException(status_code=503, detail="development daemon is unavailable") from exc
        with response:
            payload = response.read(MAX_DEVELOPMENT_PROXY_RESPONSE_BYTES + 1)
            if len(payload) > MAX_DEVELOPMENT_PROXY_RESPONSE_BYTES:
                raise HTTPException(status_code=503, detail="development daemon response exceeds the proxy limit")
            forwarded_headers = {
                name: value
                for name in ("Content-Type", "Content-Disposition")
                if (value := response.headers.get(name)) is not None
            }
            return response.status, payload, forwarded_headers


class DevelopmentAgentDesktopV2Provider:
    def __init__(
        self,
        client: DevelopmentDaemonClient,
        *,
        source_commit: str,
        event_broker: DesktopEventBrokerV2 | None = None,
    ) -> None:
        self._client = client
        self._source_commit = source_commit
        self._event_broker = event_broker or DesktopEventBrokerV2()
        self._operations: dict[str, m.LifecycleOperationV2] = {}
        self._actions: dict[str, str] = {}
        self._state_cache: dict[str, object] | None = None
        self._state_cache_deadline = 0.0
        self._state_lock = threading.RLock()
        self._started_at = _now()

    def invoke(self, operation_id: str, arguments: Mapping[str, object]) -> object:
        handlers = {
            "getDesktopContractVersionV2": self._version,
            "getDesktopHealthV2": self._health,
            "getDesktopStateV2": self._state,
            "listConfiguredSshHostsV2": self._hosts,
            "listRemoteWorkspaceProfilesV2": self._profiles,
            "getRemoteWorkspaceProfileV2": self._profile,
            "listDesktopProjectsV2": self._projects,
            "createDesktopProjectV2": self._create_project,
            "getDesktopProjectV2": self._project,
            "updateDesktopProjectV2": self._update_project,
            "activateDesktopProjectV2": self._activate_project,
            "getDesktopLifecycleOperationByActionV2": self._operation_by_action,
            "getDesktopLifecycleOperationV2": self._operation,
            "getDesktopLifecycleOperationLogsV2": self._operation_logs,
            "acknowledgeDesktopLifecycleOperationV2": self._acknowledge_operation,
            "getDesktopProjectCapabilitiesV2": self._capabilities,
            "validateDesktopProjectV2": self._validate,
            "listDesktopTasksV2": self._tasks,
            "submitDesktopTaskV2": self._submit_task,
            "getDesktopTaskV2": self._task,
            "getDesktopTaskTimelineV2": self._timeline,
            "getDesktopTaskLogsV2": self._logs,
            "getDesktopTaskContextV2": self._task_context,
            "listDesktopTaskArtifactsV2": self._task_artifacts,
            "getDesktopArtifactV2": self._artifact,
            "getDesktopArtifactContentV2": self._artifact_content,
            "listDesktopServicesV2": self._services,
            "streamDesktopEventsV2": self._events,
        }
        handler = handlers.get(operation_id)
        if handler is None:
            raise HTTPException(
                status_code=503,
                detail=f"{operation_id} is not implemented by the incremental development bridge",
            )
        return handler(arguments)

    def _remote_state(self, *, refresh: bool = False) -> dict[str, object]:
        with self._state_lock:
            now = time.monotonic()
            if not refresh and self._state_cache is not None and now < self._state_cache_deadline:
                return self._state_cache
        payload = self._client.request("/state")
        if not isinstance(payload, dict) or payload.get("schema_version") != "1":
            raise HTTPException(status_code=503, detail="development daemon returned an invalid state")
        with self._state_lock:
            self._state_cache = payload
            self._state_cache_deadline = time.monotonic() + STATE_CACHE_SECONDS
        return payload

    def _invalidate_state(self) -> None:
        with self._state_lock:
            self._state_cache = None
            self._state_cache_deadline = 0.0

    def observe_remote_state(self, payload: dict[str, object]) -> None:
        """Publish one authoritative snapshot for the next browser refresh."""

        with self._state_lock:
            self._state_cache = payload
            self._state_cache_deadline = time.monotonic() + STATE_CACHE_SECONDS

    def _version(self, _: Mapping[str, object]) -> m.DesktopVersionV2:
        build_id = _canonical_digest({"source_commit": self._source_commit, "features": FEATURES})
        return m.DesktopVersionV2(
            api_name="openevo-desktop-local-api", preferred_major=2, supported_majors=[2],
            mutation_major=2, openapi_sha256=OPENAPI_SHA256,
            event_schema_sha256=EVENT_SCHEMA_SHA256, release_version=RELEASE_VERSION,
            build_id=build_id, source_commit=self._source_commit, build_channel="development",
            provider_kind="desktop_sidecar", feature_flags=FEATURES,
            feature_set_sha256=_canonical_digest(FEATURES), required_core_api_major=2,
            mutation_compatible=True,
        )

    def _health(self, _: Mapping[str, object]) -> m.DesktopHealthV2:
        self._client.request("/state")
        return m.DesktopHealthV2(status="ready", checked_at=_now())

    def _profile_model(self, state: Mapping[str, object]) -> m.RemoteWorkspaceProfileV2:
        active = state.get("active_project_id")
        capabilities = self._client.request("/capabilities")
        registry = capabilities.get("capabilities", {}).get("registry_digest", DIGEST) if isinstance(capabilities, dict) else DIGEST
        updated_at = self._state_timestamp(state)
        profile_etag = f'"{_canonical_digest({"active_project_id": active, "registry": registry})}"'
        return m.RemoteWorkspaceProfileV2(
            profile_id=PROFILE_ID, display_name="Development agent tunnel", ssh_host_alias="development-tunnel",
            catalog_generation=1, connection_generation=1, connection_state="connected", prompt=None,
            trust={"schema_version": "2", "connection_generation": 1, "state": "trusted", "review_id": None,
                   "review_sha256": None, "key_fingerprints": [], "repair_support": "not_needed"},
            failure=None, active_project_id=active, core_api_major=2, core_openapi_sha256=DIGEST,
            core_event_schema_sha256=DIGEST, core_registry_sha256=registry,
            created_at=self._started_at, updated_at=updated_at, etag=profile_etag,
        )

    def _state_timestamp(self, state: Mapping[str, object]) -> str:
        candidates = [self._started_at]
        for collection_name in ("projects", "sessions", "artifacts", "evolution_jobs", "evolution_runs"):
            collection = state.get(collection_name, [])
            if not isinstance(collection, list):
                continue
            for item in collection:
                if not isinstance(item, dict):
                    continue
                for field in ("updated_at", "created_at"):
                    value = item.get(field)
                    if isinstance(value, str):
                        candidates.append(value)
        return max(candidates)

    def _state(self, _: Mapping[str, object]) -> m.DesktopStateV2:
        state = self._remote_state()
        profile = self._profile_model(state)
        return m.DesktopStateV2(profiles=[profile], active_profile_id=PROFILE_ID,
                                active_project_id=state.get("active_project_id"), pending_operations=[],
                                last_event_id=None, updated_at=self._state_timestamp(state))

    def _hosts(self, _: Mapping[str, object]) -> m.SshHostCatalogV2:
        return m.SshHostCatalogV2(catalog_generation=1, hosts=[], warnings=[], scanned_at=_now())

    def _profiles(self, _: Mapping[str, object]) -> m.RemoteProfilePageV2:
        return m.RemoteProfilePageV2(items=[self._profile_model(self._remote_state())], next_cursor=None, has_more=False)

    def _profile(self, arguments: Mapping[str, object]) -> m.RemoteWorkspaceProfileV2:
        if arguments.get("profile_id") != PROFILE_ID:
            raise HTTPException(status_code=404, detail="profile not found")
        return self._profile_model(self._remote_state())

    def _head(self, project_id: str, config: core.ScienceProjectConfigV2, generation: int = 0) -> core.ProjectHeadRefV2:
        predecessor = None if generation == 0 else f"{project_id}-head-{generation - 1}"
        revision = {"schema_version": "2", "evolution_revision_id": f"{project_id}-evolution-{generation}",
                    "project_id": project_id, "manifest_sha256": _canonical_digest([project_id, generation, "evolution"]),
                    "artifact_count": 0}
        return core.ProjectHeadRefV2.model_validate({
            "schema_version": "2", "project_head_id": f"{project_id}-head-{generation}", "project_id": project_id,
            "generation": generation, "predecessor_project_head_id": predecessor,
            "workspace_snapshot": {"schema_version": "2", "workspace_snapshot_id": f"{project_id}-workspace-{generation}",
                "project_id": project_id, "manifest_sha256": _canonical_digest([project_id, generation, "workspace"]),
                "entry_count": 0, "byte_size": 0},
            "evolution_revision": revision,
            "runtime_context_snapshot": {"schema_version": "2", "runtime_context_snapshot_id": f"{project_id}-context-{generation}",
                "project_id": project_id, "evolution_revision_id": revision["evolution_revision_id"],
                "evolution_revision_manifest_sha256": revision["manifest_sha256"], "registry_sha256": DIGEST,
                "runtime_contract_sha256": _canonical_digest([project_id, generation, "runtime"]),
                "manifest_sha256": _canonical_digest([project_id, generation, "context"])},
            "effective_execution_snapshot": {"schema_version": "2", "effective_execution_snapshot_id": f"{project_id}-execution-{generation}",
                "project_id": project_id, "execution_mode": config.execution.mode, "capture_mode": config.execution.capture_mode,
                "token_level_metrics_available": config.execution.token_level_metrics_available,
                "producer_id": "development-daemon", "snapshot_sha256": _canonical_digest([project_id, generation, "execution"])},
            "registry_sha256": DIGEST, "manifest_sha256": _canonical_digest([project_id, generation, "head"]),
        })

    def _project_models(self, state: Mapping[str, object]) -> list[core.ProjectV2]:
        result = []
        for raw in state.get("projects", []):
            config = core.ScienceProjectConfigV2.model_validate(raw["config"])
            sessions = [item for item in state.get("sessions", []) if item["project_id"] == raw["project_id"] and item["state"] == "completed"]
            result.append(core.ProjectV2(project_id=raw["project_id"], display_name=raw["display_name"], config=config,
                project_config_sha256=core.project_config_sha256_for(config), active_project_head=self._head(raw["project_id"], config, len(sessions)),
                admission_etag=ETAG, state="ready", created_at=raw["created_at"], updated_at=raw["updated_at"], etag=ETAG))
        return result

    def _projects(self, _: Mapping[str, object]) -> core.ProjectPageV2:
        state = self._remote_state()
        active_project_id = state.get("active_project_id")
        projects = self._project_models(state)
        if active_project_id is not None:
            projects = [project for project in projects if project.project_id == active_project_id]
        return core.ProjectPageV2(items=projects, next_cursor=None, has_more=False)

    def _find_project(self, project_id: object) -> core.ProjectV2:
        for project in self._project_models(self._remote_state()):
            if project.project_id == project_id:
                return project
        raise HTTPException(status_code=404, detail="project not found")

    def _project(self, arguments: Mapping[str, object]) -> core.ProjectV2:
        return self._find_project(arguments.get("project_id"))

    def _update_project(self, arguments: Mapping[str, object]) -> core.ProjectV2:
        project_id = str(arguments["project_id"])
        request = arguments["request"]
        self._client.request(f"/projects/{project_id}", method="PUT", body={"schema_version": "1", "display_name": request.display_name, "config": request.config.model_dump(mode="json")})
        self._invalidate_state()
        return self._find_project(project_id)

    def _terminal_operation(self, *, kind: str, project_id: str, action_id: str) -> m.LifecycleOperationV2:
        operation_id = f"development-{kind}-{_canonical_digest(action_id)[:16]}"
        timestamp = _now()
        operation = m.LifecycleOperationV2(
            operation_id=operation_id, kind=kind,
            resource={"resource_kind": "project", "resource_id": project_id},
            request_sha256=_canonical_digest([kind, project_id, action_id]), status="succeeded",
            phase="finalizing", phase_index=16, phase_total=17, progress=None, cancellable=False,
            result={"result_kind": "project", "project_id": project_id}, failure=None,
            log_sequence_high_watermark=0, created_at=timestamp, started_at=timestamp,
            updated_at=timestamp, finished_at=timestamp, etag=ETAG,
        )
        self._operations[operation_id] = operation
        self._actions[action_id] = operation_id
        return operation

    def _create_project(self, arguments: Mapping[str, object]) -> m.LifecycleOperationV2:
        request = arguments["request"]
        if request.config.workspace.kind != "scratch":
            raise HTTPException(status_code=503, detail="the incremental bridge currently supports scratch workspaces only")
        action_id = str(arguments["idempotency_key"])
        if action_id in self._actions:
            return self._operations[self._actions[action_id]]
        project_id = f"development-project-{_canonical_digest(action_id)[:12]}"
        self._client.request("/projects", method="POST", body={
            "schema_version": "1", "project_id": project_id,
            "display_name": request.display_name, "config": request.config.model_dump(mode="json"),
        })
        self._invalidate_state()
        return self._terminal_operation(kind="project_create", project_id=project_id, action_id=action_id)

    def _activate_project(self, arguments: Mapping[str, object]) -> m.LifecycleOperationV2:
        project_id = str(arguments["project_id"])
        self._find_project(project_id)
        action_id = str(arguments["idempotency_key"])
        if action_id in self._actions:
            return self._operations[self._actions[action_id]]
        self._client.request(f"/projects/{project_id}/activate", method="POST", body={"schema_version": "1"})
        self._invalidate_state()
        return self._terminal_operation(kind="project_activate", project_id=project_id,
                                        action_id=action_id)

    def _operation(self, arguments: Mapping[str, object]) -> m.LifecycleOperationV2:
        operation = self._operations.get(str(arguments.get("operation_id")))
        if operation is None: raise HTTPException(status_code=404, detail="operation not found")
        return operation

    def _operation_by_action(self, arguments: Mapping[str, object]) -> m.LifecycleOperationV2:
        operation_id = self._actions.get(str(arguments.get("action_id")))
        if operation_id is None: raise HTTPException(status_code=404, detail="operation not found")
        operation = self._operations[operation_id]
        if operation.kind != arguments.get("kind"): raise HTTPException(status_code=404, detail="operation not found")
        return operation

    def _operation_logs(self, arguments: Mapping[str, object]) -> m.LifecycleLogPageV2:
        operation = self._operation(arguments)
        return m.LifecycleLogPageV2(operation_id=operation.operation_id, dropped_before_sequence=0,
                                    items=[], next_cursor=None, has_more=False)

    def _acknowledge_operation(self, arguments: Mapping[str, object]) -> Response:
        self._operation(arguments)
        return Response(status_code=204)

    def _task_model(self, raw: Mapping[str, object], project: core.ProjectV2, ordinal: int) -> core.TaskV2:
        task_id = str(raw["session_id"])
        head = project.active_project_head
        assert head is not None
        admission_data = {"schema_version": "2", "task_admission_id": f"{project.project_id}-admission-{ordinal}",
            "task_id": task_id, "project_id": project.project_id, "predecessor_project_head": head.model_dump(mode="json"),
            "workspace_snapshot": head.workspace_snapshot.model_dump(mode="json"), "project_config_sha256": project.project_config_sha256,
            "task_envelope_sha256": _canonical_digest([task_id, "envelope"]), "normalized_evolution_intent_sha256": _canonical_digest([task_id, "evolution"]),
            "registry_sha256": head.registry_sha256, "admitted_at": raw["created_at"]}
        admission_data["admission_sha256"] = _canonical_digest(admission_data)
        admission = core.TaskAdmissionRefV2.model_validate(admission_data)
        attempt_id = f"{task_id}-attempt-1"
        state = "closed" if raw["state"] == "completed" else raw["state"]
        return core.TaskV2(task_id=task_id, project_id=project.project_id, admission=admission,
            attempts=[{"schema_version": "2", "attempt_id": attempt_id, "ordinal": 1, "task_id": task_id,
                "task_admission_id": admission.task_admission_id, "admission_sha256": admission.admission_sha256,
                "project_id": project.project_id, "predecessor_project_head_id": head.project_head_id, "created_at": raw["created_at"]}],
            authoritative_attempt_id=attempt_id, successor_transition=None, state=state,
            created_at=raw["created_at"], updated_at=raw["updated_at"], etag=ETAG)

    def _all_tasks(self) -> tuple[dict[str, object], list[core.TaskV2]]:
        state = self._remote_state(); projects = {p.project_id: p for p in self._project_models(state)}
        tasks = [self._task_model(raw, projects[raw["project_id"]], index + 1) for index, raw in enumerate(state.get("sessions", [])) if raw["project_id"] in projects]
        return state, tasks

    def _tasks(self, arguments: Mapping[str, object]) -> core.TaskPageV2:
        _, tasks = self._all_tasks(); project_id = arguments.get("project_id")
        return core.TaskPageV2(items=[t for t in tasks if project_id is None or t.project_id == project_id], next_cursor=None, has_more=False)

    def _task(self, arguments: Mapping[str, object]) -> core.TaskV2:
        _, tasks = self._all_tasks()
        for task in tasks:
            if task.task_id == arguments.get("task_id"): return task
        raise HTTPException(status_code=404, detail="task not found")

    def _submit_task(self, arguments: Mapping[str, object]) -> core.TaskV2:
        request = arguments["request"]; project = self._find_project(request.project_id)
        payload = self._client.request("/sessions", method="POST", body={"schema_version": "1", "project_id": project.project_id,
            "project_name": project.display_name, "task_title": project.config.task.title, "instruction": project.config.task.objective})
        self._invalidate_state()
        return self._task({"task_id": payload["session_id"]})

    def _raw_task(self, task_id: object) -> Mapping[str, object]:
        for raw in self._remote_state().get("sessions", []):
            if raw["session_id"] == task_id: return raw
        raise HTTPException(status_code=404, detail="task not found")

    def _timeline(self, _: Mapping[str, object]) -> core.TimelinePageV2:
        return core.TimelinePageV2(items=[], next_cursor=None, has_more=False)

    def _logs(self, arguments: Mapping[str, object]) -> core.LogPageV2:
        raw = self._raw_task(arguments.get("task_id")); created = raw["created_at"]
        return core.LogPageV2(items=[{"sequence": i + 1, "occurred_at": created, "stream": "system" if i == 0 else "transcript", "message": text}
                                       for i, text in enumerate(raw.get("logs", []))], next_cursor=None, has_more=False)

    def _task_context(self, arguments: Mapping[str, object]) -> core.TaskContextV2:
        task = self._task(arguments)
        return core.TaskContextV2(task_id=task.task_id, task_admission_id=task.admission.task_admission_id,
                                  project_head=task.admission.predecessor_project_head, workspace_snapshot=task.admission.workspace_snapshot)

    def _artifacts_for(self, task_id: object | None = None) -> list[core.ArtifactV2]:
        items = []
        for raw in self._remote_state().get("artifacts", []):
            if task_id is not None and raw["session_id"] != task_id: continue
            artifact_type = "diagnostic" if raw["artifact_type"] == "report" else raw["artifact_type"]
            items.append(core.ArtifactV2(artifact_id=raw["artifact_id"], project_id=raw["project_id"], artifact_type=artifact_type,
                                         manifest_sha256=raw["content_sha256"], byte_size=raw["byte_size"], created_at=raw["created_at"]))
        return items

    def _task_artifacts(self, arguments: Mapping[str, object]) -> core.ArtifactPageV2:
        return core.ArtifactPageV2(items=self._artifacts_for(arguments.get("task_id")), next_cursor=None, has_more=False)

    def _artifact(self, arguments: Mapping[str, object]) -> core.ArtifactV2:
        for artifact in self._artifacts_for():
            if artifact.artifact_id == arguments.get("artifact_id"): return artifact
        raise HTTPException(status_code=404, detail="artifact not found")

    def _artifact_content(self, arguments: Mapping[str, object]) -> core.ArtifactContentV2:
        artifact = self._artifact(arguments)
        return core.ArtifactContentV2(artifact=artifact, media_type="text/markdown", content_sha256=artifact.manifest_sha256, byte_size=artifact.byte_size)

    def _services(self, _: Mapping[str, object]) -> core.ServicePageV2:
        return core.ServicePageV2(items=[{"schema_version": "2", "service_id": "development-daemon", "kind": "daemon",
                                         "status": "ready", "updated_at": _now(), "etag": ETAG}], next_cursor=None, has_more=False)

    def _capabilities(self, arguments: Mapping[str, object]) -> m.ProjectCapabilityProjectionV2:
        project = self._find_project(arguments.get("project_id")); payload = self._client.request("/capabilities")
        capabilities = payload["capabilities"]
        return m.ProjectCapabilityProjectionV2(project_id=project.project_id, execution_mode=project.config.execution.mode,
            registry_sha256=capabilities["registry_digest"], capabilities_sha256=m.evolution_capabilities_sha256_for(capabilities),
            capabilities=capabilities, fetched_at=_now())

    def _validate(self, arguments: Mapping[str, object]) -> m.ProjectValidationV2:
        project = self._find_project(arguments.get("project_id"))
        return m.ProjectValidationV2(project_id=project.project_id, valid=True, registry_sha256=self._capabilities(arguments).registry_sha256,
                                     checks=[], validated_at=_now())

    def _events(self, _: Mapping[str, object]) -> StreamingResponse:
        last_event_id = _.get("last_event_id")
        if last_event_id is not None and type(last_event_id) is not str:
            raise TypeError("Desktop event cursor has the wrong type")
        subscription = self._event_broker.subscribe(last_event_id)
        return StreamingResponse(subscription, media_type="text/event-stream")


class DevelopmentAgentStateEventRelay:
    """Translate daemon snapshot changes into bounded Desktop v2 replay events.

    This is the development equivalent of nanobot's gateway hydration loop: the
    daemon remains the authority, while the Web Layer detects a changed
    authoritative snapshot and wakes every connected renderer.  The renderer
    then reloads the closed v2 snapshot; event payloads never become a second
    copy of domain state.
    """

    def __init__(
        self,
        *,
        client: DevelopmentDaemonClient,
        provider: DevelopmentAgentDesktopV2Provider,
        broker: DesktopEventBrokerV2,
        poll_seconds: float = STATE_EVENT_POLL_SECONDS,
    ) -> None:
        if not 0.05 <= poll_seconds <= 5:
            raise ValueError("development event polling interval is outside the supported bound")
        self._client = client
        self._provider = provider
        self._broker = broker
        self._poll_seconds = poll_seconds
        self._last_digest: str | None = None
        self._core_sequence = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def poll_once(self) -> bool:
        payload = self._client.request("/state")
        if not isinstance(payload, dict) or payload.get("schema_version") != "1":
            raise RuntimeError("development daemon returned an invalid event snapshot")
        digest = _canonical_digest(payload)
        self._provider.observe_remote_state(payload)
        if self._last_digest is None:
            self._last_digest = digest
            return False
        if digest == self._last_digest:
            return False
        self._last_digest = digest
        active_project_id = payload.get("active_project_id")
        if not isinstance(active_project_id, str) or not active_project_id:
            # Project creation is synchronously reconciled by its lifecycle
            # operation. Wait until an active project gives this event an exact
            # authority scope instead of publishing a misleading global event.
            return False
        self._core_sequence += 1
        self._broker.publish(
            m.CoreAuthorityEventPayloadV2(
                payload_kind="core_authority_changed",
                profile_id=PROFILE_ID,
                project_id=active_project_id,
                core_event_id=f"development-state-{digest[:32]}",
                core_event_sequence=self._core_sequence,
                core_event_type="transition_changed",
                core_payload_sha256=digest,
            )
        )
        return True

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="development-agent-state-events",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        self._thread = None
        if thread is not None:
            thread.join(timeout=max(2.0, self._poll_seconds * 4))
        self._broker.close()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception:
                LOGGER.exception("Development daemon state event polling failed")
            self._stop.wait(self._poll_seconds)


def create_development_agent_web_app(*, daemon_endpoint: str, daemon_token: str, session_token: str,
                                     bootstrap_token: str, browser_endpoint: str, source_commit: str,
                                     static_root: Path | str | None = None) -> FastAPI:
    daemon_client = DevelopmentDaemonClient(daemon_endpoint, daemon_token)
    event_broker = DesktopEventBrokerV2()
    provider = DevelopmentAgentDesktopV2Provider(
        daemon_client,
        source_commit=source_commit,
        event_broker=event_broker,
    )
    event_relay = DevelopmentAgentStateEventRelay(
        client=daemon_client,
        provider=provider,
        broker=event_broker,
    )
    app = create_desktop_local_v2_contract_app(provider)

    @app.on_event("startup")
    async def start_development_event_relay() -> None:
        event_relay.start()

    @app.on_event("shutdown")
    async def stop_development_event_relay() -> None:
        event_relay.stop()

    @app.api_route(
        "/openevo-dev-agent/v1/{path:path}",
        methods=["GET", "POST", "PUT", "PATCH"],
        include_in_schema=False,
    )
    async def proxy_development_daemon(path: str, request: Request) -> Response:
        candidate = request.headers.get("X-OpenEvo-Development-Web-Token", "").encode("utf-8")
        if not secrets.compare_digest(candidate, session_token.encode("utf-8")):
            return JSONResponse(status_code=401, content={"error": "development Web Layer session is invalid"})
        body = bytearray()
        async for chunk in request.stream():
            if len(chunk) > MAX_DEVELOPMENT_PROXY_REQUEST_BYTES - len(body):
                return JSONResponse(status_code=413, content={"error": "development Web Layer request is too large"})
            body.extend(chunk)
        status, payload, headers = provider._client.proxy(
            path,
            query=request.url.query,
            method=request.method,
            body=bytes(body),
            content_type=request.headers.get("Content-Type"),
        )
        return Response(content=payload, status_code=status, headers=dict(headers))

    @app.exception_handler(HTTPException)
    async def desktop_http_error(request: Request, exc: HTTPException) -> JSONResponse:
        del request
        status = exc.status_code
        return _desktop_error_response(
            status=status,
            code="desktop_resource_not_found" if status == 404 else "development_bridge_unavailable",
            summary=str(exc.detail) if isinstance(exc.detail, str) else "The development Web Layer request failed.",
            retryable=status >= 500,
            action="retry" if status >= 500 else "none",
        )

    @app.exception_handler(RequestValidationError)
    async def desktop_request_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        del request, exc
        return _desktop_error_response(
            status=422, code="desktop_request_invalid",
            summary="The Desktop request did not match the closed v2 contract.",
            retryable=False, action="none",
        )

    @app.exception_handler(ResponseValidationError)
    @app.exception_handler(ValidationError)
    async def desktop_projection_validation_error(request: Request, exc: Exception) -> JSONResponse:
        LOGGER.exception("Development Web Layer produced an invalid v2 projection", exc_info=exc)
        del request
        return _desktop_error_response(
            status=503, code="development_projection_invalid",
            summary="The development daemon state could not be projected into Desktop v2.",
            retryable=True, action="retry",
        )

    @app.exception_handler(Exception)
    async def desktop_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        LOGGER.exception("Development Web Layer request failed", exc_info=exc)
        del request
        return _desktop_error_response(
            status=503, code="development_bridge_failed",
            summary="The development Web Layer could not complete the request.",
            retryable=True, action="retry",
        )

    app.add_middleware(_ExactDevelopmentSessionMiddleware, session_token=session_token)

    version = provider._version({}).model_dump(mode="json")
    negotiated = {"major": 2, "mutation_major": 2, "openapi_sha256": version["openapi_sha256"],
        "event_schema_sha256": version["event_schema_sha256"], "release_version": version["release_version"],
        "build_id": version["build_id"], "source_commit": version["source_commit"], "build_channel": version["build_channel"],
        "provider_kind": version["provider_kind"], "feature_flags": version["feature_flags"],
        "feature_set_sha256": version["feature_set_sha256"], "required_core_api_major": 2, "mutation_compatible": True}
    install_browser_host_routes(app, endpoint=browser_endpoint, bootstrap_token=bootstrap_token,
                                session_token=session_token, negotiated_contract=negotiated)
    if static_root is not None:
        create_desktop_app(app, static_root=static_root)
    return app


def _required_secret(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Serve the development Desktop UI and v2 Web Layer.")
    parser.add_argument("--daemon-endpoint", required=True)
    parser.add_argument("--browser-endpoint", required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--static-root", type=Path, required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8788)
    args = parser.parse_args(argv)

    import uvicorn

    app = create_development_agent_web_app(
        daemon_endpoint=args.daemon_endpoint,
        daemon_token=_required_secret("OPENEVO_DEV_AGENT_TOKEN"),
        session_token=_required_secret("OPENEVO_DEV_WEB_SESSION_TOKEN"),
        bootstrap_token=_required_secret("OPENEVO_DEV_WEB_BOOTSTRAP_TOKEN"),
        browser_endpoint=args.browser_endpoint,
        source_commit=args.source_commit,
        static_root=args.static_root,
    )
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


def _desktop_error_response(
    *, status: int, code: str, summary: str, retryable: bool, action: m.DesktopActionV2
) -> JSONResponse:
    error = m.DesktopErrorV2(
        code=code,
        summary=summary[:512],
        retryable=retryable,
        action=action,
        affected_resource_id=None,
    )
    return JSONResponse(status_code=status, content=error.model_dump(mode="json"))


class _ExactDevelopmentSessionMiddleware:
    """Small pure-ASGI guard; avoids exposing the daemon credential to the renderer."""

    def __init__(self, app, *, session_token: str) -> None:
        self._app = app
        self._session_token = session_token.encode("utf-8")

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http" and scope.get("path", "").startswith("/desktop/v2"):
            headers = {name.lower(): value for name, value in scope.get("headers", [])}
            candidate = headers.get(b"x-openevo-desktop-session", b"")
            import secrets
            if not secrets.compare_digest(candidate, self._session_token):
                response = JSONResponse(
                    status_code=401,
                    content={
                        "schema_version": "2", "code": "desktop_session_invalid",
                        "summary": "The Desktop development session is invalid.",
                        "retryable": False, "action": "reconnect", "affected_resource_id": None,
                    },
                )
                await response(scope, receive, send)
                return
        await self._app(scope, receive, send)


if __name__ == "__main__":
    raise SystemExit(main())
