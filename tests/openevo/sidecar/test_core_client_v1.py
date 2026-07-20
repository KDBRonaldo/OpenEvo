from __future__ import annotations

import base64
from contextlib import contextmanager
import hashlib
import json
import secrets
import threading
import time

import httpx
import pytest

from desktop.sidecar.core_client_v1 import (
    CORE_OPENAPI_SHA256,
    CoreBootstrapTunnelConnectionV1,
    CoreClientErrorV1,
    CoreClientLocalErrorCodeV1,
    CoreClientLocalErrorV1,
    CoreControlClientV1,
    CoreMutationOutcomeUnknownV1,
    CoreProjectBootstrapClientV1,
    CoreProjectBootstrapResultV1,
    CoreSseStreamV1,
    CoreTunnelConnectionV1,
    MAX_CORE_ARTIFACT_RESPONSE_BYTES,
    MAX_CORE_JSON_RESPONSE_BYTES,
    MAX_CORE_SSE_FRAME_BYTES,
    MAX_CORE_SSE_RESPONSE_BYTES,
    _ensure_project_create_response,
)
from openevo.backend.contracts.v1 import models as v1
from openevo.evolution.framework import (
    CapabilityAudience,
    build_evolution_capabilities,
)
from openevo.evolution.framework.builtins import (
    ImplementationDistributionIdentity,
    build_builtin_registry,
)
from openevo.evolution.framework.profiles import execution_profile_for_release_mode


PROJECT_ID = "project/active?one"
SESSION_ID = "desktop-session-1"
ETAG = '"' + ("a" * 64) + '"'


def _token() -> str:
    return secrets.token_urlsafe(32)


def _connection(*, token: str | None = None) -> CoreTunnelConnectionV1:
    return CoreTunnelConnectionV1(
        endpoint="http://127.0.0.1:48765",
        bearer_token=token or _token(),
        project_id=PROJECT_ID,
        session_id=SESSION_ID,
    )


def _bootstrap_connection(*, token: str | None = None) -> CoreBootstrapTunnelConnectionV1:
    return CoreBootstrapTunnelConnectionV1(
        endpoint="http://127.0.0.1:48765",
        bearer_token=token or _token(),
        session_id=SESSION_ID,
    )


def _client(
    handler,
    *,
    connection: CoreTunnelConnectionV1 | None = None,
    negotiate: bool = True,
    timeout: float | httpx.Timeout = 30.0,
) -> CoreControlClientV1:
    def versioned_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/version":
            return httpx.Response(200, json=_version_payload())
        return handler(request)

    client = CoreControlClientV1(
        connection or _connection(),
        transport=httpx.MockTransport(versioned_handler),
        timeout=timeout,
    )
    if negotiate:
        assert client.version().openapi_sha256 == CORE_OPENAPI_SHA256
    return client


def _version_payload(
    *,
    digest: str = CORE_OPENAPI_SHA256,
    provider_kind: str = "openevo_core",
    build_channel: str = "release",
) -> dict[str, object]:
    return {
        "schema_version": "1",
        "preferred_major": 1,
        "supported_majors": [1],
        "openapi_sha256": digest,
        "build_version": "0.1.0",
        "source_commit": "60a06597",
        "build_channel": build_channel,
        "provider_kind": provider_kind,
        "features": [],
    }


def _health_payload() -> dict[str, object]:
    return {
        "schema_version": "1",
        "status": "ok",
        "ready": True,
        "checked_at": "2026-07-14T12:00:00Z",
    }


def _capabilities(
    execution_mode: v1.ExecutionMode = v1.ExecutionMode.SELF_DEPLOYED,
) -> v1.CapabilitiesResponseV1:
    return v1.CapabilitiesResponseV1(
        core_version="0.1.0",
        registry_digest="4" * 64,
        evaluated_profile=execution_profile_for_release_mode(execution_mode),
        targets=(),
    )


def _full_capabilities(
    execution_mode: v1.ExecutionMode = v1.ExecutionMode.SELF_DEPLOYED,
) -> v1.CapabilitiesResponseV1:
    snapshot = build_builtin_registry(
        ImplementationDistributionIdentity(
            distribution="openevo-test",
            distribution_version="0.1.0-test",
            distribution_digest="a" * 64,
        )
    )
    return build_evolution_capabilities(
        snapshot,
        profile=execution_profile_for_release_mode(execution_mode),
        audience=CapabilityAudience.DESKTOP,
        core_version="0.1.0-test",
    )


def _api_error_payload(status: int = 409) -> dict[str, object]:
    return {
        "schema_version": "1",
        "request_id": "request-1",
        "code": "resource_conflict",
        "http_status": status,
        "message": "The resource changed.",
        "severity": "blocking",
        "category": "project",
        "retryable": True,
        "repair_action": "openevo_can_retry",
        "next_action": "Reload and retry.",
        "details": {"field_issues": [], "conflicts": []},
        "logs_ref": None,
    }


def _queued_environment_operation(
    request: v1.EnvironmentRepairRequestV1,
) -> v1.OperationV1:
    return v1.OperationV1(
        id="operation-1",
        kind=v1.OperationKind.ENVIRONMENT_REPAIR,
        descriptor=v1.OperationDescriptorV1(
            kind=v1.OperationKind.ENVIRONMENT_REPAIR,
            cancellable=True,
        ),
        status=v1.OperationStatus.QUEUED,
        request=v1.EnvironmentRepairOperationRequestV1(
            kind=v1.OperationKind.ENVIRONMENT_REPAIR,
            request=request,
        ),
        logs_ref="operation-1-logs",
        created_at="2026-07-14T12:00:00Z",
        updated_at="2026-07-14T12:00:00Z",
        observed_at="2026-07-14T12:00:00Z",
        etag=ETAG,
    )


def _snapshot(snapshot_id: str, kind: v1.SnapshotKind, seed: str) -> v1.ImmutableSnapshotRefV1:
    return v1.ImmutableSnapshotRefV1(
        id=snapshot_id,
        kind=kind,
        content_sha256=seed * 64,
        created_at="2026-07-14T12:00:00Z",
    )


def _revision_ref(project_id: str = PROJECT_ID) -> v1.RevisionRefV1:
    return v1.RevisionRefV1(
        id="revision-1",
        project_id=project_id,
        generation=1,
        manifest_sha256="9" * 64,
    )


def _required_revision(project_id: str = PROJECT_ID) -> v1.ReachableRequiredRevisionRefV1:
    revision = _revision_ref(project_id)
    return v1.ReachableRequiredRevisionRefV1(
        revision=revision,
        reachable_from_revision_id=revision.id,
        relation=v1.RequiredRevisionRelation.ACTIVE,
    )


def _run(run_id: str = "run-1", project_id: str = PROJECT_ID) -> v1.RunV1:
    return v1.RunV1(
        id=run_id,
        project_id=project_id,
        project_snapshot=_snapshot("project-snapshot-1", v1.SnapshotKind.PROJECT, "1"),
        task_snapshot=_snapshot("task-snapshot-1", v1.SnapshotKind.TASK, "2"),
        workspace_snapshot=_snapshot("workspace-snapshot-1", v1.SnapshotKind.WORKSPACE, "3"),
        registry_digest="4" * 64,
        execution_mode=v1.ExecutionMode.SELF_DEPLOYED,
        capture_mode=v1.CaptureMode.TRANSCRIPT,
        status=v1.RunStatus.QUEUED,
        queued_reason=v1.QueuedReasonV1(
            code=v1.QueuedReasonCode.CAPACITY,
            summary="Capacity is pending.",
            retry_after_seconds=5,
        ),
        attempt_count=0,
        required_revision=_required_revision(project_id),
        created_at="2026-07-14T12:00:00Z",
        updated_at="2026-07-14T12:00:00Z",
        etag=ETAG,
        attempts=[],
    )


def _run_create(run: v1.RunV1 | None = None) -> v1.RunCreateV1:
    run = run or _run()
    return v1.RunCreateV1(
        project_id=run.project_id,
        project_snapshot=run.project_snapshot,
        task_snapshot=run.task_snapshot,
        workspace_snapshot=run.workspace_snapshot,
        expected_registry_digest=run.registry_digest,
        required_revision=run.required_revision,
    )


def _failed_run() -> v1.RunV1:
    error = v1.ApiErrorV1.model_validate_json(json.dumps(_api_error_payload(503)))
    attempt = v1.AttemptV1(
        id="attempt-terminal-1",
        run_id="run-1",
        number=1,
        status=v1.RunStatus.FAILED,
        created_at="2026-07-14T12:00:00Z",
        updated_at="2026-07-14T12:00:02Z",
        started_at="2026-07-14T12:00:01Z",
        finished_at="2026-07-14T12:00:02Z",
        error=error,
    )
    run = _run()
    return run.model_copy(
        update={
            "status": v1.RunStatus.FAILED,
            "queued_reason": None,
            "current_attempt_id": attempt.id,
            "current_attempt": attempt,
            "attempt_count": 1,
            "current_error": error,
            "pinned_revision": run.required_revision.revision,
            "admitted_at": "2026-07-14T12:00:00Z",
            "started_at": attempt.started_at,
            "finished_at": attempt.finished_at,
            "updated_at": attempt.updated_at,
            "attempts": [attempt],
        }
    )


def _service(service_id: str = "service-1") -> v1.ServiceSummaryV1:
    return v1.ServiceSummaryV1(
        id=service_id,
        display_name="Gateway",
        kind=v1.ServiceKind.GATEWAY,
        status=v1.ServiceStatus.RUNNING,
        restartable=True,
        updated_at="2026-07-14T12:00:00Z",
        observed_at="2026-07-14T12:00:00Z",
        etag=ETAG,
    )


def _artifact(
    artifact_id: str,
    *,
    digest: str,
    run_id: str | None = "run-1",
    source_artifact_ids: list[str] | None = None,
) -> v1.SkillBundleArtifactSummaryV1:
    revision = _revision_ref()
    return v1.SkillBundleArtifactSummaryV1(
        id=artifact_id,
        project_id=PROJECT_ID,
        run_id=run_id,
        target_id="skill_bundle",
        display_name="Skill",
        summary="A generated skill.",
        byte_size=5,
        produced_revision=revision,
        membership_revisions=[revision],
        content_sha256=digest,
        selected=True,
        promoted=False,
        release_enabled=True,
        compatibility=v1.ArtifactCompatibilityV1(
            execution_modes=[v1.ExecutionMode.SELF_DEPLOYED],
            harness_ids=["codex"],
            base_model_refs=["openai/gpt-oss-20b"],
        ),
        lineage=v1.ArtifactLineageV1(
            method_id="method-1",
            job_id="job-1",
            source_dataset_ids=[],
            source_artifact_ids=source_artifact_ids or [],
        ),
        scores=[],
        metadata=v1.SkillBundleArtifactMetadataV1(document_count=1),
        created_at="2026-07-14T12:00:00Z",
        artifact_type=v1.ArtifactType.SKILL_BUNDLE,
    )


def _archive() -> v1.WorkspaceArchiveDeclarationV1:
    return v1.WorkspaceArchiveDeclarationV1(
        content_sha256="c" * 64,
        byte_size=1024,
        format=v1.WorkspaceArchiveFormat.OPENEVO_DETERMINISTIC_TAR_V1,
        entry_count=0,
        extracted_byte_size=0,
        policy=v1.WorkspaceArchivePolicyV1(
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
        ),
    )


def _chunk() -> v1.WorkspaceUploadChunkV1:
    content = b"x" * 1024
    return v1.WorkspaceUploadChunkV1(
        offset=0,
        byte_length=len(content),
        content_base64=base64.b64encode(content).decode(),
        content_sha256=hashlib.sha256(content).hexdigest(),
    )


def _project(
    *,
    publication: v1.WorkspacePublicationV1 | None = None,
    registry_digest: str | None = None,
) -> v1.ProjectV1:
    project_snapshot = _snapshot(
        "project-snapshot-2" if publication else "project-snapshot-1",
        v1.SnapshotKind.PROJECT,
        "5" if publication else "1",
    )
    workspace_snapshot = publication.workspace_snapshot if publication else None
    spec = v1.ProjectSpecV1(
        execution_mode=v1.ExecutionMode.SELF_DEPLOYED,
        capture_mode=v1.CaptureMode.TRANSCRIPT,
        harness_id="codex",
        agent_model_ref="openai/gpt-oss-20b",
        evolution=v1.EvolutionConfigV1(targets={}),
    )
    workspace = v1.ImportedWorkspaceSpecV1(
        kind=v1.WorkspaceSourceKind.NATIVE_FOLDER_SNAPSHOT,
        display_name="Workspace",
        archive=_archive(),
    )
    return v1.ProjectV1(
        id=PROJECT_ID,
        name="Project",
        status=v1.ProjectStatus.DRAFT,
        execution_mode=v1.ExecutionMode.SELF_DEPLOYED,
        workspace_kind=v1.WorkspaceSourceKind.NATIVE_FOLDER_SNAPSHOT,
        current_project_snapshot=project_snapshot,
        current_task_snapshot=_snapshot("task-snapshot-1", v1.SnapshotKind.TASK, "2"),
        current_workspace_snapshot=workspace_snapshot,
        workspace_publication=publication,
        registry_digest=registry_digest,
        model_preparation=v1.ModelPreparationV1(
            model_ref="openai/gpt-oss-20b",
            status=v1.ModelPreparationStatus.UNRESOLVED,
            updated_at="2026-07-14T12:00:00Z",
        ),
        created_at="2026-07-14T12:00:00Z",
        updated_at="2026-07-14T12:00:00Z",
        etag='"' + (("f" if publication else "a") * 64) + '"',
        spec=spec,
        task=v1.TaskSpecV1(title="Task", objective="Complete the task."),
        workspace=workspace,
    )


def _project_create_request() -> v1.ProjectCreateV1:
    project = _project()
    return v1.ProjectCreateV1(
        name=project.name,
        description=project.description,
        spec=project.spec,
        task=project.task,
        workspace=project.workspace,
    )


def _upload(
    *,
    accepted_offset: int = 0,
    status: v1.WorkspaceUploadStatus = v1.WorkspaceUploadStatus.OPEN,
    etag: str = '"' + ("b" * 64) + '"',
    publication: v1.WorkspacePublicationV1 | None = None,
) -> v1.WorkspaceUploadSessionV1:
    return v1.WorkspaceUploadSessionV1(
        id="upload-1",
        project_id=PROJECT_ID,
        status=status,
        accepted_offset=accepted_offset,
        project_snapshot=_snapshot("project-snapshot-1", v1.SnapshotKind.PROJECT, "1"),
        project_etag=ETAG,
        archive=_archive(),
        publication=publication,
        created_at="2026-07-14T12:00:00Z",
        updated_at="2026-07-14T12:00:01Z",
        etag=etag,
    )


def _page(items: list[object]) -> dict[str, object]:
    return {"schema_version": "1", "items": items, "next_cursor": None, "has_more": False}


def _publication() -> v1.WorkspacePublicationV1:
    return v1.WorkspacePublicationV1(
        archive=_archive(),
        content_ref=v1.ContentRefV1(
            content_id="workspace-content-1",
            sha256="c" * 64,
            byte_size=1024,
        ),
        workspace_snapshot=_snapshot("workspace-snapshot-2", v1.SnapshotKind.WORKSPACE, "6"),
        published_at="2026-07-14T12:00:02Z",
    )


def _diagnostic(diagnostic_id: str = "diagnostic-1") -> v1.DiagnosticV1:
    return v1.DiagnosticV1(
        id=diagnostic_id,
        status=v1.DiagnosticStatus.QUEUED,
        scopes=[v1.DiagnosticScope.PROJECT],
        target=v1.ProjectDiagnosticTargetV1(
            kind=v1.DiagnosticTargetKind.PROJECT,
            project_id=PROJECT_ID,
        ),
        checks=[],
        created_at="2026-07-14T12:00:00Z",
        updated_at="2026-07-14T12:00:00Z",
        observed_at="2026-07-14T12:00:00Z",
        etag=ETAG,
    )


def _diagnostic_with_log(
    diagnostic_id: str = "diagnostic-1",
    logs_ref: str = "diagnostic-1-logs",
) -> v1.DiagnosticV1:
    return _diagnostic(diagnostic_id).model_copy(
        update={
            "checks": [
                v1.DiagnosticCheckV1(
                    id=f"{diagnostic_id}-check",
                    scope=v1.DiagnosticScope.PROJECT,
                    status=v1.CheckStatus.OK,
                    message="Project state is valid.",
                    repair_action=v1.RepairAction.OPENEVO_CAN_RETRY,
                    logs_ref=logs_ref,
                )
            ]
        }
    )


def _sse_bytes(payload: dict[str, object]) -> bytes:
    data = json.dumps(payload, separators=(",", ":")).encode()
    event_id = str(payload["id"]).encode()
    event_name = str(payload["event"]).encode()
    return b"id: " + event_id + b"\nevent: " + event_name + b"\ndata: " + data + b"\n\n"


def _capture_stream_error(
    stream: CoreSseStreamV1,
    errors: list[CoreClientErrorV1],
) -> None:
    try:
        list(stream)
    except CoreClientErrorV1 as exc:
        errors.append(exc)


def _timeline_event(run_id: str = "run-1") -> dict[str, object]:
    digest = "7" * 64
    return {
        "schema_version": "1",
        "id": "event-timeline-1",
        "sequence": 1,
        "occurred_at": "2026-07-14T12:00:00Z",
        "event": "run.timeline_appended.v1",
        "change": {
            "change_id": "change-timeline-1",
            "resource_type": "timeline_entry",
            "resource_id": "timeline-1",
            "parent_resource_type": "run",
            "parent_resource_id": run_id,
            "resource_etag": None,
            "content_sha256": digest,
        },
        "payload": {
            "run_id": run_id,
            "entry": {
                "id": "timeline-1",
                "run_id": run_id,
                "attempt_id": None,
                "sequence": 1,
                "service_id": "service-1",
                "phase": "execution",
                "status": "running",
                "title": "Agent",
                "message": "Running.",
                "occurred_at": "2026-07-14T12:00:00Z",
                "artifact_ids": [],
                "content_sha256": digest,
                "error": None,
            },
        },
    }


