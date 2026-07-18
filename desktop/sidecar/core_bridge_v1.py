"""Active-project bridge from Desktop Local API intent to Core Control API v1."""

from __future__ import annotations

import base64
from collections.abc import Callable, Iterator
from concurrent.futures import Future, TimeoutError as FutureTimeoutError
from contextlib import AbstractContextManager
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
import hashlib
import json
import queue
import secrets
import threading
import time
from typing import Any, BinaryIO, Literal, Protocol, TypeVar

import httpx
from pydantic import ValidationError

from desktop.sidecar.contracts.v1 import models as local_v1
from desktop.sidecar.core_client_v1 import (
    CoreBootstrapTunnelConnectionV1,
    CoreClientErrorV1,
    CoreControlClientV1,
    CoreMutationOutcomeUnknownV1,
    CoreProjectBootstrapClientV1,
    CoreTunnelConnectionV1,
)
from openevo.backend.contracts.v1 import models as core_v1


DEFAULT_BRIDGE_TIMEOUT_SECONDS = 60.0
MAX_BRIDGE_TIMEOUT_SECONDS = 300.0
MAX_ACTIVATION_TIMEOUT_SECONDS = 900.0
WORKSPACE_CHUNK_BYTES = core_v1.MAX_WORKSPACE_CHUNK_BYTES
ADAPTER_WORKER_COUNT = 4
MAX_ADAPTER_QUEUE_SIZE = 64

_ResponseT = TypeVar("_ResponseT")


class _BoundedAdapterExecutor:
    def __init__(self) -> None:
        self._queue: queue.Queue[tuple[Future[Any], Callable[[], Any]]] = queue.Queue(
            maxsize=MAX_ADAPTER_QUEUE_SIZE
        )
        for index in range(ADAPTER_WORKER_COUNT):
            threading.Thread(
                target=self._worker,
                name=f"openevo-core-bridge-adapter-{index}",
                daemon=True,
            ).start()

    def submit(self, action: Callable[[], _ResponseT]) -> Future[_ResponseT] | None:
        future: Future[_ResponseT] = Future()
        try:
            self._queue.put_nowait((future, action))
        except queue.Full:
            return None
        return future

    def _worker(self) -> None:
        while True:
            future, action = self._queue.get()
            try:
                if future.set_running_or_notify_cancel():
                    try:
                        future.set_result(action())
                    except BaseException as exc:
                        future.set_exception(exc)
            finally:
                self._queue.task_done()


_ADAPTER_EXECUTOR = _BoundedAdapterExecutor()


@dataclass(eq=False, repr=False, slots=True)
class _ArchiveContextLease:
    context: AbstractContextManager[BinaryIO]
    lock: threading.Lock
    stream: BinaryIO | None = None
    entered: bool = False
    closed: bool = False

    def enter(self) -> BinaryIO:
        with self.lock:
            if self.closed:
                raise RuntimeError("archive context is closed")
            if not self.entered:
                self.stream = self.context.__enter__()
                self.entered = True
            assert self.stream is not None
            return self.stream

    def close(self) -> None:
        with self.lock:
            if self.closed:
                return
            if not self.entered:
                self.stream = self.context.__enter__()
                self.entered = True
            self.context.__exit__(None, None, None)
            self.closed = True


@dataclass(eq=False, repr=False, slots=True)
class _GenerationToken:
    generation: int
    external_lock: threading.RLock
    resource_lock: threading.Lock
    activation_id: str | None = None
    cancel_event: threading.Event | None = None
    cancelled: bool = False
    retired: bool = False
    clients: list[Any] | None = None
    tunnels: list[CoreTunnelHandleV1] | None = None
    archives: list[_ArchiveContextLease] | None = None
    adapter_futures: set[Future[Any]] | None = None

    def __post_init__(self) -> None:
        self.clients = []
        self.tunnels = []
        self.archives = []
        self.adapter_futures = set()

    def add_client(self, client: Any) -> None:
        with self.resource_lock:
            assert self.clients is not None
            self.clients.append(client)

    def add_tunnel(self, tunnel: CoreTunnelHandleV1) -> None:
        with self.resource_lock:
            assert self.tunnels is not None
            self.tunnels.append(tunnel)

    def add_archive(self, archive: _ArchiveContextLease) -> None:
        with self.resource_lock:
            assert self.archives is not None
            self.archives.append(archive)

    def track_future(self, future: Future[Any]) -> None:
        with self.resource_lock:
            assert self.adapter_futures is not None
            self.adapter_futures.add(future)

    def untrack_future(self, future: Future[Any]) -> None:
        with self.resource_lock:
            assert self.adapter_futures is not None
            self.adapter_futures.discard(future)


@dataclass(frozen=True, slots=True, repr=False)
class CoreHostAttachmentV1:
    """Host-global Core authority returned without exposing a Core URL."""

    profile_id: str
    remote_port: int
    bearer_token: str
    bearer_identity: str

    def __post_init__(self) -> None:
        if not self.profile_id or not self.bearer_identity:
            raise ValueError("Core host attachment identities must not be empty")
        if self.bearer_identity == self.bearer_token:
            raise ValueError("Core host bearer identity must not be the bearer secret")
        if not 1 <= self.remote_port <= 65_535:
            raise ValueError("Core host attachment remote port is invalid")
        if len(self.bearer_token) < 43:
            raise ValueError("Core host bearer must contain at least 256 bits")


class CoreHostService(Protocol):
    """Ensures or attaches the one host-global Core process."""

    def ensure_core(
        self,
        profile_id: str,
        *,
        deadline: float,
        cancel_event: threading.Event | None = None,
    ) -> CoreHostAttachmentV1: ...


class CoreTunnelHandleV1:
    """One private loopback tunnel owned by one bridge project session."""

    def __init__(
        self,
        *,
        endpoint: str,
        session_id: str,
        close_callback: Callable[[], None],
    ) -> None:
        self.endpoint = endpoint
        self.session_id = session_id
        self._close_callback = close_callback
        self._lock = threading.Lock()
        self._closed = False
        self._close_future: Future[None] | None = None
        self._close_failure: Literal["deadline_exceeded", "callback_failed"] | None = None

    def __repr__(self) -> str:
        return "CoreTunnelHandleV1(<private>)"

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    @property
    def close_failure(self) -> Literal["deadline_exceeded", "callback_failed"] | None:
        with self._lock:
            return self._close_failure

    def request_close(self, *, token: _GenerationToken | None = None) -> Future[None]:
        with self._lock:
            if self._closed:
                completed: Future[None] = Future()
                completed.set_result(None)
                return completed
            future = self._close_future
            if future is None:
                future = _ADAPTER_EXECUTOR.submit(self._close_callback)
                if future is None:
                    self._close_failure = "deadline_exceeded"
                    raise _bridge_error(
                        "core_tunnel_close_unavailable",
                        "The bounded tunnel close executor is full.",
                        retryable=True,
                    )
                self._close_future = future
                if token is not None:
                    token.track_future(future)
            return future

    def close(self, *, deadline: float, token: _GenerationToken | None = None) -> None:
        future = self.request_close(token=token)
        try:
            wait_timeout = _remaining_seconds(deadline)
        except DesktopCoreBridgeErrorV1:
            with self._lock:
                self._close_failure = "deadline_exceeded"
            raise
        try:
            future.result(timeout=wait_timeout)
        except FutureTimeoutError:
            if future.done():
                try:
                    future.result()
                except BaseException:
                    if token is not None:
                        token.untrack_future(future)
                    with self._lock:
                        if self._close_future is future:
                            self._close_future = None
                        self._close_failure = "callback_failed"
                    raise _bridge_error(
                        "core_tunnel_close_failed",
                        "The active Core tunnel close operation failed.",
                        retryable=True,
                    ) from None
                # The callback won the timeout boundary. Consume its success
                # below without clearing ownership or submitting it again.
            else:
                with self._lock:
                    self._close_failure = "deadline_exceeded"
                raise _bridge_error(
                    "core_tunnel_close_deadline_exceeded",
                    "The active Core tunnel did not close before the deadline.",
                    retryable=True,
                ) from None
        except BaseException:
            if token is not None:
                token.untrack_future(future)
            with self._lock:
                if self._close_future is future:
                    self._close_future = None
                self._close_failure = "callback_failed"
            raise _bridge_error(
                "core_tunnel_close_failed",
                "The active Core tunnel close operation failed.",
                retryable=True,
            ) from None
        if token is not None:
            token.untrack_future(future)
        with self._lock:
            if self._close_future is future:
                self._close_future = None
            self._close_failure = None
            self._closed = True


class CoreTunnelFactory(Protocol):
    def open_tunnel(
        self,
        *,
        profile_id: str,
        remote_port: int,
        session_id: str,
        deadline: float,
    ) -> CoreTunnelHandleV1: ...


class WorkspaceArchiveSource(Protocol):
    """Resolves only an already adopted opaque import reference to a stream."""

    def open_archive(
        self, ref: local_v1.WorkspaceImportRefV1
    ) -> AbstractContextManager[BinaryIO]: ...


class CoreProjectCreateStateV1(StrEnum):
    PRE_CREATE = "pre_create"
    UNKNOWN = "unknown"
    BOUND = "bound"


class CoreWorkspaceUploadAbortStateV1(StrEnum):
    PRE_ABORT = "pre_abort"
    UNKNOWN = "unknown"


class CoreWorkspaceUploadFinalizeStateV1(StrEnum):
    PRE_FINALIZE = "pre_finalize"
    UNKNOWN = "unknown"
    APPLIED = "applied"


@dataclass(frozen=True, slots=True)
class CoreWorkspaceUploadAbortOperationV1:
    upload: core_v1.WorkspaceUploadSessionV1
    request_sha256: str
    request: core_v1.WorkspaceUploadAbortV1
    idempotency_key: str
    state: CoreWorkspaceUploadAbortStateV1 = CoreWorkspaceUploadAbortStateV1.PRE_ABORT

    def __post_init__(self) -> None:
        if self.upload.status is not core_v1.WorkspaceUploadStatus.OPEN:
            raise ValueError("a durable abort operation must retain the open upload authority")
        if _model_digest(self.request) != self.request_sha256:
            raise ValueError("workspace abort request digest does not match canonical request")


@dataclass(frozen=True, slots=True)
class CoreWorkspaceUploadFinalizeAuthorityV1:
    upload: core_v1.WorkspaceUploadSessionV1
    request_sha256: str
    request: core_v1.WorkspaceUploadFinalizeV1
    idempotency_key: str
    upload_etag: str
    project_etag: str
    state: CoreWorkspaceUploadFinalizeStateV1 = CoreWorkspaceUploadFinalizeStateV1.PRE_FINALIZE
    outcome: core_v1.WorkspaceUploadFinalizeResponseV1 | None = None
    outcome_sha256: str | None = None

    def __post_init__(self) -> None:
        self.verify()

    def verify(self) -> None:
        if _model_digest(self.request) != self.request_sha256:
            raise ValueError("workspace finalize request digest does not match canonical request")
        if self.upload.status is not core_v1.WorkspaceUploadStatus.OPEN or (
            self.upload.accepted_offset != self.upload.archive.byte_size
            or self.request.content_sha256 != self.upload.archive.content_sha256
            or self.upload_etag != self.upload.etag
            or self.project_etag != self.upload.project_etag
        ):
            raise ValueError("workspace finalize authority is not an exact open upload request")
        has_outcome = self.outcome is not None and self.outcome_sha256 is not None
        if (self.state is CoreWorkspaceUploadFinalizeStateV1.APPLIED) != has_outcome:
            raise ValueError("only applied workspace finalize authority has an outcome")
        if self.outcome is None:
            if self.outcome_sha256 is not None:
                raise ValueError("workspace finalize outcome binding is incomplete")
            return
        if _model_digest(self.outcome) != self.outcome_sha256:
            raise ValueError("workspace finalize outcome digest does not match canonical outcome")
        finalized = self.outcome.upload
        if (
            self.outcome.project_id != self.upload.project_id
            or finalized.id != self.upload.id
            or finalized.project_id != self.upload.project_id
            or finalized.status is not core_v1.WorkspaceUploadStatus.FINALIZED
            or finalized.accepted_offset != self.upload.accepted_offset
            or finalized.project_snapshot != self.upload.project_snapshot
            or finalized.project_etag != self.upload.project_etag
            or finalized.archive != self.upload.archive
            or finalized.base_workspace_snapshot != self.upload.base_workspace_snapshot
            or finalized.created_at != self.upload.created_at
            or finalized.publication != self.outcome.publication
            or finalized.etag == self.upload.etag
            or _utc_timestamp(finalized.updated_at) < _utc_timestamp(self.upload.updated_at)
            or self.outcome.project.workspace_publication != self.outcome.publication
            or self.outcome.project.current_project_snapshot == self.upload.project_snapshot
            or self.outcome.project.etag == self.upload.project_etag
        ):
            raise ValueError("workspace finalize authority is not an exact upload transition")


@dataclass(frozen=True, slots=True)
class CoreProjectCreateOperationV1:
    local_project_id: str
    profile_id: str
    core_host_identity: str
    request_sha256: str
    project_create: core_v1.ProjectCreateV1
    idempotency_key: str
    state: CoreProjectCreateStateV1 = CoreProjectCreateStateV1.PRE_CREATE
    core_project_id: str | None = None
    project_immutable_authority: CoreProjectPatchImmutableAuthorityV1 | None = None
    workspace_upload_id: str | None = None
    workspace_upload_project_snapshot: core_v1.ImmutableSnapshotRefV1 | None = None
    workspace_upload_abort: CoreWorkspaceUploadAbortOperationV1 | None = None
    workspace_upload_finalize: CoreWorkspaceUploadFinalizeAuthorityV1 | None = None

    def __post_init__(self) -> None:
        if _model_digest(self.project_create) != self.request_sha256:
            raise ValueError("project create request digest does not match canonical request")
        if (self.state is CoreProjectCreateStateV1.BOUND) != (self.core_project_id is not None):
            raise ValueError("only a bound create operation has a Core project ID")
        if (self.core_project_id is None) != (self.project_immutable_authority is None):
            raise ValueError("a bound create operation must retain immutable project authority")
        if self.project_immutable_authority is not None and (
            self.project_immutable_authority.project_id != self.core_project_id
            or self.project_immutable_authority.project_create != self.project_create
        ):
            raise ValueError("create immutable authority must match the bound Core project")
        if (self.workspace_upload_id is None) != (self.workspace_upload_project_snapshot is None):
            raise ValueError("workspace upload ID and project snapshot must be paired")
        if self.workspace_upload_abort is not None and (
            self.workspace_upload_id != self.workspace_upload_abort.upload.id
            or self.workspace_upload_project_snapshot
            != self.workspace_upload_abort.upload.project_snapshot
            or self.core_project_id != self.workspace_upload_abort.upload.project_id
        ):
            raise ValueError("workspace abort authority must match the bound upload")
        if self.workspace_upload_finalize is not None and (
            self.workspace_upload_id != self.workspace_upload_finalize.upload.id
            or self.workspace_upload_project_snapshot
            != self.workspace_upload_finalize.upload.project_snapshot
            or self.core_project_id != self.workspace_upload_finalize.upload.project_id
            or self.workspace_upload_finalize.idempotency_key
            != _derived_key(
                _derived_key(
                    self.idempotency_key,
                    "workspace-upload-"
                    f"{_model_digest(self.workspace_upload_finalize.upload.project_snapshot)}",
                ),
                "finalize",
            )
        ):
            raise ValueError("workspace finalize authority must match the bound upload")
        if self.workspace_upload_finalize is not None:
            self.workspace_upload_finalize.verify()
        if self.workspace_upload_abort is not None and self.workspace_upload_finalize is not None:
            raise ValueError("one workspace upload cannot have abort and finalize authority")


class CoreProjectPatchStateV1(StrEnum):
    PRE_PATCH = "pre_patch"
    UNKNOWN = "unknown"
    APPLIED = "applied"


@dataclass(frozen=True, slots=True)
class CoreProjectPatchImmutableAuthorityV1:
    project_id: str
    project_create: core_v1.ProjectCreateV1
    task_snapshot: core_v1.ImmutableSnapshotRefV1
    created_at: str


@dataclass(frozen=True, slots=True)
class CoreProjectPatchMutableAuthorityV1:
    status: core_v1.ProjectStatus
    project_snapshot: core_v1.ImmutableSnapshotRefV1
    workspace_snapshot: core_v1.ImmutableSnapshotRefV1 | None
    workspace_publication: core_v1.WorkspacePublicationV1 | None
    active_revision: core_v1.RevisionRefV1 | None
    registry_digest: str | None
    model_preparation: core_v1.ModelPreparationV1
    updated_at: str
    etag: str


