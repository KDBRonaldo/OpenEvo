"""Active-project bridge from Desktop Local API intent to Core Control API v1."""

from __future__ import annotations

import base64
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, replace
import hashlib
import json
import secrets
import threading
import time
from typing import Any, BinaryIO, Protocol, TypeVar

import httpx

from desktop.sidecar.contracts.v1 import models as local_v1
from desktop.sidecar.core_client_v1 import (
    CoreBootstrapTunnelConnectionV1,
    CoreControlClientV1,
    CoreProjectBootstrapClientV1,
    CoreTunnelConnectionV1,
)
from openevo.backend.contracts.v1 import models as core_v1


DEFAULT_BRIDGE_TIMEOUT_SECONDS = 60.0
MAX_BRIDGE_TIMEOUT_SECONDS = 300.0
WORKSPACE_CHUNK_BYTES = core_v1.MAX_WORKSPACE_CHUNK_BYTES

_ResponseT = TypeVar("_ResponseT")


@dataclass(frozen=True, slots=True)
class CoreHostAttachmentV1:
    """Host-global Core authority returned without exposing a Core URL."""

    profile_id: str
    remote_port: int
    bearer_token: str
    bearer_identity: str

    def __post_init__(self) -> None:
        if not self.profile_id or not self.bearer_identity:
            raise ValueError("Core host attachment identities must not be empty")
        if not 1 <= self.remote_port <= 65_535:
            raise ValueError("Core host attachment remote port is invalid")
        if len(self.bearer_token) < 43:
            raise ValueError("Core host bearer must contain at least 256 bits")


class CoreHostService(Protocol):
    """Ensures or attaches the one host-global Core process."""

    def ensure_core(self, profile_id: str, *, deadline: float) -> CoreHostAttachmentV1: ...


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

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        try:
            self._close_callback()
        except Exception:
            pass


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


@dataclass(frozen=True, slots=True)
class CoreProjectCreateOperationV1:
    local_project_id: str
    profile_id: str
    core_host_identity: str
    request_sha256: str
    idempotency_key: str
    core_project_id: str | None = None
    workspace_upload_id: str | None = None


@dataclass(frozen=True, slots=True)
class CoreProjectMappingV1:
    local_project_id: str
    profile_id: str
    core_host_identity: str
    core_project_id: str
    request_sha256: str
    project_snapshot: core_v1.ImmutableSnapshotRefV1
    task_snapshot: core_v1.ImmutableSnapshotRefV1
    workspace_snapshot: core_v1.ImmutableSnapshotRefV1
    registry_digest: str


