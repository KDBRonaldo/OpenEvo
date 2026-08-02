from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import threading

import httpx
import pytest

from desktop.sidecar.core_client_v2 import (
    CORE_EVENTS_SCHEMA_SHA256,
    CORE_OPENAPI_SHA256,
    CoreBootstrapTunnelConnectionV2,
    CoreClientErrorV2,
    CoreClientLocalErrorCodeV2,
    CoreControlClientV2,
    CoreMutationOutcomeUnknownV2,
    CoreProjectBootstrapClientV2,
    CoreTunnelConnectionV2,
)
from openevo.backend.contracts.v2 import models as m


_TOKEN = "abcdefghijklmnopqrstuvwxyzABCDEFGH0123456789._-abcdefghijklmnop"
_FEATURES = [
    "atomic_successor_v2",
    "event_replay_v2",
    "project_genesis_v2",
    "project_heads_v2",
    "task_admission_v2",
    "task_execution_v2",
    "verified_capabilities",
    "verified_registry",
    "workspace_snapshots_v2",
]


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _version(**updates: object) -> dict[str, object]:
    feature_digest = hashlib.sha256(
        json.dumps(_FEATURES, ensure_ascii=True, separators=(",", ":")).encode()
    ).hexdigest()
    value: dict[str, object] = {
        "schema_version": "2",
        "api_name": "openevo-core-control-api",
        "preferred_major": 2,
        "supported_majors": [2],
        "mutation_major": 2,
        "contracts": [
            {
                "schema_version": "2",
                "api_major": 2,
                "openapi_sha256": CORE_OPENAPI_SHA256,
                "event_schema_sha256": CORE_EVENTS_SCHEMA_SHA256,
                "access": "mutation",
                "mutation_compatible": True,
            }
        ],
        "release_version": "0.1.10",
        "build_id": "b" * 64,
        "source_commit": "c" * 40,
        "build_channel": "release",
        "provider_kind": "openevo_daemon",
        "feature_flags": _FEATURES,
        "feature_set_sha256": feature_digest,
        "registry_sha256": "a" * 64,
        "runtime_contract_sha256": "d" * 64,
        "mutation_compatible": True,
    }
    value.update(updates)
    return value


def _config() -> m.ScienceProjectConfigV2:
    return m.ScienceProjectConfigV2.model_validate(
        {
            "task": {"title": "Task", "objective": "Exercise the v2 authority."},
            "workspace": {"kind": "scratch", "display_name": "Workspace"},
            "execution": {
                "mode": "codex_subscription_transcript",
                "capture_mode": "transcript",
                "token_level_metrics_available": False,
                "harness_id": "codex",
                "codex_model": "gpt-5.5",
                "reasoning_effort": "high",
                "token_limit": 32_768,
                "task_network_allow_internet": False,
            },
            "evolution": {"targets": {}},
        },
        strict=True,
    )


def _head(project_id: str = "project-1") -> m.ProjectHeadRefV2:
    evolution = m.EvolutionRevisionRefV2(
        evolution_revision_id="evolution-0",
        project_id=project_id,
        manifest_sha256="2" * 64,
        artifact_count=0,
    )
    context = m.RuntimeContextSnapshotRefV2(
        runtime_context_snapshot_id="runtime-context-0",
        project_id=project_id,
        evolution_revision_id=evolution.evolution_revision_id,
        evolution_revision_manifest_sha256=evolution.manifest_sha256,
        registry_sha256="a" * 64,
        runtime_contract_sha256="d" * 64,
        manifest_sha256="3" * 64,
    )
    return m.ProjectHeadRefV2(
        project_head_id="head-0",
        project_id=project_id,
        generation=0,
        predecessor_project_head_id=None,
        workspace_snapshot=m.WorkspaceSnapshotRefV2(
            workspace_snapshot_id="workspace-0",
            project_id=project_id,
            manifest_sha256="1" * 64,
            entry_count=0,
            byte_size=0,
        ),
        evolution_revision=evolution,
        runtime_context_snapshot=context,
        effective_execution_snapshot=m.EffectiveExecutionSnapshotRefV2(
            effective_execution_snapshot_id="execution-0",
            project_id=project_id,
            execution_mode="codex_subscription_transcript",
            capture_mode="transcript",
            token_level_metrics_available=False,
            producer_id="subscription-issuer-v1",
            snapshot_sha256="4" * 64,
        ),
        registry_sha256="a" * 64,
        manifest_sha256="5" * 64,
    )