def _service_log_event(service_id: str = "service-1") -> dict[str, object]:
    digest = "8" * 64
    return {
        "schema_version": "1",
        "id": "event-log-1",
        "sequence": 2,
        "occurred_at": "2026-07-14T12:00:00Z",
        "event": "log.appended.v1",
        "change": {
            "change_id": "change-log-1",
            "resource_type": "log_entry",
            "resource_id": "log-1",
            "parent_resource_type": "service",
            "parent_resource_id": service_id,
            "resource_etag": None,
            "content_sha256": digest,
        },
        "payload": {
            "id": "log-1",
            "sequence": 1,
            "occurred_at": "2026-07-14T12:00:00Z",
            "stream": "service",
            "level": "info",
            "message": "Service ready.",
            "run_id": None,
            "attempt_id": None,
            "service_id": service_id,
            "content_sha256": digest,
        },
    }


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://127.0.0.1:48765",
        "http://localhost:48765",
        "http://127.0.0.2:48765",
        "http://example.com:48765",
        "http://user@127.0.0.1:48765",
        "http://127.0.0.1:48765/core",
        "http://127.0.0.1:48765?next=remote",
        "http://127.0.0.1:48765#fragment",
        "http://127.0.0.1",
    ],
)
def test_connection_rejects_every_non_tunnel_origin(endpoint: str) -> None:
    with pytest.raises(CoreClientErrorV1) as exc_info:
        CoreTunnelConnectionV1(
            endpoint=endpoint,
            bearer_token=_token(),
            project_id=PROJECT_ID,
            session_id=SESSION_ID,
        )

    assert isinstance(exc_info.value.error, CoreClientLocalErrorV1)
    assert exc_info.value.error.code is CoreClientLocalErrorCodeV1.INVALID_CONNECTION
    assert endpoint not in str(exc_info.value)


def test_connection_accepts_only_explicit_ipv4_and_ipv6_loopback_origins() -> None:
    ipv4 = _connection()
    ipv6 = CoreTunnelConnectionV1(
        endpoint="http://[::1]:48765/",
        bearer_token=_token(),
        project_id=PROJECT_ID,
        session_id=SESSION_ID,
    )

    assert ipv4.origin == "http://127.0.0.1:48765"
    assert ipv6.origin == "http://[::1]:48765"


@pytest.mark.parametrize("bearer", ["short", "x" * 64, "space " + ("a" * 60)])
def test_connection_rejects_non_random_or_invalid_bearers(bearer: str) -> None:
    with pytest.raises(CoreClientErrorV1) as exc_info:
        CoreTunnelConnectionV1(
            endpoint="http://127.0.0.1:48765",
            bearer_token=bearer,
            project_id=PROJECT_ID,
            session_id=SESSION_ID,
        )

    assert exc_info.value.error.code is CoreClientLocalErrorCodeV1.INVALID_CONNECTION
    assert bearer not in str(exc_info.value)


def test_token_is_absent_from_connection_repr_and_discovery_headers() -> None:
    token = _token()
    connection = _connection(token=token)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "http://127.0.0.1:48765/health"
        assert "authorization" not in request.headers
        return httpx.Response(200, json=_health_payload())

    with _client(handler, connection=connection) as client:
        assert client.health().ready is True

    assert token not in repr(connection)
    assert token not in repr(client)


def test_v1_authorization_is_sent_only_to_fixed_origin_and_redirect_is_rejected() -> None:
    token = _token()
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.headers["authorization"] == f"Bearer {token}"
        return httpx.Response(
            307,
            headers={"Location": "https://attacker.invalid/collect"},
            content=b"redirect",
        )

    with _client(handler, connection=_connection(token=token)) as client:
        with pytest.raises(CoreClientErrorV1) as exc_info:
            client.status()

    assert len(requests) == 1
    assert requests[0].url.host == "127.0.0.1"
    assert exc_info.value.error.code is CoreClientLocalErrorCodeV1.REDIRECT_REJECTED
    assert token not in str(exc_info.value)
    assert "attacker" not in str(exc_info.value)


def test_project_bootstrap_negotiates_then_binds_the_core_generated_project_id() -> None:
    connection = _bootstrap_connection()
    request = _project_create_request()
    created = _project().model_copy(update={"id": "project-created-by-core"})
    seen: list[httpx.Request] = []

    def handler(http_request: httpx.Request) -> httpx.Response:
        seen.append(http_request)
        if http_request.url.path == "/version":
            return httpx.Response(200, json=_version_payload())
        assert http_request.url.path == "/v1/projects"
        return httpx.Response(201, json=created.model_dump(mode="json"))

    client = CoreProjectBootstrapClientV1(
        connection,
        transport=httpx.MockTransport(handler),
    )
    assert client.version().openapi_sha256 == CORE_OPENAPI_SHA256
    result = client.create_project(request, idempotency_key="bootstrap-project-0001")

    assert result == CoreProjectBootstrapResultV1(
        project=created,
        connection=connection.bind(created.id),
    )
    assert seen[1].headers["authorization"] == f"Bearer {connection.bearer_token}"
    assert seen[1].headers["idempotency-key"] == "bootstrap-project-0001"
    assert json.loads(seen[1].content) == request.model_dump(mode="json")
    assert connection.bearer_token not in repr(result)

    # A delivered success is replayed locally and cannot create a second Core project.
    assert client.create_project(request, idempotency_key="bootstrap-project-0001") == result
    assert len(seen) == 2
    client.close()


@pytest.mark.parametrize(
    ("endpoint", "session_id"),
    (
        (None, SESSION_ID),
        ("http://127.0.0.1:48765", None),
        ("http://127.0.0.1:48765", "invalid-\ud800-session"),
    ),
)
def test_project_bootstrap_connection_rejects_invalid_identity_without_raw_errors(
    endpoint: object,
    session_id: object,
) -> None:
    with pytest.raises(CoreClientErrorV1) as exc_info:
        CoreBootstrapTunnelConnectionV1(
            endpoint=endpoint,  # type: ignore[arg-type]
            bearer_token=_token(),
            session_id=session_id,  # type: ignore[arg-type]
        )

    assert exc_info.value.error.code is CoreClientLocalErrorCodeV1.INVALID_CONNECTION


def test_project_bootstrap_requires_version_negotiation_before_create_transport() -> None:
    create_called = False

    def handler(http_request: httpx.Request) -> httpx.Response:
        nonlocal create_called
        create_called = True
        return httpx.Response(201, json=_project().model_dump(mode="json"))

    client = CoreProjectBootstrapClientV1(
        _bootstrap_connection(),
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(CoreClientErrorV1) as exc_info:
        client.create_project(
            _project_create_request(),
            idempotency_key="bootstrap-without-version-0001",
        )

    assert exc_info.value.error.code is CoreClientLocalErrorCodeV1.INVALID_CONNECTION
    assert create_called is False
    client.close()


def test_project_bootstrap_lock_wait_uses_the_public_operation_deadline() -> None:
    def handler(http_request: httpx.Request) -> httpx.Response:
        assert http_request.url.path == "/version"
        return httpx.Response(200, json=_version_payload())

    client = CoreProjectBootstrapClientV1(
        _bootstrap_connection(),
        transport=httpx.MockTransport(handler),
        timeout=0.05,
    )
    client.version()
    assert client._create_lock.acquire(timeout=1)
    started = time.monotonic()
    try:
        with pytest.raises(CoreClientErrorV1) as exc_info:
            client.create_project(
                _project_create_request(),
                idempotency_key="bootstrap-lock-deadline-0001",
            )
    finally:
        client._create_lock.release()

    assert exc_info.value.error.code is CoreClientLocalErrorCodeV1.CONNECTION_FAILED
    assert time.monotonic() - started < 0.5
    client.close()


def test_bound_client_rejects_project_creation_before_transport() -> None:
    called = False

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(201, json=_project().model_dump(mode="json"))

    client = _client(handler)
    called = False  # Ignore the version negotiation performed by _client.
    with pytest.raises(CoreClientErrorV1) as exc_info:
        client.create_project(
            _project_create_request(),
            idempotency_key="bound-create-forbidden-0001",
        )

    assert exc_info.value.error.code is CoreClientLocalErrorCodeV1.INVALID_REQUEST
    assert called is False


def test_project_bootstrap_rejects_cross_wired_response_and_can_retry() -> None:
    connection = _bootstrap_connection()
    request = _project_create_request()
    valid = _project().model_copy(update={"id": "project-created-by-core"})
    invalid = valid.model_copy(update={"name": "Another project"})
    project_responses = iter((invalid, valid))

    def handler(http_request: httpx.Request) -> httpx.Response:
        if http_request.url.path == "/version":
            return httpx.Response(200, json=_version_payload())
        return httpx.Response(
            201,
            json=next(project_responses).model_dump(mode="json"),
        )

    client = CoreProjectBootstrapClientV1(
        connection,
        transport=httpx.MockTransport(handler),
    )
    client.version()
    with pytest.raises(CoreClientErrorV1) as exc_info:
        client.create_project(request, idempotency_key="bootstrap-project-0002")
    assert exc_info.value.error.code is CoreClientLocalErrorCodeV1.INVALID_RESPONSE

    result = client.create_project(request, idempotency_key="bootstrap-project-0002")
    assert result.project == valid
    assert result.connection.project_id == valid.id
    client.close()


def test_project_bootstrap_accepts_ready_scratch_generation_zero_response() -> None:
    base = _project()
    spec = v1.ProjectSpecV1(
        execution_mode=v1.ExecutionMode.CODEX_SUBSCRIPTION_TRANSCRIPT,
        capture_mode=v1.CaptureMode.TRANSCRIPT,
        harness_id="codex",
        agent_model_ref="gpt-5.3-codex-spark",
        evolution=v1.EvolutionConfigV1(targets={}),
    )
    workspace = v1.ScratchWorkspaceSpecV1(
        kind=v1.WorkspaceSourceKind.SCRATCH,
        display_name="Scratch workspace",
    )
    request = v1.ProjectCreateV1(
        name=base.name,
        description=base.description,
        spec=spec,
        task=base.task,
        workspace=workspace,
    )
    ready = base.model_copy(
        update={
            "spec": spec,
            "workspace": workspace,
            "execution_mode": v1.ExecutionMode.CODEX_SUBSCRIPTION_TRANSCRIPT,
            "workspace_kind": v1.WorkspaceSourceKind.SCRATCH,
            "current_workspace_snapshot": _snapshot(
                "workspace-snapshot-ready",
                v1.SnapshotKind.WORKSPACE,
                "8",
            ),
            "status": v1.ProjectStatus.READY,
            "active_revision": v1.RevisionRefV1(
                id="revision-generation-zero",
                project_id=base.id,
                generation=0,
                manifest_sha256="9" * 64,
            ),
            "model_preparation": v1.ModelPreparationV1(
                model_ref="gpt-5.3-codex-spark",
                status=v1.ModelPreparationStatus.READY,
                updated_at="2026-07-14T12:00:00Z",
            ),
        }
    )

    _ensure_project_create_response(request, ready)


def test_project_bootstrap_retries_unknown_transport_outcome_with_same_identity() -> None:
    connection = _bootstrap_connection()
    request = _project_create_request()
    created = _project().model_copy(update={"id": "project-created-after-retry"})
    create_attempts = 0

    def handler(http_request: httpx.Request) -> httpx.Response:
        nonlocal create_attempts
        if http_request.url.path == "/version":
            return httpx.Response(200, json=_version_payload())
        create_attempts += 1
        if create_attempts == 1:
            raise httpx.ConnectError("unknown outcome", request=http_request)
        return httpx.Response(201, json=created.model_dump(mode="json"))

    client = CoreProjectBootstrapClientV1(
        connection,
        transport=httpx.MockTransport(handler),
    )
    client.version()
    with pytest.raises(CoreClientErrorV1) as exc_info:
        client.create_project(request, idempotency_key="bootstrap-project-0003")
    assert exc_info.value.error.code is CoreClientLocalErrorCodeV1.CONNECTION_FAILED

    result = client.create_project(request, idempotency_key="bootstrap-project-0003")
    assert result.project == created
    assert create_attempts == 2
    client.close()


def test_project_bootstrap_freezes_request_identity_before_unknown_transport_outcome() -> None:
    connection = _bootstrap_connection()
    request = _project_create_request()
    create_attempts = 0

    def handler(http_request: httpx.Request) -> httpx.Response:
        nonlocal create_attempts
        if http_request.url.path == "/version":
            return httpx.Response(200, json=_version_payload())
        create_attempts += 1
        raise httpx.ConnectError("unknown outcome", request=http_request)

    client = CoreProjectBootstrapClientV1(
        connection,
        transport=httpx.MockTransport(handler),
    )
    client.version()
    with pytest.raises(CoreClientErrorV1):
        client.create_project(request, idempotency_key="bootstrap-project-frozen-0001")

    with pytest.raises(CoreClientErrorV1) as changed_request:
        client.create_project(
            request.model_copy(update={"name": "Different project"}),
            idempotency_key="bootstrap-project-frozen-0001",
        )
    with pytest.raises(CoreClientErrorV1) as changed_key:
        client.create_project(request, idempotency_key="bootstrap-project-frozen-other")

    assert changed_request.value.error.code is CoreClientLocalErrorCodeV1.INVALID_REQUEST
    assert changed_key.value.error.code is CoreClientLocalErrorCodeV1.INVALID_REQUEST
    assert create_attempts == 1
    client.close()


def test_project_bootstrap_close_seals_validation_and_result_delivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _bootstrap_connection()
    request = _project_create_request()
    created = _project().model_copy(update={"id": "project-created-before-close"})
    validation_started = threading.Event()
    release_validation = threading.Event()
    original_validate = _ensure_project_create_response

    def pause_validation(
        submitted: v1.ProjectCreateV1,
        response: v1.ProjectV1,
    ) -> None:
        original_validate(submitted, response)
        validation_started.set()
        assert release_validation.wait(timeout=5)

    monkeypatch.setattr(
        "desktop.sidecar.core_client_v1._ensure_project_create_response",
        pause_validation,
    )

    def handler(http_request: httpx.Request) -> httpx.Response:
        if http_request.url.path == "/version":
            return httpx.Response(200, json=_version_payload())
        return httpx.Response(201, json=created.model_dump(mode="json"))

    client = CoreProjectBootstrapClientV1(
        connection,
        transport=httpx.MockTransport(handler),
    )
    client.version()
    delivered: list[CoreProjectBootstrapResultV1] = []
    failures: list[CoreClientErrorV1] = []

    def create() -> None:
        try:
            delivered.append(
                client.create_project(
                    request,
                    idempotency_key="bootstrap-close-delivery-0001",
                )
            )
        except CoreClientErrorV1 as exc:
            failures.append(exc)

    thread = threading.Thread(target=create)
    thread.start()
    assert validation_started.wait(timeout=5)
    client.close()
    release_validation.set()
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert delivered == []
    assert len(failures) == 1
    assert failures[0].error.code is CoreClientLocalErrorCodeV1.CLIENT_CLOSED


def test_project_bootstrap_rejects_a_previously_published_workspace() -> None:
    connection = _bootstrap_connection()
    request = _project_create_request()
    publication = _publication()
    stale = _project(publication=publication).model_copy(
        update={"id": "project-stale-published-workspace"}
    )

    def handler(http_request: httpx.Request) -> httpx.Response:
        if http_request.url.path == "/version":
            return httpx.Response(200, json=_version_payload())
        return httpx.Response(201, json=stale.model_dump(mode="json"))

    client = CoreProjectBootstrapClientV1(
        connection,
        transport=httpx.MockTransport(handler),
    )
    client.version()
    with pytest.raises(CoreClientErrorV1) as exc_info:
        client.create_project(request, idempotency_key="bootstrap-stale-workspace-0001")

    assert exc_info.value.error.code is CoreClientLocalErrorCodeV1.INVALID_RESPONSE
    client.close()


def test_project_bootstrap_refuses_a_different_request_after_success() -> None:
    connection = _bootstrap_connection()
    request = _project_create_request()
    created = _project().model_copy(update={"id": "project-created-by-core"})
    create_calls = 0

    def handler(http_request: httpx.Request) -> httpx.Response:
        nonlocal create_calls
        if http_request.url.path == "/version":
            return httpx.Response(200, json=_version_payload())
        create_calls += 1
        return httpx.Response(201, json=created.model_dump(mode="json"))

    client = CoreProjectBootstrapClientV1(
        connection,
        transport=httpx.MockTransport(handler),
    )
    client.version()
    client.create_project(request, idempotency_key="bootstrap-project-0004")

    changed = request.model_copy(update={"name": "Changed request"})
    with pytest.raises(CoreClientErrorV1) as changed_request:
        client.create_project(changed, idempotency_key="bootstrap-project-0004")
    with pytest.raises(CoreClientErrorV1) as changed_key:
        client.create_project(request, idempotency_key="bootstrap-project-other")
    assert changed_request.value.error.code is CoreClientLocalErrorCodeV1.INVALID_REQUEST
    assert changed_key.value.error.code is CoreClientLocalErrorCodeV1.INVALID_REQUEST
    assert create_calls == 1
    client.close()


def test_client_disables_environment_transport_and_redirects() -> None:
    client = _client(lambda _request: httpx.Response(200, json=_health_payload()))
    try:
        assert client._http.trust_env is False
        assert client._http.follow_redirects is False
    finally:
        client.close()


def test_authenticated_calls_require_successful_frozen_version_negotiation() -> None:
    called = False

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json={})

    client = _client(handler, negotiate=False)
    try:
        with pytest.raises(CoreClientErrorV1) as exc_info:
            client.status()
    finally:
        client.close()

    assert called is False
    assert exc_info.value.error.code is CoreClientLocalErrorCodeV1.INVALID_CONNECTION


@pytest.mark.parametrize(
    ("digest", "provider_kind", "build_channel"),
    [
        ("f" * 64, "openevo_core", "release"),
        (CORE_OPENAPI_SHA256, "contract_simulator", "release"),
        (CORE_OPENAPI_SHA256, "scaffold", "release"),
        (CORE_OPENAPI_SHA256, "dry_run", "release"),
        (CORE_OPENAPI_SHA256, "future_provider", "release"),
        (CORE_OPENAPI_SHA256, "openevo_core", "development"),
    ],
)
def test_version_rejects_non_release_or_non_frozen_core_provider(
    digest: str,
    provider_kind: str,
    build_channel: str,
) -> None:
    client = CoreControlClientV1(
        _connection(),
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                json=_version_payload(
                    digest=digest,
                    provider_kind=provider_kind,
                    build_channel=build_channel,
                ),
            )
        ),
    )
    try:
        with pytest.raises(CoreClientErrorV1) as exc_info:
            client.version()
    finally:
        client.close()

    assert exc_info.value.error.code is CoreClientLocalErrorCodeV1.INVALID_RESPONSE


def test_version_pin_rejects_a_changed_release_identity() -> None:
    responses = iter(
        [
            _version_payload(),
            {**_version_payload(), "source_commit": "abcdef01"},
        ]
    )
    client = CoreControlClientV1(
        _connection(),
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=next(responses))),
    )
    try:
        assert client.version().openapi_sha256 == CORE_OPENAPI_SHA256
        with pytest.raises(CoreClientErrorV1) as exc_info:
            client.version()
    finally:
        client.close()

    assert exc_info.value.error.code is CoreClientLocalErrorCodeV1.INVALID_RESPONSE


@pytest.mark.parametrize(
    "timeout",
    [None, float("inf"), 0.0, 301.0, httpx.Timeout(None), httpx.Timeout(5.0, read=None)],
)
def test_client_rejects_unbounded_or_invalid_total_deadlines(timeout: object) -> None:
    with pytest.raises(CoreClientErrorV1) as exc_info:
        CoreControlClientV1(
            _connection(),
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(200, json=_health_payload())
            ),
            timeout=timeout,  # type: ignore[arg-type]
        )

    assert exc_info.value.error.code is CoreClientLocalErrorCodeV1.INVALID_CONNECTION


