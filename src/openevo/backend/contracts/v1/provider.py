from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Callable, Mapping
from concurrent.futures import Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import datetime, timezone
import difflib
import hashlib
import hmac
from pathlib import Path, PurePosixPath
import threading
from typing import TYPE_CHECKING, Any, NoReturn, cast

from fastapi import FastAPI
from fastapi.responses import JSONResponse, Response, StreamingResponse
import httpx

from openevo import __version__
from openevo.backend.service_control import CoreServiceControl, CoreServiceControlError
from openevo.backend.maintenance import CoreMaintenanceOwnerV1
from openevo.backend.run_admission import install_core_run_admission_endpoint
from openevo.backend.run_control import (
    RUN_OPERATION_IDS,
    CoreRunControl,
    CoreRunControlError,
)
from openevo.backend.science_run_store import (
    ProjectInFlightCoordinator,
    ScienceProjectInFlight,
)
from openevo.evolution.artifact_payloads import (
    ArtifactPayloadBudgetExceeded,
    ArtifactPayloadLimits,
    ArtifactPayloadService,
)
from openevo.evolution.framework.contracts import MAX_CONTRIBUTION_TEXT
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
from openevo.experiments.clients import EvolutionHttpClient
from openevo.evolution.models import (
    ArtifactResponse as EvolutionArtifactResponse,
    ArtifactState as EvolutionArtifactState,
)
from openevo.projects.science.compiler import MANAGED_RUNTIME_IMAGES

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
    ArtifactReachability,
    _failed_idempotency_identity,
)

if TYPE_CHECKING:
    from openevo.backend.service_supervisor import ServiceRunReadinessCode