def _project(project_id: str = "project-1") -> m.ProjectV2:
    return m.ProjectV2(
        project_id=project_id,
        display_name="Project",
        config=_config(),
        project_config_sha256=m.project_config_sha256_for(_config()),
        active_project_head=_head(project_id),
        admission_etag='"' + "7" * 64 + '"',
        state="ready",
        created_at="2026-07-23T06:00:00Z",
        updated_at="2026-07-23T06:00:00Z",
        etag='"' + "8" * 64 + '"',
    )


def _published_v019_project(project_id: str = "project-1") -> m.ProjectV2:
    payload = _project(project_id).model_dump(mode="json")
    head = payload["active_project_head"]
    assert isinstance(head, dict)
    head["registry_sha256"] = "0c8d466db17fd0dc312a647c34e35bed04eba4e615799effebec761533c30874"
    context = head["runtime_context_snapshot"]
    assert isinstance(context, dict)
    context["registry_sha256"] = "0c8d466db17fd0dc312a647c34e35bed04eba4e615799effebec761533c30874"
    context["runtime_contract_sha256"] = (
        "535e3a05645590c90956769d960884fbbd818280b7517582a72e0b4fb41987f0"
    )
    return m.ProjectV2.model_validate(payload, strict=True)


def _connection(endpoint: str = "http://127.0.0.1:49201") -> CoreTunnelConnectionV2:
    return CoreTunnelConnectionV2(
        endpoint=endpoint,
        bearer_token=_TOKEN,
        profile_id="profile-1",
        profile_connection_generation=3,
        project_id="project-1",
        session_id="session-1",
    )


def _client(handler) -> CoreControlClientV2:
    return CoreControlClientV2(_connection(), transport=httpx.MockTransport(handler))


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://127.0.0.1:49201",
        "http://localhost:49201",
        "http://10.0.0.4:49201",
        "http://127.0.0.1:49201/path",
        "http://user@127.0.0.1:49201",
    ],
)
def test_connection_accepts_only_explicit_loopback_tunnel(endpoint: str) -> None:
    with pytest.raises(CoreClientErrorV2) as caught:
        _connection(endpoint)
    assert caught.value.error.code == CoreClientLocalErrorCodeV2.INVALID_CONNECTION


def test_version_is_frozen_and_authentication_stays_on_v2_fixed_origin() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/version":
            return httpx.Response(200, json=_version())
        assert request.url.path == "/v2/system/status"
        return httpx.Response(
            200,
            json={
                "schema_version": "2",
                "status": "ready",
                "release_version": "0.1.10",
                "source_commit": "c" * 40,
                "registry_sha256": "a" * 64,
                "checked_at": "2026-07-23T06:00:00Z",
            },
        )

    with _client(handler) as client:
        assert client.version().release_version == "0.1.10"
        assert client.system_status().status == "ready"
    assert "authorization" not in requests[0].headers
    assert requests[1].headers["authorization"] == f"Bearer {_TOKEN}"
    assert _TOKEN not in repr(_connection())


def test_current_client_accepts_only_exact_published_v019_project_head_for_reconnect() -> None:
    historical = _published_v019_project()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/version":
            return httpx.Response(200, json=_version())
        assert request.url.path == "/v2/projects/project-1"
        return httpx.Response(200, json=historical.model_dump(mode="json"))

    with _client(handler) as client:
        client.version()
        assert client.get_project() == historical

    payload = historical.model_dump(mode="json")
    head = payload["active_project_head"]
    assert isinstance(head, dict)
    head["registry_sha256"] = "f" * 64
    context = head["runtime_context_snapshot"]
    assert isinstance(context, dict)
    context["registry_sha256"] = "f" * 64
    foreign = m.ProjectV2.model_validate(payload, strict=True)

    def foreign_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/version":
            return httpx.Response(200, json=_version())
        return httpx.Response(200, json=foreign.model_dump(mode="json"))

    with _client(foreign_handler) as client:
        client.version()
        with pytest.raises(CoreClientErrorV2) as caught:
            client.get_project()
    assert caught.value.error.code == CoreClientLocalErrorCodeV2.AUTHORITY_DRIFT


def test_authenticated_calls_require_version_and_changed_version_is_rejected() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(200, json=_version())
        return httpx.Response(200, json=_version(build_id="e" * 64))

    client = _client(handler)
    with pytest.raises(CoreClientErrorV2) as caught:
        client.system_status()
    assert caught.value.error.code == CoreClientLocalErrorCodeV2.NEGOTIATION_REQUIRED
    assert calls == 0
    client.version()
    with pytest.raises(CoreClientErrorV2) as changed:
        client.version()
    assert changed.value.error.code == CoreClientLocalErrorCodeV2.INVALID_RESPONSE
    client.close()