@dataclass(frozen=True, slots=True)
class CoreProjectPatchOperationV1:
    local_project_id: str
    profile_id: str
    core_host_identity: str
    core_project_id: str
    old_request_sha256: str
    old_project_create: core_v1.ProjectCreateV1
    new_request_sha256: str
    new_project_create: core_v1.ProjectCreateV1
    patch_request_sha256: str
    patch: core_v1.ProjectPatchV1
    idempotency_key: str
    base_project: core_v1.ProjectV1
    state: CoreProjectPatchStateV1 = CoreProjectPatchStateV1.PRE_PATCH
    outcome: core_v1.ProjectV1 | None = None
    outcome_immutable: CoreProjectPatchImmutableAuthorityV1 | None = None
    outcome_mutable: CoreProjectPatchMutableAuthorityV1 | None = None

    def __post_init__(self) -> None:
        if _model_digest(self.old_project_create) != self.old_request_sha256:
            raise ValueError("old project intent digest does not match canonical request")
        if _model_digest(self.new_project_create) != self.new_request_sha256:
            raise ValueError("new project intent digest does not match canonical request")
        if _model_digest(self.patch) != self.patch_request_sha256:
            raise ValueError("project patch digest does not match canonical request")
        expected_patch = core_v1.ProjectPatchV1(
            name=self.new_project_create.name,
            description=self.new_project_create.description,
            spec=self.new_project_create.spec,
            task=self.new_project_create.task,
            workspace=self.new_project_create.workspace,
        )
        if self.patch != expected_patch:
            raise ValueError("project patch is not the canonical new Local intent")
        if self.old_request_sha256 == self.new_request_sha256:
            raise ValueError("a project patch must change canonical Local intent")
        if self.base_project.id != self.core_project_id:
            raise ValueError("project patch base authority belongs to another project")
        if not _project_identity_matches(self.base_project, self.old_project_create):
            raise ValueError("project patch base authority does not match old Local intent")
        outcome_fields = (self.outcome, self.outcome_immutable, self.outcome_mutable)
        has_complete_outcome = all(value is not None for value in outcome_fields)
        has_any_outcome = any(value is not None for value in outcome_fields)
        if (
            self.state is CoreProjectPatchStateV1.APPLIED
        ) != has_complete_outcome or has_any_outcome != has_complete_outcome:
            raise ValueError("only an applied project patch has complete durable authority")
        if self.outcome is not None and self.outcome.id != self.core_project_id:
            raise ValueError("project patch outcome belongs to another project")
        if self.outcome is not None and self.outcome.created_at != self.base_project.created_at:
            raise ValueError("project patch outcome changed immutable project creation time")
        if self.outcome is not None and not _project_identity_matches(
            self.outcome, self.new_project_create
        ):
            raise ValueError("project patch outcome does not match new Local intent")
        if self.outcome is not None and (
            self.outcome_immutable != _patch_immutable_authority(self.outcome)
            or self.outcome_mutable != _patch_mutable_authority(self.outcome)
        ):
            raise ValueError("project patch outcome authority boundary is incomplete")


@dataclass(frozen=True, slots=True)
class CoreProjectMappingV1:
    local_project_id: str
    profile_id: str
    core_host_identity: str
    core_project_id: str
    request_sha256: str
    project_create: core_v1.ProjectCreateV1
    project_snapshot: core_v1.ImmutableSnapshotRefV1
    task_snapshot: core_v1.ImmutableSnapshotRefV1
    workspace_snapshot: core_v1.ImmutableSnapshotRefV1
    registry_digest: str
    project_etag: str
    active_revision: core_v1.RevisionRefV1
    project_updated_at: str
    immutable_authority: CoreProjectPatchImmutableAuthorityV1
    mutable_authority: CoreProjectPatchMutableAuthorityV1
    mapping_generation: int
    predecessor_request_sha256: str | None

    def __post_init__(self) -> None:
        if self.mapping_generation < 1:
            raise ValueError("mapping generation must be positive")
        if (self.mapping_generation == 1) != (self.predecessor_request_sha256 is None):
            raise ValueError("only the first mapping has no predecessor")


@dataclass(frozen=True, slots=True, eq=False)
class _CoreActivationAuthorityV1:
    """Process-local capability proving one bridge-produced activation."""


