from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Callable, Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import hmac
from pathlib import Path
import threading
from typing import Any, NoReturn, cast

from fastapi import FastAPI
from fastapi.responses import JSONResponse, Response, StreamingResponse

from openevo import __version__
from openevo.backend.service_control import CoreServiceControl, CoreServiceControlError
from openevo.backend.run_admission import install_core_run_admission_endpoint
from openevo.backend.run_control import (
    RUN_OPERATION_IDS,
    CoreRunControl,
    CoreRunControlError,
)
from openevo.evolution.framework import (
    CapabilityAudience,
    EvolutionExecutionProfile,
    build_evolution_capabilities,
    execution_profile_for_release_mode,
)
from openevo.evolution.framework.builtins import (
    VerifiedExecutableRegistry,
    require_verified_executable_registry,
)
from openevo.experiments import (
    ProjectEvolutionValidationError,
    validate_project_evolution_selections,
)

from . import models as m
from .app import CoreControlHTTPError, _iter_api_routes, create_core_control_contract_app
from .snapshots import openapi_sha256
from .store import (
    CoreControlStoreError,
    CoreControlStoreV1,
    CursorExpiredError,
    CursorInvalidError,
    ETagPreconditionError,
    EventCursorExpiredError,
    EventCursorInvalidError,
    IdempotencyCapacityError,
    IdempotencyConflictError,
    PostCommitStoreError,
    ResourceConflictError,
    ResourceNotFoundError,
    StoreCorruptionError,
    StoredResult,
    _failed_idempotency_identity,
)


_FEATURES = [
    m.FeatureFlag.PROJECTS,
    m.FeatureFlag.WORKSPACE_SYNC,
    m.FeatureFlag.VERIFIED_CAPABILITIES,
    m.FeatureFlag.TRANSCRIPT_CAPTURE,
    m.FeatureFlag.NON_PARAMETRIC_EVOLUTION,
    m.FeatureFlag.SSE_REPLAY,
]

_RUN_MUTATION_OPERATION_IDS = frozenset(
    {
        "cancelCoreRunV1",
        "createCoreRunV1",
        "deleteCoreRunV1",
        "retryCoreRunV1",
    }
)
_RUN_MUTATION_SINGLEFLIGHT_CAPACITY = 256

_UNAVAILABLE_OPERATIONS = frozenset(
    {
        "doctorCoreEnvironmentV1",
        "repairCoreEnvironmentV1",
        "listCoreRunsV1",
        "createCoreRunV1",
        "getCoreRunV1",
        "deleteCoreRunV1",
        "cancelCoreRunV1",
        "retryCoreRunV1",
        "getCoreRunTimelineV1",
        "getCoreRunLogsV1",
        "getCoreRunContextV1",
        "listCoreRunArtifactsV1",
        "getCoreArtifactV1",
        "getCoreArtifactContentV1",
        "getCoreArtifactDiffV1",
        "restartCoreServiceV1",
        "getCoreServiceLogsV1",
        "getCoreOperationV1",
        "cancelCoreOperationV1",
        "getCoreLogsByRefV1",
        "createCoreDiagnosticV1",
        "getCoreDiagnosticV1",
        "deleteCoreDiagnosticV1",
        "cleanupCoreCachesV1",
    }
)


class _RunControlHTTPError(CoreControlHTTPError):
    """Preserve run-owner error provenance through the frozen HTTP contract."""


def _run_control_http_error(exc: CoreRunControlError) -> _RunControlHTTPError:
    return _RunControlHTTPError(
        exc.http_status,
        code=exc.code,
        message=str(exc),
        category=m.ErrorCategory.RUN,
        retryable=exc.retryable,
        repair_action=(
            m.RepairAction.OPENEVO_CAN_RETRY
            if exc.retryable
            else m.RepairAction.USER_ACTION_REQUIRED
        ),
        next_action=(
            "Retry the run operation after Core reports readiness."
            if exc.retryable
            else "Reload the run and project state before continuing."
        ),
    )


