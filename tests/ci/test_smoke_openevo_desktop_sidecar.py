from __future__ import annotations

import importlib.util
from pathlib import Path
import sqlite3
import sys

import pytest

from desktop.sidecar.provider_store_v2 import (
    DATABASE_FILENAME,
    DesktopProviderStoreV2,
    EXPECTED_SCHEMA_V3_SHA256,
)


SCRIPT_PATH = (
    Path(__file__).resolve().parents[2] / "scripts/ci/smoke_openevo_desktop_sidecar.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "smoke_openevo_desktop_sidecar_lifecycle",
        SCRIPT_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_v019_provider_state_migrates_to_v3_without_losing_profile(
    tmp_path: Path,
) -> None:
    smoke = _load_module()
    config_root = tmp_path / "config"

    smoke._prepare_v019_provider_state(config_root)
    provider_root = smoke._provider_state_root(config_root)
    with sqlite3.connect(provider_root / DATABASE_FILENAME) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (2,)

    with DesktopProviderStoreV2(provider_root) as store:
        assert store.schema_fingerprint == EXPECTED_SCHEMA_V3_SHA256
        assert [profile.profile_id for profile in store.list_profiles()] == [
            smoke.SMOKE_V019_PROFILE_ID
        ]


def test_restart_operation_keeps_phase_log_and_exact_replay(
    tmp_path: Path,
) -> None:
    smoke = _load_module()
    config_root = tmp_path / "config"
    smoke._prepare_v019_provider_state(config_root)
    with DesktopProviderStoreV2(smoke._provider_state_root(config_root)):
        pass

    authority = smoke._prime_recoverable_lifecycle(config_root)
    with DesktopProviderStoreV2(smoke._provider_state_root(config_root)) as store:
        operation = store.get_lifecycle_operation(authority.operation_id)
        logs = store.read_lifecycle_logs(authority.operation_id, limit=100, after=None)
        assert operation.status == "running"
        assert operation.phase == "remote_preflight"
        assert [item.source for item in logs.items] == ["ssh_stderr"]
        assert [item.text for item in logs.items] == [smoke.SMOKE_LIFECYCLE_LOG_TEXT]

    replay = smoke._verify_exact_lifecycle_replay(config_root, authority)
    assert replay.operation_id == authority.operation_id
    assert replay.request_sha256 == authority.request_sha256


def test_recovered_lifecycle_http_evidence_fails_closed_on_identity_change() -> None:
    smoke = _load_module()
    authority = smoke._LifecycleSmokeAuthority(
        operation_id="operation-smoke-0001",
        request_sha256="a" * 64,
    )
    operation = {
        "operation_id": authority.operation_id,
        "request_sha256": authority.request_sha256,
        "kind": "project_create",
        "status": "failed",
        "phase": "remote_preflight",
    }
    logs = {
        "operation_id": authority.operation_id,
        "items": [
            {
                "operation_id": authority.operation_id,
                "sequence": 1,
                "source": "ssh_stderr",
                "text": smoke.SMOKE_LIFECYCLE_LOG_TEXT,
                "truncated": False,
            }
        ],
    }

    smoke._assert_recovered_lifecycle_http(operation, logs, authority)
    operation["operation_id"] = "operation-smoke-0002"
    with pytest.raises(smoke.SmokeFailure, match="identity changed"):
        smoke._assert_recovered_lifecycle_http(operation, logs, authority)


def test_native_restart_uses_distinct_immutable_launch_copies(tmp_path: Path) -> None:
    smoke = _load_module()
    source = tmp_path / "packaged-sidecar"
    source.write_bytes(b"packaged-sidecar-bytes")
    source.chmod(0o755)

    first = smoke._stage_native_launch_copy(
        source,
        parent=tmp_path,
        basename=smoke.NATIVE_SIDECAR_BASENAME,
        mode=0o500,
    )
    second = smoke._stage_native_launch_copy(
        source,
        parent=tmp_path,
        basename=smoke.NATIVE_SIDECAR_BASENAME,
        mode=0o500,
    )

    assert first != second
    assert first.name == smoke.NATIVE_SIDECAR_BASENAME
    assert second.name == smoke.NATIVE_SIDECAR_BASENAME
    assert first.parent != second.parent
    assert first.read_bytes() == source.read_bytes()
    assert second.read_bytes() == source.read_bytes()
    assert first.stat().st_mode & 0o777 == 0o500
    assert second.stat().st_mode & 0o777 == 0o500
