from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
import shutil
import sqlite3
import threading

import pytest

from openevo.backend.contracts.v2.models import WorkspaceSnapshotRefV2
from openevo.backend.workspace_handoff_v2 import (
    RuntimeInjectedWorkspaceFileV2,
    WorkspaceHandoffConflictV2,
    WorkspaceHandoffIntegrityErrorV2,
    WorkspaceHandoffRequestV2,
    WorkspaceHandoffStoreV2,
)
from openevo.workspace_archive import WorkspaceArchiveBuildError, write_workspace_archive


NOW = datetime(2026, 7, 23, 4, tzinfo=timezone.utc)


def test_workspace_archive_rejects_a_nonzero_output_cursor(tmp_path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    output = tmp_path / "output.tar"
    descriptor = os.open(output, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.lseek(descriptor, 4096, os.SEEK_SET)
        with pytest.raises(WorkspaceArchiveBuildError):
            write_workspace_archive(source, descriptor)
    finally:
        os.close(descriptor)


def test_workspace_archive_translates_an_unavailable_root(tmp_path) -> None:
    output = tmp_path / "output.tar"
    descriptor = os.open(output, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with pytest.raises(WorkspaceArchiveBuildError):
            write_workspace_archive(tmp_path / "missing", descriptor)
    finally:
        os.close(descriptor)


def test_fresh_handoff_store_refuses_to_claim_preexisting_managed_state(
    tmp_path,
) -> None:
    root = tmp_path / "handoffs"
    inputs = root / "inputs"
    inputs.mkdir(parents=True, mode=0o700)
    root.chmod(0o700)
    orphan = inputs / "workspace-handoff-orphan.tar"
    orphan.write_bytes(bytes(1024))
    orphan.chmod(0o600)

    with pytest.raises(WorkspaceHandoffIntegrityErrorV2):
        WorkspaceHandoffStoreV2(root)

    assert orphan.exists()


def test_handoff_store_binds_exact_schema_and_database_inode(tmp_path) -> None:
    root = tmp_path / "handoffs"
    store = WorkspaceHandoffStoreV2(root)
    store.close()
    database = root / "workspace-handoff-v2.sqlite3"

    with sqlite3.connect(database) as connection:
        connection.execute("ALTER TABLE handoffs ADD COLUMN injected TEXT")
        connection.commit()
    with pytest.raises(WorkspaceHandoffIntegrityErrorV2):
        WorkspaceHandoffStoreV2(root)

    # Recreate a valid store, then replace its database with a byte-identical inode.
    shutil.rmtree(root)
    store = WorkspaceHandoffStoreV2(root)
    store.close()
    replacement = root / "replacement.sqlite3"
    shutil.copyfile(database, replacement)
    replacement.chmod(0o600)
    os.replace(replacement, database)

    with pytest.raises(WorkspaceHandoffIntegrityErrorV2):
        WorkspaceHandoffStoreV2(root)


def _request(source, tmp_path) -> WorkspaceHandoffRequestV2:
    probe = tmp_path / f"probe-{source.name}.tar"
    descriptor = os.open(probe, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        archive = write_workspace_archive(source, descriptor)
    finally:
        os.close(descriptor)
        probe.unlink()
    return WorkspaceHandoffRequestV2(
        task_id="task-handoff",
        attempt_id="attempt-handoff",
        task_admission_id="task-admission-handoff",
        admission_sha256="1" * 64,
        project_id="project-handoff",
        input_workspace_snapshot=WorkspaceSnapshotRefV2(
            workspace_snapshot_id="workspace-input",
            project_id="project-handoff",
            manifest_sha256="2" * 64,
            entry_count=archive.entry_count,
            byte_size=archive.extracted_byte_size,
        ),
        input_archive=archive,
        service_generation_sha256="3" * 64,
        registry_sha256="4" * 64,
        framework_lock_sha256="5" * 64,
    )


def test_handoff_is_opaque_durable_and_publishes_one_exact_result(tmp_path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "src").mkdir()
    (source / "src" / "main.py").write_text("old\n", encoding="utf-8")
    (source / "README.md").write_text("input\n", encoding="utf-8")
    store = WorkspaceHandoffStoreV2(tmp_path / "handoffs")
    request = _request(source, tmp_path)

    binding = store.reserve(request, source, now=NOW)
    replay = store.reserve(request, source, now=NOW)
    assert replay == binding
    assert binding.input_archive.entry_count == 3
    assert binding.input_archive.extracted_byte_size == 10
    serialized = json.dumps(binding.model_dump(mode="json"))
    assert os.fspath(tmp_path) not in serialized

    store.claim(
        binding,
        session_id="sk-openevo-session-1",
        generation_sha256=request.service_generation_sha256,
        registry_sha256=request.registry_sha256,
        framework_lock_sha256=request.framework_lock_sha256,
    )
    session = tmp_path / "session"
    session.mkdir(mode=0o700)
    store.materialize_input(
        binding,
        session_id="sk-openevo-session-1",
        destination_parent=session,
    )
    workspace = session / "workspace"
    assert (workspace / "src" / "main.py").read_text(encoding="utf-8") == "old\n"

    (workspace / "src" / "main.py").write_text("new\n", encoding="utf-8")
    (workspace / "result.txt").write_text("done\n", encoding="utf-8")
    receipt = store.publish_result(
        binding,
        session_id="sk-openevo-session-1",
        workspace_root=workspace,
        now=NOW,
    )
    assert (
        store.publish_result(
            binding,
            session_id="sk-openevo-session-1",
            workspace_root=workspace,
            now=NOW,
        )
        == receipt
    )
    assert receipt.output_archive.entry_count == 4
    assert receipt.output_archive.extracted_byte_size == 15
    assert (
        receipt.result_manifest_sha256
        == hashlib.sha256(receipt.canonical_manifest_bytes()).hexdigest()
    )
    assert os.fspath(tmp_path) not in json.dumps(receipt.model_dump(mode="json"))
    with store.open_result(receipt) as stream:
        archive = stream.read()
    assert hashlib.sha256(archive).hexdigest() == receipt.output_archive.content_sha256
    store.close()

    restarted = WorkspaceHandoffStoreV2(tmp_path / "handoffs")
    try:
        assert restarted.get_binding(binding.handoff_id) == binding
        assert restarted.get_result(binding.handoff_id) == receipt
        with restarted.open_result(receipt) as stream:
            assert hashlib.sha256(stream.read()).hexdigest() == (
                receipt.output_archive.content_sha256
            )
    finally:
        restarted.close()


def test_handoff_rejects_wrong_generation_and_session(tmp_path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    store = WorkspaceHandoffStoreV2(tmp_path / "handoffs")
    request = _request(source, tmp_path)
    binding = store.reserve(request, source, now=NOW)

    with pytest.raises(WorkspaceHandoffConflictV2):
        store.claim(
            binding,
            session_id="sk-openevo-session-1",
            generation_sha256="0" * 64,
            registry_sha256=request.registry_sha256,
            framework_lock_sha256=request.framework_lock_sha256,
        )
    store.claim(
        binding,
        session_id="sk-openevo-session-1",
        generation_sha256=request.service_generation_sha256,
        registry_sha256=request.registry_sha256,
        framework_lock_sha256=request.framework_lock_sha256,
    )
    with pytest.raises(WorkspaceHandoffConflictV2):
        store.materialize_input(
            binding,
            session_id="sk-openevo-session-2",
            destination_parent=tmp_path,
        )
    store.close()


def test_workspace_result_rejects_symlink_and_retains_no_receipt(tmp_path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    store = WorkspaceHandoffStoreV2(tmp_path / "handoffs")
    request = _request(source, tmp_path)
    binding = store.reserve(request, source, now=NOW)
    store.claim(
        binding,
        session_id="sk-openevo-session-1",
        generation_sha256=request.service_generation_sha256,
        registry_sha256=request.registry_sha256,
        framework_lock_sha256=request.framework_lock_sha256,
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "outside").symlink_to(tmp_path / "outside")

    with pytest.raises(WorkspaceHandoffConflictV2):
        store.publish_result(
            binding,
            session_id="sk-openevo-session-1",
            workspace_root=workspace,
            now=NOW,
        )
    assert store.get_result(binding.handoff_id) is None
    store.close()


def _injected_file(relative_path: str, payload: bytes) -> RuntimeInjectedWorkspaceFileV2:
    return RuntimeInjectedWorkspaceFileV2(
        relative_path=relative_path,
        size_bytes=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
    )


def _claimed_materialized_handoff(tmp_path, source):
    store = WorkspaceHandoffStoreV2(tmp_path / "handoffs")
    request = _request(source, tmp_path)
    binding = store.reserve(request, source, now=NOW)
    store.claim(
        binding,
        session_id="sk-openevo-session-1",
        generation_sha256=request.service_generation_sha256,
        registry_sha256=request.registry_sha256,
        framework_lock_sha256=request.framework_lock_sha256,
    )
    session = tmp_path / "session"
    session.mkdir(mode=0o700)
    store.materialize_input(
        binding,
        session_id="sk-openevo-session-1",
        destination_parent=session,
    )
    return store, binding, session / "workspace"


def test_workspace_result_removes_unchanged_runtime_injection_and_empty_parents(
    tmp_path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    store, binding, workspace = _claimed_materialized_handoff(tmp_path, source)
    injected = b"# Evolved agent system\n"
    target = workspace / ".openhands" / "microagents" / "openevo.md"
    target.parent.mkdir(parents=True)
    target.write_bytes(injected)

    receipt = store.publish_result(
        binding,
        session_id="sk-openevo-session-1",
        workspace_root=workspace,
        runtime_injected_files=(_injected_file(".openhands/microagents/openevo.md", injected),),
        now=NOW,
    )

    assert receipt.output_archive == binding.input_archive
    assert list(workspace.iterdir()) == []
    store.close()


def test_workspace_result_restores_preexisting_runtime_injection_target(tmp_path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    original = b"# Repository instructions\n"
    (source / "AGENTS.md").write_bytes(original)
    (source / "AGENTS.md").chmod(0o755)
    store, binding, workspace = _claimed_materialized_handoff(tmp_path, source)
    injected = b"# Evolved agent system\n"
    (workspace / "AGENTS.md").write_bytes(injected)
    (workspace / "AGENTS.md").chmod(0o644)

    receipt = store.publish_result(
        binding,
        session_id="sk-openevo-session-1",
        workspace_root=workspace,
        runtime_injected_files=(_injected_file("AGENTS.md", injected),),
        now=NOW,
    )

    assert receipt.output_archive == binding.input_archive
    assert (workspace / "AGENTS.md").read_bytes() == original
    assert (workspace / "AGENTS.md").stat().st_mode & 0o777 == 0o755
    store.close()


def test_workspace_result_rejects_a_changed_runtime_injection_target(tmp_path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    store, binding, workspace = _claimed_materialized_handoff(tmp_path, source)
    injected = b"# Evolved agent system\n"
    (workspace / "AGENTS.md").write_bytes(b"agent changed it\n")

    with pytest.raises(
        WorkspaceHandoffConflictV2,
        match="runtime-injected workspace file changed",
    ):
        store.publish_result(
            binding,
            session_id="sk-openevo-session-1",
            workspace_root=workspace,
            runtime_injected_files=(_injected_file("AGENTS.md", injected),),
            now=NOW,
        )

    assert store.get_result(binding.handoff_id) is None
    store.close()


def test_workspace_runtime_injection_restore_is_idempotent_after_archive_failure(
    tmp_path,
    monkeypatch,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    original = b"# Repository instructions\n"
    (source / "AGENTS.md").write_bytes(original)
    store, binding, workspace = _claimed_materialized_handoff(tmp_path, source)
    injected = b"# Evolved agent system\n"
    (workspace / "AGENTS.md").write_bytes(injected)
    import openevo.backend.workspace_handoff_v2 as handoff_module

    original_writer = handoff_module.write_workspace_archive
    attempts = 0

    def fail_once(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise WorkspaceArchiveBuildError("injected archive failure")
        return original_writer(*args, **kwargs)

    monkeypatch.setattr(handoff_module, "write_workspace_archive", fail_once)
    runtime_injected_files = (_injected_file("AGENTS.md", injected),)

    with pytest.raises(WorkspaceHandoffConflictV2):
        store.publish_result(
            binding,
            session_id="sk-openevo-session-1",
            workspace_root=workspace,
            runtime_injected_files=runtime_injected_files,
            now=NOW,
        )
    assert (workspace / "AGENTS.md").read_bytes() == original

    receipt = store.publish_result(
        binding,
        session_id="sk-openevo-session-1",
        workspace_root=workspace,
        runtime_injected_files=runtime_injected_files,
        now=NOW,
    )
    assert receipt.output_archive == binding.input_archive
    assert attempts == 2
    store.close()


def test_handoff_publication_is_serialized_against_a_second_process_store(
    tmp_path,
    monkeypatch,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "README.md").write_text("input\n", encoding="utf-8")
    root = tmp_path / "handoffs"
    first = WorkspaceHandoffStoreV2(root)
    request = _request(source, tmp_path)
    published = threading.Event()
    release = threading.Event()
    reserve_done = threading.Event()
    second_ready = threading.Event()
    results: dict[str, object] = {}
    original_publish = first._publish_no_replace

    def pause_after_archive_publication(*args, **kwargs) -> None:
        original_publish(*args, **kwargs)
        published.set()
        assert release.wait(timeout=5.0)

    monkeypatch.setattr(first, "_publish_no_replace", pause_after_archive_publication)

    def reserve() -> None:
        try:
            results["binding"] = first.reserve(request, source, now=NOW)
        except BaseException as exc:  # pragma: no cover - asserted in the parent thread
            results["reserve_error"] = exc
        finally:
            reserve_done.set()

    def open_second() -> None:
        try:
            results["second"] = WorkspaceHandoffStoreV2(root)
        except BaseException as exc:  # pragma: no cover - asserted in the parent thread
            results["second_error"] = exc
        finally:
            second_ready.set()

    reserve_thread = threading.Thread(target=reserve)
    second_thread = threading.Thread(target=open_second)
    reserve_thread.start()
    assert published.wait(timeout=5.0)
    second_thread.start()
    try:
        assert not second_ready.wait(timeout=0.2)
    finally:
        release.set()
        reserve_thread.join(timeout=5.0)
        second_thread.join(timeout=5.0)
        first.close()

    assert reserve_done.is_set()
    assert second_ready.is_set()
    assert "reserve_error" not in results
    assert "second_error" not in results
    binding = results["binding"]
    second = results["second"]
    assert second.get_binding(binding.handoff_id) == binding
    second.close()
