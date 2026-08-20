from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3

import pytest

from desktop.sidecar.contracts.v2 import models as contract_models
from desktop.sidecar.contracts.v2.models import (
    ProfileConnectionActionV2,
    ProfileDisplayNamePatchV2,
    ProfileRebindV2,
    SystemOpenSshProfileCreateV2,
)
from desktop.sidecar import provider_store_v2 as store_module
from desktop.sidecar.provider_store_v2 import (
    LegacyDraftSourceV2,
    LegacyProfileImportV2,
    ProviderCapacityV2Error,
    ProviderIdempotencyConflictV2,
    ProviderPreconditionFailedV2,
    ProviderSchemaV2Error,
    ProviderStateV2Error,
    DesktopProviderStoreV2,
)
from desktop.sidecar.release_runtime import create_release_local_state_v2
from desktop.sidecar.release_capabilities import V0110_RELEASE_AUTHORITY_POLICY
from openevo.backend.contracts.v2.models import (
    ContractOfferV2,
    ScienceProjectConfigV2,
    VersionResponseV2,
)


class _Clock:
    def __init__(self) -> None:
        self._next = datetime(2026, 7, 23, 4, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        value = self._next
        self._next += timedelta(microseconds=1)
        return value


class _MutableClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 7, 27, 4, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        current = self.value
        self.value += timedelta(microseconds=1)
        return current

    def advance(self, delta: timedelta) -> None:
        self.value += delta


def _profile(alias: str = "evolab") -> SystemOpenSshProfileCreateV2:
    return SystemOpenSshProfileCreateV2(
        display_name="Evolution lab",
        ssh_host_alias=alias,
    )


def _science_config() -> ScienceProjectConfigV2:
    return ScienceProjectConfigV2.model_validate(
        {
            "task": {
                "title": "Migrated draft",
                "objective": "Continue only after strict v2 validation.",
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
    )


def _legacy_profile(*, state: str = "rebind_required") -> LegacyProfileImportV2:
    return LegacyProfileImportV2.model_validate(
        {
            "source_ref_sha256": "1" * 64,
            "source_document_sha256": "2" * 64,
            "display_name": "Preview lab",
            "migration_state": state,
            "created_at": "2026-07-22T01:00:00.000000Z",
            "updated_at": "2026-07-22T02:00:00.000000Z",
        }
    )


def _core_version() -> VersionResponseV2:
    features = list(V0110_RELEASE_AUTHORITY_POLICY.required_core_feature_flags)
    encoded = json.dumps(features, separators=(",", ":")).encode("ascii")
    return VersionResponseV2(
        api_name="openevo-core-control-api",
        preferred_major=2,
        supported_majors=[2],
        mutation_major=2,
        contracts=[
            ContractOfferV2(
                api_major=2,
                openapi_sha256=V0110_RELEASE_AUTHORITY_POLICY.core_openapi_sha256,
                event_schema_sha256=(V0110_RELEASE_AUTHORITY_POLICY.core_event_schema_sha256),
                access="mutation",
                mutation_compatible=True,
            )
        ],
        release_version="0.1.10",
        build_id="b" * 64,
        source_commit="a" * 40,
        build_channel="release",
        provider_kind="openevo_daemon",
        feature_flags=features,
        feature_set_sha256=hashlib.sha256(encoded).hexdigest(),
        registry_sha256="c" * 64,
        runtime_contract_sha256="d" * 64,
        mutation_compatible=True,
    )


def _project_create_request() -> contract_models.ProjectCreateV2:
    return contract_models.ProjectCreateV2(
        profile_id="profile-project-owner",
        profile_connection_generation=3,
        display_name="Lifecycle project",
        config=_science_config(),
    )


def _project_reservation(
    project_id: str = "desktop-project-1",
) -> object:
    return store_module.LifecycleOperationReservationV2(
        kind="project_create",
        resource={"resource_kind": "project", "resource_id": project_id},
        request=store_module.LifecycleProjectCreateRequestV2(
            request_kind="project_create",
            project_id=project_id,
            action_id="lifecycle-project-action-0001",
            request=_project_create_request(),
            resource_generation=3,
        ),
    )


def _connect_reservation(
    profile: contract_models.RemoteWorkspaceProfileV2,
) -> object:
    return store_module.LifecycleOperationReservationV2(
        kind="profile_connect",
        resource={"resource_kind": "profile", "resource_id": profile.profile_id},
        request=store_module.LifecycleProfileConnectRequestV2(
            request_kind="profile_connect",
            profile_id=profile.profile_id,
            request=ProfileConnectionActionV2(
                expected_connection_generation=profile.connection_generation
            ),
            resource_generation=profile.connection_generation,
            if_match=profile.etag,
        ),
    )


def _disconnect_reservation(
    profile: contract_models.RemoteWorkspaceProfileV2,
) -> object:
    return store_module.LifecycleOperationReservationV2(
        kind="profile_disconnect",
        resource={"resource_kind": "profile", "resource_id": profile.profile_id},
        request=store_module.LifecycleProfileDisconnectRequestV2(
            request_kind="profile_disconnect",
            profile_id=profile.profile_id,
            request=ProfileConnectionActionV2(
                expected_connection_generation=profile.connection_generation
            ),
            resource_generation=profile.connection_generation,
            if_match=profile.etag,
        ),
    )


def test_release_state_uses_a_separate_v2_namespace_and_exact_schema(
    tmp_path: Path,
) -> None:
    assert store_module.EXPECTED_SCHEMA_V1_SHA256 == (
        "d2ae490ad5b98ca03548570a8d56a6a5ea349694ed647102a69eb5b69e3dac34"
    )
    assert store_module.EXPECTED_SCHEMA_V2_SHA256 == (
        "7314032a52da83b70a43f36f161984bef8bf03274848bf62ab1963a039279c06"
    )
    assert store_module.EXPECTED_SCHEMA_V3_SHA256 == (
        "fa2284e9374ed21bdeaa318565c81692314430ce9a1bd43251bccb886c31c5c6"
    )
    base = tmp_path / "state-v2"
    runtime = create_release_local_state_v2(base, clock=_Clock())
    try:
        store = runtime.provider_store
        assert store.state_root == base / "provider-v2"
        assert store.database_path == base / "provider-v2" / "provider-v2.sqlite3"
        assert not (base / "provider.sqlite3").exists()
        assert store.schema_fingerprint == store_module.EXPECTED_SCHEMA_V3_SHA256
        assert store.list_profiles() == ()
        assert runtime.legacy_import.profiles == ()
        assert runtime.legacy_import.diagnostics == ()

        with sqlite3.connect(store.database_path) as connection:
            assert connection.execute("PRAGMA user_version").fetchone()[0] == 3
            row = connection.execute(
                "SELECT namespace, schema_version, schema_sha256 FROM schema_metadata"
            ).fetchone()
            assert row == (
                "openevo.desktop.provider.v2",
                3,
                store_module.EXPECTED_SCHEMA_V3_SHA256,
            )
    finally:
        runtime.close()


def test_profile_create_replay_conflict_etag_and_restart_are_exact(
    tmp_path: Path,
) -> None:
    root = tmp_path / "provider-v2"
    store = DesktopProviderStoreV2(root, clock=_Clock())
    created = store.create_system_profile(
        _profile(),
        catalog_generation=7,
        idempotency_key="create-system-profile-0001",
    )
    replay = store.create_system_profile(
        _profile(),
        catalog_generation=7,
        idempotency_key="create-system-profile-0001",
    )
    assert replay == created
    assert created.profile_kind == "system_openssh"
    assert created.ssh_host_alias == "evolab"
    assert created.connection_generation == 1
    assert created.catalog_generation == 7

    reused = store.create_system_profile(
        SystemOpenSshProfileCreateV2(
            display_name="Another card name",
            ssh_host_alias="evolab",
        ),
        catalog_generation=7,
        idempotency_key="create-system-profile-same-alias-0002",
    )
    assert reused == created
    assert store.list_profiles() == (created,)

    with pytest.raises(ProviderIdempotencyConflictV2):
        store.create_system_profile(
            _profile("other-lab"),
            catalog_generation=7,
            idempotency_key="create-system-profile-0001",
        )

    renamed = store.rename_profile(
        created.profile_id,
        ProfileDisplayNamePatchV2(display_name="Renamed lab"),
        if_match=created.etag,
        idempotency_key="rename-system-profile-0001",
    )
    assert renamed.display_name == "Renamed lab"
    assert renamed.etag != created.etag
    with pytest.raises(ProviderPreconditionFailedV2):
        store.rename_profile(
            created.profile_id,
            ProfileDisplayNamePatchV2(display_name="Stale rename"),
            if_match=created.etag,
            idempotency_key="rename-system-profile-0002",
        )
    assert (
        store.rename_profile(
            created.profile_id,
            ProfileDisplayNamePatchV2(display_name="Renamed lab"),
            if_match=created.etag,
            idempotency_key="rename-system-profile-0001",
        )
        == renamed
    )
    store.close()

    reopened = DesktopProviderStoreV2(root, clock=_Clock())
    try:
        assert reopened.get_profile(created.profile_id) == renamed
        assert (
            reopened.create_system_profile(
                _profile(),
                catalog_generation=7,
                idempotency_key="create-system-profile-0001",
            )
            == created
        )
    finally:
        reopened.close()


def test_disconnect_and_process_restart_preserve_the_reconnect_project_binding(
    tmp_path: Path,
) -> None:
    root = tmp_path / "provider-v2"
    store = DesktopProviderStoreV2(root, clock=_Clock())
    created = store.create_system_profile(
        _profile(),
        catalog_generation=7,
        idempotency_key="reconnect-profile-create-0001",
    )
    connecting = store.begin_profile_action(
        created.profile_id,
        ProfileConnectionActionV2(expected_connection_generation=created.connection_generation),
        action="connect",
        resource_generation=created.connection_generation,
        if_match=created.etag,
        idempotency_key="reconnect-profile-connect-001",
    )
    connected = store.complete_profile_connection(
        created.profile_id,
        connection_generation=connecting.connection_generation,
        core_version=_core_version(),
    )
    bound = store.bind_active_project(
        created.profile_id,
        connection_generation=connected.connection_generation,
        project_id="desktop-project-reconnect",
    )

    disconnecting = store.begin_profile_action(
        created.profile_id,
        ProfileConnectionActionV2(expected_connection_generation=bound.connection_generation),
        action="disconnect",
        resource_generation=bound.connection_generation,
        if_match=bound.etag,
        idempotency_key="reconnect-profile-disconnect-1",
    )
    assert disconnecting.active_project_id == "desktop-project-reconnect"
    disconnected = store.complete_profile_disconnect(
        created.profile_id,
        connection_generation=disconnecting.connection_generation,
    )
    assert disconnected.active_project_id == "desktop-project-reconnect"

    reconnecting = store.begin_profile_action(
        created.profile_id,
        ProfileConnectionActionV2(
            expected_connection_generation=disconnected.connection_generation
        ),
        action="connect",
        resource_generation=disconnected.connection_generation,
        if_match=disconnected.etag,
        idempotency_key="reconnect-profile-connect-002",
    )
    reconnected = store.complete_profile_connection(
        created.profile_id,
        connection_generation=reconnecting.connection_generation,
        core_version=_core_version(),
    )
    assert reconnected.active_project_id == "desktop-project-reconnect"
    store.close()

    reopened = DesktopProviderStoreV2(root, clock=_Clock())
    try:
        recovered = reopened.reconcile_process_restart()
        assert [item.profile_id for item in recovered] == [created.profile_id]
        current = reopened.get_profile(created.profile_id)
        assert current.connection_state == "disconnected"
        assert current.connection_generation == reconnected.connection_generation + 1
        assert current.active_project_id == "desktop-project-reconnect"
        assert current.core_api_major is None
    finally:
        reopened.close()


def test_incompatible_saved_project_can_be_detached_only_while_reconnecting(
    tmp_path: Path,
) -> None:
    store = DesktopProviderStoreV2(tmp_path / "provider-v2", clock=_Clock())
    try:
        created = store.create_system_profile(
            _profile(),
            catalog_generation=7,
            idempotency_key="detach-profile-create-0001",
        )
        connecting = store.begin_profile_action(
            created.profile_id,
            ProfileConnectionActionV2(
                expected_connection_generation=created.connection_generation
            ),
            action="connect",
            resource_generation=created.connection_generation,
            if_match=created.etag,
            idempotency_key="detach-profile-connect-001",
        )
        connected = store.complete_profile_connection(
            created.profile_id,
            connection_generation=connecting.connection_generation,
            core_version=_core_version(),
        )
        bound = store.bind_active_project(
            created.profile_id,
            connection_generation=connected.connection_generation,
            project_id="desktop-project-expired",
        )
        disconnecting = store.begin_profile_action(
            created.profile_id,
            ProfileConnectionActionV2(
                expected_connection_generation=bound.connection_generation
            ),
            action="disconnect",
            resource_generation=bound.connection_generation,
            if_match=bound.etag,
            idempotency_key="detach-profile-disconnect-001",
        )
        disconnected = store.complete_profile_disconnect(
            created.profile_id,
            connection_generation=disconnecting.connection_generation,
        )
        reconnecting = store.begin_profile_action(
            created.profile_id,
            ProfileConnectionActionV2(
                expected_connection_generation=disconnected.connection_generation
            ),
            action="connect",
            resource_generation=disconnected.connection_generation,
            if_match=disconnected.etag,
            idempotency_key="detach-profile-connect-002",
        )

        cleared = store.clear_incompatible_active_project(
            created.profile_id,
            connection_generation=reconnecting.connection_generation,
            project_id="desktop-project-expired",
        )

        assert cleared.connection_state == "connecting"
        assert cleared.active_project_id is None
        completed = store.complete_profile_connection(
            created.profile_id,
            connection_generation=cleared.connection_generation,
            core_version=_core_version(),
        )
        assert completed.connection_state == "connected"
        assert completed.active_project_id is None
    finally:
        store.close()


def test_restart_quarantines_a_profile_with_unproven_ssh_cleanup(
    tmp_path: Path,
) -> None:
    root = tmp_path / "provider-v2"
    store = DesktopProviderStoreV2(root, clock=_Clock())
    created = store.create_system_profile(
        _profile(),
        catalog_generation=7,
        idempotency_key="cleanup-quarantine-profile-create-0001",
    )
    connecting = store.begin_profile_action(
        created.profile_id,
        ProfileConnectionActionV2(
            expected_connection_generation=created.connection_generation
        ),
        action="connect",
        resource_generation=created.connection_generation,
        if_match=created.etag,
        idempotency_key="cleanup-quarantine-profile-connect-001",
    )
    connected = store.complete_profile_connection(
        created.profile_id,
        connection_generation=connecting.connection_generation,
        core_version=_core_version(),
    )
    queued = store.reserve_lifecycle_operation(
        _disconnect_reservation(connected),
        idempotency_key="cleanup-quarantine-disconnect-0001",
    )
    work = store.claim_next_lifecycle_operation()
    assert work is not None
    assert work.operation.operation_id == queued.operation_id
    cleanup_failure = contract_models.DesktopErrorV2(
        code="ssh_cleanup_failed",
        summary="The system OpenSSH connection could not be closed safely.",
        retryable=True,
        action="retry",
        affected_resource_id=created.profile_id,
    )
    failed_profile = store.fail_profile_disconnect(
        created.profile_id,
        connection_generation=connected.connection_generation + 1,
        failure=cleanup_failure,
    )
    assert failed_profile.connection_state == "failed"
    terminal = store.finish_lifecycle_operation(
        store_module.LifecycleOperationCompletionV2(
            operation_id=work.operation.operation_id,
            expected_etag=work.operation.etag,
            status="failed",
            result=None,
            failure=cleanup_failure,
        )
    )
    assert terminal.status == "failed"
    store.close()

    reopened = DesktopProviderStoreV2(root, clock=_Clock())
    try:
        recovered = reopened.reconcile_process_restart()
        assert [item.profile_id for item in recovered] == [created.profile_id]
        quarantined = reopened.get_profile(created.profile_id)
        assert quarantined.connection_state == "failed"
        assert quarantined.connection_generation == failed_profile.connection_generation + 1
        assert quarantined.failure == contract_models.DesktopErrorV2(
            code="ssh_cleanup_authority_lost",
            summary=(
                "Desktop cannot prove that the previous system OpenSSH master stopped."
            ),
            retryable=False,
            action="administrator_action",
            affected_resource_id=created.profile_id,
        )
        with pytest.raises(store_module.ProviderConflictV2, match="cleanup authority"):
            reopened.reserve_lifecycle_operation(
                _disconnect_reservation(quarantined),
                idempotency_key="cleanup-quarantine-disconnect-0002",
            )
        with pytest.raises(store_module.ProviderConflictV2, match="cleanup authority"):
            reopened.begin_profile_action(
                quarantined.profile_id,
                ProfileConnectionActionV2(
                    expected_connection_generation=quarantined.connection_generation
                ),
                action="connect",
                resource_generation=quarantined.connection_generation,
                if_match=quarantined.etag,
                idempotency_key="cleanup-quarantine-connect-direct-0001",
            )
        assert reopened.reconcile_process_restart() == ()
        assert reopened.get_profile(created.profile_id) == quarantined
    finally:
        reopened.close()


def test_restart_quarantines_an_interrupted_running_profile_disconnect(
    tmp_path: Path,
) -> None:
    root = tmp_path / "provider-v2"
    store = DesktopProviderStoreV2(root, clock=_Clock())
    created = store.create_system_profile(
        _profile(),
        catalog_generation=7,
        idempotency_key="running-cleanup-profile-create-0001",
    )
    connecting = store.begin_profile_action(
        created.profile_id,
        ProfileConnectionActionV2(expected_connection_generation=1),
        action="connect",
        resource_generation=1,
        if_match=created.etag,
        idempotency_key="running-cleanup-profile-connect-0001",
    )
    connected = store.complete_profile_connection(
        created.profile_id,
        connection_generation=connecting.connection_generation,
        core_version=_core_version(),
    )
    queued = store.reserve_lifecycle_operation(
        _disconnect_reservation(connected),
        idempotency_key="running-cleanup-disconnect-0001",
    )
    running = store.claim_next_lifecycle_operation()
    assert running is not None
    assert running.operation.operation_id == queued.operation_id
    assert running.operation.status == "running"
    store.close()

    reopened = DesktopProviderStoreV2(root, clock=_Clock())
    try:
        recovered = reopened.reconcile_process_restart()
        assert [item.profile_id for item in recovered] == [created.profile_id]
        quarantined = reopened.get_profile(created.profile_id)
        assert quarantined.connection_state == "failed"
        assert quarantined.failure is not None
        assert quarantined.failure.code == "ssh_cleanup_authority_lost"
        assert quarantined.failure.action == "administrator_action"
        assert quarantined.connection_generation == connected.connection_generation + 2
    finally:
        reopened.close()


def test_native_prompt_observation_is_generation_bound_and_never_persists_text(
    tmp_path: Path,
) -> None:
    store = DesktopProviderStoreV2(tmp_path / "provider-v2", clock=_Clock())
    try:
        created = store.create_system_profile(
            _profile(),
            catalog_generation=1,
            idempotency_key="prompt-profile-create-0001",
        )
        connecting = store.begin_profile_action(
            created.profile_id,
            ProfileConnectionActionV2(expected_connection_generation=1),
            action="connect",
            resource_generation=1,
            if_match=created.etag,
            idempotency_key="prompt-profile-connect-0001",
        )
        pending = store.observe_profile_prompt(
            created.profile_id,
            connection_generation=connecting.connection_generation,
            kind="passphrase",
            state="pending",
        )
        assert pending is not None
        assert pending.connection_state == "prompt_pending"
        assert pending.prompt is not None
        assert pending.prompt.kind == "passphrase"

        resumed = store.observe_profile_prompt(
            created.profile_id,
            connection_generation=connecting.connection_generation,
            kind="passphrase",
            state="completed",
        )
        assert resumed is not None
        assert resumed.connection_state == "connecting"
        assert resumed.prompt is None
        assert (
            store.observe_profile_prompt(
                created.profile_id,
                connection_generation=1,
                kind="password",
                state="pending",
            )
            is None
        )
    finally:
        store.close()


def test_legacy_rebind_preserves_nonconnectable_record_and_creates_new_profile(
    tmp_path: Path,
) -> None:
    store = DesktopProviderStoreV2(tmp_path / "provider-v2", clock=_Clock())
    try:
        legacy = store.import_legacy_profile(_legacy_profile())
        assert legacy.profile_kind == "legacy_explicit"
        assert legacy.connectable is False
        assert "host" not in legacy.model_dump(mode="json")

        rebound = store.rebind_legacy_profile(
            legacy.profile_id,
            ProfileRebindV2(
                ssh_host_alias="configured-lab",
                catalog_generation=11,
            ),
            display_name="Configured lab",
            if_match=legacy.etag,
            idempotency_key="rebind-legacy-profile-0001",
        )
        assert rebound.profile_kind == "system_openssh"
        assert rebound.ssh_host_alias == "configured-lab"
        assert store.get_profile(legacy.profile_id) == legacy
        assert store.list_profiles() == (rebound,)
        assert (
            store.rebind_legacy_profile(
                legacy.profile_id,
                ProfileRebindV2(
                    ssh_host_alias="configured-lab",
                    catalog_generation=11,
                ),
                display_name="Configured lab",
                if_match=legacy.etag,
                idempotency_key="rebind-legacy-profile-0001",
            )
            == rebound
        )

        with pytest.raises(ProviderIdempotencyConflictV2):
            store.rebind_legacy_profile(
                legacy.profile_id,
                ProfileRebindV2(
                    ssh_host_alias="another-lab",
                    catalog_generation=11,
                ),
                display_name="Configured lab",
                if_match=legacy.etag,
                idempotency_key="rebind-legacy-profile-0001",
            )
    finally:
        store.close()


def test_legacy_draft_copy_persists_only_validated_v2_intent(
    tmp_path: Path,
) -> None:
    store = DesktopProviderStoreV2(tmp_path / "provider-v2", clock=_Clock())
    try:
        profile = store.create_system_profile(
            _profile(),
            catalog_generation=1,
            idempotency_key="draft-target-profile-0001",
        )
        source = LegacyDraftSourceV2(
            source_ref_sha256="3" * 64,
            source_document_sha256="4" * 64,
            display_name="Preview draft",
        )
        draft = store.copy_legacy_draft(
            source,
            profile_id=profile.profile_id,
            config=_science_config(),
            idempotency_key="copy-preview-draft-0001",
        )
        assert draft.profile_id == profile.profile_id
        assert draft.config == _science_config()
        assert draft.project_config_sha256
        assert (
            store.copy_legacy_draft(
                source,
                profile_id=profile.profile_id,
                config=_science_config(),
                idempotency_key="copy-preview-draft-0001",
            )
            == draft
        )

        with sqlite3.connect(store.database_path) as connection:
            schema = " ".join(
                row[0] or ""
                for row in connection.execute(
                    "SELECT sql FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%'"
                )
            ).lower()
            assert "remote_state" not in schema
            assert "revision" not in schema
            document = connection.execute(
                "SELECT config_json FROM project_drafts WHERE draft_id = ?",
                (draft.draft_id,),
            ).fetchone()[0]
            assert b"active_revision" not in bytes(document)
            assert b"core_project_id" not in bytes(document)

        invalid = _science_config().model_dump(mode="json")
        invalid["remote"] = {"active_revision": "legacy"}
        with pytest.raises(store_module.ProviderContractV2Error):
            store.copy_legacy_draft(
                LegacyDraftSourceV2(
                    source_ref_sha256="5" * 64,
                    source_document_sha256="6" * 64,
                    display_name="Invalid Preview draft",
                ),
                profile_id=profile.profile_id,
                config=invalid,
                idempotency_key="copy-preview-draft-0002",
            )
    finally:
        store.close()


def _write_schema_v1_database(root: Path) -> None:
    root.mkdir(mode=0o700)
    database = root / store_module.DATABASE_FILENAME
    database.touch(mode=0o600)
    with sqlite3.connect(database) as connection:
        connection.execute("BEGIN EXCLUSIVE")
        for statement in store_module._SCHEMA_V1_STATEMENTS:
            connection.execute(statement)
        timestamp = "2026-07-23T04:00:00.000000Z"
        connection.execute(
            "INSERT INTO schema_metadata VALUES (1, ?, 1, ?, ?)",
            (
                store_module.STORE_NAMESPACE,
                store_module.EXPECTED_SCHEMA_V1_SHA256,
                timestamp,
            ),
        )
        connection.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (1, ?)",
            (timestamp,),
        )
        connection.execute("PRAGMA user_version = 1")


def _write_schema_v2_database_with_authority(root: Path) -> tuple[str, str]:
    root.mkdir(mode=0o700)
    database = root / store_module.DATABASE_FILENAME
    database.touch(mode=0o600)
    timestamp = "2026-07-23T04:00:00.000000Z"
    profile_id = "profile-preserved-v2"
    draft_id = "draft-preserved-v2"
    profile = contract_models.RemoteWorkspaceProfileV2(
        profile_id=profile_id,
        display_name="Preserved profile",
        ssh_host_alias="preserved-lab",
        catalog_generation=2,
        connection_generation=1,
        connection_state="disconnected",
        prompt=None,
        trust=contract_models.SshTrustStateV2(
            connection_generation=1,
            state="unverified",
            review_id=None,
            review_sha256=None,
            key_fingerprints=[],
            repair_support="not_needed",
        ),
        failure=None,
        active_project_id=None,
        core_api_major=None,
        core_openapi_sha256=None,
        core_event_schema_sha256=None,
        core_registry_sha256=None,
        created_at=timestamp,
        updated_at=timestamp,
        etag=DesktopProviderStoreV2._etag("profile", profile_id, 1),
    )
    config = _science_config()
    draft = store_module.LocalProjectDraftV2(
        draft_id=draft_id,
        profile_id=profile_id,
        display_name="Preserved draft",
        config=config,
        project_config_sha256=store_module.project_config_sha256_for(config),
        legacy_source_ref_sha256="7" * 64,
        legacy_source_document_sha256="8" * 64,
        created_at=timestamp,
        updated_at=timestamp,
        etag=DesktopProviderStoreV2._etag("draft", draft_id, 1),
    )
    with sqlite3.connect(database) as connection:
        connection.execute("BEGIN EXCLUSIVE")
        for statement in (
            *store_module._SCHEMA_V1_STATEMENTS,
            *store_module._SCHEMA_V2_ADDITIONS,
        ):
            connection.execute(statement)
        connection.execute(
            "INSERT INTO schema_metadata VALUES (1, ?, 2, ?, ?)",
            (
                store_module.STORE_NAMESPACE,
                store_module.EXPECTED_SCHEMA_V2_SHA256,
                timestamp,
            ),
        )
        connection.executemany(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
            ((1, timestamp), (2, timestamp)),
        )
        connection.execute(
            """
            INSERT INTO profiles(
                profile_id, profile_kind, document_json, resource_version,
                legacy_source_ref_sha256, legacy_source_document_sha256,
                rebound_from_sha256, created_at, updated_at
            ) VALUES (?, 'system_openssh', ?, 1, NULL, NULL, NULL, ?, ?)
            """,
            (
                profile_id,
                store_module._canonical_json_bytes(profile),
                timestamp,
                timestamp,
            ),
        )
        connection.execute(
            """
            INSERT INTO project_drafts(
                draft_id, profile_id, document_json, config_json,
                project_config_sha256, legacy_source_ref_sha256,
                legacy_source_document_sha256, resource_version,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
            """,
            (
                draft_id,
                profile_id,
                store_module._canonical_json_bytes(draft),
                store_module._canonical_json_bytes(config),
                draft.project_config_sha256,
                draft.legacy_source_ref_sha256,
                draft.legacy_source_document_sha256,
                timestamp,
                timestamp,
            ),
        )
        for operation, scope, key, response_kind, response in (
            (
                "createSystemOpenSshProfileV2",
                "profiles",
                "preserved-profile-key-001",
                "profile",
                profile,
            ),
            (
                "copyLegacyDraftV2",
                draft.legacy_source_ref_sha256,
                "preserved-draft-key-0001",
                "draft",
                draft,
            ),
        ):
            connection.execute(
                """
                INSERT INTO idempotency_records(
                    principal, operation, resource_scope, idempotency_key,
                    request_sha256, response_kind, response_resource_version,
                    response_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
                """,
                (
                    store_module.LOCAL_PRINCIPAL,
                    operation,
                    scope,
                    key,
                    "9" * 64,
                    response_kind,
                    store_module._canonical_json_bytes(response),
                    timestamp,
                ),
            )
        connection.execute("PRAGMA user_version = 2")
    return profile_id, draft_id


def test_schema_migration_is_atomic_and_retries_after_interruption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "provider-v2"
    _write_schema_v1_database(root)

    def interrupt(stage: str) -> None:
        if stage == "v1_to_v2_after_ddl":
            raise RuntimeError("simulated migration interruption")

    monkeypatch.setattr(store_module, "_migration_checkpoint", interrupt)
    with pytest.raises(RuntimeError, match="simulated migration interruption"):
        DesktopProviderStoreV2(root, clock=_Clock())

    with sqlite3.connect(root / store_module.DATABASE_FILENAME) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
        assert (
            connection.execute(
                "SELECT 1 FROM sqlite_schema WHERE name = 'project_drafts'"
            ).fetchone()
            is None
        )

    monkeypatch.setattr(store_module, "_migration_checkpoint", lambda _stage: None)
    store = DesktopProviderStoreV2(root, clock=_Clock())
    try:
        assert store.schema_fingerprint == store_module.EXPECTED_SCHEMA_V3_SHA256
    finally:
        store.close()


def test_post_commit_interruption_replays_without_duplicate_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "provider-v2"
    store = DesktopProviderStoreV2(root, clock=_Clock())
    raised = False

    def interrupt(_operation: str) -> None:
        nonlocal raised
        if not raised:
            raised = True
            raise RuntimeError("simulated process loss after commit")

    monkeypatch.setattr(store_module, "_post_commit_checkpoint", interrupt)
    with pytest.raises(RuntimeError, match="after commit"):
        store.create_system_profile(
            _profile(),
            catalog_generation=3,
            idempotency_key="post-commit-profile-0001",
        )
    store.close()

    monkeypatch.setattr(store_module, "_post_commit_checkpoint", lambda _operation: None)
    reopened = DesktopProviderStoreV2(root, clock=_Clock())
    try:
        recovered = reopened.create_system_profile(
            _profile(),
            catalog_generation=3,
            idempotency_key="post-commit-profile-0001",
        )
        assert len(reopened.list_profiles()) == 1
        assert recovered.ssh_host_alias == "evolab"
    finally:
        reopened.close()


def test_restart_atomically_reserves_project_profile_reconnect_prerequisite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "provider-v2"
    store = DesktopProviderStoreV2(root, clock=_Clock())
    created = store.create_system_profile(
        _profile(),
        catalog_generation=1,
        idempotency_key="restart-reclaim-profile-create-0001",
    )
    connecting = store.begin_profile_action(
        created.profile_id,
        ProfileConnectionActionV2(expected_connection_generation=1),
        action="connect",
        resource_generation=1,
        if_match=created.etag,
        idempotency_key="restart-reclaim-profile-connect-0001",
    )
    connected = store.complete_profile_connection(
        created.profile_id,
        connection_generation=connecting.connection_generation,
        core_version=_core_version(),
    )
    project_id = "desktop-project-restart-reclaim"
    request = contract_models.ProjectCreateV2(
        profile_id=connected.profile_id,
        profile_connection_generation=connected.connection_generation,
        display_name="Restart reclaim",
        config=_science_config(),
    )
    project_operation = store.reserve_lifecycle_operation(
        store_module.LifecycleOperationReservationV2(
            kind="project_create",
            resource={"resource_kind": "project", "resource_id": project_id},
            request=store_module.LifecycleProjectCreateRequestV2(
                request_kind="project_create",
                project_id=project_id,
                action_id="restart-reclaim-project-action-0001",
                request=request,
                resource_generation=connected.connection_generation,
            ),
        ),
        idempotency_key="restart-reclaim-project-reserve-0001",
    )
    claimed = store.claim_next_lifecycle_operation()
    assert claimed is not None
    assert claimed.operation.operation_id == project_operation.operation_id
    store.close()

    reopened = DesktopProviderStoreV2(root, clock=_Clock())
    monkeypatch.setattr(
        store_module,
        "_lifecycle_reservation_checkpoint",
        lambda stage: (
            (_ for _ in ()).throw(RuntimeError("restart reservation interrupted"))
            if stage == "after_profile_transition"
            else None
        ),
    )
    with pytest.raises(RuntimeError, match="restart reservation interrupted"):
        reopened.reconcile_process_restart()
    assert reopened.get_profile(created.profile_id) == connected
    assert [item.operation.operation_id for item in reopened.reconcile_lifecycle_operations()] == [
        project_operation.operation_id
    ]

    monkeypatch.setattr(
        store_module,
        "_lifecycle_reservation_checkpoint",
        lambda _stage: None,
    )
    recovered = reopened.reconcile_process_restart()
    assert len(recovered) == 1
    invalidated = recovered[0]
    assert invalidated.connection_state == "disconnected"
    assert invalidated.connection_generation == connected.connection_generation + 1

    current = reopened.get_profile(created.profile_id)
    assert current.connection_state == "connecting"
    assert current.connection_generation == invalidated.connection_generation + 1
    work = reopened.reconcile_lifecycle_operations()
    assert {item.operation.kind for item in work} == {"project_create", "profile_connect"}
    assert len(work) == 2
    reconnect = next(item for item in work if item.operation.kind == "profile_connect")
    assert isinstance(reconnect.request, store_module.LifecycleProfileConnectRequestV2)
    assert reconnect.request.profile_id == created.profile_id
    assert reconnect.request.resource_generation == invalidated.connection_generation
    assert reconnect.request.request.expected_connection_generation == (
        invalidated.connection_generation
    )
    assert reconnect.request.if_match == invalidated.etag
    reconnect_operation_id = reconnect.operation.operation_id
    reopened.close()

    restarted_again = DesktopProviderStoreV2(root, clock=_Clock())
    try:
        assert restarted_again.reconcile_process_restart() == ()
        assert {
            item.operation.operation_id
            for item in restarted_again.reconcile_lifecycle_operations()
        } == {project_operation.operation_id, reconnect_operation_id}
    finally:
        restarted_again.close()


def test_restart_does_not_reconnect_for_a_cancelled_project_operation(
    tmp_path: Path,
) -> None:
    root = tmp_path / "provider-v2"
    store = DesktopProviderStoreV2(root, clock=_Clock())
    created = store.create_system_profile(
        _profile(),
        catalog_generation=1,
        idempotency_key="cancelled-restart-profile-create-0001",
    )
    connecting = store.begin_profile_action(
        created.profile_id,
        ProfileConnectionActionV2(expected_connection_generation=1),
        action="connect",
        resource_generation=1,
        if_match=created.etag,
        idempotency_key="cancelled-restart-profile-connect-0001",
    )
    connected = store.complete_profile_connection(
        created.profile_id,
        connection_generation=connecting.connection_generation,
        core_version=_core_version(),
    )
    project_operation = store.reserve_lifecycle_operation(
        store_module.LifecycleOperationReservationV2(
            kind="project_create",
            resource={
                "resource_kind": "project",
                "resource_id": "desktop-project-cancelled-restart",
            },
            request=store_module.LifecycleProjectCreateRequestV2(
                request_kind="project_create",
                project_id="desktop-project-cancelled-restart",
                action_id="cancelled-restart-project-action-0001",
                request=contract_models.ProjectCreateV2(
                    profile_id=connected.profile_id,
                    profile_connection_generation=connected.connection_generation,
                    display_name="Cancelled restart",
                    config=_science_config(),
                ),
                resource_generation=connected.connection_generation,
            ),
        ),
        idempotency_key="cancelled-restart-project-reserve-0001",
    )
    claimed = store.claim_next_lifecycle_operation()
    assert claimed is not None
    requested = store.request_lifecycle_cancellation(
        project_operation.operation_id,
        if_match=claimed.operation.etag,
        idempotency_key="cancelled-restart-project-cancel-0001",
    )
    assert requested.status == "running"
    assert requested.cancellable is False
    store.close()

    reopened = DesktopProviderStoreV2(root, clock=_Clock())
    try:
        recovered = reopened.reconcile_process_restart()
        assert len(recovered) == 1
        assert recovered[0].connection_state == "disconnected"
        pending = reopened.reconcile_lifecycle_operations()
        assert [work.operation.operation_id for work in pending] == [
            project_operation.operation_id
        ]
        assert pending[0].cancellation_requested is True
    finally:
        reopened.close()


def test_capacity_is_bounded_but_exact_retry_survives_full_store(
    tmp_path: Path,
) -> None:
    store = DesktopProviderStoreV2(
        tmp_path / "provider-v2",
        clock=_Clock(),
        max_profiles=1,
    )
    try:
        first = store.create_system_profile(
            _profile(),
            catalog_generation=1,
            idempotency_key="bounded-profile-create-0001",
        )
        with pytest.raises(ProviderCapacityV2Error):
            store.create_system_profile(
                _profile("second-lab"),
                catalog_generation=1,
                idempotency_key="bounded-profile-create-0002",
            )
        assert (
            store.create_system_profile(
                _profile(),
                catalog_generation=1,
                idempotency_key="bounded-profile-create-0001",
            )
            == first
        )
    finally:
        store.close()


def test_oversized_sqlite_journal_is_rejected_before_database_open(
    tmp_path: Path,
) -> None:
    root = tmp_path / "provider-v2"
    store = DesktopProviderStoreV2(root, clock=_Clock())
    store.close()
    journal = root / store_module.JOURNAL_FILENAME
    journal.touch(mode=0o600)
    os.truncate(journal, store_module.MAX_JOURNAL_BYTES + 1)

    with pytest.raises(ProviderStateV2Error):
        DesktopProviderStoreV2(root, clock=_Clock())


def test_schema_drift_oversized_rows_and_path_replacement_fail_closed(
    tmp_path: Path,
) -> None:
    root = tmp_path / "provider-v2"
    store = DesktopProviderStoreV2(root, clock=_Clock())
    profile = store.create_system_profile(
        _profile(),
        catalog_generation=1,
        idempotency_key="tamper-profile-create-0001",
    )
    held = tmp_path / "held-provider-v2.sqlite3"
    store.database_path.rename(held)
    store.database_path.touch(mode=0o600)
    with pytest.raises(ProviderStateV2Error):
        store.list_profiles()
    store.close()

    os.replace(held, root / store_module.DATABASE_FILENAME)
    with sqlite3.connect(root / store_module.DATABASE_FILENAME) as connection:
        connection.execute("CREATE TABLE injected(value TEXT) STRICT")
    with pytest.raises(ProviderSchemaV2Error):
        DesktopProviderStoreV2(root, clock=_Clock())

    with sqlite3.connect(root / store_module.DATABASE_FILENAME) as connection:
        connection.execute("DROP TABLE injected")
        connection.execute("PRAGMA ignore_check_constraints = ON")
        connection.execute(
            "UPDATE profiles SET document_json = zeroblob(?) WHERE profile_id = ?",
            (store_module.MAX_PROFILE_DOCUMENT_BYTES + 1, profile.profile_id),
        )
    with pytest.raises(store_module.ProviderDataV2Error):
        DesktopProviderStoreV2(root, clock=_Clock())


def test_startup_rejects_corrupt_idempotency_identity_and_response_etag(
    tmp_path: Path,
) -> None:
    root = tmp_path / "provider-v2"
    store = DesktopProviderStoreV2(root, clock=_Clock())
    store.create_system_profile(
        _profile(),
        catalog_generation=1,
        idempotency_key="recovery-profile-create-0001",
    )
    store.close()

    with sqlite3.connect(root / store_module.DATABASE_FILENAME) as connection:
        connection.execute(
            "UPDATE idempotency_records SET request_sha256 = ?",
            ("z" * 64,),
        )
    with pytest.raises(store_module.ProviderDataV2Error):
        DesktopProviderStoreV2(root, clock=_Clock())

    with sqlite3.connect(root / store_module.DATABASE_FILENAME) as connection:
        connection.execute(
            "UPDATE idempotency_records SET request_sha256 = ?",
            ("a" * 64,),
        )
        raw = connection.execute("SELECT response_json FROM idempotency_records").fetchone()[0]
        document = json.loads(bytes(raw))
        document["etag"] = f'"{"f" * 64}"'
        connection.execute(
            "UPDATE idempotency_records SET response_json = ?",
            (store_module._canonical_json_bytes(document),),
        )
    with pytest.raises(store_module.ProviderDataV2Error):
        DesktopProviderStoreV2(root, clock=_Clock())


def test_startup_rejects_a_draft_without_its_system_openssh_profile(
    tmp_path: Path,
) -> None:
    root = tmp_path / "provider-v2"
    store = DesktopProviderStoreV2(root, clock=_Clock())
    profile = store.create_system_profile(
        _profile(),
        catalog_generation=1,
        idempotency_key="orphan-profile-create-0001",
    )
    store.copy_legacy_draft(
        LegacyDraftSourceV2(
            source_ref_sha256="7" * 64,
            source_document_sha256="8" * 64,
            display_name="Orphan candidate",
        ),
        profile_id=profile.profile_id,
        config=_science_config(),
        idempotency_key="orphan-draft-copy-0001",
    )
    store.close()

    with sqlite3.connect(root / store_module.DATABASE_FILENAME) as connection:
        connection.execute("DELETE FROM idempotency_records WHERE response_kind = 'profile'")
        connection.execute(
            "DELETE FROM profiles WHERE profile_id = ?",
            (profile.profile_id,),
        )

    with pytest.raises(store_module.ProviderDataV2Error):
        DesktopProviderStoreV2(root, clock=_Clock())


def test_schema_v3_migration_is_atomic_and_preserves_v019_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "provider-v2"
    profile_id, draft_id = _write_schema_v2_database_with_authority(root)

    def interrupt(stage: str) -> None:
        if stage == "v2_to_v3_after_ddl":
            raise RuntimeError("simulated v2-to-v3 interruption")

    monkeypatch.setattr(store_module, "_migration_checkpoint", interrupt)
    with pytest.raises(RuntimeError, match="v2-to-v3"):
        DesktopProviderStoreV2(root, clock=_Clock())
    with sqlite3.connect(root / store_module.DATABASE_FILENAME) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
        assert (
            connection.execute(
                "SELECT 1 FROM sqlite_schema WHERE name = 'lifecycle_operations'"
            ).fetchone()
            is None
        )

    monkeypatch.setattr(store_module, "_migration_checkpoint", lambda _stage: None)
    store = DesktopProviderStoreV2(root, clock=_Clock())
    try:
        assert store_module.SCHEMA_VERSION == 3
        assert store.schema_fingerprint == store_module.EXPECTED_SCHEMA_V3_SHA256
        assert store.get_profile(profile_id).profile_id == profile_id
        assert [draft.draft_id for draft in store.list_drafts()] == [draft_id]
        with sqlite3.connect(store.database_path) as connection:
            assert connection.execute("PRAGMA user_version").fetchone()[0] == 3
            assert (
                connection.execute("SELECT count(*) FROM idempotency_records").fetchone()[0] == 2
            )
            assert (
                len(
                    connection.execute(
                        "SELECT cursor_key FROM lifecycle_cursor_key WHERE singleton = 1"
                    ).fetchone()[0]
                )
                == 32
            )
    finally:
        store.close()


def test_lifecycle_reservation_is_exact_replay_and_survives_capacity_and_restart(
    tmp_path: Path,
) -> None:
    root = tmp_path / "provider-v2"
    store = DesktopProviderStoreV2(
        root,
        clock=_Clock(),
        max_lifecycle_operations=1,
    )
    request = _project_reservation()
    operation = store.reserve_lifecycle_operation(
        request,
        idempotency_key="project-create-action-0001",
    )
    assert operation.kind == "project_create"
    assert operation.status == "queued"
    assert operation.phase == "queued"
    assert operation.started_at is None
    assert (
        store.reserve_lifecycle_operation(
            request,
            idempotency_key="project-create-action-0001",
        )
        == operation
    )

    with pytest.raises(ProviderIdempotencyConflictV2):
        store.reserve_lifecycle_operation(
            _project_reservation("desktop-project-conflict"),
            idempotency_key="project-create-action-0001",
        )
    with pytest.raises(ProviderCapacityV2Error):
        store.reserve_lifecycle_operation(
            _project_reservation("desktop-project-2"),
            idempotency_key="project-create-action-0002",
        )
    assert store.list_pending_lifecycle_operations() == (
        contract_models.LifecycleOperationRefV2.from_operation(operation),
    )
    store.close()

    reopened = DesktopProviderStoreV2(
        root,
        clock=_Clock(),
        max_lifecycle_operations=1,
    )
    try:
        assert reopened.get_lifecycle_operation(operation.operation_id) == operation
        assert (
            reopened.reserve_lifecycle_operation(
                request,
                idempotency_key="project-create-action-0001",
            )
            == operation
        )
    finally:
        reopened.close()


def test_profile_transition_and_lifecycle_reservation_are_one_transaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = DesktopProviderStoreV2(tmp_path / "provider-v2", clock=_Clock())
    profile = store.create_system_profile(
        _profile(),
        catalog_generation=1,
        idempotency_key="atomic-profile-create-0001",
    )

    def interrupt(stage: str) -> None:
        if stage == "after_profile_transition":
            raise RuntimeError("simulated reservation interruption")

    monkeypatch.setattr(store_module, "_lifecycle_reservation_checkpoint", interrupt)
    with pytest.raises(RuntimeError, match="reservation interruption"):
        store.reserve_lifecycle_operation(
            _connect_reservation(profile),
            idempotency_key="atomic-profile-connect-001",
        )
    assert store.get_profile(profile.profile_id) == profile
    assert store.list_pending_lifecycle_operations() == ()

    monkeypatch.setattr(
        store_module,
        "_lifecycle_reservation_checkpoint",
        lambda _stage: None,
    )
    operation = store.reserve_lifecycle_operation(
        _connect_reservation(profile),
        idempotency_key="atomic-profile-connect-001",
    )
    transitioned = store.get_profile(profile.profile_id)
    assert transitioned.connection_state == "connecting"
    assert transitioned.connection_generation == profile.connection_generation + 1
    assert operation.resource.resource_id == profile.profile_id
    assert (
        store.reserve_lifecycle_operation(
            _connect_reservation(profile),
            idempotency_key="atomic-profile-connect-001",
        )
        == operation
    )
    store.close()


def test_profile_connect_cancellation_atomically_restores_disconnected_state(
    tmp_path: Path,
) -> None:
    store = DesktopProviderStoreV2(tmp_path / "provider-v2", clock=_Clock())
    first = store.create_system_profile(
        _profile(),
        catalog_generation=1,
        idempotency_key="cancel-profile-create-0001",
    )
    queued = store.reserve_lifecycle_operation(
        _connect_reservation(first),
        idempotency_key="cancel-profile-connect-0001",
    )

    cancelled = store.request_lifecycle_cancellation(
        queued.operation_id,
        if_match=queued.etag,
        idempotency_key="cancel-profile-connect-request-0001",
    )

    assert cancelled.status == "cancelled"
    queued_profile = store.get_profile(first.profile_id)
    assert queued_profile.connection_state == "disconnected"
    assert queued_profile.failure is None

    second = store.create_system_profile(
        _profile("evolab-second"),
        catalog_generation=1,
        idempotency_key="cancel-profile-create-0002",
    )
    running = store.reserve_lifecycle_operation(
        _connect_reservation(second),
        idempotency_key="cancel-profile-connect-0002",
    )
    work = store.claim_next_lifecycle_operation()
    assert work is not None
    assert work.operation.operation_id == running.operation_id
    requested = store.request_lifecycle_cancellation(
        running.operation_id,
        if_match=work.operation.etag,
        idempotency_key="cancel-profile-connect-request-0002",
    )
    terminal = store.finish_lifecycle_operation(
        store_module.LifecycleOperationCompletionV2(
            operation_id=requested.operation_id,
            expected_etag=requested.etag,
            status="cancelled",
            result=None,
            failure=None,
        )
    )

    assert terminal.status == "cancelled"
    running_profile = store.get_profile(second.profile_id)
    assert running_profile.connection_state == "disconnected"
    assert running_profile.failure is None
    store.close()


def test_running_profile_cancellation_atomically_overrides_same_generation_failure(
    tmp_path: Path,
) -> None:
    store = DesktopProviderStoreV2(tmp_path / "provider-v2", clock=_Clock())
    profile = store.create_system_profile(
        _profile(),
        catalog_generation=1,
        idempotency_key="cancel-racing-failure-profile-create-0001",
    )
    queued = store.reserve_lifecycle_operation(
        _connect_reservation(profile),
        idempotency_key="cancel-racing-failure-profile-connect-0001",
    )
    work = store.claim_next_lifecycle_operation()
    assert work is not None
    assert work.operation.operation_id == queued.operation_id
    requested = store.request_lifecycle_cancellation(
        queued.operation_id,
        if_match=work.operation.etag,
        idempotency_key="cancel-racing-failure-request-0001",
    )
    failed = store.fail_profile_connection(
        profile.profile_id,
        connection_generation=profile.connection_generation + 1,
        failure=contract_models.DesktopErrorV2(
            code="ssh_connection_failed",
            summary="System OpenSSH could not establish the remote workspace connection.",
            retryable=True,
            action="retry",
            affected_resource_id=profile.profile_id,
        ),
    )
    assert failed.connection_state == "failed"
    with pytest.raises(store_module.ProviderConflictV2, match="active lifecycle"):
        store.reserve_lifecycle_operation(
            _connect_reservation(failed),
            idempotency_key="connect-before-cancel-terminal-0001",
        )
    assert [
        item.operation_id for item in store.list_pending_lifecycle_operations()
    ] == [requested.operation_id]

    terminal = store.finish_lifecycle_operation(
        store_module.LifecycleOperationCompletionV2(
            operation_id=requested.operation_id,
            expected_etag=requested.etag,
            status="cancelled",
            result=None,
            failure=None,
        )
    )

    assert terminal.status == "cancelled"
    assert (
        store.finish_lifecycle_operation(
            store_module.LifecycleOperationCompletionV2(
                operation_id=requested.operation_id,
                expected_etag=requested.etag,
                status="cancelled",
                result=None,
                failure=None,
            )
        )
        == terminal
    )
    cancelled_profile = store.get_profile(profile.profile_id)
    assert cancelled_profile.connection_generation == profile.connection_generation + 1
    assert cancelled_profile.connection_state == "disconnected"
    assert cancelled_profile.failure is None
    store.close()


def test_running_profile_cancellation_preserves_unproven_ssh_cleanup(
    tmp_path: Path,
) -> None:
    root = tmp_path / "provider-v2"
    store = DesktopProviderStoreV2(root, clock=_Clock())
    profile = store.create_system_profile(
        _profile(),
        catalog_generation=1,
        idempotency_key="cancel-cleanup-profile-create-0001",
    )
    queued = store.reserve_lifecycle_operation(
        _connect_reservation(profile),
        idempotency_key="cancel-cleanup-profile-connect-0001",
    )
    work = store.claim_next_lifecycle_operation()
    assert work is not None
    requested = store.request_lifecycle_cancellation(
        queued.operation_id,
        if_match=work.operation.etag,
        idempotency_key="cancel-cleanup-profile-request-0001",
    )
    cleanup_failure = contract_models.DesktopErrorV2(
        code="ssh_cleanup_failed",
        summary="The system OpenSSH connection could not be closed safely.",
        retryable=True,
        action="retry",
        affected_resource_id=profile.profile_id,
    )
    store.fail_profile_connection(
        profile.profile_id,
        connection_generation=profile.connection_generation + 1,
        failure=cleanup_failure,
    )

    terminal = store.finish_lifecycle_operation(
        store_module.LifecycleOperationCompletionV2(
            operation_id=requested.operation_id,
            expected_etag=requested.etag,
            status="cancelled",
            result=None,
            failure=None,
        )
    )

    assert terminal.status == "cancelled"
    retained = store.get_profile(profile.profile_id)
    assert retained.connection_state == "failed"
    assert retained.failure == cleanup_failure
    store.close()

    reopened = DesktopProviderStoreV2(root, clock=_Clock())
    try:
        assert [item.profile_id for item in reopened.reconcile_process_restart()] == [
            profile.profile_id
        ]
        quarantined = reopened.get_profile(profile.profile_id)
        assert quarantined.connection_state == "failed"
        assert quarantined.failure is not None
        assert quarantined.failure.code == "ssh_cleanup_authority_lost"
    finally:
        reopened.close()


def test_running_lifecycle_cancellation_replays_with_the_latest_etag(
    tmp_path: Path,
) -> None:
    root = tmp_path / "provider-v2"
    store = DesktopProviderStoreV2(root, clock=_Clock())
    queued = store.reserve_lifecycle_operation(
        _project_reservation("project-cancel-replay"),
        idempotency_key="cancel-replay-project-create-0001",
    )
    work = store.claim_next_lifecycle_operation()
    assert work is not None
    requested = store.request_lifecycle_cancellation(
        queued.operation_id,
        if_match=work.operation.etag,
        idempotency_key="cancel-replay-running-operation-0001",
    )

    assert (
        store.request_lifecycle_cancellation(
            queued.operation_id,
            if_match=requested.etag,
            idempotency_key="cancel-replay-running-operation-0001",
        )
        == requested
    )
    with pytest.raises(
        store_module.ProviderConflictV2,
        match="not safely cancellable",
    ):
        store.request_lifecycle_cancellation(
            queued.operation_id,
            if_match=requested.etag,
            idempotency_key="cancel-replay-distinct-action-0002",
        )
    store.close()
    with sqlite3.connect(root / store_module.DATABASE_FILENAME) as connection:
        assert connection.execute(
            "SELECT count(*) FROM lifecycle_idempotency_records "
            "WHERE action = 'cancel' AND operation_id = ?",
            (queued.operation_id,),
        ).fetchone() == (1,)


@pytest.mark.parametrize("digest_kind", ["forged", "legacy_if_match"])
def test_noncanonical_lifecycle_cancellation_digest_fails_recovery_closed(
    tmp_path: Path,
    digest_kind: str,
) -> None:
    root = tmp_path / "provider-v2"
    store = DesktopProviderStoreV2(root, clock=_Clock())
    queued = store.reserve_lifecycle_operation(
        _project_reservation("project-legacy-cancel-replay"),
        idempotency_key="legacy-cancel-replay-project-create-0001",
    )
    work = store.claim_next_lifecycle_operation()
    assert work is not None
    store.request_lifecycle_cancellation(
        queued.operation_id,
        if_match=work.operation.etag,
        idempotency_key="legacy-cancel-replay-running-operation-0001",
    )
    store.close()

    noncanonical_digest = (
        "f" * 64
        if digest_kind == "forged"
        else hashlib.sha256(
            store_module._canonical_json_bytes(
                {"operation_id": queued.operation_id, "if_match": work.operation.etag}
            )
        ).hexdigest()
    )
    with sqlite3.connect(root / store_module.DATABASE_FILENAME) as connection:
        connection.execute(
            "UPDATE lifecycle_idempotency_records SET request_sha256 = ? "
            "WHERE action = 'cancel' AND operation_id = ?",
            (noncanonical_digest, queued.operation_id),
        )

    with pytest.raises(
        store_module.ProviderDataV2Error,
        match="cancellation idempotency authority differs",
    ):
        DesktopProviderStoreV2(root, clock=_Clock())


def test_lifecycle_cancellation_record_requires_matching_operation_state(
    tmp_path: Path,
) -> None:
    root = tmp_path / "provider-v2"
    store = DesktopProviderStoreV2(root, clock=_Clock())
    queued = store.reserve_lifecycle_operation(
        _project_reservation("project-forged-cancellation-state"),
        idempotency_key="forged-cancel-state-project-create-0001",
    )
    store.close()

    cancellation_digest = hashlib.sha256(
        store_module._canonical_json_bytes({"operation_id": queued.operation_id})
    ).hexdigest()
    with sqlite3.connect(root / store_module.DATABASE_FILENAME) as connection:
        connection.execute(
            """
            INSERT INTO lifecycle_idempotency_records(
                principal, action, resource_scope, idempotency_key,
                request_sha256, operation_id, created_at
            ) VALUES (?, 'cancel', ?, ?, ?, ?, ?)
            """,
            (
                store_module.LOCAL_PRINCIPAL,
                queued.operation_id,
                "forged-cancel-state-request-0001",
                cancellation_digest,
                queued.operation_id,
                queued.created_at,
            ),
        )

    with pytest.raises(
        store_module.ProviderDataV2Error,
        match="cancellation record differs from operation state",
    ):
        DesktopProviderStoreV2(root, clock=_Clock())


def test_recovery_rejects_cancellation_intent_that_remains_cancellable(
    tmp_path: Path,
) -> None:
    root = tmp_path / "provider-v2"
    store = DesktopProviderStoreV2(root, clock=_Clock())
    queued = store.reserve_lifecycle_operation(
        _project_reservation("project-invalid-cancellation-flags"),
        idempotency_key="invalid-cancel-flags-project-create-0001",
    )
    running = store.claim_next_lifecycle_operation()
    assert running is not None
    store.request_lifecycle_cancellation(
        queued.operation_id,
        if_match=running.operation.etag,
        idempotency_key="invalid-cancel-flags-request-0001",
    )
    store.close()

    with sqlite3.connect(root / store_module.DATABASE_FILENAME) as connection:
        connection.execute(
            "UPDATE lifecycle_operations SET cancellable = 1 WHERE operation_id = ?",
            (queued.operation_id,),
        )

    with pytest.raises(
        store_module.ProviderDataV2Error,
        match="cancellation intent remained cancellable",
    ):
        DesktopProviderStoreV2(root, clock=_Clock())


@pytest.mark.parametrize("terminal_status", ["succeeded", "failed"])
def test_recovery_rejects_non_cancelled_terminal_with_cancellation_intent(
    tmp_path: Path,
    terminal_status: str,
) -> None:
    root = tmp_path / "provider-v2"
    store = DesktopProviderStoreV2(root, clock=_Clock())
    queued = store.reserve_lifecycle_operation(
        _project_reservation("project-cancel-terminal-corruption"),
        idempotency_key="cancel-terminal-corruption-create-0001",
    )
    running = store.claim_next_lifecycle_operation()
    assert running is not None
    requested = store.request_lifecycle_cancellation(
        queued.operation_id,
        if_match=running.operation.etag,
        idempotency_key="cancel-terminal-corruption-request-0001",
    )
    cancelled = store.finish_lifecycle_operation(
        store_module.LifecycleOperationCompletionV2(
            operation_id=queued.operation_id,
            expected_etag=requested.etag,
            status="cancelled",
            result=None,
            failure=None,
        )
    )
    store.close()

    result = (
        store_module._canonical_json_bytes(
            {
                "result_kind": "project",
                "project_id": "core-project-cancel-terminal-corruption",
            }
        )
        if terminal_status == "succeeded"
        else None
    )
    failure = (
        store_module._canonical_json_bytes(
            contract_models.DesktopErrorV2(
                code="corrupt_terminal_winner",
                summary="A non-cancelled terminal result replaced durable cancellation.",
                retryable=False,
                action="none",
                affected_resource_id=queued.operation_id,
            )
        )
        if terminal_status == "failed"
        else None
    )
    with sqlite3.connect(root / store_module.DATABASE_FILENAME) as connection:
        connection.execute(
            """
            UPDATE lifecycle_operations
            SET status = ?, result_json = ?, failure_json = ?
            WHERE operation_id = ?
            """,
            (terminal_status, result, failure, cancelled.operation_id),
        )

    with pytest.raises(
        store_module.ProviderDataV2Error,
        match="terminal lifecycle operation conflicts with cancellation intent",
    ):
        DesktopProviderStoreV2(root, clock=_Clock())


def test_profile_disconnect_reservation_is_an_atomic_non_cancellable_barrier(
    tmp_path: Path,
) -> None:
    store = DesktopProviderStoreV2(tmp_path / "provider-v2", clock=_Clock())
    created = store.create_system_profile(
        _profile(),
        catalog_generation=1,
        idempotency_key="disconnect-barrier-profile-create-0001",
    )
    connecting = store.begin_profile_action(
        created.profile_id,
        ProfileConnectionActionV2(
            expected_connection_generation=created.connection_generation
        ),
        action="connect",
        resource_generation=created.connection_generation,
        if_match=created.etag,
        idempotency_key="disconnect-barrier-profile-connect-0001",
    )
    connected = store.complete_profile_connection(
        created.profile_id,
        connection_generation=connecting.connection_generation,
        core_version=_core_version(),
    )

    disconnect = store.reserve_lifecycle_operation(
        _disconnect_reservation(connected),
        idempotency_key="disconnect-barrier-reserve-0001",
    )

    assert disconnect.status == "queued"
    assert disconnect.cancellable is False
    transitioned = store.get_profile(connected.profile_id)
    assert transitioned.connection_state == "disconnecting"
    with pytest.raises(
        store_module.ProviderConflictV2,
        match="not safely cancellable",
    ):
        store.request_lifecycle_cancellation(
            disconnect.operation_id,
            if_match=disconnect.etag,
            idempotency_key="disconnect-barrier-cancel-0001",
        )
    assert store.get_profile(connected.profile_id) == transitioned
    assert store.get_lifecycle_operation(disconnect.operation_id) == disconnect
    running = store.claim_next_lifecycle_operation()
    assert running is not None
    assert running.operation.operation_id == disconnect.operation_id
    assert running.operation.cancellable is False
    with pytest.raises(
        store_module.ProviderPreconditionFailedV2,
        match="disconnect cancellation barrier",
    ):
        store.advance_lifecycle_operation(
            store_module.LifecycleOperationAdvanceV2(
                operation_id=running.operation.operation_id,
                expected_etag=running.operation.etag,
                phase="connecting",
                progress={"kind": "indeterminate"},
                cancellable=True,
            )
        )
    with pytest.raises(
        store_module.ProviderConflictV2,
        match="not safely cancellable",
    ):
        store.request_lifecycle_cancellation(
            running.operation.operation_id,
            if_match=running.operation.etag,
            idempotency_key="disconnect-barrier-running-cancel-0001",
        )
    with pytest.raises(
        store_module.ProviderPreconditionFailedV2,
        match="disconnect cannot finish as cancelled",
    ):
        store.finish_lifecycle_operation(
            store_module.LifecycleOperationCompletionV2(
                operation_id=running.operation.operation_id,
                expected_etag=running.operation.etag,
                status="cancelled",
                result=None,
                failure=None,
            )
        )
    assert store.get_lifecycle_operation(disconnect.operation_id) == running.operation
    assert store.get_profile(connected.profile_id) == transitioned
    store.close()


def test_lifecycle_non_cancellable_barrier_cannot_reopen(
    tmp_path: Path,
) -> None:
    store = DesktopProviderStoreV2(tmp_path / "provider-v2", clock=_Clock())
    queued = store.reserve_lifecycle_operation(
        _project_reservation("project-monotonic-cancellation-barrier"),
        idempotency_key="monotonic-barrier-project-create-0001",
    )
    running = store.claim_next_lifecycle_operation()
    assert running is not None
    barrier = store.advance_lifecycle_operation(
        store_module.LifecycleOperationAdvanceV2(
            operation_id=queued.operation_id,
            expected_etag=running.operation.etag,
            phase="activating",
            progress={"kind": "indeterminate"},
            cancellable=False,
        )
    )

    with pytest.raises(
        store_module.ProviderPreconditionFailedV2,
        match="cannot reopen",
    ):
        store.advance_lifecycle_operation(
            store_module.LifecycleOperationAdvanceV2(
                operation_id=queued.operation_id,
                expected_etag=barrier.etag,
                phase="finalizing",
                progress={"kind": "indeterminate"},
                cancellable=True,
            )
        )
    assert store.get_lifecycle_operation(queued.operation_id) == barrier
    store.close()


def test_profile_disconnect_cannot_overtake_dependent_project_work(
    tmp_path: Path,
) -> None:
    store = DesktopProviderStoreV2(tmp_path / "provider-v2", clock=_Clock())
    created = store.create_system_profile(
        _profile(),
        catalog_generation=1,
        idempotency_key="disconnect-owner-profile-create-0001",
    )
    connecting = store.begin_profile_action(
        created.profile_id,
        ProfileConnectionActionV2(
            expected_connection_generation=created.connection_generation
        ),
        action="connect",
        resource_generation=created.connection_generation,
        if_match=created.etag,
        idempotency_key="disconnect-owner-profile-connect-0001",
    )
    connected = store.complete_profile_connection(
        created.profile_id,
        connection_generation=connecting.connection_generation,
        core_version=_core_version(),
    )
    project_id = "project-blocks-profile-disconnect"
    project_request = contract_models.ProjectCreateV2(
        profile_id=connected.profile_id,
        profile_connection_generation=connected.connection_generation,
        display_name="Disconnect owner",
        config=_science_config(),
    )
    project = store.reserve_lifecycle_operation(
        store_module.LifecycleOperationReservationV2(
            kind="project_create",
            resource={"resource_kind": "project", "resource_id": project_id},
            request=store_module.LifecycleProjectCreateRequestV2(
                request_kind="project_create",
                project_id=project_id,
                action_id="disconnect-owner-project-action-0001",
                request=project_request,
                resource_generation=connected.connection_generation,
            ),
        ),
        idempotency_key="disconnect-owner-project-create-0001",
    )

    with pytest.raises(
        store_module.ProviderLifecycleResourceBusyV2,
        match="active lifecycle",
    ):
        store.reserve_lifecycle_operation(
            _disconnect_reservation(connected),
            idempotency_key="disconnect-overtakes-project-0001",
        )
    assert store.get_profile(connected.profile_id) == connected
    assert [item.operation_id for item in store.list_pending_lifecycle_operations()] == [
        project.operation_id
    ]
    store.close()


def test_project_work_cannot_overtake_profile_disconnect(
    tmp_path: Path,
) -> None:
    store = DesktopProviderStoreV2(tmp_path / "provider-v2", clock=_Clock())
    created = store.create_system_profile(
        _profile(),
        catalog_generation=1,
        idempotency_key="project-owner-profile-create-0001",
    )
    connecting = store.begin_profile_action(
        created.profile_id,
        ProfileConnectionActionV2(
            expected_connection_generation=created.connection_generation
        ),
        action="connect",
        resource_generation=created.connection_generation,
        if_match=created.etag,
        idempotency_key="project-owner-profile-connect-0001",
    )
    connected = store.complete_profile_connection(
        created.profile_id,
        connection_generation=connecting.connection_generation,
        core_version=_core_version(),
    )
    disconnect = store.reserve_lifecycle_operation(
        _disconnect_reservation(connected),
        idempotency_key="project-owner-profile-disconnect-0001",
    )
    project_id = "project-overtakes-profile-disconnect"
    project_request = contract_models.ProjectCreateV2(
        profile_id=connected.profile_id,
        profile_connection_generation=connected.connection_generation,
        display_name="Project owner",
        config=_science_config(),
    )

    with pytest.raises(
        store_module.ProviderLifecycleResourceBusyV2,
        match="active lifecycle",
    ):
        store.reserve_lifecycle_operation(
            store_module.LifecycleOperationReservationV2(
                kind="project_create",
                resource={"resource_kind": "project", "resource_id": project_id},
                request=store_module.LifecycleProjectCreateRequestV2(
                    request_kind="project_create",
                    project_id=project_id,
                    action_id="project-overtakes-disconnect-action-0001",
                    request=project_request,
                    resource_generation=connected.connection_generation,
                ),
            ),
            idempotency_key="project-overtakes-profile-disconnect-0001",
        )
    assert [item.operation_id for item in store.list_pending_lifecycle_operations()] == [
        disconnect.operation_id
    ]
    store.close()


def test_recovery_rejects_multiple_active_lifecycle_owners_for_one_resource(
    tmp_path: Path,
) -> None:
    root = tmp_path / "provider-v2"
    store = DesktopProviderStoreV2(root, clock=_Clock())
    first = store.reserve_lifecycle_operation(
        _project_reservation("project-active-first"),
        idempotency_key="active-first-project-create-0001",
    )
    second = store.reserve_lifecycle_operation(
        _project_reservation("project-active-second"),
        idempotency_key="active-second-project-create-0001",
    )
    store.close()

    with sqlite3.connect(root / store_module.DATABASE_FILENAME) as connection:
        first_request = connection.execute(
            "SELECT request_sha256, request_json FROM lifecycle_operations "
            "WHERE operation_id = ?",
            (first.operation_id,),
        ).fetchone()
        assert first_request is not None
        connection.execute(
            "UPDATE lifecycle_operations "
            "SET resource_id = ?, request_sha256 = ?, request_json = ? "
            "WHERE operation_id = ?",
            (
                first.resource.resource_id,
                first_request[0],
                first_request[1],
                second.operation_id,
            ),
        )
        connection.execute(
            "UPDATE lifecycle_idempotency_records SET request_sha256 = ? "
            "WHERE operation_id = ? AND action = 'reserve'",
            (first_request[0], second.operation_id),
        )

    with pytest.raises(
        store_module.ProviderDataV2Error,
        match="multiple lifecycle operations",
    ):
        DesktopProviderStoreV2(root, clock=_Clock())


def test_lifecycle_progress_logs_terminal_retry_and_acknowledgement_are_durable(
    tmp_path: Path,
) -> None:
    clock = _MutableClock()
    store = DesktopProviderStoreV2(tmp_path / "provider-v2", clock=clock)
    queued = store.reserve_lifecycle_operation(
        _project_reservation(),
        idempotency_key="progress-project-create-001",
    )
    work = store.claim_next_lifecycle_operation()
    assert work is not None
    assert work.operation.operation_id == queued.operation_id
    assert work.operation.status == "running"

    transferring = store.advance_lifecycle_operation(
        store_module.LifecycleOperationAdvanceV2(
            operation_id=work.operation.operation_id,
            expected_etag=work.operation.etag,
            phase="transferring",
            progress={"kind": "bytes", "completed": 5, "total": 10},
            cancellable=True,
        )
    )
    with pytest.raises(ProviderPreconditionFailedV2, match="regress"):
        store.advance_lifecycle_operation(
            store_module.LifecycleOperationAdvanceV2(
                operation_id=transferring.operation_id,
                expected_etag=transferring.etag,
                phase="transferring",
                progress={"kind": "bytes", "completed": 4, "total": 10},
                cancellable=True,
            )
        )

    with_log = store.append_lifecycle_log(
        store_module.LifecycleLogAppendV2(
            operation_id=transferring.operation_id,
            source="ssh_stdout",
            text="Preparing remote project\n",
            truncated=False,
        )
    )
    assert with_log.log_sequence_high_watermark == 1
    completion = store_module.LifecycleOperationCompletionV2(
        operation_id=with_log.operation_id,
        expected_etag=with_log.etag,
        status="succeeded",
        result={"result_kind": "project", "project_id": "desktop-project-1"},
        failure=None,
    )
    finished = store.finish_lifecycle_operation(completion)
    assert finished.status == "succeeded"
    assert store.finish_lifecycle_operation(completion) == finished
    with pytest.raises(store_module.ProviderConflictV2, match="terminal"):
        store.append_lifecycle_log(
            store_module.LifecycleLogAppendV2(
                operation_id=finished.operation_id,
                source="daemon_stderr",
                text="late output",
                truncated=False,
            )
        )

    acknowledgement = contract_models.LifecycleAcknowledgeV2(
        expected_operation_id=finished.operation_id,
        expected_terminal_status="succeeded",
    )
    store.acknowledge_lifecycle_operation(
        finished.operation_id,
        acknowledgement,
        if_match=finished.etag,
        idempotency_key="ack-project-create-0001",
    )
    store.acknowledge_lifecycle_operation(
        finished.operation_id,
        acknowledgement,
        if_match=finished.etag,
        idempotency_key="ack-project-create-0001",
    )
    assert store.list_pending_lifecycle_operations() == ()

    clock.advance(timedelta(days=8))
    assert store.reconcile_lifecycle_operations() == ()
    with pytest.raises(store_module.ProviderNotFoundV2):
        store.get_lifecycle_operation(finished.operation_id)
    store.close()


def test_lifecycle_log_pages_are_bounded_signed_and_report_eviction(
    tmp_path: Path,
) -> None:
    store = DesktopProviderStoreV2(
        tmp_path / "provider-v2",
        clock=_Clock(),
        max_lifecycle_log_entries=2,
        max_lifecycle_log_bytes=store_module.MAX_LIFECYCLE_LOG_BYTES,
        max_lifecycle_global_log_bytes=store_module.MAX_LIFECYCLE_GLOBAL_LOG_BYTES,
    )
    queued = store.reserve_lifecycle_operation(
        _project_reservation(),
        idempotency_key="log-project-create-000001",
    )
    work = store.claim_next_lifecycle_operation()
    assert work is not None
    for text in ("first", "second", "third"):
        store.append_lifecycle_log(
            store_module.LifecycleLogAppendV2(
                operation_id=queued.operation_id,
                source="daemon_stdout",
                text=text,
                truncated=False,
            )
        )
    page = store.read_lifecycle_logs(queued.operation_id, limit=1, after=None)
    assert page.dropped_before_sequence == 1
    assert [entry.text for entry in page.items] == ["second"]
    assert page.has_more and page.next_cursor is not None
    with pytest.raises(store_module.ProviderContractV2Error):
        store.read_lifecycle_logs(
            queued.operation_id,
            limit=1,
            after=page.next_cursor + "tampered",
        )

    cursor = page.next_cursor
    store.append_lifecycle_log(
        store_module.LifecycleLogAppendV2(
            operation_id=queued.operation_id,
            source="ssh_stderr",
            text="fourth",
            truncated=False,
        )
    )
    store.append_lifecycle_log(
        store_module.LifecycleLogAppendV2(
            operation_id=queued.operation_id,
            source="ssh_stderr",
            text="fifth",
            truncated=False,
        )
    )
    with pytest.raises(store_module.ProviderCursorExpiredV2):
        store.read_lifecycle_logs(queued.operation_id, limit=1, after=cursor)

    oversized = store.append_lifecycle_log(
        store_module.LifecycleLogAppendV2(
            operation_id=queued.operation_id,
            source="ssh_stdout",
            text="界" * 6_000,
            truncated=False,
        )
    )
    tail = store.read_lifecycle_logs(oversized.operation_id, limit=2, after=None)
    entry = tail.items[-1]
    assert len(entry.text.encode("utf-8")) <= 16 * 1024
    assert entry.truncated is True
    store.close()


def test_lifecycle_recovery_rejects_oversized_log_before_loading_text(
    tmp_path: Path,
) -> None:
    root = tmp_path / "provider-v2"
    store = DesktopProviderStoreV2(root, clock=_Clock())
    queued = store.reserve_lifecycle_operation(
        _project_reservation(),
        idempotency_key="corrupt-log-project-0001",
    )
    store.claim_next_lifecycle_operation()
    store.append_lifecycle_log(
        store_module.LifecycleLogAppendV2(
            operation_id=queued.operation_id,
            source="desktop",
            text="checkpoint",
            truncated=False,
        )
    )
    store.close()

    with sqlite3.connect(root / store_module.DATABASE_FILENAME) as connection:
        connection.execute("PRAGMA ignore_check_constraints = ON")
        connection.execute(
            "UPDATE lifecycle_operation_logs SET text = zeroblob(?)",
            (store_module.MAX_LIFECYCLE_LOG_ENTRY_BYTES + 1,),
        )
    with pytest.raises(store_module.ProviderDataV2Error, match="log"):
        DesktopProviderStoreV2(root, clock=_Clock())
