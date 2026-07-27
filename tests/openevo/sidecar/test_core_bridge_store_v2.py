from __future__ import annotations

from dataclasses import replace
import os
from pathlib import Path
import sqlite3

import pytest

from desktop.sidecar.core_bridge_store_v2 import (
    CoreBridgeStoreConflictV2,
    CoreBridgeStoreDataV2Error,
    CoreBridgeStoreStateV2Error,
    DesktopCoreBridgeStoreV2,
    OWNER_LOCK_FILENAME,
)
from desktop.sidecar.core_bridge_v2 import (
    CoreBridgeMutationStateV2,
    CoreBridgeMutationV2,
    CoreProjectMappingV2,
    core_project_mapping_sha256_v2,
)
from openevo.backend.contracts.v2 import models as m
from tests.openevo.sidecar.test_core_client_v2 import (
    CORE_EVENTS_SCHEMA_SHA256,
    CORE_OPENAPI_SHA256,
    _head,
    _project,
    _version,
)


def _mapping(
    *,
    mapping_generation: int = 1,
    predecessor_mapping_sha256: str | None = None,
    profile_connection_generation: int = 3,
    project: m.ProjectV2 | None = None,
    last_event_id: str | None = None,
    last_event_sequence: int | None = None,
    last_event_payload_sha256: str | None = None,
) -> CoreProjectMappingV2:
    remote = project or _project()
    version = m.VersionResponseV2.model_validate(_version(), strict=True)
    return CoreProjectMappingV2(
        desktop_project_id="desktop-project-1",
        profile_id="profile-1",
        profile_connection_generation=profile_connection_generation,
        core_project_id=remote.project_id,
        project_config_sha256=remote.project_config_sha256,
        project_etag=remote.etag,
        project_admission_etag=remote.admission_etag,
        active_project_head=remote.active_project_head,
        project_head_successor_proof=(
            () if remote.active_project_head is None else (remote.active_project_head,)
        )
        if mapping_generation == 1
        else (() if remote.active_project_head is None else (remote.active_project_head,)),
        daemon_release_version=version.release_version,
        daemon_build_id=version.build_id,
        daemon_source_commit=version.source_commit,
        daemon_openapi_sha256=CORE_OPENAPI_SHA256,
        daemon_event_schema_sha256=CORE_EVENTS_SCHEMA_SHA256,
        daemon_registry_sha256=version.registry_sha256,
        daemon_runtime_contract_sha256=version.runtime_contract_sha256,
        core_project=remote,
        core_version=version,
        mapping_generation=mapping_generation,
        predecessor_mapping_sha256=predecessor_mapping_sha256,
        last_core_event_id=last_event_id,
        last_core_event_sequence=last_event_sequence,
        last_core_event_payload_sha256=last_event_payload_sha256,
    )


def _successor(previous: CoreProjectMappingV2) -> CoreProjectMappingV2:
    old = previous.active_project_head
    assert old is not None
    next_head = _head()
    next_head = next_head.model_copy(
        update={
            "project_head_id": "head-1",
            "generation": 1,
            "predecessor_project_head_id": old.project_head_id,
            "manifest_sha256": "9" * 64,
        }
    )
    project = _project().model_copy(
        update={
            "active_project_head": next_head,
            "etag": '"' + "6" * 64 + '"',
            "updated_at": "2026-07-23T06:00:01Z",
        }
    )
    return _mapping(
        mapping_generation=previous.mapping_generation + 1,
        predecessor_mapping_sha256=core_project_mapping_sha256_v2(previous),
        project=project,
        last_event_id="event-2",
        last_event_sequence=2,
        last_event_payload_sha256="e" * 64,
    )


def test_mapping_binds_distinct_v2_authorities_and_has_no_generic_revision() -> None:
    mapping = _mapping()
    assert mapping.active_project_head is not None
    assert mapping.active_project_head.evolution_revision.evolution_revision_id == "evolution-0"
    assert "revision" not in mapping.__dataclass_fields__
    with pytest.raises((TypeError, ValueError)):
        replace(mapping, core_project_id="other-project")
    with pytest.raises((TypeError, ValueError)):
        replace(mapping, daemon_registry_sha256="f" * 64)


def test_store_uses_private_separate_namespace_and_atomic_mapping_history(
    tmp_path: Path,
) -> None:
    root = tmp_path / "core-bridge-v2"
    root.mkdir(mode=0o700)
    store = DesktopCoreBridgeStoreV2(root)
    first = _mapping()
    store.commit_mapping(first, expected_previous=None)
    store.commit_mapping(first, expected_previous=None)
    assert store.load_mapping("desktop-project-1") == first
    assert store.load_mapping_by_core_project_id(first.core_project_id) == first
    assert store.load_mapping_by_core_project_id("project-missing") is None
    assert store.load_mapping_history("desktop-project-1") == (first,)
    assert store.database_path.name == "core-bridge-v2.sqlite3"
    assert len(store.schema_fingerprint) == 64

    second = _successor(first)
    store.commit_mapping(second, expected_previous=first)
    assert store.load_mapping_history("desktop-project-1") == (first, second)
    store.close()