def test_total_deadline_interrupts_blocking_send() -> None:
    entered = threading.Event()
    release = threading.Event()

    def handler(_request: httpx.Request) -> httpx.Response:
        entered.set()
        release.wait(1)
        return httpx.Response(200, json=_health_payload())

    client = CoreControlClientV1(
        _connection(), transport=httpx.MockTransport(handler), timeout=0.05
    )
    started_at = time.monotonic()
    try:
        with pytest.raises(CoreClientErrorV1) as exc_info:
            client.health()
        elapsed = time.monotonic() - started_at
    finally:
        release.set()
        client.close()

    assert entered.is_set()
    assert elapsed < 0.25
    assert exc_info.value.error.code is CoreClientLocalErrorCodeV1.CONNECTION_FAILED


def test_total_deadline_stops_trickle_body_without_replaying_mutation() -> None:
    class TrickleStream(httpx.SyncByteStream):
        def __iter__(self):
            for chunk in (b"{", b'"schema_version":"1"}'):
                time.sleep(0.04)
                yield chunk

    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            headers={"Content-Type": "application/json"},
            stream=TrickleStream(),
        )

    client = _client(handler, timeout=0.06)
    request = v1.EnvironmentDoctorRequestV1(
        execution_mode=v1.ExecutionMode.SELF_DEPLOYED,
        checks=[v1.EnvironmentCheckKind.PYTHON],
    )
    started_at = time.monotonic()
    try:
        with pytest.raises(CoreClientErrorV1) as exc_info:
            client.environment_doctor(request, idempotency_key="doctor-deadline-1")
        elapsed = time.monotonic() - started_at
    finally:
        client.close()

    assert calls == 1
    assert elapsed < 0.25
    assert exc_info.value.error.code is CoreClientLocalErrorCodeV1.CONNECTION_FAILED


def test_redirect_is_rejected_without_reading_a_trickle_body() -> None:
    class UnreadRedirectBody(httpx.SyncByteStream):
        read = False

        def __iter__(self):
            self.read = True
            time.sleep(1)
            yield b"redirect"

    body = UnreadRedirectBody()
    client = _client(
        lambda _request: httpx.Response(
            307,
            headers={"Location": "http://127.0.0.1:48765/other"},
            stream=body,
        ),
        connection=_connection(),
        timeout=0.05,
    )
    started_at = time.monotonic()
    try:
        with pytest.raises(CoreClientErrorV1) as exc_info:
            client.status()
        elapsed = time.monotonic() - started_at
    finally:
        client.close()

    assert body.read is False
    assert elapsed < 0.25
    assert exc_info.value.error.code is CoreClientLocalErrorCodeV1.REDIRECT_REJECTED


def test_success_body_is_bounded_by_declared_content_length_before_streaming() -> None:
    class UnreadStream(httpx.SyncByteStream):
        read = False

        def __iter__(self):
            self.read = True
            yield b"{}"

    stream = UnreadStream()
    client = _client(
        lambda _request: httpx.Response(
            200,
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(MAX_CORE_JSON_RESPONSE_BYTES + 1),
            },
            stream=stream,
        )
    )

    with pytest.raises(CoreClientErrorV1) as exc_info:
        client.health()

    assert stream.read is False
    assert exc_info.value.error.code is CoreClientLocalErrorCodeV1.RESPONSE_TOO_LARGE


def test_chunked_success_body_is_bounded_while_streaming() -> None:
    class OversizedStream(httpx.SyncByteStream):
        def __iter__(self):
            yield b"x" * MAX_CORE_JSON_RESPONSE_BYTES
            yield b"x"

    client = _client(
        lambda _request: httpx.Response(
            200,
            headers={"Content-Type": "application/json"},
            stream=OversizedStream(),
        )
    )

    with pytest.raises(CoreClientErrorV1) as exc_info:
        client.health()

    assert exc_info.value.error.code is CoreClientLocalErrorCodeV1.RESPONSE_TOO_LARGE


def test_error_body_is_bounded_before_typed_error_parsing() -> None:
    class UnreadErrorStream(httpx.SyncByteStream):
        read = False

        def __iter__(self):
            self.read = True
            yield b"{}"

    stream = UnreadErrorStream()
    client = _client(
        lambda _request: httpx.Response(
            409,
            headers={
                "Content-Type": "application/json",
                "Content-Length": str((64 * 1024) + 1),
            },
            stream=stream,
        )
    )

    with pytest.raises(CoreClientErrorV1) as exc_info:
        client.status()

    assert stream.read is False
    assert exc_info.value.error.code is CoreClientLocalErrorCodeV1.RESPONSE_TOO_LARGE


def test_retry_response_loss_after_send_is_an_unknown_mutation_outcome() -> None:
    failed = _failed_run()

    class LostCommittedResponse(httpx.SyncByteStream):
        def __iter__(self):
            yield failed.model_dump_json().encode("utf-8")
            raise httpx.ReadError("injected post-commit response loss")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=failed.model_dump(mode="json"))
        assert request.url.path == "/v1/runs/run-1/retry"
        return httpx.Response(
            202,
            headers={"Content-Type": "application/json"},
            stream=LostCommittedResponse(),
        )

    client = _client(handler)
    client.get_run(failed.id, project_id=PROJECT_ID)

    with pytest.raises(CoreMutationOutcomeUnknownV1) as unknown:
        client.retry_run(
            failed.id,
            v1.RunRetryRequestV1(terminal_attempt_id=failed.current_attempt_id),
            project_id=PROJECT_ID,
            if_match=failed.etag,
            idempotency_key="retry-response-loss-0001",
        )

    assert unknown.value.code == "core_mutation_outcome_unknown"
    assert unknown.value.__cause__ is None
    assert unknown.value.__context__ is None


def test_retry_typed_http_rejection_remains_deterministic() -> None:
    failed = _failed_run()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=failed.model_dump(mode="json"))
        return httpx.Response(409, json=_api_error_payload())

    client = _client(handler)
    client.get_run(failed.id, project_id=PROJECT_ID)

    with pytest.raises(CoreClientErrorV1) as rejected:
        client.retry_run(
            failed.id,
            v1.RunRetryRequestV1(terminal_attempt_id=failed.current_attempt_id),
            project_id=PROJECT_ID,
            if_match=failed.etag,
            idempotency_key="retry-typed-rejection-0001",
        )

    assert isinstance(rejected.value.error, v1.ApiErrorV1)
    assert rejected.value.status_code == 409


@pytest.mark.parametrize("failure", ["send", "parse"])
def test_retry_send_or_success_parse_failure_is_an_unknown_outcome(failure: str) -> None:
    failed = _failed_run()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=failed.model_dump(mode="json"))
        if failure == "send":
            raise httpx.WriteError("injected retry send failure", request=request)
        return httpx.Response(
            202,
            headers={"Content-Type": "application/json"},
            content=b"not-json",
        )

    client = _client(handler)
    client.get_run(failed.id, project_id=PROJECT_ID)

    with pytest.raises(CoreMutationOutcomeUnknownV1) as unknown:
        client.retry_run(
            failed.id,
            v1.RunRetryRequestV1(terminal_attempt_id=failed.current_attempt_id),
            project_id=PROJECT_ID,
            if_match=failed.etag,
            idempotency_key=f"retry-{failure}-failure-0001",
        )

    assert unknown.value.code == "core_mutation_outcome_unknown"


def test_retry_pre_send_validation_failure_remains_deterministic() -> None:
    failed = _failed_run()
    post_called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal post_called
        if request.method == "GET":
            return httpx.Response(200, json=failed.model_dump(mode="json"))
        post_called = True
        return httpx.Response(202, json=failed.model_dump(mode="json"))

    client = _client(handler)
    client.get_run(failed.id, project_id=PROJECT_ID)

    with pytest.raises(CoreClientErrorV1) as rejected:
        client.retry_run(
            failed.id,
            v1.RunCancelRequestV1(reason=v1.RunCancelReason.USER_REQUESTED),  # type: ignore[arg-type]
            project_id=PROJECT_ID,
            if_match=failed.etag,
            idempotency_key="retry-pre-send-invalid-0001",
        )

    assert post_called is False
    assert rejected.value.error.code is CoreClientLocalErrorCodeV1.INVALID_REQUEST


def test_retry_cross_wired_success_is_an_unknown_outcome() -> None:
    failed = _failed_run()
    wrong_attempt = failed.attempts[0].model_copy(update={"run_id": "run-cross-wired"})
    cross_wired = failed.model_copy(
        update={
            "id": "run-cross-wired",
            "current_attempt": wrong_attempt,
            "current_attempt_id": wrong_attempt.id,
            "attempts": [wrong_attempt],
        }
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=failed.model_dump(mode="json"))
        return httpx.Response(202, json=cross_wired.model_dump(mode="json"))

    client = _client(handler)
    client.get_run(failed.id, project_id=PROJECT_ID)

    with pytest.raises(CoreMutationOutcomeUnknownV1):
        client.retry_run(
            failed.id,
            v1.RunRetryRequestV1(terminal_attempt_id=failed.current_attempt_id),
            project_id=PROJECT_ID,
            if_match=failed.etag,
            idempotency_key="retry-cross-wired-response-0001",
        )


def test_retry_registration_batch_exit_race_is_an_unknown_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import desktop.sidecar.core_client_v1 as core_client_module

    monkeypatch.setattr(core_client_module, "MAX_CORE_CLOSE_WAIT_SECONDS", 0.05)
    failed = _failed_run()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200 if request.method == "GET" else 202, json=failed.model_dump(mode="json")
        )

    client = _client(handler)
    client.get_run(failed.id, project_id=PROJECT_ID)
    original_batch = client._registration_batch
    transaction_ready = threading.Event()
    release_transaction = threading.Event()
    results: list[v1.RunV1] = []
    errors: list[BaseException] = []

    @contextmanager
    def pause_before_retry_transaction_exit(*args, **kwargs):
        with original_batch(*args, **kwargs):
            yield
            transaction_ready.set()
            release_transaction.wait(1)

    def retry() -> None:
        try:
            results.append(
                client.retry_run(
                    failed.id,
                    v1.RunRetryRequestV1(terminal_attempt_id=failed.current_attempt_id),
                    project_id=PROJECT_ID,
                    if_match=failed.etag,
                    idempotency_key="retry-batch-exit-race-0001",
                )
            )
        except BaseException as exc:
            errors.append(exc)

    monkeypatch.setattr(client, "_registration_batch", pause_before_retry_transaction_exit)
    request_thread = threading.Thread(target=retry)
    request_thread.start()
    assert transaction_ready.wait(1)
    client.close()
    release_transaction.set()
    request_thread.join(1)

    assert not request_thread.is_alive()
    assert results == []
    assert len(errors) == 1
    assert isinstance(errors[0], CoreMutationOutcomeUnknownV1)
    assert errors[0].__cause__ is None
    assert errors[0].__context__ is None


@pytest.mark.parametrize(
    "content",
    [
        b"not-json",
        b"\xff",
        b'[{"status":"ok"}]',
        json.dumps({**_health_payload(), "unknown": True}).encode(),
        json.dumps({**_health_payload(), "ready": 1}).encode(),
    ],
    ids=["invalid-json", "invalid-utf8", "wrong-shape", "extra-field", "coerced-type"],
)
def test_success_requires_exact_strict_response_dto(content: bytes) -> None:
    client = _client(
        lambda _request: httpx.Response(
            200,
            headers={"Content-Type": "application/json"},
            content=content,
        )
    )

    with pytest.raises(CoreClientErrorV1) as exc_info:
        client.health()

    assert exc_info.value.error.code is CoreClientLocalErrorCodeV1.INVALID_RESPONSE
    decoded = content.decode(errors="ignore")
    if decoded:
        assert decoded not in str(exc_info.value)


def test_non_bytes_json_chunk_is_normalized_to_typed_response_error() -> None:
    class NonBytesStream(httpx.SyncByteStream):
        def __iter__(self):
            yield "private malformed chunk"

    client = _client(
        lambda _request: httpx.Response(
            200,
            headers={"Content-Type": "application/json"},
            stream=NonBytesStream(),
        )
    )
    try:
        with pytest.raises(CoreClientErrorV1) as exc_info:
            client.health()
    finally:
        client.close()

    assert exc_info.value.error.code is CoreClientLocalErrorCodeV1.INVALID_RESPONSE
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None


def test_non_bytes_sse_chunk_is_normalized_to_typed_protocol_error() -> None:
    class NonBytesStream(httpx.SyncByteStream):
        def __iter__(self):
            yield "private malformed chunk"

    client = _client(
        lambda _request: httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            stream=NonBytesStream(),
        )
    )
    try:
        with client.events() as stream:
            with pytest.raises(CoreClientErrorV1) as exc_info:
                list(stream)
    finally:
        client.close()

    assert exc_info.value.error.code is CoreClientLocalErrorCodeV1.SSE_PROTOCOL_ERROR
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None


@pytest.mark.parametrize("use_sse", [False, True])
def test_response_is_owned_when_deadline_expires_immediately_after_send(
    monkeypatch,
    use_sse: bool,
) -> None:
    import desktop.sidecar.core_client_v1 as core_client_module

    response_closed = threading.Event()

    class TrackingBody(httpx.SyncByteStream):
        def __iter__(self):
            if use_sse:
                yield b""
            else:
                yield json.dumps(_health_payload()).encode("utf-8")

        def close(self) -> None:
            response_closed.set()

    client = _client(
        lambda _request: httpx.Response(
            200,
            headers={"Content-Type": ("text/event-stream" if use_sse else "application/json")},
            stream=TrackingBody(),
        )
    )
    original_send = core_client_module._send_before_deadline
    original_check = core_client_module._check_deadline
    expire_after_send = threading.Event()

    def tracked_send(*args, **kwargs):
        response = original_send(*args, **kwargs)
        expire_after_send.set()
        return response

    def controlled_check(deadline: float) -> None:
        if expire_after_send.is_set():
            core_client_module._raise_local(CoreClientLocalErrorCodeV1.CONNECTION_FAILED, 503)
        original_check(deadline)

    monkeypatch.setattr(core_client_module, "_send_before_deadline", tracked_send)
    monkeypatch.setattr(core_client_module, "_check_deadline", controlled_check)
    try:
        with pytest.raises(CoreClientErrorV1) as exc_info:
            if use_sse:
                with client.events():
                    pass
            else:
                client.health()
    finally:
        client.close()

    assert exc_info.value.error.code is CoreClientLocalErrorCodeV1.CONNECTION_FAILED
    assert response_closed.wait(1)


def test_capabilities_accepts_standard_json_arrays_from_model_dump_json() -> None:
    capabilities = _capabilities()
    client = _client(
        lambda _request: httpx.Response(
            200,
            headers={"Content-Type": "application/json"},
            content=capabilities.model_dump_json(),
        )
    )

    assert client.capabilities(v1.ExecutionMode.SELF_DEPLOYED) == capabilities


@pytest.mark.parametrize(
    "update",
    [
        {"unknown": True},
        {"core_version": 1},
        {"targets": {}},
    ],
    ids=["unknown-field", "wrong-scalar-type", "wrong-collection-type"],
)
def test_capabilities_still_rejects_non_contract_json(update: dict[str, object]) -> None:
    payload = _capabilities().model_dump(mode="json")
    payload.update(update)
    client = _client(lambda _request: httpx.Response(200, json=payload))

    with pytest.raises(CoreClientErrorV1) as exc_info:
        client.capabilities(v1.ExecutionMode.SELF_DEPLOYED)

    assert exc_info.value.error.code is CoreClientLocalErrorCodeV1.INVALID_RESPONSE


def test_capabilities_response_profile_matches_requested_release_mode() -> None:
    capabilities = _capabilities(v1.ExecutionMode.CODEX_SUBSCRIPTION_TRANSCRIPT)
    client = _client(
        lambda _request: httpx.Response(200, json=capabilities.model_dump(mode="json"))
    )

    with pytest.raises(CoreClientErrorV1) as exc_info:
        client.capabilities(v1.ExecutionMode.SELF_DEPLOYED)

    assert exc_info.value.error.code is CoreClientLocalErrorCodeV1.INVALID_RESPONSE


def test_capabilities_recursively_rejects_scalar_coercion_in_nested_schema() -> None:
    payload = _full_capabilities().model_dump(mode="json")
    payload["targets"][0]["context_order"] = True
    client = _client(lambda _request: httpx.Response(200, json=payload))

    with pytest.raises(CoreClientErrorV1) as exc_info:
        client.capabilities(v1.ExecutionMode.SELF_DEPLOYED)

    assert exc_info.value.error.code is CoreClientLocalErrorCodeV1.INVALID_RESPONSE


def test_capabilities_pin_session_registry_and_execution_profile_authority() -> None:
    capabilities = _capabilities()
    changed_registry = capabilities.model_copy(update={"registry_digest": "5" * 64})
    subscription = _capabilities(v1.ExecutionMode.CODEX_SUBSCRIPTION_TRANSCRIPT)
    responses = iter([capabilities, changed_registry, subscription])
    client = _client(
        lambda _request: httpx.Response(
            200,
            json=next(responses).model_dump(mode="json"),
        )
    )

    assert client.capabilities(v1.ExecutionMode.SELF_DEPLOYED) == capabilities
    with pytest.raises(CoreClientErrorV1) as changed_digest:
        client.capabilities(v1.ExecutionMode.SELF_DEPLOYED)
    with pytest.raises(CoreClientErrorV1) as changed_profile:
        client.capabilities(v1.ExecutionMode.CODEX_SUBSCRIPTION_TRANSCRIPT)

    assert changed_digest.value.error.code is CoreClientLocalErrorCodeV1.INVALID_RESPONSE
    assert changed_profile.value.error.code is CoreClientLocalErrorCodeV1.INVALID_RESPONSE


@pytest.mark.parametrize(
    "project_first", [False, True], ids=["capabilities-first", "project-first"]
)
def test_capabilities_and_cached_project_bind_registry_digest_in_either_order(
    project_first: bool,
) -> None:
    capabilities = _capabilities()
    project = _project(registry_digest=capabilities.registry_digest)

    def handler(request: httpx.Request) -> httpx.Response:
        payload = project if request.url.path.startswith("/v1/projects/") else capabilities
        return httpx.Response(200, json=payload.model_dump(mode="json"))

    client = _client(handler)
    if project_first:
        assert client.get_project() == project
        assert client.capabilities(v1.ExecutionMode.SELF_DEPLOYED) == capabilities
    else:
        assert client.capabilities(v1.ExecutionMode.SELF_DEPLOYED) == capabilities
        assert client.get_project() == project

    assert client._project_state == project
    assert client._capability_authority is not None
    assert client._capability_authority.registry_digest == capabilities.registry_digest