class _RunMutationSingleFlight:
    def __init__(self, capacity: int) -> None:
        if capacity < 1:
            raise ValueError("run mutation single-flight capacity must be positive")
        self._capacity = capacity
        self._lock = threading.Lock()
        self._entries: OrderedDict[
            tuple[str, str, str], tuple[str, Future[object]]
        ] = OrderedDict()
        self._closing = False

    def invoke(
        self,
        identity: tuple[str, str, str],
        request_digest: str,
        call: Callable[[], object],
    ) -> object:
        with self._lock:
            if self._closing:
                raise _run_mutation_closing_error()
            entry = self._entries.get(identity)
            if entry is not None:
                retained_digest, future = entry
                if not hmac.compare_digest(retained_digest, request_digest):
                    raise _idempotency_conflict_error()
                self._entries.move_to_end(identity)
                owner = False
            else:
                self._evict_completed_locked()
                if len(self._entries) >= self._capacity:
                    raise _run_mutation_capacity_error()
                future = Future()
                self._entries[identity] = (request_digest, future)
                owner = True

        if not owner:
            try:
                return future.result()
            except CoreControlHTTPError as exc:
                replay = CoreControlHTTPError.from_error(exc.error)
                replay.headers = dict(exc.headers)
                raise replay from exc

        try:
            result = call()
        except BaseException as exc:
            with self._lock:
                retained = self._entries.get(identity)
                if retained is not None and retained[1] is future:
                    del self._entries[identity]
                future.set_exception(exc)
            raise
        with self._lock:
            if self._closing:
                retained = self._entries.get(identity)
                if retained is not None and retained[1] is future:
                    del self._entries[identity]
            else:
                self._entries.move_to_end(identity)
            future.set_result(result)
        return result

    def close(self) -> tuple[Future[object], ...]:
        with self._lock:
            self._closing = True
            active = tuple(
                future for _, future in self._entries.values() if not future.done()
            )
            self._entries.clear()
            return active

    def _evict_completed_locked(self) -> None:
        while len(self._entries) >= self._capacity:
            completed_identity = next(
                (
                    identity
                    for identity, (_, future) in self._entries.items()
                    if future.done()
                ),
                None,
            )
            if completed_identity is None:
                return
            del self._entries[completed_identity]


class _PostCommitHTTPError(CoreControlHTTPError):
    """Fail closed without overwriting a transaction's committed idempotency result."""


