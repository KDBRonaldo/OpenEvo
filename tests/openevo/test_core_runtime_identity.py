from __future__ import annotations

import json
import os
from pathlib import Path
import threading
import stat
from types import SimpleNamespace

import pytest

from openevo.backend.runtime_identity import (
    HostServiceRoot,
    RuntimeIdentityError,
    compute_release_identity,
    default_core_service_root,
    load_bounded_json,
    load_or_create_core_bearer_token,
    release_runtime_contract_sha256,
    require_host_global_service_root,
    source_development_core_service_root,
)


def test_release_runtime_contract_identity_is_frozen() -> None:
    assert release_runtime_contract_sha256() == (
        "c602442fed4a891f5a37264d2545eaa0f6b4171f1448afea8ae47b8605f247d1"
    )


def test_concurrent_host_root_creation_converges(tmp_path: Path) -> None:
    root_path = tmp_path / "core"
    barrier = threading.Barrier(2)
    failures: list[BaseException] = []

    def create() -> None:
        try:
            barrier.wait()
            with HostServiceRoot(root_path):
                pass
        except BaseException as exc:
            failures.append(exc)

    threads = [threading.Thread(target=create) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert failures == []
    assert root_path.stat().st_mode & 0o777 == 0o700


def test_host_service_root_rejects_symlink_and_insecure_mode(tmp_path: Path) -> None:
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    linked = tmp_path / "linked"
    linked.symlink_to(private, target_is_directory=True)

    with pytest.raises((OSError, RuntimeIdentityError)):
        HostServiceRoot(linked)

    insecure = tmp_path / "insecure"
    insecure.mkdir(mode=0o777)
    insecure.chmod(0o777)
    with pytest.raises(RuntimeIdentityError, match="owner-only"):
        HostServiceRoot(insecure)
    assert stat.S_IMODE(insecure.stat().st_mode) == 0o777


def test_host_global_root_rejects_alternate_per_project_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = default_core_service_root()
    monkeypatch.setenv("HOME", str(tmp_path / "attacker-selected-home"))
    assert default_core_service_root() == expected
    assert require_host_global_service_root(default_core_service_root()) == (
        default_core_service_root()
    )
    assert require_host_global_service_root(source_development_core_service_root()) == (
        source_development_core_service_root()
    )
    with pytest.raises(RuntimeIdentityError, match="canonical"):
        require_host_global_service_root(tmp_path / "project-a" / "core")


def test_host_service_root_rejects_insecure_child_without_chmod(tmp_path: Path) -> None:
    root_path = tmp_path / "core"
    root_path.mkdir(mode=0o700)
    child = root_path / "state"
    child.mkdir(mode=0o777)
    child.chmod(0o777)

    with HostServiceRoot(root_path) as root:
        with pytest.raises(RuntimeIdentityError, match="owner-only"):
            root.ensure_directory("state")
    assert stat.S_IMODE(child.stat().st_mode) == 0o777


@pytest.mark.skipif(os.geteuid() != 0, reason="foreign-owner fixture requires root")
def test_host_service_root_rejects_foreign_owner(tmp_path: Path) -> None:
    root_path = tmp_path / "core"
    root_path.mkdir(mode=0o700)
    os.chown(root_path, 65534, -1)

    with pytest.raises(RuntimeIdentityError, match="owner-only"):
        HostServiceRoot(root_path)


def test_bearer_is_atomic_bounded_and_not_followed(tmp_path: Path) -> None:
    root_path = tmp_path / "core"
    root_path.mkdir(mode=0o700)
    with HostServiceRoot(root_path) as root:
        first = load_or_create_core_bearer_token(root)
        second = load_or_create_core_bearer_token(root)
        assert first == second
        assert len(first) == 64
        assert stat.S_IMODE((root_path / "bearer-token").stat().st_mode) == 0o600

    (root_path / "bearer-token").unlink()
    target = tmp_path / "target"
    target.write_text("x" * 64 + "\n", encoding="ascii")
    (root_path / "bearer-token").symlink_to(target)
    with HostServiceRoot(root_path) as root:
        with pytest.raises(OSError):
            load_or_create_core_bearer_token(root)


def test_bounded_json_rejects_duplicate_keys_and_oversize() -> None:
    with pytest.raises(RuntimeIdentityError, match="duplicate"):
        load_bounded_json(b'{"value":1,"value":2}', max_bytes=100)
    with pytest.raises(RuntimeIdentityError, match="size"):
        load_bounded_json(json.dumps({"value": "x" * 100}).encode(), max_bytes=32)


def test_release_identity_binds_lock_registry_and_install_inventory(tmp_path: Path) -> None:
    lock = tmp_path / "framework-lock.json"
    lock.write_text('{"schema_version":"1"}\n', encoding="ascii")
    attestation = SimpleNamespace(
        expectation=SimpleNamespace(
            distribution="openevo",
            distribution_version="0.1.0",
        ),
        inventory_digest="d" * 64,
    )
    registry = SimpleNamespace(
        snapshot=SimpleNamespace(registry_digest="b" * 64),
        distribution_attestations={"c" * 64: attestation},
    )

    first = compute_release_identity(
        framework_lock=lock,
        registry=registry,
        source_commit="1" * 40,
    )
    attestation.inventory_digest = "e" * 64
    second = compute_release_identity(
        framework_lock=lock,
        registry=registry,
        source_commit="1" * 40,
    )

    assert first.registry_digest == "b" * 64
    assert first.digest != second.digest
