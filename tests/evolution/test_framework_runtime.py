from __future__ import annotations

import json
from pathlib import Path

import pytest

from openevo.evolution.framework import runtime
from openevo.evolution.framework.runtime import (
    FrameworkDistributionLock,
    load_framework_distribution_lock,
    load_verified_framework_registry,
)


def _payload(**updates: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": "1",
        "distribution": "openevo",
        "distribution_version": "0.1.0",
        "distribution_digest": "a" * 64,
        "wheel_filename": "openevo-0.1.0-py3-none-any.whl",
    }
    payload.update(updates)
    return payload


def _write_lock(tmp_path: Path, **updates: object) -> Path:
    path = tmp_path / "framework-lock.json"
    path.write_text(json.dumps(_payload(**updates)), encoding="utf-8")
    return path


def test_framework_lock_resolves_only_a_sibling_wheel(tmp_path: Path) -> None:
    lock_path = _write_lock(tmp_path)

    lock, wheel = load_framework_distribution_lock(lock_path)

    assert lock == FrameworkDistributionLock.model_validate(_payload())
    assert wheel == tmp_path.resolve() / "openevo-0.1.0-py3-none-any.whl"


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"wheel_filename": "../openevo-0.1.0.whl"}, "safe wheel basename"),
        ({"wheel_filename": "openevo-9.9.9.whl"}, "locked version"),
        ({"distribution_digest": "not-a-digest"}, "SHA-256"),
        ({"distribution": "other"}, "openevo"),
    ],
)
def test_framework_lock_rejects_unsafe_or_mismatched_identity(
    tmp_path: Path,
    updates: dict[str, object],
    message: str,
) -> None:
    lock_path = _write_lock(tmp_path, **updates)

    with pytest.raises(ValueError, match=message):
        load_framework_distribution_lock(lock_path)


def test_framework_lock_rejects_symlink_and_oversized_input(tmp_path: Path) -> None:
    target = _write_lock(tmp_path)
    link = tmp_path / "linked-lock.json"
    link.symlink_to(target)
    with pytest.raises(ValueError, match="non-symlink"):
        load_framework_distribution_lock(link)

    target.write_bytes(b" " * (64 * 1024 + 1))
    with pytest.raises(ValueError, match="size limit"):
        load_framework_distribution_lock(target)


def test_verified_runtime_uses_lock_identity_for_verification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock_path = _write_lock(tmp_path)
    wheel = tmp_path / "openevo-0.1.0-py3-none-any.whl"
    wheel.write_bytes(b"wheel")
    calls: dict[str, object] = {}
    verified = object()
    executable = object()

    def fake_verify(expectation, artifact_path):
        calls["expectation"] = expectation
        calls["artifact_path"] = artifact_path
        return verified

    def fake_load(value):
        calls["verified"] = value
        return executable

    monkeypatch.setattr(runtime, "verify_distribution_install", fake_verify)
    monkeypatch.setattr(runtime, "load_verified_builtin_registry", fake_load)

    assert load_verified_framework_registry(lock_path) is executable
    expectation = calls["expectation"]
    assert expectation.distribution == "openevo"
    assert expectation.distribution_version == "0.1.0"
    assert expectation.distribution_digest == "a" * 64
    assert calls["artifact_path"] == wheel
    assert calls["verified"] is verified
