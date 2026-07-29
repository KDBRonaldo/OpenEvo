from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from contextlib import contextmanager
import hashlib
from pathlib import Path
from threading import Event
from types import SimpleNamespace
import time

from fastapi.testclient import TestClient
import pytest

from desktop.sidecar.contracts.v1 import WorkspaceImportRefV1
from desktop.sidecar.contracts.v2 import models as local_v2
from desktop.sidecar.core_bridge_v2 import DesktopCoreBridgeErrorV2
from desktop.sidecar.event_broker_v2 import DesktopEventBrokerV2
from desktop.sidecar.provider_store_v2 import (
    DesktopProviderStoreV2,
    LifecycleNativeWorkspacePrepareRequestV2,
    LifecycleOperationAdvanceV2,
    LifecycleOperationReservationV2,
    LifecycleProjectActivateRequestV2,
    LifecycleProjectCreateRequestV2,
)
from desktop.sidecar.release_app import create_release_desktop_local_api_v2_app
from desktop.sidecar.release_provider_v2 import DesktopReleaseProviderV2
from desktop.sidecar.workspace_identity import (
    native_import_id_for_action,
    ownership_for_native_import,
    project_id_for_native_import,
)
from desktop.sidecar.workspace_imports import (
    WorkspaceImportNotFoundError,
    WorkspaceImportStore,
)
from desktop.sidecar.native_workspace import NativeWorkspaceArchiveCancelled
import desktop.sidecar.release_provider_v2 as release_provider_module
from openevo.backend.contracts.v2 import models as core_v2
from tests.openevo.sidecar.test_core_bridge_v2 import _capabilities, _task
from tests.openevo.sidecar.test_core_bridge_store_v2 import _mapping
from tests.openevo.sidecar.test_core_client_v2 import _config, _head, _project
from tests.openevo.sidecar.test_release_local_api_v2 import (
    SESSION,
    SOURCE_COMMIT,
    _Catalog,
    _CoreConnector,
    _Lifecycle,
    _wait_lifecycle_operation,
)


NOW = datetime(2026, 7, 23, 11, 0, tzinfo=timezone.utc)