class CoreControlProviderV1:
    """First real Core Control v1 business provider."""

    def __init__(
        self,
        store: CoreControlStoreV1,
        *,
        bearer_token: str,
        build_version: str,
        source_commit: str,
        build_channel: m.BuildChannel | str,
        evolution_registry: VerifiedExecutableRegistry | None = None,
        service_supervisor: CoreServiceControl | None = None,
        run_control: CoreRunControl | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        try:
            token_bytes = bearer_token.encode("ascii")
        except UnicodeEncodeError as exc:
            raise ValueError("Core bearer token must be ASCII") from exc
        if not token_bytes or any(value <= 32 or value == 127 for value in token_bytes):
            raise ValueError("Core bearer token must be non-empty and contain no whitespace")
        if evolution_registry is not None:
            require_verified_executable_registry(evolution_registry)
        self.store = store
        self._executor = ThreadPoolExecutor(
            max_workers=4, thread_name_prefix="openevo-core-control"
        )
        self._close_lock = threading.Lock()
        self._closed = False
        self._run_mutations = _RunMutationSingleFlight(
            _RUN_MUTATION_SINGLEFLIGHT_CAPACITY
        )
        self._authorization = b"Bearer " + token_bytes
        self._registry = evolution_registry
        self._service_supervisor = service_supervisor
        self._run_control = run_control
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._started_at = self._timestamp()
        self._version = m.VersionResponseV1(
            preferred_major=1,
            supported_majors=[1],
            openapi_sha256=openapi_sha256(),
            build_version=build_version,
            source_commit=source_commit,
            build_channel=(
                m.BuildChannel(build_channel) if isinstance(build_channel, str) else build_channel
            ),
            provider_kind=m.ProviderKind.OPENEVO_CORE,
            features=_FEATURES,
        )
        self._handlers = {
            "discoverCoreContractVersionV1": self._version_response,
            "discoverCoreHealthV1": self._health,
            "getCoreStatusV1": self._status,
            "getCoreCapabilitiesV1": self._capabilities,
            "listCoreProjectsV1": self._list_projects,
            "createCoreProjectV1": self._create_project,
            "getCoreProjectV1": self._get_project,
            "patchCoreProjectV1": self._patch_project,
            "deleteCoreProjectV1": self._delete_project,
            "listCoreProjectRevisionsV1": self._list_project_revisions,
            "getCoreProjectRevisionHeadV1": self._get_project_revision_head,
            "getCoreRevisionV1": self._get_revision,
            "createCoreWorkspaceUploadV1": self._create_upload,
            "getCoreWorkspaceUploadV1": self._get_upload,
            "putCoreWorkspaceUploadChunkV1": self._put_upload_chunk,
            "finalizeCoreWorkspaceUploadV1": self._finalize_upload,
            "abortCoreWorkspaceUploadV1": self._abort_upload,
            "validateCoreProjectV1": self._validate_project,
            "listCoreServicesV1": self._list_services,
            "getCoreServiceV1": self._get_service,
            "streamCoreEventsV1": self._events,
        }

    def close(self) -> None:
        with self._close_lock:
            if self._closed:
                return
            active_run_mutations = self._run_mutations.close()
            if self._run_control is not None:
                self._run_control.close()
                self._run_control = None
            for mutation in active_run_mutations:
                try:
                    mutation.result()
                except BaseException:
                    pass
            if self._service_supervisor is not None:
                self._service_supervisor.close()
                self._service_supervisor = None
            if self._run_control is not None:
                self._run_control.close()
                self._run_control = None
            future = self._executor.submit(self.store.close)
            future.result()
            self._closed = True
            self._executor.shutdown(wait=True, cancel_futures=True)

    async def aclose(self) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self.close)

    @property
    def operation_ids(self) -> frozenset[str]:
        return frozenset(self._handlers) | _UNAVAILABLE_OPERATIONS

    def authenticate(self, authorization_values: tuple[bytes, ...]) -> bool:
        return len(authorization_values) == 1 and hmac.compare_digest(
            authorization_values[0], self._authorization
        )

    def invoke(self, operation_id: str, arguments: Mapping[str, object]) -> object:
        if operation_id in _RUN_MUTATION_OPERATION_IDS:
            identity = _failed_idempotency_identity(operation_id, arguments)
            if identity is not None:
                scope, key, digest = identity
                return self._run_mutations.invoke(
                    (operation_id, scope, key),
                    digest,
                    lambda: self._invoke_with_failed_idempotency(operation_id, arguments),
                )
        return self._invoke_with_failed_idempotency(operation_id, arguments)

    def _invoke_with_failed_idempotency(
        self, operation_id: str, arguments: Mapping[str, object]
    ) -> object:
        try:
            previous_error = self.store.replay_failed_idempotency(
                operation_id,
                arguments,
                clear_retryable=operation_id in RUN_OPERATION_IDS,
            )
        except IdempotencyConflictError as exc:
            raise _idempotency_conflict_error() from exc
        if previous_error is not None:
            raise CoreControlHTTPError.from_error(previous_error)
        try:
            return self._invoke(operation_id, arguments)
        except _PostCommitHTTPError:
            raise
        except CoreControlHTTPError as exc:
            if not (isinstance(exc, _RunControlHTTPError) and exc.error.retryable):
                self.store.record_failed_idempotency(operation_id, arguments, exc.error)
            raise

    async def invoke_async(self, operation_id: str, arguments: Mapping[str, object]) -> object:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, self.invoke, operation_id, arguments)

    def _invoke(self, operation_id: str, arguments: Mapping[str, object]) -> object:
        if operation_id in RUN_OPERATION_IDS and self._run_control is not None:
            try:
                return self._run_control.invoke(operation_id, arguments)
            except CoreRunControlError as exc:
                raise _run_control_http_error(exc) from exc
        if operation_id in _UNAVAILABLE_OPERATIONS:
            self._unavailable(operation_id)
        handler = self._handlers.get(operation_id)
        if handler is None:
            self._unavailable(operation_id)
        try:
            return handler(arguments)
        except CoreControlHTTPError:
            raise
        except CoreRunControlError as exc:
            raise _run_control_http_error(exc) from exc
        except ResourceNotFoundError as exc:
            raise _error(
                404,
                code=f"{exc.resource_type}_not_found",
                message=f"The requested {exc.resource_type.replace('_', ' ')} was not found.",
                category=_category_for_resource(exc.resource_type),
                retryable=False,
                repair_action=m.RepairAction.USER_ACTION_REQUIRED,
                next_action="Reload the authoritative Core snapshot.",
            ) from exc
        except ETagPreconditionError as exc:
            code = (
                "project_etag_precondition_failed"
                if exc.resource_type == "finalize_project"
                else "etag_precondition_failed"
            )
            raise _error(
                412,
                code=code,
                message="The resource changed before this mutation was applied.",
                category=_category_for_resource(exc.resource_type),
                retryable=True,
                repair_action=m.RepairAction.OPENEVO_CAN_RETRY,
                next_action="Reload the resource and retry with its current ETag.",
            ) from exc
        except IdempotencyConflictError as exc:
            raise _idempotency_conflict_error() from exc
        except IdempotencyCapacityError as exc:
            raise _error(
                503,
                code="idempotency_capacity_exhausted",
                message="Core cannot accept another idempotent mutation right now.",
                category=m.ErrorCategory.SERVICE,
                retryable=True,
                repair_action=m.RepairAction.OPENEVO_CAN_RETRY,
                next_action="Retry after retained idempotency records expire.",
            ) from exc
        except ResourceConflictError as exc:
            raise _resource_conflict_error(exc) from exc
        except CursorExpiredError as exc:
            raise _error(
                410,
                code="cursor_expired",
                message="The project cursor expired.",
                category=m.ErrorCategory.CONTRACT,
                retryable=True,
                repair_action=m.RepairAction.OPENEVO_CAN_RETRY,
                next_action="Reload the first project page.",
            ) from exc
        except CursorInvalidError as exc:
            raise _error(
                400,
                code="cursor_invalid",
                message="The project cursor is invalid.",
                category=m.ErrorCategory.CONTRACT,
                retryable=False,
                repair_action=m.RepairAction.USER_ACTION_REQUIRED,
                next_action="Reload the first project page.",
            ) from exc
        except EventCursorExpiredError as exc:
            raise _error(
                410,
                code="event_cursor_expired",
                message="The event replay cursor expired.",
                category=m.ErrorCategory.CONTRACT,
                retryable=True,
                repair_action=m.RepairAction.OPENEVO_CAN_RETRY,
                next_action="Reload authoritative snapshots before reconnecting.",
            ) from exc
        except EventCursorInvalidError as exc:
            raise _error(
                400,
                code="event_cursor_invalid",
                message="The event replay cursor is invalid.",
                category=m.ErrorCategory.CONTRACT,
                retryable=False,
                repair_action=m.RepairAction.USER_ACTION_REQUIRED,
                next_action="Reconnect without the invalid cursor after reloading snapshots.",
            ) from exc
        except PostCommitStoreError as exc:
            raise _post_commit_error() from exc
        except StoreCorruptionError as exc:
            raise _error(
                500,
                code="core_control_store_corrupt",
                message="Core Control durable state failed closed integrity validation.",
                category=m.ErrorCategory.INTERNAL,
                retryable=False,
                repair_action=m.RepairAction.USER_ACTION_REQUIRED,
                next_action="Stop Core and inspect or restore the provider state.",
            ) from exc
        except CoreControlStoreError as exc:
            raise _error(
                500,
                code="core_control_store_failed",
                message="Core Control durable state could not complete the request.",
                category=m.ErrorCategory.INTERNAL,
                retryable=True,
                repair_action=m.RepairAction.OPENEVO_CAN_RETRY,
                next_action="Inspect Core diagnostics before retrying.",
            ) from exc
        except CoreServiceControlError as exc:
            raise _error(
                503,
                code="core_service_supervisor_failed",
                message="Core could not inspect or control its managed services.",
                category=m.ErrorCategory.SERVICE,
                retryable=True,
                repair_action=m.RepairAction.OPENEVO_CAN_RETRY,
                next_action="Retry after Core service ownership is restored.",
            ) from exc

    def _version_response(self, arguments: Mapping[str, object]) -> m.VersionResponseV1:
        del arguments
        return self._version

    def _health(self, arguments: Mapping[str, object]) -> m.HealthResponseV1:
        del arguments
        ready = self._registry is not None
        return m.HealthResponseV1(
            status=m.HealthStatus.OK if ready else m.HealthStatus.DEGRADED,
            ready=ready,
            checked_at=self._timestamp(),
        )

    def _status(self, arguments: Mapping[str, object]) -> m.CoreStatusV1:
        del arguments
        services = self._services()
        verified = self._registry is not None
        active_runs, queued_runs = (
            self._run_control.counts() if self._run_control is not None else (0, 0)
        )
        return m.CoreStatusV1(
            status=m.HealthStatus.OK if verified else m.HealthStatus.DEGRADED,
            registry_status=(
                m.RegistryStatus.VERIFIED if verified else m.RegistryStatus.UNAVAILABLE
            ),
            registry_digest=(
                self._registry.snapshot.registry_digest if self._registry is not None else None
            ),
            active_runs=active_runs,
            queued_runs=queued_runs,
            services=services,
            checked_at=self._timestamp(),
        )

    def _capabilities(self, arguments: Mapping[str, object]) -> object:
        registry = self._require_registry("capabilities")
        execution_mode = cast(m.ExecutionMode, arguments["execution_mode"])
        return build_evolution_capabilities(
            registry.snapshot,
            profile=execution_profile_for_release_mode(execution_mode),
            audience=CapabilityAudience.DESKTOP,
            core_version=__version__,
        )

    def _list_projects(self, arguments: Mapping[str, object]) -> m.ProjectPageV1:
        return self.store.list_projects(
            limit=cast(int, arguments["limit"]),
            after=cast(str | None, arguments["after"]),
            sort=cast(Any, arguments["sort"]),
            direction=cast(Any, arguments["direction"]),
        )

    def _create_project(self, arguments: Mapping[str, object]) -> Response:
        result = self.store.create_project(
            cast(m.ProjectCreateV1, arguments["request"]),
            idempotency_key=cast(str, arguments["idempotency_key"]),
            registry_digest=self._registry_digest(),
        )
        return _stored_response(result)

    def _get_project(self, arguments: Mapping[str, object]) -> Response:
        project = self.store.get_project(cast(str, arguments["project_id"]))
        return _model_response(project, etag=project.etag)

    def _patch_project(self, arguments: Mapping[str, object]) -> Response:
        result = self.store.patch_project(
            cast(str, arguments["project_id"]),
            cast(m.ProjectPatchV1, arguments["request"]),
            if_match=cast(str, arguments["if_match"]),
            idempotency_key=cast(str, arguments["idempotency_key"]),
            registry_digest=self._registry_digest(),
        )
        return _stored_response(result)

    def _delete_project(self, arguments: Mapping[str, object]) -> Response:
        return _stored_response(
            self.store.delete_project(
                cast(str, arguments["project_id"]),
                if_match=cast(str, arguments["if_match"]),
                idempotency_key=cast(str, arguments["idempotency_key"]),
            )
        )

    def _list_project_revisions(self, arguments: Mapping[str, object]) -> m.RevisionPageV1:
        return self.store.list_project_revisions(
            cast(str, arguments["project_id"]),
            limit=cast(int, arguments["limit"]),
            after=cast(str | None, arguments["after"]),
            sort=cast(Any, arguments["sort"]),
            direction=cast(Any, arguments["direction"]),
        )

    def _get_project_revision_head(self, arguments: Mapping[str, object]) -> Response:
        head = self.store.get_revision_head(cast(str, arguments["project_id"]))
        return _model_response(head, etag=head.etag)

    def _get_revision(self, arguments: Mapping[str, object]) -> Response:
        revision = self.store.get_revision(cast(str, arguments["revision_id"]))
        return _model_response(revision, etag=revision.etag)

    def _create_upload(self, arguments: Mapping[str, object]) -> Response:
        return _stored_response(
            self.store.create_upload(
                cast(str, arguments["project_id"]),
                cast(m.WorkspaceUploadCreateV1, arguments["request"]),
                if_match=cast(str, arguments["if_match"]),
                idempotency_key=cast(str, arguments["idempotency_key"]),
            )
        )

    def _get_upload(self, arguments: Mapping[str, object]) -> Response:
        upload = self.store.get_upload(
            cast(str, arguments["project_id"]), cast(str, arguments["upload_id"])
        )
        return _model_response(upload, etag=upload.etag)

    def _put_upload_chunk(self, arguments: Mapping[str, object]) -> Response:
        return _stored_response(
            self.store.put_upload_chunk(
                cast(str, arguments["project_id"]),
                cast(str, arguments["upload_id"]),
                cast(m.WorkspaceUploadChunkV1, arguments["request"]),
                if_match=cast(str, arguments["if_match"]),
                idempotency_key=cast(str, arguments["idempotency_key"]),
            )
        )

    def _finalize_upload(self, arguments: Mapping[str, object]) -> Response:
        return _stored_response(
            self.store.finalize_upload(
                cast(str, arguments["project_id"]),
                cast(str, arguments["upload_id"]),
                cast(m.WorkspaceUploadFinalizeV1, arguments["request"]),
                if_match=cast(str, arguments["if_match"]),
                if_project_match=cast(str, arguments["if_project_match"]),
                idempotency_key=cast(str, arguments["idempotency_key"]),
                registry_digest=self._registry_digest(),
            )
        )

    def _abort_upload(self, arguments: Mapping[str, object]) -> Response:
        return _stored_response(
            self.store.abort_upload(
                cast(str, arguments["project_id"]),
                cast(str, arguments["upload_id"]),
                cast(m.WorkspaceUploadAbortV1, arguments["request"]),
                if_match=cast(str, arguments["if_match"]),
                idempotency_key=cast(str, arguments["idempotency_key"]),
            )
        )

    def _validate_project(self, arguments: Mapping[str, object]) -> Response:
        request = cast(m.ProjectValidationRequestV1, arguments["request"])
        registry = self._require_registry("project validation")

        def validate(project: m.ProjectV1) -> m.ProjectValidationResponseV1:
            if request.expected_registry_digest != registry.snapshot.registry_digest:
                raise ResourceConflictError(
                    "evolution_registry_changed",
                    "Evolution capabilities changed before project validation.",
                )
            if request.project_snapshot != project.current_project_snapshot:
                raise ResourceConflictError(
                    "project_snapshot_changed",
                    "The project snapshot is no longer current.",
                )
            if (
                project.current_workspace_snapshot is None
                or request.workspace_snapshot != project.current_workspace_snapshot
            ):
                raise ResourceConflictError(
                    "workspace_snapshot_changed",
                    "The workspace snapshot is missing or no longer current.",
                )
            try:
                validate_project_evolution_selections(
                    project.spec.evolution.targets,
                    agent_model=project.spec.agent_model_ref,
                    reflector_llm={
                        "provider": "codex_cli",
                        "model": project.spec.agent_model_ref,
                    },
                    registry_snapshot=registry.snapshot,
                    execution_profile=EvolutionExecutionProfile(
                        execution_mode=(
                            "subscription"
                            if project.spec.execution_mode
                            is m.ExecutionMode.CODEX_SUBSCRIPTION_TRANSCRIPT
                            else "self_deployed"
                        ),
                        capture_mode=project.spec.capture_mode.value,
                        harness_id=project.spec.harness_id,
                    ),
                )
            except ProjectEvolutionValidationError as exc:
                raise ResourceConflictError(
                    "evolution_project_invalid",
                    "The project evolution configuration is invalid.",
                ) from exc
            return m.ProjectValidationResponseV1(
                valid=True,
                registry_digest=registry.snapshot.registry_digest,
                checks=[
                    m.ValidationCheckV1(
                        id="verified-registry",
                        status=m.CheckStatus.OK,
                        message="The project is valid against the verified executable registry.",
                    )
                ],
                validated_at=self._timestamp(),
            )

        return _stored_response(
            self.store.store_validation_result(
                cast(str, arguments["project_id"]),
                request,
                idempotency_key=cast(str, arguments["idempotency_key"]),
                response_factory=validate,
            )
        )

    def _list_services(self, arguments: Mapping[str, object]) -> m.ServicePageV1:
        if arguments["after"] is not None:
            raise CursorInvalidError("service pages have no continuation cursor")
        services = self._services()
        sort = cast(str, arguments["sort"])
        direction = cast(str, arguments["direction"])
        services.sort(
            key=lambda service: (str(getattr(service, sort)), service.id),
            reverse=direction == "desc",
        )
        limit = cast(int, arguments["limit"])
        return m.ServicePageV1(items=services[:limit], next_cursor=None, has_more=False)

    def _get_service(self, arguments: Mapping[str, object]) -> Response:
        service_id = cast(str, arguments["service_id"])
        for service in self._services():
            if service.id == service_id:
                return _model_response(service, etag=service.etag)
        raise ResourceNotFoundError("service", service_id)

    def _events(self, arguments: Mapping[str, object]) -> StreamingResponse:
        last_event_id = cast(str | None, arguments["last_event_id"])
        initial = self.store.replay_events(last_event_id)

        async def stream():
            frames = initial
            cursor = last_event_id
            last_emit = asyncio.get_running_loop().time()
            while True:
                if frames:
                    for frame in frames:
                        cursor = cast(str, frame["id"])
                        yield _sse_bytes(frame)
                        last_emit = asyncio.get_running_loop().time()
                await asyncio.sleep(1)
                loop = asyncio.get_running_loop()
                frames = await loop.run_in_executor(
                    self._executor, self.store.replay_events, cursor
                )
                if not frames and asyncio.get_running_loop().time() - last_emit >= 15:
                    heartbeat = await loop.run_in_executor(
                        self._executor, self.store.append_heartbeat
                    )
                    frames = [heartbeat]

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    def _services(self) -> list[m.ServiceSummaryV1]:
        observed_at = self._timestamp()
        identity = {
            "id": "core-control",
            "kind": m.ServiceKind.CONTROL,
            "status": m.ServiceStatus.RUNNING,
            "restartable": False,
            "updated_at": self._started_at,
        }
        etag = '"' + hashlib.sha256(json_bytes(identity)).hexdigest() + '"'
        services = [
            m.ServiceSummaryV1(
                id="core-control",
                display_name="Core Control",
                kind=m.ServiceKind.CONTROL,
                status=m.ServiceStatus.RUNNING,
                restartable=False,
                status_message="The Core Control provider is serving this request.",
                updated_at=self._started_at,
                observed_at=observed_at,
                etag=etag,
            )
        ]
        if self._service_supervisor is not None:
            services.extend(
                service.to_contract() for service in self._service_supervisor.list()
            )
        return services

    def _require_registry(self, purpose: str) -> VerifiedExecutableRegistry:
        if self._registry is None:
            raise _error(
                503,
                code="evolution_registry_unavailable",
                message=f"Verified evolution {purpose} is unavailable.",
                category=m.ErrorCategory.SERVICE,
                retryable=True,
                repair_action=m.RepairAction.OPENEVO_CAN_RETRY,
                next_action="Restore the verified executable registry and retry.",
            )
        return self._registry

    def _registry_digest(self) -> str | None:
        return self._registry.snapshot.registry_digest if self._registry is not None else None

    def _timestamp(self) -> str:
        return (
            self._clock()
            .astimezone(timezone.utc)
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z")
        )

    @staticmethod
    def _unavailable(operation_id: str) -> NoReturn:
        raise _error(
            503,
            code="provider_capability_unavailable",
            message="This Core Control operation has no verified business owner in phase one.",
            category=m.ErrorCategory.SERVICE,
            retryable=False,
            repair_action=m.RepairAction.UNSUPPORTED,
            next_action=f"Wait for the release provider that owns {operation_id}.",
        )