@pytest.mark.parametrize(
    "project_first", [False, True], ids=["capabilities-first", "project-first"]
)
def test_capabilities_and_cached_project_reject_registry_mismatch_in_either_order(
    project_first: bool,
) -> None:
    capabilities = _capabilities()
    project = _project(registry_digest="5" * 64)

    def handler(request: httpx.Request) -> httpx.Response:
        payload = project if request.url.path.startswith("/v1/projects/") else capabilities
        return httpx.Response(200, json=payload.model_dump(mode="json"))

    client = _client(handler)
    if project_first:
        assert client.get_project() == project
        with pytest.raises(CoreClientErrorV1) as exc_info:
            client.capabilities(v1.ExecutionMode.SELF_DEPLOYED)
        assert client._project_state == project
        assert client._capability_authority is None
    else:
        assert client.capabilities(v1.ExecutionMode.SELF_DEPLOYED) == capabilities
        authority = client._capability_authority
        with pytest.raises(CoreClientErrorV1) as exc_info:
            client.get_project()
        assert client._project_state is None
        assert client._capability_authority == authority

    assert exc_info.value.error.code is CoreClientLocalErrorCodeV1.INVALID_RESPONSE


def test_validation_and_run_admission_require_exact_capability_authority() -> None:
    capabilities = _capabilities()
    run = _run()
    validation = v1.ProjectValidationResponseV1(
        valid=True,
        registry_digest=capabilities.registry_digest,
        checks=[],
        validated_at="2026-07-14T12:00:00Z",
    )
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path == "/v1/capabilities":
            return httpx.Response(200, json=capabilities.model_dump(mode="json"))
        if request.url.path.endswith("/validate"):
            return httpx.Response(200, json=validation.model_dump(mode="json"))
        return httpx.Response(202, json=run.model_dump(mode="json"))

    client = _client(handler)
    client.capabilities(v1.ExecutionMode.SELF_DEPLOYED)
    wrong_validation = v1.ProjectValidationRequestV1(
        project_snapshot=run.project_snapshot,
        workspace_snapshot=run.workspace_snapshot,
        expected_registry_digest="5" * 64,
    )
    wrong_run = _run_create(run).model_copy(update={"expected_registry_digest": "5" * 64})

    with pytest.raises(CoreClientErrorV1) as validation_error:
        client.validate_project(wrong_validation, idempotency_key="validate-authority")
    with pytest.raises(CoreClientErrorV1) as run_error:
        client.create_run(wrong_run, idempotency_key="run-authority")

    assert validation_error.value.error.code is CoreClientLocalErrorCodeV1.INVALID_REQUEST
    assert run_error.value.error.code is CoreClientLocalErrorCodeV1.INVALID_REQUEST
    assert seen == ["/v1/capabilities"]


def test_run_response_execution_mode_must_match_capability_profile() -> None:
    capabilities = _capabilities()
    run = _run().model_copy(
        update={"execution_mode": v1.ExecutionMode.CODEX_SUBSCRIPTION_TRANSCRIPT}
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/capabilities":
            return httpx.Response(200, json=capabilities.model_dump(mode="json"))
        return httpx.Response(202, json=run.model_dump(mode="json"))

    client = _client(handler)
    client.capabilities(v1.ExecutionMode.SELF_DEPLOYED)

    with pytest.raises(CoreClientErrorV1) as exc_info:
        client.create_run(_run_create(run), idempotency_key="run-profile")

    assert exc_info.value.error.code is CoreClientLocalErrorCodeV1.INVALID_RESPONSE


def test_valid_api_error_is_returned_as_exact_contract_type() -> None:
    payload = _api_error_payload()
    client = _client(lambda _request: httpx.Response(409, json=payload))

    with pytest.raises(CoreClientErrorV1) as exc_info:
        client.status()

    assert exc_info.value.status_code == 409
    assert isinstance(exc_info.value.error, v1.ApiErrorV1)
    assert exc_info.value.error == v1.ApiErrorV1.model_validate_json(
        json.dumps(payload), strict=True
    )


@pytest.mark.parametrize(
    ("status", "payload"),
    [
        (409, "not-json"),
        (409, {"code": "partial"}),
        (409, {**_api_error_payload(), "unknown": "field"}),
        (412, _api_error_payload(409)),
    ],
)
def test_invalid_error_body_becomes_safe_local_typed_error(
    status: int,
    payload: object,
) -> None:
    response = (
        httpx.Response(status, text=payload)
        if isinstance(payload, str)
        else httpx.Response(status, json=payload)
    )
    client = _client(lambda _request: response)

    with pytest.raises(CoreClientErrorV1) as exc_info:
        client.status()

    assert isinstance(exc_info.value.error, CoreClientLocalErrorV1)
    assert exc_info.value.error.code is CoreClientLocalErrorCodeV1.INVALID_ERROR_RESPONSE
    assert "partial" not in str(exc_info.value)


def test_echoed_bearer_is_never_exposed_even_inside_otherwise_typed_error() -> None:
    token = _token()
    payload = {**_api_error_payload(), "message": f"Bearer {token}"}
    client = _client(
        lambda _request: httpx.Response(409, json=payload),
        connection=_connection(token=token),
    )

    with pytest.raises(CoreClientErrorV1) as exc_info:
        client.status()

    assert isinstance(exc_info.value.error, CoreClientLocalErrorV1)
    assert token not in str(exc_info.value)
    assert token not in repr(exc_info.value.error)


def test_mutation_serializes_exact_request_dto_and_required_headers() -> None:
    request_model = v1.EnvironmentDoctorRequestV1(
        execution_mode=v1.ExecutionMode.SELF_DEPLOYED,
        checks=[v1.EnvironmentCheckKind.PYTHON],
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/v1/environment/doctor"
        assert request.headers["idempotency-key"] == "doctor-1"
        assert "if-match" not in request.headers
        assert request.headers["content-type"] == "application/json"
        assert request.content == request_model.model_dump_json().encode()
        return httpx.Response(
            200,
            json={
                "schema_version": "1",
                "status": "ok",
                "checks": [],
                "checked_at": "2026-07-14T12:00:00Z",
            },
        )

    with _client(handler) as client:
        response = client.environment_doctor(request_model, idempotency_key="doctor-1")

    assert response.status is v1.DoctorStatus.OK


def test_environment_repair_maps_to_final_operation_resource() -> None:
    request_model = v1.EnvironmentRepairRequestV1(
        execution_mode=v1.ExecutionMode.SELF_DEPLOYED,
        actions=[v1.EnvironmentRepairAction.RETRY_NETWORK],
    )
    operation = _queued_environment_operation(request_model)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/v1/environment/repair"
        assert request.headers["idempotency-key"] == "repair-1"
        return httpx.Response(
            202,
            headers={"Content-Type": "application/json"},
            content=operation.model_dump_json(),
        )

    with _client(handler) as client:
        result = client.environment_repair(request_model, idempotency_key="repair-1")

    assert result == operation
    assert result.request.request == request_model


def test_project_validation_response_binds_expected_registry_digest() -> None:
    run = _run()
    request = v1.ProjectValidationRequestV1(
        project_snapshot=run.project_snapshot,
        workspace_snapshot=run.workspace_snapshot,
        expected_registry_digest="5" * 64,
    )
    response = v1.ProjectValidationResponseV1(
        valid=True,
        registry_digest="6" * 64,
        checks=[],
        validated_at="2026-07-14T12:00:00Z",
    )
    capabilities = _capabilities().model_copy(update={"registry_digest": "5" * 64})

    def handler(request: httpx.Request) -> httpx.Response:
        payload = capabilities if request.url.path == "/v1/capabilities" else response
        return httpx.Response(200, json=payload.model_dump(mode="json"))

    client = _client(handler)
    client.capabilities(v1.ExecutionMode.SELF_DEPLOYED)

    with pytest.raises(CoreClientErrorV1) as exc_info:
        client.validate_project(request, idempotency_key="validate-1")

    assert exc_info.value.error.code is CoreClientLocalErrorCodeV1.INVALID_RESPONSE


def test_run_create_response_binds_expected_registry_digest() -> None:
    run = _run()
    request = _run_create(run).model_copy(update={"expected_registry_digest": "5" * 64})
    capabilities = _capabilities().model_copy(update={"registry_digest": "5" * 64})

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/capabilities":
            return httpx.Response(200, json=capabilities.model_dump(mode="json"))
        return httpx.Response(202, json=run.model_dump(mode="json"))

    client = _client(handler)
    client.capabilities(v1.ExecutionMode.SELF_DEPLOYED)

    with pytest.raises(CoreClientErrorV1) as exc_info:
        client.create_run(request, idempotency_key="run-1")

    assert exc_info.value.error.code is CoreClientLocalErrorCodeV1.INVALID_RESPONSE


def test_final_mutation_routes_send_exact_precondition_headers() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.method == "GET" and "workspace-uploads" in request.url.path:
            return httpx.Response(200, json=_upload(accepted_offset=1024).model_dump(mode="json"))
        if request.method == "GET" and "/operations/" in request.url.path:
            operation = _queued_environment_operation(
                v1.EnvironmentRepairRequestV1(
                    execution_mode=v1.ExecutionMode.SELF_DEPLOYED,
                    actions=[v1.EnvironmentRepairAction.RETRY_NETWORK],
                )
            )
            return httpx.Response(200, json=operation.model_dump(mode="json"))
        status = 201 if request.url.path.endswith("/finalize") else 202
        return httpx.Response(status, json={})

    client = _client(handler)
    client.get_workspace_upload("upload-1")
    with pytest.raises(CoreClientErrorV1):
        client.finalize_workspace_upload(
            "upload-1",
            v1.WorkspaceUploadFinalizeV1(content_sha256="c" * 64),
            if_match='"' + ("b" * 64) + '"',
            if_project_match=ETAG,
            idempotency_key="finalize-1",
        )
    client.get_operation("operation-1")
    with pytest.raises(CoreClientErrorV1):
        client.cancel_operation(
            "operation-1",
            v1.OperationCancelRequestV1(reason=v1.OperationCancelReason.USER_REQUESTED),
            if_match=ETAG,
            idempotency_key="cancel-operation-1",
        )

    finalize = next(request for request in seen if request.url.path.endswith("/finalize"))
    cancel = next(request for request in seen if request.url.path.endswith("/cancel"))
    assert finalize.method == "POST"
    assert finalize.url.raw_path.decode() == (
        "/v1/projects/project%2Factive%3Fone/workspace-uploads/upload-1/finalize"
    )
    assert finalize.headers["if-match"] == '"' + ("b" * 64) + '"'
    assert finalize.headers["if-project-match"] == ETAG
    assert finalize.headers["idempotency-key"] == "finalize-1"
    assert cancel.method == "POST"
    assert cancel.url.raw_path.decode() == "/v1/operations/operation-1/cancel"
    assert cancel.headers["if-match"] == ETAG
    assert cancel.headers["idempotency-key"] == "cancel-operation-1"


def test_path_segments_are_escaped_and_other_project_is_rejected_before_transport() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.raw_path.decode())
        return httpx.Response(200, json={})

    client = _client(handler)
    with pytest.raises(CoreClientErrorV1) as invalid_response:
        client.get_project()
    with pytest.raises(CoreClientErrorV1) as wrong_project:
        client.get_project("project-other")

    assert paths == ["/v1/projects/project%2Factive%3Fone"]
    assert invalid_response.value.error.code is CoreClientLocalErrorCodeV1.INVALID_RESPONSE
    assert wrong_project.value.error.code is CoreClientLocalErrorCodeV1.ACTIVE_PROJECT_MISMATCH


def test_get_pagination_uses_only_closed_query_and_active_project_filter() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={})

    client = _client(handler)
    with pytest.raises(CoreClientErrorV1):
        client.list_runs(
            limit=25,
            after="cursor-1",
            sort="started_at",
            direction="asc",
            status=v1.RunStatus.RUNNING,
        )
    with pytest.raises(CoreClientErrorV1) as invalid_query:
        client.list_runs(sort="unknown")  # type: ignore[arg-type]

    assert dict(seen[0].url.params) == {
        "limit": "25",
        "after": "cursor-1",
        "sort": "started_at",
        "direction": "asc",
        "project_id": PROJECT_ID,
        "status": "running",
    }
    assert invalid_query.value.error.code is CoreClientLocalErrorCodeV1.INVALID_REQUEST
    assert len(seen) == 1


def test_private_values_are_rejected_from_path_query_header_and_body_before_transport() -> None:
    called = False
    token = _token()
    connection = _connection(token=token)

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json={})

    client = _client(handler, connection=connection)
    project = _project()
    create_request = v1.ProjectCreateV1(
        name=connection.session_id,
        spec=project.spec,
        task=project.task,
        workspace=project.workspace,
    )
    doctor_request = v1.EnvironmentDoctorRequestV1(
        execution_mode=v1.ExecutionMode.SELF_DEPLOYED,
        checks=[v1.EnvironmentCheckKind.PYTHON],
    )

    def open_private_cursor() -> None:
        with client.events(last_event_id=connection.session_id):
            pass

    operations = (
        lambda: client.get_service(connection.endpoint),
        lambda: client.list_services(after=connection.origin),
        lambda: client.environment_doctor(doctor_request, idempotency_key=token),
        lambda: client.create_project(create_request, idempotency_key="create-private-body-1"),
        open_private_cursor,
    )
    for operation in operations:
        with pytest.raises(CoreClientErrorV1) as exc_info:
            operation()
        assert exc_info.value.error.code is CoreClientLocalErrorCodeV1.INVALID_REQUEST

    assert called is False


def test_connection_rejects_private_session_identity_as_project_id() -> None:
    with pytest.raises(CoreClientErrorV1) as exc_info:
        CoreTunnelConnectionV1(
            endpoint="http://127.0.0.1:48765",
            bearer_token=_token(),
            project_id=SESSION_ID,
            session_id=SESSION_ID,
        )

    assert exc_info.value.error.code is CoreClientLocalErrorCodeV1.INVALID_CONNECTION


def test_run_admission_for_another_project_is_rejected_before_transport() -> None:
    called = False

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(202, json={})

    def snapshot(snapshot_id: str, kind: v1.SnapshotKind) -> v1.ImmutableSnapshotRefV1:
        return v1.ImmutableSnapshotRefV1(
            id=snapshot_id,
            kind=kind,
            content_sha256="d" * 64,
            created_at="2026-07-14T12:00:00Z",
        )

    revision = v1.RevisionRefV1(
        id="revision-other",
        project_id="project-other",
        generation=0,
        manifest_sha256="e" * 64,
    )
    request = v1.RunCreateV1(
        project_id="project-other",
        project_snapshot=snapshot("project-snapshot", v1.SnapshotKind.PROJECT),
        task_snapshot=snapshot("task-snapshot", v1.SnapshotKind.TASK),
        workspace_snapshot=snapshot("workspace-snapshot", v1.SnapshotKind.WORKSPACE),
        expected_registry_digest="f" * 64,
        required_revision=v1.ReachableRequiredRevisionRefV1(
            revision=revision,
            reachable_from_revision_id=revision.id,
            relation=v1.RequiredRevisionRelation.ACTIVE,
        ),
    )
    client = _client(handler)

    with pytest.raises(CoreClientErrorV1) as exc_info:
        client.create_run(request, idempotency_key="run-other")

    assert called is False
    assert exc_info.value.error.code is CoreClientLocalErrorCodeV1.ACTIVE_PROJECT_MISMATCH


def test_major_read_route_mapping_matches_final_core_app() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={})

    client = _client(handler)
    calls = [
        (lambda: client.status(), "/v1/status"),
        (
            lambda: client.capabilities(v1.ExecutionMode.SELF_DEPLOYED),
            "/v1/capabilities",
        ),
        (lambda: client.get_project(), "/v1/projects/project%2Factive%3Fone"),
        (
            lambda: client.list_revisions(),
            "/v1/projects/project%2Factive%3Fone/revisions",
        ),
        (
            lambda: client.revision_head(),
            "/v1/projects/project%2Factive%3Fone/revisions/head",
        ),
        (
            lambda: client.get_revision("revision/one", project_id=PROJECT_ID),
            "/v1/revisions/revision%2Fone",
        ),
        (
            lambda: client.get_workspace_upload("upload/one"),
            "/v1/projects/project%2Factive%3Fone/workspace-uploads/upload%2Fone",
        ),
        (lambda: client.list_runs(), "/v1/runs"),
        (
            lambda: client.get_run("run/one", project_id=PROJECT_ID),
            "/v1/runs/run%2Fone",
        ),
        (
            lambda: client.get_artifact("artifact/one", project_id=PROJECT_ID),
            "/v1/projects/project%2Factive%3Fone/artifacts/artifact%2Fone",
        ),
        (lambda: client.list_services(), "/v1/services"),
        (lambda: client.get_service("service/one"), "/v1/services/service%2Fone"),
        (
            lambda: client.get_operation("operation/one"),
            "/v1/operations/operation%2Fone",
        ),
        (
            lambda: client.get_diagnostic("diagnostic/one"),
            "/v1/diagnostics/diagnostic%2Fone",
        ),
    ]

    for call, expected_path in calls:
        with pytest.raises(CoreClientErrorV1):
            call()
        assert seen[-1].method == "GET"
        assert seen[-1].url.raw_path.decode().split("?", 1)[0] == expected_path