class _RoutingBridge:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.active_activation: object | None = None
        self.fail_reads = False
        self.project: core_v2.ProjectV2 | None = None
        self.upload: core_v2.WorkspaceUploadSessionV2 | None = None
        self.mapping = None
        self.activation_started: Event | None = None
        self.activation_release: Event | None = None

    def activate_project(
        self,
        desktop_project_id: str,
        request: local_v2.ProjectCreateV2,
        *,
        idempotency_key: str,
    ) -> object:
        self.calls.append(
            (
                "activate_project",
                desktop_project_id,
                request.profile_id,
                request.profile_connection_generation,
                idempotency_key,
            )
        )
        if self.activation_started is not None and self.activation_release is not None:
            self.activation_started.set()
            assert self.activation_release.wait(16)
        if self.project is None:
            if request.config.workspace.kind == "native_folder_snapshot":
                self.project = core_v2.ProjectV2(
                    project_id="project-1",
                    display_name=request.display_name,
                    config=request.config,
                    project_config_sha256=core_v2.project_config_sha256_for(request.config),
                    active_project_head=None,
                    admission_etag=None,
                    state="not_ready",
                    created_at="2026-07-23T11:00:00Z",
                    updated_at="2026-07-23T11:00:00Z",
                    etag='"' + ("1" * 64) + '"',
                )
            else:
                self.project = _project()
        project = self.project
        activation = SimpleNamespace(
            desktop_project_id=desktop_project_id,
            profile_id=request.profile_id,
            profile_connection_generation=request.profile_connection_generation,
            project=project,
            capabilities=None,
            mapping=SimpleNamespace(core_project_id=project.project_id),
        )
        self.active_activation = activation
        base_mapping = _mapping(
            profile_connection_generation=request.profile_connection_generation,
            project=project,
        )
        self.mapping = replace(
            base_mapping,
            desktop_project_id=desktop_project_id,
            profile_id=request.profile_id,
        )
        return activation

    def load_mapping_by_core_project_id(self, core_project_id: str):
        if self.mapping is None or self.mapping.core_project_id != core_project_id:
            return None
        return self.mapping

    def get_project(
        self,
        desktop_project_id: str,
        profile_connection_generation: int,
    ) -> core_v2.ProjectV2:
        self.calls.append(("get_project", desktop_project_id, profile_connection_generation))
        if self.fail_reads:
            raise DesktopCoreBridgeErrorV2(
                503,
                local_v2.DesktopErrorV2(
                    code="core_temporarily_unavailable",
                    summary="The remote OpenEvo Daemon is temporarily unavailable.",
                    retryable=True,
                    action="retry",
                    affected_resource_id="project-1",
                ),
            )
        return self.project or _project()

    def list_projects(
        self,
        desktop_project_id: str,
        profile_connection_generation: int,
        *,
        limit: int,
        after: str | None,
    ) -> core_v2.ProjectPageV2:
        self.calls.append(
            ("list_projects", desktop_project_id, profile_connection_generation, limit, after)
        )
        return core_v2.ProjectPageV2(
            items=[self.project or _project()], has_more=False, next_cursor=None
        )

    def create_workspace_upload(
        self,
        desktop_project_id: str,
        profile_connection_generation: int,
        request: core_v2.WorkspaceUploadCreateV2,
        *,
        if_match: str,
        idempotency_key: str,
    ) -> core_v2.WorkspaceUploadSessionV2:
        self.calls.append(
            (
                "create_workspace_upload",
                desktop_project_id,
                profile_connection_generation,
                request,
                if_match,
                idempotency_key,
            )
        )
        if self.upload is None:
            self.upload = core_v2.WorkspaceUploadSessionV2(
                upload_id="workspace-upload-1",
                project_id="project-1",
                state="open",
                expected_project_head_id=request.expected_project_head_id,
                expected_project_head_manifest_sha256=(
                    request.expected_project_head_manifest_sha256
                ),
                expected_project_config_sha256=request.expected_project_config_sha256,
                archive=request.archive,
                chunk_byte_size=request.chunk_byte_size,
                chunk_count=request.chunk_count,
                next_chunk_index=0,
                accepted_byte_size=0,
                workspace_snapshot=None,
                created_at="2026-07-23T11:00:00Z",
                updated_at="2026-07-23T11:00:00Z",
                etag='"' + ("2" * 64) + '"',
            )
        return self.upload

    def put_workspace_upload_chunk(
        self,
        desktop_project_id: str,
        profile_connection_generation: int,
        upload_id: str,
        chunk_index: int,
        chunk: bytes,
        *,
        chunk_sha256: str,
        if_match: str,
        idempotency_key: str,
    ) -> core_v2.WorkspaceUploadSessionV2:
        self.calls.append(
            (
                "put_workspace_upload_chunk",
                desktop_project_id,
                profile_connection_generation,
                upload_id,
                chunk_index,
                chunk_sha256,
                len(chunk),
                if_match,
                idempotency_key,
            )
        )
        current = self.upload
        assert current is not None and current.upload_id == upload_id
        assert chunk_index == current.next_chunk_index
        assert hashlib.sha256(chunk).hexdigest() == chunk_sha256
        self.upload = core_v2.WorkspaceUploadSessionV2(
            **current.model_dump(
                exclude={
                    "next_chunk_index",
                    "accepted_byte_size",
                    "updated_at",
                    "etag",
                }
            ),
            next_chunk_index=chunk_index + 1,
            accepted_byte_size=current.accepted_byte_size + len(chunk),
            updated_at="2026-07-23T11:00:01Z",
            etag='"' + ("3" * 64) + '"',
        )
        return self.upload

    def finalize_workspace_upload(
        self,
        desktop_project_id: str,
        profile_connection_generation: int,
        upload_id: str,
        request: core_v2.WorkspaceUploadFinalizeV2,
        *,
        if_match: str,
        idempotency_key: str,
    ) -> core_v2.WorkspaceUploadSessionV2:
        self.calls.append(
            (
                "finalize_workspace_upload",
                desktop_project_id,
                profile_connection_generation,
                upload_id,
                request,
                if_match,
                idempotency_key,
            )
        )
        current = self.upload
        assert current is not None and current.next_chunk_index == current.chunk_count
        snapshot = core_v2.WorkspaceSnapshotRefV2(
            workspace_snapshot_id="workspace-native-1",
            project_id="project-1",
            manifest_sha256="4" * 64,
            entry_count=current.archive.entry_count,
            byte_size=current.archive.extracted_byte_size,
        )
        self.upload = core_v2.WorkspaceUploadSessionV2(
            **current.model_dump(exclude={"state", "workspace_snapshot", "updated_at", "etag"}),
            state="finalized",
            workspace_snapshot=snapshot,
            updated_at="2026-07-23T11:00:02Z",
            etag='"' + ("5" * 64) + '"',
        )
        project = self.project
        assert project is not None
        head_payload = _head().model_dump(mode="json")
        head_payload["workspace_snapshot"] = snapshot.model_dump(mode="json")
        self.project = core_v2.ProjectV2(
            project_id=project.project_id,
            display_name=project.display_name,
            config=project.config,
            project_config_sha256=project.project_config_sha256,
            active_project_head=core_v2.ProjectHeadRefV2.model_validate(head_payload),
            admission_etag='"' + ("6" * 64) + '"',
            state="ready",
            created_at=project.created_at,
            updated_at="2026-07-23T11:00:02Z",
            etag='"' + ("7" * 64) + '"',
        )
        return self.upload

    def capabilities(
        self,
        desktop_project_id: str,
        profile_connection_generation: int,
        execution_mode: core_v2.ExecutionModeV2,
    ) -> core_v2.CapabilitiesResponseV2:
        self.calls.append(
            (
                "capabilities",
                desktop_project_id,
                profile_connection_generation,
                execution_mode,
            )
        )
        return core_v2.CapabilitiesResponseV2.model_validate(_capabilities())

    def validate_project(
        self,
        desktop_project_id: str,
        profile_connection_generation: int,
        request: core_v2.ProjectValidationRequestV2,
        *,
        idempotency_key: str,
    ) -> core_v2.ProjectValidationResponseV2:
        self.calls.append(
            (
                "validate_project",
                desktop_project_id,
                profile_connection_generation,
                request,
                idempotency_key,
            )
        )
        return core_v2.ProjectValidationResponseV2(
            project_id="project-1",
            valid=True,
            registry_sha256="a" * 64,
            checks=[
                core_v2.ProjectValidationCheckV2(
                    check_id="project-config-valid",
                    status="passed",
                    message="Project configuration is valid.",
                )
            ],
            validated_at="2026-07-23T11:00:00Z",
        )

    def update_project(
        self,
        desktop_project_id: str,
        profile_connection_generation: int,
        request: core_v2.ProjectUpdateV2,
        *,
        if_match: str,
        idempotency_key: str,
    ) -> core_v2.ProjectV2:
        self.calls.append(
            (
                "update_project",
                desktop_project_id,
                profile_connection_generation,
                request,
                if_match,
                idempotency_key,
            )
        )
        return _project()

    def submit_task(
        self,
        desktop_project_id: str,
        profile_connection_generation: int,
        request: core_v2.TaskSubmitRequestV2,
        *,
        idempotency_key: str,
    ) -> core_v2.TaskV2:
        self.calls.append(
            (
                "submit_task",
                desktop_project_id,
                profile_connection_generation,
                request,
                idempotency_key,
            )
        )
        return _task()

    def list_tasks(
        self,
        desktop_project_id: str,
        profile_connection_generation: int,
        *,
        limit: int,
        after: str | None,
    ) -> core_v2.TaskPageV2:
        self.calls.append(
            ("list_tasks", desktop_project_id, profile_connection_generation, limit, after)
        )
        return core_v2.TaskPageV2(items=[_task()], has_more=False, next_cursor=None)

    def get_task(
        self,
        desktop_project_id: str,
        profile_connection_generation: int,
        task_id: str,
    ) -> core_v2.TaskV2:
        self.calls.append(("get_task", desktop_project_id, profile_connection_generation, task_id))
        return _task()

    def append_task_attempt(
        self,
        desktop_project_id: str,
        profile_connection_generation: int,
        task_id: str,
        request: core_v2.AttemptAppendRequestV2,
        *,
        idempotency_key: str,
    ) -> core_v2.AttemptRefV2:
        self.calls.append(
            (
                "append_task_attempt",
                desktop_project_id,
                profile_connection_generation,
                task_id,
                request,
                idempotency_key,
            )
        )
        task = _task()
        return core_v2.AttemptRefV2(
            attempt_id="attempt-2",
            ordinal=2,
            task_id=task.task_id,
            task_admission_id=task.admission.task_admission_id,
            admission_sha256=task.admission.admission_sha256,
            project_id=task.project_id,
            predecessor_project_head_id=(task.admission.predecessor_project_head.project_head_id),
            created_at="2026-07-23T11:00:01Z",
        )

    def cancel_task_attempt(
        self,
        desktop_project_id: str,
        profile_connection_generation: int,
        task_id: str,
        attempt_id: str,
        request: core_v2.TaskActionRequestV2,
        *,
        if_match: str,
        idempotency_key: str,
    ) -> core_v2.OperationV2:
        self.calls.append(
            (
                "cancel_task_attempt",
                desktop_project_id,
                profile_connection_generation,
                task_id,
                attempt_id,
                request,
                if_match,
                idempotency_key,
            )
        )
        return _operation("operation-task-cancel", "attempt_cancel")

    def task_timeline(
        self,
        desktop_project_id: str,
        profile_connection_generation: int,
        task_id: str,
        *,
        limit: int,
        after: str | None,
    ) -> core_v2.TimelinePageV2:
        self.calls.append(
            ("task_timeline", desktop_project_id, profile_connection_generation, task_id)
        )
        return core_v2.TimelinePageV2(items=[], has_more=False, next_cursor=None)

    def task_logs(
        self,
        desktop_project_id: str,
        profile_connection_generation: int,
        task_id: str,
        *,
        limit: int,
        after: str | None,
    ) -> core_v2.LogPageV2:
        self.calls.append(
            ("task_logs", desktop_project_id, profile_connection_generation, task_id)
        )
        return core_v2.LogPageV2(items=[], has_more=False, next_cursor=None)

    def task_context(
        self,
        desktop_project_id: str,
        profile_connection_generation: int,
        task_id: str,
    ) -> core_v2.TaskContextV2:
        self.calls.append(
            ("task_context", desktop_project_id, profile_connection_generation, task_id)
        )
        task = _task()
        return core_v2.TaskContextV2(
            task_id=task.task_id,
            task_admission_id=task.admission.task_admission_id,
            project_head=_head(),
            workspace_snapshot=_head().workspace_snapshot,
        )

    def task_artifacts(
        self,
        desktop_project_id: str,
        profile_connection_generation: int,
        task_id: str,
        *,
        limit: int,
        after: str | None,
    ) -> core_v2.ArtifactPageV2:
        self.calls.append(
            ("task_artifacts", desktop_project_id, profile_connection_generation, task_id)
        )
        return core_v2.ArtifactPageV2(
            items=[_artifact("artifact-1", "6")], has_more=False, next_cursor=None
        )

    def get_project_head(
        self,
        desktop_project_id: str,
        profile_connection_generation: int,
        project_head_id: str,
    ) -> core_v2.ProjectHeadRefV2:
        self.calls.append(
            (
                "get_project_head",
                desktop_project_id,
                profile_connection_generation,
                project_head_id,
            )
        )
        return _head()

    def list_project_heads(
        self,
        desktop_project_id: str,
        profile_connection_generation: int,
        *,
        limit: int,
        after: str | None,
    ) -> core_v2.ProjectHeadPageV2:
        self.calls.append(
            ("list_project_heads", desktop_project_id, profile_connection_generation)
        )
        return core_v2.ProjectHeadPageV2(items=[_head()], has_more=False, next_cursor=None)

    def get_transition(
        self,
        desktop_project_id: str,
        profile_connection_generation: int,
        successor_transition_id: str,
    ) -> core_v2.SuccessorTransitionV2:
        self.calls.append(
            (
                "get_transition",
                desktop_project_id,
                profile_connection_generation,
                successor_transition_id,
            )
        )
        return _transition()

    def retry_transition(
        self,
        desktop_project_id: str,
        profile_connection_generation: int,
        successor_transition_id: str,
        request: core_v2.ActionRequestV2,
        *,
        idempotency_key: str,
    ) -> core_v2.OperationV2:
        self.calls.append(
            (
                "retry_transition",
                desktop_project_id,
                profile_connection_generation,
                successor_transition_id,
                request,
                idempotency_key,
            )
        )
        return _operation("operation-transition-retry", "transition_retry")

    def abandon_transition(
        self,
        desktop_project_id: str,
        profile_connection_generation: int,
        successor_transition_id: str,
        request: core_v2.ActionRequestV2,
        *,
        idempotency_key: str,
    ) -> core_v2.OperationV2:
        self.calls.append(
            (
                "abandon_transition",
                desktop_project_id,
                profile_connection_generation,
                successor_transition_id,
                request,
                idempotency_key,
            )
        )
        return _operation("operation-transition-abandon", "transition_abandon")

    def get_artifact(
        self,
        desktop_project_id: str,
        profile_connection_generation: int,
        artifact_id: str,
    ) -> core_v2.ArtifactV2:
        self.calls.append(
            ("get_artifact", desktop_project_id, profile_connection_generation, artifact_id)
        )
        return _artifact(artifact_id, "6" if artifact_id == "artifact-1" else "7")

    def artifact_content(
        self,
        desktop_project_id: str,
        profile_connection_generation: int,
        artifact_id: str,
    ) -> core_v2.ArtifactContentV2:
        self.calls.append(
            (
                "artifact_content",
                desktop_project_id,
                profile_connection_generation,
                artifact_id,
            )
        )
        artifact = _artifact(artifact_id, "6")
        return core_v2.ArtifactContentV2(
            artifact=artifact,
            media_type="text/markdown",
            content_sha256="9" * 64,
            byte_size=artifact.byte_size,
        )

    def list_services(
        self,
        desktop_project_id: str,
        profile_connection_generation: int,
        *,
        limit: int,
        after: str | None,
    ) -> core_v2.ServicePageV2:
        self.calls.append(("list_services", desktop_project_id, profile_connection_generation))
        return core_v2.ServicePageV2(items=[_service()], has_more=False, next_cursor=None)

    def get_service(
        self,
        desktop_project_id: str,
        profile_connection_generation: int,
        service_id: str,
    ) -> core_v2.ServiceV2:
        self.calls.append(
            ("get_service", desktop_project_id, profile_connection_generation, service_id)
        )
        return _service()

    def restart_service(
        self,
        desktop_project_id: str,
        profile_connection_generation: int,
        service_id: str,
        request: core_v2.ActionRequestV2,
        *,
        if_match: str,
        idempotency_key: str,
    ) -> core_v2.OperationV2:
        self.calls.append(
            (
                "restart_service",
                desktop_project_id,
                profile_connection_generation,
                service_id,
                request,
                if_match,
                idempotency_key,
            )
        )
        return _operation("operation-service-restart", "service_restart")

    def service_logs(
        self,
        desktop_project_id: str,
        profile_connection_generation: int,
        service_id: str,
        *,
        limit: int,
        after: str | None,
    ) -> core_v2.LogPageV2:
        self.calls.append(
            (
                "service_logs",
                desktop_project_id,
                profile_connection_generation,
                service_id,
                limit,
                after,
            )
        )
        return core_v2.LogPageV2(
            items=[
                core_v2.LogEntryV2(
                    sequence=1,
                    occurred_at="2026-07-23T11:00:00Z",
                    stream="stdout",
                    message="daemon ready",
                )
            ],
            has_more=False,
            next_cursor=None,
        )

    def get_operation(
        self,
        desktop_project_id: str,
        profile_connection_generation: int,
        operation_id: str,
    ) -> core_v2.OperationV2:
        self.calls.append(
            (
                "get_operation",
                desktop_project_id,
                profile_connection_generation,
                operation_id,
            )
        )
        return _operation(operation_id, "service_restart")

    def cancel_operation(
        self,
        desktop_project_id: str,
        profile_connection_generation: int,
        operation_id: str,
        *,
        if_match: str,
        idempotency_key: str,
    ) -> core_v2.OperationV2:
        self.calls.append(
            (
                "cancel_operation",
                desktop_project_id,
                profile_connection_generation,
                operation_id,
                if_match,
                idempotency_key,
            )
        )
        return _operation(operation_id, "service_restart")

    def cache_cleanup(
        self,
        desktop_project_id: str,
        profile_connection_generation: int,
        request: core_v2.CacheCleanupRequestV2,
        *,
        idempotency_key: str,
    ) -> core_v2.OperationV2:
        self.calls.append(
            (
                "cache_cleanup",
                desktop_project_id,
                profile_connection_generation,
                request,
                idempotency_key,
            )
        )
        return _operation("operation-cache-cleanup", "cache_cleanup")

    def create_diagnostic(
        self,
        desktop_project_id: str,
        profile_connection_generation: int,
        request: core_v2.DiagnosticRequestV2,
        *,
        idempotency_key: str,
    ) -> core_v2.DiagnosticV2:
        self.calls.append(
            (
                "create_diagnostic",
                desktop_project_id,
                profile_connection_generation,
                request,
                idempotency_key,
            )
        )
        return _diagnostic()

    def get_diagnostic(
        self,
        desktop_project_id: str,
        profile_connection_generation: int,
        diagnostic_id: str,
    ) -> core_v2.DiagnosticV2:
        self.calls.append(
            (
                "get_diagnostic",
                desktop_project_id,
                profile_connection_generation,
                diagnostic_id,
            )
        )
        return _diagnostic()

    def deactivate_project(
        self,
        desktop_project_id: str,
        profile_connection_generation: int,
    ) -> None:
        self.calls.append(
            ("deactivate_project", desktop_project_id, profile_connection_generation)
        )
        self.active_activation = None

    def close(self) -> None:
        self.active_activation = None