def create_core_control_app(
    *,
    state_root: str | Path,
    bearer_token: str,
    build_version: str = __version__,
    source_commit: str = "0" * 40,
    build_channel: m.BuildChannel | str = m.BuildChannel.DEVELOPMENT,
    evolution_registry: VerifiedExecutableRegistry | None = None,
    service_supervisor: CoreServiceControl | None = None,
    run_control: CoreRunControl | None = None,
    run_control_factory: Callable[[CoreControlStoreV1], CoreRunControl] | None = None,
    event_replay_limit: int = 10_000,
) -> FastAPI:
    """Create a provider-backed app without adding a second route table."""

    if run_control is not None and run_control_factory is not None:
        raise ValueError("run_control and run_control_factory are mutually exclusive")
    store = CoreControlStoreV1(state_root, event_replay_limit=event_replay_limit)
    provider: CoreControlProviderV1 | None = None
    resolved_run_control = run_control
    try:
        if run_control_factory is not None:
            resolved_run_control = run_control_factory(store)
        provider = CoreControlProviderV1(
            store,
            bearer_token=bearer_token,
            build_version=build_version,
            source_commit=source_commit,
            build_channel=build_channel,
            evolution_registry=evolution_registry,
            service_supervisor=service_supervisor,
            run_control=resolved_run_control,
        )
        app = create_core_control_contract_app(provider)
        contract_operation_ids = frozenset(
            route.operation_id
            for route in _iter_api_routes(app.routes)
            if route.operation_id is not None
        )
        if provider.operation_ids != contract_operation_ids:
            raise RuntimeError("Core Control provider ownership does not cover the frozen routes")
        if resolved_run_control is not None and service_supervisor is not None:
            install_core_run_admission_endpoint(
                app,
                service_supervisor,
                resolved_run_control,
            )
    except Exception:
        if provider is None:
            if resolved_run_control is not None:
                resolved_run_control.close()
            store.close()
        else:
            provider.close()
        raise
    app.state.core_control_provider = provider
    app.router.add_event_handler("shutdown", provider.aclose)
    return app


