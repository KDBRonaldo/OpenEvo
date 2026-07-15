from __future__ import annotations

import os
from pathlib import Path

import pytest

from openevo.gateway import session_files
from openevo.gateway.session_files import (
    CODEX_CREDENTIAL_SNAPSHOT_FD_ENV,
    HeldCodexCredentialAuthority,
    PreparedCodexCredentialSnapshot,
    SessionFileSecurityError,
    capture_session_root_identity,
    load_staged_codex_subscription_redactor,
    remove_credential_tree,
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


def test_held_credential_authority_rejects_atomic_auth_replacement(
    tmp_path: Path,
) -> None:
    source = _private_auth(tmp_path, '{"access_token":"readiness-secret"}\n')
    authority = HeldCodexCredentialAuthority.open(source)
    replacement = source.with_name("auth.replacement")
    replacement.write_text('{"access_token":"replacement-secret"}\n', encoding="utf-8")
    replacement.chmod(0o600)
    os.replace(replacement, source)
    root, identity = _session_root(tmp_path)
    try:
        with pytest.raises(SessionFileSecurityError, match="changed|readiness authority"):
            stage_codex_subscription_auth(
                source=source,
                source_authority=authority,
                session_dir=root,
                session_identity=identity,
                target_home_parts=("home", ".codex"),
            )
        assert not (root / "home" / ".codex" / "auth.json").exists()
        assert list(root.glob(".openevo-credential-staging-*")) == []
    finally:
        authority.close()
        authority.close()


def test_committed_snapshot_survives_later_auth_path_replacement(tmp_path: Path) -> None:
    original = '{"access_token":"readiness-secret"}\n'
    source = _private_auth(tmp_path, original)
    authority = HeldCodexCredentialAuthority.open(source)
    snapshot = authority.prepare_snapshot()
    replacement = source.with_name("auth.replacement")
    replacement.write_text('{"access_token":"replacement-secret"}\n', encoding="utf-8")
    replacement.chmod(0o600)
    os.replace(replacement, source)
    root, identity = _session_root(tmp_path)
    try:
        stage_codex_subscription_auth(
            source=source,
            prepared_snapshot=snapshot,
            session_dir=root,
            session_identity=identity,
            target_home_parts=("home", ".codex"),
        )
        assert (root / "home" / ".codex" / "auth.json").read_text() == original
        with pytest.raises(SessionFileSecurityError, match="changed"):
            authority.verify()
    finally:
        snapshot.close()
        authority.close()


def test_sealed_snapshot_inheritance_preserves_cloexec_and_exact_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = b'{"access_token":"inherited-readiness-secret"}\n'
    source = _private_auth(tmp_path, original.decode("utf-8"))
    authority = HeldCodexCredentialAuthority.open(source)
    snapshot = authority.prepare_snapshot()
    inherited_fd = os.dup(snapshot.inheritance_descriptor())
    monkeypatch.setenv(CODEX_CREDENTIAL_SNAPSHOT_FD_ENV, str(inherited_fd))
    try:
        inherited = PreparedCodexCredentialSnapshot.from_inherited_environment(
            required=True
        )
        assert inherited is not None
        assert os.get_inheritable(inherited.inheritance_descriptor()) is False
        clone = inherited.prepare_snapshot()
        descriptor = clone.duplicate_verified_descriptor()
        try:
            assert os.pread(descriptor, clone.size, 0) == original
            assert os.get_inheritable(descriptor) is False
        finally:
            os.close(descriptor)
            clone.close()
            inherited.close()
    finally:
        snapshot.close()
        authority.close()


def test_log_writer_and_reader_share_private_pinned_authority(tmp_path: Path) -> None:
    root, identity = _session_root(tmp_path)

    path = session_files.write_verified_session_log(
        root,
        identity,
        directory_parts=("logs", "agent"),
        leaf_name="step.00.stdout.log",
        content="verified transcript\n",
    )
    transcript = session_files.read_verified_session_transcript(
        root,
        identity,
        step_index=0,
        require_private_root=True,
    )

    opened = path.stat(follow_symlinks=False)
    assert opened.st_mode & 0o777 == 0o600
    assert opened.st_nlink == 1
    assert transcript.content == b"verified transcript\n"


def test_log_writer_rejects_nonprivate_authority_root(tmp_path: Path) -> None:
    root, identity = _session_root(tmp_path)
    root.chmod(0o755)

    with pytest.raises(SessionFileSecurityError, match="root is not private"):
        session_files.write_verified_session_log(
            root,
            identity,
            directory_parts=("logs", "agent"),
            leaf_name="step.00.stdout.log",
            content="must not publish",
        )

    assert not (root / "logs").exists()


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
    secret = '{"access_token":"private-session-secret"}'
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


def test_auth_staging_detects_source_ancestor_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _private_auth(tmp_path, '{"access_token":"source-secret"}')
    source_home = tmp_path / "home"
    displaced_home = tmp_path / "displaced-home"
    root, identity = _session_root(tmp_path)
    original_copy = session_files._copy_exact

    def replace_ancestor_then_copy(
        source_fd: int,
        target_fd: int,
        expected_size: int,
    ) -> None:
        source_home.rename(displaced_home)
        replacement = source
        replacement.parent.mkdir(parents=True)
        replacement.write_text('{"access_token":"replacement"}', encoding="utf-8")
        replacement.chmod(0o600)
        original_copy(source_fd, target_fd, expected_size)

    monkeypatch.setattr(session_files, "_copy_exact", replace_ancestor_then_copy)

    with pytest.raises(SessionFileSecurityError, match="ancestor.*changed"):
        _stage(source, root, identity)


def test_auth_staging_detects_credential_root_ancestor_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _private_auth(tmp_path, '{"access_token":"target-secret"}')
    anchor = tmp_path / "credential-anchor"
    root = anchor / "session"
    root.mkdir(parents=True, mode=0o700)
    identity = capture_session_root_identity(root)
    displaced_anchor = tmp_path / "displaced-credential-anchor"
    original_copy = session_files._copy_exact
    copy_count = 0

    def replace_ancestor_then_copy(
        source_fd: int,
        target_fd: int,
        expected_size: int,
    ) -> None:
        nonlocal copy_count
        copy_count += 1
        if copy_count == 2:
            anchor.rename(displaced_anchor)
            root.mkdir(parents=True, mode=0o700)
        original_copy(source_fd, target_fd, expected_size)

    monkeypatch.setattr(session_files, "_copy_exact", replace_ancestor_then_copy)

    with pytest.raises(SessionFileSecurityError, match="ancestor.*changed"):
        _stage(source, root, identity)


def test_auth_staging_detects_target_replacement_without_leaking_secret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = '{"access_token":"private-session-secret"}'
    source = _private_auth(tmp_path, secret)
    root, identity = _session_root(tmp_path)
    target = root / "home" / ".codex" / "auth.json"
    original_publish = session_files._rename_noreplace

    def publish_then_replace(*args, **kwargs) -> None:
        original_publish(*args, **kwargs)
        target.unlink()
        target.write_text("replacement", encoding="utf-8")
        target.chmod(0o600)

    monkeypatch.setattr(session_files, "_rename_noreplace", publish_then_replace)

    with pytest.raises(SessionFileSecurityError, match="path changed"):
        _stage(source, root, identity)

    assert target.read_text(encoding="utf-8") == "replacement"
    assert secret not in target.read_text(encoding="utf-8")


def test_auth_snapshot_final_authority_recheck_detects_new_source_hardlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = '{"access_token":"canary-secret"}\n'
    source = _private_auth(tmp_path, secret)
    root, identity = _session_root(tmp_path)
    raced_link = source.with_name("raced-source-link.json")
    authority = HeldCodexCredentialAuthority.open(source)
    original_verify = HeldCodexCredentialAuthority.verify
    verify_count = 0

    def add_hardlink_before_final_verify(
        candidate: HeldCodexCredentialAuthority,
    ) -> None:
        nonlocal verify_count
        verify_count += 1
        if candidate is authority and verify_count == 3:
            os.link(source, raced_link)
        original_verify(candidate)

    monkeypatch.setattr(
        HeldCodexCredentialAuthority,
        "verify",
        add_hardlink_before_final_verify,
    )
    try:
        with pytest.raises(SessionFileSecurityError, match="changed"):
            stage_codex_subscription_auth(
                source=source,
                source_authority=authority,
                session_dir=root,
                session_identity=identity,
                target_home_parts=("home", ".codex"),
            )
    finally:
        authority.close()

    assert not (root / "home" / ".codex" / "auth.json").exists()


def test_auth_staging_final_path_recheck_detects_new_target_hardlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = '{"access_token":"canary-secret"}\n'
    source = _private_auth(tmp_path, secret)
    root, identity = _session_root(tmp_path)
    target = root / "home" / ".codex" / "auth.json"
    raced_link = target.with_name("raced-target-link.json")
    original_require = session_files._require_path_identity
    raced = False

    def add_hardlink_before_recheck(
        directory_fd: int,
        name: str,
        expected: os.stat_result,
        *,
        label: str,
    ) -> None:
        nonlocal raced
        is_selected = label == "staged Codex auth" and target.exists()
        if is_selected and not raced:
            raced = True
            os.link(
                name,
                raced_link.name,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
                follow_symlinks=False,
            )
        original_require(directory_fd, name, expected, label=label)

    monkeypatch.setattr(
        session_files,
        "_require_path_identity",
        add_hardlink_before_recheck,
    )

    with pytest.raises(SessionFileSecurityError, match="path changed"):
        _stage(source, root, identity)

    assert not target.exists()
    assert secret not in raced_link.read_text(encoding="utf-8")


def test_auth_staging_rejects_same_size_target_content_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _private_auth(tmp_path, '{"access_token":"canary-secret"}\n')
    root, identity = _session_root(tmp_path)

    def copy_wrong_bytes(source_fd: int, target_fd: int, expected_size: int) -> None:
        del source_fd
        os.write(target_fd, b"x" * expected_size)

    monkeypatch.setattr(session_files, "_copy_exact", copy_wrong_bytes)

    with pytest.raises(SessionFileSecurityError, match="digest"):
        _stage(source, root, identity)


def test_invalid_auth_is_never_visible_at_final_credential_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _private_auth(tmp_path, '{"access_token":"invalid-canary"}\n')
    root, identity = _session_root(tmp_path)
    target = root / "home" / ".codex" / "auth.json"
    visibility_during_validation: list[bool] = []

    def reject_auth(cls, auth_bytes: bytes):
        del cls, auth_bytes
        visibility_during_validation.append(target.exists())
        raise SessionFileSecurityError("invalid credential probe")

    monkeypatch.setattr(
        session_files.CredentialRedactor,
        "from_auth_json",
        classmethod(reject_auth),
    )

    with pytest.raises(SessionFileSecurityError, match="invalid credential probe"):
        _stage(source, root, identity)

    assert visibility_during_validation == [False]
    assert not target.exists()


def test_auth_publication_fails_closed_without_atomic_noreplace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _private_auth(tmp_path, '{"access_token":"validated-canary"}\n')
    root, identity = _session_root(tmp_path)
    target = root / "home" / ".codex" / "auth.json"

    def unavailable(*args, **kwargs) -> None:
        del args, kwargs
        raise OSError("renameat2 unavailable")

    monkeypatch.setattr(session_files, "_rename_noreplace", unavailable)

    with pytest.raises(SessionFileSecurityError, match="could not be staged safely"):
        _stage(source, root, identity)

    assert not target.exists()
    assert list(tmp_path.glob(".openevo-credential-staging-*")) == []


def test_published_auth_survives_recoverable_staging_cleanup_fault(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _private_auth(tmp_path, '{"access_token":"published-canary"}\n')
    root, identity = _session_root(tmp_path)

    def fail_cleanup(*args, **kwargs) -> None:
        del args, kwargs
        raise OSError("injected staging cleanup failure")

    monkeypatch.setattr(session_files, "_remove_pinned_private_staging", fail_cleanup)

    staged = stage_codex_subscription_auth(
        source=source,
        session_dir=root,
        session_identity=identity,
        target_home_parts=("home", ".codex"),
    )

    target = root / "home" / ".codex" / "auth.json"
    assert target.read_bytes() == source.read_bytes()
    assert staged.redactor.redact("published-canary") != "published-canary"
    staging_roots = list(root.glob(".openevo-credential-staging-*"))
    assert len(staging_roots) == 1
    assert list(staging_roots[0].iterdir()) == []


def test_credential_redactor_redacts_auth_json_and_nested_sensitive_leaves() -> None:
    auth = (
        b'{"tokens":{"access_token":"access-canary",'
        b'"refresh_token":"refresh-canary"},"account_id":"account-canary"}'
    )
    redactor = session_files.CredentialRedactor.from_auth_json(auth)
    captured = (
        f"raw={auth.decode()} access=access-canary refresh=refresh-canary "
        "account=account-canary safe=visible"
    )

    redacted = redactor.redact(captured)

    for canary in (
        auth.decode(),
        "access-canary",
        "refresh-canary",
        "account-canary",
    ):
        assert canary not in redacted
    assert "safe=visible" in redacted


def test_credential_redactor_fails_closed_for_oversized_capture() -> None:
    redactor = session_files.CredentialRedactor.from_auth_json(b'{"access_token":"access-canary"}')

    redacted = redactor.redact("x" * (session_files._CAPTURE_REDACTION_MAX_BYTES + 1))

    assert redacted == session_files.CAPTURE_REDACTION_LIMIT_MARKER


def test_core_capture_tree_redaction_covers_owned_logs(
    tmp_path: Path,
) -> None:
    auth = b'{"access_token":"access-canary","account_id":"account-canary"}'
    redactor = session_files.CredentialRedactor.from_auth_json(auth)
    root, identity = _session_root(tmp_path)
    for relative in (
        "logs/postrun.log",
        "logs/agent/step.00.stdout.log",
    ):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"{auth.decode()} access-canary account-canary visible",
            encoding="utf-8",
        )

    session_files.redact_core_capture_tree(root, identity, redactor)

    persisted = "\n".join(
        path.read_text(encoding="utf-8") for path in root.rglob("*") if path.is_file()
    )
    assert "access-canary" not in persisted
    assert "account-canary" not in persisted
    assert auth.decode() not in persisted
    assert "visible" in persisted


def test_capture_tree_redaction_rejects_oversized_file_without_changing_bytes(
    tmp_path: Path,
) -> None:
    redactor = session_files.CredentialRedactor.from_auth_json(b'{"access_token":"access-canary"}')
    root, identity = _session_root(tmp_path)
    capture = root / "workspace" / "large.bin"
    capture.parent.mkdir()
    original = b"access-canary\n" + b"x" * session_files._CAPTURE_REDACTION_MAX_BYTES
    capture.write_bytes(original)

    with pytest.raises(SessionFileSecurityError, match="per-file byte limit"):
        session_files.redact_core_capture_tree(root, identity, redactor)

    assert capture.read_bytes() == original


def test_capture_tree_redaction_preflights_total_limit_before_changing_any_file(
    tmp_path: Path,
) -> None:
    redactor = session_files.CredentialRedactor.from_auth_json(b'{"access_token":"access-canary"}')
    root, identity = _session_root(tmp_path)
    first = root / "logs" / "first.log"
    second = root / "logs" / "second.log"
    first.parent.mkdir()
    first.write_bytes(b"access-canary-first")
    second.write_bytes(b"access-canary-second")
    originals = (first.read_bytes(), second.read_bytes())

    with pytest.raises(SessionFileSecurityError, match="total byte limit"):
        session_files.redact_core_capture_tree(
            root,
            identity,
            redactor,
            max_total_bytes=len(originals[0]),
        )

    assert (first.read_bytes(), second.read_bytes()) == originals


def test_capture_tree_redaction_rechecks_recursive_directory_path_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redactor = session_files.CredentialRedactor.from_auth_json(b'{"access_token":"access-canary"}')
    root, identity = _session_root(tmp_path)
    nested = root / "workspace" / "nested"
    nested.mkdir(parents=True)
    (nested / "capture.txt").write_text("access-canary", encoding="utf-8")
    displaced = root / "workspace" / "displaced"
    original_replace = session_files._replace_fd_contents
    replaced = False

    def replace_directory_after_file_redaction(descriptor: int, value: bytes) -> None:
        nonlocal replaced
        original_replace(descriptor, value)
        if not replaced:
            replaced = True
            nested.rename(displaced)
            nested.mkdir()
            (nested / "attacker.txt").write_text("access-canary", encoding="utf-8")

    monkeypatch.setattr(
        session_files,
        "_replace_fd_contents",
        replace_directory_after_file_redaction,
    )

    with pytest.raises(SessionFileSecurityError, match="directory path changed"):
        session_files.redact_core_capture_tree(root, identity, redactor)

    assert (nested / "attacker.txt").read_text(encoding="utf-8") == "access-canary"


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


def test_credential_cleanup_scrubs_bound_auth_before_node_budget_exhaustion(
    tmp_path: Path,
) -> None:
    root, identity = _session_root(tmp_path)
    auth = root / "auth.json"
    auth.write_text('{"access_token":"cleanup-budget-canary"}\n', encoding="utf-8")
    auth.chmod(0o600)
    auth_state = auth.stat(follow_symlinks=False)
    auth_identity = session_files._auth_identity(auth_state)
    held_auth = os.open(auth, os.O_RDONLY | os.O_CLOEXEC)
    moved_auth = root / "renamed-secret.json"
    auth.rename(moved_auth)
    auth.write_text('{"access_token":"replacement-canary"}\n', encoding="utf-8")
    auth.chmod(0o600)
    for name in ("000-attacker", "001-attacker"):
        directory = root / name
        directory.mkdir()
        (directory / "entry").write_text("budget", encoding="utf-8")

    try:
        with pytest.raises(SessionFileSecurityError, match="node limit"):
            remove_credential_tree(root, identity, auth_identity, max_nodes=1)

        assert moved_auth.stat(follow_symlinks=False).st_size == 0
        assert os.pread(held_auth, 1, 0) == b""
        assert auth.read_text(encoding="utf-8") == '{"access_token":"replacement-canary"}\n'
        assert root.exists()
    finally:
        os.close(held_auth)


def test_credential_cleanup_recursively_scrubs_nested_bound_auth_inode(
    tmp_path: Path,
) -> None:
    root, identity = _session_root(tmp_path)
    auth = root / "auth.json"
    auth.write_text('{"access_token":"nested-cleanup-canary"}\n', encoding="utf-8")
    auth.chmod(0o600)
    auth_identity = session_files._auth_identity(auth.stat(follow_symlinks=False))
    held_auth = os.open(auth, os.O_RDONLY | os.O_CLOEXEC)
    nested = root / "moved" / "twice"
    nested.mkdir(parents=True)
    auth.rename(nested / "renamed-auth.json")
    auth.write_text('{"access_token":"replacement-canary"}\n', encoding="utf-8")
    auth.chmod(0o600)

    try:
        remove_credential_tree(root, identity, auth_identity)

        assert not root.exists()
        assert os.pread(held_auth, 1, 0) == b""
    finally:
        os.close(held_auth)


def test_credential_cleanup_without_journal_identity_scrubs_handoff_inode(
    tmp_path: Path,
) -> None:
    root, identity = _session_root(tmp_path)
    staging = root / ".openevo-credential-staging-crash"
    staging.mkdir(mode=0o700)
    auth = staging / "auth.json"
    auth.write_text('{"access_token":"handoff-cleanup-canary"}\n', encoding="utf-8")
    auth.chmod(0o600)
    held_auth = os.open(auth, os.O_RDONLY | os.O_CLOEXEC)

    try:
        remove_credential_tree(root, identity, None)

        assert not root.exists()
        assert os.pread(held_auth, 1, 0) == b""
    finally:
        os.close(held_auth)


def test_credential_auth_scan_budget_failure_keeps_nested_inode_linked(
    tmp_path: Path,
) -> None:
    root, identity = _session_root(tmp_path)
    blocker = root / "000-blocker"
    blocker.mkdir()
    nested = root / "zzz-secret" / "deeper"
    nested.mkdir(parents=True)
    auth = nested / "renamed-auth.json"
    auth.write_text('{"access_token":"bounded-cleanup-canary"}\n', encoding="utf-8")
    auth.chmod(0o600)
    auth_identity = session_files._auth_identity(auth.stat(follow_symlinks=False))

    with pytest.raises(SessionFileSecurityError, match="auth scan exceeds the node limit"):
        remove_credential_tree(root, identity, auth_identity, max_auth_nodes=1)

    assert root.exists()
    assert auth.read_text(encoding="utf-8") == '{"access_token":"bounded-cleanup-canary"}\n'


def test_recovery_redactor_rejects_replaced_journal_bound_auth(tmp_path: Path) -> None:
    root, identity = _session_root(tmp_path)
    auth = root / "auth.json"
    auth.write_text('{"access_token":"original-canary"}\n', encoding="utf-8")
    auth.chmod(0o600)
    auth_identity = session_files._auth_identity(auth.stat(follow_symlinks=False))
    auth.unlink()
    auth.write_text('{"access_token":"replacement-canary"}\n', encoding="utf-8")
    auth.chmod(0o600)

    with pytest.raises(SessionFileSecurityError, match="journal-bound.*identity"):
        load_staged_codex_subscription_redactor(root, identity, auth_identity)

    assert "replacement-canary" in auth.read_text(encoding="utf-8")