def test_artifact_diff_uses_final_document_changes_union() -> None:
    payload = {
        "schema_version": "1",
        "artifact_id": "artifact-current",
        "artifact_content_sha256": "a" * 64,
        "previous_artifact_id": "artifact-previous",
        "previous_artifact_content_sha256": "b" * 64,
        "document_changes": [],
        "total_document_changes": 0,
        "total_hunks": 0,
        "total_lines": 0,
        "truncated": False,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/v1/projects/{PROJECT_ID}/artifacts/artifact-current":
            return httpx.Response(
                200,
                json=_artifact(
                    "artifact-current",
                    digest="a" * 64,
                    source_artifact_ids=["artifact-previous"],
                ).model_dump(mode="json"),
            )
        if request.url.path == f"/v1/projects/{PROJECT_ID}/artifacts/artifact-previous":
            return httpx.Response(
                200, json=_artifact("artifact-previous", digest="b" * 64).model_dump(mode="json")
            )
        return httpx.Response(200, json=payload)

    client = _client(handler)
    client.get_artifact("artifact-current", project_id=PROJECT_ID)

    result = client.artifact_diff("artifact-current", project_id=PROJECT_ID)

    assert result == v1.ArtifactDiffV1.model_validate_json(json.dumps(payload), strict=True)
    assert result.document_changes == []


def test_artifact_diff_does_not_refetch_historical_detail() -> None:
    requested_paths: list[str] = []
    payload = {
        "schema_version": "1",
        "artifact_id": "artifact-current",
        "artifact_content_sha256": "a" * 64,
        "previous_artifact_id": "artifact-previous",
        "previous_artifact_content_sha256": "b" * 64,
        "document_changes": [],
        "total_document_changes": 0,
        "total_hunks": 0,
        "total_lines": 0,
        "truncated": False,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        requested_paths.append(request.url.path)
        if request.url.path.endswith("/artifact-current"):
            return httpx.Response(
                200,
                json=_artifact(
                    "artifact-current",
                    digest="a" * 64,
                    source_artifact_ids=["artifact-previous"],
                ).model_dump(mode="json"),
            )
        if request.url.path.endswith("/artifact-previous"):
            return httpx.Response(404, json=_api_error_payload(404))
        return httpx.Response(200, json=payload)

    client = _client(handler)
    client.get_artifact("artifact-current", project_id=PROJECT_ID)

    result = client.artifact_diff("artifact-current", project_id=PROJECT_ID)

    assert result.previous_artifact_id == "artifact-previous"
    assert not any(path.endswith("/artifact-previous") for path in requested_paths)


def test_artifact_diff_rejects_predecessor_outside_current_lineage() -> None:
    payload = {
        "schema_version": "1",
        "artifact_id": "artifact-current",
        "artifact_content_sha256": "a" * 64,
        "previous_artifact_id": "artifact-substituted",
        "previous_artifact_content_sha256": "b" * 64,
        "document_changes": [],
        "total_document_changes": 0,
        "total_hunks": 0,
        "total_lines": 0,
        "truncated": False,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/artifact-current"):
            return httpx.Response(
                200,
                json=_artifact(
                    "artifact-current",
                    digest="a" * 64,
                    source_artifact_ids=["artifact-authorized"],
                ).model_dump(mode="json"),
            )
        return httpx.Response(200, json=payload)

    client = _client(handler)
    client.get_artifact("artifact-current", project_id=PROJECT_ID)

    with pytest.raises(CoreClientErrorV1) as raised:
        client.artifact_diff("artifact-current", project_id=PROJECT_ID)

    assert raised.value.error.code is CoreClientLocalErrorCodeV1.INVALID_RESPONSE


@pytest.mark.parametrize(
    ("character", "repeat"),
    [
        ("\0", v1.MAX_ARTIFACT_PREVIEW_UTF8_BYTES),
        ("\n", v1.MAX_ARTIFACT_PREVIEW_UTF8_BYTES),
        ("界", v1.MAX_ARTIFACT_PREVIEW_UTF8_BYTES // 3),
    ],
)
def test_artifact_content_response_budget_covers_worst_case_json_escaping(
    character: str,
    repeat: int,
) -> None:
    content = character * repeat
    content_bytes = content.encode("utf-8")
    digest = hashlib.sha256(content_bytes).hexdigest()
    empty_digest = hashlib.sha256(b"").hexdigest()
    documents = [
        v1.ArtifactDocumentPreviewV1(
            document_id="document-1",
            display_name="SKILL.md",
            relative_path="SKILL.md",
            mime_type="text/markdown",
            content=content,
            content_sha256=digest,
            byte_size=len(content_bytes),
            truncated=False,
        )
    ]
    for index in range(1, v1.MAX_ARTIFACT_PREVIEW_DOCUMENTS):
        documents.append(
            v1.ArtifactDocumentPreviewV1(
                document_id=f"document-{index}",
                display_name=f"Document {index}",
                relative_path=f"document-{index}.md",
                mime_type="text/markdown",
                content="",
                content_sha256=empty_digest,
                byte_size=0,
                truncated=False,
            )
        )
    payload = v1.ArtifactContentV1(
        artifact_id="artifact-current",
        artifact_type=v1.ArtifactType.SKILL_BUNDLE,
        documents=documents,
        total_documents=v1.MAX_ARTIFACT_PREVIEW_DOCUMENTS,
        total_utf8_bytes=len(content_bytes),
        returned_utf8_bytes=len(content_bytes),
        truncated=False,
    )
    response_bytes = json.dumps(
        payload.model_dump(mode="json"),
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")
    assert len(response_bytes) <= MAX_CORE_ARTIFACT_RESPONSE_BYTES
    if character == "\0":
        assert len(response_bytes) > MAX_CORE_JSON_RESPONSE_BYTES

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/artifact-current"):
            return httpx.Response(
                200,
                json=_artifact("artifact-current", digest="a" * 64).model_dump(mode="json"),
            )
        return httpx.Response(
            200,
            content=response_bytes,
            headers={"content-type": "application/json"},
        )

    client = _client(handler)
    client.get_artifact("artifact-current", project_id=PROJECT_ID)
    result = client.artifact_content("artifact-current", project_id=PROJECT_ID)

    assert result.returned_utf8_bytes == len(content_bytes)
    assert result.documents[0].content == content


def test_artifact_diff_response_budget_covers_maximum_escaped_text_and_lines() -> None:
    old_document = v1.ArtifactDiffDocumentIdentityV1(
        artifact_id="artifact-previous",
        artifact_content_sha256="b" * 64,
        document_id="d" * 128,
        relative_path="p" * 256,
        content_sha256="c" * 64,
    )
    new_document = v1.ArtifactDiffDocumentIdentityV1(
        artifact_id="artifact-current",
        artifact_content_sha256="a" * 64,
        document_id="e" * 128,
        relative_path="p" * 256,
        content_sha256="d" * 64,
    )
    changes: list[v1.ModifiedArtifactDocumentChangeV1] = []
    next_line = 1
    for _ in range(v1.MAX_ARTIFACT_DIFF_HUNKS):
        lines = [
            v1.ArtifactDiffLineV1(
                kind=v1.DiffLineKind.ADDED,
                new_line_number=next_line + index,
                text="\0" * 256,
            )
            for index in range(64)
        ]
        hunk = v1.ArtifactDiffHunkV1(
            old_document=old_document,
            new_document=new_document,
            old_start=0,
            old_count=0,
            new_start=next_line,
            new_count=64,
            lines=lines,
        )
        changes.append(
            v1.ModifiedArtifactDocumentChangeV1(
                kind=v1.ArtifactDocumentChangeKind.MODIFIED,
                old_document=old_document,
                new_document=new_document,
                hunks=[hunk],
            )
        )
        next_line += 64
    payload = v1.ArtifactDiffV1(
        artifact_id="artifact-current",
        artifact_content_sha256="a" * 64,
        previous_artifact_id="artifact-previous",
        previous_artifact_content_sha256="b" * 64,
        document_changes=changes,
        total_document_changes=v1.MAX_ARTIFACT_PREVIEW_DOCUMENTS,
        total_hunks=v1.MAX_ARTIFACT_DIFF_HUNKS,
        total_lines=v1.MAX_ARTIFACT_DIFF_LINES,
        truncated=False,
    )
    response_bytes = json.dumps(
        payload.model_dump(mode="json"),
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")
    assert len(response_bytes) <= MAX_CORE_ARTIFACT_RESPONSE_BYTES
    assert len(response_bytes) > MAX_CORE_JSON_RESPONSE_BYTES

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/artifact-current"):
            return httpx.Response(
                200,
                json=_artifact(
                    "artifact-current",
                    digest="a" * 64,
                    source_artifact_ids=["artifact-previous"],
                ).model_dump(mode="json"),
            )
        return httpx.Response(
            200,
            content=response_bytes,
            headers={"content-type": "application/json"},
        )

    client = _client(handler)
    client.get_artifact("artifact-current", project_id=PROJECT_ID)
    result = client.artifact_diff("artifact-current", project_id=PROJECT_ID)

    assert result.total_lines == v1.MAX_ARTIFACT_DIFF_LINES
    assert result.document_changes[-1].hunks[-1].lines[-1].text == "\0" * 256


def test_close_is_idempotent_and_prevents_new_requests() -> None:
    called = False

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json=_health_payload())

    client = _client(handler)
    client.close()
    client.close()

    with pytest.raises(CoreClientErrorV1) as exc_info:
        client.health()

    assert called is False
    assert exc_info.value.error.code is CoreClientLocalErrorCodeV1.CLIENT_CLOSED


def test_connection_failures_do_not_expose_transport_message() -> None:
    secret_path = "/private/token/file"

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"failed at {secret_path}", request=request)

    client = _client(handler)
    with pytest.raises(CoreClientErrorV1) as exc_info:
        client.health()

    assert exc_info.value.error.code is CoreClientLocalErrorCodeV1.CONNECTION_FAILED
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None
    assert secret_path not in str(exc_info.value)
    assert secret_path not in repr(exc_info.value.error)


def test_close_failure_is_typed_and_has_no_raw_exception_chain() -> None:
    class CloseFailureTransport(httpx.BaseTransport):
        def handle_request(self, request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_health_payload(), request=request)

        def close(self) -> None:
            raise RuntimeError("private close failure")

    client = CoreControlClientV1(_connection(), transport=CloseFailureTransport())
    assert client.health().ready is True

    with pytest.raises(CoreClientErrorV1) as exc_info:
        client.close()

    assert exc_info.value.error.code is CoreClientLocalErrorCodeV1.CLIENT_CLOSED
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None
    assert "private close failure" not in str(exc_info.value)


def test_close_deadline_covers_blocking_transport_close(monkeypatch) -> None:
    import desktop.sidecar.core_client_v1 as core_client_module

    monkeypatch.setattr(core_client_module, "MAX_CORE_CLOSE_WAIT_SECONDS", 0.05)

    class BlockingCloseTransport(httpx.BaseTransport):
        def __init__(self) -> None:
            self.started = threading.Event()
            self.release = threading.Event()

        def handle_request(self, request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_health_payload(), request=request)

        def close(self) -> None:
            self.started.set()
            self.release.wait(1)

    transport = BlockingCloseTransport()
    client = CoreControlClientV1(_connection(), transport=transport)
    assert client.health().ready is True

    started_at = time.monotonic()
    try:
        client.close()
        elapsed = time.monotonic() - started_at
    finally:
        transport.release.set()

    assert transport.started.is_set()
    assert elapsed < 0.25
    with pytest.raises(CoreClientErrorV1):
        client.health()


def test_close_deadline_covers_blocking_response_close(monkeypatch) -> None:
    import desktop.sidecar.core_client_v1 as core_client_module

    monkeypatch.setattr(core_client_module, "MAX_CORE_CLOSE_WAIT_SECONDS", 0.05)

    class BlockingCloseStream(httpx.SyncByteStream):
        def __init__(self) -> None:
            self.started = threading.Event()
            self.release = threading.Event()

        def __iter__(self):
            yield b""

        def close(self) -> None:
            self.started.set()
            self.release.wait(1)

    body = BlockingCloseStream()
    client = _client(
        lambda _request: httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            stream=body,
        )
    )

    with client.events():
        started_at = time.monotonic()
        try:
            client.close()
            elapsed = time.monotonic() - started_at
        finally:
            body.release.set()

    assert body.started.is_set()
    assert elapsed < 0.25
    with pytest.raises(CoreClientErrorV1):
        client.health()


def test_close_generation_rejects_json_body_released_after_linearization() -> None:
    class BlockingJsonStream(httpx.SyncByteStream):
        def __init__(self) -> None:
            self.read_started = threading.Event()
            self.release_body = threading.Event()
            self.close_called = threading.Event()

        def __iter__(self):
            self.read_started.set()
            self.release_body.wait(1)
            yield json.dumps(_health_payload()).encode()

        def close(self) -> None:
            self.close_called.set()

    body = BlockingJsonStream()
    client = _client(
        lambda _request: httpx.Response(
            200,
            headers={"Content-Type": "application/json"},
            stream=body,
        )
    )
    results: list[v1.HealthResponseV1] = []
    errors: list[CoreClientErrorV1] = []
    close_errors: list[CoreClientErrorV1] = []

    def read_health() -> None:
        try:
            results.append(client.health())
        except CoreClientErrorV1 as exc:
            errors.append(exc)

    def close_client() -> None:
        try:
            client.close()
        except CoreClientErrorV1 as exc:
            close_errors.append(exc)

    request_thread = threading.Thread(target=read_health)
    close_thread = threading.Thread(target=close_client)
    request_thread.start()
    assert body.read_started.wait(1)
    close_thread.start()
    assert body.close_called.wait(1)
    body.release_body.set()
    request_thread.join(1)
    close_thread.join(1)

    assert not request_thread.is_alive()
    assert not close_thread.is_alive()
    assert results == []
    assert close_errors == []
    assert errors[0].error.code is CoreClientLocalErrorCodeV1.CLIENT_CLOSED


def test_close_generation_rejects_public_validation_cache_and_return(monkeypatch) -> None:
    import desktop.sidecar.core_client_v1 as core_client_module

    monkeypatch.setattr(core_client_module, "MAX_CORE_CLOSE_WAIT_SECONDS", 0.05)
    capabilities = _capabilities()
    client = _client(
        lambda _request: httpx.Response(200, json=capabilities.model_dump(mode="json"))
    )
    original_json = client._json
    json_returned = threading.Event()
    release_validation = threading.Event()
    close_attempted = threading.Event()
    close_returned = threading.Event()
    results: list[v1.CapabilitiesResponseV1] = []
    request_errors: list[CoreClientErrorV1] = []
    close_errors: list[CoreClientErrorV1] = []

    def pause_after_json(*args, **kwargs):
        result = original_json(*args, **kwargs)
        json_returned.set()
        release_validation.wait(1)
        return result

    def read_capabilities() -> None:
        try:
            results.append(client.capabilities(v1.ExecutionMode.SELF_DEPLOYED))
        except CoreClientErrorV1 as exc:
            request_errors.append(exc)

    def close_client() -> None:
        close_attempted.set()
        try:
            client.close()
        except CoreClientErrorV1 as exc:
            close_errors.append(exc)
        finally:
            close_returned.set()

    monkeypatch.setattr(client, "_json", pause_after_json)
    request_thread = threading.Thread(target=read_capabilities)
    close_thread = threading.Thread(target=close_client)
    request_thread.start()
    assert json_returned.wait(1)
    close_thread.start()
    assert close_attempted.wait(1)
    assert close_returned.wait(0.25)
    release_validation.set()
    request_thread.join(1)
    close_thread.join(1)

    assert not request_thread.is_alive()
    assert not close_thread.is_alive()
    assert results == []
    assert request_errors[0].error.code is CoreClientLocalErrorCodeV1.CLIENT_CLOSED
    assert close_errors == []
    assert client._capability_authority is None


def test_generation_lease_exit_rejects_json_after_final_delivery_check(monkeypatch) -> None:
    import desktop.sidecar.core_client_v1 as core_client_module

    monkeypatch.setattr(core_client_module, "MAX_CORE_CLOSE_WAIT_SECONDS", 0.05)
    capabilities = _capabilities()
    client = _client(
        lambda _request: httpx.Response(
            200,
            json=capabilities.model_dump(mode="json"),
        )
    )
    original_linearize = client._linearize_generation_result
    final_check_passed = threading.Event()
    release_return = threading.Event()
    results: list[v1.CapabilitiesResponseV1] = []
    errors: list[CoreClientErrorV1] = []

    def pause_after_final_check(generation: int, deadline: float | None = None) -> None:
        original_linearize(generation, deadline)
        if deadline is not None and not final_check_passed.is_set():
            final_check_passed.set()
            release_return.wait(1)

    def read_capabilities() -> None:
        try:
            results.append(client.capabilities(v1.ExecutionMode.SELF_DEPLOYED))
        except CoreClientErrorV1 as exc:
            errors.append(exc)

    monkeypatch.setattr(client, "_linearize_generation_result", pause_after_final_check)
    request_thread = threading.Thread(target=read_capabilities)
    request_thread.start()
    assert final_check_passed.wait(1)
    client.close()
    release_return.set()
    request_thread.join(1)

    assert not request_thread.is_alive()
    assert results == []
    assert errors[0].error.code is CoreClientLocalErrorCodeV1.CLIENT_CLOSED
    assert client._capability_authority is None
    assert client._leases == 0
    assert client._lease_owners == {}


def test_close_seals_json_in_post_lease_cache_transaction_window(monkeypatch) -> None:
    capabilities = _capabilities()
    client = _client(
        lambda _request: httpx.Response(
            200,
            json=capabilities.model_dump(mode="json"),
        )
    )
    original_batch = client._registration_batch
    transaction_ready = threading.Event()
    release_transaction = threading.Event()
    depth = threading.local()
    results: list[v1.CapabilitiesResponseV1] = []
    errors: list[CoreClientErrorV1] = []

    @contextmanager
    def pause_before_outer_transaction_exit(*args, **kwargs):
        current_depth = getattr(depth, "value", 0) + 1
        depth.value = current_depth
        try:
            with original_batch(*args, **kwargs):
                yield
                if current_depth == 1:
                    transaction_ready.set()
                    release_transaction.wait(1)
        finally:
            depth.value = current_depth - 1

    def read_capabilities() -> None:
        try:
            results.append(client.capabilities(v1.ExecutionMode.SELF_DEPLOYED))
        except CoreClientErrorV1 as exc:
            errors.append(exc)

    monkeypatch.setattr(client, "_registration_batch", pause_before_outer_transaction_exit)
    request_thread = threading.Thread(target=read_capabilities)
    request_thread.start()
    assert transaction_ready.wait(1)

    started_at = time.monotonic()
    client.close()
    close_elapsed = time.monotonic() - started_at
    release_transaction.set()
    request_thread.join(1)

    assert not request_thread.is_alive()
    assert close_elapsed < 0.25
    assert results == []
    assert errors[0].error.code is CoreClientLocalErrorCodeV1.CLIENT_CLOSED
    assert client._capability_authority is None
    assert client._leases == 0
    assert client._lease_owners == {}


def test_artifact_diff_cannot_hold_close_past_deadline(monkeypatch) -> None:
    import desktop.sidecar.core_client_v1 as core_client_module

    monkeypatch.setattr(core_client_module, "MAX_CORE_CLOSE_WAIT_SECONDS", 0.05)
    payload = {
        "schema_version": "1",
        "artifact_id": "artifact-current",
        "artifact_content_sha256": "a" * 64,
        "previous_artifact_id": "artifact-previous",
        "previous_artifact_content_sha256": "b" * 64,
        "document_changes": [],
        "total_document_changes": 0,
        "total_hunks": 0,
        "total_lines": 0,
        "truncated": False,
    }
    diff_request_started = threading.Event()
    release_diff_request = threading.Event()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/v1/projects/{PROJECT_ID}/artifacts/artifact-current":
            artifact = _artifact(
                "artifact-current",
                digest="a" * 64,
                source_artifact_ids=["artifact-previous"],
            )
            return httpx.Response(200, json=artifact.model_dump(mode="json"))
        if request.url.path.endswith("/artifact-current/diff"):
            diff_request_started.set()
            release_diff_request.wait(1)
        return httpx.Response(200, json=payload)

    client = _client(handler)
    client.get_artifact("artifact-current", project_id=PROJECT_ID)
    results: list[v1.ArtifactDiffV1] = []
    request_errors: list[CoreClientErrorV1] = []
    close_errors: list[CoreClientErrorV1] = []
    close_elapsed: list[float] = []

    def read_diff() -> None:
        try:
            results.append(client.artifact_diff("artifact-current", project_id=PROJECT_ID))
        except CoreClientErrorV1 as exc:
            request_errors.append(exc)

    def close_client() -> None:
        started_at = time.monotonic()
        try:
            client.close()
        except CoreClientErrorV1 as exc:
            close_errors.append(exc)
        finally:
            close_elapsed.append(time.monotonic() - started_at)

    request_thread = threading.Thread(target=read_diff)
    close_thread = threading.Thread(target=close_client)
    request_thread.start()
    assert diff_request_started.wait(1)
    close_thread.start()
    try:
        close_thread.join(0.25)
        assert not close_thread.is_alive()
    finally:
        release_diff_request.set()
        request_thread.join(1)
        close_thread.join(1)

    assert results == []
    assert request_errors[0].error.code is CoreClientLocalErrorCodeV1.CLIENT_CLOSED
    assert close_errors == []
    assert close_elapsed[0] < 0.25
    assert "artifact-previous" not in client._artifacts


def test_blocking_closers_share_one_process_fixed_thread_budget(monkeypatch) -> None:
    import desktop.sidecar.core_client_v1 as core_client_module

    monkeypatch.setattr(core_client_module, "MAX_CORE_CLOSE_WAIT_SECONDS", 0.05)

    class CloseTrackingTransport(httpx.BaseTransport):
        def __init__(self) -> None:
            self.close_started = threading.Event()
            self.release_close = threading.Event()

        def handle_request(self, request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_health_payload(), request=request)

        def close(self) -> None:
            self.close_started.set()
            self.release_close.wait(1)

    transports = [CloseTrackingTransport() for _ in range(12)]
    clients = [CoreControlClientV1(_connection(), transport=transport) for transport in transports]
    worker_ids_before = {
        thread.ident
        for thread in threading.enumerate()
        if thread.name.startswith("openevo-core-resource-closer-")
    }
    closers = [threading.Thread(target=client.close) for client in clients]
    for closer in closers:
        closer.start()
    try:
        for closer in closers:
            closer.join(0.25)
        assert all(not closer.is_alive() for closer in closers)
        assert sum(transport.close_started.is_set() for transport in transports) == (
            core_client_module.CORE_CLOSE_WORKER_COUNT
        )
        worker_ids_after = {
            thread.ident
            for thread in threading.enumerate()
            if thread.name.startswith("openevo-core-resource-closer-")
        }
        assert worker_ids_after == worker_ids_before
        assert len(worker_ids_after) == core_client_module.CORE_CLOSE_WORKER_COUNT
        assert not any(
            thread.name == "openevo-core-resource-owner" for thread in threading.enumerate()
        )
    finally:
        for transport in transports:
            transport.release_close.set()

    deadline = time.monotonic() + 1
    while any(client._close_tasks_pending for client in clients) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert all(client._close_tasks_pending == 0 for client in clients)


def test_global_close_capacity_retains_response_ownership_and_gates_transport(
    monkeypatch,
) -> None:
    import desktop.sidecar.core_client_v1 as core_client_module

    closer = core_client_module._BoundedResourceCloser(worker_count=1, capacity=3)
    monkeypatch.setattr(core_client_module, "_PROCESS_RESOURCE_CLOSER", closer)
    blocker_started = threading.Event()
    release_blocker = threading.Event()
    response_closed = threading.Event()
    non_version_requests: list[str] = []

    class TrackingSse(httpx.SyncByteStream):
        def __iter__(self):
            yield b""

        def close(self) -> None:
            response_closed.set()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/version":
            return httpx.Response(200, json=_version_payload())
        non_version_requests.append(request.url.path)
        return httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            stream=TrackingSse(),
        )

    client = CoreControlClientV1(
        _connection(),
        transport=httpx.MockTransport(handler),
    )
    assert client.version().openapi_sha256 == CORE_OPENAPI_SHA256
    deadline = time.monotonic() + 1
    while closer.owned_count != 1 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert closer.owned_count == 1

    reservation = closer.reserve()
    assert reservation is not None

    def block_close() -> None:
        blocker_started.set()
        release_blocker.wait(1)

    assert reservation.submit(block_close) is True
    assert blocker_started.wait(1)

    with client.events():
        pass
    assert response_closed.is_set() is False
    assert closer.owned_count == 3

    with pytest.raises(CoreClientErrorV1) as saturated:
        client.status()
    assert saturated.value.error.code is CoreClientLocalErrorCodeV1.CONNECTION_FAILED
    assert non_version_requests == ["/v1/events"]

    release_blocker.set()
    assert response_closed.wait(1)
    client.close()


def test_close_action_failure_seals_future_lease_admission() -> None:
    close_attempted = threading.Event()
    post_failure_transport = False

    class FailingCloseStream(httpx.SyncByteStream):
        def __iter__(self):
            yield b""

        def close(self) -> None:
            close_attempted.set()
            raise TypeError("private close detail")

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal post_failure_transport
        if request.url.path == "/v1/events":
            return httpx.Response(
                200,
                headers={"Content-Type": "text/event-stream"},
                stream=FailingCloseStream(),
            )
        post_failure_transport = True
        return httpx.Response(200, json=_health_payload())

    client = _client(handler)
    with client.events():
        pass
    assert close_attempted.wait(1)
    deadline = time.monotonic() + 1
    while not client._close_failed and time.monotonic() < deadline:
        time.sleep(0.01)
    assert client._close_failed is True

    with pytest.raises(CoreClientErrorV1) as exc_info:
        client.health()

    assert post_failure_transport is False
    assert exc_info.value.error.code is CoreClientLocalErrorCodeV1.CLIENT_CLOSED
    with pytest.raises(CoreClientErrorV1):
        client.close()


def test_late_response_close_is_handed_to_bounded_closer_without_state_lock(
    monkeypatch,
) -> None:
    import desktop.sidecar.core_client_v1 as core_client_module

    monkeypatch.setattr(core_client_module, "MAX_CORE_CLOSE_WAIT_SECONDS", 0.05)
    handler_entered = threading.Event()
    release_handler = threading.Event()

    class BlockingCloseStream(httpx.SyncByteStream):
        def __init__(self) -> None:
            self.close_started = threading.Event()
            self.release_close = threading.Event()

        def __iter__(self):
            yield json.dumps(_health_payload()).encode()

        def close(self) -> None:
            self.close_started.set()
            self.release_close.wait(1)

    body = BlockingCloseStream()

    def handler(_request: httpx.Request) -> httpx.Response:
        handler_entered.set()
        release_handler.wait(1)
        return httpx.Response(200, headers={"Content-Type": "application/json"}, stream=body)

    client = _client(handler)
    errors: list[CoreClientErrorV1] = []

    def request_health() -> None:
        try:
            client.health()
        except CoreClientErrorV1 as exc:
            errors.append(exc)

    request_thread = threading.Thread(target=request_health)
    request_thread.start()
    assert handler_entered.wait(1)
    client.close()
    release_handler.set()
    try:
        request_thread.join(0.25)
        assert not request_thread.is_alive()
        assert body.close_started.wait(0.25)
        assert client._state.acquire(timeout=0.1)
        client._state.release()
    finally:
        body.release_close.set()
        request_thread.join(1)

    assert errors[0].error.code is CoreClientLocalErrorCodeV1.CLIENT_CLOSED


def test_normal_sse_context_exit_uses_bounded_response_closer() -> None:
    class BlockingCloseStream(httpx.SyncByteStream):
        def __init__(self) -> None:
            self.started = threading.Event()
            self.release = threading.Event()

        def __iter__(self):
            yield b""

        def close(self) -> None:
            self.started.set()
            self.release.wait(1)

    body = BlockingCloseStream()
    client = _client(
        lambda _request: httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            stream=body,
        )
    )

    started_at = time.monotonic()
    try:
        with client.events():
            pass
        elapsed = time.monotonic() - started_at
        assert body.started.wait(0.25)
    finally:
        body.release.set()
        client.close()

    assert elapsed < 0.25


def test_sse_stream_validates_and_yields_final_wire_frame() -> None:
    payload = {
        "schema_version": "1",
        "id": "event-1",
        "sequence": 1,
        "occurred_at": "2026-07-14T12:00:00Z",
        "event": "heartbeat.v1",
        "payload": {"active_run_count": 0},
    }
    data = json.dumps(payload, separators=(",", ":")).encode()
    frame = b"id: event-1\nevent: heartbeat.v1\ndata: " + data + b"\n\n"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["accept"] == "text/event-stream"
        assert request.headers["last-event-id"] == "event-0"
        return httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            content=frame,
        )

    with _client(handler) as client:
        with client.events(last_event_id="event-0") as stream:
            events = list(stream)

    assert len(events) == 1
    assert isinstance(events[0], v1.SseFrameV1)
    assert events[0].event == "heartbeat.v1"
    assert events[0].data.root.event == "heartbeat.v1"