def _stored_response(result: StoredResult) -> Response:
    if result.model is None:
        return Response(status_code=result.status_code)
    return _model_response(result.model, status_code=result.status_code, etag=result.etag)


def _model_response(
    model: BaseModelLike,
    *,
    status_code: int = 200,
    etag: str | None = None,
) -> JSONResponse:
    headers = {"ETag": etag} if etag is not None else None
    return JSONResponse(
        status_code=status_code,
        content=model.model_dump(mode="json"),
        headers=headers,
    )


class BaseModelLike:
    def model_dump(self, *, mode: str) -> dict[str, object]: ...


def _error(
    status_code: int,
    *,
    code: str,
    message: str,
    category: m.ErrorCategory,
    retryable: bool,
    repair_action: m.RepairAction,
    next_action: str,
) -> CoreControlHTTPError:
    return CoreControlHTTPError(
        status_code,
        code=code,
        message=message,
        category=category,
        retryable=retryable,
        repair_action=repair_action,
        next_action=next_action,
    )


def _category_for_resource(resource_type: str) -> m.ErrorCategory:
    if resource_type in {
        "project",
        "finalize_project",
        "workspace_upload",
        "revision",
        "revision_head",
    }:
        return m.ErrorCategory.PROJECT
    if resource_type == "service":
        return m.ErrorCategory.SERVICE
    return m.ErrorCategory.INTERNAL


