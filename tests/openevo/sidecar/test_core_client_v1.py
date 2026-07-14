from __future__ import annotations

import base64
import hashlib
import json
import secrets
import threading

import httpx
import pytest

from desktop.sidecar.core_client_v1 import (
    CoreClientErrorV1,
    CoreClientLocalErrorCodeV1,
    CoreClientLocalErrorV1,
    CoreControlClientV1,
    CoreSseStreamV1,
    CoreTunnelConnectionV1,
    MAX_CORE_JSON_RESPONSE_BYTES,
    MAX_CORE_SSE_FRAME_BYTES,
    MAX_CORE_SSE_RESPONSE_BYTES,
)
from openevo.backend.contracts.v1 import models as v1


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


def _client(
    handler,
    *,
    connection: CoreTunnelConnectionV1 | None = None,
) -> CoreControlClientV1:
    return CoreControlClientV1(
        connection or _connection(),
        transport=httpx.MockTransport(handler),
    )


def _health_payload() -> dict[str, object]:
    return {
        "schema_version": "1",
        "status": "ok",
        "ready": True,
        "checked_at": "2026-07-14T12:00:00Z",
    }


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
            source_artifact_ids=[],
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


def _project(*, publication: v1.WorkspacePublicationV1 | None = None) -> v1.ProjectV1:
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


def test_client_disables_environment_transport_and_redirects() -> None:
    client = _client(lambda _request: httpx.Response(200, json=_health_payload()))
    try:
        assert client._http.trust_env is False
        assert client._http.follow_redirects is False
    finally:
        client.close()


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
            "/v1/artifacts/artifact%2Fone",
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
        if request.url.path == "/v1/artifacts/artifact-current":
            return httpx.Response(
                200, json=_artifact("artifact-current", digest="a" * 64).model_dump(mode="json")
            )
        if request.url.path == "/v1/artifacts/artifact-previous":
            return httpx.Response(
                200, json=_artifact("artifact-previous", digest="b" * 64).model_dump(mode="json")
            )
        return httpx.Response(200, json=payload)

    client = _client(handler)
    client.get_artifact("artifact-current", project_id=PROJECT_ID)

    result = client.artifact_diff("artifact-current", project_id=PROJECT_ID)

    assert result == v1.ArtifactDiffV1.model_validate_json(json.dumps(payload), strict=True)
    assert result.document_changes == []


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


def test_run_child_and_artifact_content_bind_requested_parent_ids() -> None:
    run = _run()
    timeline = _timeline_event()["payload"]["entry"]
    wrong_timeline = {**timeline, "run_id": "run-other"}
    artifact = _artifact("artifact-1", digest="a" * 64)
    responses: dict[str, object] = {
        "/v1/runs/run-1": run.model_dump(mode="json"),
        "/v1/runs/run-1/timeline": _page([wrong_timeline]),
        "/v1/artifacts/artifact-1": artifact.model_dump(mode="json"),
        "/v1/artifacts/artifact-1/content": {
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
