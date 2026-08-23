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
from http import HTTPStatus
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
try:  # package import in tests; direct import when launched as a script
    from scripts.dev.development_agent_v2_contract import (
        DevelopmentArtifactPageV2,
        DevelopmentArtifactV2,
        DevelopmentEvolutionRunApplyV2,
        DevelopmentEvolutionRunCreateV2,
        DevelopmentEvolutionRunPageV2,
        DevelopmentEvolutionRunV2,
        DevelopmentTaskObservationPageV2,
        DevelopmentTaskObservationV2,
        DevelopmentTaskTimelinePageV2,
        DevelopmentWorkspaceDeleteV2,
        DevelopmentWorkspaceMutationV2,
        DevelopmentWorkspacePageV2,
    )
except ModuleNotFoundError:  # pragma: no cover - exercised by the launcher
    from development_agent_v2_contract import (  # type: ignore[no-redef]
        DevelopmentArtifactPageV2,
        DevelopmentArtifactV2,
        DevelopmentEvolutionRunApplyV2,
        DevelopmentEvolutionRunCreateV2,
        DevelopmentEvolutionRunPageV2,
        DevelopmentEvolutionRunV2,
        DevelopmentTaskObservationPageV2,
        DevelopmentTaskObservationV2,
        DevelopmentTaskTimelinePageV2,
        DevelopmentWorkspaceDeleteV2,
        DevelopmentWorkspaceMutationV2,
        DevelopmentWorkspacePageV2,
    )


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
MAX_DEVELOPMENT_WORKSPACE_UPLOAD_BYTES = 32 * 1024 * 1024
MAX_DEVELOPMENT_EVOLUTION_REQUEST_BYTES = 256 * 1024
STATE_CACHE_SECONDS = 1.0
DAEMON_EVENT_WAIT_MILLISECONDS = 5_000
LOGGER = logging.getLogger("openevo.development_agent_web_layer")