def test_close_generation_rejects_late_sse_frame_without_replay_update() -> None:
    first = {
        "schema_version": "1",
        "id": "event-1",
        "sequence": 1,
        "occurred_at": "2026-07-14T12:00:00Z",
        "event": "heartbeat.v1",
        "payload": {"active_run_count": 0},
    }
    second = {**first, "id": "event-2", "sequence": 2}

    class BlockingSseStream(httpx.SyncByteStream):
        def __init__(self) -> None:
            self.second_read_started = threading.Event()
            self.release_second = threading.Event()

        def __iter__(self):
            yield _sse_bytes(first)
            self.second_read_started.set()
            self.release_second.wait(1)
            yield _sse_bytes(second)

    body = BlockingSseStream()
    client = _client(
        lambda _request: httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            stream=body,
        )
    )
    late_frames: list[v1.SseFrameV1] = []
    errors: list[CoreClientErrorV1] = []

    with client.events() as stream:
        iterator = iter(stream)
        assert next(iterator).id == "event-1"

        def read_late_frame() -> None:
            try:
                late_frames.append(next(iterator))
            except CoreClientErrorV1 as exc:
                errors.append(exc)

        reader = threading.Thread(target=read_late_frame)
        reader.start()
        assert body.second_read_started.wait(1)
        client.close()
        body.release_second.set()
        reader.join(1)

    assert not reader.is_alive()
    assert late_frames == []
    assert errors[0].error.code is CoreClientLocalErrorCodeV1.CLIENT_CLOSED
    assert "event-1" in client._sse_event_digests
    assert "event-2" not in client._sse_event_digests


def test_sse_seal_before_delivery_linearization_rejects_frame(monkeypatch) -> None:
    payload = {
        "schema_version": "1",
        "id": "event-1",
        "sequence": 1,
        "occurred_at": "2026-07-14T12:00:00Z",
        "event": "heartbeat.v1",
        "payload": {"active_run_count": 0},
    }
    client = _client(
        lambda _request: httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            content=_sse_bytes(payload),
        )
    )
    original_linearize = client._linearize_sse_frame_delivery
    delivery_ready = threading.Event()
    release_delivery = threading.Event()
    frames: list[v1.SseFrameV1] = []
    errors: list[CoreClientErrorV1] = []

    def pause_before_linearization(frame: v1.SseFrameV1, generation: int) -> None:
        delivery_ready.set()
        release_delivery.wait(1)
        original_linearize(frame, generation)

    monkeypatch.setattr(client, "_linearize_sse_frame_delivery", pause_before_linearization)

    with client.events() as stream:
        iterator = iter(stream)

        def read_frame() -> None:
            try:
                frames.append(next(iterator))
            except CoreClientErrorV1 as exc:
                errors.append(exc)

        reader = threading.Thread(target=read_frame)
        reader.start()
        assert delivery_ready.wait(1)
        client.close()
        release_delivery.set()
        reader.join(1)

    assert not reader.is_alive()
    assert frames == []
    assert errors[0].error.code is CoreClientLocalErrorCodeV1.CLIENT_CLOSED
    assert "event-1" not in client._sse_event_digests


def test_sse_delivery_linearized_before_seal_is_not_yielded_after_close(monkeypatch) -> None:
    payload = {
        "schema_version": "1",
        "id": "event-1",
        "sequence": 1,
        "occurred_at": "2026-07-14T12:00:00Z",
        "event": "heartbeat.v1",
        "payload": {"active_run_count": 0},
    }
    client = _client(
        lambda _request: httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            content=_sse_bytes(payload),
        )
    )
    original_linearize = client._linearize_sse_frame_delivery
    delivery_linearized = threading.Event()
    release_yield = threading.Event()
    frames: list[v1.SseFrameV1] = []
    errors: list[CoreClientErrorV1] = []

    def pause_after_linearization(frame: v1.SseFrameV1, generation: int) -> None:
        original_linearize(frame, generation)
        delivery_linearized.set()
        release_yield.wait(1)

    monkeypatch.setattr(client, "_linearize_sse_frame_delivery", pause_after_linearization)

    with client.events() as stream:
        iterator = iter(stream)

        def read_frame() -> None:
            try:
                frames.append(next(iterator))
            except CoreClientErrorV1 as exc:
                errors.append(exc)

        reader = threading.Thread(target=read_frame)
        reader.start()
        assert delivery_linearized.wait(1)
        started_at = time.monotonic()
        client.close()
        close_elapsed = time.monotonic() - started_at
        release_yield.set()
        reader.join(1)

    assert not reader.is_alive()
    assert close_elapsed < 0.25
    assert frames == []
    assert errors[0].error.code is CoreClientLocalErrorCodeV1.CLIENT_CLOSED


def test_sse_next_exit_rejects_frame_after_final_delivery_check(monkeypatch) -> None:
    payload = {
        "schema_version": "1",
        "id": "event-final-check",
        "sequence": 1,
        "occurred_at": "2026-07-14T12:00:00Z",
        "event": "heartbeat.v1",
        "payload": {"active_run_count": 0},
    }
    client = _client(
        lambda _request: httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            content=_sse_bytes(payload),
        )
    )
    original_linearize = client._linearize_generation_result
    final_check_passed = threading.Event()
    release_yield = threading.Event()
    frames: list[v1.SseFrameV1] = []
    errors: list[CoreClientErrorV1] = []

    def pause_after_final_check(generation: int, deadline: float | None = None) -> None:
        original_linearize(generation, deadline)
        if (
            deadline is not None
            and "event-final-check" in client._sse_event_digests
            and not final_check_passed.is_set()
        ):
            final_check_passed.set()
            release_yield.wait(1)

    monkeypatch.setattr(client, "_linearize_generation_result", pause_after_final_check)
    with client.events() as stream:

        def read_frame() -> None:
            try:
                frames.append(next(iter(stream)))
            except CoreClientErrorV1 as exc:
                errors.append(exc)

        reader = threading.Thread(target=read_frame)
        reader.start()
        assert final_check_passed.wait(1)
        client.close()
        release_yield.set()
        reader.join(1)

    assert not reader.is_alive()
    assert frames == []
    assert errors[0].error.code is CoreClientLocalErrorCodeV1.CLIENT_CLOSED
    assert "event-final-check" not in client._sse_event_digests


def test_close_seals_sse_in_post_lease_cache_transaction_window(monkeypatch) -> None:
    payload = {
        "schema_version": "1",
        "id": "event-post-lease",
        "sequence": 1,
        "occurred_at": "2026-07-14T12:00:00Z",
        "event": "heartbeat.v1",
        "payload": {"active_run_count": 0},
    }
    client = _client(
        lambda _request: httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            content=_sse_bytes(payload),
        )
    )
    original_batch = client._registration_batch
    transaction_ready = threading.Event()
    release_transaction = threading.Event()
    depth = threading.local()
    frames: list[v1.SseFrameV1] = []
    errors: list[CoreClientErrorV1] = []

    @contextmanager
    def pause_before_outer_transaction_exit(*args, **kwargs):
        current_depth = getattr(depth, "value", 0) + 1
        depth.value = current_depth
        try:
            with original_batch(*args, **kwargs):
                yield
                if current_depth == 1:
                    transaction_ready.set()
                    release_transaction.wait(1)
        finally:
            depth.value = current_depth - 1

    monkeypatch.setattr(client, "_registration_batch", pause_before_outer_transaction_exit)
    with client.events() as stream:

        def read_frame() -> None:
            try:
                frames.append(next(stream))
            except CoreClientErrorV1 as exc:
                errors.append(exc)

        reader = threading.Thread(target=read_frame)
        reader.start()
        assert transaction_ready.wait(1)

        started_at = time.monotonic()
        client.close()
        close_elapsed = time.monotonic() - started_at
        release_transaction.set()
        reader.join(1)

    assert not reader.is_alive()
    assert close_elapsed < 0.25
    assert frames == []
    assert errors[0].error.code is CoreClientLocalErrorCodeV1.CLIENT_CLOSED
    assert "event-post-lease" not in client._sse_event_digests
    assert client._leases == 0
    assert client._lease_owners == {}


def test_sse_wait_does_not_hold_the_client_cache_transaction_lock() -> None:
    payload = {
        "schema_version": "1",
        "id": "event-concurrent-read",
        "sequence": 1,
        "occurred_at": "2026-07-14T12:00:00Z",
        "event": "heartbeat.v1",
        "payload": {"active_run_count": 0},
    }
    read_started = threading.Event()
    release_read = threading.Event()
    health_transport_reached = threading.Event()

    class BlockingSse(httpx.SyncByteStream):
        def __iter__(self):
            read_started.set()
            release_read.wait(1)
            yield _sse_bytes(payload)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/events":
            return httpx.Response(
                200,
                headers={"Content-Type": "text/event-stream"},
                stream=BlockingSse(),
            )
        health_transport_reached.set()
        return httpx.Response(200, json=_health_payload())

    client = _client(handler)
    reader: threading.Thread | None = None
    health_reader: threading.Thread | None = None
    frame_results: list[v1.SseFrameV1] = []
    health_results: list[v1.HealthResponseV1] = []
    reached_while_sse_waited = False
    try:
        with client.events() as stream:
            reader = threading.Thread(target=lambda: frame_results.append(next(stream)))
            reader.start()
            assert read_started.wait(1)
            health_reader = threading.Thread(target=lambda: health_results.append(client.health()))
            health_reader.start()
            reached_while_sse_waited = health_transport_reached.wait(0.2)
            release_read.set()
            reader.join(1)
            health_reader.join(1)
    finally:
        release_read.set()
        if reader is not None:
            reader.join(1)
        if health_reader is not None:
            health_reader.join(1)
        client.close()

    assert reached_while_sse_waited is True
    assert len(frame_results) == 1
    assert len(health_results) == 1


def test_sse_event_id_is_bound_to_canonical_payload_across_reconnects() -> None:
    first = {
        "schema_version": "1",
        "id": "event-1",
        "sequence": 1,
        "occurred_at": "2026-07-14T12:00:00Z",
        "event": "heartbeat.v1",
        "payload": {"active_run_count": 0},
    }
    changed = {**first, "payload": {"active_run_count": 1}}
    alternate_data = json.dumps(first, sort_keys=True).encode()
    alternate_frame = b"id: event-1\nevent: heartbeat.v1\ndata: " + alternate_data + b"\n\n"
    frames = iter([_sse_bytes(first), alternate_frame, _sse_bytes(changed)])
    client = _client(
        lambda _request: httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            content=next(frames),
        )
    )

    with client.events() as stream:
        assert len(list(stream)) == 1
    with client.events(last_event_id="event-1") as stream:
        assert len(list(stream)) == 1
    with client.events(last_event_id="event-1") as stream:
        with pytest.raises(CoreClientErrorV1) as exc_info:
            list(stream)

    assert exc_info.value.error.code is CoreClientLocalErrorCodeV1.SSE_PROTOCOL_ERROR