def _artifact(artifact_id: str, digest_character: str) -> core_v2.ArtifactV2:
    return core_v2.ArtifactV2(
        artifact_id=artifact_id,
        project_id="project-1",
        artifact_type="text_memory",
        manifest_sha256=digest_character * 64,
        byte_size=16,
        created_at="2026-07-23T11:00:00Z",
    )


def _service() -> core_v2.ServiceV2:
    return core_v2.ServiceV2(
        service_id="service-daemon",
        kind="daemon",
        status="ready",
        updated_at="2026-07-23T11:00:00Z",
        etag='"' + ("b" * 64) + '"',
    )


def _diagnostic() -> core_v2.DiagnosticV2:
    return core_v2.DiagnosticV2(
        diagnostic_id="diagnostic-1",
        scope="project",
        resource_id="project-1",
        status="ready",
        artifact_id="artifact-1",
        created_at="2026-07-23T11:00:00Z",
        updated_at="2026-07-23T11:00:00Z",
        etag='"' + ("c" * 64) + '"',
    )


def _operation(
    operation_id: str,
    kind: str,
) -> core_v2.OperationV2:
    return core_v2.OperationV2.model_validate(
        {
            "schema_version": "2",
            "operation_id": operation_id,
            "kind": kind,
            "status": "succeeded",
            "progress_completed": 1,
            "progress_total": 1,
            "error": None,
            "created_at": "2026-07-23T11:00:00Z",
            "updated_at": "2026-07-23T11:00:00Z",
            "etag": '"' + ("d" * 64) + '"',
        }
    )


