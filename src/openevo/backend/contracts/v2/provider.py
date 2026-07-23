"""Authoritative Core Control API v2 provider.

The provider is intentionally a thin projection over durable Core owners.  It does
not synthesize task results, project heads, artifacts, logs, or successor state when
the corresponding authority is not wired; unfinished surfaces fail closed with a
typed 503 response.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
import hashlib
import json
import secrets
import threading
from typing import Literal

from fastapi.responses import JSONResponse, Response, StreamingResponse

from openevo.backend.run_control import CoreTaskControlError
from openevo.backend.project_authority_v2 import (
    ProjectAuthorityConflictV2,
    ProjectAuthorityInvalidV2,
    ProjectAuthorityV2,
    ProjectAuthorityV2Error,
)
from openevo.backend.science_run_owner import CoreScienceTaskOwnerV2
from openevo.backend.science_run_store import (
    ScienceProjectAdmissionAuthorityV2,
    page_items,
)
from openevo.backend.workspace_store_v2 import (
    WorkspaceConflictV2,
    WorkspaceIdempotencyConflictV2,
    WorkspaceIntegrityErrorV2,
    WorkspaceNotFoundV2,
    WorkspacePreconditionFailedV2,
    WorkspaceStoreV2Error,
)
from openevo.evolution.framework.builtins import (
    VerifiedExecutableRegistry,
    require_verified_executable_registry,
)
from openevo.evolution.framework.capabilities import (
    CapabilityAudience,
    build_evolution_capabilities,
)
from openevo.evolution.framework.profiles import execution_profile_for_release_mode

from . import models as m
from .app import CoreControlHTTPErrorV2
from .snapshots import (
    EVENTS_SCHEMA_SNAPSHOT_PATH,
    OPENAPI_SNAPSHOT_PATH,
    events_schema_sha256,
    openapi_sha256,
)
from .store import (
    CoreControlStoreV2,
    CoreControlStoreV2Error,
    OperationNotFoundV2,
    ProjectConflictV2,
    ProjectIdempotencyConflictV2,
    ProjectNotFoundV2,
    ProjectPreconditionFailedV2,
    ProjectRecordV2,
    operation_etag_for,
    project_etag_payload,
)


_BASE_FEATURE_FLAGS = [
    "event_replay_v2",
    "project_heads_v2",
    "task_admission_v2",
    "verified_capabilities",
    "verified_registry",
]
_PROJECT_OPERATIONS = frozenset(
    {
        "createCoreProjectV2",
        "getCoreActiveProjectHeadV2",
        "getCoreProjectHeadV2",
        "getCoreProjectV2",
        "listCoreProjectHeadsV2",
        "listCoreProjectsV2",
        "updateCoreProjectV2",
        "validateCoreProjectV2",
    }
)
_WORKSPACE_OPERATIONS = frozenset(
    {
        "abortCoreWorkspaceUploadV2",
        "createCoreWorkspaceUploadV2",
        "finalizeCoreWorkspaceUploadV2",
        "getCoreWorkspaceUploadV2",
        "putCoreWorkspaceUploadChunkV2",
    }
)
_TRANSITION_OPERATIONS = frozenset(
    {
        "abandonCoreSuccessorTransitionV2",
        "getCoreSuccessorTransitionV2",
        "listCoreSuccessorTransitionsV2",
        "retryCoreSuccessorTransitionV2",
    }
)
_ARTIFACT_OPERATIONS = frozenset(
    {
        "getCoreArtifactContentV2",
        "getCoreArtifactV2",
        "listCoreTaskArtifactsV2",
    }
)
_SERVICE_OPERATIONS = frozenset(
    {
        "getCoreServiceLogsV2",
        "getCoreServiceV2",
        "listCoreServicesV2",
        "restartCoreServiceV2",
    }
)


class CoreControlProviderV2:
    """Serve the frozen v2 contract from exact Core-owned durable state."""

    def __init__(
        self,
        store: CoreControlStoreV2,
        *,
        task_owner: CoreScienceTaskOwnerV2,
        executable_registry: VerifiedExecutableRegistry,
        project_authority: ProjectAuthorityV2 | None = None,
        bearer_token: str,
        release_version: str,
        source_commit: str,
        build_channel: Literal["release", "development", "test"],
        runtime_contract_sha256: str,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        try:
            token_bytes = bearer_token.encode("ascii")
        except UnicodeEncodeError as exc:
            raise ValueError("Core v2 bearer token must be ASCII") from exc
        if not token_bytes or any(value <= 32 or value == 127 for value in token_bytes):
            raise ValueError(
                "Core v2 bearer token must be non-empty and contain no whitespace"
            )
        self.store = store
        self._task_owner = task_owner
        self._registry = require_verified_executable_registry(executable_registry)
        if project_authority is not None and type(project_authority) is not ProjectAuthorityV2:
            raise TypeError("Core v2 project authority has the wrong type")
        self._project_authority = project_authority
        self._authorization = b"Bearer " + token_bytes
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.RLock()
        self._closed = False
        self._release_version = release_version
        self._source_commit = source_commit
        self._build_channel = build_channel
        self._registry_sha256 = _digest(
            self._registry.snapshot.registry_digest,
            label="registry",
        )
        self._runtime_contract_sha256 = _digest(
            runtime_contract_sha256,
            label="runtime contract",
        )
        _require_exact_schema_snapshots()
        self._started_at = _timestamp(self._clock())
        feature_flags = sorted(
            [
                *_BASE_FEATURE_FLAGS,
                *(
                    ["atomic_successor_v2"]
                    if task_owner.successor_available
                    else []
                ),
                *(
                    ["project_genesis_v2", "workspace_snapshots_v2"]
                    if project_authority is not None
                    else []
                ),
            ]
        )
        feature_json = json.dumps(
            feature_flags,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
        feature_sha256 = hashlib.sha256(feature_json).hexdigest()
        build_payload = json.dumps(
            {
                "build_channel": build_channel,
                "events_schema_sha256": events_schema_sha256(),
                "feature_set_sha256": feature_sha256,
                "openapi_sha256": openapi_sha256(),
                "registry_sha256": self._registry_sha256,
                "release_version": release_version,
                "runtime_contract_sha256": self._runtime_contract_sha256,
                "source_commit": source_commit,
            },
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        self._version = m.VersionResponseV2(
            api_name="openevo-core-control-api",
            preferred_major=2,
            supported_majors=[2],
            mutation_major=2,
            contracts=[
                m.ContractOfferV2(
                    api_major=2,
                    openapi_sha256=openapi_sha256(),
                    event_schema_sha256=events_schema_sha256(),
                    access="mutation",
                    mutation_compatible=True,
                ),
            ],
            release_version=release_version,
            build_id=hashlib.sha256(build_payload).hexdigest(),
            source_commit=source_commit,
            build_channel=build_channel,
            provider_kind="openevo_daemon",
            feature_flags=feature_flags,
            feature_set_sha256=feature_sha256,
            registry_sha256=self._registry_sha256,
            runtime_contract_sha256=self._runtime_contract_sha256,
            mutation_compatible=True,
        )
        self._handlers: dict[
            str, Callable[[Mapping[str, object]], object]
        ] = {
            "discoverCoreContractVersionV2": self._version_response,
            "discoverCoreHealthV2": self._health,
            "getCoreSystemStatusV2": self._system_status,
            "getCoreCapabilitiesV2": self._capabilities,
            "listCoreProjectsV2": self._list_projects,
            "createCoreProjectV2": self._create_project,
            "getCoreProjectV2": self._get_project,
            "listCoreProjectHeadsV2": self._list_project_heads,
            "getCoreActiveProjectHeadV2": self._get_active_project_head,
            "getCoreProjectHeadV2": self._get_project_head,
            "listCoreSuccessorTransitionsV2": self._list_transitions,
            "getCoreSuccessorTransitionV2": self._get_transition,
            "listCoreTasksV2": self._list_tasks,
            "submitCoreTaskV2": self._submit_task,
            "getCoreTaskV2": self._get_task,
            "getCoreTaskAdmissionV2": self._get_task_admission,
            "listCoreTaskAttemptsV2": self._list_task_attempts,
            "appendCoreTaskAttemptV2": self._append_task_attempt,
            "getCoreTaskAttemptV2": self._get_task_attempt,
            "getCoreTaskTimelineV2": self._task_timeline,
            "getCoreTaskContextV2": self._task_context,
            "closeCoreTaskV2": self._close_task,
            "listCoreServicesV2": self._list_services,
            "getCoreServiceV2": self._get_service,
            "getCoreOperationV2": self._get_operation,
            "streamCoreEventsV2": self._events,
        }
        if project_authority is not None:
            self._handlers.update(
                {
                    "abortCoreWorkspaceUploadV2": self._abort_workspace_upload,
                    "createCoreWorkspaceUploadV2": self._create_workspace_upload,
                    "finalizeCoreWorkspaceUploadV2": self._finalize_workspace_upload,
                    "getCoreWorkspaceUploadV2": self._get_workspace_upload,
                    "putCoreWorkspaceUploadChunkV2": self._put_workspace_chunk,
                    "updateCoreProjectV2": self._update_project,
                    "validateCoreProjectV2": self._validate_project,
                }
            )
        self._all_operation_ids = frozenset(
            {
                *self._handlers,
                "retryCoreSuccessorTransitionV2",
                "abandonCoreSuccessorTransitionV2",
                "cancelCoreTaskAttemptV2",
                "getCoreTaskLogsV2",
                "listCoreTaskArtifactsV2",
                "getCoreArtifactV2",
                "getCoreArtifactContentV2",
                "restartCoreServiceV2",
                "getCoreServiceLogsV2",
                "cancelCoreOperationV2",
                "createCoreDiagnosticV2",
                "getCoreDiagnosticV2",
                "deleteCoreDiagnosticV2",
                "cleanupCoreCachesV2",
                "validateCoreProjectV2",
                "updateCoreProjectV2",
                *_WORKSPACE_OPERATIONS,
            }
        )

    @property
    def operation_ids(self) -> frozenset[str]:
        return self._all_operation_ids

    def authenticate(self, authorization_values: tuple[bytes, ...]) -> bool:
        return len(authorization_values) == 1 and secrets.compare_digest(
            authorization_values[0], self._authorization
        )

    def publish_project_admission_authority(
        self,
        *,
        display_name: str,
        config: m.ScienceProjectConfigV2,
        authority: ScienceProjectAdmissionAuthorityV2,
        expected_project_head_id: str | None = None,
    ) -> m.ProjectV2:
        self._ensure_open()
        head = authority.active_project_head
        if (
            authority.project_id != head.project_id
            or authority.project_config_sha256
            != m.project_config_sha256_for(config)
            or head.registry_sha256 != self._registry_sha256
            or head.runtime_context_snapshot.runtime_contract_sha256
            != self._runtime_contract_sha256
        ):
            raise ValueError(
                "project admission authority does not match negotiated Core digests"
            )
        self.store.upsert_authoritative_project(
            project_id=authority.project_id,
            display_name=display_name,
            config=config,
            now=self._clock(),
        )
        self._task_owner.publish_project_admission_authority(
            authority,
            expected_project_head_id=expected_project_head_id,
        )
        return self._project_model(self.store.get_project(authority.project_id))

    def invoke(self, operation_id: str, arguments: Mapping[str, object]) -> object:
        self._ensure_open()
        handler = self._handlers.get(operation_id)
        if handler is None:
            if operation_id not in self._all_operation_ids:
                raise _http_error(
                    500,
                    code="provider_route_unowned",
                    message="The provider does not own the requested frozen operation.",
                    category="internal",
                    retryable=False,
                    repair_action="repair",
                )
            raise self._feature_not_ready(operation_id)
        try:
            return handler(arguments)
        except CoreControlHTTPErrorV2:
            raise
        except CoreTaskControlError as exc:
            raise _task_owner_http_error(exc, operation_id=operation_id) from exc
        except WorkspaceIdempotencyConflictV2 as exc:
            raise _http_error(
                409,
                code="workspace_idempotency_key_reused",
                message="The workspace idempotency key was reused for another request.",
                category="project",
                retryable=False,
                repair_action="user_action_required",
            ) from exc
        except WorkspacePreconditionFailedV2 as exc:
            raise _http_error(
                412,
                code="workspace_authority_changed",
                message="The workspace upload authority changed before this action.",
                category="project",
                retryable=True,
                repair_action="retry",
            ) from exc
        except WorkspaceNotFoundV2 as exc:
            raise _http_error(
                404,
                code="workspace_upload_not_found",
                message="The requested workspace upload was not found.",
                category="project",
                retryable=False,
                repair_action="user_action_required",
            ) from exc
        except WorkspaceConflictV2 as exc:
            raise _http_error(
                409,
                code="workspace_conflict",
                message="The workspace mutation conflicts with durable Core state.",
                category="project",
                retryable=False,
                repair_action="repair",
            ) from exc
        except WorkspaceIntegrityErrorV2 as exc:
            raise _http_error(
                422,
                code="workspace_archive_invalid",
                message="The workspace archive failed closed validation.",
                category="contract",
                retryable=False,
                repair_action="reconfigure",
            ) from exc
        except WorkspaceStoreV2Error as exc:
            raise _http_error(
                503,
                code="workspace_authority_unavailable",
                message="The durable workspace authority is unavailable.",
                category="system",
                retryable=True,
                repair_action="retry",
            ) from exc
        except ProjectAuthorityInvalidV2 as exc:
            raise _http_error(
                409,
                code="evolution_project_invalid",
                message="The saved project evolution configuration is invalid.",
                category="project",
                retryable=False,
                repair_action="reconfigure",
            ) from exc
        except ProjectAuthorityConflictV2 as exc:
            raise _http_error(
                412,
                code="project_authority_changed",
                message="The project authority changed before this action.",
                category="project",
                retryable=True,
                repair_action="retry",
            ) from exc
        except ProjectAuthorityV2Error as exc:
            raise _http_error(
                503,
                code="project_authority_unavailable",
                message="The durable project authority is unavailable.",
                category="system",
                retryable=True,
                repair_action="retry",
            ) from exc
        except ProjectIdempotencyConflictV2 as exc:
            raise _http_error(
                409,
                code="project_idempotency_key_reused",
                message="The idempotency key was used for another project request.",
                category="project",
                retryable=False,
                repair_action="user_action_required",
            ) from exc
        except ProjectPreconditionFailedV2 as exc:
            raise _http_error(
                412,
                code="project_authority_changed",
                message="The project authority changed before this mutation.",
                category="project",
                retryable=True,
                repair_action="retry",
            ) from exc
        except OperationNotFoundV2 as exc:
            raise _http_error(
                404,
                code="operation_not_found",
                message="The requested Core operation was not found.",
                category="system",
                retryable=False,
                repair_action="user_action_required",
            ) from exc
        except ProjectNotFoundV2 as exc:
            raise _http_error(
                404,
                code="project_not_found",
                message="The requested project was not found.",
                category="project",
                retryable=False,
                repair_action="user_action_required",
            ) from exc
        except ProjectConflictV2 as exc:
            raise _http_error(
                409,
                code="project_conflict",
                message="The project mutation conflicts with durable Core state.",
                category="project",
                retryable=False,
                repair_action="repair",
            ) from exc
        except CoreControlStoreV2Error as exc:
            raise _http_error(
                503,
                code="project_catalog_unavailable",
                message="The durable Core project catalog is unavailable.",
                category="system",
                retryable=True,
                repair_action="retry",
            ) from exc
        except (KeyError, TypeError, ValueError) as exc:
            raise _http_error(
                422,
                code="provider_request_invalid",
                message="The request does not satisfy the closed provider operation.",
                category="contract",
                retryable=False,
                repair_action="reconfigure",
            ) from exc

    async def invoke_async(
        self,
        operation_id: str,
        arguments: Mapping[str, object],
    ) -> object:
        return await asyncio.to_thread(self.invoke, operation_id, arguments)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        try:
            if self._project_authority is not None:
                self._project_authority.close()
            self._task_owner.close()
        finally:
            self.store.close()

    async def aclose(self) -> None:
        await asyncio.to_thread(self.close)

    def _version_response(self, arguments: Mapping[str, object]) -> m.VersionResponseV2:
        _keys(arguments)
        return self._version

    def _health(self, arguments: Mapping[str, object]) -> m.HealthResponseV2:
        _keys(arguments)
        return m.HealthResponseV2(status="healthy", checked_at=_timestamp(self._clock()))

    def _system_status(self, arguments: Mapping[str, object]) -> m.SystemStatusV2:
        _keys(arguments)
        return m.SystemStatusV2(
            status="ready",
            release_version=self._release_version,
            source_commit=self._source_commit,
            registry_sha256=self._registry_sha256,
            checked_at=_timestamp(self._clock()),
        )

    def _capabilities(self, arguments: Mapping[str, object]) -> object:
        _keys(arguments, "execution_mode")
        execution_mode = _string(arguments["execution_mode"])
        if execution_mode != "codex_subscription_transcript":
            raise _http_error(
                503,
                code="execution_mode_unavailable",
                message="The requested execution mode is unavailable in this release.",
                category="project",
                retryable=False,
                repair_action="unsupported",
            )
        return build_evolution_capabilities(
            self._registry.snapshot,
            profile=execution_profile_for_release_mode(execution_mode),
            audience=CapabilityAudience.DESKTOP,
            core_version=self._release_version,
        )

    def _create_project(self, arguments: Mapping[str, object]) -> object:
        _keys(arguments, "request", "idempotency_key")
        request = _model(m.ProjectCreateV2, arguments["request"])
        if self._project_authority is not None:
            self._project_authority.validate_config(request.config)
        record, _replayed = self.store.create_project(
            request,
            idempotency_key=_string(arguments["idempotency_key"]),
            now=self._clock(),
        )
        if self._project_authority is not None:
            self._project_authority.ensure_project(record)
        project = self._project_model(record)
        return JSONResponse(
            status_code=201,
            content=project.model_dump(mode="json"),
            headers={"ETag": project.etag},
        )

    def _list_projects(self, arguments: Mapping[str, object]) -> m.ProjectPageV2:
        _keys(arguments, "limit", "after", "direction")
        records = self.store.list_projects()
        records.sort(key=lambda item: (item.created_at, item.project_id))
        direction = _direction(arguments["direction"])
        if direction == "desc":
            records.reverse()
        selected, next_cursor, has_more = _page_items(
            records,
            limit=_limit(arguments["limit"]),
            after=_optional_string(arguments["after"]),
            query=f"projects:{direction}",
        )
        return m.ProjectPageV2(
            items=[self._project_model(record) for record in selected],
            next_cursor=next_cursor,
            has_more=has_more,
        )

    def _get_project(self, arguments: Mapping[str, object]) -> Response:
        _keys(arguments, "project_id")
        project = self._project_model(
            self.store.get_project(_string(arguments["project_id"]))
        )
        return JSONResponse(
            content=project.model_dump(mode="json"),
            headers={"ETag": project.etag},
        )

    def _project_model(self, record: ProjectRecordV2) -> m.ProjectV2:
        if self._project_authority is not None:
            self._project_authority.ensure_project(record)
        active: m.ProjectHeadRefV2 | None = None
        admission_etag: str | None = None
        state: Literal["ready", "transitioning", "not_ready", "needs_attention"]
        try:
            authority = self._task_owner.project_admission_authority(record.project_id)
        except CoreTaskControlError as exc:
            if exc.code != "task_not_found":
                raise
            state = "not_ready"
        else:
            if authority.project_config_sha256 != record.project_config_sha256:
                raise CoreControlStoreV2Error(
                    "project catalog and admission authority digests differ"
                )
            active = authority.active_project_head
            admission_etag = authority.project_etag
            negotiated_digest_drift = (
                active.registry_sha256 != self._registry_sha256
                or active.runtime_context_snapshot.runtime_contract_sha256
                != self._runtime_contract_sha256
            )
            state = (
                "transitioning"
                if authority.blockers
                else "not_ready" if negotiated_digest_drift else "ready"
            )
            if authority.blockers:
                transitions = self._task_owner.list_successor_transitions(
                    record.project_id
                )
                if (
                    transitions
                    and max(transitions, key=lambda item: item.created_at).state
                    == "failed"
                ):
                    state = "needs_attention"
        if self._project_authority is not None:
            readiness = self._project_authority.readiness(record)
            if not readiness.ready and state == "ready":
                state = "not_ready"
        etag = project_etag_payload(
            record,
            active_project_head=active,
            admission_etag=admission_etag,
            state=state,
        )
        return m.ProjectV2(
            project_id=record.project_id,
            display_name=record.display_name,
            config=record.config,
            project_config_sha256=record.project_config_sha256,
            active_project_head=active,
            admission_etag=admission_etag,
            state=state,
            created_at=record.created_at,
            updated_at=record.updated_at,
            etag=etag,
        )

    def _update_project(self, arguments: Mapping[str, object]) -> object:
        _keys(
            arguments,
            "project_id",
            "request",
            "if_match",
            "idempotency_key",
        )
        authority = self._require_project_authority()
        project_id = _string(arguments["project_id"])
        request = _model(m.ProjectUpdateV2, arguments["request"])
        authority.validate_config(request.config)
        current_record = self.store.get_project(project_id)
        current = self._project_model(current_record)
        head = current.active_project_head
        if (
            request.expected_project_head_id
            != (None if head is None else head.project_head_id)
            or request.expected_project_head_manifest_sha256
            != (None if head is None else head.manifest_sha256)
        ):
            raise ProjectAuthorityConflictV2("project head changed")
        if request.config != current_record.config:
            raise _http_error(
                409,
                code="project_update_requires_successor",
                message=(
                    "Changing a pinned project configuration requires an atomic "
                    "successor transition."
                ),
                category="project",
                retryable=False,
                repair_action="repair",
            )
        updated, _replayed = self.store.update_project(
            project_id,
            request,
            if_match=_string(arguments["if_match"]),
            current_etag=current.etag,
            idempotency_key=_string(arguments["idempotency_key"]),
            now=self._clock(),
        )
        project = self._project_model(updated)
        return JSONResponse(
            content=project.model_dump(mode="json"),
            headers={"ETag": project.etag},
        )

    def _create_workspace_upload(self, arguments: Mapping[str, object]) -> object:
        _keys(
            arguments,
            "project_id",
            "request",
            "if_match",
            "idempotency_key",
        )
        authority = self._require_project_authority()
        record = self.store.get_project(_string(arguments["project_id"]))
        self._require_project_etag(record, _string(arguments["if_match"]))
        session, _replayed = authority.create_workspace_upload(
            record,
            _model(m.WorkspaceUploadCreateV2, arguments["request"]),
            idempotency_key=_string(arguments["idempotency_key"]),
            now=self._clock(),
        )
        return session

    def _get_workspace_upload(self, arguments: Mapping[str, object]) -> object:
        _keys(arguments, "project_id", "upload_id")
        authority = self._require_project_authority()
        record = self.store.get_project(_string(arguments["project_id"]))
        session = authority.get_workspace_upload(
            record,
            _string(arguments["upload_id"]),
        )
        return JSONResponse(
            content=session.model_dump(mode="json"),
            headers={"ETag": session.etag},
        )

    def _put_workspace_chunk(self, arguments: Mapping[str, object]) -> object:
        _keys(
            arguments,
            "project_id",
            "upload_id",
            "chunk_index",
            "chunk",
            "chunk_sha256",
            "chunk_byte_size",
            "if_match",
            "idempotency_key",
        )
        authority = self._require_project_authority()
        record = self.store.get_project(_string(arguments["project_id"]))
        chunk = arguments["chunk"]
        if type(chunk) is not bytes:
            raise TypeError("workspace chunk must be exact bytes")
        session, _replayed = authority.put_workspace_chunk(
            record,
            _string(arguments["upload_id"]),
            chunk_index=arguments["chunk_index"],  # type: ignore[arg-type]
            chunk=chunk,
            chunk_sha256=_string(arguments["chunk_sha256"]),
            chunk_byte_size=arguments["chunk_byte_size"],  # type: ignore[arg-type]
            if_match=_string(arguments["if_match"]),
            idempotency_key=_string(arguments["idempotency_key"]),
            now=self._clock(),
        )
        return JSONResponse(
            content=session.model_dump(mode="json"),
            headers={"ETag": session.etag},
        )

    def _finalize_workspace_upload(self, arguments: Mapping[str, object]) -> object:
        _keys(
            arguments,
            "project_id",
            "upload_id",
            "request",
            "if_match",
            "idempotency_key",
        )
        authority = self._require_project_authority()
        record = self.store.get_project(_string(arguments["project_id"]))
        session, _replayed = authority.finalize_workspace_upload(
            record,
            _string(arguments["upload_id"]),
            _model(m.WorkspaceUploadFinalizeV2, arguments["request"]),
            if_match=_string(arguments["if_match"]),
            idempotency_key=_string(arguments["idempotency_key"]),
            now=self._clock(),
        )
        return session

    def _abort_workspace_upload(self, arguments: Mapping[str, object]) -> object:
        _keys(
            arguments,
            "project_id",
            "upload_id",
            "request",
            "if_match",
            "idempotency_key",
        )
        authority = self._require_project_authority()
        record = self.store.get_project(_string(arguments["project_id"]))
        session, _replayed = authority.abort_workspace_upload(
            record,
            _string(arguments["upload_id"]),
            _model(m.WorkspaceUploadAbortV2, arguments["request"]),
            if_match=_string(arguments["if_match"]),
            idempotency_key=_string(arguments["idempotency_key"]),
            now=self._clock(),
        )
        return JSONResponse(
            content=session.model_dump(mode="json"),
            headers={"ETag": session.etag},
        )

    def _validate_project(self, arguments: Mapping[str, object]) -> object:
        _keys(arguments, "project_id", "request", "idempotency_key")
        authority = self._require_project_authority()
        project_id = _string(arguments["project_id"])
        idempotency_key = _string(arguments["idempotency_key"])
        request = _model(m.ProjectValidationRequestV2, arguments["request"])
        replay = self.store.begin_project_validation(
            project_id,
            request,
            idempotency_key=idempotency_key,
        )
        if replay is not None:
            return replay
        record = self.store.get_project(project_id)
        response = authority.validate_project(
            record,
            request,
            now=self._clock(),
        )
        return self.store.commit_project_validation(
            project_id,
            request,
            response,
            idempotency_key=idempotency_key,
        )

    def _require_project_authority(self) -> ProjectAuthorityV2:
        if self._project_authority is None:
            raise ProjectAuthorityV2Error("project authority is not configured")
        return self._project_authority

    def _require_project_etag(self, record: ProjectRecordV2, expected: str) -> None:
        project = self._project_model(record)
        if project.etag != expected:
            raise ProjectAuthorityConflictV2("project resource ETag changed")

    def _list_project_heads(self, arguments: Mapping[str, object]) -> m.ProjectHeadPageV2:
        _keys(arguments, "project_id", "limit", "after", "direction")
        project_id = _string(arguments["project_id"])
        self.store.get_project(project_id)
        heads = self._task_owner.list_project_heads(project_id)
        direction = _direction(arguments["direction"])
        if direction == "desc":
            heads.reverse()
        selected, next_cursor, has_more = _page_items(
            heads,
            limit=_limit(arguments["limit"]),
            after=_optional_string(arguments["after"]),
            query=f"project-heads:{project_id}:{direction}",
        )
        return m.ProjectHeadPageV2(
            items=selected,
            next_cursor=next_cursor,
            has_more=has_more,
        )

    def _get_active_project_head(
        self, arguments: Mapping[str, object]
    ) -> m.ProjectHeadRefV2:
        _keys(arguments, "project_id")
        project_id = _string(arguments["project_id"])
        self.store.get_project(project_id)
        return self._task_owner.active_project_head(project_id)

    def _get_project_head(self, arguments: Mapping[str, object]) -> m.ProjectHeadRefV2:
        _keys(arguments, "project_head_id")
        return self._task_owner.get_project_head(
            _string(arguments["project_head_id"])
        )

    def _list_transitions(
        self, arguments: Mapping[str, object]
    ) -> m.SuccessorTransitionPageV2:
        _keys(arguments, "project_id", "limit", "after", "direction")
        project_id = _string(arguments["project_id"])
        transitions = self._task_owner.list_successor_transitions(project_id)
        transitions.sort(
            key=lambda item: (
                item.created_at,
                item.transition.successor_transition_id,
            )
        )
        direction = _direction(arguments["direction"])
        if direction == "desc":
            transitions.reverse()
        selected, next_cursor, has_more = _page_items(
            transitions,
            limit=_limit(arguments["limit"]),
            after=_optional_string(arguments["after"]),
            query=f"transitions:{project_id}:{direction}",
        )
        return m.SuccessorTransitionPageV2(
            items=selected,
            next_cursor=next_cursor,
            has_more=has_more,
        )

    def _get_transition(self, arguments: Mapping[str, object]) -> m.SuccessorTransitionV2:
        _keys(arguments, "successor_transition_id")
        return self._task_owner.get_successor_transition(
            _string(arguments["successor_transition_id"])
        )

    def _submit_task(self, arguments: Mapping[str, object]) -> m.TaskV2:
        _keys(arguments, "request", "idempotency_key")
        request = _model(m.TaskSubmitRequestV2, arguments["request"])
        record = self.store.get_project(request.project_id)
        project = self._project_model(record)
        if project.state != "ready" or project.active_project_head is None:
            raise _http_error(
                409,
                code="project_not_ready",
                message="The project has unresolved state and cannot admit a Task.",
                category="project",
                retryable=True,
                repair_action="retry",
            )
        return self._task_owner.invoke(
            "submitCoreTaskV2",
            {
                "request": request,
                "idempotency_key": _string(arguments["idempotency_key"]),
            },
        )  # type: ignore[return-value]

    def _list_tasks(self, arguments: Mapping[str, object]) -> m.TaskPageV2:
        _keys(arguments, "limit", "after", "project_id", "direction")
        project_id = _optional_string(arguments["project_id"])
        tasks = self._task_owner.invoke(
            "listCoreTasksV2",
            {"project_id": project_id},
        )
        if not isinstance(tasks, list):
            raise RuntimeError("v2 Task owner returned the wrong inventory type")
        tasks.sort(key=lambda item: (item.created_at, item.task_id))
        direction = _direction(arguments["direction"])
        if direction == "desc":
            tasks.reverse()
        selected, next_cursor, has_more = _page_items(
            tasks,
            limit=_limit(arguments["limit"]),
            after=_optional_string(arguments["after"]),
            query=f"tasks:{project_id or '*'}:{direction}",
        )
        return m.TaskPageV2(
            items=selected,
            next_cursor=next_cursor,
            has_more=has_more,
        )

    def _get_task(self, arguments: Mapping[str, object]) -> Response:
        _keys(arguments, "task_id")
        task = self._task_owner.invoke(
            "getCoreTaskV2", {"task_id": _string(arguments["task_id"])}
        )
        if not isinstance(task, m.TaskV2):
            raise RuntimeError("v2 Task owner returned the wrong Task type")
        return JSONResponse(
            content=task.model_dump(mode="json"),
            headers={"ETag": task.etag},
        )

    def _get_task_admission(self, arguments: Mapping[str, object]) -> object:
        _keys(arguments, "task_id")
        return self._task_owner.invoke(
            "getCoreTaskAdmissionV2",
            {"task_id": _string(arguments["task_id"])},
        )

    def _list_task_attempts(self, arguments: Mapping[str, object]) -> m.AttemptPageV2:
        _keys(arguments, "task_id", "limit", "after")
        task_id = _string(arguments["task_id"])
        attempts = self._task_owner.invoke(
            "listCoreTaskAttemptsV2", {"task_id": task_id}
        )
        if not isinstance(attempts, list):
            raise RuntimeError("v2 Task owner returned the wrong Attempt inventory")
        selected, next_cursor, has_more = _page_items(
            attempts,
            limit=_limit(arguments["limit"]),
            after=_optional_string(arguments["after"]),
            query=f"attempts:{task_id}",
        )
        return m.AttemptPageV2(
            items=selected,
            next_cursor=next_cursor,
            has_more=has_more,
        )

    def _append_task_attempt(self, arguments: Mapping[str, object]) -> object:
        _keys(arguments, "task_id", "request", "idempotency_key")
        return self._task_owner.invoke(
            "appendCoreTaskAttemptV2",
            {
                "task_id": _string(arguments["task_id"]),
                "request": _model(m.AttemptAppendRequestV2, arguments["request"]),
                "idempotency_key": _string(arguments["idempotency_key"]),
            },
        )

    def _get_task_attempt(self, arguments: Mapping[str, object]) -> object:
        _keys(arguments, "task_id", "attempt_id")
        return self._task_owner.invoke(
            "getCoreTaskAttemptV2",
            {
                "task_id": _string(arguments["task_id"]),
                "attempt_id": _string(arguments["attempt_id"]),
            },
        )

    def _task_timeline(self, arguments: Mapping[str, object]) -> m.TimelinePageV2:
        _keys(arguments, "task_id", "limit", "after")
        task_id = _string(arguments["task_id"])
        events = self._task_owner.list_task_events(task_id)
        selected, next_cursor, has_more = _page_items(
            events,
            limit=_limit(arguments["limit"]),
            after=_optional_string(arguments["after"]),
            query=f"task-events:{task_id}",
        )
        return m.TimelinePageV2(
            items=selected,
            next_cursor=next_cursor,
            has_more=has_more,
        )

    def _task_context(self, arguments: Mapping[str, object]) -> m.TaskContextV2:
        _keys(arguments, "task_id")
        task_id = _string(arguments["task_id"])
        task = self._task_owner.invoke("getCoreTaskV2", {"task_id": task_id})
        if not isinstance(task, m.TaskV2):
            raise RuntimeError("v2 Task owner returned the wrong Task type")
        return m.TaskContextV2(
            task_id=task.task_id,
            task_admission_id=task.admission.task_admission_id,
            project_head=task.admission.predecessor_project_head,
            workspace_snapshot=task.admission.workspace_snapshot,
        )

    def _close_task(self, arguments: Mapping[str, object]) -> Response:
        _keys(arguments, "task_id", "request", "if_match", "idempotency_key")
        task_id = _string(arguments["task_id"])
        request = _model(m.TaskActionRequestV2, arguments["request"])
        if_match = _string(arguments["if_match"])
        idempotency_key = _string(arguments["idempotency_key"])
        action_scope = f"task-close:{task_id}"
        request_json = _canonical_action_request(
            {
                "action": "task_close",
                "task_id": task_id,
                "request": request.model_dump(mode="json"),
                "if_match": if_match,
            }
        )
        with self._lock:
            try:
                reservation = self.store.begin_action(
                    action_scope=action_scope,
                    idempotency_key=idempotency_key,
                    request_json=request_json,
                )
            except ProjectIdempotencyConflictV2 as exc:
                raise _http_error(
                    409,
                    code="task_idempotency_key_reused",
                    message="The idempotency key was used for another Task action.",
                    category="task",
                    retryable=False,
                    repair_action="user_action_required",
                ) from exc
            if reservation.operation is not None:
                return _operation_response(reservation.operation, status_code=202)
            closed = self._task_owner.close_task(
                task_id,
                request,
                expected_etag=if_match,
                allow_closed_recovery=reservation.resumed,
            )
            operation_seed = hashlib.sha256(
                action_scope.encode("utf-8")
                + b"\0"
                + idempotency_key.encode("utf-8")
                + b"\0"
                + request_json
            ).hexdigest()
            provisional = m.OperationV2(
                operation_id=f"operation-{operation_seed[:32]}",
                kind="task_close",
                status="succeeded",
                progress_completed=1,
                progress_total=1,
                error=None,
                created_at=closed.updated_at,
                updated_at=closed.updated_at,
                etag=f'"{"0" * 64}"',
            )
            operation = m.OperationV2.model_validate(
                {
                    **provisional.model_dump(mode="python"),
                    "etag": operation_etag_for(provisional),
                }
            )
            committed = self.store.commit_action(
                action_scope=action_scope,
                idempotency_key=idempotency_key,
                request_json=request_json,
                operation=operation,
            )
            return _operation_response(committed, status_code=202)

    def _get_operation(self, arguments: Mapping[str, object]) -> Response:
        _keys(arguments, "operation_id")
        operation = self.store.get_operation(_string(arguments["operation_id"]))
        return _operation_response(operation)

    def _list_services(self, arguments: Mapping[str, object]) -> m.ServicePageV2:
        _keys(arguments, "limit", "after")
        service = self._daemon_service()
        selected, next_cursor, has_more = _page_items(
            [service],
            limit=_limit(arguments["limit"]),
            after=_optional_string(arguments["after"]),
            query="services",
        )
        return m.ServicePageV2(
            items=selected,
            next_cursor=next_cursor,
            has_more=has_more,
        )

    def _get_service(self, arguments: Mapping[str, object]) -> Response:
        _keys(arguments, "service_id")
        if _string(arguments["service_id"]) != "daemon":
            raise _http_error(
                404,
                code="service_not_found",
                message="The requested Core service was not found.",
                category="service",
                retryable=False,
                repair_action="user_action_required",
            )
        service = self._daemon_service()
        return JSONResponse(
            content=service.model_dump(mode="json"),
            headers={"ETag": service.etag},
        )

    def _daemon_service(self) -> m.ServiceV2:
        payload = (
            f"daemon:{self._started_at}:{self._release_version}:{self._source_commit}"
        ).encode("utf-8")
        return m.ServiceV2(
            service_id="daemon",
            kind="daemon",
            status="ready",
            updated_at=self._started_at,
            etag=f'"{hashlib.sha256(payload).hexdigest()}"',
        )

    def _events(self, arguments: Mapping[str, object]) -> StreamingResponse:
        _keys(arguments, "last_event_id")
        initial = self._task_owner.list_events(
            after_event_id=_optional_string(arguments["last_event_id"])
        )

        async def stream():
            events = initial
            cursor = _optional_string(arguments["last_event_id"])
            loop = asyncio.get_running_loop()
            last_emit = loop.time()
            while True:
                if events:
                    for event in events:
                        cursor = event.event_id
                        yield _sse_bytes(event)
                        last_emit = loop.time()
                await asyncio.sleep(1)
                if self._closed:
                    return
                events = await asyncio.to_thread(
                    self._task_owner.list_events,
                    after_event_id=cursor,
                )
                if not events and loop.time() - last_emit >= 15:
                    yield b": heartbeat\n\n"
                    last_emit = loop.time()

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    def _feature_not_ready(self, operation_id: str) -> CoreControlHTTPErrorV2:
        category: Literal[
            "system",
            "project",
            "task",
            "transition",
            "artifact",
            "service",
            "authentication",
            "contract",
            "internal",
        ] = "system"
        if operation_id in _PROJECT_OPERATIONS or operation_id in _WORKSPACE_OPERATIONS:
            category = "project"
        elif operation_id in _TRANSITION_OPERATIONS:
            category = "transition"
        elif operation_id in _ARTIFACT_OPERATIONS:
            category = "artifact"
        elif operation_id in _SERVICE_OPERATIONS:
            category = "service"
        elif "Task" in operation_id or "Attempt" in operation_id:
            category = "task"
        return _http_error(
            503,
            code="feature_not_ready",
            message="This Core authority is not wired in the current release build.",
            category=category,
            retryable=False,
            repair_action="unsupported",
        )

    def _ensure_open(self) -> None:
        with self._lock:
            if self._closed:
                raise RuntimeError("Core Control v2 provider is closed")


def _task_owner_http_error(
    exc: CoreTaskControlError,
    *,
    operation_id: str,
) -> CoreControlHTTPErrorV2:
    category: Literal["task", "transition", "contract"] = "task"
    if "Transition" in operation_id or exc.code.startswith("successor_"):
        category = "transition"
    if exc.code == "event_cursor_expired":
        category = "contract"
    return _http_error(
        exc.http_status,
        code=exc.code,
        message=str(exc),
        category=category,
        retryable=exc.retryable,
        repair_action="retry" if exc.retryable else "user_action_required",
    )


def _http_error(
    status_code: int,
    *,
    code: str,
    message: str,
    category: Literal[
        "system",
        "project",
        "task",
        "transition",
        "artifact",
        "service",
        "authentication",
        "contract",
        "internal",
    ],
    retryable: bool,
    repair_action: Literal[
        "retry", "repair", "reconfigure", "user_action_required", "unsupported"
    ],
) -> CoreControlHTTPErrorV2:
    return CoreControlHTTPErrorV2(
        status_code,
        code=code,
        message=message,
        category=category,
        retryable=retryable,
        repair_action=repair_action,
        next_action=(
            "Retry after the authoritative Core state is available."
            if retryable
            else "Use only the authority advertised by this Core build."
        ),
    )


def _page_items(
    items: Sequence[object],
    *,
    limit: int,
    after: str | None,
    query: str,
) -> tuple[list[object], str | None, bool]:
    try:
        return page_items(items, limit=limit, after=after, query=query)
    except ValueError as exc:
        raise _http_error(
            400,
            code="cursor_invalid",
            message="The page cursor is invalid for this resource query.",
            category="contract",
            retryable=False,
            repair_action="reconfigure",
        ) from exc


def _canonical_action_request(value: Mapping[str, object]) -> bytes:
    try:
        return json.dumps(
            dict(value),
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("v2 action request is not canonical data") from exc


def _operation_response(
    operation: m.OperationV2,
    *,
    status_code: int = 200,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content=operation.model_dump(mode="json"),
        headers={"ETag": operation.etag},
    )


def _sse_bytes(event: m.EventEnvelopeV2) -> bytes:
    data = json.dumps(
        event.model_dump(mode="json"),
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return (
        f"id: {event.event_id}\nevent: {event.event_type}\ndata: {data}\n\n"
    ).encode("utf-8")


def _keys(arguments: Mapping[str, object], *required: str) -> None:
    if not isinstance(arguments, Mapping) or set(arguments) != set(required):
        raise ValueError("v2 provider operation arguments are not closed")


def _model(model_type: type[m.ContractModel], value: object):
    if type(value) is model_type:
        return model_type.model_validate(value.model_dump(mode="python"))
    return model_type.model_validate(value)


def _string(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("v2 provider argument must be a string")
    return value


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    return _string(value)


def _limit(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 100:
        raise ValueError("v2 page limit is invalid")
    return value


def _direction(value: object) -> Literal["asc", "desc"]:
    if value not in {"asc", "desc"}:
        raise ValueError("v2 page direction is invalid")
    return value  # type: ignore[return-value]


def _digest(value: str, *, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"Core v2 {label} digest is invalid")
    return value


def _timestamp(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise TypeError("Core v2 timestamp requires an aware datetime")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _require_exact_schema_snapshots() -> None:
    expected = (
        (OPENAPI_SNAPSHOT_PATH, openapi_sha256(), "OpenAPI"),
        (EVENTS_SCHEMA_SNAPSHOT_PATH, events_schema_sha256(), "event schema"),
    )
    for snapshot_path, expected_sha256, label in expected:
        try:
            payload = snapshot_path.read_bytes()
        except OSError as exc:
            raise RuntimeError(f"Core v2 {label} snapshot is unavailable") from exc
        if hashlib.sha256(payload).hexdigest() != expected_sha256:
            raise RuntimeError(f"Core v2 {label} snapshot does not match provider code")


__all__ = ["CoreControlProviderV2"]