def test_identical_sse_replay_is_digest_checked_without_reapplying_state(
    monkeypatch,
) -> None:
    payload = {
        "schema_version": "1",
        "id": "event-1",
        "sequence": 1,
        "occurred_at": "2026-07-14T12:00:00Z",
        "event": "heartbeat.v1",
        "payload": {"active_run_count": 0},
    }
    alternate = json.dumps(payload, sort_keys=True).encode()
    frames = iter(
        [
            _sse_bytes(payload),
            b"id: event-1\nevent: heartbeat.v1\ndata: " + alternate + b"\n\n",
        ]
    )
    client = _client(
        lambda _request: httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            content=next(frames),
        )
    )
    applications: list[v1.EventEnvelopeV1] = []
    original = client._validate_event_membership

    def count_application(envelope: v1.EventEnvelopeV1) -> None:
        applications.append(envelope)
        original(envelope)

    monkeypatch.setattr(client, "_validate_event_membership", count_application)

    with client.events() as stream:
        assert len(list(stream)) == 1
    with client.events(last_event_id="event-1") as stream:
        assert len(list(stream)) == 1

    assert len(applications) == 1


def test_sse_event_identity_ledger_fails_closed_at_bound(monkeypatch) -> None:
    import desktop.sidecar.core_client_v1 as core_client_module

    monkeypatch.setattr(core_client_module, "MAX_CORE_SSE_EVENT_BINDINGS", 1)
    first = {
        "schema_version": "1",
        "id": "event-1",
        "sequence": 1,
        "occurred_at": "2026-07-14T12:00:00Z",
        "event": "heartbeat.v1",
        "payload": {"active_run_count": 0},
    }
    second = {**first, "id": "event-2", "sequence": 2}
    client = _client(
        lambda _request: httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            content=_sse_bytes(first) + _sse_bytes(second),
        )
    )

    with client.events() as stream:
        iterator = iter(stream)
        assert next(iterator).id == "event-1"
        with pytest.raises(CoreClientErrorV1) as exc_info:
            next(iterator)

    assert exc_info.value.error.code is CoreClientLocalErrorCodeV1.SSE_PROTOCOL_ERROR


def test_sse_rejects_cross_wired_metadata_without_rebuilding_payload() -> None:
    payload = {
        "schema_version": "1",
        "id": "event-1",
        "sequence": 1,
        "occurred_at": "2026-07-14T12:00:00Z",
        "event": "heartbeat.v1",
        "payload": {"active_run_count": 0},
    }
    frame = b"id: different\nevent: heartbeat.v1\ndata: " + json.dumps(payload).encode() + b"\n\n"
    client = _client(
        lambda _request: httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            content=frame,
        )
    )

    with client.events() as stream:
        with pytest.raises(CoreClientErrorV1) as exc_info:
            list(stream)

    assert exc_info.value.error.code is CoreClientLocalErrorCodeV1.SSE_PROTOCOL_ERROR


def test_sse_frame_memory_is_bounded() -> None:
    class OversizedSse(httpx.SyncByteStream):
        def __iter__(self):
            yield b"data: " + (b"x" * MAX_CORE_SSE_FRAME_BYTES)

    client = _client(
        lambda _request: httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            stream=OversizedSse(),
        )
    )

    with client.events() as stream:
        with pytest.raises(CoreClientErrorV1) as exc_info:
            list(stream)

    assert exc_info.value.error.code is CoreClientLocalErrorCodeV1.SSE_PROTOCOL_ERROR


def test_sse_declared_stream_limit_is_checked_before_reading() -> None:
    class UnreadSse(httpx.SyncByteStream):
        read = False

        def __iter__(self):
            self.read = True
            yield b""

    stream = UnreadSse()
    client = _client(
        lambda _request: httpx.Response(
            200,
            headers={
                "Content-Type": "text/event-stream",
                "Content-Length": str(MAX_CORE_SSE_RESPONSE_BYTES + 1),
            },
            stream=stream,
        )
    )

    with pytest.raises(CoreClientErrorV1) as exc_info:
        with client.events():
            pass

    assert stream.read is False
    assert exc_info.value.error.code is CoreClientLocalErrorCodeV1.RESPONSE_TOO_LARGE


def test_sse_trickle_cannot_extend_total_deadline() -> None:
    first = {
        "schema_version": "1",
        "id": "event-1",
        "sequence": 1,
        "occurred_at": "2026-07-14T12:00:00Z",
        "event": "heartbeat.v1",
        "payload": {"active_run_count": 0},
    }
    second = {**first, "id": "event-2", "sequence": 2}

    class TrickleSse(httpx.SyncByteStream):
        def __iter__(self):
            for payload in (first, second):
                time.sleep(0.04)
                yield _sse_bytes(payload)

    client = _client(
        lambda _request: httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            stream=TrickleSse(),
        ),
        timeout=0.06,
    )
    try:
        with client.events() as stream:
            iterator = iter(stream)
            assert next(iterator).id == "event-1"
            with pytest.raises(CoreClientErrorV1) as exc_info:
                next(iterator)
    finally:
        client.close()

    assert exc_info.value.error.code is CoreClientLocalErrorCodeV1.CONNECTION_FAILED


@pytest.mark.parametrize("private_kind", ["bearer", "origin", "session"])
def test_json_recursively_rejects_unicode_escaped_private_tunnel_values(
    private_kind: str,
) -> None:
    token = _token()
    connection = _connection(token=token)
    private_value = {
        "bearer": token,
        "origin": connection.origin,
        "session": connection.session_id,
    }[private_kind]
    escaped = "".join(f"\\u{ord(character):04x}" for character in private_value)
    body = (
        '{"schema_version":"1","status":"ok","ready":true,"checked_at":"' + escaped + '"}'
    ).encode()
    assert private_value.encode() not in body
    client = _client(
        lambda _request: httpx.Response(
            200, headers={"Content-Type": "application/json"}, content=body
        ),
        connection=connection,
    )

    with pytest.raises(CoreClientErrorV1) as exc_info:
        client.health()

    assert exc_info.value.error.code is CoreClientLocalErrorCodeV1.INVALID_RESPONSE
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None
    assert private_value not in repr(exc_info.value.error)


def test_error_and_sse_unicode_escaped_bearer_are_sanitized() -> None:
    token = _token()
    escaped = "".join(f"\\u{ord(character):04x}" for character in token)
    error_body = (
        json.dumps(_api_error_payload()).replace("The resource changed.", escaped).encode()
    )
    error_client = _client(
        lambda _request: httpx.Response(
            409, headers={"Content-Type": "application/json"}, content=error_body
        ),
        connection=_connection(token=token),
    )

    with pytest.raises(CoreClientErrorV1) as error_info:
        error_client.status()

    assert error_info.value.error.code is CoreClientLocalErrorCodeV1.INVALID_ERROR_RESPONSE
    assert error_info.value.__cause__ is None
    assert error_info.value.__context__ is None

    payload = {
        "schema_version": "1",
        "id": "event-1",
        "sequence": 1,
        "occurred_at": "2026-07-14T12:00:00Z",
        "event": "heartbeat.v1",
        "payload": {"active_run_count": escaped},
    }
    data = json.dumps(payload, separators=(",", ":")).replace("\\\\u", "\\u").encode()
    frame = b"id: event-1\nevent: heartbeat.v1\ndata: " + data + b"\n\n"
    sse_client = _client(
        lambda _request: httpx.Response(
            200, headers={"Content-Type": "text/event-stream"}, content=frame
        ),
        connection=_connection(token=token),
    )

    with sse_client.events() as stream:
        with pytest.raises(CoreClientErrorV1) as sse_info:
            list(stream)

    assert sse_info.value.error.code is CoreClientLocalErrorCodeV1.SSE_PROTOCOL_ERROR
    assert sse_info.value.__cause__ is None
    assert sse_info.value.__context__ is None


@pytest.mark.parametrize("value", ["idempotency-\u00e9", "line\nfeed", "has space", "\u2603"])
def test_header_carried_identifiers_require_visible_ascii(value: str) -> None:
    called = False

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json={})

    client = _client(handler)
    request = v1.EnvironmentDoctorRequestV1(
        execution_mode=v1.ExecutionMode.SELF_DEPLOYED,
        checks=[v1.EnvironmentCheckKind.PYTHON],
    )
    with pytest.raises(CoreClientErrorV1) as idempotency_error:
        client.environment_doctor(request, idempotency_key=value)
    with pytest.raises(CoreClientErrorV1) as cursor_error:
        with client.events(last_event_id=value):
            pass

    assert called is False
    assert idempotency_error.value.error.code is CoreClientLocalErrorCodeV1.INVALID_REQUEST
    assert cursor_error.value.error.code is CoreClientLocalErrorCodeV1.INVALID_REQUEST


def test_sse_id_requires_visible_ascii_before_yield() -> None:
    payload = {
        "schema_version": "1",
        "id": "event-\u00e9",
        "sequence": 1,
        "occurred_at": "2026-07-14T12:00:00Z",
        "event": "heartbeat.v1",
        "payload": {"active_run_count": 0},
    }
    client = _client(
        lambda _request: httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            content=_sse_bytes(payload),
        )
    )

    with client.events() as stream:
        with pytest.raises(CoreClientErrorV1) as exc_info:
            list(stream)

    assert exc_info.value.error.code is CoreClientLocalErrorCodeV1.SSE_PROTOCOL_ERROR


def test_close_linearizes_inflight_request_and_blocks_old_tunnel_reuse(monkeypatch) -> None:
    import desktop.sidecar.core_client_v1 as core_client_module

    monkeypatch.setattr(core_client_module, "MAX_CORE_CLOSE_WAIT_SECONDS", 0.05)
    entered = threading.Event()
    release = threading.Event()
    old_requests: list[httpx.Request] = []
    connection = _connection()

    def old_handler(request: httpx.Request) -> httpx.Response:
        old_requests.append(request)
        entered.set()
        release.wait(1)
        return httpx.Response(200, json=_health_payload())

    client = _client(old_handler, connection=connection)
    outcome: list[CoreClientErrorV1] = []

    def request_health() -> None:
        try:
            client.health()
        except CoreClientErrorV1 as exc:
            outcome.append(exc)

    request_thread = threading.Thread(target=request_health)
    request_thread.start()
    assert entered.wait(1)
    client.close()
    release.set()
    request_thread.join(1)
    assert not request_thread.is_alive()
    assert outcome[0].error.code is CoreClientLocalErrorCodeV1.CLIENT_CLOSED
    assert outcome[0].__cause__ is None
    assert outcome[0].__context__ is None

    with pytest.raises(CoreClientErrorV1):
        client.status()
    assert len(old_requests) == 1

    new_token = _token()
    new_connection = CoreTunnelConnectionV1(
        endpoint="http://127.0.0.1:48766",
        bearer_token=new_token,
        project_id=PROJECT_ID,
        session_id="desktop-session-2",
    )

    def new_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.port == 48766
        assert connection.bearer_token not in request.headers.get("authorization", "")
        assert request.headers["authorization"] == f"Bearer {new_token}"
        return httpx.Response(200, json=_api_error_payload())

    new_client = _client(new_handler, connection=new_connection)
    with pytest.raises(CoreClientErrorV1):
        new_client.status()
    assert len(old_requests) == 1


def test_sse_unknown_parent_requires_snapshot_refresh_and_known_parent_yields() -> None:
    frame = _sse_bytes(_timeline_event())

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/runs/run-1":
            return httpx.Response(200, json=_run().model_dump(mode="json"))
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, content=frame)

    client = _client(handler)
    with client.events() as stream:
        with pytest.raises(CoreClientErrorV1) as unknown:
            list(stream)
    assert unknown.value.error.code is CoreClientLocalErrorCodeV1.SNAPSHOT_REFRESH_REQUIRED

    client.get_run("run-1", project_id=PROJECT_ID)
    with client.events() as stream:
        events = list(stream)
    assert events[0].data.root.payload.run_id == "run-1"


def test_service_log_sse_requires_authoritative_service_membership() -> None:
    frame = _sse_bytes(_service_log_event())

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/services/service-1":
            return httpx.Response(200, json=_service().model_dump(mode="json"))
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, content=frame)

    client = _client(handler)
    with client.events() as stream:
        with pytest.raises(CoreClientErrorV1) as unknown:
            list(stream)
    assert unknown.value.error.code is CoreClientLocalErrorCodeV1.SNAPSHOT_REFRESH_REQUIRED
    assert unknown.value.error.retryable is True

    client.get_service("service-1")
    with client.events() as stream:
        events = list(stream)
    assert events[0].data.root.payload.service_id == "service-1"


def test_close_cancels_active_sse_and_normalizes_stream_error() -> None:
    class BlockingSse(httpx.SyncByteStream):
        def __init__(self) -> None:
            self.started = threading.Event()
            self.closed = threading.Event()

        def __iter__(self):
            self.started.set()
            self.closed.wait(1)
            raise httpx.ReadError("private stream close")
            yield b""  # pragma: no cover

        def close(self) -> None:
            self.closed.set()

    body = BlockingSse()
    client = _client(
        lambda _request: httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            stream=body,
        )
    )
    errors: list[CoreClientErrorV1] = []

    with client.events() as stream:
        iterator = threading.Thread(
            target=lambda: _capture_stream_error(stream, errors),
        )
        iterator.start()
        assert body.started.wait(1)
        client.close()
        iterator.join(1)

    assert not iterator.is_alive()
    assert errors[0].error.code is CoreClientLocalErrorCodeV1.CLIENT_CLOSED
    assert errors[0].__cause__ is None
    assert errors[0].__context__ is None


def test_sse_rejects_cross_project_resource_even_with_valid_shape() -> None:
    run = _run(project_id="project-other")
    payload = {
        "schema_version": "1",
        "id": "event-run-1",
        "sequence": 1,
        "occurred_at": "2026-07-14T12:00:00Z",
        "event": "run.updated.v1",
        "change": {
            "change_id": "change-run-1",
            "resource_type": "run",
            "resource_id": run.id,
            "parent_resource_type": "project",
            "parent_resource_id": run.project_id,
            "resource_etag": run.etag,
            "content_sha256": None,
        },
        "payload": run.model_dump(mode="json", exclude={"attempts"}),
    }
    client = _client(
        lambda _request: httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            content=_sse_bytes(payload),
        )
    )

    with client.events() as stream:
        with pytest.raises(CoreClientErrorV1) as exc_info:
            list(stream)

    assert exc_info.value.error.code is CoreClientLocalErrorCodeV1.ACTIVE_PROJECT_MISMATCH


def test_workspace_upload_routes_bind_the_complete_publication_chain() -> None:
    publication = _publication()
    open_upload = _upload()
    complete_upload = _upload(
        accepted_offset=1024,
        etag='"' + ("d" * 64) + '"',
    )
    finalized_upload = _upload(
        accepted_offset=1024,
        status=v1.WorkspaceUploadStatus.FINALIZED,
        etag='"' + ("e" * 64) + '"',
        publication=publication,
    )
    final_response = v1.WorkspaceUploadFinalizeResponseV1(
        project_id=PROJECT_ID,
        upload=finalized_upload,
        publication=publication,
        project=_project(publication=publication),
    )
    project = _project()
    chunk_content = b"x" * 1024
    chunk = v1.WorkspaceUploadChunkV1(
        offset=0,
        byte_length=len(chunk_content),
        content_base64=base64.b64encode(chunk_content).decode(),
        content_sha256=hashlib.sha256(chunk_content).hexdigest(),
    )
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.method == "GET" and request.url.path.endswith(PROJECT_ID):
            return httpx.Response(200, json=project.model_dump(mode="json"))
        if request.method == "POST" and request.url.path.endswith("workspace-uploads"):
            return httpx.Response(201, json=open_upload.model_dump(mode="json"))
        if request.method == "GET" and request.url.path.endswith("upload-1"):
            return httpx.Response(200, json=open_upload.model_dump(mode="json"))
        if request.method == "PUT":
            return httpx.Response(200, json=complete_upload.model_dump(mode="json"))
        return httpx.Response(201, json=final_response.model_dump(mode="json"))

    client = _client(handler)
    client.get_project()
    create = v1.WorkspaceUploadCreateV1(
        project_snapshot=project.current_project_snapshot,
        archive=_archive(),
    )
    assert (
        client.create_workspace_upload(
            create,
            if_match=project.etag,
            idempotency_key="workspace-create-1",
        ).accepted_offset
        == 0
    )
    assert client.get_workspace_upload("upload-1").id == "upload-1"
    assert (
        client.put_workspace_upload_chunk(
            "upload-1",
            chunk,
            if_match=open_upload.etag,
            idempotency_key="workspace-chunk-1",
        ).accepted_offset
        == 1024
    )
    result = client.finalize_workspace_upload(
        "upload-1",
        v1.WorkspaceUploadFinalizeV1(content_sha256=_archive().content_sha256),
        if_match=complete_upload.etag,
        if_project_match=complete_upload.project_etag,
        idempotency_key="workspace-finalize-1",
    )

    assert result.publication.workspace_snapshot == result.project.current_workspace_snapshot
    assert any(path.endswith("/chunk") for path in seen)
    assert any(path.endswith("/finalize") for path in seen)


def test_workspace_abort_and_wrong_upload_response_are_bound_to_route() -> None:
    open_upload = _upload()
    aborted = _upload(
        status=v1.WorkspaceUploadStatus.ABORTED,
        etag='"' + ("3" * 64) + '"',
    )
    responses = [open_upload, aborted]

    def handler(request: httpx.Request) -> httpx.Response:
        value = responses.pop(0)
        return httpx.Response(200, json=value.model_dump(mode="json"))

    client = _client(handler)
    client.get_workspace_upload("upload-1")
    result = client.abort_workspace_upload(
        "upload-1",
        v1.WorkspaceUploadAbortV1(reason="User cancelled."),
        if_match=open_upload.etag,
        idempotency_key="workspace-abort-1",
    )
    assert result.status is v1.WorkspaceUploadStatus.ABORTED

    wrong = _upload().model_copy(update={"id": "upload-other"})
    wrong_client = _client(
        lambda _request: httpx.Response(200, json=wrong.model_dump(mode="json"))
    )
    with pytest.raises(CoreClientErrorV1) as exc_info:
        wrong_client.get_workspace_upload("upload-1")
    assert exc_info.value.error.code is CoreClientLocalErrorCodeV1.INVALID_RESPONSE