def _transition() -> core_v2.SuccessorTransitionV2:
    task = _task()
    ref = core_v2.SuccessorTransitionRefV2(
        successor_transition_id="transition-1",
        project_id="project-1",
        kind="run_result",
        predecessor_project_head=_head(),
        expected_successor_generation=1,
        plan_sha256="e" * 64,
        task_admission=task.admission,
        accepted_attempt=task.attempts[0],
        successor_project_head=None,
    )
    return core_v2.SuccessorTransitionV2(
        transition=ref,
        state="pending",
        progress_completed=0,
        progress_total=1,
        error=None,
        created_at="2026-07-23T11:00:00Z",
        updated_at="2026-07-23T11:00:00Z",
    )


def _provider(
    tmp_path: Path,
    *,
    workspace_import_store: WorkspaceImportStore | None = None,
) -> tuple[
    DesktopReleaseProviderV2,
    DesktopProviderStoreV2,
    _Lifecycle,
    _RoutingBridge,
]:
    store = DesktopProviderStoreV2(tmp_path / "provider-v2", clock=lambda: NOW)
    lifecycle = _Lifecycle()
    bridge = _RoutingBridge()
    provider = DesktopReleaseProviderV2(
        store=store,
        catalog=_Catalog(),
        lifecycle=lifecycle,
        core_connector=_CoreConnector(),
        bridge=bridge,
        bridge_store=bridge,
        workspace_import_store=workspace_import_store,
        event_broker=DesktopEventBrokerV2(clock=lambda: NOW),
        build_version="0.1.10",
        source_commit=SOURCE_COMMIT,
        build_channel="release",
        instance_id="routing-instance-v2",
        clock=lambda: NOW,
        own_resources=False,
    )
    return provider, store, lifecycle, bridge


def _headers(**extra: str) -> dict[str, str]:
    return {"X-OpenEvo-Desktop-Session": SESSION, **extra}


def _connected_profile(client: TestClient) -> dict[str, object]:
    created = client.post(
        "/desktop/v2/profiles",
        headers=_headers(
            **{
                "X-OpenEvo-Resource-Generation": "1",
                "Idempotency-Key": "routing-create-profile-01",
            }
        ),
        json={
            "schema_version": "2",
            "display_name": "Lab GPU",
            "connection_authority": "system_openssh",
            "ssh_host_alias": "gpu-lab",
        },
    )
    assert created.status_code == 201, created.text
    profile = created.json()
    connected = client.post(
        f"/desktop/v2/profiles/{profile['profile_id']}/connect",
        headers=_headers(
            **{
                "X-OpenEvo-Resource-Generation": "1",
                "If-Match": str(profile["etag"]),
                "Idempotency-Key": "routing-connect-profile-01",
            }
        ),
        json={"schema_version": "2", "expected_connection_generation": 1},
    )
    assert connected.status_code == 202, connected.text
    assert _wait_lifecycle_operation(client, connected.json())["status"] == "succeeded"
    return client.get(f"/desktop/v2/profiles/{profile['profile_id']}", headers=_headers()).json()


def _project_create(profile: dict[str, object]) -> dict[str, object]:
    return {
        "schema_version": "2",
        "profile_id": profile["profile_id"],
        "profile_connection_generation": profile["connection_generation"],
        "display_name": "Project",
        "config": _config().model_dump(mode="json"),
    }


def test_project_create_and_read_use_only_generation_bound_core_v2(
    tmp_path: Path,
) -> None:
    provider, store, lifecycle, bridge = _provider(tmp_path)
    client = TestClient(
        create_release_desktop_local_api_v2_app(
            session_token=SESSION,
            provider=provider,
            close_on_shutdown=False,
        )
    )
    try:
        profile = _connected_profile(client)
        ssh_calls = list(lifecycle.calls)
        created = client.post(
            "/desktop/v2/projects",
            headers=_headers(
                **{
                    "X-OpenEvo-Resource-Generation": str(profile["connection_generation"]),
                    "Idempotency-Key": "routing-create-project-01",
                }
            ),
            json=_project_create(profile),
        )
        assert created.status_code == 202, created.text
        terminal = _wait_lifecycle_operation(client, created.json())
        assert terminal["status"] == "succeeded"
        assert terminal["result"]["project_id"] == "project-1"
        assert bridge.calls[0][0] == "activate_project"
        assert bridge.calls[0][2:] == (
            profile["profile_id"],
            profile["connection_generation"],
            "routing-create-project-01",
        )
        assert lifecycle.calls == ssh_calls

        read = client.get("/desktop/v2/projects/project-1", headers=_headers())
        assert read.status_code == 200, read.text
        assert read.json()["project_id"] == "project-1"
        assert bridge.calls[-1][0] == "get_project"
        assert lifecycle.calls == ssh_calls
    finally:
        client.close()
        provider.close()
        store.close()


def test_running_project_create_reconnects_and_resumes_after_sidecar_restart(
    tmp_path: Path,
) -> None:
    first_provider, first_store, _first_lifecycle, _first_bridge = _provider(tmp_path)
    first_client = TestClient(
        create_release_desktop_local_api_v2_app(
            session_token=SESSION,
            provider=first_provider,
            close_on_shutdown=False,
        )
    )
    profile = _connected_profile(first_client)
    first_client.close()
    first_provider.close()

    action_id = "routing-restart-project-create-0001"
    project_id = "desktop-project-restart-1"
    request = local_v2.ProjectCreateV2.model_validate(_project_create(profile))
    operation = first_store.reserve_lifecycle_operation(
        LifecycleOperationReservationV2(
            kind="project_create",
            resource={"resource_kind": "project", "resource_id": project_id},
            request=LifecycleProjectCreateRequestV2(
                request_kind="project_create",
                project_id=project_id,
                action_id=action_id,
                request=request,
                resource_generation=request.profile_connection_generation,
            ),
        ),
        idempotency_key="routing-restart-project-reservation-0001",
    )
    claimed = first_store.claim_next_lifecycle_operation()
    assert claimed is not None and claimed.operation.operation_id == operation.operation_id
    first_store.advance_lifecycle_operation(
        LifecycleOperationAdvanceV2(
            operation_id=operation.operation_id,
            expected_etag=claimed.operation.etag,
            phase="creating_remote_project",
            progress=local_v2.LifecycleProgressIndeterminateV2(kind="indeterminate"),
            cancellable=False,
        )
    )
    first_store.close()

    reopened = DesktopProviderStoreV2(tmp_path / "provider-v2", clock=lambda: NOW)
    recovered_profiles = reopened.reconcile_process_restart()
    assert len(recovered_profiles) == 1
    disconnected = recovered_profiles[0]
    lifecycle = _Lifecycle()
    bridge = _RoutingBridge()
    provider = DesktopReleaseProviderV2(
        store=reopened,
        catalog=_Catalog(),
        lifecycle=lifecycle,
        core_connector=_CoreConnector(),
        bridge=bridge,
        bridge_store=bridge,
        workspace_import_store=None,
        event_broker=DesktopEventBrokerV2(clock=lambda: NOW),
        build_version="0.1.10",
        source_commit=SOURCE_COMMIT,
        build_channel="release",
        instance_id="routing-restart-instance-v2",
        clock=lambda: NOW,
        own_resources=False,
    )
    client = TestClient(
        create_release_desktop_local_api_v2_app(
            session_token=SESSION,
            provider=provider,
            close_on_shutdown=False,
        )
    )
    try:
        terminal = _wait_lifecycle_operation(
            client,
            {"operation_id": operation.operation_id},
        )

        assert terminal["status"] == "succeeded"
        assert lifecycle.calls == [
            ("connect", disconnected.ssh_host_alias, disconnected.connection_generation + 1)
        ]
        assert [call[0] for call in bridge.calls] == ["activate_project"]
        assert bridge.calls[0][3] == disconnected.connection_generation + 1
        assert bridge.calls[0][4] == action_id
    finally:
        client.close()
        provider.close()
        reopened.close()


