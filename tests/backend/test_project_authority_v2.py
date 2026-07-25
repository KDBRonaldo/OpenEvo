from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import hashlib
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from openevo.backend.contracts.v2.app import create_core_control_v2_contract_app
from openevo.backend.contracts.v2.models import (
    ProjectCreateV2,
    ProjectUpdateV2,
    ProjectValidationRequestV2,
    ScienceProjectConfigV2,
    TaskSubmitRequestV2,
    project_config_sha256_for,
)
from openevo.backend.contracts.v2.store import (
    CoreControlStoreV2,
    ProjectPreconditionFailedV2,
    project_etag_payload,
)
from openevo.backend.contracts.v2.provider import CoreControlProviderV2
import openevo.backend.contracts.v2.provider as provider_module
from openevo.backend.project_authority_v2 import (
    ProjectAuthorityInvalidV2,
    ProjectAuthorityV2,
    normalized_evolution_intent_sha256_for,
)
import openevo.backend.project_authority_v2 as authority_module
from openevo.backend.run_control import CoreTaskControlError
from openevo.backend.science_run_owner import CoreScienceTaskOwnerV2
import openevo.backend.science_run_store as task_store_module
from openevo.backend.science_run_store import ScienceProjectReadinessBlockerV2
from openevo.backend.service_supervisor import ServiceExecutionMode, ServiceRunBinding
from openevo.backend.workspace_store_v2 import WorkspaceStoreV2
from openevo.evolution.framework import canonical_digest
from openevo.internal_auth import InternalServiceIdentity
from openevo.projects.science.compiler import MANAGED_RUNTIME_IMAGES
from openevo.runtime.managed import MANAGED_RUNTIME_RELEASES
from tests.framework_testkit import verified_builtin_registry


_RUNTIME_CONTRACT_SHA256 = "d" * 64