def test_store_rejects_nonadjacent_or_owner_drifting_mapping(tmp_path: Path) -> None:
    root = tmp_path / "state"
    root.mkdir(mode=0o700)
    with DesktopCoreBridgeStoreV2(root) as store:
        first = _mapping()
        store.commit_mapping(first, expected_previous=None)
        with pytest.raises(CoreBridgeStoreConflictV2):
            store.commit_mapping(
                replace(
                    first,
                    mapping_generation=2,
                    predecessor_mapping_sha256="0" * 64,
                ),
                expected_previous=first,
            )
        with pytest.raises(CoreBridgeStoreConflictV2):
            store.commit_mapping(
                replace(
                    first,
                    profile_id="profile-other",
                    mapping_generation=2,
                    predecessor_mapping_sha256=core_project_mapping_sha256_v2(first),
                ),
                expected_previous=first,
            )


def test_mutation_replay_ledger_has_exact_state_machine(tmp_path: Path) -> None:
    root = tmp_path / "state"
    root.mkdir(mode=0o700)
    prepared = CoreBridgeMutationV2(
        desktop_project_id="desktop-project-1",
        profile_id="profile-1",
        profile_connection_generation=3,
        operation="submitCoreTaskV2",
        resource_scope="project-1",
        idempotency_key="submit-task-0001",
        request_sha256="a" * 64,
        state=CoreBridgeMutationStateV2.PREPARED,
        response_sha256=None,
        response_resource_id=None,
    )
    with DesktopCoreBridgeStoreV2(root) as store:
        assert store.reserve_mutation(prepared) == prepared
        assert store.reserve_mutation(prepared) == prepared
        unknown = store.mark_mutation_unknown(prepared)
        assert unknown.state is CoreBridgeMutationStateV2.UNKNOWN
        applied = store.mark_mutation_applied(
            unknown,
            response_sha256="b" * 64,
            response_resource_id="task-1",
        )
        assert applied.state is CoreBridgeMutationStateV2.APPLIED
        assert (
            store.load_mutation(
                "desktop-project-1",
                "submitCoreTaskV2",
                "submit-task-0001",
            )
            == applied
        )
        with pytest.raises(CoreBridgeStoreConflictV2):
            store.reserve_mutation(replace(prepared, request_sha256="c" * 64))


def test_release_evidence_summary_requires_one_applied_project_create(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state"
    root.mkdir(mode=0o700)
    mapping = _mapping()
    action_id = "release-project-create-action"
    prepared = CoreBridgeMutationV2(
        desktop_project_id=mapping.desktop_project_id,
        profile_id=mapping.profile_id,
        profile_connection_generation=mapping.profile_connection_generation,
        operation="create_project_v2",
        resource_scope="projects",
        idempotency_key=action_id,
        request_sha256="a" * 64,
        state=CoreBridgeMutationStateV2.PREPARED,
        response_sha256=None,
        response_resource_id=None,
    )
    with DesktopCoreBridgeStoreV2(root) as store:
        store.commit_mapping(mapping, expected_previous=None)
        store.reserve_mutation(prepared)
        store.mark_mutation_applied(
            prepared,
            response_sha256="b" * 64,
            response_resource_id=mapping.core_project_id,
        )

        assert store.release_evidence_summary(
            core_project_id=mapping.core_project_id,
            action_id=action_id,
        ) == {
            "project_mapping_count": 1,
            "applied_create_project_mutation_count": 1,
        }

        with pytest.raises(CoreBridgeStoreDataV2Error, match="release evidence"):
            store.release_evidence_summary(
                core_project_id=mapping.core_project_id,
                action_id="different-release-action",
            )


def test_startup_rejects_tampered_mapping_and_path_replacement(tmp_path: Path) -> None:
    root = tmp_path / "state"
    root.mkdir(mode=0o700)
    store = DesktopCoreBridgeStoreV2(root)
    store.commit_mapping(_mapping(), expected_previous=None)
    database = store.database_path
    store.close()

    connection = sqlite3.connect(database)
    connection.execute(
        "UPDATE mappings SET document_sha256 = ? WHERE desktop_project_id = ?",
        ("0" * 64, "desktop-project-1"),
    )
    connection.commit()
    connection.close()
    with pytest.raises(CoreBridgeStoreDataV2Error):
        DesktopCoreBridgeStoreV2(root)

    replacement_root = tmp_path / "replacement"
    replacement_root.mkdir(mode=0o700)
    live = DesktopCoreBridgeStoreV2(replacement_root)
    moved = tmp_path / "moved"
    os.rename(replacement_root, moved)
    replacement_root.mkdir(mode=0o700)
    with pytest.raises(CoreBridgeStoreStateV2Error):
        live.load_mapping("desktop-project-1")
    live.close(suppress_errors=True)


def test_live_store_rejects_owner_lock_path_replacement(tmp_path: Path) -> None:
    root = tmp_path / "state"
    root.mkdir(mode=0o700)
    live = DesktopCoreBridgeStoreV2(root)
    lock_path = root / OWNER_LOCK_FILENAME
    os.rename(lock_path, root / "displaced-owner-lock")
    replacement = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(replacement)

    with pytest.raises(CoreBridgeStoreStateV2Error):
        live.load_mapping("desktop-project-1")
    live.close(suppress_errors=True)


def test_mapping_rejects_config_digest_or_head_context_drift() -> None:
    mapping = _mapping()
    bad_project = mapping.core_project.model_copy(update={"project_config_sha256": "0" * 64})
    with pytest.raises((TypeError, ValueError)):
        replace(mapping, core_project=bad_project)

    head = mapping.active_project_head
    assert head is not None
    bad_head = head.model_copy(
        update={
            "evolution_revision": head.evolution_revision.model_copy(
                update={"evolution_revision_id": "other-evolution"}
            )
        }
    )
    with pytest.raises((TypeError, ValueError)):
        replace(mapping, active_project_head=bad_head)