def test_running_project_activate_reconnects_and_resumes_after_sidecar_restart(
    tmp_path: Path,
) -> None:
    first_provider, first_store, _first_lifecycle, bridge = _provider(tmp_path)
    first_client = TestClient(
        create_release_desktop_local_api_v2_app(
            session_token=SESSION,
            provider=first_provider,
            close_on_shutdown=False,
        )
    )
    profile = _connected_profile(first_client)
    created = first_client.post(
        "/desktop/v2/projects",
        headers=_headers(
            **{
                "X-OpenEvo-Resource-Generation": str(profile["connection_generation"]),
                "Idempotency-Key": "routing-activate-restart-create-project-0001",
            }
        ),
        json=_project_create(profile),
    )
    assert created.status_code == 202, created.text
    assert _wait_lifecycle_operation(first_client, created.json())["status"] == "succeeded"
    project = bridge.project
    assert project is not None and project.active_project_head is not None
    first_client.close()
    first_provider.close()

    head = project.active_project_head
    operation = first_store.reserve_lifecycle_operation(
        LifecycleOperationReservationV2(
            kind="project_activate",
            resource={"resource_kind": "project", "resource_id": project.project_id},
            request=LifecycleProjectActivateRequestV2(
                request_kind="project_activate",
                project_id=project.project_id,
                request=local_v2.ProjectActionV2(
                    expected_project_head_id=head.project_head_id,
                    expected_project_head_manifest_sha256=head.manifest_sha256,
                ),
                resource_generation=head.generation,
                if_match=project.etag,
            ),
        ),
        idempotency_key="routing-activate-restart-reservation-0001",
    )
    claimed = first_store.claim_next_lifecycle_operation()
    assert claimed is not None and claimed.operation.operation_id == operation.operation_id
    first_store.close()

    reopened = DesktopProviderStoreV2(tmp_path / "provider-v2", clock=lambda: NOW)
    recovered_profiles = reopened.reconcile_process_restart()
    assert len(recovered_profiles) == 1
    disconnected = recovered_profiles[0]
    lifecycle = _Lifecycle()
    recovered_provider = DesktopReleaseProviderV2(
        store=reopened,
        catalog=_Catalog(),
        lifecycle=lifecycle,
        core_connector=_CoreConnector(),
        bridge=bridge,
        bridge_store=bridge,
        workspace_import_store=None,
        event_broker=DesktopEventBrokerV2(clock=lambda: NOW),
        build_version="0.1.10",
        source_commit=SOURCE_COMMIT,
        build_channel="release",
        instance_id="routing-activate-restart-instance-v2",
        clock=lambda: NOW,
        own_resources=False,
    )
    client = TestClient(
        create_release_desktop_local_api_v2_app(
            session_token=SESSION,
            provider=recovered_provider,
            close_on_shutdown=False,
        )
    )
    try:
        terminal = _wait_lifecycle_operation(
            client,
            {"operation_id": operation.operation_id},
        )

        assert terminal["status"] == "succeeded"
        assert terminal["result"]["project_id"] == project.project_id
        assert lifecycle.calls == [
            ("connect", disconnected.ssh_host_alias, disconnected.connection_generation + 1)
        ]
        assert bridge.active_activation is not None
        assert (
            bridge.active_activation.profile_connection_generation
            == disconnected.connection_generation + 1
        )
    finally:
        client.close()
        recovered_provider.close()
        reopened.close()


def test_project_create_returns_before_a_sixteen_second_bridge_activation(
    tmp_path: Path,
) -> None:
    provider, store, _lifecycle, bridge = _provider(tmp_path)
    client = TestClient(
        create_release_desktop_local_api_v2_app(
            session_token=SESSION,
            provider=provider,
            close_on_shutdown=False,
        )
    )
    bridge.activation_started = Event()
    bridge.activation_release = Event()
    try:
        profile = _connected_profile(client)
        headers = _headers(
            **{
                "X-OpenEvo-Resource-Generation": str(profile["connection_generation"]),
                "Idempotency-Key": "routing-slow-project-create-01",
            }
        )
        before = time.monotonic()
        created = client.post(
            "/desktop/v2/projects",
            headers=headers,
            json=_project_create(profile),
        )
        elapsed = time.monotonic() - before

        assert created.status_code == 202, created.text
        assert elapsed < 0.5
        assert bridge.activation_started.wait(2)
        replay = client.post(
            "/desktop/v2/projects",
            headers=headers,
            json=_project_create(profile),
        )
        assert replay.status_code == 202, replay.text
        assert replay.json()["operation_id"] == created.json()["operation_id"]
        assert len([call for call in bridge.calls if call[0] == "activate_project"]) == 1
        bridge.activation_release.set()
        terminal = _wait_lifecycle_operation(client, created.json())
        assert terminal["status"] == "succeeded"
    finally:
        bridge.activation_release.set()
        client.close()
        provider.close()
        store.close()


