from __future__ import annotations

from pathlib import Path
import time

from fastapi.testclient import TestClient
import pytest

from openevo.backend import launcher
from openevo.backend import science_execution_v2, science_successor_preparer_v2
from openevo.backend.contracts.v1.provider import create_core_control_app
from openevo.backend.contracts.v2.provider import (
    RELEASE_DAEMON_FEATURE_FLAGS_V2,
    CoreControlProviderV2,
)
from openevo.backend.contracts.v2.snapshots import (
    events_schema_sha256,
    openapi_sha256,
)
from openevo.backend.contracts.v2.store import CoreControlStoreV2
from openevo.backend.project_authority_v2 import ProjectAuthorityV2
from openevo.backend.runtime_identity import release_runtime_contract_sha256
from openevo.backend.science_execution_v2 import ScienceAttemptExecutorV2
from openevo.backend.science_run_owner import CoreScienceTaskOwnerV2
from openevo.backend.science_successor_preparer_v2 import (
    ProductionScienceSuccessorPreparerV2,
)
from openevo.backend.service_supervisor import (
    ServiceExecutionMode,
    ServiceGroupSnapshot,
    ServiceRunLease,
    ServiceRunBinding,
    ServiceRunReadinessCode,
)
from openevo.backend.workspace_handoff_v2 import WorkspaceHandoffStoreV2
from openevo.backend.workspace_store_v2 import WorkspaceStoreV2
from openevo.internal_auth import InternalServiceIdentity
from openevo.projects.science.compiler import MANAGED_RUNTIME_IMAGES
from openevo.runtime.managed import MANAGED_RUNTIME_RELEASES
from tests.backend.test_science_execution_v2 import (
    _Rollout,
    _project_config as _execution_project_config,
)
from tests.backend.test_science_successor_preparer_v2 import _Evolution
from tests.framework_testkit import verified_builtin_registry


_TOKEN = "release-daemon-v2-token-" + "x" * 40
_SOURCE_COMMIT = "1" * 40


def _project_config() -> dict[str, object]:
    return {
        "task": {
            "title": "Release composition",
            "objective": "Prove the packaged Daemon owns one real v2 project.",
        },
        "workspace": {"kind": "scratch", "display_name": "Scratch"},
        "execution": {
            "mode": "codex_subscription_transcript",
            "capture_mode": "transcript",
            "token_level_metrics_available": False,
            "harness_id": "codex",
            "codex_model": "gpt-5.5",
            "reasoning_effort": "high",
            "token_limit": 32768,
            "task_network_allow_internet": False,
        },
        "evolution": {"targets": {}},
    }


def _binding(registry_sha256: str) -> ServiceRunBinding:
    image = MANAGED_RUNTIME_IMAGES["managed_science"]
    identity = InternalServiceIdentity(
        service_id="core-control",
        generation_digest="2" * 64,
        registry_digest=registry_sha256,
        framework_lock_digest="4" * 64,
        credential="daemon-v2-composition-test-credential-" + "x" * 40,
    )
    return ServiceRunBinding(
        execution_mode=ServiceExecutionMode.CODEX_SUBSCRIPTION_TRANSCRIPT,
        codex_model="gpt-5.5",
        runtime_image=image,
        runtime_image_immutable_reference=(
            MANAGED_RUNTIME_RELEASES["managed_science"].immutable_reference
        ),
        runtime_identity_digest="3" * 64,
        generation_digest=identity.generation_digest,
        registry_digest=registry_sha256,
        framework_lock_digest=identity.framework_lock_digest,
        rollout_url="http://127.0.0.1:41001",
        evolution_backend_url="http://127.0.0.1:41002",
        gateway_url="http://127.0.0.1:41003",
        _identity=identity,
    )


class _ReleaseServices:
    def __init__(self, root: Path, registry_sha256: str) -> None:
        self._root = root
        self._root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._binding = _binding(registry_sha256)
        self.closed = False
        self.released = False

    @property
    def workspace_handoff_root(self) -> Path:
        return self._root

    def run_binding(self) -> ServiceRunBinding:
        return self._binding

    def ensure_run_binding(self, *_args: object, **_kwargs: object) -> tuple[object, object]:
        snapshot = ServiceGroupSnapshot(
            execution_mode=self._binding.execution_mode,
            services_available=True,
            run_ready=True,
            run_readiness_code=ServiceRunReadinessCode.READY,
            generation_digest=self._binding.generation_digest,
            services=(),
            runtime_image=self._binding.runtime_image,
            runtime_image_immutable_reference=(self._binding.runtime_image_immutable_reference),
            runtime_identity_digest=self._binding.runtime_identity_digest,
        )
        return snapshot, ServiceRunLease(
            binding=self._binding,
            _release=lambda: setattr(self, "released", True),
        )

    def authenticates_run_service(self, _headers: object) -> bool:
        return False

    def close(self) -> None:
        self.closed = True