def test_page_registration_is_copy_on_write_and_cross_project_fails_closed() -> None:
    page = {
        "schema_version": "2",
        "items": [
            _project().model_dump(mode="json"),
            _project("project-other").model_dump(mode="json"),
        ],
        "next_cursor": None,
        "has_more": False,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/version":
            return httpx.Response(200, json=_version())
        return httpx.Response(200, json=page)

    with _client(handler) as client:
        client.version()
        with pytest.raises(CoreClientErrorV2) as caught:
            client.list_projects()
        assert caught.value.error.code == CoreClientLocalErrorCodeV2.ACTIVE_PROJECT_MISMATCH
        assert client.cached_project is None


def test_mutation_uses_exact_headers_and_post_send_invalid_success_is_unknown() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/version":
            return httpx.Response(200, json=_version())
        return httpx.Response(200, json={"schema_version": "2", "project_id": "wrong"})

    with _client(handler) as client:
        client.version()
        request = m.ProjectUpdateV2(
            expected_project_head_id="head-0",
            expected_project_head_manifest_sha256="5" * 64,
            expected_project_config_sha256=m.project_config_sha256_for(_config()),
            display_name="Project",
            config=_config(),
        )
        with pytest.raises(CoreMutationOutcomeUnknownV2):
            client.update_project(
                request,
                if_match='"' + "8" * 64 + '"',
                idempotency_key="update-project-0001",
            )
    mutation = requests[-1]
    assert mutation.url.path == "/v2/projects/project-1"
    assert mutation.headers["if-match"] == '"' + "8" * 64 + '"'
    assert mutation.headers["idempotency-key"] == "update-project-0001"
    assert json.loads(mutation.content) == request.model_dump(mode="json")


def test_typed_http_mutation_error_remains_deterministic_and_private_echo_is_rejected() -> None:
    def typed_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/version":
            return httpx.Response(200, json=_version())
        return httpx.Response(
            409,
            json={
                "schema_version": "2",
                "request_id": "request-1",
                "code": "project_conflict",
                "http_status": 409,
                "message": "The project changed.",
                "category": "project",
                "retryable": True,
                "repair_action": "retry",
                "next_action": "Reload the project.",
            },
        )

    with _client(typed_handler) as client:
        client.version()
        with pytest.raises(CoreClientErrorV2) as caught:
            client.submit_task(
                m.TaskSubmitRequestV2(
                    project_id="project-1",
                    expected_project_admission_etag='"' + "7" * 64 + '"',
                    expected_project_head_id="head-0",
                    expected_project_head_manifest_sha256="5" * 64,
                    expected_project_config_sha256=m.project_config_sha256_for(_config()),
                ),
                idempotency_key="submit-task-0001",
            )
        assert caught.value.status_code == 409
        assert isinstance(caught.value.error, m.ApiErrorV2)

    def echo_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/version":
            return httpx.Response(200, json=_version())
        payload = typed_handler(request).json()
        payload["message"] = f"bad {_TOKEN}"
        return httpx.Response(409, json=payload)

    with _client(echo_handler) as client:
        client.version()
        with pytest.raises(CoreClientErrorV2) as caught:
            client.get_project()
        assert caught.value.error.code == CoreClientLocalErrorCodeV2.INVALID_ERROR_RESPONSE
        assert _TOKEN not in str(caught.value)


def _sse(event: dict[str, object]) -> bytes:
    return (
        b"id: "
        + str(event["event_id"]).encode()
        + b"\nevent: "
        + str(event["event_type"]).encode()
        + b"\ndata: "
        + _canonical(event)
        + b"\n\n"
    )


def test_sse_replay_is_digest_bound_and_cross_project_events_fail_closed() -> None:
    event = {
        "schema_version": "2",
        "event_id": "event-1",
        "sequence": 1,
        "occurred_at": "2026-07-23T06:00:00Z",
        "project_id": "project-1",
        "event_type": "project_head_activated",
        "successor_transition_id": "transition-1",
        "project_head": _head().model_dump(mode="json"),
    }
    bodies: Iterator[bytes] = iter(
        [
            _sse(event),
            _sse(event),
            _sse({**event, "sequence": 2}),
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/version":
            return httpx.Response(200, json=_version())
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=next(bodies),
        )

    with _client(handler) as client:
        client.version()
        with client.events() as stream:
            assert next(stream).data.event_id == "event-1"
        with client.events(last_event_id="event-1") as stream:
            assert next(stream).data.event_id == "event-1"
        with client.events(last_event_id="event-1") as stream:
            with pytest.raises(CoreClientErrorV2) as caught:
                next(stream)
        assert caught.value.error.code == CoreClientLocalErrorCodeV2.SSE_PROTOCOL_ERROR


def test_bootstrap_freezes_request_and_binds_core_created_project() -> None:
    request = m.ProjectCreateV2(display_name="Project", config=_config())

    def handler(http_request: httpx.Request) -> httpx.Response:
        if http_request.url.path == "/version":
            return httpx.Response(200, json=_version())
        assert http_request.url.path == "/v2/projects"
        return httpx.Response(201, json=_project().model_dump(mode="json"))

    connection = CoreBootstrapTunnelConnectionV2(
        endpoint="http://127.0.0.1:49201",
        bearer_token=_TOKEN,
        profile_id="profile-1",
        profile_connection_generation=3,
        session_id="session-1",
    )
    with CoreProjectBootstrapClientV2(
        connection,
        transport=httpx.MockTransport(handler),
    ) as client:
        client.version()
        result = client.create_project(request, idempotency_key="create-project-0001")
        assert result.project.project_id == "project-1"
        assert result.connection.project_id == "project-1"
        with pytest.raises(CoreClientErrorV2):
            client.create_project(
                m.ProjectCreateV2(display_name="Changed", config=_config()),
                idempotency_key="create-project-0002",
            )


def test_capabilities_accept_json_arrays_but_reject_scalar_coercion() -> None:
    payload = {
        "schema_version": "1",
        "core_version": "0.1.10",
        "registry_digest": "a" * 64,
        "evaluated_profile": {
            "execution_mode": "subscription",
            "capture_mode": "transcript",
            "harness_id": "codex",
            "harness_capabilities": [],
            "runtime_capabilities": [],
        },
        "targets": [],
    }
    invalid = {**payload, "core_version": 19}
    responses = iter((payload, invalid))

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/version":
            return httpx.Response(200, json=_version())
        return httpx.Response(200, json=next(responses))

    with _client(handler) as client:
        client.version()
        assert client.capabilities("codex_subscription_transcript").targets == ()
        with pytest.raises(CoreClientErrorV2) as caught:
            client.capabilities("codex_subscription_transcript")
        assert caught.value.error.code == CoreClientLocalErrorCodeV2.INVALID_RESPONSE


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(
            200,
            content=b'{"schema_version":"2","schema_version":"2"}',
            headers={"content-type": "application/json"},
        ),
        httpx.Response(
            200,
            content=b"{}",
            headers={"content-type": "text/plain"},
        ),
        httpx.Response(307, headers={"location": "http://127.0.0.1:49202/version"}),
        httpx.Response(
            200,
            content=b"{}",
            headers={
                "content-type": "application/json",
                "content-length": str(4 * 1024 * 1024 + 1),
            },
        ),
    ],
)
def test_malicious_json_framing_content_type_redirect_and_size_fail_closed(
    response: httpx.Response,
) -> None:
    with _client(lambda _request: response) as client:
        with pytest.raises(CoreClientErrorV2) as caught:
            client.version()
        assert caught.value.error.code in {
            CoreClientLocalErrorCodeV2.INVALID_RESPONSE,
            CoreClientLocalErrorCodeV2.REDIRECT_REJECTED,
            CoreClientLocalErrorCodeV2.RESPONSE_TOO_LARGE,
        }


