from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import threading

import httpx
import pytest

from desktop.sidecar.contracts.v2 import models as local_v2
from desktop.sidecar.core_bridge_store_v2 import DesktopCoreBridgeStoreV2
from desktop.sidecar.core_bridge_v2 import (
    CoreHostAttachmentV2,
    CoreTunnelHandleV2,
    DesktopCoreBridgeErrorV2,
    DesktopCoreBridgeV2,
)
from openevo.backend.contracts.v2 import models as m
from tests.openevo.sidecar.test_core_client_v2 import (
    _TOKEN,
    _canonical,
    _config,
    _head,
    _project,
    _sse,
    _version,
)


def _capabilities() -> dict[str, object]:
    return {
        "schema_version": "1",
        "core_version": "0.1.9",
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


def _status() -> dict[str, object]:
    return {
        "schema_version": "2",
        "status": "ready",
        "release_version": "0.1.9",
        "source_commit": "c" * 40,
        "registry_sha256": "a" * 64,
        "checked_at": "2026-07-23T06:00:00Z",
    }


def _create_request(connection_generation: int = 3) -> local_v2.ProjectCreateV2:
    return local_v2.ProjectCreateV2(
        profile_id="profile-1",
        profile_connection_generation=connection_generation,
        display_name="Project",
        config=_config(),
    )


def _native_create_request(connection_generation: int = 3) -> local_v2.ProjectCreateV2:
    payload = _config().model_dump(mode="json")
    payload["workspace"] = {
        "kind": "native_folder_snapshot",
        "display_name": "Selected workspace",
    }
    return local_v2.ProjectCreateV2(
        profile_id="profile-1",
        profile_connection_generation=connection_generation,
        display_name="Project",
        config=m.ScienceProjectConfigV2.model_validate(payload),
    )


def _native_project() -> m.ProjectV2:
    request = _native_create_request()
    return m.ProjectV2(
        project_id="project-1",
        display_name=request.display_name,
        config=request.config,
        project_config_sha256=m.project_config_sha256_for(request.config),
        active_project_head=None,
        admission_etag=None,
        state="not_ready",
        created_at="2026-07-23T06:00:00Z",
        updated_at="2026-07-23T06:00:00Z",
        etag='"' + "8" * 64 + '"',
    )


def _submit_request() -> m.TaskSubmitRequestV2:
    project = _project()
    head = project.active_project_head
    assert head is not None and project.admission_etag is not None
    return m.TaskSubmitRequestV2(
        project_id=project.project_id,
        expected_project_admission_etag=project.admission_etag,
        expected_project_head_id=head.project_head_id,
        expected_project_head_manifest_sha256=head.manifest_sha256,
        expected_project_config_sha256=project.project_config_sha256,
    )


def _task() -> m.TaskV2:
    head = _head()
    seed = m.TaskAdmissionRefV2.model_construct(
        schema_version="2",
        task_admission_id="admission-1",
        task_id="task-1",
        project_id="project-1",
        predecessor_project_head=head,
        workspace_snapshot=head.workspace_snapshot,
        project_config_sha256=m.project_config_sha256_for(_config()),
        task_envelope_sha256="e" * 64,
        normalized_evolution_intent_sha256="f" * 64,
        registry_sha256="a" * 64,
        admission_sha256="0" * 64,
        admitted_at="2026-07-23T06:00:01Z",
    )
    admission = m.TaskAdmissionRefV2(
        **seed.model_dump(exclude={"admission_sha256"}),
        admission_sha256=m.task_admission_sha256_for(seed),
    )
    attempt = m.AttemptRefV2(
        attempt_id="attempt-1",
        ordinal=1,
        task_id="task-1",
        task_admission_id=admission.task_admission_id,
        admission_sha256=admission.admission_sha256,
        project_id="project-1",
        predecessor_project_head_id=head.project_head_id,
        created_at="2026-07-23T06:00:01Z",
    )
    return m.TaskV2(
        task_id="task-1",
        project_id="project-1",
        admission=admission,
        attempts=[attempt],
        authoritative_attempt_id=None,
        successor_transition=None,
        state="admitted",
        created_at="2026-07-23T06:00:01Z",
        updated_at="2026-07-23T06:00:01Z",
        etag='"' + "9" * 64 + '"',
    )


class _HostService:
    def __init__(self, generation: int = 3) -> None:
        self.generation = generation
        self.calls: list[tuple[str, int]] = []

    def ensure_core(
        self,
        profile_id: str,
        profile_connection_generation: int,
        *,
        deadline: float,
    ) -> CoreHostAttachmentV2:
        assert deadline > 0
        self.calls.append((profile_id, profile_connection_generation))
        if profile_connection_generation != self.generation:
            raise RuntimeError("stale generation")
        return CoreHostAttachmentV2(
            profile_id=profile_id,
            profile_connection_generation=profile_connection_generation,
            remote_port=8765,
            bearer_token=_TOKEN,
            bearer_identity=hashlib.sha256(_TOKEN.encode()).hexdigest(),
        )


class _TunnelFactory:
    def __init__(self) -> None:
        self.opened: list[tuple[str, int, int, str]] = []
        self.closed: list[str] = []

    def open_tunnel(
        self,
        *,
        profile_id: str,
        profile_connection_generation: int,
        remote_port: int,
        session_id: str,
        deadline: float,
    ) -> CoreTunnelHandleV2:
        assert deadline > 0
        self.opened.append((profile_id, profile_connection_generation, remote_port, session_id))
        return CoreTunnelHandleV2(
            endpoint="http://127.0.0.1:49201",
            profile_id=profile_id,
            profile_connection_generation=profile_connection_generation,
            session_id=session_id,
            close_callback=lambda: self.closed.append(session_id),
        )


class _Publisher:
    def __init__(self) -> None:
        self.payloads: list[local_v2.CoreAuthorityEventPayloadV2] = []

    def publish(
        self, payload: local_v2.DesktopEventPayloadV2
    ) -> local_v2.DesktopEventEnvelopeV2 | None:
        assert type(payload) is local_v2.CoreAuthorityEventPayloadV2
        self.payloads.append(payload)
        return None


def _store(tmp_path: Path) -> DesktopCoreBridgeStoreV2:
    root = tmp_path / "bridge-state"
    root.mkdir(mode=0o700)
    return DesktopCoreBridgeStoreV2(root)


def _base_handler(
    requests: list[httpx.Request],
    *,
    project: m.ProjectV2 | None = None,
):
    remote = project or _project()

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/version":
            return httpx.Response(200, json=_version())
        if request.url.path == "/v2/system/status":
            return httpx.Response(200, json=_status())
        if request.url.path == "/v2/capabilities":
            return httpx.Response(200, json=_capabilities())
        if request.url.path == "/v2/projects" and request.method == "POST":
            return httpx.Response(201, json=remote.model_dump(mode="json"))
        if request.url.path == "/v2/projects/project-1":
            return httpx.Response(200, json=remote.model_dump(mode="json"))
        raise AssertionError(f"unexpected Core request: {request.method} {request.url.path}")

    return handler


def test_activation_bootstraps_only_through_private_project_tunnel_and_persists_mapping(
    tmp_path: Path,
) -> None:
    requests: list[httpx.Request] = []
    host = _HostService()
    tunnels = _TunnelFactory()
    with _store(tmp_path) as store:
        bridge = DesktopCoreBridgeV2(
            host_service=host,
            tunnel_factory=tunnels,
            persistence=store,
            transport_factory=lambda: httpx.MockTransport(_base_handler(requests)),
        )
        activation = bridge.activate_project(
            "desktop-project-1",
            _create_request(),
            idempotency_key="activate-project-0001",
        )

        assert activation.project == _project()
        assert activation.mapping.core_project_id == "project-1"
        assert activation.mapping.active_project_head == _head()
        assert activation.mapping.project_head_successor_proof == (_head(),)
        assert activation.capabilities.registry_digest == "a" * 64
        assert store.load_mapping("desktop-project-1") == activation.mapping
        assert host.calls == [("profile-1", 3)]
        assert len(tunnels.opened) == 1
        assert all(request.url.host == "127.0.0.1" for request in requests)
        assert all(
            "authorization" in request.headers
            for request in requests
            if request.url.path != "/version"
        )
        assert not hasattr(bridge, "backend_url")
        bridge.close()
    assert len(tunnels.closed) == 1


def test_activation_accepts_exact_initial_native_workspace_authority(
    tmp_path: Path,
) -> None:
    requests: list[httpx.Request] = []
    remote = _native_project()
    tunnels = _TunnelFactory()
    with _store(tmp_path) as store:
        bridge = DesktopCoreBridgeV2(
            host_service=_HostService(),
            tunnel_factory=tunnels,
            persistence=store,
            transport_factory=lambda: httpx.MockTransport(
                _base_handler(requests, project=remote)
            ),
        )

        activation = bridge.activate_project(
            "desktop-project-native",
            _native_create_request(),
            idempotency_key="activate-native-workspace-0001",
        )

        assert activation.project == remote
        assert activation.mapping.active_project_head is None
        assert activation.mapping.project_admission_etag is None
        bridge.close()
    assert len(tunnels.closed) == 1


def test_reconnect_reuses_exact_core_project_and_advances_only_connection_mapping(
    tmp_path: Path,
) -> None:
    requests: list[httpx.Request] = []
    host = _HostService()
    tunnels = _TunnelFactory()
    with _store(tmp_path) as store:
        bridge = DesktopCoreBridgeV2(
            host_service=host,
            tunnel_factory=tunnels,
            persistence=store,
            transport_factory=lambda: httpx.MockTransport(_base_handler(requests)),
        )
        first = bridge.activate_project(
            "desktop-project-1",
            _create_request(3),
            idempotency_key="activate-project-0001",
        )
        bridge.deactivate_project("desktop-project-1", 3)
        post_count = sum(
            request.method == "POST" and request.url.path == "/v2/projects" for request in requests
        )

        host.generation = 4
        second = bridge.activate_project(
            "desktop-project-1",
            _create_request(4),
            idempotency_key="activate-project-0002",
        )

        assert second.mapping.mapping_generation == first.mapping.mapping_generation + 1
        assert second.mapping.profile_connection_generation == 4
        assert second.mapping.project_head_successor_proof == ()
        assert (
            sum(
                request.method == "POST" and request.url.path == "/v2/projects"
                for request in requests
            )
            == post_count
        )
        bridge.close()


def test_reconnect_persists_a_bounded_exact_multi_head_successor_proof(
    tmp_path: Path,
) -> None:
    requests: list[httpx.Request] = []
    head0 = _head()
    head1 = head0.model_copy(
        update={
            "project_head_id": "head-1",
            "generation": 1,
            "predecessor_project_head_id": head0.project_head_id,
            "manifest_sha256": "6" * 64,
        }
    )
    head2 = head1.model_copy(
        update={
            "project_head_id": "head-2",
            "generation": 2,
            "predecessor_project_head_id": head1.project_head_id,
            "manifest_sha256": "7" * 64,
        }
    )
    advanced = _project().model_copy(
        update={
            "active_project_head": head2,
            "etag": '"' + "6" * 64 + '"',
            "updated_at": "2026-07-23T06:00:02Z",
        }
    )
    host = _HostService()
    use_advanced = False

    def handler(request: httpx.Request) -> httpx.Response:
        if use_advanced and request.url.path == "/v2/projects/project-1":
            requests.append(request)
            return httpx.Response(200, json=advanced.model_dump(mode="json"))
        if use_advanced and request.url.path == "/v2/project-heads/head-1":
            requests.append(request)
            return httpx.Response(200, json=head1.model_dump(mode="json"))
        return _base_handler(requests)(request)

    with _store(tmp_path) as store:
        bridge = DesktopCoreBridgeV2(
            host_service=host,
            tunnel_factory=_TunnelFactory(),
            persistence=store,
            transport_factory=lambda: httpx.MockTransport(handler),
        )
        bridge.activate_project(
            "desktop-project-1",
            _create_request(3),
            idempotency_key="activate-project-0001",
        )
        bridge.deactivate_project("desktop-project-1", 3)
        host.generation = 4
        use_advanced = True

        activation = bridge.activate_project(
            "desktop-project-1",
            _create_request(4),
            idempotency_key="activate-project-0002",
        )

        assert activation.mapping.active_project_head == head2
        assert activation.mapping.project_head_successor_proof == (head1, head2)
        assert store.load_mapping("desktop-project-1") == activation.mapping
        bridge.close()


def test_mutation_unknown_is_durable_and_same_key_replays_without_ssh(
    tmp_path: Path,
) -> None:
    requests: list[httpx.Request] = []
    task = _task()
    evolved_task = task.model_copy(
        update={
            "state": "running",
            "updated_at": "2026-07-23T06:00:02Z",
            "etag": '"' + "a" * 64 + '"',
        }
    )
    submit_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal submit_calls
        try:
            return _base_handler(requests)(request)
        except AssertionError:
            pass
        requests.append(request)
        if request.url.path == "/v2/tasks" and request.method == "POST":
            submit_calls += 1
            if submit_calls == 1:
                raise httpx.ReadError("lost after send", request=request)
            return httpx.Response(202, json=task.model_dump(mode="json"))
        if request.url.path == "/v2/tasks/task-1":
            return httpx.Response(200, json=evolved_task.model_dump(mode="json"))
        raise AssertionError(request.url.path)

    host = _HostService()
    with _store(tmp_path) as store:
        bridge = DesktopCoreBridgeV2(
            host_service=host,
            tunnel_factory=_TunnelFactory(),
            persistence=store,
            transport_factory=lambda: httpx.MockTransport(handler),
        )
        bridge.activate_project(
            "desktop-project-1",
            _create_request(),
            idempotency_key="activate-project-0001",
        )
        with pytest.raises(DesktopCoreBridgeErrorV2) as unknown:
            bridge.submit_task(
                "desktop-project-1",
                3,
                _submit_request(),
                idempotency_key="submit-task-0001",
            )
        assert unknown.value.error.code == "core_mutation_outcome_unknown"
        durable = store.load_mutation("desktop-project-1", "submit_task_v2", "submit-task-0001")
        assert durable is not None and durable.state.value == "unknown"

        assert (
            bridge.submit_task(
                "desktop-project-1",
                3,
                _submit_request(),
                idempotency_key="submit-task-0001",
            )
            == task
        )
        assert (
            bridge.submit_task(
                "desktop-project-1",
                3,
                _submit_request(),
                idempotency_key="submit-task-0001",
            )
            == evolved_task
        )
        assert submit_calls == 2
        assert host.calls == [("profile-1", 3)]
        bridge.close()


def test_event_delivery_persists_exact_core_cursor_and_publishes_safe_local_event(
    tmp_path: Path,
) -> None:
    requests: list[httpx.Request] = []
    publisher = _Publisher()
    event = {
        "schema_version": "2",
        "event_id": "event-1",
        "sequence": 1,
        "occurred_at": "2026-07-23T06:00:01Z",
        "project_id": "project-1",
        "event_type": "project_head_activated",
        "successor_transition_id": "transition-1",
        "project_head": _head().model_dump(mode="json"),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v2/events":
            requests.append(request)
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=_sse(event),
            )
        return _base_handler(requests)(request)

    with _store(tmp_path) as store:
        bridge = DesktopCoreBridgeV2(
            host_service=_HostService(),
            tunnel_factory=_TunnelFactory(),
            persistence=store,
            event_publisher=publisher,
            transport_factory=lambda: httpx.MockTransport(handler),
        )
        bridge.activate_project(
            "desktop-project-1",
            _create_request(),
            idempotency_key="activate-project-0001",
        )
        with bridge.events("desktop-project-1", 3) as stream:
            assert next(stream).data.event_id == "event-1"

        mapping = store.load_mapping("desktop-project-1")
        assert mapping is not None
        assert mapping.last_core_event_id == "event-1"
        assert mapping.last_core_event_sequence == 1
        expected_digest = hashlib.sha256(
            _canonical(
                m.ProjectHeadActivatedEventV2.model_validate(event, strict=True).model_dump(
                    mode="json"
                )
            )
        ).hexdigest()
        assert mapping.last_core_event_payload_sha256 == expected_digest
        assert publisher.payloads == [
            local_v2.CoreAuthorityEventPayloadV2(
                payload_kind="core_authority_changed",
                profile_id="profile-1",
                project_id="desktop-project-1",
                core_event_id="event-1",
                core_event_sequence=1,
                core_event_type="project_head_activated",
                core_payload_sha256=expected_digest,
            )
        ]
        bridge.close()


def test_candidate_authority_drift_is_safe_and_does_not_replace_active_session(
    tmp_path: Path,
) -> None:
    requests: list[httpx.Request] = []
    bad = _project().model_copy(update={"project_config_sha256": "0" * 64})
    host = _HostService()
    tunnels = _TunnelFactory()
    current_handler = _base_handler(requests)
    use_bad = False

    def handler(request: httpx.Request) -> httpx.Response:
        if use_bad and request.url.path == "/v2/projects/project-1":
            return httpx.Response(200, json=bad.model_dump(mode="json"))
        return current_handler(request)

    with _store(tmp_path) as store:
        bridge = DesktopCoreBridgeV2(
            host_service=host,
            tunnel_factory=tunnels,
            persistence=store,
            transport_factory=lambda: httpx.MockTransport(handler),
        )
        bridge.activate_project(
            "desktop-project-1",
            _create_request(),
            idempotency_key="activate-project-0001",
        )
        use_bad = True
        with pytest.raises(DesktopCoreBridgeErrorV2) as caught:
            bridge.activate_project(
                "desktop-project-1",
                _create_request(),
                idempotency_key="activate-project-0002",
            )
        rendered = json.dumps(caught.value.error.model_dump(mode="json"))
        assert _TOKEN not in rendered
        assert "http://" not in rendered
        assert "/Users/" not in rendered
        use_bad = False
        assert bridge.get_project("desktop-project-1", 3) == _project()
        bridge.close()


def test_bridge_generation_seals_inflight_read_when_project_is_deactivated(
    tmp_path: Path,
) -> None:
    requests: list[httpx.Request] = []
    entered = threading.Event()
    release = threading.Event()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v2/tasks" and request.method == "GET":
            entered.set()
            assert release.wait(timeout=3)
            return httpx.Response(
                200,
                json={
                    "schema_version": "2",
                    "items": [],
                    "next_cursor": None,
                    "has_more": False,
                },
            )
        return _base_handler(requests)(request)

    with _store(tmp_path) as store:
        bridge = DesktopCoreBridgeV2(
            host_service=_HostService(),
            tunnel_factory=_TunnelFactory(),
            persistence=store,
            transport_factory=lambda: httpx.MockTransport(handler),
        )
        bridge.activate_project(
            "desktop-project-1",
            _create_request(),
            idempotency_key="activate-project-0001",
        )
        with ThreadPoolExecutor(max_workers=2) as executor:
            read = executor.submit(bridge.list_tasks, "desktop-project-1", 3)
            assert entered.wait(timeout=2)
            deactivate = executor.submit(bridge.deactivate_project, "desktop-project-1", 3)
            release.set()
            with pytest.raises(DesktopCoreBridgeErrorV2) as caught:
                read.result(timeout=3)
            deactivate.result(timeout=3)
        assert caught.value.error.code in {
            "core_connection_failed",
            "active_core_project_superseded",
        }
        bridge.close()


def test_bridge_close_retains_failed_tunnel_cleanup_for_an_exact_retry(
    tmp_path: Path,
) -> None:
    class _FailOnceTunnelFactory(_TunnelFactory):
        def __init__(self) -> None:
            super().__init__()
            self.close_attempts = 0

        def open_tunnel(
            self,
            *,
            profile_id: str,
            profile_connection_generation: int,
            remote_port: int,
            session_id: str,
            deadline: float,
        ) -> CoreTunnelHandleV2:
            assert deadline > 0
            self.opened.append(
                (profile_id, profile_connection_generation, remote_port, session_id)
            )

            def close() -> None:
                self.close_attempts += 1
                if self.close_attempts == 1:
                    raise RuntimeError("transient close failure")
                self.closed.append(session_id)

            return CoreTunnelHandleV2(
                endpoint="http://127.0.0.1:49201",
                profile_id=profile_id,
                profile_connection_generation=profile_connection_generation,
                session_id=session_id,
                close_callback=close,
            )

    tunnels = _FailOnceTunnelFactory()
    with _store(tmp_path) as store:
        bridge = DesktopCoreBridgeV2(
            host_service=_HostService(),
            tunnel_factory=tunnels,
            persistence=store,
            transport_factory=lambda: httpx.MockTransport(_base_handler([])),
        )
        bridge.activate_project(
            "desktop-project-1",
            _create_request(),
            idempotency_key="activate-project-0001",
        )

        with pytest.raises(DesktopCoreBridgeErrorV2) as first_close:
            bridge.close()
        assert first_close.value.error.code == "core_tunnel_close_failed"

        bridge.close()
        assert tunnels.close_attempts == 2
        assert len(tunnels.closed) == 1


def test_invalid_mutation_type_is_rejected_before_replay_identity_is_persisted(
    tmp_path: Path,
) -> None:
    with _store(tmp_path) as store:
        bridge = DesktopCoreBridgeV2(
            host_service=_HostService(),
            tunnel_factory=_TunnelFactory(),
            persistence=store,
            transport_factory=lambda: httpx.MockTransport(_base_handler([])),
        )
        bridge.activate_project(
            "desktop-project-1",
            _create_request(),
            idempotency_key="activate-project-0001",
        )

        with pytest.raises(TypeError):
            bridge.update_project(
                "desktop-project-1",
                3,
                m.ActionRequestV2(expected_project_head_id="head-0"),  # type: ignore[arg-type]
                if_match='"' + "5" * 64 + '"',
                idempotency_key="update-project-0001",
            )
        assert (
            store.load_mutation(
                "desktop-project-1",
                "update_project_v2",
                "update-project-0001",
            )
            is None
        )
        bridge.close()
