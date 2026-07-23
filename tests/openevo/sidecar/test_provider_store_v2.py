from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3

import pytest

from desktop.sidecar.contracts.v2.models import (
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
from openevo.backend.contracts.v2.models import ScienceProjectConfigV2


class _Clock:
    def __init__(self) -> None:
        self._next = datetime(2026, 7, 23, 4, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        value = self._next
        self._next += timedelta(microseconds=1)
        return value


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


def test_release_state_uses_a_separate_v2_namespace_and_exact_schema(
    tmp_path: Path,
) -> None:
    assert store_module.EXPECTED_SCHEMA_V1_SHA256 == (
        "d2ae490ad5b98ca03548570a8d56a6a5ea349694ed647102a69eb5b69e3dac34"
    )
    assert store_module.EXPECTED_SCHEMA_V2_SHA256 == (
        "7314032a52da83b70a43f36f161984bef8bf03274848bf62ab1963a039279c06"
    )
    base = tmp_path / "state-v2"
    runtime = create_release_local_state_v2(base, clock=_Clock())
    try:
        store = runtime.provider_store
        assert store.state_root == base / "provider-v2"
        assert store.database_path == base / "provider-v2" / "provider-v2.sqlite3"
        assert not (base / "provider.sqlite3").exists()
        assert store.schema_fingerprint == store_module.EXPECTED_SCHEMA_V2_SHA256
        assert store.list_profiles() == ()
        assert runtime.legacy_import.profiles == ()
        assert runtime.legacy_import.diagnostics == ()

        with sqlite3.connect(store.database_path) as connection:
            assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
            row = connection.execute(
                "SELECT namespace, schema_version, schema_sha256 FROM schema_metadata"
            ).fetchone()
            assert row == (
                "openevo.desktop.provider.v2",
                2,
                store_module.EXPECTED_SCHEMA_V2_SHA256,
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
        assert store.schema_fingerprint == store_module.EXPECTED_SCHEMA_V2_SHA256
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