def test_release_composition_mounts_only_production_v2_authority(
    tmp_path: Path,
) -> None:
    registry = verified_builtin_registry(tmp_path / "registry")
    services = _ReleaseServices(
        tmp_path / "managed-services" / "workspace-handoffs",
        registry.snapshot.registry_digest,
    )
    composition = launcher._build_release_daemon_v2_composition(
        state_root=tmp_path / "state",
        bearer_token=_TOKEN,
        source_commit=_SOURCE_COMMIT,
        executable_registry=registry,
        service_supervisor=services,
        runtime_contract_sha256=release_runtime_contract_sha256(),
    )

    assert type(composition.provider) is CoreControlProviderV2
    assert type(composition.catalog) is CoreControlStoreV2
    assert type(composition.workspaces) is WorkspaceStoreV2
    assert type(composition.workspace_handoffs) is WorkspaceHandoffStoreV2
    assert type(composition.task_owner) is CoreScienceTaskOwnerV2
    assert type(composition.project_authority) is ProjectAuthorityV2
    assert type(composition.attempt_executor) is ScienceAttemptExecutorV2
    assert type(composition.successor_preparer) is ProductionScienceSuccessorPreparerV2

    headers = {"Authorization": f"Bearer {_TOKEN}"}
    with TestClient(composition.app) as client:
        version = client.get("/version")
        assert version.status_code == 200
        discovered = version.json()
        assert discovered["provider_kind"] == "openevo_daemon"
        assert discovered["preferred_major"] == 2
        assert discovered["supported_majors"] == [2]
        assert discovered["feature_flags"] == list(RELEASE_DAEMON_FEATURE_FLAGS_V2)
        assert discovered["contracts"] == [
            {
                "schema_version": "2",
                "api_major": 2,
                "openapi_sha256": openapi_sha256(),
                "event_schema_sha256": events_schema_sha256(),
                "access": "mutation",
                "mutation_compatible": True,
            }
        ]
        assert discovered["registry_sha256"] == registry.snapshot.registry_digest
        assert discovered["runtime_contract_sha256"] == release_runtime_contract_sha256()

        legacy = client.get("/v1/status", headers=headers)
        assert legacy.status_code == 426
        assert legacy.json()["code"] == "contract_version_unsupported"
        status = client.get("/v2/system/status", headers=headers)
        assert status.status_code == 200
        assert status.json()["status"] == "ready"

        created = client.post(
            "/v2/projects",
            headers={**headers, "Idempotency-Key": "release-project-create-0001"},
            json={"display_name": "Release project", "config": _project_config()},
        )
        assert created.status_code == 201
        project = created.json()
        assert project["state"] == "ready"
        assert project["active_project_head"]["generation"] == 0
        project_id = project["project_id"]
        active = client.get(
            f"/v2/projects/{project_id}/heads/active",
            headers=headers,
        )
        assert active.status_code == 200
        assert active.json() == project["active_project_head"]

        private_admission = client.post(
            "/internal/v1/run-admissions/verify",
            json={},
        )
        assert private_admission.status_code == 401

    assert services.closed is True
    composition.close()


def _wait_remote_task(
    client: TestClient,
    task_id: str,
    *,
    headers: dict[str, str],
    expected: str,
) -> dict[str, object]:
    deadline = time.monotonic() + 10
    latest: dict[str, object] = {}
    while time.monotonic() < deadline:
        response = client.get(f"/v2/tasks/{task_id}", headers=headers)
        assert response.status_code == 200
        latest = response.json()
        if latest["state"] == expected:
            return latest
        time.sleep(0.01)
    raise AssertionError(f"release v2 Task did not reach {expected}: {latest}")


