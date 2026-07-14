from __future__ import annotations

import os
from pathlib import Path

import pytest

from openevo.gateway import session_files
from openevo.gateway.session_files import (
    SessionFileSecurityError,
    capture_session_root_identity,
    remove_session_tree,
    stage_codex_subscription_auth,
)


def _private_auth(tmp_path: Path, content: str = '{"subscription": true}\n') -> Path:
    source = tmp_path / "home" / ".codex" / "auth.json"
    source.parent.mkdir(parents=True)
    source.write_text(content, encoding="utf-8")
    source.chmod(0o600)
    return source


def _session_root(tmp_path: Path) -> tuple[Path, tuple[int, int, int]]:
    root = tmp_path / "session"
    root.mkdir(mode=0o700)
    return root, capture_session_root_identity(root)


def _stage(source: Path, root: Path, identity: tuple[int, int, int]) -> Path:
    stage_codex_subscription_auth(
        source=source,
        session_dir=root,
        session_identity=identity,
        target_home_parts=("home", ".codex"),
    )
    return root / "home" / ".codex" / "auth.json"


def test_auth_staging_uses_private_owned_files_and_directories(tmp_path: Path) -> None:
    source = _private_auth(tmp_path)
    root, identity = _session_root(tmp_path)

    target = _stage(source, root, identity)

    assert target.read_bytes() == source.read_bytes()
    assert target.stat().st_mode & 0o777 == 0o600
    assert target.stat().st_nlink == 1
    assert target.stat().st_uid == os.geteuid()
    assert target.parent.stat().st_mode & 0o777 == 0o700
    assert target.parent.parent.stat().st_mode & 0o777 == 0o700


def test_auth_staging_rejects_symlink_source(tmp_path: Path) -> None:
    real_source = _private_auth(tmp_path)
    source = real_source.with_name("linked-auth.json")
    source.symlink_to(real_source)
    root, identity = _session_root(tmp_path)

    with pytest.raises(SessionFileSecurityError, match="regular file"):
        _stage(source, root, identity)

    assert not (root / "home" / ".codex" / "auth.json").exists()


def test_auth_staging_rejects_hardlink_source(tmp_path: Path) -> None:
    source = _private_auth(tmp_path)
    os.link(source, source.with_name("second-link.json"))
    root, identity = _session_root(tmp_path)

    with pytest.raises(SessionFileSecurityError, match="hard links"):
        _stage(source, root, identity)


def test_auth_staging_rejects_non_regular_source(tmp_path: Path) -> None:
    source = tmp_path / "home" / ".codex" / "auth.json"
    source.parent.mkdir(parents=True)
    os.mkfifo(source, mode=0o600)
    root, identity = _session_root(tmp_path)

    with pytest.raises(SessionFileSecurityError, match="regular file"):
        _stage(source, root, identity)


def test_auth_staging_rejects_owner_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _private_auth(tmp_path)
    root, identity = _session_root(tmp_path)
    monkeypatch.setattr(session_files.os, "geteuid", lambda: identity[2] + 1)

    with pytest.raises(SessionFileSecurityError, match="Core service user"):
        _stage(source, root, identity)


def test_auth_staging_rejects_group_or_world_readable_source(tmp_path: Path) -> None:
    source = _private_auth(tmp_path)
    source.chmod(0o644)
    root, identity = _session_root(tmp_path)

    with pytest.raises(SessionFileSecurityError, match="private and owner-readable"):
        _stage(source, root, identity)


def test_auth_staging_exclusively_rejects_existing_target_symlink(
    tmp_path: Path,
) -> None:
    source = _private_auth(tmp_path)
    root, identity = _session_root(tmp_path)
    target = root / "home" / ".codex" / "auth.json"
    target.parent.mkdir(parents=True, mode=0o700)
    external = tmp_path / "external-auth.json"
    external.write_text("keep", encoding="utf-8")
    target.symlink_to(external)

    with pytest.raises(SessionFileSecurityError, match="could not be staged safely"):
        _stage(source, root, identity)

    assert target.is_symlink()
    assert external.read_text(encoding="utf-8") == "keep"


def test_auth_staging_detects_source_path_exchange_and_removes_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "private-session-secret"
    source = _private_auth(tmp_path, secret)
    root, identity = _session_root(tmp_path)
    original_copy = session_files._copy_exact

    def exchange_then_copy(source_fd: int, target_fd: int, expected_size: int) -> None:
        source.unlink()
        source.write_text("replacement", encoding="utf-8")
        source.chmod(0o600)
        original_copy(source_fd, target_fd, expected_size)

    monkeypatch.setattr(session_files, "_copy_exact", exchange_then_copy)

    with pytest.raises(SessionFileSecurityError, match="changed"):
        _stage(source, root, identity)

    assert not (root / "home" / ".codex" / "auth.json").exists()