def _canonical_digest(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class DevelopmentDaemonClient:
    def __init__(self, endpoint: str, token: str) -> None:
        self._root_endpoint = endpoint.rstrip("/")
        self._endpoint = self._root_endpoint + "/openevo-dev-agent/v1"
        self._v2_endpoint = self._root_endpoint + "/v2"
        self._token = token

    def request(self, path: str, *, method: str = "GET", body: object | None = None) -> object:
        return self._request_at(self._endpoint, path, method=method, body=body)

    def request_v2(self, path: str, *, method: str = "GET", body: object | None = None) -> object:
        return self._request_at(self._v2_endpoint, path, method=method, body=body)

    def _request_at(
        self,
        endpoint: str,
        path: str,
        *,
        method: str,
        body: object | None,
    ) -> object:
        data = None if body is None else json.dumps(body, separators=(",", ":")).encode()
        headers = {"Authorization": f"Bearer {self._token}"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(endpoint + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                declared_length = response.headers.get("Content-Length")
                if declared_length is not None and int(declared_length) > MAX_DEVELOPMENT_DAEMON_STATE_BYTES:
                    raise HTTPException(status_code=503, detail="development daemon response exceeds the bounded bridge limit")
                payload = response.read(MAX_DEVELOPMENT_DAEMON_STATE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            if endpoint == self._endpoint and exc.code == 410 and path.startswith("/events?"):
                raise DevelopmentDaemonEventCursorExpired from exc
            raise HTTPException(status_code=503, detail="development daemon is unavailable") from exc
        except OSError as exc:
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
        return self._proxy_at(
            self._endpoint,
            path,
            query=query,
            method=method,
            body=body,
            content_type=content_type,
        )

    def proxy_v2(
        self,
        path: str,
        *,
        query: str,
        method: str,
        body: bytes,
        content_type: str | None,
        content_sha256: str | None = None,
    ) -> tuple[int, bytes, Mapping[str, str]]:
        return self._proxy_at(
            self._v2_endpoint,
            path,
            query=query,
            method=method,
            body=body,
            content_type=content_type,
            content_sha256=content_sha256,
        )

    def _proxy_at(
        self,
        endpoint: str,
        path: str,
        *,
        query: str,
        method: str,
        body: bytes,
        content_type: str | None,
        content_sha256: str | None = None,
    ) -> tuple[int, bytes, Mapping[str, str]]:
        segments = path.split("/")
        if not path or any(segment in {"", ".", ".."} for segment in segments):
            raise HTTPException(status_code=404, detail="development daemon route not found")
        url = endpoint + "/" + quote(path, safe="/-._~")
        if query:
            url += "?" + query
        headers = {"Authorization": f"Bearer {self._token}"}
        if content_type:
            headers["Content-Type"] = content_type
        if content_sha256:
            headers["X-OpenEvo-Content-SHA256"] = content_sha256
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
                for name in (
                    "Content-Type",
                    "Content-Disposition",
                    "X-OpenEvo-Content-SHA256",
                )
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

    def _task_observations_v2(self) -> list[DevelopmentTaskObservationV2]:
        items: list[DevelopmentTaskObservationV2] = []
        after: str | None = None
        seen: set[str] = set()
        while True:
            query = "?limit=100"
            if after is not None:
                query += f"&after={quote(after, safe='')}"
            try:
                page = DevelopmentTaskObservationPageV2.model_validate(
                    self._client.request_v2(f"/tasks{query}")
                )
            except ValidationError as exc:
                raise HTTPException(
                    status_code=503,
                    detail="development daemon returned invalid v2 Task authority",
                ) from exc
            items.extend(page.items)
            if not page.has_more:
                return items
            if page.next_cursor is None or page.next_cursor in seen:
                raise HTTPException(
                    status_code=503,
                    detail="development daemon returned an invalid v2 Task cursor",
                )
            seen.add(page.next_cursor)
            after = page.next_cursor

    def _all_tasks(self) -> tuple[dict[str, object], list[core.TaskV2]]:
        state = self._remote_state(); projects = {p.project_id: p for p in self._project_models(state)}
        observations = self._task_observations_v2()
        raw_by_id = {raw["session_id"]: raw for raw in state.get("sessions", [])}
        tasks = []
        for index, observation in enumerate(observations):
            raw = raw_by_id.get(observation.task_id)
            project = projects.get(observation.project_id)
            if raw is None or project is None:
                raise HTTPException(status_code=503, detail="development daemon Task authority drifted across v1/v2 reads")
            task = self._task_model(raw, project, index + 1)
            tasks.append(task.model_copy(update={
                "state": observation.state,
                "created_at": observation.created_at,
                "updated_at": observation.updated_at,
            }))
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

    def _timeline(self, arguments: Mapping[str, object]) -> core.TimelinePageV2:
        task_id = str(arguments.get("task_id"))
        after = arguments.get("after")
        limit = arguments.get("limit", 100)
        query = f"?limit={limit}"
        if after is not None:
            query += f"&after={quote(str(after), safe='')}"
        try:
            page = DevelopmentTaskTimelinePageV2.model_validate(
                self._client.request_v2(
                    f"/tasks/{quote(task_id, safe='')}/timeline{query}"
                )
            )
        except ValidationError as exc:
            raise HTTPException(
                status_code=503,
                detail="development daemon returned an invalid v2 Task timeline",
            ) from exc

        task = self._task({"task_id": task_id})
        attempt = task.attempts[0]
        events: list[core.EventEnvelopeV2] = []
        for observation in page.items:
            if (
                observation.task_id != task.task_id
                or observation.project_id != task.project_id
            ):
                raise HTTPException(
                    status_code=503,
                    detail="development daemon Task timeline authority drifted",
                )
            common = {
                "schema_version": "2",
                "event_id": observation.event_id,
                "sequence": observation.sequence,
                "occurred_at": observation.occurred_at,
                "project_id": observation.project_id,
            }
            if observation.event_type == "task_admitted":
                events.append(core.TaskAdmittedEventV2(
                    **common,
                    event_type="task_admitted",
                    admission=task.admission,
                ))
            elif observation.event_type == "attempt_appended":
                events.append(core.AttemptAppendedEventV2(
                    **common,
                    event_type="attempt_appended",
                    attempt=attempt,
                ))
            else:
                events.append(core.DatasetSealedEventV2(
                    **common,
                    event_type="dataset_sealed",
                    task_id=task.task_id,
                    task_admission_id=task.admission.task_admission_id,
                    attempt_id=attempt.attempt_id,
                    dataset_id=observation.dataset_id,
                    dataset_sha256=observation.dataset_sha256,
                ))
        return core.TimelinePageV2(
            items=events,
            next_cursor=page.next_cursor,
            has_more=page.has_more,
        )

    def _logs(self, arguments: Mapping[str, object]) -> core.LogPageV2:
        task_id = str(arguments.get("task_id"))
        after = arguments.get("after")
        limit = arguments.get("limit", 100)
        query = f"?limit={limit}"
        if after is not None:
            query += f"&after={quote(str(after), safe='')}"
        try:
            return core.LogPageV2.model_validate(
                self._client.request_v2(f"/tasks/{quote(task_id, safe='')}/logs{query}")
            )
        except ValidationError as exc:
            raise HTTPException(
                status_code=503,
                detail="development daemon returned invalid v2 Task logs",
            ) from exc

    def _task_context(self, arguments: Mapping[str, object]) -> core.TaskContextV2:
        task = self._task(arguments)
        return core.TaskContextV2(task_id=task.task_id, task_admission_id=task.admission.task_admission_id,
                                  project_head=task.admission.predecessor_project_head, workspace_snapshot=task.admission.workspace_snapshot)

    def _task_artifacts(self, arguments: Mapping[str, object]) -> core.ArtifactPageV2:
        task_id = str(arguments.get("task_id"))
        query = f"?limit={arguments.get('limit', 100)}"
        if arguments.get("after") is not None:
            query += f"&after={quote(str(arguments['after']), safe='')}"
        try:
            return core.ArtifactPageV2.model_validate(
                self._client.request_v2(
                    f"/tasks/{quote(task_id, safe='')}/artifacts{query}"
                )
            )
        except ValidationError as exc:
            raise HTTPException(
                status_code=503,
                detail="development daemon returned invalid v2 Task artifacts",
            ) from exc

    def _artifact(self, arguments: Mapping[str, object]) -> core.ArtifactV2:
        artifact_id = quote(str(arguments.get("artifact_id")), safe="")
        try:
            return core.ArtifactV2.model_validate(
                self._client.request_v2(f"/artifacts/{artifact_id}")
            )
        except ValidationError as exc:
            raise HTTPException(
                status_code=503,
                detail="development daemon returned invalid v2 Artifact metadata",
            ) from exc

    def _artifact_content(self, arguments: Mapping[str, object]) -> core.ArtifactContentV2:
        artifact_id = quote(str(arguments.get("artifact_id")), safe="")
        try:
            return core.ArtifactContentV2.model_validate(
                self._client.request_v2(f"/artifacts/{artifact_id}/content")
            )
        except ValidationError as exc:
            raise HTTPException(
                status_code=503,
                detail="development daemon returned invalid v2 Artifact content metadata",
            ) from exc

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


class DevelopmentDaemonEventCursorExpired(RuntimeError):
    pass


class DevelopmentAgentStateEventRelay:
    """Relay the daemon's persistent event authority into Desktop v2 SSE.

    This is the development equivalent of nanobot's gateway hydration loop: the
    daemon owns mutation order and replay, while the Web Layer only authenticates
    the browser, projects an event into the closed Desktop v2 envelope, and
    reloads authoritative state. Event payloads never become a second copy of
    domain state.
    """

    def __init__(
        self,
        *,
        client: DevelopmentDaemonClient,
        provider: DevelopmentAgentDesktopV2Provider,
        broker: DesktopEventBrokerV2,
        wait_milliseconds: int = DAEMON_EVENT_WAIT_MILLISECONDS,
    ) -> None:
        if type(wait_milliseconds) is not int or not 0 <= wait_milliseconds <= 10_000:
            raise ValueError("development event wait is outside the supported bound")
        self._client = client
        self._provider = provider
        self._broker = broker
        self._wait_milliseconds = wait_milliseconds
        self._daemon_sequence: int | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def poll_once(self, *, wait_milliseconds: int | None = None) -> bool:
        wait = self._wait_milliseconds if wait_milliseconds is None else wait_milliseconds
        if type(wait) is not int or not 0 <= wait <= 10_000:
            raise ValueError("development event wait is outside the supported bound")
        if self._daemon_sequence is None:
            page = self._event_page(self._client.request("/events?limit=100"))
            self._daemon_sequence = page["latest_sequence"]
            self._refresh_state()
            return False
        try:
            page = self._event_page(
                self._client.request(
                    f"/events?after={self._daemon_sequence}&limit=100&wait_ms={wait}"
                )
            )
        except DevelopmentDaemonEventCursorExpired:
            return self._resynchronize_after_gap()
        events = page["events"]
        if events:
            # Refresh the provider cache before waking the renderer. Otherwise
            # a fast browser could consume the SSE notification and observe the
            # previous one-second cache entry.
            self._refresh_state()
        expected = self._daemon_sequence + 1
        for event in events:
            sequence = event["sequence"]
            if sequence != expected:
                raise RuntimeError("development daemon event sequence is not contiguous")
            expected += 1
            self._broker.publish(
                m.CoreAuthorityEventPayloadV2(
                    payload_kind="core_authority_changed",
                    profile_id=PROFILE_ID,
                    project_id=event["project_id"],
                    core_event_id=event["event_id"],
                    core_event_sequence=sequence,
                    core_event_type="transition_changed",
                    core_payload_sha256=event["payload_sha256"],
                )
            )
            self._daemon_sequence = sequence
        return bool(events)

    def _refresh_state(self) -> dict[str, object]:
        payload = self._client.request("/state")
        if not isinstance(payload, dict) or payload.get("schema_version") != "1":
            raise RuntimeError("development daemon returned an invalid state snapshot")
        self._provider.observe_remote_state(payload)
        return payload

    def _resynchronize_after_gap(self) -> bool:
        previous_sequence = self._daemon_sequence
        self._daemon_sequence = None
        page = self._event_page(self._client.request("/events?limit=100"))
        self._daemon_sequence = page["latest_sequence"]
        state = self._refresh_state()
        active_project_id = state.get("active_project_id")
        if previous_sequence is None or not isinstance(active_project_id, str) or not active_project_id:
            return False
        digest = _canonical_digest(state)
        self._broker.publish(
            m.CoreAuthorityEventPayloadV2(
                payload_kind="core_authority_changed",
                profile_id=PROFILE_ID,
                project_id=active_project_id,
                core_event_id=f"development-resync-{digest[:32]}",
                core_event_sequence=page["latest_sequence"],
                core_event_type="transition_changed",
                core_payload_sha256=digest,
            )
        )
        return True

    @staticmethod
    def _event_page(payload: object) -> dict[str, object]:
        if not isinstance(payload, dict) or set(payload) != {
            "schema_version",
            "events",
            "latest_sequence",
            "has_more",
        }:
            raise RuntimeError("development daemon returned an invalid event page")
        if payload.get("schema_version") != "1":
            raise RuntimeError("development daemon returned an incompatible event page")
        events = payload.get("events")
        latest_sequence = payload.get("latest_sequence")
        has_more = payload.get("has_more")
        if not isinstance(events, list) or len(events) > 100:
            raise RuntimeError("development daemon event page exceeds its bound")
        if type(latest_sequence) is not int or latest_sequence < 0:
            raise RuntimeError("development daemon event cursor is invalid")
        if type(has_more) is not bool:
            raise RuntimeError("development daemon event pagination flag is invalid")
        normalized: list[dict[str, object]] = []
        for event in events:
            if not isinstance(event, dict) or set(event) != {
                "sequence",
                "event_id",
                "project_id",
                "event_type",
                "payload_sha256",
                "occurred_at",
            }:
                raise RuntimeError("development daemon event is not a closed object")
            if type(event.get("sequence")) is not int or event["sequence"] < 1:
                raise RuntimeError("development daemon event sequence is invalid")
            if event.get("event_type") != "state_changed":
                raise RuntimeError("development daemon event type is unsupported")
            if not isinstance(event.get("event_id"), str):
                raise RuntimeError("development daemon event identity is invalid")
            if not isinstance(event.get("project_id"), str):
                raise RuntimeError("development daemon event project is invalid")
            digest = event.get("payload_sha256")
            if not isinstance(digest, str) or len(digest) != 64 or any(
                character not in "0123456789abcdef" for character in digest
            ):
                raise RuntimeError("development daemon event digest is invalid")
            if not isinstance(event.get("occurred_at"), str):
                raise RuntimeError("development daemon event timestamp is invalid")
            normalized.append(event)
        return {
            "schema_version": "1",
            "events": normalized,
            "latest_sequence": latest_sequence,
            "has_more": has_more,
        }

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
            thread.join(timeout=max(2.0, self._wait_milliseconds / 1000 + 2.0))
        self._broker.close()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception:
                LOGGER.exception("Development daemon event relay failed")
                self._stop.wait(1.0)


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

    @app.get(
        "/desktop/v2/development/projects/{project_id}/workspace",
        include_in_schema=False,
    )
    async def development_workspace_inventory(project_id: str, request: Request) -> Response:
        provider._find_project(project_id)
        status, payload, headers = provider._client.proxy_v2(
            f"projects/{quote(project_id, safe='')}/workspace",
            query=request.url.query,
            method="GET",
            body=b"",
            content_type=None,
        )
        if status == HTTPStatus.OK:
            try:
                validated = DevelopmentWorkspacePageV2.model_validate_json(payload)
            except ValidationError as exc:
                raise HTTPException(
                    status_code=503,
                    detail="development daemon returned an invalid workspace inventory",
                ) from exc
            payload = validated.model_dump_json().encode("utf-8")
        return Response(content=payload, status_code=status, headers=dict(headers))

    @app.api_route(
        "/desktop/v2/development/projects/{project_id}/workspace/files",
        methods=["GET", "PUT", "DELETE"],
        include_in_schema=False,
    )
    async def development_workspace_file(project_id: str, request: Request) -> Response:
        provider._find_project(project_id)
        body = bytearray()
        if request.method == "PUT":
            async for chunk in request.stream():
                if len(chunk) > MAX_DEVELOPMENT_WORKSPACE_UPLOAD_BYTES - len(body):
                    return _desktop_error_response(
                        status=413,
                        code="desktop_request_too_large",
                        summary="The workspace upload exceeds the 32 MiB development limit.",
                        retryable=False,
                        action="none",
                    )
                body.extend(chunk)
        content_sha256 = (
            hashlib.sha256(body).hexdigest() if request.method == "PUT" else None
        )
        status, payload, headers = provider._client.proxy_v2(
            f"projects/{quote(project_id, safe='')}/workspace/files",
            query=request.url.query,
            method=request.method,
            body=bytes(body),
            content_type=request.headers.get("Content-Type"),
            content_sha256=content_sha256,
        )
        if status in {HTTPStatus.OK, HTTPStatus.CREATED} and request.method in {
            "PUT",
            "DELETE",
        }:
            model = (
                DevelopmentWorkspaceMutationV2
                if request.method == "PUT"
                else DevelopmentWorkspaceDeleteV2
            )
            try:
                validated = model.model_validate_json(payload)
            except ValidationError as exc:
                raise HTTPException(
                    status_code=503,
                    detail="development daemon returned an invalid workspace mutation",
                ) from exc
            payload = validated.model_dump_json().encode("utf-8")
        return Response(content=payload, status_code=status, headers=dict(headers))

    @app.get(
        "/desktop/v2/development/artifacts",
        include_in_schema=False,
    )
    async def development_artifact_inventory(request: Request) -> Response:
        project_id = request.query_params.get("project_id")
        if project_id is None:
            raise HTTPException(
                status_code=422,
                detail="development artifact inventory requires project_id",
            )
        provider._find_project(project_id)
        status, payload, headers = provider._client.proxy_v2(
            "development/artifacts",
            query=request.url.query,
            method="GET",
            body=b"",
            content_type=None,
        )
        if status == HTTPStatus.OK:
            try:
                validated = DevelopmentArtifactPageV2.model_validate_json(payload)
            except ValidationError as exc:
                raise HTTPException(
                    status_code=503,
                    detail="development daemon returned an invalid artifact inventory",
                ) from exc
            if any(item.project_id != project_id for item in validated.items):
                raise HTTPException(
                    status_code=503,
                    detail="development daemon artifact inventory crossed project authority",
                )
            payload = validated.model_dump_json().encode("utf-8")
        return Response(content=payload, status_code=status, headers=dict(headers))

    @app.get(
        "/desktop/v2/development/artifacts/{artifact_id}",
        include_in_schema=False,
    )
    async def development_artifact_detail(artifact_id: str) -> Response:
        status, payload, headers = provider._client.proxy_v2(
            f"development/artifacts/{quote(artifact_id, safe='')}",
            query="",
            method="GET",
            body=b"",
            content_type=None,
        )
        if status == HTTPStatus.OK:
            try:
                validated = DevelopmentArtifactV2.model_validate_json(payload)
            except ValidationError as exc:
                raise HTTPException(
                    status_code=503,
                    detail="development daemon returned invalid artifact detail",
                ) from exc
            provider._find_project(validated.project_id)
            payload = validated.model_dump_json().encode("utf-8")
        return Response(content=payload, status_code=status, headers=dict(headers))

    @app.api_route(
        "/desktop/v2/development/evolution-runs",
        methods=["GET", "POST"],
        include_in_schema=False,
    )
    async def development_evolution_runs(request: Request) -> Response:
        body = bytearray()
        if request.method == "POST":
            async for chunk in request.stream():
                if len(chunk) > MAX_DEVELOPMENT_EVOLUTION_REQUEST_BYTES - len(body):
                    return _desktop_error_response(
                        status=413,
                        code="desktop_request_too_large",
                        summary="The Evolution Run request exceeds the development limit.",
                        retryable=False,
                        action="none",
                    )
                body.extend(chunk)
            try:
                creation = DevelopmentEvolutionRunCreateV2.model_validate_json(body)
            except ValidationError as exc:
                raise HTTPException(
                    status_code=422,
                    detail="Evolution Run request did not match the closed v2 contract",
                ) from exc
            provider._find_project(creation.project_id)
            body = bytearray(creation.model_dump_json().encode("utf-8"))
        else:
            project_id = request.query_params.get("project_id")
            if project_id is None:
                raise HTTPException(
                    status_code=422,
                    detail="Evolution Run inventory requires project_id",
                )
            provider._find_project(project_id)

        status, payload, headers = provider._client.proxy_v2(
            "development/evolution-runs",
            query=request.url.query if request.method == "GET" else "",
            method=request.method,
            body=bytes(body),
            content_type="application/json" if request.method == "POST" else None,
        )
        if status in {HTTPStatus.OK, HTTPStatus.ACCEPTED}:
            try:
                if request.method == "GET":
                    validated = DevelopmentEvolutionRunPageV2.model_validate_json(payload)
                    expected_project_id = request.query_params["project_id"]
                    if any(
                        item.project_id != expected_project_id
                        for item in validated.items
                    ):
                        raise ValueError(
                            "Evolution Run inventory crossed project authority"
                        )
                else:
                    validated = DevelopmentEvolutionRunV2.model_validate_json(payload)
                    if validated.project_id != creation.project_id:
                        raise ValueError(
                            "Evolution Run creation crossed project authority"
                        )
            except (ValidationError, ValueError) as exc:
                raise HTTPException(
                    status_code=503,
                    detail="development daemon returned an invalid Evolution Run payload",
                ) from exc
            payload = validated.model_dump_json().encode("utf-8")
        return Response(content=payload, status_code=status, headers=dict(headers))

    @app.get(
        "/desktop/v2/development/evolution-runs/{run_id}",
        include_in_schema=False,
    )
    async def development_evolution_run_detail(run_id: str) -> Response:
        status, payload, headers = provider._client.proxy_v2(
            f"development/evolution-runs/{quote(run_id, safe='')}",
            query="",
            method="GET",
            body=b"",
            content_type=None,
        )
        if status == HTTPStatus.OK:
            try:
                validated = DevelopmentEvolutionRunV2.model_validate_json(payload)
            except ValidationError as exc:
                raise HTTPException(
                    status_code=503,
                    detail="development daemon returned invalid Evolution Run detail",
                ) from exc
            provider._find_project(validated.project_id)
            payload = validated.model_dump_json().encode("utf-8")
        return Response(content=payload, status_code=status, headers=dict(headers))

    @app.post(
        "/desktop/v2/development/evolution-runs/{run_id}/apply",
        include_in_schema=False,
    )
    async def development_evolution_run_apply(run_id: str, request: Request) -> Response:
        body = bytearray()
        async for chunk in request.stream():
            if len(chunk) > MAX_DEVELOPMENT_EVOLUTION_REQUEST_BYTES - len(body):
                return _desktop_error_response(
                    status=413,
                    code="desktop_request_too_large",
                    summary="The Evolution apply request exceeds the development limit.",
                    retryable=False,
                    action="none",
                )
            body.extend(chunk)
        try:
            apply_request = DevelopmentEvolutionRunApplyV2.model_validate_json(body)
        except ValidationError as exc:
            raise HTTPException(
                status_code=422,
                detail="Evolution apply request did not match the closed v2 contract",
            ) from exc
        status, payload, headers = provider._client.proxy_v2(
            f"development/evolution-runs/{quote(run_id, safe='')}/apply",
            query="",
            method="POST",
            body=apply_request.model_dump_json().encode("utf-8"),
            content_type="application/json",
        )
        if status == HTTPStatus.OK:
            try:
                validated = DevelopmentEvolutionRunV2.model_validate_json(payload)
            except ValidationError as exc:
                raise HTTPException(
                    status_code=503,
                    detail="development daemon returned invalid applied Evolution Run",
                ) from exc
            provider._find_project(validated.project_id)
            if validated.run_id != run_id or validated.state != "applied":
                raise HTTPException(
                    status_code=503,
                    detail="development daemon returned inconsistent Evolution apply authority",
                )
            payload = validated.model_dump_json().encode("utf-8")
        return Response(content=payload, status_code=status, headers=dict(headers))

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