class DesktopCoreBridgePersistence(Protocol):
    """Durable callback boundary for create, patch, upload, and mapping state.

    A pre-create reservation may be replaced by a later Local action. Unknown
    outcomes require the exact request and key. A bound operation preserves its
    original canonical request and Core project identity while later Local
    intent converges through patching. Create updates compare the complete prior
    operation, including upload/abort authority.

    One patch operation may be pending per Local project. Reservation must not
    replace a different pending operation. The pre-patch -> unknown -> applied
    transitions are exact full-row CAS operations; applied stores the complete
    validated Core outcome. Mapping commit compares the complete previous
    mapping, appends ordered audit history, and atomically removes only the
    supplied matching applied patch. A failed transaction leaves both the old
    mapping and pending patch unchanged for recovery.
    """

    def load_mapping(self, local_project_id: str) -> CoreProjectMappingV1 | None: ...

    def load_create(self, local_project_id: str) -> CoreProjectCreateOperationV1 | None: ...

    def load_patch(self, local_project_id: str) -> CoreProjectPatchOperationV1 | None: ...

    def reserve_create(
        self, operation: CoreProjectCreateOperationV1
    ) -> CoreProjectCreateOperationV1: ...

    def mark_create_unknown(
        self, operation: CoreProjectCreateOperationV1
    ) -> CoreProjectCreateOperationV1: ...

    def bind_created_project(
        self,
        operation: CoreProjectCreateOperationV1,
        core_project_id: str,
        *,
        immutable_authority: CoreProjectPatchImmutableAuthorityV1,
    ) -> CoreProjectCreateOperationV1: ...

    def update_create(
        self,
        operation: CoreProjectCreateOperationV1,
        *,
        expected_previous: CoreProjectCreateOperationV1,
    ) -> CoreProjectCreateOperationV1: ...

    def reserve_patch(
        self, operation: CoreProjectPatchOperationV1
    ) -> CoreProjectPatchOperationV1: ...

    def mark_patch_unknown(
        self, operation: CoreProjectPatchOperationV1
    ) -> CoreProjectPatchOperationV1: ...

    def record_patch_applied(
        self,
        operation: CoreProjectPatchOperationV1,
        outcome: core_v1.ProjectV1,
        *,
        outcome_immutable: CoreProjectPatchImmutableAuthorityV1,
        outcome_mutable: CoreProjectPatchMutableAuthorityV1,
    ) -> CoreProjectPatchOperationV1: ...

    def commit_mapping(
        self,
        operation: CoreProjectCreateOperationV1,
        mapping: CoreProjectMappingV1,
        *,
        expected_previous: CoreProjectMappingV1 | None,
        completed_patch: CoreProjectPatchOperationV1 | None,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class CoreActivationV1:
    generation: int
    local_project_id: str
    profile_id: str
    local_project_etag: str
    local_project_intent_sha256: str
    core_project: core_v1.ProjectV1
    capabilities: core_v1.CapabilitiesResponseV1
    revision_head: core_v1.RevisionHeadV1
    validation: core_v1.ProjectValidationResponseV1
    _authority: _CoreActivationAuthorityV1 = field(repr=False)


@dataclass(slots=True, repr=False)
class DesktopCoreActiveSessionV1:
    token: _GenerationToken
    generation: int
    local_project_id: str
    profile_id: str
    local_project_etag: str
    local_project_intent_sha256: str
    mapping: CoreProjectMappingV1
    attachment: CoreHostAttachmentV1
    tunnel: CoreTunnelHandleV1
    client: CoreControlClientV1
    project: core_v1.ProjectV1
    capabilities: core_v1.CapabilitiesResponseV1
    revision_head: core_v1.RevisionHeadV1
    activation: CoreActivationV1
    committed_local_project: local_v1.ProjectV1 | None = None


class DesktopCoreBridgeErrorV1(RuntimeError):
    def __init__(self, error: core_v1.ApiErrorV1) -> None:
        super().__init__(error.message)
        self.error = error


def map_project_create_v1(project: local_v1.ProjectV1) -> core_v1.ProjectCreateV1:
    """Map saved Local project intent into the one frozen Core create contract."""

    try:
        return _map_project_create_v1(project)
    except ValidationError:
        raise _bridge_error(
            "invalid_local_project",
            "The saved project cannot be represented by the Core project contract.",
            status=422,
        ) from None


def _map_project_create_v1(project: local_v1.ProjectV1) -> core_v1.ProjectCreateV1:
    execution = project.execution
    if execution.mode == "codex_subscription_transcript":
        execution_mode = core_v1.ExecutionMode.CODEX_SUBSCRIPTION_TRANSCRIPT
        model_ref = execution.codex_model
    else:
        execution_mode = core_v1.ExecutionMode.SELF_DEPLOYED
        model_ref = execution.hf_model
    if model_ref is None:
        raise _bridge_error(
            "invalid_local_project",
            "The saved project does not declare the model required by its execution mode.",
            status=422,
        )

    spec = core_v1.ProjectSpecV1(
        execution_mode=execution_mode,
        capture_mode=core_v1.CaptureMode.TRANSCRIPT,
        harness_id="codex",
        agent_model_ref=model_ref,
        evolution=core_v1.EvolutionConfigV1.model_validate(
            project.evolution.model_dump(mode="json"), strict=True
        ),
    )
    task = core_v1.TaskSpecV1(
        title=project.task.title,
        objective=project.task.objective,
    )
    if project.source.kind == "scratch":
        workspace: core_v1.ProjectWorkspaceSpecV1 = core_v1.ScratchWorkspaceSpecV1(
            kind=core_v1.WorkspaceSourceKind.SCRATCH,
            display_name=project.source.display_name,
        )
    else:
        ref = project.source.import_ref
        if ref is None:
            raise _bridge_error(
                "invalid_local_project",
                "The saved imported project has no adopted workspace reference.",
                status=422,
            )
        workspace = core_v1.ImportedWorkspaceSpecV1(
            kind=core_v1.WorkspaceSourceKind.NATIVE_FOLDER_SNAPSHOT,
            display_name=project.source.display_name,
            archive=core_v1.WorkspaceArchiveDeclarationV1(
                content_sha256=ref.content_sha256,
                byte_size=ref.byte_size,
                format=core_v1.WorkspaceArchiveFormat.OPENEVO_DETERMINISTIC_TAR_V1,
                entry_count=ref.entry_count,
                extracted_byte_size=ref.extracted_byte_size,
                policy=_workspace_archive_policy(),
            ),
        )
    return core_v1.ProjectCreateV1(
        name=project.name,
        spec=spec,
        task=task,
        workspace=workspace,
    )


class DesktopCoreBridgeV1:
    """Own one generation-linearized active project tunnel and strict Core client."""

    def __init__(
        self,
        *,
        host_service: CoreHostService,
        tunnel_factory: CoreTunnelFactory,
        persistence: DesktopCoreBridgePersistence,
        archive_source: WorkspaceArchiveSource,
        transport_factory: Callable[[], httpx.BaseTransport] | None = None,
        timeout: float = DEFAULT_BRIDGE_TIMEOUT_SECONDS,
        activation_timeout: float | None = None,
    ) -> None:
        if not 0 < timeout <= MAX_BRIDGE_TIMEOUT_SECONDS:
            raise ValueError("bridge timeout must be finite and at most 300 seconds")
        resolved_activation_timeout = (
            timeout if activation_timeout is None else activation_timeout
        )
        if not 0 < resolved_activation_timeout <= MAX_ACTIVATION_TIMEOUT_SECONDS:
            raise ValueError("bridge activation timeout must be finite and at most 900 seconds")
        self._host_service = host_service
        self._tunnel_factory = tunnel_factory
        self._persistence = persistence
        self._archive_source = archive_source
        self._transport_factory = transport_factory
        self._timeout = float(timeout)
        self._activation_timeout = float(resolved_activation_timeout)
        self._lock = threading.RLock()
        self._transition_lock = threading.Lock()
        self._generation = 0
        self._closed = False
        self._close_requested = False
        self._active: DesktopCoreActiveSessionV1 | None = None
        self._candidate: _GenerationToken | None = None

    def close(self) -> None:
        deadline = time.monotonic() + self._timeout
        with self._lock:
            candidate = self._candidate
            if candidate is not None:
                candidate.cancelled = True
                if candidate.cancel_event is not None:
                    candidate.cancel_event.set()
        if candidate is not None:
            self._request_token_interrupt(candidate)
        self._acquire_transition(deadline)
        try:
            with self._lock:
                if self._closed:
                    return
                self._close_requested = True
                token = self._current_token_locked()
            if token is not None:
                self._retire_token(token, deadline=deadline)
            with self._lock:
                self._closed = True
                self._generation += 1
        finally:
            self._transition_lock.release()

    def activate_project(
        self,
        project: local_v1.ProjectV1,
        *,
        idempotency_key: str,
        activation_id: str | None = None,
        cancel_event: threading.Event | None = None,
    ) -> CoreActivationV1:
        if (activation_id is None) != (cancel_event is None):
            raise ValueError("activation cancellation identity and event must appear together")
        deadline = time.monotonic() + self._activation_timeout
        token = self._begin_activation(
            deadline,
            activation_id=activation_id,
            cancel_event=cancel_event,
        )
        generation = token.generation
        try:
            def ensure_core() -> CoreHostAttachmentV1:
                if token.cancel_event is None:
                    return self._host_service.ensure_core(
                        project.profile_id,
                        deadline=deadline,
                    )
                return self._host_service.ensure_core(
                    project.profile_id,
                    deadline=deadline,
                    cancel_event=token.cancel_event,
                )

            attachment = self._adapter_external(
                token,
                deadline,
                ensure_core,
                label="Core host attach",
            )
            if attachment.profile_id != project.profile_id:
                raise _bridge_error(
                    "core_host_identity_mismatch",
                    "The attached Core host belongs to another remote profile.",
                )
            session_id = secrets.token_urlsafe(24)
            tunnel = self._adapter_external(
                token,
                deadline,
                lambda: self._tunnel_factory.open_tunnel(
                    profile_id=attachment.profile_id,
                    remote_port=attachment.remote_port,
                    session_id=session_id,
                    deadline=deadline,
                ),
                label="Core tunnel open",
                adopt=token.add_tunnel,
            )
            create_request = map_project_create_v1(project)
            request_sha256 = _model_digest(create_request)
            mapping = self._adapter_external(
                token,
                deadline,
                lambda: self._persistence.load_mapping(project.project_id),
                label="project mapping read",
            )
            operation: CoreProjectCreateOperationV1
            if mapping is None:
                proposed_operation = CoreProjectCreateOperationV1(
                    local_project_id=project.project_id,
                    profile_id=project.profile_id,
                    core_host_identity=attachment.bearer_identity,
                    request_sha256=request_sha256,
                    project_create=create_request,
                    idempotency_key=idempotency_key,
                )
                operation = self._adapter_external(
                    token,
                    deadline,
                    lambda: self._persistence.reserve_create(proposed_operation),
                    label="project create reservation",
                )
                _ensure_create_operation(
                    operation,
                    project,
                    request_sha256,
                    idempotency_key=idempotency_key,
                    core_host_identity=attachment.bearer_identity,
                )
                connection, operation = self._bootstrap_connection(
                    token=token,
                    request=create_request,
                    operation=operation,
                    attachment=attachment,
                    tunnel=tunnel,
                    deadline=deadline,
                )
            else:
                _ensure_mapping_authority(
                    mapping,
                    project,
                    core_host_identity=attachment.bearer_identity,
                )
                loaded_operation = self._adapter_external(
                    token,
                    deadline,
                    lambda: self._persistence.load_create(project.project_id),
                    label="project create operation read",
                )
                operation = _ensure_bound_operation(loaded_operation, mapping)
                connection = CoreTunnelConnectionV1(
                    endpoint=tunnel.endpoint,
                    bearer_token=attachment.bearer_token,
                    project_id=mapping.core_project_id,
                    session_id=tunnel.session_id,
                )

            client = self._adapter_external(
                token,
                deadline,
                lambda: self._new_client(connection, deadline),
                label="Core client construction",
                adopt=token.add_client,
            )
            self._core_external(token, deadline, client.version)
            capabilities = self._core_external(
                token,
                deadline,
                lambda: client.capabilities(create_request.spec.execution_mode),
            )
            core_project = self._core_external(token, deadline, client.get_project)
            pending_patch = self._adapter_external(
                token,
                deadline,
                lambda: self._persistence.load_patch(project.project_id),
                label="project patch operation read",
            )
            pending_finalize = operation.workspace_upload_finalize
            if (
                pending_finalize is not None
                and pending_finalize.state is not CoreWorkspaceUploadFinalizeStateV1.APPLIED
            ):
                core_project, operation = self._resume_workspace_finalize(
                    token=token,
                    deadline=deadline,
                    client=client,
                    operation=operation,
                    expected_project=core_project,
                )
            initial_revision_authority: core_v1.RevisionRefV1 | None = None
            if mapping is None:
                authority_anchor = (
                    pending_patch.base_project if pending_patch is not None else core_project
                )
                initial_revision_authority = _ensure_initial_publication_authority(
                    operation,
                    authority_anchor,
                    pending_patch=pending_patch,
                )
                if (
                    pending_patch is not None
                    and pending_patch.state is not CoreProjectPatchStateV1.APPLIED
                ):
                    _ensure_revision_authority_successor(
                        pending_patch.base_project.active_revision,
                        core_project.active_revision,
                        project_id=operation.core_project_id,
                        label="durable pending project patch base",
                    )
            completed_patch: CoreProjectPatchOperationV1 | None = None
            revision_authorities: tuple[core_v1.RevisionRefV1 | None, ...] = ()
            recovered_requested_patch = False
            if pending_patch is not None:
                _ensure_patch_operation_authority(
                    pending_patch,
                    project,
                    operation=operation,
                    mapping=mapping,
                    core_host_identity=attachment.bearer_identity,
                )
                core_project, pending_patch, revision_authorities = self._resume_project_patch(
                    token=token,
                    deadline=deadline,
                    client=client,
                    current=core_project,
                    operation=pending_patch,
                    workspace_authority=operation,
                )
                completed_patch = pending_patch
                recovered_requested_patch = pending_patch.new_request_sha256 == request_sha256
                if pending_patch.new_request_sha256 != request_sha256:
                    self._ensure_project_ready(core_project, capabilities)
                    recovered_head = self._core_external(token, deadline, client.revision_head)
                    if recovered_head.active_revision != core_project.active_revision:
                        raise _bridge_error(
                            "core_project_revision_mismatch",
                            "Core project and revision head disagree.",
                        )
                    recovered_validation = self._validate_current(
                        token,
                        deadline,
                        client,
                        core_project,
                        capabilities,
                        idempotency_key=_derived_key(
                            pending_patch.idempotency_key, "recover-validate"
                        ),
                    )
                    if not recovered_validation.valid:
                        raise _bridge_error(
                            "core_project_validation_failed",
                            "Core rejected the recovered project configuration.",
                            status=422,
                        )
                    recovered_mapping = _mapping_from_request(
                        local_project_id=project.project_id,
                        profile_id=project.profile_id,
                        request=pending_patch.new_project_create,
                        request_sha256=pending_patch.new_request_sha256,
                        project=core_project,
                        capabilities=capabilities,
                        core_host_identity=attachment.bearer_identity,
                        previous_mapping=mapping,
                        revision_authorities=revision_authorities,
                        initial_revision_authority=initial_revision_authority,
                    )
                    self._adapter_external(
                        token,
                        deadline,
                        lambda: self._persistence.commit_mapping(
                            operation,
                            recovered_mapping,
                            expected_previous=mapping,
                            completed_patch=pending_patch,
                        ),
                        label="recovered project patch mapping commit",
                    )
                    mapping = recovered_mapping
                    completed_patch = None
            if recovered_requested_patch:
                pass
            elif mapping is not None:
                core_project, new_patch = self._reconcile_mapped_project(
                    token=token,
                    deadline=deadline,
                    client=client,
                    mapping=mapping,
                    current=core_project,
                    requested=create_request,
                    request_sha256=request_sha256,
                    core_host_identity=attachment.bearer_identity,
                )
                completed_patch = new_patch or completed_patch
            elif operation.request_sha256 != request_sha256:
                core_project, new_patch = self._reconcile_bound_project(
                    token=token,
                    deadline=deadline,
                    client=client,
                    operation=operation,
                    current=core_project,
                    requested=create_request,
                    request_sha256=request_sha256,
                    core_host_identity=attachment.bearer_identity,
                )
                completed_patch = new_patch or completed_patch
            else:
                _ensure_project_identity(core_project, create_request)
            pending_finalize = operation.workspace_upload_finalize
            if isinstance(create_request.workspace, core_v1.ImportedWorkspaceSpecV1) and (
                core_project.workspace_publication is None
                or (
                    pending_finalize is not None
                    and pending_finalize.state is not CoreWorkspaceUploadFinalizeStateV1.APPLIED
                )
            ):
                core_project, operation = self._publish_imported_workspace(
                    token=token,
                    client=client,
                    local_project=project,
                    core_project=core_project,
                    operation=operation,
                    deadline=deadline,
                )
            if mapping is None and completed_patch is None:
                initial_revision_authority = _ensure_initial_publication_authority(
                    operation,
                    core_project,
                    pending_patch=None,
                )
            self._ensure_project_ready(core_project, capabilities)
            if completed_patch is not None:
                revision_authorities = _ensure_persisted_patch_outcome(
                    completed_patch,
                    core_project,
                    finalize_authority=operation.workspace_upload_finalize,
                )
            revision_head = self._core_external(token, deadline, client.revision_head)
            if revision_head.active_revision != core_project.active_revision:
                raise _bridge_error(
                    "core_project_revision_mismatch",
                    "Core project and revision head disagree.",
                )
            validation = self._validate_current(
                token,
                deadline,
                client,
                core_project,
                capabilities,
                idempotency_key=_derived_key(idempotency_key, "validate"),
            )
            if not validation.valid:
                raise _bridge_error(
                    "core_project_validation_failed",
                    "Core rejected the saved project configuration.",
                    status=422,
                )
            completed_mapping = _mapping_from_project(
                project,
                request_sha256,
                core_project,
                capabilities,
                core_host_identity=attachment.bearer_identity,
                previous_mapping=mapping,
                revision_authorities=revision_authorities,
                initial_revision_authority=initial_revision_authority,
            )
            if mapping != completed_mapping:
                self._adapter_external(
                    token,
                    deadline,
                    lambda: self._persistence.commit_mapping(
                        operation,
                        completed_mapping,
                        expected_previous=mapping,
                        completed_patch=completed_patch,
                    ),
                    label="project mapping commit",
                )
            activation = CoreActivationV1(
                generation=generation,
                local_project_id=project.project_id,
                profile_id=project.profile_id,
                local_project_etag=project.etag,
                local_project_intent_sha256=request_sha256,
                core_project=core_project,
                capabilities=capabilities,
                revision_head=revision_head,
                validation=validation,
                _authority=_CoreActivationAuthorityV1(),
            )
            candidate = DesktopCoreActiveSessionV1(
                token=token,
                generation=generation,
                local_project_id=project.project_id,
                profile_id=project.profile_id,
                local_project_etag=project.etag,
                local_project_intent_sha256=request_sha256,
                mapping=completed_mapping,
                attachment=attachment,
                tunnel=tunnel,
                client=client,
                project=core_project,
                capabilities=capabilities,
                revision_head=revision_head,
                activation=activation,
            )
            self._publish_activation(candidate)
            return activation
        except BaseException as exc:
            try:
                self._cleanup_failed_candidate(token, deadline=deadline)
            except BaseException as cleanup_exc:
                raise cleanup_exc from exc
            if isinstance(exc, CoreClientErrorV1):
                raise _bridge_client_error(exc) from None
            raise

    def cancel_activation(self, activation_id: str) -> bool:
        """Interrupt exactly one in-flight activation without waiting on its adapter."""

        if not activation_id:
            raise ValueError("activation identity must not be empty")
        with self._lock:
            token = self._candidate
            if token is None or token.activation_id != activation_id:
                return False
            token.cancelled = True
            if token.cancel_event is not None:
                token.cancel_event.set()
        self._request_token_interrupt(token)
        return True

    def commit_local_activation(
        self,
        project: local_v1.ProjectV1,
        *,
        activation: CoreActivationV1,
    ) -> None:
        """Bind the durable post-activation Desktop projection to the live session."""

        deadline = time.monotonic() + self._timeout
        self._acquire_transition(deadline)
        try:
            session, generation = self._session()
            with self._lock:
                self._ensure_generation(session, generation)
                self._ensure_activation_acknowledgement(session, generation, activation)
                self._ensure_local_activation_projection(session, project)
                if project.etag == activation.local_project_etag:
                    raise _bridge_error(
                        "local_activation_source_etag_mismatch",
                        "The durable Desktop activation did not advance its Local ETag.",
                        status=409,
                    )
                if session.committed_local_project is None:
                    if session.local_project_etag != activation.local_project_etag:
                        raise _bridge_error(
                            "local_activation_source_etag_mismatch",
                            "The activation no longer owns the Local project ETag transition.",
                            status=409,
                        )
                    session.local_project_etag = project.etag
                    session.committed_local_project = project
                elif (
                    session.local_project_etag != project.etag
                    or session.committed_local_project != project
                ):
                    raise _bridge_error(
                        "local_activation_already_committed",
                        "The activation was already acknowledged with a different result.",
                        status=409,
                    )
        finally:
            self._transition_lock.release()

    def deactivate_project(self, local_project_id: str) -> None:
        """Retire one published project session without closing the bridge."""

        deadline = time.monotonic() + self._timeout
        self._acquire_transition(deadline)
        try:
            with self._lock:
                if self._closed or self._close_requested:
                    raise _bridge_error(
                        "desktop_core_bridge_closed",
                        "The Desktop Core bridge is closed.",
                    )
                if self._candidate is not None:
                    raise _bridge_error(
                        "active_project_transition_in_progress",
                        "The active project transition is still in progress.",
                        status=409,
                        retryable=True,
                    )
                session = self._active
                if session is None:
                    return
                if session.local_project_id != local_project_id:
                    raise _bridge_error(
                        "active_project_mismatch",
                        "The requested project does not own the active Core session.",
                        status=409,
                    )
                token = session.token
            self._retire_token(token, deadline=deadline)
            with self._lock:
                self._generation += 1
        finally:
            self._transition_lock.release()

    def capabilities(self, project: local_v1.ProjectV1) -> core_v1.CapabilitiesResponseV1:
        def call(
            session: DesktopCoreActiveSessionV1,
            deadline: float,
        ) -> core_v1.CapabilitiesResponseV1:
            _project, capabilities = self._refresh_authority(session, deadline)
            return capabilities

        return self._invoke_project(project, call)

    def validate_project(
        self, project: local_v1.ProjectV1, *, idempotency_key: str
    ) -> core_v1.ProjectValidationResponseV1:
        def call(
            session: DesktopCoreActiveSessionV1,
            deadline: float,
        ) -> core_v1.ProjectValidationResponseV1:
            project, capabilities = self._refresh_authority(session, deadline)
            return self._validate_current(
                session.token,
                deadline,
                session.client,
                project,
                capabilities,
                idempotency_key=idempotency_key,
            )

        return self._invoke_project(project, call)

    def create_run(
        self,
        project: local_v1.ProjectV1,
        *,
        idempotency_key: str,
    ) -> core_v1.RunV1:
        def call(session: DesktopCoreActiveSessionV1, deadline: float) -> core_v1.RunV1:
            project, capabilities = self._refresh_authority(session, deadline)
            head = self._core_external(session.token, deadline, session.client.revision_head)
            if head.active_revision != project.active_revision:
                raise _bridge_error(
                    "core_project_revision_mismatch",
                    "Core project and revision head disagree.",
                )
            validation = self._validate_current(
                session.token,
                deadline,
                session.client,
                project,
                capabilities,
                idempotency_key=_derived_key(idempotency_key, "validate"),
            )
            if not validation.valid:
                raise _bridge_error(
                    "core_project_validation_failed",
                    "Core rejected the saved project configuration.",
                    status=422,
                )
            required_revision = _select_required_revision(head)
            workspace_snapshot = project.current_workspace_snapshot
            active_revision = project.active_revision
            if workspace_snapshot is None or active_revision is None:
                raise _bridge_error(
                    "core_project_not_ready",
                    "Core has not prepared the project for run admission.",
                )
            request = core_v1.RunCreateV1(
                project_id=project.id,
                project_snapshot=project.current_project_snapshot,
                task_snapshot=project.current_task_snapshot,
                workspace_snapshot=workspace_snapshot,
                expected_registry_digest=capabilities.registry_digest,
                required_revision=required_revision,
            )
            return self._core_external(
                session.token,
                deadline,
                lambda: session.client.create_run(
                    request,
                    idempotency_key=idempotency_key,
                ),
            )

        return self._invoke_project(project, call)

    def list_runs(self, project: local_v1.ProjectV1, **kwargs: Any) -> core_v1.RunPageV1:
        return self._invoke_project(
            project, lambda session, _deadline: session.client.list_runs(**kwargs)
        )

    def get_run(self, project: local_v1.ProjectV1, run_id: str) -> core_v1.RunV1:
        return self._invoke_project(
            project,
            lambda session, _deadline: session.client.get_run(
                run_id, project_id=session.project.id
            ),
        )

    def delete_run(self, project: local_v1.ProjectV1, run_id: str, *, if_match: str) -> None:
        return self._invoke_project(
            project,
            lambda session, _deadline: session.client.delete_run(
                run_id,
                project_id=session.project.id,
                if_match=if_match,
                idempotency_key=_derived_key(run_id, f"delete-{if_match}"),
            ),
        )

    def cancel_run(
        self,
        project: local_v1.ProjectV1,
        run_id: str,
        *,
        if_match: str,
        idempotency_key: str,
    ) -> core_v1.RunV1:
        def call(session: DesktopCoreActiveSessionV1, deadline: float) -> core_v1.RunV1:
            self._core_external(
                session.token,
                deadline,
                lambda: session.client.get_run(run_id, project_id=session.project.id),
            )
            return self._core_external(
                session.token,
                deadline,
                lambda: session.client.cancel_run(
                    run_id,
                    core_v1.RunCancelRequestV1(reason=core_v1.RunCancelReason.USER_REQUESTED),
                    project_id=session.project.id,
                    if_match=if_match,
                    idempotency_key=idempotency_key,
                ),
            )

        return self._invoke_project(project, call)

    def retry_run(
        self,
        project: local_v1.ProjectV1,
        run_id: str,
        request: local_v1.RunRetryV1,
        *,
        if_match: str,
        idempotency_key: str,
    ) -> core_v1.RunV1:
        session, _generation = self._session()
        deadline = time.monotonic() + self._timeout

        def call(session: DesktopCoreActiveSessionV1, deadline: float) -> core_v1.RunV1:
            self._ensure_local_project_binding(session, project)
            return session.client.retry_run(
                run_id,
                core_v1.RunRetryRequestV1(terminal_attempt_id=request.terminal_attempt_id),
                project_id=session.project.id,
                if_match=if_match,
                idempotency_key=idempotency_key,
            )

        return self._retry_core_external(
            session.token,
            deadline,
            lambda: call(session, deadline),
        )

    def run_timeline(
        self, project: local_v1.ProjectV1, run_id: str, **kwargs: Any
    ) -> core_v1.RunTimelinePageV1:
        return self._invoke_project(
            project,
            lambda session, _deadline: session.client.run_timeline(
                run_id, project_id=session.project.id, **kwargs
            ),
        )

    def run_logs(
        self, project: local_v1.ProjectV1, run_id: str, **kwargs: Any
    ) -> core_v1.LogPageV1:
        return self._invoke_project(
            project,
            lambda session, _deadline: session.client.run_logs(
                run_id, project_id=session.project.id, **kwargs
            ),
        )

    def run_context(self, project: local_v1.ProjectV1, run_id: str) -> core_v1.RunContextV1:
        return self._invoke_project(
            project,
            lambda session, _deadline: session.client.run_context(
                run_id, project_id=session.project.id
            ),
        )

    def run_artifacts(
        self, project: local_v1.ProjectV1, run_id: str, **kwargs: Any
    ) -> core_v1.ArtifactPageV1:
        return self._invoke_project(
            project,
            lambda session, _deadline: session.client.run_artifacts(
                run_id, project_id=session.project.id, **kwargs
            ),
        )

    def get_artifact(
        self, project: local_v1.ProjectV1, artifact_id: str
    ) -> core_v1.ArtifactSummaryV1:
        return self._invoke_project(
            project,
            lambda session, _deadline: session.client.get_artifact(
                artifact_id, project_id=session.project.id
            ),
        )

    def artifact_content(
        self, project: local_v1.ProjectV1, artifact_id: str
    ) -> core_v1.ArtifactContentV1:
        return self._invoke_project(
            project,
            lambda session, _deadline: session.client.artifact_content(
                artifact_id, project_id=session.project.id
            ),
        )

    def artifact_diff(
        self,
        project: local_v1.ProjectV1,
        artifact_id: str,
        *,
        previous_artifact_id: str | None = None,
    ) -> core_v1.ArtifactDiffV1:
        return self._invoke_project(
            project,
            lambda session, _deadline: session.client.artifact_diff(
                artifact_id,
                project_id=session.project.id,
                previous_artifact_id=previous_artifact_id,
            ),
        )

    def list_services(self, project: local_v1.ProjectV1, **kwargs: Any) -> core_v1.ServicePageV1:
        return self._invoke_project(
            project, lambda session, _deadline: session.client.list_services(**kwargs)
        )

    def get_service(
        self, project: local_v1.ProjectV1, service_id: str
    ) -> core_v1.ServiceSummaryV1:
        return self._invoke_project(
            project, lambda session, _deadline: session.client.get_service(service_id)
        )

    def restart_service(
        self,
        project: local_v1.ProjectV1,
        service_id: str,
        *,
        if_match: str,
        idempotency_key: str,
    ) -> core_v1.OperationV1:
        return self._invoke_project(
            project,
            lambda session, _deadline: session.client.restart_service(
                service_id,
                core_v1.ServiceRestartRequestV1(reason="Requested from OpenEvo Desktop."),
                if_match=if_match,
                idempotency_key=idempotency_key,
            ),
        )

    def service_logs(
        self, project: local_v1.ProjectV1, service_id: str, **kwargs: Any
    ) -> core_v1.LogPageV1:
        return self._invoke_project(
            project,
            lambda session, _deadline: session.client.service_logs(service_id, **kwargs),
        )

    def get_operation(self, project: local_v1.ProjectV1, operation_id: str) -> core_v1.OperationV1:
        return self._invoke_project(
            project, lambda session, _deadline: session.client.get_operation(operation_id)
        )

    def cancel_operation(
        self,
        project: local_v1.ProjectV1,
        operation_id: str,
        request: core_v1.OperationCancelRequestV1,
        *,
        if_match: str,
        idempotency_key: str,
    ) -> core_v1.OperationV1:
        return self._invoke_project(
            project,
            lambda session, _deadline: session.client.cancel_operation(
                operation_id,
                request,
                if_match=if_match,
                idempotency_key=idempotency_key,
            ),
        )

    def logs_by_ref(
        self, project: local_v1.ProjectV1, logs_ref: str, **kwargs: Any
    ) -> core_v1.ReferencedLogPageV1:
        return self._invoke_project(
            project,
            lambda session, _deadline: session.client.logs_by_ref(logs_ref, **kwargs),
        )

    def create_diagnostic(
        self,
        project: local_v1.ProjectV1,
        request: core_v1.DiagnosticsRequestV1,
        *,
        idempotency_key: str,
    ) -> core_v1.DiagnosticV1:
        return self._invoke_project(
            project,
            lambda session, _deadline: session.client.create_diagnostic(
                request, idempotency_key=idempotency_key
            ),
        )

    def get_diagnostic(
        self, project: local_v1.ProjectV1, diagnostic_id: str
    ) -> core_v1.DiagnosticV1:
        return self._invoke_project(
            project, lambda session, _deadline: session.client.get_diagnostic(diagnostic_id)
        )

    def delete_diagnostic(
        self,
        project: local_v1.ProjectV1,
        diagnostic_id: str,
        *,
        if_match: str,
        idempotency_key: str,
    ) -> None:
        return self._invoke_project(
            project,
            lambda session, _deadline: session.client.delete_diagnostic(
                diagnostic_id,
                if_match=if_match,
                idempotency_key=idempotency_key,
            ),
        )

    def cache_cleanup(
        self,
        project: local_v1.ProjectV1,
        request: core_v1.CacheCleanupRequestV1,
        *,
        idempotency_key: str,
    ) -> core_v1.OperationV1:
        return self._invoke_project(
            project,
            lambda session, _deadline: session.client.cache_cleanup(
                request, idempotency_key=idempotency_key
            ),
        )

    def events(
        self,
        project: local_v1.ProjectV1,
        *,
        last_event_id: str | None = None,
    ):
        session, generation = self._session()
        return _BridgeEventContext(self, session, generation, project, last_event_id)

    def _begin_activation(
        self,
        deadline: float,
        *,
        activation_id: str | None = None,
        cancel_event: threading.Event | None = None,
    ) -> _GenerationToken:
        if cancel_event is not None and cancel_event.is_set():
            raise _bridge_error(
                "active_project_session_superseded",
                "The project activation was cancelled before it started.",
                retryable=True,
            )
        self._acquire_transition(deadline)
        try:
            with self._lock:
                if self._closed or self._close_requested:
                    raise _bridge_error(
                        "desktop_core_bridge_closed",
                        "The Desktop Core bridge is closed.",
                    )
                previous = self._current_token_locked()
            if previous is not None:
                self._retire_token(previous, deadline=deadline)
            with self._lock:
                if self._closed or self._close_requested:
                    raise _bridge_error(
                        "desktop_core_bridge_closed",
                        "The Desktop Core bridge is closed.",
                    )
                if cancel_event is not None and cancel_event.is_set():
                    raise _bridge_error(
                        "active_project_session_superseded",
                        "The project activation was cancelled before Core work started.",
                        retryable=True,
                    )
                self._generation += 1
                token = _GenerationToken(
                    generation=self._generation,
                    external_lock=threading.RLock(),
                    resource_lock=threading.Lock(),
                    activation_id=activation_id,
                    cancel_event=cancel_event,
                )
                self._candidate = token
                return token
        finally:
            self._transition_lock.release()

    def _publish_activation(self, candidate: DesktopCoreActiveSessionV1) -> None:
        with self._lock:
            if (
                self._closed
                or self._close_requested
                or candidate.token.cancelled
                or (
                    candidate.token.cancel_event is not None
                    and candidate.token.cancel_event.is_set()
                )
                or self._candidate is not candidate.token
                or candidate.generation != self._generation
            ):
                raise _bridge_error(
                    "active_project_session_superseded",
                    "A newer active project session superseded this result.",
                    retryable=True,
                )
            self._active = candidate
            self._candidate = None

    def _current_token_locked(self) -> _GenerationToken | None:
        if self._candidate is not None:
            return self._candidate
        if self._active is not None:
            return self._active.token
        return None

    def _acquire_transition(self, deadline: float) -> None:
        if not self._transition_lock.acquire(timeout=_remaining_seconds(deadline)):
            raise _bridge_error(
                "core_bridge_transition_deadline_exceeded",
                "The active project transition did not finish before the deadline.",
                retryable=True,
            )

    def _retire_token(self, token: _GenerationToken, *, deadline: float) -> None:
        with self._lock:
            if token.retired:
                return
            token.cancelled = True
            if token.cancel_event is not None:
                token.cancel_event.set()
        with token.resource_lock:
            clients = tuple(token.clients or ())
        for client in clients:
            self._run_adapter_cleanup(
                token,
                deadline,
                client.close,
                label="Core client close",
            )
        if not token.external_lock.acquire(timeout=_remaining_seconds(deadline)):
            raise _bridge_error(
                "core_bridge_retirement_deadline_exceeded",
                "The previous Core session still owns an external call.",
                retryable=True,
            )
        try:
            self._wait_adapter_futures(token, deadline)
            with token.resource_lock:
                clients = tuple(token.clients or ())
                tunnels = tuple(token.tunnels or ())
                archives = tuple(token.archives or ())
            # Client construction may have completed after the first close
            # snapshot. Close the complete adopted set before other resources.
            for client in clients:
                self._run_adapter_cleanup(
                    token,
                    deadline,
                    client.close,
                    label="Core client close",
                )
            for archive in archives:
                self._run_adapter_cleanup(
                    token,
                    deadline,
                    archive.close,
                    label="workspace archive close",
                )
            for tunnel in tunnels:
                tunnel.close(deadline=deadline, token=token)
            self._wait_adapter_futures(token, deadline)
        finally:
            token.external_lock.release()
        with self._lock:
            token.retired = True
            if self._candidate is token:
                self._candidate = None
            if self._active is not None and self._active.token is token:
                self._active = None

    def _cleanup_failed_candidate(self, token: _GenerationToken, *, deadline: float) -> None:
        cleanup_deadline = max(deadline, time.monotonic() + self._timeout)
        self._acquire_transition(cleanup_deadline)
        try:
            self._retire_token(token, deadline=cleanup_deadline)
        finally:
            self._transition_lock.release()

    def _gate_token(self, token: _GenerationToken, deadline: float) -> None:
        self._remaining(deadline)
        with self._lock:
            current = self._current_token_locked()
            if (
                self._closed
                or self._close_requested
                or token.cancelled
                or (token.cancel_event is not None and token.cancel_event.is_set())
                or token.retired
                or current is not token
            ):
                raise _bridge_error(
                    "active_project_session_superseded",
                    "A newer active project session superseded this result.",
                    retryable=True,
                )

    def _request_token_interrupt(self, token: _GenerationToken) -> None:
        with token.resource_lock:
            clients = tuple(token.clients or ())
            tunnels = tuple(token.tunnels or ())
            futures = tuple(token.adapter_futures or ())
        for future in futures:
            future.cancel()
        for client in clients:
            future = _ADAPTER_EXECUTOR.submit(client.close)
            if future is None:
                continue
            token.track_future(future)
            future.add_done_callback(lambda completed: token.untrack_future(completed))
        for tunnel in tunnels:
            try:
                tunnel.request_close(token=token)
            except DesktopCoreBridgeErrorV1:
                pass

    def _external_call(
        self,
        token: _GenerationToken,
        deadline: float,
        action: Callable[[], _ResponseT],
        *,
        adapter_label: str | None = None,
        adopt: Callable[[_ResponseT], None] | None = None,
    ) -> _ResponseT:
        if not token.external_lock.acquire(timeout=_remaining_seconds(deadline)):
            raise _bridge_error(
                "core_bridge_external_call_deadline_exceeded",
                "The previous external bridge call did not finish before the deadline.",
                retryable=True,
            )
        try:
            self._gate_token(token, deadline)
            if adapter_label is None:
                result = action()
            else:
                result = self._run_adapter(
                    token,
                    deadline,
                    action,
                    label=adapter_label,
                    adopt=adopt,
                )
                adopt = None
            if adopt is not None:
                adopt(result)
            self._gate_token(token, deadline)
            return result
        finally:
            token.external_lock.release()

    def _core_external(
        self,
        token: _GenerationToken,
        deadline: float,
        action: Callable[[], _ResponseT],
    ) -> _ResponseT:
        try:
            return self._external_call(token, deadline, action)
        except CoreClientErrorV1 as exc:
            raise _bridge_client_error(exc) from None

    def _retry_core_external(
        self,
        token: _GenerationToken,
        deadline: float,
        action: Callable[[], _ResponseT],
    ) -> _ResponseT:
        core_response_received = False

        def tracked_action() -> _ResponseT:
            nonlocal core_response_received
            result = action()
            core_response_received = True
            return result

        try:
            return self._external_call(token, deadline, tracked_action)
        except CoreClientErrorV1 as exc:
            raise _bridge_client_error(exc) from None
        except DesktopCoreBridgeErrorV1:
            if core_response_received:
                raise CoreMutationOutcomeUnknownV1 from None
            raise

    def _adapter_external(
        self,
        token: _GenerationToken,
        deadline: float,
        action: Callable[[], _ResponseT],
        *,
        label: str,
        adopt: Callable[[_ResponseT], None] | None = None,
    ) -> _ResponseT:
        return self._external_call(
            token,
            deadline,
            action,
            adapter_label=label,
            adopt=adopt,
        )

    def _run_adapter(
        self,
        token: _GenerationToken,
        deadline: float,
        action: Callable[[], _ResponseT],
        *,
        label: str,
        adopt: Callable[[_ResponseT], None] | None = None,
    ) -> _ResponseT:
        def run_and_adopt() -> _ResponseT:
            result = action()
            if adopt is not None:
                adopt(result)
            return result

        future = _ADAPTER_EXECUTOR.submit(run_and_adopt)
        if future is None:
            raise _bridge_error(
                "core_bridge_adapter_capacity_exhausted",
                "The bounded bridge adapter executor is full.",
                retryable=True,
            )
        token.track_future(future)
        future.add_done_callback(lambda completed: token.untrack_future(completed))
        try:
            return future.result(timeout=_remaining_seconds(deadline))
        except FutureTimeoutError:
            if future.done():
                raise _bridge_error(
                    "core_bridge_adapter_failed",
                    f"The {label} adapter failed.",
                    retryable=True,
                ) from None
            future.cancel()
            raise _bridge_error(
                "core_bridge_adapter_deadline_exceeded",
                f"The {label} adapter did not finish before the deadline.",
                retryable=True,
            ) from None
        except DesktopCoreBridgeErrorV1:
            raise
        except CoreClientErrorV1 as exc:
            raise _bridge_client_error(exc) from None
        except BaseException:
            raise _bridge_error(
                "core_bridge_adapter_failed",
                f"The {label} adapter failed.",
                retryable=True,
            ) from None

    def _run_adapter_cleanup(
        self,
        token: _GenerationToken,
        deadline: float,
        action: Callable[[], None],
        *,
        label: str,
    ) -> None:
        self._run_adapter(token, deadline, action, label=label)

    def _wait_adapter_futures(self, token: _GenerationToken, deadline: float) -> None:
        while True:
            with token.resource_lock:
                pending = tuple(
                    future for future in (token.adapter_futures or ()) if not future.done()
                )
            if not pending:
                return
            for future in pending:
                try:
                    future.result(timeout=_remaining_seconds(deadline))
                except FutureTimeoutError:
                    if future.done():
                        continue
                    raise _bridge_error(
                        "core_bridge_retirement_deadline_exceeded",
                        "A bridge adapter still owns external work.",
                        retryable=True,
                    ) from None
                except BaseException:
                    pass

    def _bootstrap_connection(
        self,
        *,
        token: _GenerationToken,
        request: core_v1.ProjectCreateV1,
        operation: CoreProjectCreateOperationV1,
        attachment: CoreHostAttachmentV1,
        tunnel: CoreTunnelHandleV1,
        deadline: float,
    ) -> tuple[CoreTunnelConnectionV1, CoreProjectCreateOperationV1]:
        bootstrap_connection = CoreBootstrapTunnelConnectionV1(
            endpoint=tunnel.endpoint,
            bearer_token=attachment.bearer_token,
            session_id=tunnel.session_id,
        )
        if operation.state is CoreProjectCreateStateV1.BOUND:
            assert operation.core_project_id is not None
            return bootstrap_connection.bind(operation.core_project_id), operation
        bootstrap = self._adapter_external(
            token,
            deadline,
            lambda: self._new_bootstrap_client(bootstrap_connection, deadline),
            label="Core bootstrap client construction",
            adopt=token.add_client,
        )
        try:
            self._core_external(token, deadline, bootstrap.version)
            self._core_external(
                token,
                deadline,
                lambda: bootstrap.capabilities(request.spec.execution_mode),
            )
            if operation.state is CoreProjectCreateStateV1.PRE_CREATE:
                expected_unknown = replace(
                    operation,
                    state=CoreProjectCreateStateV1.UNKNOWN,
                )
                stored_unknown = self._adapter_external(
                    token,
                    deadline,
                    lambda: self._persistence.mark_create_unknown(operation),
                    label="project create outcome transition",
                )
                operation = _ensure_create_transition(
                    stored_unknown,
                    expected_unknown,
                    label="unknown-outcome",
                )
            result = self._core_external(
                token,
                deadline,
                lambda: bootstrap.create_project(
                    request,
                    idempotency_key=operation.idempotency_key,
                ),
            )
            _ensure_project_identity(result.project, request)
            immutable_authority = _patch_immutable_authority(result.project)
            expected_bound = replace(
                operation,
                state=CoreProjectCreateStateV1.BOUND,
                core_project_id=result.project.id,
                project_immutable_authority=immutable_authority,
            )
            stored_bound = self._adapter_external(
                token,
                deadline,
                lambda: self._persistence.bind_created_project(
                    operation,
                    result.project.id,
                    immutable_authority=immutable_authority,
                ),
                label="project create binding",
            )
            operation = _ensure_create_transition(
                stored_bound,
                expected_bound,
                label="bound",
            )
            return result.connection, operation
        finally:
            self._run_adapter_cleanup(
                token,
                max(deadline, time.monotonic() + self._timeout),
                bootstrap.close,
                label="bootstrap client close",
            )

    def _reconcile_mapped_project(
        self,
        *,
        token: _GenerationToken,
        deadline: float,
        client: CoreControlClientV1,
        mapping: CoreProjectMappingV1,
        current: core_v1.ProjectV1,
        requested: core_v1.ProjectCreateV1,
        request_sha256: str,
        core_host_identity: str,
    ) -> tuple[core_v1.ProjectV1, CoreProjectPatchOperationV1 | None]:
        _ensure_immutable_authority_transition(
            _mapping_immutable_authority(mapping),
            _patch_immutable_authority(current),
            mismatch_code="core_project_mapping_mismatch",
            mismatch_message=(
                "Core immutable authority does not match the durable project mapping."
            ),
        )
        _ensure_mutable_authority_transition(
            _mapping_mutable_authority(mapping),
            _patch_mutable_authority(current),
            project_id=mapping.core_project_id,
            label="durable project mapping",
            mismatch_code="core_project_mapping_mismatch",
            mismatch_message=(
                "Core mutable authority does not descend from the durable project mapping."
            ),
        )
        if mapping.request_sha256 == request_sha256:
            _ensure_project_identity(current, requested)
            _ensure_mapping_content_snapshots(mapping, current)
            return current, None

        if _project_identity_matches(current, requested):
            raise _bridge_error(
                "core_project_patch_proof_missing",
                "Core matches edited Local intent without a durable patch operation.",
                status=409,
            )

        _ensure_mapping_content_snapshots(mapping, current)
        _ensure_project_identity(current, mapping.project_create)
        return self._patch_current_project(
            token=token,
            deadline=deadline,
            client=client,
            current=current,
            requested=requested,
            previous_request=mapping.project_create,
            requested_request_sha256=request_sha256,
            local_project_id=mapping.local_project_id,
            profile_id=mapping.profile_id,
            core_host_identity=core_host_identity,
        )

    def _reconcile_bound_project(
        self,
        *,
        token: _GenerationToken,
        deadline: float,
        client: CoreControlClientV1,
        operation: CoreProjectCreateOperationV1,
        current: core_v1.ProjectV1,
        requested: core_v1.ProjectCreateV1,
        request_sha256: str,
        core_host_identity: str,
    ) -> tuple[core_v1.ProjectV1, CoreProjectPatchOperationV1 | None]:
        _ensure_immutable_authority_transition(
            _create_immutable_authority(operation),
            _patch_immutable_authority(current),
            mismatch_code="core_project_create_binding_mismatch",
            mismatch_message=(
                "Core immutable authority does not match the durable project create."
            ),
        )
        if _project_identity_matches(current, requested):
            raise _bridge_error(
                "core_project_patch_proof_missing",
                "Core matches edited Local intent without a durable patch operation.",
                status=409,
            )
        _ensure_project_identity(current, operation.project_create)
        return self._patch_current_project(
            token=token,
            deadline=deadline,
            client=client,
            current=current,
            requested=requested,
            previous_request=operation.project_create,
            requested_request_sha256=request_sha256,
            local_project_id=operation.local_project_id,
            profile_id=operation.profile_id,
            core_host_identity=core_host_identity,
        )

    def _patch_current_project(
        self,
        *,
        token: _GenerationToken,
        deadline: float,
        client: CoreControlClientV1,
        current: core_v1.ProjectV1,
        requested: core_v1.ProjectCreateV1,
        previous_request: core_v1.ProjectCreateV1,
        requested_request_sha256: str,
        local_project_id: str,
        profile_id: str,
        core_host_identity: str,
    ) -> tuple[core_v1.ProjectV1, CoreProjectPatchOperationV1]:
        patch = core_v1.ProjectPatchV1(
            name=requested.name,
            description=requested.description,
            spec=requested.spec,
            task=requested.task,
            workspace=requested.workspace,
        )
        previous_request_sha256 = _model_digest(previous_request)
        proposed = CoreProjectPatchOperationV1(
            local_project_id=local_project_id,
            profile_id=profile_id,
            core_host_identity=core_host_identity,
            core_project_id=current.id,
            old_request_sha256=previous_request_sha256,
            old_project_create=previous_request,
            new_request_sha256=requested_request_sha256,
            new_project_create=requested,
            patch_request_sha256=_model_digest(patch),
            patch=patch,
            idempotency_key=_derived_key(
                local_project_id,
                f"project-patch-{previous_request_sha256}-{requested_request_sha256}",
            ),
            base_project=current,
        )
        stored = self._adapter_external(
            token,
            deadline,
            lambda: self._persistence.reserve_patch(proposed),
            label="project patch reservation",
        )
        operation = _ensure_patch_transition(stored, proposed, label="reservation")
        patched, operation, _ = self._resume_project_patch(
            token=token,
            deadline=deadline,
            client=client,
            current=current,
            operation=operation,
        )
        return patched, operation

    def _resume_project_patch(
        self,
        *,
        token: _GenerationToken,
        deadline: float,
        client: CoreControlClientV1,
        current: core_v1.ProjectV1,
        operation: CoreProjectPatchOperationV1,
        workspace_authority: CoreProjectCreateOperationV1 | None = None,
    ) -> tuple[
        core_v1.ProjectV1,
        CoreProjectPatchOperationV1,
        tuple[core_v1.RevisionRefV1 | None, ...],
    ]:
        _ensure_immutable_authority_transition(
            _patch_immutable_authority(operation.base_project),
            _patch_immutable_authority(current),
            allow_project_patch=True,
            mismatch_code="core_project_patch_replay_mismatch",
            mismatch_message=("Current Core project changed immutable patch base identity."),
        )
        if operation.state is CoreProjectPatchStateV1.APPLIED:
            assert operation.outcome is not None
            finalize_authority: CoreWorkspaceUploadFinalizeAuthorityV1 | None = None
            if _patch_outcome_needs_workspace_finalize_proof(operation, current):
                if workspace_authority is None:
                    raise _bridge_error(
                        "core_project_patch_outcome_mismatch",
                        "The applied project patch lacks durable workspace finalize authority.",
                        status=409,
                    )
                finalize_authority = workspace_authority.workspace_upload_finalize
            revision_authorities = _ensure_persisted_patch_outcome(
                operation,
                current,
                finalize_authority=finalize_authority,
            )
            return current, operation, revision_authorities
        if operation.state is CoreProjectPatchStateV1.PRE_PATCH:
            expected_unknown = replace(
                operation,
                state=CoreProjectPatchStateV1.UNKNOWN,
            )
            stored_unknown = self._adapter_external(
                token,
                deadline,
                lambda: self._persistence.mark_patch_unknown(operation),
                label="project patch outcome transition",
            )
            operation = _ensure_patch_transition(
                stored_unknown,
                expected_unknown,
                label="unknown-outcome",
            )
        patched = self._core_external(
            token,
            deadline,
            lambda: client.patch_project(
                operation.patch,
                if_match=operation.base_project.etag,
                idempotency_key=operation.idempotency_key,
            ),
        )
        _ensure_project_identity(patched, operation.new_project_create)
        _ensure_patch_signed_new_snapshots(
            operation.base_project,
            patched,
            operation.new_project_create,
        )
        expected_applied = replace(
            operation,
            state=CoreProjectPatchStateV1.APPLIED,
            outcome=patched,
            outcome_immutable=_patch_immutable_authority(patched),
            outcome_mutable=_patch_mutable_authority(patched),
        )
        stored_applied = self._adapter_external(
            token,
            deadline,
            lambda: self._persistence.record_patch_applied(
                operation,
                patched,
                outcome_immutable=expected_applied.outcome_immutable,
                outcome_mutable=expected_applied.outcome_mutable,
            ),
            label="project patch outcome commit",
        )
        operation = _ensure_patch_transition(
            stored_applied,
            expected_applied,
            label="applied-outcome",
        )
        return patched, operation, ()

    def _publish_imported_workspace(
        self,
        *,
        token: _GenerationToken,
        client: CoreControlClientV1,
        local_project: local_v1.ProjectV1,
        core_project: core_v1.ProjectV1,
        operation: CoreProjectCreateOperationV1,
        deadline: float,
    ) -> tuple[core_v1.ProjectV1, CoreProjectCreateOperationV1]:
        import_ref = local_project.source.import_ref
        if import_ref is None or not isinstance(
            core_project.workspace, core_v1.ImportedWorkspaceSpecV1
        ):
            raise _bridge_error(
                "invalid_local_project",
                "The imported workspace does not have an adopted archive reference.",
                status=422,
            )
        finalize = operation.workspace_upload_finalize
        if finalize is not None:
            _ensure_workspace_finalize_authority_binding(finalize)
            matches_current_request = not (
                finalize.request.content_sha256 != import_ref.content_sha256
                or finalize.upload.archive != core_project.workspace.archive
                or finalize.upload.project_id != operation.core_project_id
                or finalize.upload.id != operation.workspace_upload_id
            )
            if (
                finalize.state is not CoreWorkspaceUploadFinalizeStateV1.APPLIED
                and not matches_current_request
            ):
                raise _bridge_error(
                    "workspace_finalize_authority_mismatch",
                    "The durable workspace finalize request does not match Local authority.",
                    status=409,
                )
            if (
                finalize.state is CoreWorkspaceUploadFinalizeStateV1.APPLIED
                and matches_current_request
            ):
                raise _bridge_error(
                    "workspace_finalize_authority_mismatch",
                    "Core lost the durable finalized workspace publication.",
                    status=409,
                )
            if finalize.state is not CoreWorkspaceUploadFinalizeStateV1.APPLIED:
                return self._resume_workspace_finalize(
                    token=token,
                    deadline=deadline,
                    client=client,
                    operation=operation,
                    expected_project=core_project,
                )
        if (
            operation.workspace_upload_id is not None
            and operation.workspace_upload_project_snapshot
            != core_project.current_project_snapshot
        ):
            operation = self._abort_stale_workspace_upload(
                token=token,
                deadline=deadline,
                client=client,
                operation=operation,
            )

        upload_key_seed = _derived_key(
            operation.idempotency_key,
            f"workspace-upload-{_model_digest(core_project.current_project_snapshot)}",
        )
        if operation.workspace_upload_id is None:
            upload_request = core_v1.WorkspaceUploadCreateV1(
                project_snapshot=core_project.current_project_snapshot,
                archive=core_project.workspace.archive,
                base_workspace_snapshot=core_project.current_workspace_snapshot,
            )
            upload = self._core_external(
                token,
                deadline,
                lambda: client.create_workspace_upload(
                    upload_request,
                    if_match=core_project.etag,
                    idempotency_key=_derived_key(upload_key_seed, "create"),
                ),
            )
            upload_operation = replace(
                operation,
                workspace_upload_id=upload.id,
                workspace_upload_project_snapshot=upload.project_snapshot,
            )
            stored_operation = self._adapter_external(
                token,
                deadline,
                lambda: self._persistence.update_create(
                    upload_operation,
                    expected_previous=operation,
                ),
                label="workspace upload binding",
            )
            operation = _ensure_create_transition(
                stored_operation,
                upload_operation,
                label="workspace-upload-bound",
            )
        else:
            upload = self._core_external(
                token,
                deadline,
                lambda: client.get_workspace_upload(operation.workspace_upload_id),
            )
        _ensure_workspace_upload_authority(upload, core_project)

        lease_holder: list[_ArchiveContextLease] = []

        def adopt_archive(context: AbstractContextManager[BinaryIO]) -> None:
            lease = _ArchiveContextLease(context=context, lock=threading.Lock())
            lease_holder.append(lease)
            token.add_archive(lease)

        self._adapter_external(
            token,
            deadline,
            lambda: self._archive_source.open_archive(import_ref),
            label="workspace archive open",
            adopt=adopt_archive,
        )
        lease = lease_holder[0]
        stream = self._adapter_external(
            token,
            deadline,
            lease.enter,
            label="workspace archive enter",
        )
        try:
            digest = hashlib.sha256()
            offset = 0
            upload_offset = upload.accepted_offset
            while offset < import_ref.byte_size:
                chunk_size = min(
                    WORKSPACE_CHUNK_BYTES,
                    import_ref.byte_size - offset,
                )
                chunk = self._adapter_external(
                    token,
                    deadline,
                    lambda: _read_archive_chunk(stream, chunk_size),
                    label="workspace archive read",
                )
                if not chunk:
                    raise _bridge_error(
                        "workspace_archive_mismatch",
                        "The adopted workspace archive ended before its declared size.",
                        status=422,
                    )
                digest.update(chunk)
                next_offset = offset + len(chunk)
                if next_offset > upload_offset:
                    if offset != upload_offset:
                        raise _bridge_error(
                            "workspace_upload_offset_mismatch",
                            "Core accepted an offset that is not aligned to the archive stream.",
                        )
                    chunk_request = core_v1.WorkspaceUploadChunkV1(
                        offset=offset,
                        byte_length=len(chunk),
                        content_base64=base64.b64encode(chunk).decode("ascii"),
                        content_sha256=hashlib.sha256(chunk).hexdigest(),
                    )
                    upload_id = upload.id
                    upload_etag = upload.etag
                    upload = self._core_external(
                        token,
                        deadline,
                        lambda: client.put_workspace_upload_chunk(
                            upload_id,
                            chunk_request,
                            if_match=upload_etag,
                            idempotency_key=_derived_key(upload_key_seed, f"chunk-{offset}"),
                        ),
                    )
                    upload_offset = upload.accepted_offset
                offset = next_offset
            trailing = self._adapter_external(
                token,
                deadline,
                lambda: stream.read(1),
                label="workspace archive trailing read",
            )
            if trailing:
                raise _bridge_error(
                    "workspace_archive_mismatch",
                    "The adopted workspace archive exceeds its declared size.",
                    status=422,
                )
        finally:
            self._run_adapter_cleanup(
                token,
                max(deadline, time.monotonic() + self._timeout),
                lease.close,
                label="workspace archive close",
            )
        if digest.hexdigest() != import_ref.content_sha256:
            raise _bridge_error(
                "workspace_archive_mismatch",
                "The adopted workspace archive digest changed.",
                status=422,
            )
        finalize_request = core_v1.WorkspaceUploadFinalizeV1(
            content_sha256=import_ref.content_sha256
        )
        finalize_authority = CoreWorkspaceUploadFinalizeAuthorityV1(
            upload=upload,
            request_sha256=_model_digest(finalize_request),
            request=finalize_request,
            idempotency_key=_derived_key(upload_key_seed, "finalize"),
            upload_etag=upload.etag,
            project_etag=upload.project_etag,
        )
        prepared_operation = replace(
            operation,
            workspace_upload_finalize=finalize_authority,
        )
        stored_operation = self._adapter_external(
            token,
            deadline,
            lambda: self._persistence.update_create(
                prepared_operation,
                expected_previous=operation,
            ),
            label="workspace finalize request reservation",
        )
        operation = _ensure_create_transition(
            stored_operation,
            prepared_operation,
            label="workspace-finalize-reserved",
        )
        return self._resume_workspace_finalize(
            token=token,
            deadline=deadline,
            client=client,
            operation=operation,
            expected_project=core_project,
        )

    def _resume_workspace_finalize(
        self,
        *,
        token: _GenerationToken,
        deadline: float,
        client: CoreControlClientV1,
        operation: CoreProjectCreateOperationV1,
        expected_project: core_v1.ProjectV1,
    ) -> tuple[core_v1.ProjectV1, CoreProjectCreateOperationV1]:
        authority = operation.workspace_upload_finalize
        if authority is None:
            raise ValueError("workspace finalize resume requires durable authority")
        _ensure_workspace_finalize_authority_binding(authority)
        if authority.state is CoreWorkspaceUploadFinalizeStateV1.PRE_FINALIZE:
            unknown_authority = replace(
                authority,
                state=CoreWorkspaceUploadFinalizeStateV1.UNKNOWN,
            )
            unknown_operation = replace(
                operation,
                workspace_upload_finalize=unknown_authority,
            )
            stored_operation = self._adapter_external(
                token,
                deadline,
                lambda: self._persistence.update_create(
                    unknown_operation,
                    expected_previous=operation,
                ),
                label="workspace finalize outcome transition",
            )
            operation = _ensure_create_transition(
                stored_operation,
                unknown_operation,
                label="workspace-finalize-unknown",
            )
            authority = unknown_authority
        if authority.state is CoreWorkspaceUploadFinalizeStateV1.APPLIED:
            assert authority.outcome is not None
            _ensure_immutable_authority_transition(
                _patch_immutable_authority(expected_project),
                _patch_immutable_authority(authority.outcome.project),
                mismatch_code="workspace_finalize_authority_mismatch",
                mismatch_message=("Core workspace finalize changed immutable project authority."),
            )
            return authority.outcome.project, operation
        # Re-establish only the persisted open-upload membership needed for an exact replay.
        client._register_workspace_upload(authority.upload, exact_replay=True)
        finalized = self._core_external(
            token,
            deadline,
            lambda: client.finalize_workspace_upload(
                authority.upload.id,
                authority.request,
                if_match=authority.upload_etag,
                if_project_match=authority.project_etag,
                idempotency_key=authority.idempotency_key,
            ),
        )
        _ensure_immutable_authority_transition(
            _patch_immutable_authority(expected_project),
            _patch_immutable_authority(finalized.project),
            mismatch_code="workspace_finalize_authority_mismatch",
            mismatch_message="Core workspace finalize changed immutable project authority.",
        )
        applied_authority = replace(
            authority,
            state=CoreWorkspaceUploadFinalizeStateV1.APPLIED,
            outcome=finalized,
            outcome_sha256=_model_digest(finalized),
        )
        finalized_operation = replace(
            operation,
            workspace_upload_finalize=applied_authority,
        )
        stored_operation = self._adapter_external(
            token,
            deadline,
            lambda: self._persistence.update_create(
                finalized_operation,
                expected_previous=operation,
            ),
            label="workspace finalize authority commit",
        )
        operation = _ensure_create_transition(
            stored_operation,
            finalized_operation,
            label="workspace-finalize-authority",
        )
        return finalized.project, operation

    def _abort_stale_workspace_upload(
        self,
        *,
        token: _GenerationToken,
        deadline: float,
        client: CoreControlClientV1,
        operation: CoreProjectCreateOperationV1,
    ) -> CoreProjectCreateOperationV1:
        upload_id = operation.workspace_upload_id
        upload_snapshot = operation.workspace_upload_project_snapshot
        assert upload_id is not None and upload_snapshot is not None
        abort = operation.workspace_upload_abort
        if abort is None:
            upload = self._core_external(
                token,
                deadline,
                lambda: client.get_workspace_upload(upload_id),
            )
            if (
                upload.project_id != operation.core_project_id
                or upload.project_snapshot != upload_snapshot
            ):
                raise _bridge_error(
                    "workspace_upload_authority_mismatch",
                    "The stale workspace upload no longer matches its durable authority.",
                    status=409,
                )
            if upload.status is not core_v1.WorkspaceUploadStatus.OPEN:
                cleared = replace(
                    operation,
                    workspace_upload_id=None,
                    workspace_upload_project_snapshot=None,
                    workspace_upload_finalize=None,
                )
                stored = self._adapter_external(
                    token,
                    deadline,
                    lambda: self._persistence.update_create(
                        cleared,
                        expected_previous=operation,
                    ),
                    label="terminal stale workspace upload binding clear",
                )
                return _ensure_create_transition(
                    stored,
                    cleared,
                    label="terminal-workspace-upload-clear",
                )
            if upload.publication is not None:
                raise _bridge_error(
                    "workspace_upload_authority_mismatch",
                    "An open stale workspace upload unexpectedly has a publication.",
                    status=409,
                )
            request = core_v1.WorkspaceUploadAbortV1(
                reason="Desktop superseded this imported workspace draft."
            )
            abort = CoreWorkspaceUploadAbortOperationV1(
                upload=upload,
                request_sha256=_model_digest(request),
                request=request,
                idempotency_key=_derived_key(
                    operation.idempotency_key,
                    f"workspace-upload-abort-{upload.id}-{_model_digest(upload.project_snapshot)}",
                ),
            )
            proposed = replace(operation, workspace_upload_abort=abort)
            stored = self._adapter_external(
                token,
                deadline,
                lambda: self._persistence.update_create(
                    proposed,
                    expected_previous=operation,
                ),
                label="stale workspace upload abort reservation",
            )
            operation = _ensure_create_transition(
                stored,
                proposed,
                label="workspace-upload-abort-reservation",
            )
        if abort.state is CoreWorkspaceUploadAbortStateV1.PRE_ABORT:
            unknown_abort = replace(
                abort,
                state=CoreWorkspaceUploadAbortStateV1.UNKNOWN,
            )
            proposed = replace(operation, workspace_upload_abort=unknown_abort)
            stored = self._adapter_external(
                token,
                deadline,
                lambda: self._persistence.update_create(
                    proposed,
                    expected_previous=operation,
                ),
                label="stale workspace upload abort outcome transition",
            )
            operation = _ensure_create_transition(
                stored,
                proposed,
                label="workspace-upload-abort-unknown",
            )
            abort = unknown_abort

        aborted = self._core_external(
            token,
            deadline,
            lambda: client.abort_persisted_workspace_upload(
                abort.upload,
                abort.request,
                if_match=abort.upload.etag,
                idempotency_key=abort.idempotency_key,
            ),
        )
        if (
            aborted.status is not core_v1.WorkspaceUploadStatus.ABORTED
            or aborted.id != abort.upload.id
            or aborted.project_id != abort.upload.project_id
            or aborted.accepted_offset != abort.upload.accepted_offset
            or aborted.publication is not None
        ):
            raise _bridge_error(
                "workspace_upload_abort_mismatch",
                "Core returned an invalid stale workspace upload abort outcome.",
            )
        cleared = replace(
            operation,
            workspace_upload_id=None,
            workspace_upload_project_snapshot=None,
            workspace_upload_abort=None,
            workspace_upload_finalize=None,
        )
        stored = self._adapter_external(
            token,
            deadline,
            lambda: self._persistence.update_create(
                cleared,
                expected_previous=operation,
            ),
            label="stale workspace upload abort commit",
        )
        return _ensure_create_transition(
            stored,
            cleared,
            label="workspace-upload-abort-commit",
        )

    def _new_client(
        self, connection: CoreTunnelConnectionV1, deadline: float
    ) -> CoreControlClientV1:
        timeout = min(self._timeout, self._remaining(deadline))
        transport = self._new_transport()
        try:
            return CoreControlClientV1(
                connection,
                transport=transport,
                timeout=timeout,
            )
        except BaseException:
            if transport is not None:
                transport.close()
            raise

    def _new_bootstrap_client(
        self,
        connection: CoreBootstrapTunnelConnectionV1,
        deadline: float,
    ) -> CoreProjectBootstrapClientV1:
        timeout = min(self._timeout, self._remaining(deadline))
        transport = self._new_transport()
        try:
            return CoreProjectBootstrapClientV1(
                connection,
                transport=transport,
                timeout=timeout,
            )
        except BaseException:
            if transport is not None:
                transport.close()
            raise

    def _new_transport(self) -> httpx.BaseTransport | None:
        if self._transport_factory is None:
            return None
        return self._transport_factory()

    def _refresh_authority(
        self,
        session: DesktopCoreActiveSessionV1,
        deadline: float,
    ) -> tuple[core_v1.ProjectV1, core_v1.CapabilitiesResponseV1]:
        capabilities = self._core_external(
            session.token,
            deadline,
            lambda: session.client.capabilities(session.project.spec.execution_mode),
        )
        project = self._core_external(
            session.token,
            deadline,
            session.client.get_project,
        )
        self._ensure_refreshed_project_authority(session, project)
        self._ensure_project_ready(project, capabilities)
        session.project = project
        session.capabilities = capabilities
        return project, capabilities

    @staticmethod
    def _ensure_refreshed_project_authority(
        session: DesktopCoreActiveSessionV1,
        project: core_v1.ProjectV1,
    ) -> None:
        previous = session.project
        mapping = session.mapping
        _ensure_project_identity(project, mapping.project_create)
        _ensure_immutable_authority_transition(
            _patch_immutable_authority(previous),
            _patch_immutable_authority(project),
            mismatch_code="core_project_identity_mismatch",
            mismatch_message=("The refreshed Core project changed immutable project authority."),
        )
        _ensure_mapping_content_snapshots(mapping, project)
        _ensure_mutable_authority_transition(
            _patch_mutable_authority(previous),
            _patch_mutable_authority(project),
            project_id=mapping.core_project_id,
            label="active project session",
            mismatch_code="core_project_refresh_authority_mismatch",
            mismatch_message=(
                "The refreshed Core project changed outside successor publication authority."
            ),
        )

    def _validate_current(
        self,
        token: _GenerationToken,
        deadline: float,
        client: CoreControlClientV1,
        project: core_v1.ProjectV1,
        capabilities: core_v1.CapabilitiesResponseV1,
        *,
        idempotency_key: str,
    ) -> core_v1.ProjectValidationResponseV1:
        workspace_snapshot = project.current_workspace_snapshot
        if workspace_snapshot is None:
            raise _bridge_error(
                "core_project_not_ready",
                "Core has not published the project workspace snapshot.",
            )
        request = core_v1.ProjectValidationRequestV1(
            project_snapshot=project.current_project_snapshot,
            workspace_snapshot=workspace_snapshot,
            expected_registry_digest=capabilities.registry_digest,
        )
        return self._core_external(
            token,
            deadline,
            lambda: client.validate_project(
                request,
                idempotency_key=idempotency_key,
            ),
        )

    @staticmethod
    def _ensure_project_ready(
        project: core_v1.ProjectV1,
        capabilities: core_v1.CapabilitiesResponseV1,
    ) -> None:
        if (
            project.status is not core_v1.ProjectStatus.READY
            or project.active_revision is None
            or project.current_workspace_snapshot is None
            or project.registry_digest != capabilities.registry_digest
            or project.model_preparation.status is not core_v1.ModelPreparationStatus.READY
        ):
            raise _bridge_error(
                "core_project_not_ready",
                "Core has not prepared the project, model, registry, and active revision.",
                retryable=True,
            )

    def _session(self) -> tuple[DesktopCoreActiveSessionV1, int]:
        with self._lock:
            if self._closed or self._close_requested:
                raise _bridge_error(
                    "desktop_core_bridge_closed",
                    "The Desktop Core bridge is closed.",
                )
            session = self._active
            if session is None:
                raise _bridge_error(
                    "active_project_session_unavailable",
                    "Desktop has no active project Core tunnel.",
                    retryable=True,
                )
            return session, self._generation

    def _invoke_project(
        self,
        project: local_v1.ProjectV1,
        call: Callable[[DesktopCoreActiveSessionV1, float], _ResponseT],
    ) -> _ResponseT:
        session, _generation = self._session()
        deadline = time.monotonic() + self._timeout

        def bound_call() -> _ResponseT:
            self._ensure_local_project_binding(session, project)
            return call(session, deadline)

        return self._core_external(session.token, deadline, bound_call)

    @staticmethod
    def _ensure_local_project_binding(
        session: DesktopCoreActiveSessionV1,
        project: local_v1.ProjectV1,
    ) -> None:
        if session.local_project_id != project.project_id:
            raise _bridge_error(
                "active_project_mismatch",
                "The requested resource does not belong to the active local project.",
                status=409,
            )
        if session.profile_id != project.profile_id or session.local_project_etag != project.etag:
            raise _bridge_error(
                "active_local_project_version_mismatch",
                "The saved local project changed after this Core session was activated.",
                status=409,
            )
        intent_sha256 = _model_digest(map_project_create_v1(project))
        if session.local_project_intent_sha256 != intent_sha256:
            raise _bridge_error(
                "active_local_project_version_mismatch",
                "The saved local project changed after this Core session was activated.",
                status=409,
            )

    @staticmethod
    def _ensure_activation_acknowledgement(
        session: DesktopCoreActiveSessionV1,
        generation: int,
        activation: CoreActivationV1,
    ) -> None:
        if (
            activation.generation != generation
            or activation.generation != session.generation
            or activation._authority is not session.activation._authority
            or activation != session.activation
        ):
            raise _bridge_error(
                "local_activation_acknowledgement_mismatch",
                "The acknowledgement was not produced by the active project generation.",
                status=409,
            )

    @staticmethod
    def _ensure_local_activation_projection(
        session: DesktopCoreActiveSessionV1,
        project: local_v1.ProjectV1,
    ) -> None:
        remote = project.remote
        intent_sha256 = _model_digest(map_project_create_v1(project))
        if (
            project.project_id != session.local_project_id
            or project.profile_id != session.profile_id
            or project.state != "active"
            or intent_sha256 != session.local_project_intent_sha256
            or remote is None
            or remote.status != "ready"
            or remote.core_project_id != session.project.id
            or remote.active_revision != session.project.active_revision
            or remote.registry_digest != session.capabilities.registry_digest
            or remote.registry_digest != session.project.registry_digest
            or remote.model_preparation != session.project.model_preparation
            or remote.etag != session.project.etag
        ):
            raise _bridge_error(
                "local_activation_projection_mismatch",
                "The durable Desktop project does not match the active Core session.",
                status=409,
            )

    def _ensure_generation(self, session: DesktopCoreActiveSessionV1, generation: int) -> None:
        with self._lock:
            if (
                self._closed
                or self._close_requested
                or session.token.cancelled
                or self._generation != generation
                or self._active is not session
            ):
                raise _bridge_error(
                    "active_project_session_superseded",
                    "A newer active project session superseded this result.",
                    retryable=True,
                )

    @staticmethod
    def _remaining(deadline: float) -> float:
        return _remaining_seconds(deadline)


class _BridgeEventContext:
    def __init__(
        self,
        bridge: DesktopCoreBridgeV1,
        session: DesktopCoreActiveSessionV1,
        generation: int,
        project: local_v1.ProjectV1,
        last_event_id: str | None,
    ) -> None:
        self._bridge = bridge
        self._session = session
        self._generation = generation
        self._project = project
        self._last_event_id = last_event_id
        self._context = None

    def __enter__(self):
        deadline = time.monotonic() + self._bridge._timeout

        def enter():
            self._bridge._ensure_generation(self._session, self._generation)
            self._bridge._ensure_local_project_binding(self._session, self._project)
            self._context = self._session.client.events(last_event_id=self._last_event_id)
            return self._context.__enter__()

        try:
            stream = self._bridge._core_external(
                self._session.token,
                deadline,
                enter,
            )
            return _BridgeEventIterator(
                self._bridge,
                self._session,
                self._generation,
                iter(stream),
            )
        except BaseException:
            if self._context is not None:
                self._bridge._run_adapter_cleanup(
                    self._session.token,
                    max(deadline, time.monotonic() + self._bridge._timeout),
                    lambda: self._context.__exit__(None, None, None),
                    label="Core event stream close",
                )
            raise

    def __exit__(self, *exc: object) -> None:
        if self._context is not None:
            self._bridge._run_adapter_cleanup(
                self._session.token,
                time.monotonic() + self._bridge._timeout,
                lambda: self._context.__exit__(*exc),
                label="Core event stream close",
            )


class _BridgeEventIterator:
    def __init__(
        self,
        bridge: DesktopCoreBridgeV1,
        session: DesktopCoreActiveSessionV1,
        generation: int,
        stream: Iterator[core_v1.SseFrameV1],
    ) -> None:
        self._bridge = bridge
        self._session = session
        self._generation = generation
        self._stream = stream

    def __iter__(self) -> _BridgeEventIterator:
        return self

    def __next__(self) -> core_v1.SseFrameV1:
        self._bridge._ensure_generation(self._session, self._generation)
        try:
            frame = next(self._stream)
        except CoreClientErrorV1 as exc:
            raise _bridge_client_error(exc) from None
        self._bridge._ensure_generation(self._session, self._generation)
        return frame


def _workspace_archive_policy() -> core_v1.WorkspaceArchivePolicyV1:
    return core_v1.WorkspaceArchivePolicyV1(
        media_type="application/vnd.openevo.workspace-tar",
        tar_format="posix_ustar",
        entry_types="regular_files_and_directories",
        path_policy="utf8_nfc_posix_relative_ustar_split_v1",
        entry_order="header_path_byte_lexicographic_parents_first",
        metadata_policy="uid_gid_zero_names_empty_mtime_zero",
        header_policy="posix_ustar_canonical_header_v1",
        body_policy="zero_pad_to_512_bytes",
        terminator_policy="two_zero_blocks_no_trailing_bytes",
        file_mode_policy="0644_or_0755",
        directory_mode="0755",
        allow_symlinks=False,
        allow_hardlinks=False,
        allow_devices=False,
        allow_fifos=False,
        allow_sparse_files=False,
        allow_tar_extensions=False,
        max_entries=100_000,
        max_path_depth=32,
        max_path_bytes=256,
        max_file_bytes=8_589_934_591,
        max_extracted_bytes=17_179_869_184,
    )


def _model_digest(model: core_v1.ContractModel) -> str:
    encoded = json.dumps(
        model.model_dump(mode="json"),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _ensure_workspace_finalize_authority_binding(
    authority: CoreWorkspaceUploadFinalizeAuthorityV1,
    *,
    require_applied: bool = False,
) -> None:
    try:
        authority.verify()
        if require_applied and (
            authority.state is not CoreWorkspaceUploadFinalizeStateV1.APPLIED
            or authority.outcome is None
        ):
            raise ValueError("workspace finalize outcome is not applied")
    except (AttributeError, TypeError, ValueError):
        raise _bridge_error(
            "workspace_finalize_authority_mismatch",
            "The durable workspace finalize authority is incomplete or corrupted.",
            status=409,
        ) from None


def _derived_key(base: str, purpose: str) -> str:
    digest = hashlib.sha256(f"{base}\0{purpose}".encode()).hexdigest()
    return f"desktop-core-{digest}"


def _remaining_seconds(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise _bridge_error(
            "desktop_core_bridge_deadline_exceeded",
            "The Desktop Core bridge operation deadline expired.",
            retryable=True,
        )
    return remaining


def _read_archive_chunk(stream: BinaryIO, byte_count: int) -> bytes:
    chunks: list[bytes] = []
    remaining = byte_count
    while remaining:
        value = stream.read(remaining)
        if not value:
            break
        chunks.append(value)
        remaining -= len(value)
    return b"".join(chunks)


def _ensure_create_operation(
    operation: CoreProjectCreateOperationV1,
    project: local_v1.ProjectV1,
    request_sha256: str,
    *,
    idempotency_key: str,
    core_host_identity: str,
) -> None:
    finalize = operation.workspace_upload_finalize
    if finalize is not None:
        _ensure_workspace_finalize_authority_binding(finalize)
    bound = operation.state is CoreProjectCreateStateV1.BOUND
    if (
        bound
        and finalize is not None
        and (finalize.state is CoreWorkspaceUploadFinalizeStateV1.APPLIED)
    ):
        assert finalize.outcome is not None
        create_immutable = _create_immutable_authority(operation)
        _ensure_immutable_authority_transition(
            create_immutable,
            _patch_immutable_authority(finalize.outcome.project),
            allow_project_patch=True,
            mismatch_code="core_project_create_replay_mismatch",
            mismatch_message=("Workspace finalize changed immutable project create identity."),
        )
    if (
        operation.local_project_id != project.project_id
        or operation.profile_id != project.profile_id
        or operation.core_host_identity != core_host_identity
        or _model_digest(operation.project_create) != operation.request_sha256
        or (not bound and operation.request_sha256 != request_sha256)
        or (not bound and operation.project_create != map_project_create_v1(project))
        or (not bound and operation.idempotency_key != idempotency_key)
    ):
        raise _bridge_error(
            "core_project_create_replay_mismatch",
            "The durable Core project create operation does not match this request.",
            status=409,
        )


def _ensure_create_transition(
    actual: CoreProjectCreateOperationV1,
    expected: CoreProjectCreateOperationV1,
    *,
    label: str,
) -> CoreProjectCreateOperationV1:
    if actual != expected:
        raise _bridge_error(
            "core_project_create_transition_mismatch",
            f"The durable project create {label} transition was not atomic.",
            status=409,
        )
    return actual


def _ensure_patch_transition(
    actual: CoreProjectPatchOperationV1,
    expected: CoreProjectPatchOperationV1,
    *,
    label: str,
) -> CoreProjectPatchOperationV1:
    if actual != expected:
        raise _bridge_error(
            "core_project_patch_transition_mismatch",
            f"The durable project patch {label} transition was not atomic.",
            status=409,
        )
    return actual


def _ensure_patch_operation_authority(
    patch: CoreProjectPatchOperationV1,
    project: local_v1.ProjectV1,
    *,
    operation: CoreProjectCreateOperationV1,
    mapping: CoreProjectMappingV1 | None,
    core_host_identity: str,
) -> None:
    if (
        patch.local_project_id != project.project_id
        or patch.profile_id != project.profile_id
        or patch.core_host_identity != core_host_identity
        or patch.core_project_id != operation.core_project_id
        or _model_digest(patch.old_project_create) != patch.old_request_sha256
        or _model_digest(patch.new_project_create) != patch.new_request_sha256
        or _model_digest(patch.patch) != patch.patch_request_sha256
    ):
        raise _bridge_error(
            "core_project_patch_replay_mismatch",
            "The durable Core project patch operation has invalid authority.",
            status=409,
        )
    if mapping is None:
        _ensure_immutable_authority_transition(
            _create_immutable_authority(operation),
            _patch_immutable_authority(patch.base_project),
            mismatch_code="core_project_patch_replay_mismatch",
            mismatch_message=(
                "The durable Core project patch base does not descend from the create authority."
            ),
        )
        base_matches = (
            patch.old_request_sha256 == operation.request_sha256
            and patch.old_project_create == operation.project_create
        )
    else:
        _ensure_immutable_authority_transition(
            _mapping_immutable_authority(mapping),
            _patch_immutable_authority(patch.base_project),
            mismatch_code="core_project_patch_replay_mismatch",
            mismatch_message=(
                "The durable Core project patch base does not descend from the mapping."
            ),
        )
        _ensure_mutable_authority_transition(
            _mapping_mutable_authority(mapping),
            _patch_mutable_authority(patch.base_project),
            project_id=patch.core_project_id,
            label="durable project patch base",
            mismatch_code="core_project_patch_replay_mismatch",
            mismatch_message=(
                "The durable Core project patch base does not descend from the mapping."
            ),
        )
        base_matches = (
            patch.old_request_sha256 == mapping.request_sha256
            and patch.old_project_create == mapping.project_create
            and patch.base_project.current_project_snapshot == mapping.project_snapshot
            and patch.base_project.current_task_snapshot == mapping.task_snapshot
            and patch.base_project.current_workspace_snapshot == mapping.workspace_snapshot
        )
    if not base_matches:
        raise _bridge_error(
            "core_project_patch_replay_mismatch",
            "The durable Core project patch does not descend from the current mapping.",
            status=409,
        )


def _ensure_persisted_patch_outcome(
    operation: CoreProjectPatchOperationV1,
    current: core_v1.ProjectV1,
    *,
    finalize_authority: CoreWorkspaceUploadFinalizeAuthorityV1 | None,
) -> tuple[core_v1.RevisionRefV1 | None, ...]:
    assert operation.outcome is not None
    assert operation.outcome_immutable is not None
    assert operation.outcome_mutable is not None
    _ensure_immutable_authority_transition(
        operation.outcome_immutable,
        _patch_immutable_authority(current),
        mismatch_code="core_project_patch_outcome_mismatch",
        mismatch_message=("Core no longer matches the immutable applied project patch authority."),
    )
    _ensure_project_identity(operation.outcome, operation.new_project_create)
    _ensure_patch_signed_new_snapshots(
        operation.base_project,
        operation.outcome,
        operation.new_project_create,
    )
    effective_authority = _effective_applied_revision_authority(operation)
    _ensure_revision_authority_chain(
        (
            operation.base_project.active_revision,
            effective_authority,
        ),
        project_id=operation.core_project_id,
        labels=("durable applied patch predecessor",),
    )
    revision_authorities = (
        operation.base_project.active_revision,
        effective_authority,
    )
    current_mutable = _patch_mutable_authority(current)
    same_content_authority = (
        current.current_project_snapshot == operation.outcome_mutable.project_snapshot
        and current.current_workspace_snapshot == operation.outcome_mutable.workspace_snapshot
        and current.workspace_publication == operation.outcome_mutable.workspace_publication
    )
    if same_content_authority:
        _ensure_mutable_authority_transition(
            operation.outcome_mutable,
            current_mutable,
            project_id=operation.core_project_id,
            label="durable applied patch outcome",
            mismatch_code="core_project_patch_outcome_mismatch",
            mismatch_message=(
                "Core mutable authority does not descend from the applied project patch."
            ),
        )
        return revision_authorities
    finalized_revision = _ensure_workspace_finalize_proof(
        operation,
        current,
        finalize_authority,
    )
    return (*revision_authorities, finalized_revision)


def _patch_outcome_needs_workspace_finalize_proof(
    operation: CoreProjectPatchOperationV1,
    current: core_v1.ProjectV1,
) -> bool:
    assert operation.outcome_mutable is not None
    return any(
        (
            current.current_project_snapshot != operation.outcome_mutable.project_snapshot,
            current.current_workspace_snapshot != operation.outcome_mutable.workspace_snapshot,
            current.workspace_publication != operation.outcome_mutable.workspace_publication,
        )
    )


def _ensure_workspace_finalize_proof(
    operation: CoreProjectPatchOperationV1,
    current: core_v1.ProjectV1,
    authority: CoreWorkspaceUploadFinalizeAuthorityV1 | None,
) -> core_v1.RevisionRefV1 | None:
    assert operation.outcome_mutable is not None
    assert operation.outcome_immutable is not None
    if authority is not None:
        _ensure_workspace_finalize_authority_binding(authority, require_applied=True)
    requested_workspace = operation.new_project_create.workspace
    upload = authority.upload if authority is not None else None
    finalized_project = authority.outcome.project if authority is not None else None
    if finalized_project is not None:
        _ensure_immutable_authority_transition(
            operation.outcome_immutable,
            _patch_immutable_authority(finalized_project),
            mismatch_code="core_project_patch_outcome_mismatch",
            mismatch_message=(
                "Core workspace finalize changed immutable applied patch authority."
            ),
        )
        _ensure_immutable_authority_transition(
            _patch_immutable_authority(finalized_project),
            _patch_immutable_authority(current),
            mismatch_code="core_project_patch_outcome_mismatch",
            mismatch_message=("Core no longer matches immutable workspace finalize authority."),
        )
    if not isinstance(requested_workspace, core_v1.ImportedWorkspaceSpecV1) or (
        authority is None
        or upload is None
        or finalized_project is None
        or upload.status is not core_v1.WorkspaceUploadStatus.OPEN
        or upload.project_id != operation.core_project_id
        or upload.project_snapshot != operation.outcome_mutable.project_snapshot
        or upload.project_etag != operation.outcome_mutable.etag
        or upload.archive != requested_workspace.archive
        or upload.base_workspace_snapshot != operation.outcome_mutable.workspace_snapshot
        or upload.accepted_offset != upload.archive.byte_size
        or authority.outcome.publication != current.workspace_publication
        or finalized_project.current_project_snapshot != current.current_project_snapshot
        or finalized_project.current_workspace_snapshot != current.current_workspace_snapshot
        or finalized_project.workspace_publication != current.workspace_publication
    ):
        raise _bridge_error(
            "core_project_patch_outcome_mismatch",
            "Core workspace publication does not prove the applied project patch successor.",
            status=409,
        )
    finalized_mutable = _patch_mutable_authority(finalized_project)
    current_mutable = _patch_mutable_authority(current)
    _ensure_revision_authority_successor(
        _effective_applied_revision_authority(operation),
        finalized_mutable.active_revision,
        project_id=operation.core_project_id,
        label=("durable applied patch outcome (durable workspace finalize predecessor)"),
    )
    _ensure_mutable_authority_transition(
        finalized_mutable,
        current_mutable,
        project_id=operation.core_project_id,
        label="durable workspace finalize",
        mismatch_code="core_project_patch_outcome_mismatch",
        mismatch_message=(
            "Core mutable authority does not descend from the durable workspace finalize."
        ),
    )
    return finalized_mutable.active_revision


def _patch_immutable_authority(
    project: core_v1.ProjectV1,
) -> CoreProjectPatchImmutableAuthorityV1:
    return CoreProjectPatchImmutableAuthorityV1(
        project_id=project.id,
        project_create=core_v1.ProjectCreateV1(
            name=project.name,
            description=project.description,
            spec=project.spec,
            task=project.task,
            workspace=project.workspace,
        ),
        task_snapshot=project.current_task_snapshot,
        created_at=project.created_at,
    )


def _effective_applied_revision_authority(
    operation: CoreProjectPatchOperationV1,
) -> core_v1.RevisionRefV1 | None:
    assert operation.outcome_mutable is not None
    return (
        operation.outcome_mutable.active_revision
        if operation.outcome_mutable.active_revision is not None
        else operation.base_project.active_revision
    )


def _patch_mutable_authority(
    project: core_v1.ProjectV1,
) -> CoreProjectPatchMutableAuthorityV1:
    return CoreProjectPatchMutableAuthorityV1(
        status=project.status,
        project_snapshot=project.current_project_snapshot,
        workspace_snapshot=project.current_workspace_snapshot,
        workspace_publication=project.workspace_publication,
        active_revision=project.active_revision,
        registry_digest=project.registry_digest,
        model_preparation=project.model_preparation,
        updated_at=project.updated_at,
        etag=project.etag,
    )


def _utc_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.removesuffix("Z") + "+00:00")


def _create_immutable_authority(
    operation: CoreProjectCreateOperationV1,
) -> CoreProjectPatchImmutableAuthorityV1:
    authority = operation.project_immutable_authority
    if (
        operation.state is not CoreProjectCreateStateV1.BOUND
        or operation.core_project_id is None
        or authority is None
        or authority.project_id != operation.core_project_id
        or authority.project_create != operation.project_create
    ):
        raise _bridge_error(
            "core_project_create_binding_mismatch",
            "The durable project create binding has incomplete immutable authority.",
            status=409,
        )
    return authority


def _mapping_immutable_authority(
    mapping: CoreProjectMappingV1,
) -> CoreProjectPatchImmutableAuthorityV1:
    authority = mapping.immutable_authority
    if (
        authority.project_id != mapping.core_project_id
        or authority.project_create != mapping.project_create
        or authority.task_snapshot != mapping.task_snapshot
    ):
        raise _bridge_error(
            "core_project_mapping_mismatch",
            "The durable Core project mapping has inconsistent immutable authority.",
            status=409,
        )
    return authority


def _mapping_mutable_authority(
    mapping: CoreProjectMappingV1,
) -> CoreProjectPatchMutableAuthorityV1:
    authority = mapping.mutable_authority
    if (
        authority.project_snapshot != mapping.project_snapshot
        or authority.workspace_snapshot != mapping.workspace_snapshot
        or authority.registry_digest != mapping.registry_digest
        or authority.etag != mapping.project_etag
        or authority.active_revision != mapping.active_revision
        or authority.updated_at != mapping.project_updated_at
        or authority.active_revision is None
        or authority.active_revision.project_id != mapping.core_project_id
    ):
        raise _bridge_error(
            "core_project_mapping_mismatch",
            "The durable Core project mapping has inconsistent mutable authority.",
            status=409,
        )
    return authority


def _ensure_mapping_authority(
    mapping: CoreProjectMappingV1,
    project: local_v1.ProjectV1,
    *,
    core_host_identity: str,
) -> None:
    _mapping_immutable_authority(mapping)
    _mapping_mutable_authority(mapping)
    if (
        mapping.local_project_id != project.project_id
        or mapping.profile_id != project.profile_id
        or mapping.core_host_identity != core_host_identity
        or _model_digest(mapping.project_create) != mapping.request_sha256
    ):
        raise _bridge_error(
            "core_project_mapping_mismatch",
            "The durable Core project mapping does not match the saved local project.",
            status=409,
        )


def _ensure_bound_operation(
    operation: CoreProjectCreateOperationV1 | None,
    mapping: CoreProjectMappingV1,
) -> CoreProjectCreateOperationV1:
    if (
        operation is None
        or operation.state is not CoreProjectCreateStateV1.BOUND
        or operation.local_project_id != mapping.local_project_id
        or operation.profile_id != mapping.profile_id
        or operation.core_host_identity != mapping.core_host_identity
        or operation.core_project_id != mapping.core_project_id
    ):
        raise _bridge_error(
            "core_project_create_binding_mismatch",
            "The durable project create binding does not match the Core mapping.",
            status=409,
        )
    if operation.workspace_upload_finalize is not None:
        finalize = operation.workspace_upload_finalize
        _ensure_workspace_finalize_authority_binding(finalize)
        if finalize.state is CoreWorkspaceUploadFinalizeStateV1.APPLIED:
            assert finalize.outcome is not None
            _ensure_immutable_authority_transition(
                _create_immutable_authority(operation),
                _patch_immutable_authority(finalize.outcome.project),
                allow_project_patch=True,
                mismatch_code="core_project_create_binding_mismatch",
                mismatch_message=("Workspace finalize changed immutable project create identity."),
            )
    _ensure_immutable_authority_transition(
        _create_immutable_authority(operation),
        _mapping_immutable_authority(mapping),
        allow_project_patch=True,
        mismatch_code="core_project_create_binding_mismatch",
        mismatch_message=("The Core mapping changed immutable project create identity."),
    )
    return operation


def _project_identity_matches(
    project: core_v1.ProjectV1,
    request: core_v1.ProjectCreateV1,
) -> bool:
    return not any(
        (
            project.name != request.name,
            project.description != request.description,
            project.spec != request.spec,
            project.task != request.task,
            project.workspace != request.workspace,
            project.execution_mode is not request.spec.execution_mode,
            project.workspace_kind.value != request.workspace.kind,
        )
    )


def _ensure_project_identity(project: core_v1.ProjectV1, request: core_v1.ProjectCreateV1) -> None:
    if not _project_identity_matches(project, request):
        raise _bridge_error(
            "core_project_identity_mismatch",
            "The Core project does not match the saved local project.",
            status=409,
        )


def _ensure_initial_publication_authority(
    operation: CoreProjectCreateOperationV1,
    descendant: core_v1.ProjectV1,
    *,
    pending_patch: CoreProjectPatchOperationV1 | None,
) -> core_v1.RevisionRefV1 | None:
    if (
        operation.state is not CoreProjectCreateStateV1.BOUND
        or operation.core_project_id is None
        or descendant.id != operation.core_project_id
    ):
        raise _bridge_error(
            "core_project_create_binding_mismatch",
            "The durable project create binding does not own the publication authority.",
            status=409,
        )
    create_immutable = _create_immutable_authority(operation)
    _ensure_immutable_authority_transition(
        create_immutable,
        _patch_immutable_authority(descendant),
        mismatch_code="core_project_initial_publication_mismatch",
        mismatch_message=("Core does not match the immutable project create authority."),
    )
    finalize = operation.workspace_upload_finalize
    finalized_project: core_v1.ProjectV1 | None = None
    if finalize is not None:
        _ensure_workspace_finalize_authority_binding(finalize)
        if finalize.state is CoreWorkspaceUploadFinalizeStateV1.APPLIED:
            assert finalize.outcome is not None
            candidate = finalize.outcome.project
            if _project_identity_matches(candidate, operation.project_create):
                _ensure_immutable_authority_transition(
                    create_immutable,
                    _patch_immutable_authority(candidate),
                    mismatch_code="core_project_initial_publication_mismatch",
                    mismatch_message=(
                        "The durable workspace finalize changed project create authority."
                    ),
                )
                finalized_project = candidate
            elif (
                pending_patch is not None
                and pending_patch.state is CoreProjectPatchStateV1.APPLIED
                and pending_patch.outcome_immutable is not None
                and _project_identity_matches(candidate, pending_patch.new_project_create)
            ):
                _ensure_immutable_authority_transition(
                    pending_patch.outcome_immutable,
                    _patch_immutable_authority(candidate),
                    mismatch_code="core_project_initial_publication_mismatch",
                    mismatch_message=(
                        "The durable workspace finalize changed applied patch authority."
                    ),
                )
            else:
                raise _bridge_error(
                    "core_project_initial_publication_mismatch",
                    "The durable workspace publication does not match a proven project intent.",
                    status=409,
                )

    authority = finalized_project.active_revision if finalized_project is not None else None
    if finalized_project is not None:
        _ensure_immutable_authority_transition(
            _patch_immutable_authority(finalized_project),
            _patch_immutable_authority(descendant),
            mismatch_code="core_project_initial_publication_mismatch",
            mismatch_message=(
                "Core no longer matches immutable initial workspace publication authority."
            ),
        )
        same_content_authority = (
            finalized_project.id == operation.core_project_id == descendant.id
            and descendant.current_project_snapshot == finalized_project.current_project_snapshot
            and descendant.current_workspace_snapshot
            == finalized_project.current_workspace_snapshot
            and descendant.workspace_publication == finalized_project.workspace_publication
        )
        if not same_content_authority:
            raise _bridge_error(
                "core_project_initial_publication_mismatch",
                "Core no longer descends from the durable initial workspace publication.",
                status=409,
            )
        _ensure_mutable_authority_transition(
            _patch_mutable_authority(finalized_project),
            _patch_mutable_authority(descendant),
            project_id=operation.core_project_id,
            label="durable initial workspace publication",
            mismatch_code="core_project_initial_publication_mismatch",
            mismatch_message=(
                "Core no longer descends from the durable initial workspace publication."
            ),
        )
    else:
        _ensure_revision_authority_successor(
            authority,
            descendant.active_revision,
            project_id=operation.core_project_id,
            label="durable initial workspace publication",
        )
    return authority


def _ensure_patch_signed_new_snapshots(
    previous: core_v1.ProjectV1,
    patched: core_v1.ProjectV1,
    requested: core_v1.ProjectCreateV1,
) -> None:
    _ensure_immutable_authority_transition(
        _patch_immutable_authority(previous),
        _patch_immutable_authority(patched),
        allow_project_patch=True,
        mismatch_code="core_project_patch_outcome_mismatch",
        mismatch_message="Core patch changed immutable project identity.",
    )
    if patched.current_project_snapshot == previous.current_project_snapshot:
        raise _bridge_error(
            "core_project_patch_not_versioned",
            "Core patched the project without signing a new project snapshot.",
        )
    if patched.etag == previous.etag:
        raise _bridge_error(
            "core_project_patch_not_versioned",
            "Core patched the project without issuing a new ETag.",
        )
    if previous.task != requested.task and (
        patched.current_task_snapshot == previous.current_task_snapshot
    ):
        raise _bridge_error(
            "core_project_patch_not_versioned",
            "Core patched the task without signing a new task snapshot.",
        )
    if previous.task == requested.task and (
        patched.current_task_snapshot != previous.current_task_snapshot
    ):
        raise _bridge_error(
            "core_project_patch_not_versioned",
            "Core changed the task snapshot without changing task content.",
        )
    if (
        previous.workspace != requested.workspace
        and (patched.current_workspace_snapshot == previous.current_workspace_snapshot)
        and not (
            patched.current_workspace_snapshot is None
            and isinstance(previous.workspace, core_v1.ImportedWorkspaceSpecV1)
            and isinstance(requested.workspace, core_v1.ImportedWorkspaceSpecV1)
        )
    ):
        raise _bridge_error(
            "core_project_patch_not_versioned",
            "Core patched the workspace without changing its snapshot state.",
        )
    if previous.workspace == requested.workspace and (
        patched.current_workspace_snapshot != previous.current_workspace_snapshot
    ):
        raise _bridge_error(
            "core_project_patch_not_versioned",
            "Core changed the workspace snapshot without changing workspace content.",
        )


def _ensure_mapping_content_snapshots(
    mapping: CoreProjectMappingV1,
    project: core_v1.ProjectV1,
) -> None:
    _ensure_immutable_authority_transition(
        _mapping_immutable_authority(mapping),
        _patch_immutable_authority(project),
        mismatch_code="core_project_mapping_mismatch",
        mismatch_message=(
            "The Core project changed immutable authority outside Desktop authority."
        ),
    )
    if (
        mapping.core_project_id != project.id
        or mapping.project_snapshot != project.current_project_snapshot
        or mapping.task_snapshot != project.current_task_snapshot
        or mapping.workspace_snapshot != project.current_workspace_snapshot
    ):
        raise _bridge_error(
            "core_project_mapping_mismatch",
            "The Core project identity or immutable content snapshots changed outside Desktop authority.",
            status=409,
        )


def _ensure_revision_authority_successor(
    authority: core_v1.RevisionRefV1 | None,
    current: core_v1.RevisionRefV1 | None,
    *,
    project_id: str,
    label: str,
) -> None:
    valid_initial_revision = authority is None and (
        current is None or (current.project_id == project_id and current.generation == 0)
    )
    valid_same_revision = (
        authority is not None and authority.project_id == project_id and current == authority
    )
    valid_direct_successor = (
        authority is not None
        and authority.project_id == project_id
        and current is not None
        and current.project_id == authority.project_id
        and current.generation == authority.generation + 1
        and current.id != authority.id
    )
    if not valid_initial_revision and not valid_same_revision and not valid_direct_successor:
        raise _bridge_error(
            "core_project_revision_authority_mismatch",
            f"Core active revision does not directly descend from the {label} authority.",
            status=409,
        )


def _ensure_immutable_authority_transition(
    authority: CoreProjectPatchImmutableAuthorityV1,
    current: CoreProjectPatchImmutableAuthorityV1,
    *,
    allow_project_patch: bool = False,
    mismatch_code: str,
    mismatch_message: str,
) -> None:
    if allow_project_patch:
        expected_authority = replace(
            authority,
            project_create=current.project_create,
            task_snapshot=current.task_snapshot,
        )
    else:
        expected_authority = authority
    if current != expected_authority:
        raise _bridge_error(
            mismatch_code,
            mismatch_message,
            status=409,
        )


def _ensure_mutable_authority_transition(
    authority: CoreProjectPatchMutableAuthorityV1,
    current: CoreProjectPatchMutableAuthorityV1,
    *,
    project_id: str,
    label: str,
    mismatch_code: str,
    mismatch_message: str,
) -> None:
    _ensure_revision_authority_successor(
        authority.active_revision,
        current.active_revision,
        project_id=project_id,
        label=label,
    )
    if current.active_revision == authority.active_revision:
        valid_transition = current == authority
    else:
        expected_successor = replace(
            authority,
            active_revision=current.active_revision,
            registry_digest=current.registry_digest,
            updated_at=current.updated_at,
            etag=current.etag,
        )
        valid_transition = (
            current == expected_successor
            and current.etag != authority.etag
            and _utc_timestamp(current.updated_at) > _utc_timestamp(authority.updated_at)
        )
    if not valid_transition:
        raise _bridge_error(
            mismatch_code,
            mismatch_message,
            status=409,
        )


def _ensure_revision_authority_chain(
    authorities: tuple[core_v1.RevisionRefV1 | None, ...],
    *,
    project_id: str,
    labels: tuple[str, ...],
) -> None:
    if len(authorities) < 2 or len(labels) != len(authorities) - 1:
        raise ValueError("revision authority chain labels must describe every edge")
    for authority, current, label in zip(authorities[:-1], authorities[1:], labels, strict=True):
        _ensure_revision_authority_successor(
            authority,
            current,
            project_id=project_id,
            label=label,
        )


def _ensure_workspace_upload_authority(
    upload: core_v1.WorkspaceUploadSessionV1,
    project: core_v1.ProjectV1,
) -> None:
    if not isinstance(project.workspace, core_v1.ImportedWorkspaceSpecV1) or (
        upload.status is not core_v1.WorkspaceUploadStatus.OPEN
        or upload.project_id != project.id
        or upload.project_snapshot != project.current_project_snapshot
        or upload.project_etag != project.etag
        or upload.archive != project.workspace.archive
        or upload.base_workspace_snapshot != project.current_workspace_snapshot
        or upload.publication is not None
    ):
        raise _bridge_error(
            "workspace_upload_authority_mismatch",
            "The persisted workspace upload does not belong to the current Core project version.",
            status=409,
        )


def _mapping_from_project(
    local_project: local_v1.ProjectV1,
    request_sha256: str,
    project: core_v1.ProjectV1,
    capabilities: core_v1.CapabilitiesResponseV1,
    *,
    core_host_identity: str,
    previous_mapping: CoreProjectMappingV1 | None,
    revision_authorities: tuple[core_v1.RevisionRefV1 | None, ...] = (),
    initial_revision_authority: core_v1.RevisionRefV1 | None = None,
) -> CoreProjectMappingV1:
    return _mapping_from_request(
        local_project_id=local_project.project_id,
        profile_id=local_project.profile_id,
        request=map_project_create_v1(local_project),
        request_sha256=request_sha256,
        project=project,
        capabilities=capabilities,
        core_host_identity=core_host_identity,
        previous_mapping=previous_mapping,
        revision_authorities=revision_authorities,
        initial_revision_authority=initial_revision_authority,
    )


def _mapping_from_request(
    *,
    local_project_id: str,
    profile_id: str,
    request: core_v1.ProjectCreateV1,
    request_sha256: str,
    project: core_v1.ProjectV1,
    capabilities: core_v1.CapabilitiesResponseV1,
    core_host_identity: str,
    previous_mapping: CoreProjectMappingV1 | None,
    revision_authorities: tuple[core_v1.RevisionRefV1 | None, ...] = (),
    initial_revision_authority: core_v1.RevisionRefV1 | None = None,
) -> CoreProjectMappingV1:
    workspace_snapshot = project.current_workspace_snapshot
    active_revision = project.active_revision
    if workspace_snapshot is None or active_revision is None:
        raise _bridge_error(
            "core_project_not_ready",
            "Core has not published the project workspace snapshot and active revision.",
        )
    immutable_authority = _patch_immutable_authority(project)
    mutable_authority = _patch_mutable_authority(project)
    if previous_mapping is not None:
        _mapping_immutable_authority(previous_mapping)
        _mapping_mutable_authority(previous_mapping)
    chain_start = (
        previous_mapping.active_revision
        if previous_mapping is not None
        else initial_revision_authority
    )
    authorities = (chain_start, *revision_authorities, active_revision)
    authority_label = (
        "previous project mapping"
        if previous_mapping is not None
        else "initial project publication"
    )
    _ensure_revision_authority_chain(
        authorities,
        project_id=project.id,
        labels=(authority_label,) * (len(authorities) - 1),
    )
    if previous_mapping is None:
        mapping_generation = 1
        predecessor_request_sha256 = None
    elif (
        previous_mapping.request_sha256 == request_sha256
        and previous_mapping.project_snapshot == project.current_project_snapshot
        and previous_mapping.task_snapshot == project.current_task_snapshot
        and previous_mapping.workspace_snapshot == workspace_snapshot
        and previous_mapping.registry_digest == capabilities.registry_digest
        and previous_mapping.immutable_authority == immutable_authority
        and previous_mapping.mutable_authority == mutable_authority
    ):
        mapping_generation = previous_mapping.mapping_generation
        predecessor_request_sha256 = previous_mapping.predecessor_request_sha256
    else:
        mapping_generation = previous_mapping.mapping_generation + 1
        predecessor_request_sha256 = previous_mapping.request_sha256
    return CoreProjectMappingV1(
        local_project_id=local_project_id,
        profile_id=profile_id,
        core_host_identity=core_host_identity,
        core_project_id=project.id,
        request_sha256=request_sha256,
        project_create=request,
        project_snapshot=project.current_project_snapshot,
        task_snapshot=project.current_task_snapshot,
        workspace_snapshot=workspace_snapshot,
        registry_digest=capabilities.registry_digest,
        project_etag=project.etag,
        active_revision=active_revision,
        project_updated_at=project.updated_at,
        immutable_authority=immutable_authority,
        mutable_authority=mutable_authority,
        mapping_generation=mapping_generation,
        predecessor_request_sha256=predecessor_request_sha256,
    )


def _select_required_revision(
    head: core_v1.RevisionHeadV1,
) -> core_v1.ReachableRequiredRevisionRefV1:
    successor = head.successor_revision
    transition = head.transition
    if (
        successor is not None
        and transition is not None
        and transition.state
        not in {
            core_v1.RevisionTransitionState.FAILED,
            core_v1.RevisionTransitionState.CANCELLED,
            core_v1.RevisionTransitionState.UNAVAILABLE,
        }
    ):
        return core_v1.ReachableRequiredRevisionRefV1(
            revision=successor,
            reachable_from_revision_id=head.active_revision.id,
            relation=core_v1.RequiredRevisionRelation.SUCCESSOR,
        )
    return core_v1.ReachableRequiredRevisionRefV1(
        revision=head.active_revision,
        reachable_from_revision_id=head.active_revision.id,
        relation=core_v1.RequiredRevisionRelation.ACTIVE,
    )


def _bridge_error(
    code: str,
    message: str,
    *,
    status: int = 503,
    retryable: bool = False,
) -> DesktopCoreBridgeErrorV1:
    return DesktopCoreBridgeErrorV1(
        core_v1.ApiErrorV1(
            request_id=f"desktop-core-bridge-{secrets.token_hex(8)}",
            code=code,
            http_status=status,
            message=message,
            severity=core_v1.ErrorSeverity.BLOCKING,
            category=core_v1.ErrorCategory.SERVICE,
            retryable=retryable,
            repair_action=(
                core_v1.RepairAction.OPENEVO_CAN_RETRY
                if retryable
                else core_v1.RepairAction.UNSUPPORTED
            ),
            next_action=(
                "Retry after the active Core session is ready."
                if retryable
                else "Reconnect and activate the saved project."
            ),
        )
    )


def _bridge_client_error(exc: CoreClientErrorV1) -> DesktopCoreBridgeErrorV1:
    if isinstance(exc.error, core_v1.ApiErrorV1):
        return DesktopCoreBridgeErrorV1(exc.error)
    error = exc.error
    return DesktopCoreBridgeErrorV1(
        core_v1.ApiErrorV1(
            request_id=f"desktop-core-client-{secrets.token_hex(8)}",
            code=error.code.value,
            http_status=exc.status_code,
            message=error.message,
            severity=core_v1.ErrorSeverity.BLOCKING,
            category=core_v1.ErrorCategory.CONTRACT,
            retryable=error.retryable,
            repair_action=(
                core_v1.RepairAction.OPENEVO_CAN_RETRY
                if error.retryable
                else core_v1.RepairAction.UNSUPPORTED
            ),
            next_action=(
                "Retry after reconnecting the active Core session."
                if error.retryable
                else "Reconnect and activate the saved project."
            ),
        )
    )


__all__ = (
    "CoreActivationV1",
    "CoreHostAttachmentV1",
    "CoreHostService",
    "CoreProjectCreateOperationV1",
    "CoreProjectCreateStateV1",
    "CoreProjectMappingV1",
    "CoreProjectPatchOperationV1",
    "CoreProjectPatchImmutableAuthorityV1",
    "CoreProjectPatchMutableAuthorityV1",
    "CoreProjectPatchStateV1",
    "CoreTunnelFactory",
    "CoreTunnelHandleV1",
    "DesktopCoreActiveSessionV1",
    "DesktopCoreBridgeErrorV1",
    "DesktopCoreBridgePersistence",
    "DesktopCoreBridgeV1",
    "CoreWorkspaceUploadAbortOperationV1",
    "CoreWorkspaceUploadAbortStateV1",
    "CoreWorkspaceUploadFinalizeAuthorityV1",
    "WorkspaceArchiveSource",
    "map_project_create_v1",
)