def test_disconnect_and_reconnect_restore_the_exact_v2_project_tunnel(
    tmp_path: Path,
) -> None:
    provider, store, lifecycle, bridge = _provider(tmp_path)
    client = TestClient(
        create_release_desktop_local_api_v2_app(
            session_token=SESSION,
            provider=provider,
            close_on_shutdown=False,
        )
    )
    try:
        profile = _connected_profile(client)
        created = client.post(
            "/desktop/v2/projects",
            headers=_headers(
                **{
                    "X-OpenEvo-Resource-Generation": str(profile["connection_generation"]),
                    "Idempotency-Key": "reconnect-create-project-01",
                }
            ),
            json=_project_create(profile),
        )
        assert created.status_code == 202, created.text
        assert _wait_lifecycle_operation(client, created.json())["status"] == "succeeded"
        bound = client.get(
            f"/desktop/v2/profiles/{profile['profile_id']}", headers=_headers()
        ).json()
        assert bound["active_project_id"] == "project-1"

        disconnected = client.post(
            f"/desktop/v2/profiles/{profile['profile_id']}/disconnect",
            headers=_headers(
                **{
                    "X-OpenEvo-Resource-Generation": str(bound["connection_generation"]),
                    "If-Match": str(bound["etag"]),
                    "Idempotency-Key": "reconnect-disconnect-profile-1",
                }
            ),
            json={
                "schema_version": "2",
                "expected_connection_generation": bound["connection_generation"],
            },
        )
        assert disconnected.status_code == 202, disconnected.text
        assert _wait_lifecycle_operation(client, disconnected.json())["status"] == "succeeded"
        offline = client.get(
            f"/desktop/v2/profiles/{profile['profile_id']}", headers=_headers()
        ).json()
        assert offline["connection_state"] == "disconnected"
        assert offline["active_project_id"] == "project-1"
        assert ("deactivate_project", bridge.mapping.desktop_project_id, 2) in bridge.calls

        reconnected = client.post(
            f"/desktop/v2/profiles/{profile['profile_id']}/connect",
            headers=_headers(
                **{
                    "X-OpenEvo-Resource-Generation": str(offline["connection_generation"]),
                    "If-Match": str(offline["etag"]),
                    "Idempotency-Key": "reconnect-connect-profile-02",
                }
            ),
            json={
                "schema_version": "2",
                "expected_connection_generation": offline["connection_generation"],
            },
        )
        assert reconnected.status_code == 202, reconnected.text
        assert _wait_lifecycle_operation(client, reconnected.json())["status"] == "succeeded"
        online = client.get(
            f"/desktop/v2/profiles/{profile['profile_id']}", headers=_headers()
        ).json()
        assert online["connection_state"] == "connected"
        assert online["active_project_id"] == "project-1"
        assert bridge.active_activation is not None
        assert bridge.active_activation.profile_connection_generation == 4
        assert bridge.active_activation.project.project_id == "project-1"
        assert lifecycle.calls[-2:] == [
            ("disconnect", profile["profile_id"], 3),
            ("connect", "gpu-lab", 4),
        ]

        read = client.get("/desktop/v2/projects/project-1", headers=_headers())
        assert read.status_code == 200, read.text
    finally:
        client.close()
        provider.close()
        store.close()


def test_stale_project_create_and_core_failure_never_fall_back_to_ssh(
    tmp_path: Path,
) -> None:
    provider, store, lifecycle, bridge = _provider(tmp_path)
    client = TestClient(
        create_release_desktop_local_api_v2_app(
            session_token=SESSION,
            provider=provider,
            close_on_shutdown=False,
        ),
        raise_server_exceptions=False,
    )
    try:
        profile = _connected_profile(client)
        ssh_calls = list(lifecycle.calls)
        stale = client.post(
            "/desktop/v2/projects",
            headers=_headers(
                **{
                    "X-OpenEvo-Resource-Generation": "1",
                    "Idempotency-Key": "routing-stale-project-01",
                }
            ),
            json=_project_create(profile),
        )
        assert stale.status_code == 412, stale.text
        assert stale.json()["code"] == "profile_generation_changed"
        assert bridge.calls == []
        assert lifecycle.calls == ssh_calls

        created = client.post(
            "/desktop/v2/projects",
            headers=_headers(
                **{
                    "X-OpenEvo-Resource-Generation": str(profile["connection_generation"]),
                    "Idempotency-Key": "routing-create-project-02",
                }
            ),
            json=_project_create(profile),
        )
        assert created.status_code == 202, created.text
        assert _wait_lifecycle_operation(client, created.json())["status"] == "succeeded"
        bridge.fail_reads = True
        failed = client.get("/desktop/v2/projects/project-1", headers=_headers())
        assert failed.status_code == 503, failed.text
        assert failed.json()["code"] == "core_temporarily_unavailable"
        assert lifecycle.calls == ssh_calls
    finally:
        client.close()
        provider.close()
        store.close()


def test_native_project_streams_verified_private_import_to_core_v2_only(
    tmp_path: Path,
) -> None:
    action_id = "routing-native-workspace-0001"
    import_id = native_import_id_for_action(action_id)
    archive = b"\0" * 1024
    import_ref = WorkspaceImportRefV1(
        import_id=import_id,
        content_sha256=hashlib.sha256(archive).hexdigest(),
        byte_size=len(archive),
        entry_count=0,
        extracted_byte_size=0,
    )
    workspace_store = WorkspaceImportStore(tmp_path / "workspace-imports")
    archive_path = tmp_path / "workspace.tar"
    archive_path.write_bytes(archive)
    ownership = ownership_for_native_import(
        import_ref,
        project_id=project_id_for_native_import(import_id),
    )
    with archive_path.open("rb") as stream:
        workspace_store.ingest_pending(
            stream,
            ownership=ownership,
            import_id=import_id,
        )
    provider, store, lifecycle, bridge = _provider(
        tmp_path,
        workspace_import_store=workspace_store,
    )
    client = TestClient(
        create_release_desktop_local_api_v2_app(
            session_token=SESSION,
            provider=provider,
            close_on_shutdown=False,
        )
    )
    try:
        profile = _connected_profile(client)
        request = _project_create(profile)
        request["config"]["workspace"] = {
            "kind": "native_folder_snapshot",
            "display_name": "Selected workspace",
        }
        ssh_calls = list(lifecycle.calls)

        created = client.post(
            "/desktop/v2/projects",
            headers=_headers(
                **{
                    "X-OpenEvo-Resource-Generation": str(profile["connection_generation"]),
                    "Idempotency-Key": action_id,
                }
            ),
            json=request,
        )

        assert created.status_code == 202, created.text
        terminal = _wait_lifecycle_operation(client, created.json())
        assert terminal["status"] == "succeeded"
        assert terminal["result"]["project_id"] == "project-1"
        assert [call[0] for call in bridge.calls] == [
            "activate_project",
            "create_workspace_upload",
            "put_workspace_upload_chunk",
            "finalize_workspace_upload",
            "get_project",
        ]
        assert lifecycle.calls == ssh_calls
        with pytest.raises(WorkspaceImportNotFoundError):
            workspace_store.inspect(import_id)
    finally:
        client.close()
        provider.close()
        store.close()
        workspace_store.close()


def test_native_workspace_prepare_resumes_without_reselection_after_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selected = tmp_path / "selected-workspace"
    selected.mkdir()
    (selected / "input.txt").write_text("workspace input\n", encoding="utf-8")
    selected_stat = selected.stat()
    workspace_root = tmp_path / "workspace-imports"
    first_workspace_store = WorkspaceImportStore(workspace_root)
    provider, store, _lifecycle, _bridge = _provider(
        tmp_path,
        workspace_import_store=first_workspace_store,
    )
    started = Event()
    original_prepare = release_provider_module.prepare_native_workspace

    @contextmanager
    def interrupted_prepare(*_args: object, **kwargs: object):
        cancel_check = kwargs["cancel_check"]
        assert callable(cancel_check)
        started.set()
        while not cancel_check():
            time.sleep(0.01)
        raise NativeWorkspaceArchiveCancelled("sidecar stopped")
        yield  # pragma: no cover

    monkeypatch.setattr(
        release_provider_module,
        "prepare_native_workspace",
        interrupted_prepare,
    )
    action_id = "routing-native-restart-prepare-0001"
    operation = provider.reserve_native_workspace_prepare(
        action_id=action_id,
        selected_path=str(selected),
        selected_device=selected_stat.st_dev,
        selected_inode=selected_stat.st_ino,
        cancellation_token="a" * 64,
        project_id=None,
    )
    assert started.wait(1)
    provider.close()
    assert store.get_lifecycle_operation(operation.operation_id).status == "running"
    store.close()
    first_workspace_store.close()

    monkeypatch.setattr(
        release_provider_module,
        "prepare_native_workspace",
        original_prepare,
    )
    reopened = DesktopProviderStoreV2(tmp_path / "provider-v2", clock=lambda: NOW)
    second_workspace_store = WorkspaceImportStore(workspace_root)
    lifecycle = _Lifecycle()
    bridge = _RoutingBridge()
    recovered_provider = DesktopReleaseProviderV2(
        store=reopened,
        catalog=_Catalog(),
        lifecycle=lifecycle,
        core_connector=_CoreConnector(),
        bridge=bridge,
        bridge_store=bridge,
        workspace_import_store=second_workspace_store,
        event_broker=DesktopEventBrokerV2(clock=lambda: NOW),
        build_version="0.1.10",
        source_commit=SOURCE_COMMIT,
        build_channel="release",
        instance_id="routing-native-restart-instance-v2",
        clock=lambda: NOW,
        own_resources=False,
    )
    client = TestClient(
        create_release_desktop_local_api_v2_app(
            session_token=SESSION,
            provider=recovered_provider,
            close_on_shutdown=False,
        )
    )
    try:
        terminal = _wait_lifecycle_operation(
            client,
            {"operation_id": operation.operation_id},
        )
        assert terminal["status"] == "succeeded"
        assert terminal["result"]["result_kind"] == "native_workspace"
        assert str(selected) not in str(terminal)
    finally:
        client.close()
        recovered_provider.close()
        reopened.close()
        second_workspace_store.close()