def _idempotency_conflict_error() -> CoreControlHTTPError:
    return _error(
        409,
        code="idempotency_key_reused",
        message="The idempotency key was already used for a different request.",
        category=m.ErrorCategory.CONTRACT,
        retryable=False,
        repair_action=m.RepairAction.USER_ACTION_REQUIRED,
        next_action="Use the original request or issue a new idempotency key.",
    )


def _run_mutation_capacity_error() -> CoreControlHTTPError:
    return _run_control_http_error(
        CoreRunControlError(
            "run_mutation_capacity_exhausted",
            "Core cannot accept another idempotent run mutation right now.",
            http_status=503,
            retryable=True,
        )
    )


def _run_mutation_closing_error() -> CoreControlHTTPError:
    return _run_control_http_error(
        CoreRunControlError(
            "run_owner_unavailable",
            "The managed run owner is shutting down.",
            http_status=503,
            retryable=True,
        )
    )


_RETRYABLE_PROJECT_CONFLICTS = frozenset(
    {
        "project_snapshot_changed",
        "workspace_base_snapshot_changed",
        "workspace_chunk_out_of_order",
        "workspace_upload_incomplete",
        "evolution_registry_changed",
        "workspace_snapshot_changed",
    }
)
_RECONFIGURE_PROJECT_CONFLICTS = frozenset(
    {
        "workspace_upload_not_required",
        "workspace_archive_declaration_changed",
        "workspace_chunk_exceeds_declaration",
        "workspace_digest_mismatch",
        "workspace_archive_invalid",
        "evolution_project_invalid",
    }
)