class _Clock:
    def __init__(self) -> None:
        self._next = datetime(2026, 7, 23, 4, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        value = self._next
        self._next += timedelta(microseconds=1)
        return value


def _config(**execution_changes: object) -> ScienceProjectConfigV2:
    execution: dict[str, object] = {
        "mode": "codex_subscription_transcript",
        "capture_mode": "transcript",
        "token_level_metrics_available": False,
        "harness_id": "codex",
        "codex_model": "gpt-5.5",
        "reasoning_effort": "high",
        "token_limit": 32768,
        "task_network_allow_internet": False,
    }
    execution.update(execution_changes)
    return ScienceProjectConfigV2.model_validate(
        {
            "task": {
                "title": "Genesis task",
                "objective": "Create a complete Core-owned v2 genesis.",
            },
            "workspace": {"kind": "scratch", "display_name": "Scratch"},
            "execution": execution,
            "evolution": {"targets": {}},
        }
    )


def _skill_bundle_config() -> ScienceProjectConfigV2:
    payload = _config().model_dump(mode="python")
    payload["evolution"]["targets"] = {
        "skill_bundle": {
            "enabled": True,
            "method": "skill_bundle_reflector",
            "config": {},
        }
    }
    return ScienceProjectConfigV2.model_validate(payload)


def _binding(
    registry_sha256: str,
    *,
    runtime_identity_sha256: str = "1" * 64,
    generation_sha256: str = "2" * 64,
    model: str = "gpt-5.5",
) -> ServiceRunBinding:
    image = MANAGED_RUNTIME_IMAGES["managed_science"]
    identity = InternalServiceIdentity(
        service_id="core-control",
        generation_digest=generation_sha256,
        registry_digest=registry_sha256,
        framework_lock_digest="4" * 64,
        credential="project-authority-test-credential-" + "x" * 40,
    )
    return ServiceRunBinding(
        execution_mode=ServiceExecutionMode.CODEX_SUBSCRIPTION_TRANSCRIPT,
        codex_model=model,
        runtime_image=image,
        runtime_image_immutable_reference=(
            MANAGED_RUNTIME_RELEASES["managed_science"].trusted_digest
        ),
        runtime_identity_digest=runtime_identity_sha256,
        generation_digest=generation_sha256,
        registry_digest=registry_sha256,
        framework_lock_digest=identity.framework_lock_digest,
        rollout_url="http://127.0.0.1:41001",
        evolution_backend_url="http://127.0.0.1:41002",
        gateway_url="http://127.0.0.1:41003",
        _identity=identity,
    )


class _Services:
    def __init__(self, binding: ServiceRunBinding | None) -> None:
        self.binding = binding
        self.require_ensure = False
        self.ensured = False
        self.ensure_calls: list[tuple[object, str | None, str | None]] = []

    def ensure(
        self,
        execution_mode: object,
        *,
        codex_model: str | None = None,
        runtime_image: str | None = None,
    ) -> object:
        self.ensure_calls.append((execution_mode, codex_model, runtime_image))
        self.ensured = True
        return object()

    def run_binding(self) -> ServiceRunBinding:
        if self.binding is None or (self.require_ensure and not self.ensured):
            raise RuntimeError("service group is unavailable")
        return self.binding


class _Runtime:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.clock = _Clock()
        self.registry = verified_builtin_registry(root / "registry")
        self.catalog = CoreControlStoreV2(root / "catalog")
        self.workspaces = WorkspaceStoreV2(root / "workspaces")
        self.owner = CoreScienceTaskOwnerV2(state_root=root, clock=self.clock)
        self.services = _Services(
            _binding(self.registry.snapshot.registry_digest)
        )
        self.authority = ProjectAuthorityV2(
            catalog_store=self.catalog,
            workspace_store=self.workspaces,
            task_owner=self.owner,
            executable_registry=self.registry,
            service_binding_provider=self.services,
            runtime_contract_sha256=_RUNTIME_CONTRACT_SHA256,
            clock=self.clock,
        )

    def create(self, config: ScienceProjectConfigV2 | None = None):
        record, replayed = self.catalog.create_project(
            ProjectCreateV2(
                display_name="Genesis project",
                config=config or _config(),
            ),
            idempotency_key="create-project",
            now=self.clock(),
        )
        assert replayed is False
        return record

    def close(self) -> None:
        self.owner.close()
        self.workspaces.close()
        self.catalog.close()


def test_genesis_is_content_addressed_verified_and_restart_stable(
    tmp_path: Path,
) -> None:
    runtime = _Runtime(tmp_path)
    record = runtime.create()
    authority = runtime.authority.ensure_project(record)
    assert authority is not None
    assert runtime.authority.readiness(record).ready is True
    assert authority.project_id == record.project_id
    assert authority.project_config_sha256 == project_config_sha256_for(record.config)
    assert authority.normalized_evolution_intent_sha256 == (
        normalized_evolution_intent_sha256_for(record.config)
    )
    head = authority.active_project_head
    assert head.generation == 0
    assert head.predecessor_project_head_id is None
    assert head.workspace_snapshot == authority.workspace_snapshot
    assert head.evolution_revision.artifact_count == 0
    assert head.runtime_context_snapshot.evolution_revision_id == (
        head.evolution_revision.evolution_revision_id
    )
    assert head.runtime_context_snapshot.registry_sha256 == (
        runtime.registry.snapshot.registry_digest
    )
    assert head.runtime_context_snapshot.runtime_contract_sha256 == (
        _RUNTIME_CONTRACT_SHA256
    )
    assert head.effective_execution_snapshot.producer_id == (
        "subscription-snapshot-issuer-v1"
    )
    assert head.effective_execution_snapshot.execution_mode == (
        "codex_subscription_transcript"
    )
    assert head.effective_execution_snapshot.capture_mode == "transcript"
    assert head.effective_execution_snapshot.token_level_metrics_available is False
    assert head.effective_execution_snapshot.effective_execution_snapshot_id == (
        f"exec-{head.effective_execution_snapshot.snapshot_sha256}"
    )
    assert head.project_head_id == f"project-head-{head.manifest_sha256}"
    assert runtime.owner.project_admission_authority(record.project_id) == authority
    snapshot = runtime.workspaces.get_snapshot(
        authority.workspace_snapshot.workspace_snapshot_id
    )
    assert snapshot == authority.workspace_snapshot

    runtime.owner.close()
    runtime.workspaces.close()
    runtime.catalog.close()
    catalog = CoreControlStoreV2(tmp_path / "catalog")
    workspaces = WorkspaceStoreV2(tmp_path / "workspaces")
    owner = CoreScienceTaskOwnerV2(state_root=tmp_path, clock=runtime.clock)
    recovered = ProjectAuthorityV2(
        catalog_store=catalog,
        workspace_store=workspaces,
        task_owner=owner,
        executable_registry=runtime.registry,
        service_binding_provider=runtime.services,
        runtime_contract_sha256=_RUNTIME_CONTRACT_SHA256,
        clock=runtime.clock,
    )
    try:
        recovered_record = catalog.get_project(record.project_id)
        assert recovered.ensure_project(recovered_record) == authority
        assert recovered.readiness(recovered_record).ready is True
    finally:
        owner.close()
        workspaces.close()
        catalog.close()


def test_genesis_ensures_the_managed_subscription_service_group_before_binding(
    tmp_path: Path,
) -> None:
    runtime = _Runtime(tmp_path)
    runtime.services.require_ensure = True
    record = runtime.create()
    try:
        authority = runtime.authority.ensure_project(record)

        assert authority is not None
        assert runtime.services.ensure_calls == [
            (
                ServiceExecutionMode.CODEX_SUBSCRIPTION_TRANSCRIPT,
                "gpt-5.5",
                MANAGED_RUNTIME_IMAGES["managed_science"],
            )
        ]
    finally:
        runtime.close()


def test_unavailable_or_drifted_runtime_is_not_ready_without_partial_head(
    tmp_path: Path,
) -> None:
    runtime = _Runtime(tmp_path)
    record = runtime.create()
    runtime.services.binding = None
    assert runtime.authority.ensure_project(record) is None
    readiness = runtime.authority.readiness(record)
    assert readiness.ready is False
    assert [check.check_id for check in readiness.checks] == [
        "managed-subscription-runtime"
    ]
    assert readiness.checks[0].status == "unavailable"
    with pytest.raises(Exception, match="not found"):
        runtime.owner.project_admission_authority(record.project_id)

    runtime.services.binding = _binding(runtime.registry.snapshot.registry_digest)
    authority = runtime.authority.ensure_project(record)
    assert authority is not None
    runtime.services.binding = _binding(
        runtime.registry.snapshot.registry_digest,
        runtime_identity_sha256="9" * 64,
        generation_sha256="8" * 64,
    )
    readiness = runtime.authority.readiness(record)
    assert readiness.ready is False
    assert readiness.checks[-1].check_id == "effective-execution-snapshot"
    assert readiness.checks[-1].status == "failed"
    assert runtime.owner.active_project_head(record.project_id) == (
        authority.active_project_head
    )
    runtime.close()


def test_invalid_evolution_selection_never_prepares_genesis(tmp_path: Path) -> None:
    runtime = _Runtime(tmp_path)
    payload = _config().model_dump(mode="json")
    payload["evolution"]["targets"] = {
        "unknown_target": {
            "enabled": True,
            "method": "unknown_method",
            "config": {},
        }
    }
    config = ScienceProjectConfigV2.model_validate(payload)
    record = runtime.create(config)
    with pytest.raises(ProjectAuthorityInvalidV2) as rejected:
        runtime.authority.ensure_project(record)
    assert rejected.value.reason_code == "unknown_target"
    assert runtime.catalog.get_project(record.project_id).config == config
    with pytest.raises(Exception, match="not found"):
        runtime.owner.project_admission_authority(record.project_id)
    runtime.close()


def test_publication_crash_recovers_exact_prepared_genesis(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _Runtime(tmp_path)
    record = runtime.create()

    def crash(*_args: object) -> None:
        raise SystemExit("simulated authority publication crash")

    monkeypatch.setattr(
        authority_module,
        "_after_science_authority_publish_before_catalog_commit",
        crash,
    )
    with pytest.raises(SystemExit):
        runtime.authority.ensure_project(record)
    published = runtime.owner.project_admission_authority(record.project_id)
    runtime.owner.close()
    runtime.workspaces.close()
    runtime.catalog.close()
    monkeypatch.setattr(
        authority_module,
        "_after_science_authority_publish_before_catalog_commit",
        lambda *_args: None,
    )

    catalog = CoreControlStoreV2(tmp_path / "catalog")
    workspaces = WorkspaceStoreV2(tmp_path / "workspaces")
    owner = CoreScienceTaskOwnerV2(state_root=tmp_path, clock=runtime.clock)
    recovered = ProjectAuthorityV2(
        catalog_store=catalog,
        workspace_store=workspaces,
        task_owner=owner,
        executable_registry=runtime.registry,
        service_binding_provider=runtime.services,
        runtime_contract_sha256=_RUNTIME_CONTRACT_SHA256,
        clock=runtime.clock,
    )
    try:
        recovered_record = catalog.get_project(record.project_id)
        assert recovered.ensure_project(recovered_record) == published
        assert owner.project_admission_authority(record.project_id) == published
    finally:
        owner.close()
        workspaces.close()
        catalog.close()


def test_validation_uses_exact_head_config_registry_and_compiler(tmp_path: Path) -> None:
    runtime = _Runtime(tmp_path)
    record = runtime.create()
    authority = runtime.authority.ensure_project(record)
    assert authority is not None
    response = runtime.authority.validate_project(
        record,
        ProjectValidationRequestV2(
            expected_project_head_id=authority.active_project_head.project_head_id,
            expected_project_head_manifest_sha256=(
                authority.active_project_head.manifest_sha256
            ),
            expected_project_config_sha256=record.project_config_sha256,
            expected_registry_sha256=runtime.registry.snapshot.registry_digest,
        ),
        now=runtime.clock(),
    )
    assert response.valid is True
    assert response.registry_sha256 == runtime.registry.snapshot.registry_digest
    assert [check.check_id for check in response.checks] == [
        "verified-evolution-configuration",
        "managed-subscription-runtime",
        "effective-execution-snapshot",
        "workspace-snapshot",
    ]
    assert canonical_digest(record.config.evolution) == (
        authority.normalized_evolution_intent_sha256
    )
    runtime.close()


def _provider(runtime: _Runtime) -> CoreControlProviderV2:
    return CoreControlProviderV2(
        runtime.catalog,
        task_owner=runtime.owner,
        executable_registry=runtime.registry,
        project_authority=runtime.authority,
        bearer_token="project-authority-bearer",
        release_version="0.1.9",
        source_commit="1" * 40,
        build_channel="test",
        runtime_contract_sha256=_RUNTIME_CONTRACT_SHA256,
        clock=runtime.clock,
    )


def test_provider_creates_ready_genesis_and_admits_only_server_derived_pins(
    tmp_path: Path,
) -> None:
    runtime = _Runtime(tmp_path)
    provider = _provider(runtime)
    client = TestClient(create_core_control_v2_contract_app(provider))
    headers = {
        "Authorization": "Bearer project-authority-bearer",
        "Idempotency-Key": "create-live-project",
    }
    created = client.post(
        "/v2/projects",
        headers=headers,
        json={
            "schema_version": "2",
            "display_name": "Live project",
            "config": _config().model_dump(mode="json"),
        },
    )
    assert created.status_code == 201
    project = created.json()
    assert project["state"] == "ready"
    assert project["active_project_head"]["generation"] == 0
    validation_request = {
        "schema_version": "2",
        "expected_project_head_id": project["active_project_head"][
            "project_head_id"
        ],
        "expected_project_head_manifest_sha256": project["active_project_head"][
            "manifest_sha256"
        ],
        "expected_project_config_sha256": project["project_config_sha256"],
        "expected_registry_sha256": runtime.registry.snapshot.registry_digest,
    }
    validation_headers = {
        "Authorization": "Bearer project-authority-bearer",
        "Idempotency-Key": "validate-live-project",
    }
    validated = client.post(
        f"/v2/projects/{project['project_id']}/validate",
        headers=validation_headers,
        json=validation_request,
    )
    assert validated.status_code == 200
    assert validated.json()["valid"] is True
    replayed_validation = client.post(
        f"/v2/projects/{project['project_id']}/validate",
        headers=validation_headers,
        json=validation_request,
    )
    assert replayed_validation.status_code == 200
    assert replayed_validation.json() == validated.json()
    reused_validation = client.post(
        f"/v2/projects/{project['project_id']}/validate",
        headers=validation_headers,
        json={**validation_request, "expected_registry_sha256": "f" * 64},
    )
    assert reused_validation.status_code == 409
    assert reused_validation.json()["code"] == "project_idempotency_key_reused"
    submit = client.post(
        "/v2/tasks",
        headers={
            "Authorization": "Bearer project-authority-bearer",
            "Idempotency-Key": "submit-live-task",
        },
        json={
            "schema_version": "2",
            "project_id": project["project_id"],
            "expected_project_admission_etag": project["admission_etag"],
            "expected_project_head_id": project["active_project_head"][
                "project_head_id"
            ],
            "expected_project_head_manifest_sha256": project[
                "active_project_head"
            ]["manifest_sha256"],
            "expected_project_config_sha256": project["project_config_sha256"],
        },
    )
    assert submit.status_code == 202
    admission = submit.json()["admission"]
    assert admission["workspace_snapshot"] == project["active_project_head"][
        "workspace_snapshot"
    ]
    assert admission["project_config_sha256"] == project[
        "project_config_sha256"
    ]
    assert admission["registry_sha256"] == runtime.registry.snapshot.registry_digest
    client.close()
    provider.close()


def test_provider_native_workspace_upload_publishes_genesis_without_host_paths(
    tmp_path: Path,
) -> None:
    runtime = _Runtime(tmp_path)
    provider = _provider(runtime)
    client = TestClient(create_core_control_v2_contract_app(provider))
    config_payload = _config().model_dump(mode="json")
    config_payload["workspace"] = {
        "kind": "native_folder_snapshot",
        "display_name": "Uploaded folder",
    }
    config = ScienceProjectConfigV2.model_validate(config_payload)
    auth = "Bearer project-authority-bearer"
    created = client.post(
        "/v2/projects",
        headers={"Authorization": auth, "Idempotency-Key": "create-native"},
        json={
            "schema_version": "2",
            "display_name": "Native project",
            "config": config.model_dump(mode="json"),
        },
    )
    assert created.status_code == 201
    project = created.json()
    assert project["state"] == "not_ready"
    assert project["active_project_head"] is None

    archive = b"\0" * 1024
    upload = client.post(
        f"/v2/projects/{project['project_id']}/workspace-uploads",
        headers={
            "Authorization": auth,
            "If-Match": project["etag"],
            "Idempotency-Key": "create-upload",
        },
        json={
            "schema_version": "2",
            "expected_project_head_id": None,
            "expected_project_head_manifest_sha256": None,
            "expected_project_config_sha256": project["project_config_sha256"],
            "archive": {
                "format": "openevo_deterministic_tar_v1",
                "media_type": "application/vnd.openevo.workspace-tar",
                "content_sha256": hashlib.sha256(archive).hexdigest(),
                "byte_size": len(archive),
                "entry_count": 0,
                "extracted_byte_size": 0,
            },
            "chunk_byte_size": 1024,
            "chunk_count": 1,
        },
    )
    assert upload.status_code == 201
    session = upload.json()
    chunk = client.put(
        f"/v2/projects/{project['project_id']}/workspace-uploads/"
        f"{session['upload_id']}/chunks/0",
        headers={
            "Authorization": auth,
            "If-Match": session["etag"],
            "Idempotency-Key": "chunk-0",
            "Content-Type": "application/octet-stream",
            "X-OpenEvo-Chunk-SHA256": hashlib.sha256(archive).hexdigest(),
            "X-OpenEvo-Chunk-Byte-Size": str(len(archive)),
        },
        content=archive,
    )
    assert chunk.status_code == 200
    session = chunk.json()
    finalized = client.post(
        f"/v2/projects/{project['project_id']}/workspace-uploads/"
        f"{session['upload_id']}/finalize",
        headers={
            "Authorization": auth,
            "If-Match": session["etag"],
            "Idempotency-Key": "finalize-upload",
        },
        json={
            "schema_version": "2",
            "expected_content_sha256": hashlib.sha256(archive).hexdigest(),
        },
    )
    assert finalized.status_code == 201
    assert finalized.json()["state"] == "finalized"
    encoded = finalized.content.decode("utf-8")
    assert str(tmp_path) not in encoded
    fetched = client.get(
        f"/v2/projects/{project['project_id']}",
        headers={"Authorization": auth},
    )
    assert fetched.status_code == 200
    assert fetched.json()["state"] == "ready"
    assert fetched.json()["active_project_head"]["workspace_snapshot"] == (
        finalized.json()["workspace_snapshot"]
    )
    replayed_finalize = client.post(
        f"/v2/projects/{project['project_id']}/workspace-uploads/"
        f"{session['upload_id']}/finalize",
        headers={
            "Authorization": auth,
            "If-Match": session["etag"],
            "Idempotency-Key": "finalize-upload",
        },
        json={
            "schema_version": "2",
            "expected_content_sha256": hashlib.sha256(archive).hexdigest(),
        },
    )
    assert replayed_finalize.status_code == 201
    assert replayed_finalize.json() == finalized.json()
    client.close()
    provider.close()


def test_provider_project_update_is_etag_bound_idempotent_and_head_safe(
    tmp_path: Path,
) -> None:
    runtime = _Runtime(tmp_path)
    provider = _provider(runtime)
    client = TestClient(create_core_control_v2_contract_app(provider))
    auth = "Bearer project-authority-bearer"
    created = client.post(
        "/v2/projects",
        headers={"Authorization": auth, "Idempotency-Key": "create-update"},
        json={
            "schema_version": "2",
            "display_name": "Before update",
            "config": _config().model_dump(mode="json"),
        },
    ).json()
    update_request = {
        "schema_version": "2",
        "expected_project_head_id": created["active_project_head"][
            "project_head_id"
        ],
        "expected_project_head_manifest_sha256": created["active_project_head"][
            "manifest_sha256"
        ],
        "expected_project_config_sha256": created["project_config_sha256"],
        "display_name": "After update",
        "config": created["config"],
    }
    headers = {
        "Authorization": auth,
        "If-Match": created["etag"],
        "Idempotency-Key": "update-display",
    }
    updated = client.patch(
        f"/v2/projects/{created['project_id']}",
        headers=headers,
        json=update_request,
    )
    assert updated.status_code == 200
    assert updated.json()["display_name"] == "After update"
    assert updated.json()["etag"] != created["etag"]
    replay = client.patch(
        f"/v2/projects/{created['project_id']}",
        headers=headers,
        json=update_request,
    )
    assert replay.status_code == 200
    assert replay.json() == updated.json()

    stale = client.patch(
        f"/v2/projects/{created['project_id']}",
        headers={
            "Authorization": auth,
            "If-Match": created["etag"],
            "Idempotency-Key": "different-update",
        },
        json={**update_request, "display_name": "Stale mutation"},
    )
    assert stale.status_code == 412
    assert stale.json()["code"] == "project_authority_changed"

    changed_config = _config(token_limit=65536)
    rejected = client.patch(
        f"/v2/projects/{created['project_id']}",
        headers={
            "Authorization": auth,
            "If-Match": updated.json()["etag"],
            "Idempotency-Key": "change-pinned-config",
        },
        json={
            **update_request,
            "display_name": "Pinned config mutation",
            "config": changed_config.model_dump(mode="json"),
        },
    )
    assert rejected.status_code == 409
    assert rejected.json()["code"] == "project_update_requires_successor"

    authority = runtime.owner.project_admission_authority(created["project_id"])
    successor = authority.active_project_head.model_copy(
        update={
            "project_head_id": "project-head-replay-successor",
            "generation": 1,
            "predecessor_project_head_id": (
                authority.active_project_head.project_head_id
            ),
            "manifest_sha256": "b" * 64,
        }
    )
    successor_authority = authority.__class__(
        project_id=authority.project_id,
        active_project_head=successor,
        project_config_sha256=authority.project_config_sha256,
        workspace_snapshot=successor.workspace_snapshot,
        normalized_evolution_intent_sha256=(
            authority.normalized_evolution_intent_sha256
        ),
    )
    database = (
        tmp_path
        / "science-tasks-v2"
        / "science-tasks-v2.sqlite3"
    )
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        task_store_module._store_v2_project_head(connection, successor)
        connection.execute(
            "UPDATE project_admission_authorities SET authority_json = ?, "
            "resource_version = resource_version + 1 WHERE project_id = ?",
            (
                task_store_module._v2_authority_bytes(successor_authority),
                created["project_id"],
            ),
        )
        connection.commit()
    replay_after_successor = client.patch(
        f"/v2/projects/{created['project_id']}",
        headers=headers,
        json=update_request,
    )
    assert replay_after_successor.status_code == 200
    assert replay_after_successor.json()["active_project_head"] == (
        successor.model_dump(mode="json")
    )
    client.close()
    provider.close()


def test_display_update_rejects_a_concurrent_successor_head(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _Runtime(tmp_path)
    provider = _provider(runtime)
    client = TestClient(create_core_control_v2_contract_app(provider))
    auth = "Bearer project-authority-bearer"
    created = client.post(
        "/v2/projects",
        headers={"Authorization": auth, "Idempotency-Key": "create-display-race"},
        json={
            "schema_version": "2",
            "display_name": "Before successor race",
            "config": _config().model_dump(mode="json"),
        },
    ).json()
    genesis = runtime.owner.project_admission_authority(created["project_id"])
    successor = genesis.active_project_head.model_copy(
        update={
            "project_head_id": "project-head-display-race-successor",
            "generation": 1,
            "predecessor_project_head_id": (
                genesis.active_project_head.project_head_id
            ),
            "manifest_sha256": "e" * 64,
        }
    )
    successor_authority = genesis.__class__(
        project_id=genesis.project_id,
        active_project_head=successor,
        project_config_sha256=genesis.project_config_sha256,
        workspace_snapshot=successor.workspace_snapshot,
        normalized_evolution_intent_sha256=(
            genesis.normalized_evolution_intent_sha256
        ),
    )
    raced = False

    def publish_successor(*_args: object) -> None:
        nonlocal raced
        if raced:
            return
        raced = True
        database = (
            tmp_path
            / "science-tasks-v2"
            / "science-tasks-v2.sqlite3"
        )
        with sqlite3.connect(database) as connection:
            connection.row_factory = sqlite3.Row
            task_store_module._store_v2_project_head(connection, successor)
            connection.execute(
                "UPDATE project_admission_authorities SET authority_json = ?, "
                "resource_version = resource_version + 1 WHERE project_id = ?",
                (
                    task_store_module._v2_authority_bytes(successor_authority),
                    created["project_id"],
                ),
            )
            connection.commit()

    monkeypatch.setattr(
        provider_module,
        "_after_project_update_join_read",
        publish_successor,
    )
    response = client.patch(
        f"/v2/projects/{created['project_id']}",
        headers={
            "Authorization": auth,
            "If-Match": created["etag"],
            "Idempotency-Key": "display-race-update",
        },
        json={
            "schema_version": "2",
            "expected_project_head_id": created["active_project_head"][
                "project_head_id"
            ],
            "expected_project_head_manifest_sha256": created[
                "active_project_head"
            ]["manifest_sha256"],
            "expected_project_config_sha256": created["project_config_sha256"],
            "display_name": "Must not cross successor",
            "config": created["config"],
        },
    )
    assert raced is True
    assert response.status_code == 412
    assert response.json()["code"] == "project_authority_changed"
    assert runtime.catalog.get_project(created["project_id"]).display_name == (
        "Before successor race"
    )
    client.close()
    provider.close()


def test_provider_project_update_rebinds_next_task_evolution_intent_without_new_head(
    tmp_path: Path,
) -> None:
    runtime = _Runtime(tmp_path)
    provider = _provider(runtime)
    client = TestClient(create_core_control_v2_contract_app(provider))
    auth = "Bearer project-authority-bearer"
    created = client.post(
        "/v2/projects",
        headers={"Authorization": auth, "Idempotency-Key": "create-intent-update"},
        json={
            "schema_version": "2",
            "display_name": "Evolution draft",
            "config": _config().model_dump(mode="json"),
        },
    ).json()
    changed_config = _skill_bundle_config().model_dump(mode="json")
    update_request = {
        "schema_version": "2",
        "expected_project_head_id": created["active_project_head"][
            "project_head_id"
        ],
        "expected_project_head_manifest_sha256": created["active_project_head"][
            "manifest_sha256"
        ],
        "expected_project_config_sha256": created["project_config_sha256"],
        "display_name": "Evolution enabled",
        "config": changed_config,
    }
    headers = {
        "Authorization": auth,
        "If-Match": created["etag"],
        "Idempotency-Key": "update-next-task-intent",
    }
    updated_response = client.patch(
        f"/v2/projects/{created['project_id']}",
        headers=headers,
        json=update_request,
    )
    assert updated_response.status_code == 200, updated_response.text
    updated = updated_response.json()
    assert updated["config"] == changed_config
    assert updated["project_config_sha256"] != created["project_config_sha256"]
    assert updated["active_project_head"] == created["active_project_head"]
    assert updated["admission_etag"] != created["admission_etag"]
    assert updated["state"] == "ready"

    authority = runtime.owner.project_admission_authority(created["project_id"])
    assert authority.active_project_head == runtime.owner.active_project_head(
        created["project_id"]
    )
    assert authority.project_config_sha256 == updated["project_config_sha256"]
    assert authority.normalized_evolution_intent_sha256 == (
        normalized_evolution_intent_sha256_for(
            ScienceProjectConfigV2.model_validate(changed_config)
        )
    )

    replay = client.patch(
        f"/v2/projects/{created['project_id']}",
        headers=headers,
        json=update_request,
    )
    assert replay.status_code == 200
    assert replay.json() == updated

    submitted = client.post(
        "/v2/tasks",
        headers={
            "Authorization": auth,
            "Idempotency-Key": "submit-updated-intent",
        },
        json={
            "schema_version": "2",
            "project_id": updated["project_id"],
            "expected_project_admission_etag": updated["admission_etag"],
            "expected_project_head_id": updated["active_project_head"][
                "project_head_id"
            ],
            "expected_project_head_manifest_sha256": updated[
                "active_project_head"
            ]["manifest_sha256"],
            "expected_project_config_sha256": updated["project_config_sha256"],
        },
    )
    assert submitted.status_code == 202, submitted.text
    admission = submitted.json()["admission"]
    assert admission["project_config_sha256"] == updated["project_config_sha256"]
    assert admission["normalized_evolution_intent_sha256"] == (
        authority.normalized_evolution_intent_sha256
    )
    client.close()
    provider.close()


def test_provider_project_update_rejects_an_open_task_without_partial_config(
    tmp_path: Path,
) -> None:
    runtime = _Runtime(tmp_path)
    provider = _provider(runtime)
    client = TestClient(create_core_control_v2_contract_app(provider))
    auth = "Bearer project-authority-bearer"
    created = client.post(
        "/v2/projects",
        headers={"Authorization": auth, "Idempotency-Key": "create-open-task"},
        json={
            "schema_version": "2",
            "display_name": "Immutable task project",
            "config": _config().model_dump(mode="json"),
        },
    ).json()
    submitted = client.post(
        "/v2/tasks",
        headers={
            "Authorization": auth,
            "Idempotency-Key": "submit-before-config-update",
        },
        json={
            "schema_version": "2",
            "project_id": created["project_id"],
            "expected_project_admission_etag": created["admission_etag"],
            "expected_project_head_id": created["active_project_head"][
                "project_head_id"
            ],
            "expected_project_head_manifest_sha256": created[
                "active_project_head"
            ]["manifest_sha256"],
            "expected_project_config_sha256": created["project_config_sha256"],
        },
    )
    assert submitted.status_code == 202
    original_authority = runtime.owner.project_admission_authority(
        created["project_id"]
    )
    rejected = client.patch(
        f"/v2/projects/{created['project_id']}",
        headers={
            "Authorization": auth,
            "If-Match": created["etag"],
            "Idempotency-Key": "update-during-open-task",
        },
        json={
            "schema_version": "2",
            "expected_project_head_id": created["active_project_head"][
                "project_head_id"
            ],
            "expected_project_head_manifest_sha256": created[
                "active_project_head"
            ]["manifest_sha256"],
            "expected_project_config_sha256": created["project_config_sha256"],
            "display_name": "Must not persist",
            "config": _skill_bundle_config().model_dump(mode="json"),
        },
    )
    assert rejected.status_code == 409
    assert rejected.json()["code"] == "task_project_in_flight"
    persisted = runtime.catalog.get_project(created["project_id"])
    assert persisted.display_name == "Immutable task project"
    assert persisted.config == _config()
    assert (
        runtime.owner.project_admission_authority(created["project_id"])
        == original_authority
    )
    client.close()
    provider.close()


def test_project_intent_update_rejects_a_concurrent_catalog_resource_version(
    tmp_path: Path,
) -> None:
    runtime = _Runtime(tmp_path)
    record = runtime.create()
    authority = runtime.authority.ensure_project(record)
    assert authority is not None
    stale_etag = project_etag_payload(
        record,
        active_project_head=authority.active_project_head,
        admission_etag=authority.project_etag,
        state="ready",
    )
    display_update = ProjectUpdateV2(
        expected_project_head_id=authority.active_project_head.project_head_id,
        expected_project_head_manifest_sha256=(
            authority.active_project_head.manifest_sha256
        ),
        expected_project_config_sha256=record.project_config_sha256,
        display_name="Concurrent display update",
        config=record.config,
    )
    concurrent_record, replayed = runtime.catalog.update_project(
        record.project_id,
        display_update,
        if_match=stale_etag,
        current_etag=stale_etag,
        expected_resource_version=record.resource_version,
        idempotency_key="concurrent-display-update",
        now=runtime.clock(),
    )
    assert replayed is False

    intent_update = display_update.model_copy(
        update={
            "display_name": "Must not overwrite concurrent display",
            "config": _skill_bundle_config(),
        }
    )
    with pytest.raises(ProjectPreconditionFailedV2):
        runtime.authority.update_project_draft(
            record,
            intent_update,
            if_match=stale_etag,
            current_etag=stale_etag,
            idempotency_key="stale-intent-update",
            now=runtime.clock(),
        )
    persisted = runtime.catalog.get_project(record.project_id)
    assert persisted == concurrent_record
    assert runtime.owner.project_admission_authority(record.project_id) == authority
    runtime.close()


def test_project_intent_update_replays_before_validating_a_stale_snapshot(
    tmp_path: Path,
) -> None:
    runtime = _Runtime(tmp_path)
    record = runtime.create()
    authority = runtime.authority.ensure_project(record)
    assert authority is not None
    request = ProjectUpdateV2(
        expected_project_head_id=authority.active_project_head.project_head_id,
        expected_project_head_manifest_sha256=(
            authority.active_project_head.manifest_sha256
        ),
        expected_project_config_sha256=record.project_config_sha256,
        display_name="Exact retry",
        config=_skill_bundle_config(),
    )
    etag = project_etag_payload(
        record,
        active_project_head=authority.active_project_head,
        admission_etag=authority.project_etag,
        state="ready",
    )
    updated, replayed = runtime.authority.update_project_draft(
        record,
        request,
        if_match=etag,
        current_etag=etag,
        idempotency_key="concurrent-exact-retry",
        now=runtime.clock(),
    )
    assert replayed is False

    exact, replayed = runtime.authority.update_project_draft(
        record,
        request,
        if_match=etag,
        current_etag=etag,
        idempotency_key="concurrent-exact-retry",
        now=runtime.clock(),
    )
    assert replayed is True
    assert exact == updated

    with pytest.raises(ProjectPreconditionFailedV2):
        runtime.authority.update_project_draft(
            record,
            request,
            if_match=etag,
            current_etag=etag,
            idempotency_key="stale-different-writer",
            now=runtime.clock(),
        )
    runtime.close()


def test_project_intent_update_reports_an_existing_transition_as_in_flight(
    tmp_path: Path,
) -> None:
    runtime = _Runtime(tmp_path)
    provider = _provider(runtime)
    client = TestClient(create_core_control_v2_contract_app(provider))
    auth = "Bearer project-authority-bearer"
    created = client.post(
        "/v2/projects",
        headers={"Authorization": auth, "Idempotency-Key": "create-blocked-update"},
        json={
            "schema_version": "2",
            "display_name": "Transitioning project",
            "config": _config().model_dump(mode="json"),
        },
    ).json()
    authority = runtime.owner.project_admission_authority(created["project_id"])
    blocked = authority.__class__(
        project_id=authority.project_id,
        active_project_head=authority.active_project_head,
        project_config_sha256=authority.project_config_sha256,
        workspace_snapshot=authority.workspace_snapshot,
        normalized_evolution_intent_sha256=(
            authority.normalized_evolution_intent_sha256
        ),
        blockers=(ScienceProjectReadinessBlockerV2.SETTINGS_TRANSITION,),
    )
    runtime.owner.publish_project_admission_authority(
        blocked,
        expected_project_head_id=(
            authority.active_project_head.project_head_id
        ),
    )
    response = client.patch(
        f"/v2/projects/{created['project_id']}",
        headers={
            "Authorization": auth,
            "If-Match": created["etag"],
            "Idempotency-Key": "update-while-transitioning",
        },
        json={
            "schema_version": "2",
            "expected_project_head_id": created["active_project_head"][
                "project_head_id"
            ],
            "expected_project_head_manifest_sha256": created[
                "active_project_head"
            ]["manifest_sha256"],
            "expected_project_config_sha256": created["project_config_sha256"],
            "display_name": "Must remain blocked",
            "config": _skill_bundle_config().model_dump(mode="json"),
        },
    )
    assert response.status_code == 409
    assert response.json()["code"] == "task_project_in_flight"
    assert runtime.catalog.get_project(created["project_id"]).display_name == (
        "Transitioning project"
    )
    client.close()
    provider.close()


def test_project_intent_fence_blocks_a_separate_owner_task_admission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _Runtime(tmp_path)
    record = runtime.create()
    authority = runtime.authority.ensure_project(record)
    assert authority is not None
    competing_owner = CoreScienceTaskOwnerV2(
        state_root=tmp_path,
        clock=runtime.clock,
    )
    admission_result: list[str] = []
    request = TaskSubmitRequestV2(
        project_id=record.project_id,
        expected_project_admission_etag=authority.project_etag,
        expected_project_head_id=authority.active_project_head.project_head_id,
        expected_project_head_manifest_sha256=(
            authority.active_project_head.manifest_sha256
        ),
        expected_project_config_sha256=record.project_config_sha256,
    )

    def attempt_competing_admission(*_args: object) -> None:
        try:
            competing_owner.invoke(
                "submitCoreTaskV2",
                {
                    "request": request,
                    "idempotency_key": "competing-task-admission",
                },
            )
        except CoreTaskControlError as exc:
            admission_result.append(exc.code)
        else:
            admission_result.append("admitted")

    monkeypatch.setattr(
        authority_module,
        "_after_project_config_prepare_before_science_publish",
        attempt_competing_admission,
    )
    update = ProjectUpdateV2(
        expected_project_head_id=authority.active_project_head.project_head_id,
        expected_project_head_manifest_sha256=(
            authority.active_project_head.manifest_sha256
        ),
        expected_project_config_sha256=record.project_config_sha256,
        display_name="Safely rebound",
        config=_skill_bundle_config(),
    )
    try:
        updated, replayed = runtime.authority.update_project_draft(
            record,
            update,
            if_match=project_etag_payload(
                record,
                active_project_head=authority.active_project_head,
                admission_etag=authority.project_etag,
                state="ready",
            ),
            current_etag=project_etag_payload(
                record,
                active_project_head=authority.active_project_head,
                admission_etag=authority.project_etag,
                state="ready",
            ),
            idempotency_key="fenced-intent-update",
            now=runtime.clock(),
        )
        assert replayed is False
        assert updated.config == _skill_bundle_config()
        assert admission_result == ["project_not_ready"]
        assert competing_owner.ownership_counts() == (0, 0, 0)
    finally:
        competing_owner.close()
        runtime.close()


def test_project_intent_update_repairs_an_ordinary_cross_store_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _Runtime(tmp_path)
    record = runtime.create()
    authority = runtime.authority.ensure_project(record)
    assert authority is not None
    request = ProjectUpdateV2(
        expected_project_head_id=authority.active_project_head.project_head_id,
        expected_project_head_manifest_sha256=(
            authority.active_project_head.manifest_sha256
        ),
        expected_project_config_sha256=record.project_config_sha256,
        display_name="Recovered ordinary failure",
        config=_skill_bundle_config(),
    )

    def fail_once(*_args: object) -> None:
        raise RuntimeError("simulated recoverable publication failure")

    monkeypatch.setattr(
        authority_module,
        "_after_project_config_prepare_before_science_publish",
        fail_once,
    )
    etag = project_etag_payload(
        record,
        active_project_head=authority.active_project_head,
        admission_etag=authority.project_etag,
        state="ready",
    )
    with pytest.raises(RuntimeError, match="simulated recoverable"):
        runtime.authority.update_project_draft(
            record,
            request,
            if_match=etag,
            current_etag=etag,
            idempotency_key="ordinary-failure-update",
            now=runtime.clock(),
        )

    persisted = runtime.catalog.get_project(record.project_id)
    published = runtime.owner.project_admission_authority(record.project_id)
    assert persisted.config == _skill_bundle_config()
    assert published.project_config_sha256 == persisted.project_config_sha256
    assert published.normalized_evolution_intent_sha256 == (
        normalized_evolution_intent_sha256_for(persisted.config)
    )
    assert published.blockers == ()
    submitted = runtime.owner.invoke(
        "submitCoreTaskV2",
        {
            "request": TaskSubmitRequestV2(
                project_id=record.project_id,
                expected_project_admission_etag=published.project_etag,
                expected_project_head_id=(
                    published.active_project_head.project_head_id
                ),
                expected_project_head_manifest_sha256=(
                    published.active_project_head.manifest_sha256
                ),
                expected_project_config_sha256=(
                    published.project_config_sha256
                ),
            ),
            "idempotency_key": "submit-after-ordinary-recovery",
        },
    )
    assert submitted.project_id == record.project_id
    runtime.close()


def test_project_intent_update_preserves_a_successor_project_head(
    tmp_path: Path,
) -> None:
    runtime = _Runtime(tmp_path)
    record = runtime.create()
    genesis = runtime.authority.ensure_project(record)
    assert genesis is not None
    successor = genesis.active_project_head.model_copy(
        update={
            "project_head_id": "project-head-successor-test",
            "generation": 1,
            "predecessor_project_head_id": (
                genesis.active_project_head.project_head_id
            ),
            "manifest_sha256": "f" * 64,
        }
    )
    successor_authority = genesis.__class__(
        project_id=genesis.project_id,
        active_project_head=successor,
        project_config_sha256=genesis.project_config_sha256,
        workspace_snapshot=successor.workspace_snapshot,
        normalized_evolution_intent_sha256=(
            genesis.normalized_evolution_intent_sha256
        ),
    )
    database = (
        tmp_path
        / "science-tasks-v2"
        / "science-tasks-v2.sqlite3"
    )
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        task_store_module._store_v2_project_head(connection, successor)
        connection.execute(
            "UPDATE project_admission_authorities SET authority_json = ?, "
            "resource_version = resource_version + 1 WHERE project_id = ?",
            (
                task_store_module._v2_authority_bytes(successor_authority),
                record.project_id,
            ),
        )
        connection.commit()

    request = ProjectUpdateV2(
        expected_project_head_id=successor.project_head_id,
        expected_project_head_manifest_sha256=successor.manifest_sha256,
        expected_project_config_sha256=record.project_config_sha256,
        display_name="Successor intent update",
        config=_skill_bundle_config(),
    )
    etag = project_etag_payload(
        record,
        active_project_head=successor,
        admission_etag=successor_authority.project_etag,
        state="ready",
    )
    updated, replayed = runtime.authority.update_project_draft(
        record,
        request,
        if_match=etag,
        current_etag=etag,
        idempotency_key="successor-intent-update",
        now=runtime.clock(),
    )
    assert replayed is False
    published = runtime.owner.project_admission_authority(record.project_id)
    assert updated.config == _skill_bundle_config()
    assert published.active_project_head == successor
    assert published.project_config_sha256 == updated.project_config_sha256
    assert published.blockers == ()
    runtime.close()


@pytest.mark.parametrize(
    "hook_name",
    [
        "_after_project_config_rebind_fence_before_catalog_prepare",
        "_after_project_config_catalog_publish_before_rebind_release",
    ],
)
def test_successor_project_intent_rebind_recovers_without_losing_the_active_head(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    hook_name: str,
) -> None:
    runtime = _Runtime(tmp_path)
    record = runtime.create()
    genesis = runtime.authority.ensure_project(record)
    assert genesis is not None
    successor = genesis.active_project_head.model_copy(
        update={
            "project_head_id": "project-head-recovery-successor",
            "generation": 1,
            "predecessor_project_head_id": (
                genesis.active_project_head.project_head_id
            ),
            "manifest_sha256": "c" * 64,
        }
    )
    successor_authority = genesis.__class__(
        project_id=genesis.project_id,
        active_project_head=successor,
        project_config_sha256=genesis.project_config_sha256,
        workspace_snapshot=successor.workspace_snapshot,
        normalized_evolution_intent_sha256=(
            genesis.normalized_evolution_intent_sha256
        ),
    )
    database = (
        tmp_path
        / "science-tasks-v2"
        / "science-tasks-v2.sqlite3"
    )
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        task_store_module._store_v2_project_head(connection, successor)
        connection.execute(
            "UPDATE project_admission_authorities SET authority_json = ?, "
            "resource_version = resource_version + 1 WHERE project_id = ?",
            (
                task_store_module._v2_authority_bytes(successor_authority),
                record.project_id,
            ),
        )
        connection.commit()
    request = ProjectUpdateV2(
        expected_project_head_id=successor.project_head_id,
        expected_project_head_manifest_sha256=successor.manifest_sha256,
        expected_project_config_sha256=record.project_config_sha256,
        display_name="Successor crash recovery",
        config=_skill_bundle_config(),
    )
    etag = project_etag_payload(
        record,
        active_project_head=successor,
        admission_etag=successor_authority.project_etag,
        state="ready",
    )

    def crash(*_args: object) -> None:
        raise SystemExit("simulated successor config-rebind crash")

    monkeypatch.setattr(authority_module, hook_name, crash)
    with pytest.raises(SystemExit, match="successor config-rebind"):
        runtime.authority.update_project_draft(
            record,
            request,
            if_match=etag,
            current_etag=etag,
            idempotency_key=f"successor-crash-{hook_name}",
            now=runtime.clock(),
        )
    monkeypatch.setattr(authority_module, hook_name, lambda *_args: None)

    catalog = CoreControlStoreV2(tmp_path / "catalog")
    workspaces = WorkspaceStoreV2(tmp_path / "workspaces")
    recovered = ProjectAuthorityV2(
        catalog_store=catalog,
        workspace_store=workspaces,
        task_owner=runtime.owner,
        executable_registry=runtime.registry,
        service_binding_provider=runtime.services,
        runtime_contract_sha256=_RUNTIME_CONTRACT_SHA256,
        clock=runtime.clock,
    )
    try:
        actual = runtime.owner.project_admission_authority(record.project_id)
        assert actual.active_project_head == successor
        assert actual.blockers == ()
        if hook_name == (
            "_after_project_config_catalog_publish_before_rebind_release"
        ):
            assert actual.project_config_sha256 == (
                project_config_sha256_for(_skill_bundle_config())
            )
        else:
            assert actual.project_config_sha256 == record.project_config_sha256
    finally:
        recovered.close()
        catalog.close()
        runtime.close()


@pytest.mark.parametrize(
    ("hook_name", "catalog_updated_before_restart", "ledger_updated_before_restart"),
    [
        (
            "_after_project_config_rebind_fence_before_catalog_prepare",
            False,
            False,
        ),
        ("_after_project_config_prepare_before_science_publish", True, False),
        ("_after_science_authority_publish_before_catalog_commit", True, True),
        (
            "_after_project_config_catalog_publish_before_rebind_release",
            True,
            True,
        ),
    ],
)
def test_project_intent_update_recovers_each_cross_store_crash_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    hook_name: str,
    catalog_updated_before_restart: bool,
    ledger_updated_before_restart: bool,
) -> None:
    runtime = _Runtime(tmp_path)
    provider = _provider(runtime)
    client = TestClient(create_core_control_v2_contract_app(provider))
    created = client.post(
        "/v2/projects",
        headers={
            "Authorization": "Bearer project-authority-bearer",
            "Idempotency-Key": "create-crash-update",
        },
        json={
            "schema_version": "2",
            "display_name": "Before crash update",
            "config": _config().model_dump(mode="json"),
        },
    ).json()
    client.close()
    original_authority = runtime.owner.project_admission_authority(
        created["project_id"]
    )
    request = ProjectUpdateV2(
        expected_project_head_id=created["active_project_head"]["project_head_id"],
        expected_project_head_manifest_sha256=created["active_project_head"][
            "manifest_sha256"
        ],
        expected_project_config_sha256=created["project_config_sha256"],
        display_name="Recovered evolution intent",
        config=_skill_bundle_config(),
    )

    def crash(*_args: object) -> None:
        raise SystemExit("simulated project intent publication crash")

    monkeypatch.setattr(authority_module, hook_name, crash)
    with pytest.raises(SystemExit, match="simulated project intent"):
        runtime.authority.update_project_draft(
            runtime.catalog.get_project(created["project_id"]),
            request,
            if_match=created["etag"],
            current_etag=created["etag"],
            idempotency_key="crash-update",
            now=runtime.clock(),
        )
    prepared_project = runtime.catalog.get_project(created["project_id"])
    expected_config = (
        _skill_bundle_config()
        if catalog_updated_before_restart
        else _config()
    )
    assert prepared_project.config == expected_config
    before_restart = runtime.owner.project_admission_authority(
        created["project_id"]
    )
    if ledger_updated_before_restart:
        assert before_restart.project_config_sha256 == (
            prepared_project.project_config_sha256
        )
        assert before_restart.blockers == (
            ScienceProjectReadinessBlockerV2.PROJECT_CONFIG_REBIND,
        )
    else:
        assert before_restart == original_authority.__class__(
            project_id=original_authority.project_id,
            active_project_head=original_authority.active_project_head,
            project_config_sha256=original_authority.project_config_sha256,
            workspace_snapshot=original_authority.workspace_snapshot,
            normalized_evolution_intent_sha256=(
                original_authority.normalized_evolution_intent_sha256
            ),
            blockers=(
                ScienceProjectReadinessBlockerV2.PROJECT_CONFIG_REBIND,
            ),
        )
    provider.close()
    monkeypatch.setattr(authority_module, hook_name, lambda *_args: None)

    catalog = CoreControlStoreV2(tmp_path / "catalog")
    workspaces = WorkspaceStoreV2(tmp_path / "workspaces")
    owner = CoreScienceTaskOwnerV2(state_root=tmp_path, clock=runtime.clock)
    recovered = ProjectAuthorityV2(
        catalog_store=catalog,
        workspace_store=workspaces,
        task_owner=owner,
        executable_registry=runtime.registry,
        service_binding_provider=runtime.services,
        runtime_contract_sha256=_RUNTIME_CONTRACT_SHA256,
        clock=runtime.clock,
    )
    try:
        recovered_project = catalog.get_project(created["project_id"])
        recovered_authority = recovered.ensure_project(recovered_project)
        assert recovered_authority is not None
        assert recovered_project.config == expected_config
        assert (
            recovered_authority.active_project_head
            == original_authority.active_project_head
        )
        assert recovered_authority.project_config_sha256 == (
            recovered_project.project_config_sha256
        )
        assert recovered_authority.normalized_evolution_intent_sha256 == (
            normalized_evolution_intent_sha256_for(expected_config)
        )
        assert owner.project_admission_authority(created["project_id"]) == (
            recovered_authority
        )
    finally:
        owner.close()
        recovered.close()
        catalog.close()


def test_provider_runtime_drift_blocks_task_before_admission(tmp_path: Path) -> None:
    runtime = _Runtime(tmp_path)
    provider = _provider(runtime)
    client = TestClient(create_core_control_v2_contract_app(provider))
    auth = "Bearer project-authority-bearer"
    created = client.post(
        "/v2/projects",
        headers={"Authorization": auth, "Idempotency-Key": "create-drift"},
        json={
            "schema_version": "2",
            "display_name": "Drift project",
            "config": _config().model_dump(mode="json"),
        },
    ).json()
    runtime.services.binding = _binding(
        runtime.registry.snapshot.registry_digest,
        runtime_identity_sha256="9" * 64,
        generation_sha256="8" * 64,
    )
    fetched = client.get(
        f"/v2/projects/{created['project_id']}",
        headers={"Authorization": auth},
    )
    assert fetched.status_code == 200
    assert fetched.json()["state"] == "not_ready"
    submit = client.post(
        "/v2/tasks",
        headers={"Authorization": auth, "Idempotency-Key": "blocked-task"},
        json={
            "schema_version": "2",
            "project_id": created["project_id"],
            "expected_project_admission_etag": created["admission_etag"],
            "expected_project_head_id": created["active_project_head"][
                "project_head_id"
            ],
            "expected_project_head_manifest_sha256": created[
                "active_project_head"
            ]["manifest_sha256"],
            "expected_project_config_sha256": created["project_config_sha256"],
        },
    )
    assert submit.status_code == 409
    assert submit.json()["code"] == "project_not_ready"
    tasks = client.get(
        f"/v2/tasks?project_id={created['project_id']}",
        headers={"Authorization": auth},
    )
    assert tasks.status_code == 200
    assert tasks.json()["items"] == []
    client.close()
    provider.close()


def test_recovery_rejects_rehashed_non_content_addressed_genesis(
    tmp_path: Path,
) -> None:
    runtime = _Runtime(tmp_path)
    record = runtime.create()
    assert runtime.authority.ensure_project(record) is not None
    database = runtime.catalog.database
    runtime.close()
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT record_json FROM project_authority_records WHERE project_id = ?",
            (record.project_id,),
        ).fetchone()
        assert row is not None
        payload = json.loads(bytes(row[0]))
        payload["active_project_head"]["manifest_sha256"] = "f" * 64
        encoded = json.dumps(
            payload,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        connection.execute(
            "UPDATE project_authority_records SET record_json = ?, "
            "record_sha256 = ? WHERE project_id = ?",
            (encoded, hashlib.sha256(encoded).hexdigest(), record.project_id),
        )
        connection.commit()

    catalog = CoreControlStoreV2(tmp_path / "catalog")
    workspaces = WorkspaceStoreV2(tmp_path / "workspaces")
    owner = CoreScienceTaskOwnerV2(state_root=tmp_path, clock=runtime.clock)
    with pytest.raises(Exception, match="content addressed"):
        ProjectAuthorityV2(
            catalog_store=catalog,
            workspace_store=workspaces,
            task_owner=owner,
            executable_registry=runtime.registry,
            service_binding_provider=runtime.services,
            runtime_contract_sha256=_RUNTIME_CONTRACT_SHA256,
            clock=runtime.clock,
        )
    owner.close()
    workspaces.close()
    catalog.close()