def test_release_composition_executes_two_sessions_recovers_events_and_reconnects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = verified_builtin_registry(tmp_path / "registry")
    services = _ReleaseServices(
        tmp_path / "managed-services" / "workspace-handoffs",
        registry.snapshot.registry_digest,
    )
    state_root = tmp_path / "state"
    composition = launcher._build_release_daemon_v2_composition(
        state_root=state_root,
        bearer_token=_TOKEN,
        source_commit=_SOURCE_COMMIT,
        executable_registry=registry,
        service_supervisor=services,
        runtime_contract_sha256=release_runtime_contract_sha256(),
    )
    gateway_sessions = tmp_path / "gateway-sessions"
    gateway_sessions.mkdir(mode=0o700)
    rollout = _Rollout(
        composition.workspace_handoffs,
        services.run_binding(),
        gateway_sessions,
    )
    evolution = _Evolution(registry.snapshot.registry_digest)
    monkeypatch.setattr(
        science_execution_v2,
        "RolloutHttpClient",
        lambda _url, *, headers: rollout,
    )
    monkeypatch.setattr(
        science_successor_preparer_v2,
        "EvolutionHttpClient",
        lambda _url, *, headers: evolution,
    )

    headers = {"Authorization": f"Bearer {_TOKEN}"}
    with TestClient(composition.app) as client:
        created = client.post(
            "/v2/projects",
            headers={**headers, "Idempotency-Key": "release-execution-project-0001"},
            json={
                "display_name": "Executable release project",
                "config": _execution_project_config().model_dump(mode="json"),
            },
        )
        assert created.status_code == 201
        project = created.json()
        project_id = project["project_id"]

        def submit(current: dict[str, object], key: str) -> str:
            head = current["active_project_head"]
            assert isinstance(head, dict)
            response = client.post(
                "/v2/tasks",
                headers={**headers, "Idempotency-Key": key},
                json={
                    "project_id": project_id,
                    "expected_project_admission_etag": current["admission_etag"],
                    "expected_project_head_id": head["project_head_id"],
                    "expected_project_head_manifest_sha256": head["manifest_sha256"],
                    "expected_project_config_sha256": current["project_config_sha256"],
                },
            )
            assert response.status_code == 202, response.text
            return response.json()["task_id"]

        first_id = submit(project, "release-task-session-0001")
        first = _wait_remote_task(
            client,
            first_id,
            headers=headers,
            expected="completed",
        )
        assert first["successor_transition"]["successor_project_head"]["generation"] == 1
        timeline = client.get(f"/v2/tasks/{first_id}/timeline", headers=headers)
        assert timeline.status_code == 200
        first_events = [item["event_type"] for item in timeline.json()["items"]]
        assert "dataset_sealed" in first_events
        assert "project_head_activated" in first_events

        refreshed = client.get(f"/v2/projects/{project_id}", headers=headers)
        assert refreshed.status_code == 200
        assert refreshed.json()["active_project_head"]["generation"] == 1
        second_id = submit(refreshed.json(), "release-task-session-0002")
        second = _wait_remote_task(
            client,
            second_id,
            headers=headers,
            expected="completed",
        )
        assert second["successor_transition"]["successor_project_head"]["generation"] == 2
        assert len(rollout.requests) == 2
        assert rollout.requests[0].runtime_context_binding.source == "empty_genesis"
        assert rollout.requests[1].runtime_context_binding.source == ("materialized_successor")
        assert rollout.input_answer_before_run == [None, "accepted\n"]
        assert services.released is True

    restarted_services = _ReleaseServices(
        tmp_path / "managed-services" / "workspace-handoffs",
        registry.snapshot.registry_digest,
    )
    restarted = launcher._build_release_daemon_v2_composition(
        state_root=state_root,
        bearer_token=_TOKEN,
        source_commit=_SOURCE_COMMIT,
        executable_registry=registry,
        service_supervisor=restarted_services,
        runtime_contract_sha256=release_runtime_contract_sha256(),
    )
    with TestClient(restarted.app) as client:
        recovered = client.get(f"/v2/projects/{project_id}", headers=headers)
        assert recovered.status_code == 200
        assert recovered.json()["active_project_head"]["generation"] == 2
        tasks = client.get(
            "/v2/tasks",
            headers=headers,
            params={"project_id": project_id},
        )
        assert tasks.status_code == 200
        assert [item["state"] for item in tasks.json()["items"]] == [
            "completed",
            "completed",
        ]
        replay = client.get(f"/v2/tasks/{first_id}/timeline", headers=headers)
        assert replay.status_code == 200
        assert any(
            item["event_type"] == "project_head_activated" for item in replay.json()["items"]
        )
    assert restarted_services.closed is True


def test_release_ready_payload_binds_the_exact_v2_build(tmp_path: Path) -> None:
    registry = verified_builtin_registry(tmp_path / "registry")
    services = _ReleaseServices(
        tmp_path / "managed-services" / "workspace-handoffs",
        registry.snapshot.registry_digest,
    )
    composition = launcher._build_release_daemon_v2_composition(
        state_root=tmp_path / "state",
        bearer_token=_TOKEN,
        source_commit=_SOURCE_COMMIT,
        executable_registry=registry,
        service_supervisor=services,
        runtime_contract_sha256=release_runtime_contract_sha256(),
    )
    try:
        payload = launcher._release_daemon_v2_ready_payload(
            composition.provider,
            generation="5" * 32,
            release_identity="6" * 64,
        )
        assert set(payload) == {
            "api_major",
            "build_id",
            "event_schema_sha256",
            "feature_set_sha256",
            "generation",
            "openapi_sha256",
            "provider_kind",
            "registry_digest",
            "release_identity",
            "release_version",
            "runtime_contract_sha256",
            "schema_version",
            "source_commit",
        }
        assert payload["schema_version"] == 2
        assert payload["api_major"] == 2
        assert payload["provider_kind"] == "openevo_daemon"
        assert payload["openapi_sha256"] == openapi_sha256()
        assert payload["event_schema_sha256"] == events_schema_sha256()
        assert payload["registry_digest"] == registry.snapshot.registry_digest
        assert payload["runtime_contract_sha256"] == release_runtime_contract_sha256()
    finally:
        composition.close()


def test_v019_release_cannot_build_the_legacy_v1_mutation_provider(
    tmp_path: Path,
) -> None:
    with pytest.raises(RuntimeError, match="Core Control API v2"):
        create_core_control_app(
            state_root=tmp_path / "legacy-v1",
            bearer_token=_TOKEN,
            build_version="0.1.9",
            source_commit=_SOURCE_COMMIT,
            build_channel="release",
        )
    assert not (tmp_path / "legacy-v1").exists()