def test_native_project_retry_releases_import_after_remote_finalize(
    tmp_path: Path,
) -> None:
    action_id = "routing-native-workspace-retry-0001"
    import_id = native_import_id_for_action(action_id)
    archive = b"\0" * 1024
    import_ref = WorkspaceImportRefV1(
        import_id=import_id,
        content_sha256=hashlib.sha256(archive).hexdigest(),
        byte_size=len(archive),
        entry_count=0,
        extracted_byte_size=0,
    )
    workspace_store = WorkspaceImportStore(tmp_path / "workspace-imports")
    archive_path = tmp_path / "workspace.tar"
    archive_path.write_bytes(archive)
    ownership = ownership_for_native_import(
        import_ref,
        project_id=project_id_for_native_import(import_id),
    )
    with archive_path.open("rb") as stream:
        workspace_store.ingest_pending(
            stream,
            ownership=ownership,
            import_id=import_id,
        )
    provider, store, _lifecycle, bridge = _provider(
        tmp_path,
        workspace_import_store=workspace_store,
    )
    client = TestClient(
        create_release_desktop_local_api_v2_app(
            session_token=SESSION,
            provider=provider,
            close_on_shutdown=False,
        )
    )
    try:
        profile = _connected_profile(client)
        request = _project_create(profile)
        request["config"]["workspace"] = {
            "kind": "native_folder_snapshot",
            "display_name": "Selected workspace",
        }
        headers = _headers(
            **{
                "X-OpenEvo-Resource-Generation": str(profile["connection_generation"]),
                "Idempotency-Key": action_id,
            }
        )
        bridge.fail_reads = True

        failed = client.post("/desktop/v2/projects", headers=headers, json=request)

        assert failed.status_code == 202, failed.text
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and not any(
            call[0] == "get_project" for call in bridge.calls
        ):
            time.sleep(0.01)
        assert any(call[0] == "get_project" for call in bridge.calls)
        assert workspace_store.inspect(import_id).pending

        bridge.fail_reads = False
        retried = client.post("/desktop/v2/projects", headers=headers, json=request)

        assert retried.status_code == 202, retried.text
        assert retried.json()["operation_id"] == failed.json()["operation_id"]
        terminal = _wait_lifecycle_operation(client, retried.json())
        assert terminal["status"] == "succeeded"
        assert terminal["result"]["project_id"] == "project-1"
        with pytest.raises(WorkspaceImportNotFoundError):
            workspace_store.inspect(import_id)
    finally:
        client.close()
        provider.close()
        store.close()
        workspace_store.close()


def test_native_prepare_and_project_create_have_distinct_action_steps(
    tmp_path: Path,
) -> None:
    action_id = "routing-native-action-chain-0001"
    provider, store, _lifecycle, _bridge = _provider(tmp_path)
    client = TestClient(
        create_release_desktop_local_api_v2_app(
            session_token=SESSION,
            provider=provider,
            close_on_shutdown=False,
        )
    )
    try:
        profile = _connected_profile(client)
        import_id = native_import_id_for_action(action_id)
        prepared = store.reserve_lifecycle_operation(
            LifecycleOperationReservationV2(
                kind="native_workspace_prepare",
                resource={
                    "resource_kind": "native_workspace",
                    "resource_id": import_id,
                },
                request=LifecycleNativeWorkspacePrepareRequestV2(
                    request_kind="native_workspace_prepare",
                    native_workspace_id=import_id,
                    native_journal_sha256="a" * 64,
                    display_name="Selected workspace",
                ),
            ),
            idempotency_key=action_id,
        )
        request = _project_create(profile)
        request["config"]["workspace"] = {
            "kind": "native_folder_snapshot",
            "display_name": "Selected workspace",
        }

        created = client.post(
            "/desktop/v2/projects",
            headers=_headers(
                **{
                    "X-OpenEvo-Resource-Generation": str(profile["connection_generation"]),
                    "Idempotency-Key": action_id,
                }
            ),
            json=request,
        )

        assert created.status_code == 202, created.text
        assert created.json()["operation_id"] != prepared.operation_id
        recovered = client.get(
            "/desktop/v2/operations/by-action",
            params={"action_id": action_id, "kind": "project_create"},
            headers=_headers(),
        )
        assert recovered.status_code == 200, recovered.text
        assert recovered.json()["operation_id"] == created.json()["operation_id"]
    finally:
        client.close()
        provider.close()
        store.close()