def _resource_conflict_error(exc: ResourceConflictError) -> CoreControlHTTPError:
    if exc.code == "provider_storage_quota_exceeded":
        return _error(
            409,
            code=exc.code,
            message=str(exc),
            category=m.ErrorCategory.SERVICE,
            retryable=False,
            repair_action=m.RepairAction.USER_ACTION_REQUIRED,
            next_action="Remove managed workspace state or use a smaller workspace.",
        )
    if exc.code in _RETRYABLE_PROJECT_CONFLICTS:
        return _error(
            409,
            code=exc.code,
            message=str(exc),
            category=m.ErrorCategory.PROJECT,
            retryable=True,
            repair_action=m.RepairAction.OPENEVO_CAN_RETRY,
            next_action="Reload the authoritative project and upload snapshots, then retry.",
        )
    if exc.code in _RECONFIGURE_PROJECT_CONFLICTS:
        return _error(
            409,
            code=exc.code,
            message=str(exc),
            category=m.ErrorCategory.PROJECT,
            retryable=False,
            repair_action=m.RepairAction.OPENEVO_CAN_RECONFIGURE,
            next_action="Correct the project or workspace request before retrying.",
        )
    return _error(
        409,
        code=exc.code,
        message=str(exc),
        category=m.ErrorCategory.PROJECT,
        retryable=False,
        repair_action=m.RepairAction.USER_ACTION_REQUIRED,
        next_action="Reload the authoritative resource and choose a valid next action.",
    )


def _post_commit_error() -> _PostCommitHTTPError:
    return _PostCommitHTTPError(
        500,
        code="core_control_store_failed",
        message="Core Control durable state could not complete the request.",
        category=m.ErrorCategory.INTERNAL,
        retryable=True,
        repair_action=m.RepairAction.OPENEVO_CAN_RETRY,
        next_action="Inspect Core diagnostics before retrying.",
    )


def _sse_bytes(frame: Mapping[str, object]) -> bytes:
    import json

    data = json.dumps(frame["data"], ensure_ascii=False, separators=(",", ":"))
    return f"id: {frame['id']}\nevent: {frame['event']}\ndata: {data}\n\n".encode("utf-8")


def json_bytes(value: object) -> bytes:
    import json

    return json.dumps(value, default=str, sort_keys=True, separators=(",", ":")).encode("utf-8")


__all__ = ["CoreControlProviderV1", "create_core_control_app"]
