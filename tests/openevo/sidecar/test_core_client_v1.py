from __future__ import annotations

import json
import secrets

import httpx
import pytest

from desktop.sidecar.core_client_v1 import (
    CoreClientErrorV1,
    CoreClientLocalErrorCodeV1,
    CoreClientLocalErrorV1,
    CoreControlClientV1,
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
        b'[{"status":"ok"}]',
        json.dumps({**_health_payload(), "unknown": True}).encode(),
        json.dumps({**_health_payload(), "ready": 1}).encode(),
    ],
    ids=["invalid-json", "wrong-shape", "extra-field", "coerced-type"],
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
    assert content.decode(errors="ignore") not in str(exc_info.value)


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
        status = 201 if request.url.path.endswith("/finalize") else 202
        return httpx.Response(status, json={})

    client = _client(handler)
    with pytest.raises(CoreClientErrorV1):
        client.finalize_workspace_upload(
            "upload/one",
            v1.WorkspaceUploadFinalizeV1(content_sha256="b" * 64),
            if_match=ETAG,
            if_project_match='"' + ("c" * 64) + '"',
            idempotency_key="finalize-1",
        )
    with pytest.raises(CoreClientErrorV1):
        client.cancel_operation(
            "operation/one",
            v1.OperationCancelRequestV1(reason=v1.OperationCancelReason.USER_REQUESTED),
            if_match=ETAG,
            idempotency_key="cancel-operation-1",
        )

    finalize, cancel = seen
    assert finalize.method == "POST"
    assert finalize.url.raw_path.decode() == (
        "/v1/projects/project%2Factive%3Fone/workspace-uploads/"
        "upload%2Fone/finalize"
    )
    assert finalize.headers["if-match"] == ETAG
    assert finalize.headers["if-project-match"] == '"' + ("c" * 64) + '"'
    assert finalize.headers["idempotency-key"] == "finalize-1"
    assert cancel.method == "POST"
    assert cancel.url.raw_path.decode() == "/v1/operations/operation%2Fone/cancel"
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
            lambda: client.run_timeline("run/one", project_id=PROJECT_ID),
            "/v1/runs/run%2Fone/timeline",
        ),
        (
            lambda: client.run_logs("run/one", project_id=PROJECT_ID),
            "/v1/runs/run%2Fone/logs",
        ),
        (
            lambda: client.run_context("run/one", project_id=PROJECT_ID),
            "/v1/runs/run%2Fone/context",
        ),
        (
            lambda: client.run_artifacts("run/one", project_id=PROJECT_ID),
            "/v1/runs/run%2Fone/artifacts",
        ),
        (
            lambda: client.get_artifact("artifact/one", project_id=PROJECT_ID),
            "/v1/artifacts/artifact%2Fone",
        ),
        (
            lambda: client.artifact_content("artifact/one", project_id=PROJECT_ID),
            "/v1/artifacts/artifact%2Fone/content",
        ),
        (
            lambda: client.artifact_diff(
                "artifact/one",
                project_id=PROJECT_ID,
                previous_artifact_id="artifact/zero",
            ),
            "/v1/artifacts/artifact%2Fone/diff",
        ),
        (lambda: client.list_services(), "/v1/services"),
        (lambda: client.get_service("service/one"), "/v1/services/service%2Fone"),
        (
            lambda: client.service_logs("service/one"),
            "/v1/services/service%2Fone/logs",
        ),
        (
            lambda: client.get_operation("operation/one"),
            "/v1/operations/operation%2Fone",
        ),
        (lambda: client.logs_by_ref("logs/one"), "/v1/logs/logs%2Fone"),
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
    client = _client(lambda _request: httpx.Response(200, json=payload))

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
    assert secret_path not in str(exc_info.value)
    assert secret_path not in repr(exc_info.value.error)


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
    frame = (
        b"id: different\nevent: heartbeat.v1\ndata: "
        + json.dumps(payload).encode()
        + b"\n\n"
    )
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