def test_active_project_business_surface_routes_to_core_v2_without_ssh(
    tmp_path: Path,
) -> None:
    provider, store, lifecycle, bridge = _provider(tmp_path)
    client = TestClient(
        create_release_desktop_local_api_v2_app(
            session_token=SESSION,
            provider=provider,
            close_on_shutdown=False,
        )
    )
    try:
        profile = _connected_profile(client)
        create = client.post(
            "/desktop/v2/projects",
            headers=_headers(
                **{
                    "X-OpenEvo-Resource-Generation": "2",
                    "Idempotency-Key": "routing-full-project-01",
                }
            ),
            json=_project_create(profile),
        )
        assert create.status_code == 202, create.text
        assert _wait_lifecycle_operation(client, create.json())["status"] == "succeeded"
        ssh_calls = list(lifecycle.calls)
        project_response = client.get(
            "/desktop/v2/projects/project-1",
            headers=_headers(),
        )
        assert project_response.status_code == 200, project_response.text
        project = project_response.json()
        head = project["active_project_head"]
        project_headers = _headers(
            **{
                "X-OpenEvo-Resource-Generation": str(head["generation"]),
                "If-Match": project["etag"],
                "Idempotency-Key": "routing-project-mutation-01",
            }
        )

        assert client.get("/desktop/v2/projects", headers=_headers()).status_code == 200
        capabilities = client.get(
            "/desktop/v2/projects/project-1/capabilities", headers=_headers()
        )
        assert capabilities.status_code == 200, capabilities.text
        assert capabilities.json()["registry_sha256"] == "a" * 64

        validation = client.post(
            "/desktop/v2/projects/project-1/validate",
            headers=project_headers,
            json={
                "schema_version": "2",
                "expected_project_head_id": head["project_head_id"],
                "expected_project_head_manifest_sha256": head["manifest_sha256"],
                "expected_project_config_sha256": project["project_config_sha256"],
                "capability_registry_sha256": "a" * 64,
            },
        )
        assert validation.status_code == 200, validation.text
        assert validation.json()["valid"] is True

        patched = client.patch(
            "/desktop/v2/projects/project-1",
            headers={
                **project_headers,
                "Idempotency-Key": "routing-project-patch-01",
            },
            json={
                "schema_version": "2",
                "expected_project_head_id": head["project_head_id"],
                "expected_project_head_manifest_sha256": head["manifest_sha256"],
                "expected_project_config_sha256": project["project_config_sha256"],
                "display_name": "Project",
                "config": _config().model_dump(mode="json"),
            },
        )
        assert patched.status_code == 200, patched.text

        submitted = client.post(
            "/desktop/v2/tasks",
            headers=_headers(
                **{
                    "X-OpenEvo-Resource-Generation": "0",
                    "Idempotency-Key": "routing-submit-task-01",
                }
            ),
            json={
                "schema_version": "2",
                "project_id": "project-1",
                "expected_project_admission_etag": project["admission_etag"],
                "expected_project_head_id": head["project_head_id"],
                "expected_project_head_manifest_sha256": head["manifest_sha256"],
                "expected_project_config_sha256": project["project_config_sha256"],
            },
        )
        assert submitted.status_code == 202, submitted.text
        task = submitted.json()
        assert task["task_id"] == "task-1"
        for path in (
            "/desktop/v2/tasks?project_id=project-1",
            "/desktop/v2/tasks/task-1",
            "/desktop/v2/tasks/task-1/timeline",
            "/desktop/v2/tasks/task-1/logs",
            "/desktop/v2/tasks/task-1/context",
            "/desktop/v2/tasks/task-1/artifacts",
        ):
            response = client.get(path, headers=_headers())
            assert response.status_code == 200, (path, response.text)

        task_action = {
            "schema_version": "2",
            "task_admission_id": task["admission"]["task_admission_id"],
            "admission_sha256": task["admission"]["admission_sha256"],
            "predecessor_project_head_id": head["project_head_id"],
        }
        task_headers = _headers(
            **{
                "X-OpenEvo-Resource-Generation": "1",
                "If-Match": task["etag"],
                "Idempotency-Key": "routing-task-cancel-01",
            }
        )
        cancelled = client.post(
            "/desktop/v2/tasks/task-1/cancel",
            headers=task_headers,
            json=task_action,
        )
        assert cancelled.status_code == 202, cancelled.text
        assert cancelled.json()["kind"] == "attempt_cancel"
        retried = client.post(
            "/desktop/v2/tasks/task-1/retry",
            headers={**task_headers, "Idempotency-Key": "routing-task-retry-001"},
            json=task_action,
        )
        assert retried.status_code == 202, retried.text
        assert retried.json()["kind"] == "task_retry"

        for path in (
            "/desktop/v2/project-heads/head-0",
            "/desktop/v2/evolution-revisions/evolution-0",
            "/desktop/v2/runtime-contexts/runtime-context-0",
            "/desktop/v2/transitions/transition-1",
            "/desktop/v2/artifacts/artifact-1",
            "/desktop/v2/artifacts/artifact-1/content",
            "/desktop/v2/artifacts/artifact-1/diff?previous_artifact_id=artifact-0",
            "/desktop/v2/services",
        ):
            response = client.get(path, headers=_headers())
            assert response.status_code == 200, (path, response.text)

        transition_action = {
            "schema_version": "2",
            "expected_predecessor_project_head_id": "head-0",
            "plan_sha256": "e" * 64,
        }
        transition_headers = _headers(
            **{
                "X-OpenEvo-Resource-Generation": "1",
                "If-Match": project["etag"],
                "Idempotency-Key": "routing-transition-retry-01",
            }
        )
        retried_transition = client.post(
            "/desktop/v2/transitions/transition-1/retry",
            headers=transition_headers,
            json=transition_action,
        )
        assert retried_transition.status_code == 202, retried_transition.text
        assert retried_transition.json()["kind"] == "transition_retry"
        abandoned = client.post(
            "/desktop/v2/transitions/transition-1/abandon",
            headers={
                **transition_headers,
                "Idempotency-Key": "routing-transition-abandon-1",
            },
            json=transition_action,
        )
        assert abandoned.status_code == 202, abandoned.text
        assert abandoned.json()["kind"] == "transition_abandon"

        restarted = client.post(
            "/desktop/v2/services/service-daemon/restart",
            headers=_headers(
                **{
                    "X-OpenEvo-Resource-Generation": "2",
                    "If-Match": _service().etag,
                    "Idempotency-Key": "routing-service-restart-01",
                }
            ),
            json={"schema_version": "2", "expected_service_id": "service-daemon"},
        )
        assert restarted.status_code == 202, restarted.text
        assert restarted.json()["kind"] == "service_restart"

        core_operation = client.get(
            "/desktop/v2/core-operations/operation-service-restart",
            headers=_headers(),
        )
        assert core_operation.status_code == 200, core_operation.text
        assert core_operation.json()["operation_id"] == "operation-service-restart"
        cancelled_operation = client.post(
            "/desktop/v2/core-operations/operation-service-restart/cancel",
            headers=_headers(
                **{
                    "X-OpenEvo-Resource-Generation": "2",
                    "If-Match": core_operation.json()["etag"],
                    "Idempotency-Key": "routing-core-operation-cancel-1",
                }
            ),
        )
        assert cancelled_operation.status_code == 202, cancelled_operation.text
        assert cancelled_operation.json()["operation_id"] == core_operation.json()["operation_id"]
        service_logs = client.get(
            "/desktop/v2/services/service-daemon/logs?limit=25",
            headers=_headers(),
        )
        assert service_logs.status_code == 200, service_logs.text
        assert service_logs.json()["items"][0]["message"] == "daemon ready"
        cache_cleanup = client.post(
            "/desktop/v2/maintenance/cache-cleanup",
            headers=_headers(
                **{
                    "X-OpenEvo-Resource-Generation": "2",
                    "Idempotency-Key": "routing-cache-cleanup-0001",
                }
            ),
            json={"schema_version": "2", "scope": "safe_unreferenced"},
        )
        assert cache_cleanup.status_code == 202, cache_cleanup.text
        assert cache_cleanup.json()["kind"] == "cache_cleanup"

        diagnostic = client.post(
            "/desktop/v2/diagnostics",
            headers=_headers(
                **{
                    "X-OpenEvo-Resource-Generation": "2",
                    "Idempotency-Key": "routing-diagnostic-create-1",
                }
            ),
            json={
                "schema_version": "2",
                "profile_id": profile["profile_id"],
                "profile_connection_generation": 2,
                "scope": "project",
                "resource_id": "project-1",
            },
        )
        assert diagnostic.status_code == 202, diagnostic.text
        assert (
            client.get("/desktop/v2/diagnostics/diagnostic-1", headers=_headers()).status_code
            == 200
        )

        called = {str(call[0]) for call in bridge.calls}
        assert {
            "list_projects",
            "capabilities",
            "validate_project",
            "update_project",
            "submit_task",
            "list_tasks",
            "get_task",
            "append_task_attempt",
            "cancel_task_attempt",
            "task_timeline",
            "task_logs",
            "task_context",
            "task_artifacts",
            "get_project_head",
            "list_project_heads",
            "get_transition",
            "retry_transition",
            "abandon_transition",
            "get_artifact",
            "artifact_content",
            "list_services",
            "get_service",
            "restart_service",
            "get_operation",
            "cancel_operation",
            "service_logs",
            "cache_cleanup",
            "create_diagnostic",
            "get_diagnostic",
        } <= called
        assert lifecycle.calls == ssh_calls
    finally:
        client.close()
        provider.close()
        store.close()