_BASE_FEATURES = [
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
_PROJECT_OWNER_GUARDED_OPERATION_IDS = frozenset(
    {
        "abortCoreWorkspaceUploadV1",
        "createCoreWorkspaceUploadV1",
        "deleteCoreProjectV1",
        "finalizeCoreWorkspaceUploadV1",
        "patchCoreProjectV1",
        "putCoreWorkspaceUploadChunkV1",
    }
)
_SYSTEM_MAINTENANCE_START_OPERATIONS = frozenset(
    {
        "repairCoreEnvironmentV1",
        "restartCoreServiceV1",
        "cleanupCoreCachesV1",
    }
)
_MAINTENANCE_OWNER_OPERATION_IDS = frozenset(
    {
        "doctorCoreEnvironmentV1",
        "repairCoreEnvironmentV1",
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
_RUN_MUTATION_SINGLEFLIGHT_CAPACITY = 256
_RUN_MUTATION_SHUTDOWN_DRAIN_TIMEOUT_SECONDS = 30.0
_ARTIFACT_PAGE_LIMIT = 100
_MAX_ARTIFACT_PAGES_PER_RUN = 11
_MAX_ARTIFACT_SOURCE_REVISIONS = 128
_MAX_ARTIFACT_DIFF_INPUT_LINES = 8_192
_MAX_ARTIFACT_DIFF_SEQUENCE_LINES = 2_048
_MAX_ARTIFACT_DIFF_COMPARISONS = 1_000_000
_ARTIFACT_INSPECTION_LIMITS = ArtifactPayloadLimits(
    max_nodes=1_024,
    max_files=m.MAX_ARTIFACT_PREVIEW_DOCUMENTS,
    max_entry_bytes=MAX_CONTRIBUTION_TEXT,
    max_total_bytes=m.MAX_ARTIFACT_PREVIEW_UTF8_BYTES,
    max_attempted_nodes=20_000,
    max_attempted_files=3 * m.MAX_ARTIFACT_PREVIEW_DOCUMENTS,
    max_attempted_bytes=2 * m.MAX_ARTIFACT_PREVIEW_UTF8_BYTES,
)
_TEXT_ARTIFACT_TYPES = frozenset(
    {
        m.ArtifactType.TEXT_MEMORY,
        m.ArtifactType.SKILL_BUNDLE,
        m.ArtifactType.AGENT_SYSTEM,
    }
)
_TEXT_ARTIFACT_MIME_TYPES = frozenset(
    {
        "application/json",
        "application/toml",
        "application/x-sh",
        "application/yaml",
        "text/css",
        "text/csv",
        "text/html",
        "text/javascript",
        "text/markdown",
        "text/plain",
        "text/x-python",
    }
)

_UNAVAILABLE_OPERATIONS = frozenset(
    {
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
    }
)


@dataclass(frozen=True, slots=True)
class _VerifiedArtifactDocument:
    document_id: str
    display_name: str
    relative_path: str
    mime_type: str
    content: str
    content_sha256: str
    byte_size: int


@dataclass(frozen=True, slots=True)
class _VerifiedArtifactContent:
    summary: m.ArtifactSummaryV1
    documents: tuple[_VerifiedArtifactDocument, ...]


class _RunControlHTTPError(CoreControlHTTPError):
    """Preserve run-owner error provenance through the frozen HTTP contract."""


class _ReleaseActivationReadinessHTTPError(CoreControlHTTPError):
    """A repairable release activation prerequisite failed before persistence."""


class _ProjectInFlightHTTPError(CoreControlHTTPError):
    """A transient project owner conflict that must not consume idempotency state."""


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


def _service_control_http_error(exc: CoreServiceControlError) -> CoreControlHTTPError:
    return _error(
        503,
        code="core_service_supervisor_failed",
        message="Core could not inspect or control its managed services.",
        category=m.ErrorCategory.SERVICE,
        retryable=True,
        repair_action=m.RepairAction.OPENEVO_CAN_RETRY,
        next_action="Retry after Core service ownership is restored.",
    )


def _release_activation_error(
    *,
    code: str,
    message: str,
    category: m.ErrorCategory,
    repair_action: m.RepairAction,
    next_action: str,
) -> _ReleaseActivationReadinessHTTPError:
    return _ReleaseActivationReadinessHTTPError(
        503,
        code=code,
        message=message,
        category=category,
        retryable=True,
        repair_action=repair_action,
        next_action=next_action,
    )


def _release_readiness_error(
    code: ServiceRunReadinessCode,
) -> _ReleaseActivationReadinessHTTPError:
    errors = {
        "codex_cli_unavailable": (
            "Codex CLI is unavailable for the remote SSH user.",
            m.ErrorCategory.ENVIRONMENT,
            m.RepairAction.USER_ACTION_REQUIRED,
            (
                "Install the supported Codex CLI as the current remote SSH user, "
                "then retry activation."
            ),
        ),
        "codex_subscription_auth_unavailable": (
            "Codex subscription login is unavailable for the remote SSH user.",
            m.ErrorCategory.AUTHENTICATION,
            m.RepairAction.USER_ACTION_REQUIRED,
            "Sign in with Codex CLI as the current remote SSH user, then retry activation.",
        ),
        "runtime_executable_unavailable": (
            "The managed Science runtime executable is unavailable.",
            m.ErrorCategory.ENVIRONMENT,
            m.RepairAction.USER_ACTION_REQUIRED,
            "Restore the supported container runtime for the SSH user, then retry activation.",
        ),
        "runtime_image_unavailable": (
            "The managed Science runtime image is unavailable.",
            m.ErrorCategory.ENVIRONMENT,
            m.RepairAction.OPENEVO_CAN_INSTALL,
            "Repair the managed Science runtime installation, then retry activation.",
        ),
        "runtime_evidence_invalid": (
            "Managed Science runtime readiness evidence is invalid.",
            m.ErrorCategory.ENVIRONMENT,
            m.RepairAction.OPENEVO_CAN_RECONFIGURE,
            "Repair the managed Science runtime installation, then retry activation.",
        ),
        "service_group_unavailable": (
            "Required OpenEvo Daemon services are unavailable.",
            m.ErrorCategory.SERVICE,
            m.RepairAction.OPENEVO_CAN_RETRY,
            "Reconnect OpenEvo Daemon, then retry project activation.",
        ),
        "run_admission_unavailable": (
            "The managed science run admission owner is unavailable.",
            m.ErrorCategory.SERVICE,
            m.RepairAction.OPENEVO_CAN_RETRY,
            "Restart OpenEvo Daemon, then retry project activation.",
        ),
        "self_deployed_unavailable": (
            "The managed service group does not support this release project.",
            m.ErrorCategory.PROJECT,
            m.RepairAction.UNSUPPORTED,
            "Select Codex subscription transcript for this Preview release.",
        ),
    }
    readiness_code = code.value
    message, category, repair_action, next_action = errors.get(
        readiness_code,
        (
            "Managed Science readiness is unavailable.",
            m.ErrorCategory.SERVICE,
            m.RepairAction.OPENEVO_CAN_RETRY,
            "Reconnect OpenEvo Daemon, then retry project activation.",
        ),
    )
    return _release_activation_error(
        code=f"project_activation_{readiness_code}",
        message=message,
        category=category,
        repair_action=repair_action,
        next_action=next_action,
    )


class _RunMutationFlight:
    __slots__ = (
        "drained",
        "future",
        "identity",
        "owner_active",
        "request_digest",
        "waiters",
    )

    def __init__(self, identity: tuple[str, str, str], request_digest: str) -> None:
        self.drained: Future[None] = Future()
        self.future: Future[object] = Future()
        self.identity = identity
        self.owner_active = True
        self.request_digest = request_digest
        self.waiters = 0


class _RunMutationSingleFlight:
    def __init__(self, capacity: int) -> None:
        if capacity < 1:
            raise ValueError("run mutation single-flight capacity must be positive")
        self._capacity = capacity
        self._lock = threading.Lock()
        self._entries: OrderedDict[tuple[str, str, str], _RunMutationFlight] = OrderedDict()
        self._retired: set[_RunMutationFlight] = set()
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
                if not hmac.compare_digest(entry.request_digest, request_digest):
                    raise _idempotency_conflict_error()
                entry.waiters += 1
                future = entry.future
                self._entries.move_to_end(identity)
                owner = False
            else:
                for retired in self._retired:
                    if retired.identity == identity and not hmac.compare_digest(
                        retired.request_digest, request_digest
                    ):
                        raise _idempotency_conflict_error()
                self._evict_completed_locked()
                if len(self._entries) + len(self._retired) >= self._capacity:
                    raise _run_mutation_capacity_error()
                entry = _RunMutationFlight(identity, request_digest)
                future = entry.future
                self._entries[identity] = entry
                owner = True

        if not owner:
            try:
                try:
                    return future.result()
                except CoreControlHTTPError as exc:
                    replay = CoreControlHTTPError.from_error(exc.error)
                    replay.headers = dict(exc.headers)
                    raise replay from exc
            finally:
                self._release_waiter(entry)

        try:
            result = call()
        except BaseException as exc:
            with self._lock:
                retained = self._entries.get(identity)
                if retained is entry:
                    del self._entries[identity]
                future.set_exception(exc)
                entry.owner_active = False
                if entry.waiters > 0 and not self._closing:
                    self._retired.add(entry)
                self._resolve_drain_locked(entry)
            raise
        with self._lock:
            if self._closing:
                retained = self._entries.get(identity)
                if retained is entry:
                    del self._entries[identity]
            else:
                self._entries.move_to_end(identity)
            future.set_result(result)
            entry.owner_active = False
            self._resolve_drain_locked(entry)
        return result

    def close(self) -> tuple[Future[None], ...]:
        with self._lock:
            self._closing = True
            entries = tuple(self._entries.values()) + tuple(self._retired)
            for entry in entries:
                self._resolve_drain_locked(entry)
            self._entries.clear()
            self._retired.clear()
            return tuple(entry.drained for entry in entries)

    def _release_waiter(self, entry: _RunMutationFlight) -> None:
        with self._lock:
            entry.waiters -= 1
            if entry.waiters == 0:
                self._retired.discard(entry)
            self._resolve_drain_locked(entry)

    def _resolve_drain_locked(self, entry: _RunMutationFlight) -> None:
        if (
            self._closing
            and not entry.owner_active
            and entry.waiters == 0
            and not entry.drained.done()
        ):
            entry.drained.set_result(None)

    def _evict_completed_locked(self) -> None:
        while len(self._entries) + len(self._retired) >= self._capacity:
            completed_identity = next(
                (
                    identity
                    for identity, entry in self._entries.items()
                    if not entry.owner_active and entry.waiters == 0 and entry.future.done()
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
        evolution_artifact_root: str | Path | None = None,
        artifact_loader: Callable[[str], Mapping[str, object]] | None = None,
        clock: Callable[[], datetime] | None = None,
        _enable_maintenance_owner_for_tests: bool = False,
    ) -> None:
        try:
            token_bytes = bearer_token.encode("ascii")
        except UnicodeEncodeError as exc:
            raise ValueError("Core bearer token must be ASCII") from exc
        if not token_bytes or any(value <= 32 or value == 127 for value in token_bytes):
            raise ValueError("Core bearer token must be non-empty and contain no whitespace")
        if evolution_registry is not None:
            require_verified_executable_registry(evolution_registry)
        resolved_build_channel = (
            m.BuildChannel(build_channel) if isinstance(build_channel, str) else build_channel
        )
        if (
            _enable_maintenance_owner_for_tests
            and resolved_build_channel is not m.BuildChannel.TEST
        ):
            raise ValueError("the maintenance owner test seam is allowed only in test builds")
        self.store = store
        self._executor = ThreadPoolExecutor(
            max_workers=4, thread_name_prefix="openevo-core-control"
        )
        self._close_lock = threading.Lock()
        self._closed = False
        self._run_mutations = _RunMutationSingleFlight(_RUN_MUTATION_SINGLEFLIGHT_CAPACITY)
        self._run_maintenance_gate = threading.RLock()
        self._run_mutation_drain: tuple[Future[None], ...] | None = None
        self._authorization = b"Bearer " + token_bytes
        self._registry = evolution_registry
        self._service_supervisor = service_supervisor
        self._run_control = run_control
        coordinator = (
            None
            if run_control is None
            else getattr(run_control, "project_in_flight_coordinator", None)
        )
        if coordinator is not None and not isinstance(
            coordinator, ProjectInFlightCoordinator
        ):
            raise ValueError("run control project coordinator is invalid")
        self._project_in_flight = coordinator
        self._maintenance = (
            CoreMaintenanceOwnerV1(
                store,
                registry=evolution_registry,
                service_control=service_supervisor,
                run_control=run_control,
                clock=clock,
            )
            if _enable_maintenance_owner_for_tests
            else None
        )
        self._evolution_artifact_root = (
            None
            if evolution_artifact_root is None
            else Path(evolution_artifact_root).expanduser().absolute()
        )
        self._artifact_loader = artifact_loader
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._started_at = self._timestamp()
        self._build_channel = resolved_build_channel
        self._version = m.VersionResponseV1(
            preferred_major=1,
            supported_majors=[1],
            openapi_sha256=openapi_sha256(),
            build_version=build_version,
            source_commit=source_commit,
            build_channel=self._build_channel,
            provider_kind=m.ProviderKind.OPENEVO_CORE,
            features=[
                *_BASE_FEATURES,
                *(
                    [m.FeatureFlag.DIAGNOSTICS]
                    if (
                        self._maintenance is not None
                        and self._maintenance.diagnostics_available
                        and self._maintenance.service_control_available
                        and self._maintenance.maintenance_available
                    )
                    else []
                ),
            ],
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
            "getCoreArtifactV1": self._get_artifact,
            "getCoreArtifactContentV1": self._get_artifact_content,
            "getCoreArtifactDiffV1": self._get_artifact_diff,
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
        if self._maintenance is not None:
            self._handlers.update(
                {
                    "doctorCoreEnvironmentV1": self._doctor,
                    "repairCoreEnvironmentV1": self._repair,
                    "restartCoreServiceV1": self._restart_service,
                    "getCoreServiceLogsV1": self._service_logs,
                    "getCoreOperationV1": self._get_operation,
                    "cancelCoreOperationV1": self._cancel_operation,
                    "getCoreLogsByRefV1": self._referenced_logs,
                    "createCoreDiagnosticV1": self._create_diagnostic,
                    "getCoreDiagnosticV1": self._get_diagnostic,
                    "deleteCoreDiagnosticV1": self._delete_diagnostic,
                    "cleanupCoreCachesV1": self._cleanup_caches,
                }
            )

    def close(self) -> None:
        with self._close_lock:
            if self._closed:
                return
            if self._run_mutation_drain is None:
                self._run_mutation_drain = self._run_mutations.close()
            _, pending_run_mutations = wait(
                self._run_mutation_drain,
                timeout=_RUN_MUTATION_SHUTDOWN_DRAIN_TIMEOUT_SECONDS,
            )
            if pending_run_mutations:
                raise RuntimeError("admitted run mutations did not drain before shutdown timeout")
            if self._run_control is not None:
                self._run_control.close()
                self._run_control = None
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
        return (
            frozenset(self._handlers) | _UNAVAILABLE_OPERATIONS | _MAINTENANCE_OWNER_OPERATION_IDS
        )

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
            if not (
                (isinstance(exc, _RunControlHTTPError) and exc.error.retryable)
                or isinstance(exc, _ReleaseActivationReadinessHTTPError)
                or isinstance(exc, _ProjectInFlightHTTPError)
            ):
                self.store.record_failed_idempotency(operation_id, arguments, exc.error)
            raise

    async def invoke_async(self, operation_id: str, arguments: Mapping[str, object]) -> object:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, self.invoke, operation_id, arguments)

    def _invoke(self, operation_id: str, arguments: Mapping[str, object]) -> object:
        if operation_id in RUN_OPERATION_IDS and self._run_control is not None:
            try:
                if operation_id == "createCoreRunV1":
                    with self._run_maintenance_gate:
                        return self._run_control.invoke(operation_id, arguments)
                return self._run_control.invoke(operation_id, arguments)
            except CoreRunControlError as exc:
                raise _run_control_http_error(exc) from exc
            except CoreServiceControlError as exc:
                raise _service_control_http_error(exc) from exc
        if operation_id in _UNAVAILABLE_OPERATIONS:
            self._unavailable(operation_id)
        handler = self._handlers.get(operation_id)
        if handler is None:
            self._unavailable(operation_id)
        try:
            if operation_id in _SYSTEM_MAINTENANCE_START_OPERATIONS:
                with self._run_maintenance_gate:
                    return handler(arguments)
            if (
                operation_id in _PROJECT_OWNER_GUARDED_OPERATION_IDS
                and self._project_in_flight is not None
            ):
                project_id = cast(str, arguments["project_id"])
                with self._project_in_flight.guard_project_mutation(
                    project_id,
                    exact_replay=lambda: self.store.has_successful_idempotency_replay(
                        operation_id,
                        arguments,
                    ),
                ):
                    return handler(arguments)
            return handler(arguments)
        except CoreControlHTTPError:
            raise
        except CoreRunControlError as exc:
            raise _run_control_http_error(exc) from exc
        except ScienceProjectInFlight as exc:
            raise _ProjectInFlightHTTPError(
                409,
                code="project_in_flight",
                message="The project has an admitted task or successor transition in flight.",
                category=m.ErrorCategory.PROJECT,
                retryable=True,
                repair_action=m.RepairAction.OPENEVO_CAN_RETRY,
                next_action="Wait for the current project task or transition to resolve.",
            ) from exc
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
            raise _service_control_http_error(exc) from exc

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

    def _doctor(self, arguments: Mapping[str, object]) -> Response:
        return _stored_response(
            self._maintenance_owner("doctorCoreEnvironmentV1").doctor(
                cast(m.EnvironmentDoctorRequestV1, arguments["request"]),
                idempotency_key=cast(str, arguments["idempotency_key"]),
            )
        )

    def _repair(self, arguments: Mapping[str, object]) -> Response:
        return _stored_response(
            self._maintenance_owner("repairCoreEnvironmentV1").repair(
                cast(m.EnvironmentRepairRequestV1, arguments["request"]),
                idempotency_key=cast(str, arguments["idempotency_key"]),
            )
        )

    def _capabilities(self, arguments: Mapping[str, object]) -> object:
        execution_mode = cast(m.ExecutionMode, arguments["execution_mode"])
        self._require_release_execution_mode(execution_mode)
        registry = self._require_registry("capabilities")
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
        request = cast(m.ProjectCreateV1, arguments["request"])
        self._preflight_release_project_spec(request.spec)
        result = self.store.create_project(
            request,
            idempotency_key=cast(str, arguments["idempotency_key"]),
            registry_digest=self._registry_digest(),
        )
        return _stored_response(result)

    def _get_project(self, arguments: Mapping[str, object]) -> Response:
        project = self.store.get_project(cast(str, arguments["project_id"]))
        return _model_response(project, etag=project.etag)

    def _patch_project(self, arguments: Mapping[str, object]) -> Response:
        project_id = cast(str, arguments["project_id"])
        request = cast(m.ProjectPatchV1, arguments["request"])
        if self._build_channel is m.BuildChannel.RELEASE:
            spec = request.spec
            if spec is None:
                spec = self.store.get_project(project_id).spec
            self._preflight_release_project_spec(spec)
        result = self.store.patch_project(
            project_id,
            request,
            if_match=cast(str, arguments["if_match"]),
            idempotency_key=cast(str, arguments["idempotency_key"]),
            registry_digest=self._registry_digest(),
        )
        return _stored_response(result)

    def _require_release_execution_mode(self, execution_mode: m.ExecutionMode) -> None:
        if (
            self._build_channel is m.BuildChannel.RELEASE
            and execution_mode is not m.ExecutionMode.CODEX_SUBSCRIPTION_TRANSCRIPT
        ):
            raise _error(
                422,
                code="release_execution_mode_unsupported",
                message="This OpenEvo release supports Codex subscription transcript only.",
                category=m.ErrorCategory.PROJECT,
                retryable=False,
                repair_action=m.RepairAction.UNSUPPORTED,
                next_action="Select Codex subscription transcript for this Preview release.",
            )

    def _preflight_release_project_spec(self, spec: m.ProjectSpecV1) -> None:
        if self._build_channel is not m.BuildChannel.RELEASE:
            return
        self._require_release_execution_mode(spec.execution_mode)
        if spec.capture_mode is not m.CaptureMode.TRANSCRIPT or spec.harness_id != "codex":
            raise _error(
                422,
                code="release_project_spec_unsupported",
                message="This OpenEvo release requires the Codex transcript project profile.",
                category=m.ErrorCategory.PROJECT,
                retryable=False,
                repair_action=m.RepairAction.UNSUPPORTED,
                next_action="Use the Codex harness with transcript capture for this project.",
            )
        from openevo.backend.service_supervisor import (
            ServiceExecutionMode,
            ServiceGroupSnapshot,
            ServiceRunReadinessCode,
        )

        supervisor = self._service_supervisor
        ensure = None if supervisor is None else getattr(supervisor, "ensure", None)
        if not callable(ensure):
            raise _release_activation_error(
                code="project_activation_service_supervisor_unavailable",
                message="Managed service readiness cannot be verified by this Core daemon.",
                category=m.ErrorCategory.SERVICE,
                repair_action=m.RepairAction.OPENEVO_CAN_RETRY,
                next_action="Restart or update OpenEvo Daemon, then retry project activation.",
            )
        image = MANAGED_RUNTIME_IMAGES["managed_science"]
        try:
            snapshot = ensure(
                ServiceExecutionMode.CODEX_SUBSCRIPTION_TRANSCRIPT,
                codex_model=spec.agent_model_ref,
                runtime_image=image,
            )
        except CoreServiceControlError as exc:
            raise _release_activation_error(
                code="project_activation_service_supervisor_failed",
                message="OpenEvo Daemon could not verify managed service readiness.",
                category=m.ErrorCategory.SERVICE,
                repair_action=m.RepairAction.OPENEVO_CAN_RETRY,
                next_action="Retry after OpenEvo Daemon service ownership is restored.",
            ) from exc
        if not isinstance(snapshot, ServiceGroupSnapshot):
            raise _release_activation_error(
                code="project_activation_service_snapshot_invalid",
                message="OpenEvo Daemon received invalid managed service readiness evidence.",
                category=m.ErrorCategory.SERVICE,
                repair_action=m.RepairAction.OPENEVO_CAN_RETRY,
                next_action="Restart or update OpenEvo Daemon, then retry project activation.",
            )
        if snapshot.execution_mode is not ServiceExecutionMode.CODEX_SUBSCRIPTION_TRANSCRIPT:
            raise _release_activation_error(
                code="project_activation_service_mode_mismatch",
                message="Managed services do not match the Codex subscription project mode.",
                category=m.ErrorCategory.SERVICE,
                repair_action=m.RepairAction.OPENEVO_CAN_RETRY,
                next_action="Reconnect OpenEvo Daemon, then retry project activation.",
            )
        if not snapshot.run_ready:
            raise _release_readiness_error(snapshot.run_readiness_code)
        if not snapshot.services_available:
            raise _release_readiness_error(ServiceRunReadinessCode.SERVICE_GROUP_UNAVAILABLE)
        if snapshot.runtime_image != image:
            raise _release_activation_error(
                code="project_activation_runtime_image_mismatch",
                message="Managed services do not use the required Science runtime image.",
                category=m.ErrorCategory.ENVIRONMENT,
                repair_action=m.RepairAction.OPENEVO_CAN_INSTALL,
                next_action="Repair the managed Science runtime, then retry project activation.",
            )

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

    def _get_artifact(self, arguments: Mapping[str, object]) -> m.ArtifactSummaryV1:
        return self._artifact_summary(
            cast(str, arguments["project_id"]),
            cast(str, arguments["artifact_id"]),
            require_current=True,
        )

    def _get_artifact_content(self, arguments: Mapping[str, object]) -> m.ArtifactContentV1:
        verified = self._verified_artifact_content(
            self._artifact_summary(
                cast(str, arguments["project_id"]),
                cast(str, arguments["artifact_id"]),
                require_current=True,
            )
        )
        documents = [
            m.ArtifactDocumentPreviewV1(
                document_id=document.document_id,
                display_name=document.display_name,
                relative_path=document.relative_path,
                mime_type=document.mime_type,
                content=document.content,
                content_sha256=document.content_sha256,
                byte_size=document.byte_size,
                truncated=False,
            )
            for document in verified.documents
        ]
        total_bytes = sum(document.byte_size for document in verified.documents)
        return m.ArtifactContentV1(
            artifact_id=verified.summary.id,
            artifact_type=verified.summary.artifact_type,
            documents=documents,
            total_documents=len(documents),
            total_utf8_bytes=total_bytes,
            returned_utf8_bytes=total_bytes,
            truncated=False,
        )

    def _get_artifact_diff(self, arguments: Mapping[str, object]) -> m.ArtifactDiffV1:
        project_id = cast(str, arguments["project_id"])
        current = self._artifact_summary(
            project_id,
            cast(str, arguments["artifact_id"]),
            require_current=True,
        )
        previous = self._previous_artifact_summary(
            current,
            cast(str | None, arguments["previous_artifact_id"]),
        )
        current_content = self._verified_artifact_content(current)
        previous_content = self._verified_artifact_content(previous)
        return _artifact_diff(previous_content, current_content)

    def _artifact_summary(
        self,
        project_id: str,
        artifact_id: str,
        *,
        require_current: bool,
    ) -> m.ArtifactSummaryV1:
        try:
            reachability = self.store.artifact_reachability(
                project_id,
                artifact_id,
                require_current=require_current,
            )
        except StoreCorruptionError as exc:
            raise _artifact_authority_error() from exc
        if not reachability:
            raise ResourceNotFoundError("artifact", artifact_id)
        if self._run_control is None:
            raise _artifact_authority_error()

        matches: list[m.ArtifactSummaryBaseV1] = []
        for reachable in reachability:
            after: str | None = None
            seen_cursors: set[str] = set()
            for _page_number in range(_MAX_ARTIFACT_PAGES_PER_RUN):
                try:
                    page = self._run_control.invoke(
                        "listCoreRunArtifactsV1",
                        {
                            "run_id": reachable.run_id,
                            "limit": _ARTIFACT_PAGE_LIMIT,
                            "after": after,
                            "sort": "created_at",
                            "direction": "asc",
                            "artifact_type": reachable.artifact_type,
                        },
                    )
                except CoreRunControlError as exc:
                    raise _artifact_authority_error() from exc
                if not isinstance(page, m.ArtifactPageV1):
                    _raise_artifact_authority_error(
                        "run artifact authority returned the wrong type"
                    )
                for item in page.items:
                    if item.id != artifact_id:
                        continue
                    if not _summary_matches_reachability(item, reachable):
                        _raise_artifact_authority_error(
                            "run artifact summary does not match revision reachability"
                        )
                    matches.append(item)
                if not page.has_more:
                    break
                if page.next_cursor is None or page.next_cursor in seen_cursors:
                    _raise_artifact_authority_error("run artifact pagination did not advance")
                seen_cursors.add(page.next_cursor)
                after = page.next_cursor
            else:
                _raise_artifact_authority_error(
                    "run artifact pagination exceeded its closed bound"
                )
        if len(matches) != 1:
            _raise_artifact_authority_error(
                "artifact does not have exactly one authoritative run output"
            )
        return cast(m.ArtifactSummaryV1, matches[0])

    def _previous_artifact_summary(
        self,
        current: m.ArtifactSummaryV1,
        requested_id: str | None,
    ) -> m.ArtifactSummaryV1:
        source_ids = current.lineage.source_artifact_ids
        if requested_id is not None and requested_id not in source_ids:
            raise ResourceNotFoundError("artifact", requested_id)
        candidate_ids = [requested_id] if requested_id is not None else list(source_ids)
        if len(candidate_ids) > _MAX_ARTIFACT_SOURCE_REVISIONS:
            raise StoreCorruptionError("artifact lineage exceeds its diff source bound")
        candidates: list[m.ArtifactSummaryBaseV1] = []
        for candidate_id in candidate_ids:
            assert candidate_id is not None
            try:
                candidate = self._artifact_summary(
                    current.project_id,
                    candidate_id,
                    require_current=False,
                )
            except ResourceNotFoundError:
                if requested_id is not None:
                    raise
                continue
            if _is_valid_diff_predecessor(candidate, current):
                candidates.append(candidate)
            elif requested_id is not None:
                raise ResourceNotFoundError("artifact", requested_id)
        if not candidates:
            raise _error(
                409,
                code="artifact_diff_base_missing",
                message="The artifact has no reachable compatible predecessor.",
                category=m.ErrorCategory.ARTIFACT,
                retryable=False,
                repair_action=m.RepairAction.USER_ACTION_REQUIRED,
                next_action="Select an artifact whose lineage contains a prior revision output.",
            )
        candidates.sort(
            key=lambda item: (item.produced_revision.generation, item.created_at, item.id)
        )
        return cast(m.ArtifactSummaryV1, candidates[-1])

    def _verified_artifact_content(self, summary: m.ArtifactSummaryV1) -> _VerifiedArtifactContent:
        if summary.artifact_type not in _TEXT_ARTIFACT_TYPES:
            raise _artifact_content_error(
                "artifact_content_type_unsupported",
                "This typed artifact does not expose UTF-8 document content.",
            )
        if summary.byte_size > m.MAX_ARTIFACT_PREVIEW_UTF8_BYTES:
            raise _artifact_content_error(
                "artifact_content_oversize",
                "The artifact exceeds the bounded inspection byte budget.",
            )
        if self._evolution_artifact_root is None:
            raise _artifact_authority_error()
        record = self._load_evolution_artifact(summary.id)
        try:
            if (
                record.artifact_id != summary.id
                or m.ArtifactType(str(record.type)) is not summary.artifact_type
                or record.name != summary.display_name
                or record.state is not EvolutionArtifactState.ACTIVE
            ):
                raise ValueError("artifact metadata identity does not match its summary")
            with ArtifactPayloadService(
                self._evolution_artifact_root,
                limits=_ARTIFACT_INSPECTION_LIMITS,
            ) as payloads:
                snapshot = payloads.issue_snapshot(
                    artifact_id=record.artifact_id,
                    artifact_type=str(record.type),
                    name=record.name,
                    uri=record.uri,
                    manifest=record.manifest,
                    scores=record.scores,
                    rank_index=0,
                )
                payload_bytes = sum(entry.size_bytes for entry in snapshot.payload_entries)
                if (
                    snapshot.payload_manifest_digest != summary.content_sha256
                    or payload_bytes != summary.byte_size
                ):
                    raise ValueError("artifact payload no longer matches its run summary")
                if (
                    len(snapshot.payload_entries) > m.MAX_ARTIFACT_PREVIEW_DOCUMENTS
                    or payload_bytes > m.MAX_ARTIFACT_PREVIEW_UTF8_BYTES
                    or any(
                        entry.size_bytes > MAX_CONTRIBUTION_TEXT
                        for entry in snapshot.payload_entries
                    )
                ):
                    raise ArtifactPayloadBudgetExceeded(
                        "artifact exceeds the inspection preview budget"
                    )
                if any(
                    entry.media_type not in _TEXT_ARTIFACT_MIME_TYPES
                    for entry in snapshot.payload_entries
                ):
                    raise ValueError("artifact payload contains a non-text document")
                selected_paths = _selected_artifact_paths(summary, record, snapshot)
                entries = {entry.relative_path: entry for entry in snapshot.payload_entries}
                if set(selected_paths) != set(entries):
                    raise ValueError(
                        "artifact text inventory contains documents outside its typed projection"
                    )
                documents: list[_VerifiedArtifactDocument] = []
                for relative_path in selected_paths:
                    entry = entries[relative_path]
                    content = payloads.read_utf8_prefix(
                        snapshot.payload_handle,
                        relative_path,
                        max_chars=entry.size_bytes,
                        max_bytes=entry.size_bytes,
                    )
                    if len(content.encode("utf-8")) != entry.size_bytes:
                        raise ValueError("artifact document read was unexpectedly truncated")
                    display_name = PurePosixPath(relative_path).name
                    if not display_name or len(display_name) > 128:
                        raise ValueError("artifact document display name is invalid")
                    documents.append(
                        _VerifiedArtifactDocument(
                            document_id=_artifact_document_id(summary.id, relative_path),
                            display_name=display_name,
                            relative_path=relative_path,
                            mime_type=entry.media_type,
                            content=content,
                            content_sha256=entry.sha256,
                            byte_size=entry.size_bytes,
                        )
                    )
                payloads.verify_inventory_identity(snapshot.payload_handle)
        except ArtifactPayloadBudgetExceeded as exc:
            raise _artifact_content_error(
                "artifact_content_oversize",
                "The artifact exceeds the bounded inspection byte budget.",
            ) from exc
        except (OSError, ValueError) as exc:
            raise _artifact_content_error(
                "artifact_content_invalid",
                "The artifact payload failed verified UTF-8 inspection.",
            ) from exc
        return _VerifiedArtifactContent(summary=summary, documents=tuple(documents))

    def _load_evolution_artifact(self, artifact_id: str) -> EvolutionArtifactResponse:
        try:
            if self._artifact_loader is not None:
                payload = self._artifact_loader(artifact_id)
            else:
                binding_factory = getattr(self._service_supervisor, "run_binding", None)
                if not callable(binding_factory):
                    raise RuntimeError("evolution artifact authority is not bound")
                binding = binding_factory()
                with EvolutionHttpClient(
                    binding.evolution_backend_url,
                    headers=binding.request_headers(),
                ) as client:
                    payload = client.get_artifact(artifact_id)
            return EvolutionArtifactResponse.model_validate(payload)
        except CoreControlHTTPError:
            raise
        except (httpx.HTTPError, RuntimeError, TypeError, ValueError) as exc:
            raise _artifact_authority_error() from exc

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

    def _restart_service(self, arguments: Mapping[str, object]) -> Response:
        return _stored_response(
            self._maintenance_owner("restartCoreServiceV1").restart_service(
                cast(str, arguments["service_id"]),
                cast(m.ServiceRestartRequestV1, arguments["request"]),
                if_match=cast(str, arguments["if_match"]),
                idempotency_key=cast(str, arguments["idempotency_key"]),
            )
        )

    def _service_logs(self, arguments: Mapping[str, object]) -> m.LogPageV1:
        return self._maintenance_owner("getCoreServiceLogsV1").service_logs(
            cast(str, arguments["service_id"]),
            limit=cast(int, arguments["limit"]),
            after=cast(str | None, arguments["after"]),
            sort=cast(str, arguments["sort"]),
            direction=cast(str, arguments["direction"]),
        )

    def _get_operation(self, arguments: Mapping[str, object]) -> Response:
        operation = self._maintenance_owner("getCoreOperationV1").get_operation(
            cast(str, arguments["operation_id"])
        )
        return _model_response(operation, etag=operation.etag)

    def _cancel_operation(self, arguments: Mapping[str, object]) -> Response:
        return _stored_response(
            self._maintenance_owner("cancelCoreOperationV1").cancel_operation(
                cast(str, arguments["operation_id"]),
                cast(m.OperationCancelRequestV1, arguments["request"]),
                if_match=cast(str, arguments["if_match"]),
                idempotency_key=cast(str, arguments["idempotency_key"]),
            )
        )

    def _referenced_logs(self, arguments: Mapping[str, object]) -> m.ReferencedLogPageV1:
        return self._maintenance_owner("getCoreLogsByRefV1").referenced_logs(
            cast(str, arguments["logs_ref"]),
            limit=cast(int, arguments["limit"]),
            after=cast(str | None, arguments["after"]),
            sort=cast(str, arguments["sort"]),
            direction=cast(str, arguments["direction"]),
        )

    def _create_diagnostic(self, arguments: Mapping[str, object]) -> Response:
        return _stored_response(
            self._maintenance_owner("createCoreDiagnosticV1").create_diagnostic(
                cast(m.DiagnosticsRequestV1, arguments["request"]),
                idempotency_key=cast(str, arguments["idempotency_key"]),
            )
        )

    def _get_diagnostic(self, arguments: Mapping[str, object]) -> Response:
        diagnostic = self._maintenance_owner("getCoreDiagnosticV1").get_diagnostic(
            cast(str, arguments["diagnostic_id"])
        )
        return _model_response(diagnostic, etag=diagnostic.etag)

    def _delete_diagnostic(self, arguments: Mapping[str, object]) -> Response:
        return _stored_response(
            self._maintenance_owner("deleteCoreDiagnosticV1").delete_diagnostic(
                cast(str, arguments["diagnostic_id"]),
                if_match=cast(str, arguments["if_match"]),
                idempotency_key=cast(str, arguments["idempotency_key"]),
            )
        )

    def _cleanup_caches(self, arguments: Mapping[str, object]) -> Response:
        return _stored_response(
            self._maintenance_owner("cleanupCoreCachesV1").cleanup_caches(
                cast(m.CacheCleanupRequestV1, arguments["request"]),
                idempotency_key=cast(str, arguments["idempotency_key"]),
            )
        )

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
            for service in self._service_supervisor.list():
                contract = service.to_contract()
                if self._maintenance is None and contract.restartable:
                    contract = contract.model_copy(update={"restartable": False})
                services.append(contract)
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

    def _maintenance_owner(self, operation_id: str) -> CoreMaintenanceOwnerV1:
        if self._maintenance is None:
            self._unavailable(operation_id)
        return self._maintenance

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


def _summary_matches_reachability(
    summary: m.ArtifactSummaryBaseV1,
    reachable: ArtifactReachability,
) -> bool:
    return (
        summary.id == reachable.artifact_id
        and summary.artifact_type is reachable.artifact_type
        and summary.project_id == reachable.project_id
        and summary.run_id == reachable.run_id
        and summary.produced_revision == reachable.revision
        and reachable.revision in summary.membership_revisions
    )


def _is_valid_diff_predecessor(
    previous: m.ArtifactSummaryBaseV1,
    current: m.ArtifactSummaryBaseV1,
) -> bool:
    return (
        previous.id != current.id
        and previous.project_id == current.project_id
        and previous.target_id == current.target_id
        and previous.artifact_type is current.artifact_type
        and previous.produced_revision.generation < current.produced_revision.generation
    )


def _selected_artifact_paths(
    summary: m.ArtifactSummaryBaseV1,
    record: EvolutionArtifactResponse,
    snapshot: Any,
) -> tuple[str, ...]:
    entries = {entry.relative_path: entry for entry in snapshot.payload_entries}
    if summary.artifact_type is m.ArtifactType.SKILL_BUNDLE:
        if not isinstance(summary, m.SkillBundleArtifactSummaryV1):
            raise ValueError("skill artifact summary has the wrong type")
        if "SKILL.md" not in entries or summary.metadata.document_count != len(entries):
            raise ValueError("skill bundle inventory does not match its summary")
        return tuple(sorted(entries))
    if summary.artifact_type is m.ArtifactType.TEXT_MEMORY:
        if not isinstance(summary, m.TextMemoryArtifactSummaryV1):
            raise ValueError("text memory summary has the wrong type")
        content_path = record.manifest.get("content_path")
        record_count = record.manifest.get("record_count", 0)
        if content_path != "memory.md" or record_count != summary.metadata.record_count:
            raise ValueError("text memory manifest does not match its summary")
    elif summary.artifact_type is m.ArtifactType.AGENT_SYSTEM:
        if not isinstance(summary, m.AgentSystemArtifactSummaryV1):
            raise ValueError("agent system summary has the wrong type")
        content_path = record.manifest.get("content_path")
        if record.manifest.get("target_path") != summary.metadata.target_path:
            raise ValueError("agent system target does not match its summary")
    else:
        raise ValueError("artifact type does not expose text documents")
    if not isinstance(content_path, str) or content_path not in entries:
        raise ValueError("artifact content path is absent from its verified inventory")
    return (content_path,)


def _artifact_document_id(artifact_id: str, relative_path: str) -> str:
    digest = hashlib.sha256(
        artifact_id.encode("utf-8") + b"\0" + relative_path.encode("utf-8")
    ).hexdigest()
    return f"document-{digest}"


def _artifact_content_error(code: str, message: str) -> CoreControlHTTPError:
    return _error(
        422,
        code=code,
        message=message,
        category=m.ErrorCategory.ARTIFACT,
        retryable=False,
        repair_action=m.RepairAction.USER_ACTION_REQUIRED,
        next_action="Regenerate the artifact from a supported bounded UTF-8 payload.",
    )


def _artifact_authority_error() -> CoreControlHTTPError:
    return _error(
        503,
        code="artifact_authority_invalid",
        message="Core could not verify the authoritative evolution artifact record.",
        category=m.ErrorCategory.ARTIFACT,
        retryable=True,
        repair_action=m.RepairAction.OPENEVO_CAN_RETRY,
        next_action="Retry after the managed evolution service is healthy.",
    )


def _raise_artifact_authority_error(message: str) -> NoReturn:
    raise _artifact_authority_error() from StoreCorruptionError(message)


def _diff_document_identity(
    artifact: _VerifiedArtifactContent,
    document: _VerifiedArtifactDocument,
) -> m.ArtifactDiffDocumentIdentityV1:
    return m.ArtifactDiffDocumentIdentityV1(
        artifact_id=artifact.summary.id,
        artifact_content_sha256=artifact.summary.content_sha256,
        document_id=document.document_id,
        relative_path=document.relative_path,
        content_sha256=document.content_sha256,
    )


def _artifact_diff(
    previous: _VerifiedArtifactContent,
    current: _VerifiedArtifactContent,
) -> m.ArtifactDiffV1:
    old_by_path = {document.relative_path: document for document in previous.documents}
    new_by_path = {document.relative_path: document for document in current.documents}
    if any(
        len(line) > 16_384
        for document in (*previous.documents, *current.documents)
        for line in document.content.splitlines()
    ):
        raise _artifact_content_error(
            "artifact_diff_oversize",
            "The artifact diff exceeds the bounded structured diff budget.",
        )
    total_input_lines = 0
    total_comparisons = 0
    for path in old_by_path.keys() | new_by_path.keys():
        old_lines = old_by_path[path].content.splitlines() if path in old_by_path else []
        new_lines = new_by_path[path].content.splitlines() if path in new_by_path else []
        total_input_lines += len(old_lines) + len(new_lines)
        if path in old_by_path and path in new_by_path:
            if (
                len(old_lines) > _MAX_ARTIFACT_DIFF_SEQUENCE_LINES
                or len(new_lines) > _MAX_ARTIFACT_DIFF_SEQUENCE_LINES
            ):
                raise _artifact_content_error(
                    "artifact_diff_oversize",
                    "The artifact diff exceeds the bounded structured diff budget.",
                )
            total_comparisons += len(old_lines) * len(new_lines)
    if (
        total_input_lines > _MAX_ARTIFACT_DIFF_INPUT_LINES
        or total_comparisons > _MAX_ARTIFACT_DIFF_COMPARISONS
    ):
        raise _artifact_content_error(
            "artifact_diff_oversize",
            "The artifact diff exceeds the bounded structured diff budget.",
        )
    changes: list[Any] = []

    for path in sorted(old_by_path.keys() & new_by_path.keys()):
        old_document = old_by_path[path]
        new_document = new_by_path[path]
        if old_document.content_sha256 == new_document.content_sha256:
            continue
        old_identity = _diff_document_identity(previous, old_document)
        new_identity = _diff_document_identity(current, new_document)
        changes.append(
            m.ModifiedArtifactDocumentChangeV1(
                kind=m.ArtifactDocumentChangeKind.MODIFIED,
                old_document=old_identity,
                new_document=new_identity,
                hunks=_modified_document_hunks(
                    old_document,
                    new_document,
                    old_identity=old_identity,
                    new_identity=new_identity,
                ),
            )
        )

    removed = {path: old_by_path[path] for path in old_by_path.keys() - new_by_path.keys()}
    added = {path: new_by_path[path] for path in new_by_path.keys() - old_by_path.keys()}
    for old_path in sorted(tuple(removed)):
        old_document = removed[old_path]
        new_path = next(
            (
                path
                for path in sorted(added)
                if added[path].content_sha256 == old_document.content_sha256
            ),
            None,
        )
        if new_path is None:
            continue
        new_document = added.pop(new_path)
        removed.pop(old_path)
        old_identity = _diff_document_identity(previous, old_document)
        new_identity = _diff_document_identity(current, new_document)
        changes.append(
            m.RenamedArtifactDocumentChangeV1(
                kind=m.ArtifactDocumentChangeKind.RENAMED,
                old_document=old_identity,
                new_document=new_identity,
                hunks=[],
            )
        )

    for path, old_document in sorted(removed.items()):
        old_identity = _diff_document_identity(previous, old_document)
        changes.append(
            m.RemovedArtifactDocumentChangeV1(
                kind=m.ArtifactDocumentChangeKind.REMOVED,
                old_document=old_identity,
                hunks=_whole_document_hunks(
                    old_document,
                    old_identity=old_identity,
                    new_identity=None,
                ),
            )
        )
    for path, new_document in sorted(added.items()):
        new_identity = _diff_document_identity(current, new_document)
        changes.append(
            m.AddedArtifactDocumentChangeV1(
                kind=m.ArtifactDocumentChangeKind.ADDED,
                new_document=new_identity,
                hunks=_whole_document_hunks(
                    new_document,
                    old_identity=None,
                    new_identity=new_identity,
                ),
            )
        )

    hunks = [hunk for change in changes for hunk in change.hunks]
    lines = [line for hunk in hunks for line in hunk.lines]
    if (
        len(changes) > m.MAX_ARTIFACT_PREVIEW_DOCUMENTS
        or len(hunks) > m.MAX_ARTIFACT_DIFF_HUNKS
        or len(lines) > m.MAX_ARTIFACT_DIFF_LINES
        or sum(len(line.text.encode("utf-8")) for line in lines)
        > m.MAX_ARTIFACT_PREVIEW_UTF8_BYTES
        or any(len(line.text) > 16_384 for line in lines)
    ):
        raise _artifact_content_error(
            "artifact_diff_oversize",
            "The artifact diff exceeds the bounded structured diff budget.",
        )
    return m.ArtifactDiffV1(
        artifact_id=current.summary.id,
        artifact_content_sha256=current.summary.content_sha256,
        previous_artifact_id=previous.summary.id,
        previous_artifact_content_sha256=previous.summary.content_sha256,
        document_changes=changes,
        total_document_changes=len(changes),
        total_hunks=len(hunks),
        total_lines=len(lines),
        truncated=False,
    )


def _whole_document_hunks(
    document: _VerifiedArtifactDocument,
    *,
    old_identity: m.ArtifactDiffDocumentIdentityV1 | None,
    new_identity: m.ArtifactDiffDocumentIdentityV1 | None,
) -> list[m.ArtifactDiffHunkV1]:
    text_lines = document.content.splitlines()
    if not text_lines:
        return []
    if len(text_lines) > 512:
        raise _artifact_content_error(
            "artifact_diff_oversize",
            "The artifact diff exceeds the bounded structured diff budget.",
        )
    if old_identity is None:
        lines = [
            m.ArtifactDiffLineV1(
                kind=m.DiffLineKind.ADDED,
                old_line_number=None,
                new_line_number=index,
                text=text,
            )
            for index, text in enumerate(text_lines, 1)
        ]
        return [
            m.ArtifactDiffHunkV1(
                old_document=None,
                new_document=new_identity,
                old_start=0,
                old_count=0,
                new_start=1,
                new_count=len(lines),
                lines=lines,
            )
        ]
    lines = [
        m.ArtifactDiffLineV1(
            kind=m.DiffLineKind.REMOVED,
            old_line_number=index,
            new_line_number=None,
            text=text,
        )
        for index, text in enumerate(text_lines, 1)
    ]
    return [
        m.ArtifactDiffHunkV1(
            old_document=old_identity,
            new_document=None,
            old_start=1,
            old_count=len(lines),
            new_start=0,
            new_count=0,
            lines=lines,
        )
    ]


def _modified_document_hunks(
    old_document: _VerifiedArtifactDocument,
    new_document: _VerifiedArtifactDocument,
    *,
    old_identity: m.ArtifactDiffDocumentIdentityV1,
    new_identity: m.ArtifactDiffDocumentIdentityV1,
) -> list[m.ArtifactDiffHunkV1]:
    old_lines = old_document.content.splitlines()
    new_lines = new_document.content.splitlines()
    matcher = difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False)
    hunks: list[m.ArtifactDiffHunkV1] = []
    for group in matcher.get_grouped_opcodes(3):
        lines: list[m.ArtifactDiffLineV1] = []
        old_start = group[0][1]
        new_start = group[0][3]
        old_count = 0
        new_count = 0
        for tag, old_begin, old_end, new_begin, new_end in group:
            if tag == "equal":
                for offset, text in enumerate(old_lines[old_begin:old_end]):
                    lines.append(
                        m.ArtifactDiffLineV1(
                            kind=m.DiffLineKind.CONTEXT,
                            old_line_number=old_begin + offset + 1,
                            new_line_number=new_begin + offset + 1,
                            text=text,
                        )
                    )
                old_count += old_end - old_begin
                new_count += new_end - new_begin
            if tag in {"delete", "replace"}:
                for offset, text in enumerate(old_lines[old_begin:old_end]):
                    lines.append(
                        m.ArtifactDiffLineV1(
                            kind=m.DiffLineKind.REMOVED,
                            old_line_number=old_begin + offset + 1,
                            new_line_number=None,
                            text=text,
                        )
                    )
                old_count += old_end - old_begin
            if tag in {"insert", "replace"}:
                for offset, text in enumerate(new_lines[new_begin:new_end]):
                    lines.append(
                        m.ArtifactDiffLineV1(
                            kind=m.DiffLineKind.ADDED,
                            old_line_number=None,
                            new_line_number=new_begin + offset + 1,
                            text=text,
                        )
                    )
                new_count += new_end - new_begin
        if len(lines) > 512:
            raise _artifact_content_error(
                "artifact_diff_oversize",
                "The artifact diff exceeds the bounded structured diff budget.",
            )
        hunks.append(
            m.ArtifactDiffHunkV1(
                old_document=old_identity,
                new_document=new_identity,
                old_start=old_start + 1 if old_count else old_start,
                old_count=old_count,
                new_start=new_start + 1 if new_count else new_start,
                new_count=new_count,
                lines=lines,
            )
        )
    return hunks


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
    evolution_artifact_root: str | Path | None = None,
    artifact_loader: Callable[[str], Mapping[str, object]] | None = None,
    event_replay_limit: int = 10_000,
    _enable_maintenance_owner_for_tests: bool = False,
) -> FastAPI:
    """Create a provider-backed app without adding a second route table."""

    if run_control is not None and run_control_factory is not None:
        raise ValueError("run_control and run_control_factory are mutually exclusive")
    resolved_build_channel = (
        m.BuildChannel(build_channel) if isinstance(build_channel, str) else build_channel
    )
    if _enable_maintenance_owner_for_tests and resolved_build_channel is not m.BuildChannel.TEST:
        raise ValueError("the maintenance owner test seam is allowed only in test builds")
    store = CoreControlStoreV1(
        state_root,
        event_replay_limit=event_replay_limit,
        _enable_maintenance_storage_for_tests=_enable_maintenance_owner_for_tests,
    )
    provider: CoreControlProviderV1 | None = None
    resolved_run_control = run_control
    resolved_artifact_root = evolution_artifact_root
    if resolved_artifact_root is None and service_supervisor is not None:
        resolved_artifact_root = (
            Path(state_root).expanduser().absolute().parent
            / "managed-services"
            / "evolution"
            / "artifacts"
        )
    try:
        if run_control_factory is not None:
            resolved_run_control = run_control_factory(store)
        provider = CoreControlProviderV1(
            store,
            bearer_token=bearer_token,
            build_version=build_version,
            source_commit=source_commit,
            build_channel=resolved_build_channel,
            evolution_registry=evolution_registry,
            service_supervisor=service_supervisor,
            run_control=resolved_run_control,
            evolution_artifact_root=resolved_artifact_root,
            artifact_loader=artifact_loader,
            _enable_maintenance_owner_for_tests=_enable_maintenance_owner_for_tests,
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
    if resource_type in {"service", "operation", "diagnostic", "logs"}:
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