def test_auth_staging_detects_target_replacement_without_leaking_secret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "private-session-secret"
    source = _private_auth(tmp_path, secret)
    root, identity = _session_root(tmp_path)
    target = root / "home" / ".codex" / "auth.json"
    original_fsync = session_files.os.fsync

    def replace_target(descriptor: int) -> None:
        original_fsync(descriptor)
        target.unlink()
        target.write_text("replacement", encoding="utf-8")
        target.chmod(0o600)

    monkeypatch.setattr(session_files.os, "fsync", replace_target)

    with pytest.raises(SessionFileSecurityError, match="path changed"):
        _stage(source, root, identity)

    assert target.read_text(encoding="utf-8") == "replacement"
    assert secret not in target.read_text(encoding="utf-8")


def test_cleanup_recovers_nested_zero_modes_and_removes_staged_auth(tmp_path: Path) -> None:
    source = _private_auth(tmp_path)
    root, identity = _session_root(tmp_path)
    target = _stage(source, root, identity)
    nested = root / "locked" / "deeper"
    nested.mkdir(parents=True)
    (nested / "result.txt").write_text("done", encoding="utf-8")
    nested.chmod(0)
    nested.parent.chmod(0)
    target.parent.chmod(0)
    target.parent.parent.chmod(0)
    root.chmod(0)

    remove_session_tree(root, identity)

    assert not root.exists()


def test_cleanup_does_not_follow_symlink_swapped_for_nested_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, identity = _session_root(tmp_path)
    nested = root / "nested"
    nested.mkdir()
    (nested / "owned.txt").write_text("owned", encoding="utf-8")
    external = tmp_path / "external"
    external.mkdir()
    marker = external / "keep.txt"
    marker.write_text("keep", encoding="utf-8")
    moved = root / "moved"
    original_require = session_files._require_named_identity
    swapped = False

    def swap_before_identity_check(
        directory_fd: int,
        name: str,
        expected: tuple[int, int],
        *,
        label: str,
        expected_owner: int,
    ) -> None:
        nonlocal swapped
        if name == "nested" and label == "session directory" and not swapped:
            nested.rename(moved)
            nested.symlink_to(external, target_is_directory=True)
            swapped = True
        original_require(
            directory_fd,
            name,
            expected,
            label=label,
            expected_owner=expected_owner,
        )

    monkeypatch.setattr(
        session_files,
        "_require_named_identity",
        swap_before_identity_check,
    )

    with pytest.raises(SessionFileSecurityError, match="identity changed"):
        remove_session_tree(root, identity)

    assert marker.read_text(encoding="utf-8") == "keep"
    assert nested.is_symlink()


def test_cleanup_rejects_replacement_session_root(tmp_path: Path) -> None:
    source = _private_auth(tmp_path)
    root, identity = _session_root(tmp_path)
    original_auth = _stage(source, root, identity)
    moved = tmp_path / "original-session"
    root.rename(moved)
    root.mkdir()
    replacement = root / "replacement.txt"
    replacement.write_text("keep", encoding="utf-8")

    with pytest.raises(SessionFileSecurityError, match="identity"):
        remove_session_tree(root, identity)

    assert replacement.read_text(encoding="utf-8") == "keep"
    assert (moved / original_auth.relative_to(root)).is_file()


def test_cleanup_rejects_foreign_owned_entry_before_unlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, identity = _session_root(tmp_path)
    foreign = root / "foreign"
    foreign.write_text("keep", encoding="utf-8")
    original_stat = session_files.os.stat

    def foreign_owner(path, *args, **kwargs):
        value = original_stat(path, *args, **kwargs)
        if path == "foreign" and kwargs.get("dir_fd") is not None:
            values = list(value)
            values[4] = identity[2] + 1
            return os.stat_result(values)
        return value

    monkeypatch.setattr(session_files.os, "stat", foreign_owner)

    with pytest.raises(SessionFileSecurityError, match="owned by another user"):
        remove_session_tree(root, identity)

    assert foreign.is_file()


def test_cleanup_enforces_node_budget(tmp_path: Path) -> None:
    root, identity = _session_root(tmp_path)
    (root / "one").write_text("1", encoding="utf-8")
    (root / "two").write_text("2", encoding="utf-8")

    with pytest.raises(SessionFileSecurityError, match="node limit"):
        remove_session_tree(root, identity, max_nodes=1)