def test_persisted_workspace_abort_replays_exact_authority_without_a_read() -> None:
    open_upload = _upload(accepted_offset=512, etag='"' + ("7" * 64) + '"')
    aborted = open_upload.model_copy(
        update={
            "status": v1.WorkspaceUploadStatus.ABORTED,
            "updated_at": "2026-07-14T12:00:03Z",
            "etag": '"' + ("8" * 64) + '"',
        }
    )
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=aborted.model_dump(mode="json"))

    client = _client(handler)
    request = v1.WorkspaceUploadAbortV1(reason="Replay the durable abort.")
    result = client.abort_persisted_workspace_upload(
        open_upload,
        request,
        if_match=open_upload.etag,
        idempotency_key="workspace-persisted-abort-1",
    )

    assert result == aborted
    assert [request.method for request in seen] == ["POST"]
    assert seen[0].headers["If-Match"] == open_upload.etag
    assert seen[0].headers["Idempotency-Key"] == "workspace-persisted-abort-1"
    assert client._workspace_uploads[open_upload.id] == aborted


@pytest.mark.parametrize(
    ("if_match", "idempotency_key"),
    [
        ('"' + ("9" * 64) + '"', "workspace-persisted-abort-invalid-etag"),
        ('"' + ("7" * 64) + '"', "workspace-persisted-abort\ninvalid"),
    ],
)
def test_persisted_workspace_abort_rejects_invalid_authority_before_restore(
    if_match: str,
    idempotency_key: str,
) -> None:
    open_upload = _upload(accepted_offset=512, etag='"' + ("7" * 64) + '"')
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise AssertionError("invalid persisted abort must fail before transport")

    client = _client(handler)

    with pytest.raises(CoreClientErrorV1) as exc_info:
        client.abort_persisted_workspace_upload(
            open_upload,
            v1.WorkspaceUploadAbortV1(reason="Replay the durable abort."),
            if_match=if_match,
            idempotency_key=idempotency_key,
        )

    assert exc_info.value.error.code is CoreClientLocalErrorCodeV1.INVALID_REQUEST
    assert calls == 0
    assert open_upload.id not in client._workspace_uploads


def test_close_rolls_back_persisted_workspace_abort_authority_before_delivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    open_upload = _upload(accepted_offset=512, etag='"' + ("7" * 64) + '"')
    aborted = open_upload.model_copy(
        update={
            "status": v1.WorkspaceUploadStatus.ABORTED,
            "updated_at": "2026-07-14T12:00:03Z",
            "etag": '"' + ("8" * 64) + '"',
        }
    )
    client = _client(lambda _request: httpx.Response(200, json=aborted.model_dump(mode="json")))
    original_batch = client._registration_batch
    transaction_ready = threading.Event()
    release_transaction = threading.Event()
    results: list[v1.WorkspaceUploadSessionV1] = []
    errors: list[CoreClientErrorV1] = []

    @contextmanager
    def pause_before_transaction_exit(*args, **kwargs):
        with original_batch(*args, **kwargs):
            yield
            transaction_ready.set()
            release_transaction.wait(1)

    def abort() -> None:
        try:
            results.append(
                client.abort_persisted_workspace_upload(
                    open_upload,
                    v1.WorkspaceUploadAbortV1(reason="Replay the durable abort."),
                    if_match=open_upload.etag,
                    idempotency_key="workspace-persisted-abort-close-1",
                )
            )
        except CoreClientErrorV1 as exc:
            errors.append(exc)

    monkeypatch.setattr(client, "_registration_batch", pause_before_transaction_exit)
    request_thread = threading.Thread(target=abort)
    request_thread.start()
    assert transaction_ready.wait(1)
    client.close()
    release_transaction.set()
    request_thread.join(1)

    assert not request_thread.is_alive()
    assert results == []
    assert errors[0].error.code is CoreClientLocalErrorCodeV1.CLIENT_CLOSED
    assert open_upload.id not in client._workspace_uploads
    assert not any(key[0] == open_upload.id for key in client._workspace_etag_representations)
    assert not any(key[0] == open_upload.id for key in client._workspace_representation_etags)


def test_workspace_upload_create_requires_a_new_resource_etag() -> None:
    project = _project()
    upload = _upload(etag=project.etag)
    responses = iter([project, upload])

    def handler(_request: httpx.Request) -> httpx.Response:
        value = next(responses)
        status = 200 if isinstance(value, v1.ProjectV1) else 201
        return httpx.Response(status, json=value.model_dump(mode="json"))

    client = _client(handler)
    client.get_project()

    with pytest.raises(CoreClientErrorV1) as exc_info:
        client.create_workspace_upload(
            v1.WorkspaceUploadCreateV1(
                project_snapshot=project.current_project_snapshot,
                archive=_archive(),
            ),
            if_match=project.etag,
            idempotency_key="workspace-create-etag",
        )

    assert exc_info.value.error.code is CoreClientLocalErrorCodeV1.INVALID_RESPONSE


def test_workspace_upload_create_exact_idempotent_replay_may_keep_etag() -> None:
    project = _project()
    upload = _upload()
    responses = iter([project, upload, upload])

    def handler(_request: httpx.Request) -> httpx.Response:
        value = next(responses)
        status = 200 if isinstance(value, v1.ProjectV1) else 201
        return httpx.Response(status, json=value.model_dump(mode="json"))

    client = _client(handler)
    client.get_project()
    request = v1.WorkspaceUploadCreateV1(
        project_snapshot=project.current_project_snapshot,
        archive=_archive(),
    )

    first = client.create_workspace_upload(
        request,
        if_match=project.etag,
        idempotency_key="workspace-create-replay",
    )
    replay = client.create_workspace_upload(
        request,
        if_match=project.etag,
        idempotency_key="workspace-create-replay",
    )

    assert replay == first
    assert replay.etag == first.etag


def test_workspace_upload_create_replay_rejects_changed_representation() -> None:
    project = _project()
    upload = _upload()
    changed = upload.model_copy(
        update={
            "updated_at": "2026-07-14T12:00:02Z",
            "etag": '"' + ("d" * 64) + '"',
        }
    )
    responses = iter([project, upload, changed])

    def handler(_request: httpx.Request) -> httpx.Response:
        value = next(responses)
        status = 200 if isinstance(value, v1.ProjectV1) else 201
        return httpx.Response(status, json=value.model_dump(mode="json"))

    client = _client(handler)
    client.get_project()
    request = v1.WorkspaceUploadCreateV1(
        project_snapshot=project.current_project_snapshot,
        archive=_archive(),
    )
    client.create_workspace_upload(
        request,
        if_match=project.etag,
        idempotency_key="workspace-create-replay-changed",
    )

    with pytest.raises(CoreClientErrorV1) as exc_info:
        client.create_workspace_upload(
            request,
            if_match=project.etag,
            idempotency_key="workspace-create-replay-changed",
        )

    assert exc_info.value.error.code is CoreClientLocalErrorCodeV1.INVALID_RESPONSE


def test_workspace_upload_chunk_state_change_requires_new_etag() -> None:
    open_upload = _upload()
    advanced = _upload(accepted_offset=1024, etag=open_upload.etag)
    responses = iter([open_upload, advanced])
    client = _client(
        lambda _request: httpx.Response(
            200,
            json=next(responses).model_dump(mode="json"),
        )
    )
    client.get_workspace_upload(open_upload.id)

    with pytest.raises(CoreClientErrorV1) as exc_info:
        client.put_workspace_upload_chunk(
            open_upload.id,
            _chunk(),
            if_match=open_upload.etag,
            idempotency_key="workspace-chunk-etag",
        )

    assert exc_info.value.error.code is CoreClientLocalErrorCodeV1.INVALID_RESPONSE


def test_workspace_upload_abort_state_change_requires_new_etag() -> None:
    open_upload = _upload()
    aborted = _upload(status=v1.WorkspaceUploadStatus.ABORTED, etag=open_upload.etag)
    responses = iter([open_upload, aborted])
    client = _client(
        lambda _request: httpx.Response(
            200,
            json=next(responses).model_dump(mode="json"),
        )
    )
    client.get_workspace_upload(open_upload.id)

    with pytest.raises(CoreClientErrorV1) as exc_info:
        client.abort_workspace_upload(
            open_upload.id,
            v1.WorkspaceUploadAbortV1(reason="User cancelled."),
            if_match=open_upload.etag,
            idempotency_key="workspace-abort-etag",
        )

    assert exc_info.value.error.code is CoreClientLocalErrorCodeV1.INVALID_RESPONSE


def test_workspace_upload_finalize_state_change_requires_new_etag() -> None:
    publication = _publication()
    complete = _upload(accepted_offset=1024, etag='"' + ("d" * 64) + '"')
    finalized = _upload(
        accepted_offset=1024,
        status=v1.WorkspaceUploadStatus.FINALIZED,
        etag=complete.etag,
        publication=publication,
    )
    response = v1.WorkspaceUploadFinalizeResponseV1(
        project_id=PROJECT_ID,
        upload=finalized,
        publication=publication,
        project=_project(publication=publication),
    )
    responses: list[v1.WorkspaceUploadSessionV1 | v1.WorkspaceUploadFinalizeResponseV1] = [
        complete,
        response,
    ]

    def handler(_request: httpx.Request) -> httpx.Response:
        value = responses.pop(0)
        status = 201 if isinstance(value, v1.WorkspaceUploadFinalizeResponseV1) else 200
        return httpx.Response(status, json=value.model_dump(mode="json"))

    client = _client(handler)
    client.get_workspace_upload(complete.id)

    with pytest.raises(CoreClientErrorV1) as exc_info:
        client.finalize_workspace_upload(
            complete.id,
            v1.WorkspaceUploadFinalizeV1(content_sha256=_archive().content_sha256),
            if_match=complete.etag,
            if_project_match=complete.project_etag,
            idempotency_key="workspace-finalize-etag",
        )

    assert exc_info.value.error.code is CoreClientLocalErrorCodeV1.INVALID_RESPONSE


def test_workspace_upload_etag_uniquely_binds_canonical_representation() -> None:
    original = _upload()
    same_etag_changed_offset = _upload(accepted_offset=1024, etag=original.etag)
    same_representation_changed_etag = original.model_copy(update={"etag": '"' + ("9" * 64) + '"'})

    for changed in (same_etag_changed_offset, same_representation_changed_etag):
        responses = iter([original, changed])
        client = _client(
            lambda _request: httpx.Response(
                200,
                json=next(responses).model_dump(mode="json"),
            )
        )
        client.get_workspace_upload(original.id)

        with pytest.raises(CoreClientErrorV1) as exc_info:
            client.get_workspace_upload(original.id)

        assert exc_info.value.error.code is CoreClientLocalErrorCodeV1.INVALID_RESPONSE


def test_workspace_upload_rejects_stale_state_rollback_with_fresh_etag() -> None:
    original = _upload()
    advanced = _upload(accepted_offset=1024, etag='"' + ("8" * 64) + '"')
    stale = original.model_copy(update={"etag": '"' + ("9" * 64) + '"'})
    responses = iter([original, advanced, stale])
    client = _client(
        lambda _request: httpx.Response(
            200,
            json=next(responses).model_dump(mode="json"),
        )
    )
    client.get_workspace_upload(original.id)
    client.get_workspace_upload(original.id)

    with pytest.raises(CoreClientErrorV1) as exc_info:
        client.get_workspace_upload(original.id)

    assert exc_info.value.error.code is CoreClientLocalErrorCodeV1.INVALID_RESPONSE


@pytest.mark.parametrize(
    ("route", "expected_code"),
    [
        ("run", CoreClientLocalErrorCodeV1.INVALID_RESPONSE),
        ("service", CoreClientLocalErrorCodeV1.INVALID_RESPONSE),
        ("artifact", CoreClientLocalErrorCodeV1.INVALID_RESPONSE),
        ("operation", CoreClientLocalErrorCodeV1.INVALID_RESPONSE),
        ("diagnostic", CoreClientLocalErrorCodeV1.INVALID_RESPONSE),
    ],
)
def test_detail_reads_reject_wrong_resource_identity(
    route: str,
    expected_code: CoreClientLocalErrorCodeV1,
) -> None:
    operation = _queued_environment_operation(
        v1.EnvironmentRepairRequestV1(
            execution_mode=v1.ExecutionMode.SELF_DEPLOYED,
            actions=[v1.EnvironmentRepairAction.RETRY_NETWORK],
        )
    ).model_copy(update={"id": "operation-other"})
    cases = {
        "run": (
            lambda client: client.get_run("run-1", project_id=PROJECT_ID),
            _run("run-other"),
        ),
        "service": (
            lambda client: client.get_service("service-1"),
            _service("service-other"),
        ),
        "artifact": (
            lambda client: client.get_artifact("artifact-1", project_id=PROJECT_ID),
            _artifact("artifact-other", digest="a" * 64),
        ),
        "operation": (
            lambda client: client.get_operation("operation-1"),
            operation,
        ),
        "diagnostic": (
            lambda client: client.get_diagnostic("diagnostic-1"),
            _diagnostic("diagnostic-other"),
        ),
    }
    call, response = cases[route]
    client = _client(lambda _request: httpx.Response(200, json=response.model_dump(mode="json")))

    with pytest.raises(CoreClientErrorV1) as exc_info:
        call(client)

    assert exc_info.value.error.code is expected_code


def test_operation_registration_failure_does_not_grant_member_or_log_access() -> None:
    request = v1.EnvironmentRepairRequestV1(
        execution_mode=v1.ExecutionMode.SELF_DEPLOYED,
        actions=[v1.EnvironmentRepairAction.RETRY_NETWORK],
    )
    original = _queued_environment_operation(request)
    colliding_log = original.model_copy(update={"id": "operation-2"})
    changed_request = v1.EnvironmentRepairOperationRequestV1(
        kind=v1.OperationKind.ENVIRONMENT_REPAIR,
        request=request.model_copy(
            update={"actions": [v1.EnvironmentRepairAction.RESTART_MODEL_SERVICE]}
        ),
    )
    changed_identity = original.model_copy(
        update={"request": changed_request, "logs_ref": "operation-poison-logs"}
    )
    responses = iter([original, colliding_log, changed_identity])
    client = _client(
        lambda _request: httpx.Response(
            200,
            json=next(responses).model_dump(mode="json"),
        )
    )

    client.get_operation(original.id)
    with pytest.raises(CoreClientErrorV1):
        client.get_operation(colliding_log.id)
    with pytest.raises(CoreClientErrorV1):
        client.get_operation(original.id)

    assert (v1.ResourceChangeType.OPERATION, colliding_log.id) not in client._members
    assert changed_identity.logs_ref not in client._log_refs


def test_diagnostic_registration_failure_does_not_grant_member_or_log_access() -> None:
    original = _diagnostic_with_log()
    colliding_log = _diagnostic_with_log("diagnostic-2", original.checks[0].logs_ref)
    changed_identity = _diagnostic_with_log(
        original.id,
        "diagnostic-poison-logs",
    ).model_copy(update={"created_at": "2026-07-14T12:00:01Z"})
    responses = iter([original, colliding_log, changed_identity])
    client = _client(
        lambda _request: httpx.Response(
            200,
            json=next(responses).model_dump(mode="json"),
        )
    )

    client.get_diagnostic(original.id)
    with pytest.raises(CoreClientErrorV1):
        client.get_diagnostic(colliding_log.id)
    with pytest.raises(CoreClientErrorV1):
        client.get_diagnostic(original.id)

    assert (v1.ResourceChangeType.DIAGNOSTIC, colliding_log.id) not in client._members
    assert changed_identity.checks[0].logs_ref not in client._log_refs


def test_run_page_registration_is_atomic_on_late_item_failure() -> None:
    first = _run("run-new")
    conflicting = _run("run-conflict")
    changed = conflicting.model_copy(
        update={"project_snapshot": _snapshot("project-snapshot-2", v1.SnapshotKind.PROJECT, "8")}
    )
    responses = iter(
        [
            _page([conflicting.model_dump(mode="json", exclude={"attempts"})]),
            _page(
                [
                    first.model_dump(mode="json", exclude={"attempts"}),
                    changed.model_dump(mode="json", exclude={"attempts"}),
                ]
            ),
        ]
    )
    client = _client(lambda _request: httpx.Response(200, json=next(responses)))
    client.list_runs()

    with pytest.raises(CoreClientErrorV1) as exc_info:
        client.list_runs()

    assert exc_info.value.error.code is CoreClientLocalErrorCodeV1.INVALID_RESPONSE
    assert first.id not in client._runs
    assert (v1.ResourceChangeType.RUN, first.id) not in client._members


def test_artifact_page_registration_is_atomic_on_late_item_failure() -> None:
    run = _run()
    first = _artifact("artifact-new", digest="a" * 64)
    conflicting = _artifact("artifact-conflict", digest="b" * 64)
    changed = conflicting.model_copy(update={"content_sha256": "c" * 64})
    pages = iter(
        [
            _page([conflicting.model_dump(mode="json")]),
            _page([first.model_dump(mode="json"), changed.model_dump(mode="json")]),
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/runs/run-1":
            return httpx.Response(200, json=run.model_dump(mode="json"))
        return httpx.Response(200, json=next(pages))

    client = _client(handler)
    client.get_run(run.id, project_id=PROJECT_ID)
    client.run_artifacts(run.id, project_id=PROJECT_ID)

    with pytest.raises(CoreClientErrorV1) as exc_info:
        client.run_artifacts(run.id, project_id=PROJECT_ID)

    assert exc_info.value.error.code is CoreClientLocalErrorCodeV1.INVALID_RESPONSE
    assert first.id not in client._artifacts
    assert (v1.ResourceChangeType.ARTIFACT, first.id) not in client._members


def test_run_child_and_artifact_content_bind_requested_parent_ids() -> None:
    run = _run()
    timeline = _timeline_event()["payload"]["entry"]
    wrong_timeline = {**timeline, "run_id": "run-other"}
    artifact = _artifact("artifact-1", digest="a" * 64)
    responses: dict[str, object] = {
        "/v1/runs/run-1": run.model_dump(mode="json"),
        "/v1/runs/run-1/timeline": _page([wrong_timeline]),
        f"/v1/projects/{PROJECT_ID}/artifacts/artifact-1": artifact.model_dump(mode="json"),
        f"/v1/projects/{PROJECT_ID}/artifacts/artifact-1/content": {
            "schema_version": "1",
            "artifact_id": "artifact-other",
            "artifact_type": "skill_bundle",
            "documents": [],
            "total_documents": 0,
            "total_utf8_bytes": 0,
            "returned_utf8_bytes": 0,
            "truncated": False,
        },
    }
    client = _client(lambda request: httpx.Response(200, json=responses[request.url.path]))
    client.get_run("run-1", project_id=PROJECT_ID)
    with pytest.raises(CoreClientErrorV1) as timeline_error:
        client.run_timeline("run-1", project_id=PROJECT_ID)
    client.get_artifact("artifact-1", project_id=PROJECT_ID)
    with pytest.raises(CoreClientErrorV1) as content_error:
        client.artifact_content("artifact-1", project_id=PROJECT_ID)

    assert timeline_error.value.error.code is CoreClientLocalErrorCodeV1.INVALID_RESPONSE
    assert content_error.value.error.code is CoreClientLocalErrorCodeV1.INVALID_RESPONSE