def test_transport_loss_after_mutation_send_has_unknown_outcome() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/version":
            return httpx.Response(200, json=_version())
        raise httpx.ReadError("lost", request=request)

    with _client(handler) as client:
        client.version()
        with pytest.raises(CoreMutationOutcomeUnknownV2):
            client.submit_task(
                m.TaskSubmitRequestV2(
                    project_id="project-1",
                    expected_project_admission_etag='"' + "7" * 64 + '"',
                    expected_project_head_id="head-0",
                    expected_project_head_manifest_sha256="5" * 64,
                    expected_project_config_sha256=m.project_config_sha256_for(_config()),
                ),
                idempotency_key="submit-task-0001",
            )


def test_close_seals_inflight_response_before_delivery() -> None:
    entered = threading.Event()
    release = threading.Event()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/version":
            return httpx.Response(200, json=_version())
        entered.set()
        assert release.wait(timeout=3)
        return httpx.Response(200, json=_project().model_dump(mode="json"))

    client = _client(handler)
    client.version()
    with ThreadPoolExecutor(max_workers=2) as executor:
        request = executor.submit(client.get_project)
        assert entered.wait(timeout=2)
        closing = executor.submit(client.close)
        release.set()
        with pytest.raises(CoreClientErrorV2) as caught:
            request.result(timeout=3)
        closing.result(timeout=3)
    assert caught.value.error.code == CoreClientLocalErrorCodeV2.CLIENT_CLOSED