class DesktopCoreBridgePersistence(Protocol):
    """Durable callback boundary; implementations must provide atomic exact replay."""

    def load_mapping(self, local_project_id: str) -> CoreProjectMappingV1 | None: ...

    def reserve_create(
        self, operation: CoreProjectCreateOperationV1
    ) -> CoreProjectCreateOperationV1: ...

    def update_create(self, operation: CoreProjectCreateOperationV1) -> None: ...

    def commit_mapping(
        self,
        operation: CoreProjectCreateOperationV1,
        mapping: CoreProjectMappingV1,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class CoreActivationV1:
    local_project_id: str
    core_project: core_v1.ProjectV1
    capabilities: core_v1.CapabilitiesResponseV1
    revision_head: core_v1.RevisionHeadV1
    validation: core_v1.ProjectValidationResponseV1


@dataclass(slots=True)
class DesktopCoreActiveSessionV1:
    generation: int
    local_project_id: str
    profile_id: str
    attachment: CoreHostAttachmentV1
    tunnel: CoreTunnelHandleV1
    client: CoreControlClientV1
    project: core_v1.ProjectV1
    capabilities: core_v1.CapabilitiesResponseV1
    revision_head: core_v1.RevisionHeadV1

    def close(self) -> None:
        try:
            self.client.close()
        finally:
            self.tunnel.close()


class DesktopCoreBridgeErrorV1(RuntimeError):
    def __init__(self, error: core_v1.ApiErrorV1) -> None:
        super().__init__(error.message)
        self.error = error


def map_project_create_v1(project: local_v1.ProjectV1) -> core_v1.ProjectCreateV1:
    """Map saved Local project intent into the one frozen Core create contract."""

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
    ) -> None:
        if not 0 < timeout <= MAX_BRIDGE_TIMEOUT_SECONDS:
            raise ValueError("bridge timeout must be finite and at most 300 seconds")
        self._host_service = host_service
        self._tunnel_factory = tunnel_factory
        self._persistence = persistence
        self._archive_source = archive_source
        self._transport_factory = transport_factory
        self._timeout = float(timeout)
        self._lock = threading.RLock()
        self._generation = 0
        self._closed = False
        self._active: DesktopCoreActiveSessionV1 | None = None

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._generation += 1
            active = self._active
            self._active = None
        if active is not None:
            active.close()

    def activate_project(
        self,
        project: local_v1.ProjectV1,
        *,
        idempotency_key: str,
    ) -> CoreActivationV1:
        deadline = time.monotonic() + self._timeout
        generation, old_active = self._begin_activation()
        if old_active is not None:
            old_active.close()
        candidate: DesktopCoreActiveSessionV1 | None = None
        tunnel: CoreTunnelHandleV1 | None = None
        client: CoreControlClientV1 | None = None
        try:
            attachment = self._host_service.ensure_core(project.profile_id, deadline=deadline)
            self._remaining(deadline)
            if attachment.profile_id != project.profile_id:
                raise _bridge_error(
                    "core_host_identity_mismatch",
                    "The attached Core host belongs to another remote profile.",
                )
            session_id = secrets.token_urlsafe(24)
            tunnel = self._tunnel_factory.open_tunnel(
                profile_id=attachment.profile_id,
                remote_port=attachment.remote_port,
                session_id=session_id,
                deadline=deadline,
            )
            self._remaining(deadline)
            create_request = map_project_create_v1(project)
            request_sha256 = _model_digest(create_request)
            mapping = self._persistence.load_mapping(project.project_id)
            operation: CoreProjectCreateOperationV1
            if mapping is None:
                operation = self._persistence.reserve_create(
                    CoreProjectCreateOperationV1(
                        local_project_id=project.project_id,
                        profile_id=project.profile_id,
                        core_host_identity=attachment.bearer_identity,
                        request_sha256=request_sha256,
                        idempotency_key=idempotency_key,
                    )
                )
                _ensure_create_operation(
                    operation,
                    project,
                    request_sha256,
                    idempotency_key=idempotency_key,
                    core_host_identity=attachment.bearer_identity,
                )
                connection, operation = self._bootstrap_connection(
                    project=project,
                    request=create_request,
                    operation=operation,
                    attachment=attachment,
                    tunnel=tunnel,
                    deadline=deadline,
                )
            else:
                _ensure_mapping_request(
                    mapping,
                    project,
                    request_sha256,
                    core_host_identity=attachment.bearer_identity,
                )
                operation = CoreProjectCreateOperationV1(
                    local_project_id=project.project_id,
                    profile_id=project.profile_id,
                    core_host_identity=attachment.bearer_identity,
                    request_sha256=request_sha256,
                    idempotency_key=idempotency_key,
                    core_project_id=mapping.core_project_id,
                )
                connection = CoreTunnelConnectionV1(
                    endpoint=tunnel.endpoint,
                    bearer_token=attachment.bearer_token,
                    project_id=mapping.core_project_id,
                    session_id=tunnel.session_id,
                )

            client = self._new_client(connection, deadline)
            client.version()
            self._remaining(deadline)
            capabilities = client.capabilities(create_request.spec.execution_mode)
            self._remaining(deadline)
            core_project = client.get_project()
            self._remaining(deadline)
            _ensure_project_identity(core_project, create_request)
            if mapping is not None:
                _ensure_mapping_snapshots(mapping, core_project)
            if isinstance(create_request.workspace, core_v1.ImportedWorkspaceSpecV1) and (
                core_project.workspace_publication is None
            ):
                core_project, operation = self._publish_imported_workspace(
                    client=client,
                    local_project=project,
                    core_project=core_project,
                    operation=operation,
                    deadline=deadline,
                )
                self._remaining(deadline)
            self._ensure_project_ready(core_project, capabilities)
            revision_head = client.revision_head()
            self._remaining(deadline)
            if revision_head.active_revision != core_project.active_revision:
                raise _bridge_error(
                    "core_project_revision_mismatch",
                    "Core project and revision head disagree.",
                )
            validation = self._validate_current(
                client,
                core_project,
                capabilities,
                idempotency_key=_derived_key(idempotency_key, "validate"),
            )
            self._remaining(deadline)
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
            )
            if mapping is None:
                self._persistence.commit_mapping(operation, completed_mapping)
            self._remaining(deadline)
            candidate = DesktopCoreActiveSessionV1(
                generation=generation,
                local_project_id=project.project_id,
                profile_id=project.profile_id,
                attachment=attachment,
                tunnel=tunnel,
                client=client,
                project=core_project,
                capabilities=capabilities,
                revision_head=revision_head,
            )
            self._publish_activation(candidate)
            return CoreActivationV1(
                local_project_id=project.project_id,
                core_project=core_project,
                capabilities=capabilities,
                revision_head=revision_head,
                validation=validation,
            )
        except BaseException:
            if candidate is not None:
                candidate.close()
            else:
                if client is not None:
                    client.close()
                if tunnel is not None:
                    tunnel.close()
            raise

    def capabilities(self, local_project_id: str) -> core_v1.CapabilitiesResponseV1:
        def call(session: DesktopCoreActiveSessionV1) -> core_v1.CapabilitiesResponseV1:
            return session.client.capabilities(session.project.spec.execution_mode)

        return self._invoke(local_project_id, call)

    def validate_project(
        self, local_project_id: str, *, idempotency_key: str
    ) -> core_v1.ProjectValidationResponseV1:
        def call(session: DesktopCoreActiveSessionV1) -> core_v1.ProjectValidationResponseV1:
            project, capabilities = self._refresh_authority(session)
            return self._validate_current(
                session.client,
                project,
                capabilities,
                idempotency_key=idempotency_key,
            )

        return self._invoke(local_project_id, call)

    def create_run(self, local_project_id: str, *, idempotency_key: str) -> core_v1.RunV1:
        def call(session: DesktopCoreActiveSessionV1) -> core_v1.RunV1:
            project, capabilities = self._refresh_authority(session)
            head = session.client.revision_head()
            if head.active_revision != project.active_revision:
                raise _bridge_error(
                    "core_project_revision_mismatch",
                    "Core project and revision head disagree.",
                )
            validation = self._validate_current(
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
            return session.client.create_run(request, idempotency_key=idempotency_key)

        return self._invoke(local_project_id, call)

    def list_runs(self, **kwargs: Any) -> core_v1.RunPageV1:
        return self._invoke_active(lambda session: session.client.list_runs(**kwargs))

    def get_run(self, run_id: str) -> core_v1.RunV1:
        return self._invoke_active(
            lambda session: session.client.get_run(run_id, project_id=session.project.id)
        )

    def delete_run(self, run_id: str, *, if_match: str) -> None:
        return self._invoke_active(
            lambda session: session.client.delete_run(
                run_id,
                project_id=session.project.id,
                if_match=if_match,
                idempotency_key=_derived_key(run_id, f"delete-{if_match}"),
            )
        )

    def cancel_run(self, run_id: str, *, if_match: str, idempotency_key: str) -> core_v1.RunV1:
        def call(session: DesktopCoreActiveSessionV1) -> core_v1.RunV1:
            session.client.get_run(run_id, project_id=session.project.id)
            return session.client.cancel_run(
                run_id,
                core_v1.RunCancelRequestV1(reason=core_v1.RunCancelReason.USER_REQUESTED),
                project_id=session.project.id,
                if_match=if_match,
                idempotency_key=idempotency_key,
            )

        return self._invoke_active(call)

    def retry_run(self, run_id: str, *, if_match: str, idempotency_key: str) -> core_v1.RunV1:
        def call(session: DesktopCoreActiveSessionV1) -> core_v1.RunV1:
            run = session.client.get_run(run_id, project_id=session.project.id)
            if run.current_attempt_id is None:
                raise _bridge_error(
                    "run_retry_not_ready",
                    "Core has no terminal run attempt to retry.",
                    status=409,
                )
            return session.client.retry_run(
                run_id,
                core_v1.RunRetryRequestV1(terminal_attempt_id=run.current_attempt_id),
                project_id=session.project.id,
                if_match=if_match,
                idempotency_key=idempotency_key,
            )

        return self._invoke_active(call)

    def run_timeline(self, run_id: str, **kwargs: Any) -> core_v1.RunTimelinePageV1:
        return self._invoke_active(
            lambda session: session.client.run_timeline(
                run_id, project_id=session.project.id, **kwargs
            )
        )

    def run_logs(self, run_id: str, **kwargs: Any) -> core_v1.LogPageV1:
        return self._invoke_active(
            lambda session: session.client.run_logs(
                run_id, project_id=session.project.id, **kwargs
            )
        )

    def run_context(self, run_id: str) -> core_v1.RunContextV1:
        return self._invoke_active(
            lambda session: session.client.run_context(run_id, project_id=session.project.id)
        )

    def run_artifacts(self, run_id: str, **kwargs: Any) -> core_v1.ArtifactPageV1:
        return self._invoke_active(
            lambda session: session.client.run_artifacts(
                run_id, project_id=session.project.id, **kwargs
            )
        )

    def get_artifact(self, artifact_id: str) -> core_v1.ArtifactSummaryV1:
        return self._invoke_active(
            lambda session: session.client.get_artifact(artifact_id, project_id=session.project.id)
        )

    def artifact_content(self, artifact_id: str) -> core_v1.ArtifactContentV1:
        return self._invoke_active(
            lambda session: session.client.artifact_content(
                artifact_id, project_id=session.project.id
            )
        )

    def artifact_diff(
        self, artifact_id: str, *, previous_artifact_id: str | None = None
    ) -> core_v1.ArtifactDiffV1:
        return self._invoke_active(
            lambda session: session.client.artifact_diff(
                artifact_id,
                project_id=session.project.id,
                previous_artifact_id=previous_artifact_id,
            )
        )

    def list_services(self, **kwargs: Any) -> core_v1.ServicePageV1:
        return self._invoke_active(lambda session: session.client.list_services(**kwargs))

    def get_service(self, service_id: str) -> core_v1.ServiceSummaryV1:
        return self._invoke_active(lambda session: session.client.get_service(service_id))

    def restart_service(
        self,
        service_id: str,
        *,
        if_match: str,
        idempotency_key: str,
    ) -> core_v1.OperationV1:
        return self._invoke_active(
            lambda session: session.client.restart_service(
                service_id,
                core_v1.ServiceRestartRequestV1(reason="Requested from OpenEvo Desktop."),
                if_match=if_match,
                idempotency_key=idempotency_key,
            )
        )

    def service_logs(self, service_id: str, **kwargs: Any) -> core_v1.LogPageV1:
        return self._invoke_active(
            lambda session: session.client.service_logs(service_id, **kwargs)
        )

    def get_operation(self, operation_id: str) -> core_v1.OperationV1:
        return self._invoke_active(lambda session: session.client.get_operation(operation_id))

    def cancel_operation(
        self,
        operation_id: str,
        request: core_v1.OperationCancelRequestV1,
        *,
        if_match: str,
        idempotency_key: str,
    ) -> core_v1.OperationV1:
        return self._invoke_active(
            lambda session: session.client.cancel_operation(
                operation_id,
                request,
                if_match=if_match,
                idempotency_key=idempotency_key,
            )
        )

    def logs_by_ref(self, logs_ref: str, **kwargs: Any) -> core_v1.ReferencedLogPageV1:
        return self._invoke_active(lambda session: session.client.logs_by_ref(logs_ref, **kwargs))

    def create_diagnostic(
        self, request: core_v1.DiagnosticsRequestV1, *, idempotency_key: str
    ) -> core_v1.DiagnosticV1:
        return self._invoke_active(
            lambda session: session.client.create_diagnostic(
                request, idempotency_key=idempotency_key
            )
        )

    def get_diagnostic(self, diagnostic_id: str) -> core_v1.DiagnosticV1:
        return self._invoke_active(lambda session: session.client.get_diagnostic(diagnostic_id))

    def delete_diagnostic(
        self, diagnostic_id: str, *, if_match: str, idempotency_key: str
    ) -> None:
        return self._invoke_active(
            lambda session: session.client.delete_diagnostic(
                diagnostic_id,
                if_match=if_match,
                idempotency_key=idempotency_key,
            )
        )

    def cache_cleanup(
        self, request: core_v1.CacheCleanupRequestV1, *, idempotency_key: str
    ) -> core_v1.OperationV1:
        return self._invoke_active(
            lambda session: session.client.cache_cleanup(request, idempotency_key=idempotency_key)
        )

    def events(self, *, last_event_id: str | None = None):
        session, generation = self._session(None)
        return _BridgeEventContext(self, session, generation, last_event_id)

    def _begin_activation(self) -> tuple[int, DesktopCoreActiveSessionV1 | None]:
        with self._lock:
            if self._closed:
                raise _bridge_error(
                    "desktop_core_bridge_closed",
                    "The Desktop Core bridge is closed.",
                )
            self._generation += 1
            generation = self._generation
            old_active = self._active
            self._active = None
            return generation, old_active

    def _publish_activation(self, candidate: DesktopCoreActiveSessionV1) -> None:
        with self._lock:
            if self._closed or candidate.generation != self._generation:
                raise _bridge_error(
                    "active_project_session_superseded",
                    "A newer active project session superseded this result.",
                    retryable=True,
                )
            self._active = candidate

    def _bootstrap_connection(
        self,
        *,
        project: local_v1.ProjectV1,
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
        if operation.core_project_id is not None:
            return bootstrap_connection.bind(operation.core_project_id), operation
        bootstrap = CoreProjectBootstrapClientV1(
            bootstrap_connection,
            transport=self._new_transport(),
            timeout=self._remaining(deadline),
        )
        try:
            bootstrap.version()
            self._remaining(deadline)
            bootstrap.capabilities(request.spec.execution_mode)
            self._remaining(deadline)
            result = bootstrap.create_project(
                request,
                idempotency_key=operation.idempotency_key,
            )
            self._remaining(deadline)
            operation = replace(operation, core_project_id=result.project.id)
            self._persistence.update_create(operation)
            return result.connection, operation
        finally:
            bootstrap.close()

    def _publish_imported_workspace(
        self,
        *,
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
        if operation.workspace_upload_id is None:
            upload = client.create_workspace_upload(
                core_v1.WorkspaceUploadCreateV1(
                    project_snapshot=core_project.current_project_snapshot,
                    archive=core_project.workspace.archive,
                    base_workspace_snapshot=core_project.current_workspace_snapshot,
                ),
                if_match=core_project.etag,
                idempotency_key=_derived_key(operation.idempotency_key, "upload-create"),
            )
            self._remaining(deadline)
            operation = replace(operation, workspace_upload_id=upload.id)
            self._persistence.update_create(operation)
        else:
            upload = client.get_workspace_upload(operation.workspace_upload_id)
            self._remaining(deadline)

        with self._archive_source.open_archive(import_ref) as stream:
            digest = hashlib.sha256()
            offset = 0
            upload_offset = upload.accepted_offset
            while offset < import_ref.byte_size:
                self._remaining(deadline)
                chunk = _read_archive_chunk(
                    stream,
                    min(WORKSPACE_CHUNK_BYTES, import_ref.byte_size - offset),
                )
                self._remaining(deadline)
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
                    upload = client.put_workspace_upload_chunk(
                        upload.id,
                        core_v1.WorkspaceUploadChunkV1(
                            offset=offset,
                            byte_length=len(chunk),
                            content_base64=base64.b64encode(chunk).decode("ascii"),
                            content_sha256=hashlib.sha256(chunk).hexdigest(),
                        ),
                        if_match=upload.etag,
                        idempotency_key=_derived_key(
                            operation.idempotency_key, f"upload-chunk-{offset}"
                        ),
                    )
                    upload_offset = upload.accepted_offset
                offset = next_offset
            if stream.read(1):
                raise _bridge_error(
                    "workspace_archive_mismatch",
                    "The adopted workspace archive exceeds its declared size.",
                    status=422,
                )
        if digest.hexdigest() != import_ref.content_sha256:
            raise _bridge_error(
                "workspace_archive_mismatch",
                "The adopted workspace archive digest changed.",
                status=422,
            )
        finalized = client.finalize_workspace_upload(
            upload.id,
            core_v1.WorkspaceUploadFinalizeV1(content_sha256=import_ref.content_sha256),
            if_match=upload.etag,
            if_project_match=upload.project_etag,
            idempotency_key=_derived_key(operation.idempotency_key, "upload-finalize"),
        )
        self._remaining(deadline)
        return finalized.project, operation

    def _new_client(
        self, connection: CoreTunnelConnectionV1, deadline: float
    ) -> CoreControlClientV1:
        return CoreControlClientV1(
            connection,
            transport=self._new_transport(),
            timeout=self._remaining(deadline),
        )

    def _new_transport(self) -> httpx.BaseTransport | None:
        if self._transport_factory is None:
            return None
        return self._transport_factory()

    def _refresh_authority(
        self, session: DesktopCoreActiveSessionV1
    ) -> tuple[core_v1.ProjectV1, core_v1.CapabilitiesResponseV1]:
        capabilities = session.client.capabilities(session.project.spec.execution_mode)
        project = session.client.get_project()
        self._ensure_project_ready(project, capabilities)
        return project, capabilities

    @staticmethod
    def _validate_current(
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
        return client.validate_project(
            core_v1.ProjectValidationRequestV1(
                project_snapshot=project.current_project_snapshot,
                workspace_snapshot=workspace_snapshot,
                expected_registry_digest=capabilities.registry_digest,
            ),
            idempotency_key=idempotency_key,
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

    def _session(self, local_project_id: str | None) -> tuple[DesktopCoreActiveSessionV1, int]:
        with self._lock:
            if self._closed:
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
            if local_project_id is not None and session.local_project_id != local_project_id:
                raise _bridge_error(
                    "active_project_mismatch",
                    "The requested resource does not belong to the active local project.",
                    status=409,
                )
            return session, self._generation

    def _invoke(
        self,
        local_project_id: str,
        call: Callable[[DesktopCoreActiveSessionV1], _ResponseT],
    ) -> _ResponseT:
        session, generation = self._session(local_project_id)
        result = call(session)
        self._ensure_generation(session, generation)
        return result

    def _invoke_active(
        self, call: Callable[[DesktopCoreActiveSessionV1], _ResponseT]
    ) -> _ResponseT:
        session, generation = self._session(None)
        result = call(session)
        self._ensure_generation(session, generation)
        return result

    def _ensure_generation(self, session: DesktopCoreActiveSessionV1, generation: int) -> None:
        with self._lock:
            if self._closed or self._generation != generation or self._active is not session:
                raise _bridge_error(
                    "active_project_session_superseded",
                    "A newer active project session superseded this result.",
                    retryable=True,
                )

    @staticmethod
    def _remaining(deadline: float) -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _bridge_error(
                "desktop_core_bridge_deadline_exceeded",
                "The Desktop Core bridge operation deadline expired.",
                retryable=True,
            )
        return remaining


class _BridgeEventContext:
    def __init__(
        self,
        bridge: DesktopCoreBridgeV1,
        session: DesktopCoreActiveSessionV1,
        generation: int,
        last_event_id: str | None,
    ) -> None:
        self._bridge = bridge
        self._session = session
        self._generation = generation
        self._last_event_id = last_event_id
        self._context = None

    def __enter__(self):
        self._bridge._ensure_generation(self._session, self._generation)
        self._context = self._session.client.events(last_event_id=self._last_event_id)
        stream = self._context.__enter__()
        self._bridge._ensure_generation(self._session, self._generation)
        return stream

    def __exit__(self, *exc: object) -> None:
        if self._context is not None:
            self._context.__exit__(*exc)


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


def _derived_key(base: str, purpose: str) -> str:
    digest = hashlib.sha256(f"{base}\0{purpose}".encode()).hexdigest()
    return f"desktop-core-{digest}"


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
    if (
        operation.local_project_id != project.project_id
        or operation.profile_id != project.profile_id
        or operation.core_host_identity != core_host_identity
        or operation.request_sha256 != request_sha256
        or operation.idempotency_key != idempotency_key
    ):
        raise _bridge_error(
            "core_project_create_replay_mismatch",
            "The durable Core project create operation does not match this request.",
            status=409,
        )


def _ensure_mapping_request(
    mapping: CoreProjectMappingV1,
    project: local_v1.ProjectV1,
    request_sha256: str,
    *,
    core_host_identity: str,
) -> None:
    if (
        mapping.local_project_id != project.project_id
        or mapping.profile_id != project.profile_id
        or mapping.core_host_identity != core_host_identity
        or mapping.request_sha256 != request_sha256
    ):
        raise _bridge_error(
            "core_project_mapping_mismatch",
            "The durable Core project mapping does not match the saved local project.",
            status=409,
        )


def _ensure_project_identity(project: core_v1.ProjectV1, request: core_v1.ProjectCreateV1) -> None:
    if any(
        (
            project.name != request.name,
            project.description != request.description,
            project.spec != request.spec,
            project.task != request.task,
            project.workspace != request.workspace,
            project.execution_mode is not request.spec.execution_mode,
            project.workspace_kind.value != request.workspace.kind,
        )
    ):
        raise _bridge_error(
            "core_project_identity_mismatch",
            "The Core project does not match the saved local project.",
            status=409,
        )


def _ensure_mapping_snapshots(mapping: CoreProjectMappingV1, project: core_v1.ProjectV1) -> None:
    if (
        mapping.core_project_id != project.id
        or mapping.project_snapshot != project.current_project_snapshot
        or mapping.task_snapshot != project.current_task_snapshot
        or mapping.workspace_snapshot != project.current_workspace_snapshot
        or mapping.registry_digest != project.registry_digest
    ):
        raise _bridge_error(
            "core_project_mapping_mismatch",
            "The Core project identity or immutable snapshots changed outside Desktop authority.",
            status=409,
        )


def _mapping_from_project(
    local_project: local_v1.ProjectV1,
    request_sha256: str,
    project: core_v1.ProjectV1,
    capabilities: core_v1.CapabilitiesResponseV1,
    *,
    core_host_identity: str,
) -> CoreProjectMappingV1:
    workspace_snapshot = project.current_workspace_snapshot
    if workspace_snapshot is None:
        raise _bridge_error(
            "core_project_not_ready",
            "Core has not published the project workspace snapshot.",
        )
    return CoreProjectMappingV1(
        local_project_id=local_project.project_id,
        profile_id=local_project.profile_id,
        core_host_identity=core_host_identity,
        core_project_id=project.id,
        request_sha256=request_sha256,
        project_snapshot=project.current_project_snapshot,
        task_snapshot=project.current_task_snapshot,
        workspace_snapshot=workspace_snapshot,
        registry_digest=capabilities.registry_digest,
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


__all__ = (
    "CoreActivationV1",
    "CoreHostAttachmentV1",
    "CoreHostService",
    "CoreProjectCreateOperationV1",
    "CoreProjectMappingV1",
    "CoreTunnelFactory",
    "CoreTunnelHandleV1",
    "DesktopCoreActiveSessionV1",
    "DesktopCoreBridgeErrorV1",
    "DesktopCoreBridgePersistence",
    "DesktopCoreBridgeV1",
    "WorkspaceArchiveSource",
    "map_project_create_v1",
)
