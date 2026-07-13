from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import os
import socket
from threading import Barrier, BrokenBarrierError
from pathlib import Path

import pytest

from openevo.evolution import artifact_payloads
from openevo.evolution.artifact_payloads import ArtifactPayloadService
from openevo.evolution.framework import (
    MAX_CONTRACT_JSON_BYTES,
    canonical_json,
    payload_tree_digest,
)


def _issue(
    service: ArtifactPayloadService,
    path: Path,
    *,
    manifest: dict[str, object] | None = None,
):
    return service.issue_snapshot(
        artifact_id="artifact-1",
        artifact_type="text_memory",
        name="Memory",
        uri=path.as_uri(),
        manifest=manifest or {},
        scores={"quality": 0.75},
        rank_index=0,
    )


def test_file_snapshot_inventory_and_read(tmp_path: Path) -> None:
    payload = tmp_path / "memory.md"
    payload.write_text("hello", encoding="utf-8")

    with ArtifactPayloadService(tmp_path) as service:
        snapshot = _issue(service, payload)

        assert [(entry.relative_path, entry.media_type, entry.size_bytes) for entry in snapshot.payload_entries] == [
            ("memory.md", "text/markdown", 5)
        ]
        assert snapshot.payload_entries[0].sha256 == hashlib.sha256(b"hello").hexdigest()
        assert snapshot.payload_manifest_digest == payload_tree_digest(snapshot.payload_entries)
        assert snapshot.manifest_json == "{}"
        assert snapshot.scores_json == canonical_json({"quality": 0.75})
        assert service.read_utf8_prefix(
            snapshot.payload_handle,
            "memory.md",
            max_chars=100,
            max_bytes=100,
        ) == "hello"


def test_directory_inventory_is_recursive_canonical_and_has_deterministic_mime(
    tmp_path: Path,
) -> None:
    payload = tmp_path / "bundle"
    (payload / "nested").mkdir(parents=True)
    (payload / "z.bin").write_bytes(b"bin")
    (payload / "nested" / "config.json").write_text("{}", encoding="utf-8")
    (payload / "nested" / "run.sh").write_text("exit 0", encoding="utf-8")
    (payload / "SKILL.md").write_text("skill", encoding="utf-8")
    (payload / "README.unknown").write_text("unknown", encoding="utf-8")

    with ArtifactPayloadService(tmp_path) as service:
        snapshot = _issue(service, payload)

    assert [(entry.relative_path, entry.media_type) for entry in snapshot.payload_entries] == [
        ("README.unknown", "application/octet-stream"),
        ("SKILL.md", "text/markdown"),
        ("nested/config.json", "application/json"),
        ("nested/run.sh", "application/x-sh"),
        ("z.bin", "application/octet-stream"),
    ]


def test_inventory_mime_map_covers_builtin_handler_contract(tmp_path: Path) -> None:
    payload = tmp_path / "bundle"
    payload.mkdir()
    expected = {
        "config.toml": "application/toml",
        "config.yaml": "application/yaml",
        "config.yml": "application/yaml",
        "image.jpeg": "image/jpeg",
        "image.jpg": "image/jpeg",
        "image.png": "image/png",
        "image.svg": "image/svg+xml",
        "style.css": "text/css",
        "table.csv": "text/csv",
        "page.html": "text/html",
        "page.htm": "text/html",
        "script.js": "text/javascript",
        "script.mjs": "text/javascript",
        "run.py": "text/x-python",
    }
    for relative_path in expected:
        (payload / relative_path).write_bytes(b"payload")

    with ArtifactPayloadService(tmp_path) as service:
        snapshot = _issue(service, payload)

    assert {entry.relative_path: entry.media_type for entry in snapshot.payload_entries} == expected


def test_file_content_path_reconstructs_payload_root(tmp_path: Path) -> None:
    payload = tmp_path / "artifact" / "content" / "memory.md"
    payload.parent.mkdir(parents=True)
    payload.write_text("memory", encoding="utf-8")

    with ArtifactPayloadService(tmp_path) as service:
        snapshot = _issue(service, payload, manifest={"content_path": "content/memory.md"})
        assert [entry.relative_path for entry in snapshot.payload_entries] == ["content/memory.md"]
        assert service.read_utf8_prefix(
            snapshot.payload_handle,
            "content/memory.md",
            max_chars=10,
            max_bytes=10,
        ) == "memory"


@pytest.mark.parametrize(
    "uri",
    [
        "https://example.test/file",
        "file://server/tmp/file",
        "file:///tmp/file?query=1",
        "file:///tmp/file#fragment",
        "file:relative/path",
        "file:///tmp/a%00b",
        "file:///tmp/a/../b",
        "file:///tmp//file",
        "file:///tmp/%FF",
    ],
)
def test_issue_snapshot_rejects_invalid_uri(tmp_path: Path, uri: str) -> None:
    with ArtifactPayloadService(tmp_path) as service:
        with pytest.raises(ValueError):
            service.issue_snapshot(
                artifact_id="artifact-1",
                artifact_type="text_memory",
                name="Memory",
                uri=uri,
                manifest={},
                scores={},
                rank_index=0,
            )


def test_issue_snapshot_rejects_payload_outside_allowed_root(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("outside", encoding="utf-8")

    with ArtifactPayloadService(allowed) as service:
        with pytest.raises(ValueError, match="allowed root"):
            _issue(service, outside)


def test_issue_snapshot_normalizes_missing_payload_to_validation_error(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "missing.md"
    with ArtifactPayloadService(tmp_path) as service:
        with pytest.raises(ValueError, match="opened|exist"):
            _issue(service, missing)


def test_issue_snapshot_rejects_root_and_descendant_symlinks(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    (target / "file.txt").write_text("text", encoding="utf-8")
    root_link = tmp_path / "root-link"
    root_link.symlink_to(target, target_is_directory=True)

    with ArtifactPayloadService(tmp_path) as service:
        with pytest.raises(ValueError, match="symlink"):
            _issue(service, root_link)

        bundle = tmp_path / "bundle"
        bundle.mkdir()
        (bundle / "link.txt").symlink_to(target / "file.txt")
        with pytest.raises(ValueError, match="symlink"):
            _issue(service, bundle)


def test_issue_snapshot_rejects_multiply_linked_regular_file(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside-secret.txt"
    outside.write_text("outside secret", encoding="utf-8")
    linked = tmp_path / "linked-secret.txt"
    os.link(outside, linked)

    with ArtifactPayloadService(tmp_path) as service:
        with pytest.raises(ValueError, match="hard link|link count"):
            _issue(service, linked)


def test_issue_snapshot_rejects_fifo_and_socket(tmp_path: Path) -> None:
    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)
    with socket.socket(socket.AF_UNIX) as unix_socket:
        socket_path = tmp_path / "socket"
        unix_socket.bind(str(socket_path))
        with ArtifactPayloadService(tmp_path) as service:
            with pytest.raises(ValueError, match="regular file or directory"):
                _issue(service, fifo)
            with pytest.raises(ValueError, match="regular file or directory"):
                _issue(service, socket_path)


def test_payload_count_and_depth_limits_are_enforced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = tmp_path / "bundle"
    (bundle / "one" / "two").mkdir(parents=True)
    (bundle / "a.txt").write_text("a", encoding="utf-8")
    (bundle / "b.txt").write_text("b", encoding="utf-8")
    (bundle / "one" / "two" / "deep.txt").write_text("deep", encoding="utf-8")

    monkeypatch.setattr(artifact_payloads, "MAX_PAYLOAD_ENTRIES", 2)
    with ArtifactPayloadService(tmp_path) as service:
        with pytest.raises(ValueError, match="entries"):
            _issue(service, bundle)

    monkeypatch.setattr(artifact_payloads, "MAX_PAYLOAD_ENTRIES", 10)
    monkeypatch.setattr(artifact_payloads, "MAX_PAYLOAD_TREE_DEPTH", 2)
    with ArtifactPayloadService(tmp_path) as service:
        with pytest.raises(ValueError, match="depth"):
            _issue(service, bundle)


def test_payload_node_budget_counts_empty_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "payload.txt").write_text("payload", encoding="utf-8")
    for index in range(4):
        (bundle / f"empty-{index}").mkdir()
    monkeypatch.setattr(artifact_payloads, "_MAX_PAYLOAD_NODES", 3, raising=False)

    with ArtifactPayloadService(tmp_path) as service:
        with pytest.raises(ValueError, match="nodes|entries"):
            _issue(service, bundle)
        assert service._attempted_nodes == 4


def test_payload_entry_and_total_byte_limits_are_enforced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = tmp_path / "payload.bin"
    payload.write_bytes(b"1234")
    monkeypatch.setattr(artifact_payloads, "MAX_PAYLOAD_ENTRY_BYTES", 3)
    with ArtifactPayloadService(tmp_path) as service:
        with pytest.raises(ValueError, match="entry bytes"):
            _issue(service, payload)

    monkeypatch.setattr(artifact_payloads, "MAX_PAYLOAD_ENTRY_BYTES", 10)
    monkeypatch.setattr(artifact_payloads, "MAX_PAYLOAD_TOTAL_BYTES", 3)
    with ArtifactPayloadService(tmp_path) as service:
        with pytest.raises(ValueError, match="total bytes"):
            _issue(service, payload)


def test_service_enforces_aggregate_snapshot_byte_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = tmp_path / "first.bin"
    second = tmp_path / "second.bin"
    first.write_bytes(b"123")
    second.write_bytes(b"456")
    monkeypatch.setattr(artifact_payloads, "MAX_PAYLOAD_ENTRY_BYTES", 5)
    monkeypatch.setattr(artifact_payloads, "MAX_PAYLOAD_TOTAL_BYTES", 5)

    with ArtifactPayloadService(tmp_path) as service:
        _issue(service, first)
        with pytest.raises(ValueError, match="service.*total bytes|aggregate"):
            _issue(service, second)


def test_verified_reread_consumes_aggregate_byte_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = tmp_path / "payload.txt"
    payload.write_bytes(b"123")
    monkeypatch.setattr(artifact_payloads, "MAX_PAYLOAD_ENTRY_BYTES", 6)
    monkeypatch.setattr(artifact_payloads, "MAX_PAYLOAD_TOTAL_BYTES", 6)

    with ArtifactPayloadService(tmp_path) as service:
        snapshot = _issue(service, payload)
        assert service.read_utf8_prefix(
            snapshot.payload_handle,
            "payload.txt",
            max_chars=3,
            max_bytes=3,
        ) == "123"
        assert service._attempted_bytes == 6

        with pytest.raises(
            artifact_payloads.ArtifactPayloadBudgetExceeded,
            match="aggregate total bytes",
        ):
            service.read_utf8_prefix(
                snapshot.payload_handle,
                "payload.txt",
                max_chars=3,
                max_bytes=3,
            )


def test_verified_reread_consumes_node_and_file_attempt_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = tmp_path / "empty.txt"
    payload.write_bytes(b"")
    monkeypatch.setattr(artifact_payloads, "MAX_PAYLOAD_ENTRIES", 2)

    with ArtifactPayloadService(tmp_path) as service:
        snapshot = _issue(service, payload)
        assert service.read_utf8_prefix(
            snapshot.payload_handle,
            "empty.txt",
            max_chars=0,
            max_bytes=0,
        ) == ""
        assert service._attempted_files == 2

        with pytest.raises(
            artifact_payloads.ArtifactPayloadBudgetExceeded,
            match="aggregate entries",
        ):
            service.read_utf8_prefix(
                snapshot.payload_handle,
                "empty.txt",
                max_chars=0,
                max_bytes=0,
            )
        assert service._attempted_files == 3


def test_failed_snapshot_scan_consumes_attempted_aggregate_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    oversized = tmp_path / "oversized"
    oversized.mkdir()
    (oversized / "a.bin").write_bytes(b"12345")
    (oversized / "z.bin").write_bytes(b"6")
    next_payload = tmp_path / "next.bin"
    next_payload.write_bytes(b"7")
    monkeypatch.setattr(artifact_payloads, "MAX_PAYLOAD_ENTRY_BYTES", 5)
    monkeypatch.setattr(artifact_payloads, "MAX_PAYLOAD_TOTAL_BYTES", 5)

    with ArtifactPayloadService(tmp_path) as service:
        with pytest.raises(ValueError, match="maximum total bytes"):
            _issue(service, oversized)
        assert service._attempted_bytes == 5

        with pytest.raises(
            artifact_payloads.ArtifactPayloadBudgetExceeded,
            match="aggregate total bytes",
        ):
            _issue(service, next_payload)
        assert service._attempted_bytes == 6


def test_snapshot_metadata_is_validated_before_payload_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = tmp_path / "payload.bin"
    payload.write_bytes(b"payload")

    def unexpected_read(_fd: int):
        raise AssertionError("invalid metadata reached payload hashing")
        yield b""  # pragma: no cover

    monkeypatch.setattr(artifact_payloads, "_stream_fd_chunks", unexpected_read)
    with ArtifactPayloadService(tmp_path) as service:
        with pytest.raises(ValueError, match="scores.*JSON byte budget"):
            service.issue_snapshot(
                artifact_id="artifact-1",
                artifact_type="text_memory",
                name="Memory",
                uri=payload.as_uri(),
                manifest={},
                scores={"oversized": "x" * MAX_CONTRACT_JSON_BYTES},
                rank_index=0,
            )
        assert service._attempted_bytes == 0


def test_local_file_overflow_records_final_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "a.txt").write_text("a", encoding="utf-8")
    (bundle / "b.txt").write_text("b", encoding="utf-8")
    monkeypatch.setattr(artifact_payloads, "MAX_PAYLOAD_ENTRIES", 1)

    with ArtifactPayloadService(tmp_path) as service:
        with pytest.raises(ValueError, match="maximum entries"):
            _issue(service, bundle)
        assert service._attempted_files == 2


def test_scan_fails_closed_when_file_mutates_during_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = tmp_path / "payload.txt"
    payload.write_bytes(b"first")
    original = artifact_payloads._stream_fd_chunks
    mutated = False

    def mutate_after_first_chunk(fd: int):
        nonlocal mutated
        for chunk in original(fd):
            yield chunk
            if not mutated:
                payload.write_bytes(b"changed-longer")
                mutated = True

    monkeypatch.setattr(artifact_payloads, "_stream_fd_chunks", mutate_after_first_chunk)
    with ArtifactPayloadService(tmp_path) as service:
        with pytest.raises(ValueError, match="mutated"):
            _issue(service, payload)


def test_scan_fails_closed_on_pre_stat_open_race(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = tmp_path / "payload.txt"
    payload.write_bytes(b"first")
    original = artifact_payloads._open_at
    replaced = False

    def replace_before_open(
        path: str | os.PathLike[str], flags: int, *, dir_fd: int | None = None
    ) -> int:
        nonlocal replaced
        if path == "payload.txt" and dir_fd is not None and not replaced:
            payload.unlink()
            payload.write_bytes(b"replacement")
            replaced = True
        return original(path, flags, dir_fd=dir_fd)

    monkeypatch.setattr(artifact_payloads, "_open_at", replace_before_open)
    with ArtifactPayloadService(tmp_path) as service:
        with pytest.raises(ValueError, match="mutated"):
            _issue(service, payload)


def test_scan_uses_nonblocking_open_when_regular_file_becomes_fifo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = tmp_path / "payload.txt"
    payload.write_bytes(b"first")
    original = artifact_payloads._open_at
    replaced = False

    def replace_with_fifo_before_open(
        path: str | os.PathLike[str], flags: int, *, dir_fd: int | None = None
    ) -> int:
        nonlocal replaced
        if path == "payload.txt" and dir_fd is not None and not replaced:
            payload.unlink()
            os.mkfifo(payload)
            replaced = True
            assert flags & os.O_NONBLOCK
            assert flags & os.O_PATH
        return original(path, flags, dir_fd=dir_fd)

    monkeypatch.setattr(artifact_payloads, "_open_at", replace_with_fifo_before_open)
    with ArtifactPayloadService(tmp_path) as service:
        with pytest.raises(ValueError, match="mutated"):
            _issue(service, payload)


@pytest.mark.parametrize("mutation", ["file", "nested_directory"])
def test_snapshot_revalidates_every_descendant_before_issuance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    bundle = tmp_path / "bundle"
    nested = bundle / "nested"
    nested.mkdir(parents=True)
    payload = nested / "payload.txt"
    payload.write_text("first", encoding="utf-8")
    original = ArtifactPayloadService._verify_path_identity
    mutated = False

    def mutate_before_final_identity_check(
        self: ArtifactPayloadService,
        components: tuple[str, ...],
        expected,
        *,
        mutation_label: str,
    ) -> None:
        nonlocal mutated
        if not mutated:
            if mutation == "file":
                before = payload.stat()
                payload.write_text("other", encoding="utf-8")
                os.utime(
                    payload,
                    ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000_000),
                )
            else:
                before = nested.stat()
                (nested / "added.txt").write_text("added", encoding="utf-8")
                os.utime(
                    nested,
                    ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000_000),
                )
            mutated = True
        original(self, components, expected, mutation_label=mutation_label)

    monkeypatch.setattr(
        ArtifactPayloadService,
        "_verify_path_identity",
        mutate_before_final_identity_check,
    )
    with ArtifactPayloadService(tmp_path) as service:
        with pytest.raises(ValueError, match="mutated|drift"):
            _issue(service, bundle)


def test_source_change_after_inventory_issuance_fails_verified_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = tmp_path / "payload.txt"
    payload.write_text("first", encoding="utf-8")
    original = ArtifactPayloadService._allocate_handle

    def mutate_after_stability_checks(self: ArtifactPayloadService) -> str:
        before = payload.stat()
        payload.write_text("other", encoding="utf-8")
        os.utime(
            payload,
            ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000_000),
        )
        return original(self)

    monkeypatch.setattr(
        ArtifactPayloadService,
        "_allocate_handle",
        mutate_after_stability_checks,
    )
    with ArtifactPayloadService(tmp_path) as service:
        snapshot = _issue(service, payload)
        assert snapshot.payload_entries[0].sha256 == hashlib.sha256(b"first").hexdigest()
        with pytest.raises(ValueError, match="drift"):
            service.read_utf8_prefix(
                snapshot.payload_handle,
                "payload.txt",
                max_chars=10,
                max_bytes=10,
            )


def test_read_rejects_digest_and_identity_drift(tmp_path: Path) -> None:
    payload = tmp_path / "payload.txt"
    payload.write_text("first", encoding="utf-8")
    with ArtifactPayloadService(tmp_path) as service:
        snapshot = _issue(service, payload)
        payload.write_text("other", encoding="utf-8")
        with pytest.raises(ValueError, match="drift"):
            service.read_utf8_prefix(
                snapshot.payload_handle,
                "payload.txt",
                max_chars=100,
                max_bytes=100,
            )


def test_read_clips_by_character_and_utf8_byte_boundaries(tmp_path: Path) -> None:
    payload = tmp_path / "payload.txt"
    payload.write_text("A界BC", encoding="utf-8")
    with ArtifactPayloadService(tmp_path) as service:
        snapshot = _issue(service, payload)
        assert service.read_utf8_prefix(
            snapshot.payload_handle, "payload.txt", max_chars=2, max_bytes=4
        ) == "A界"
        assert service.read_utf8_prefix(
            snapshot.payload_handle, "payload.txt", max_chars=4, max_bytes=3
        ) == "A"
        assert service.read_utf8_prefix(
            snapshot.payload_handle, "payload.txt", max_chars=0, max_bytes=0
        ) == ""


def test_read_validates_invalid_utf8_after_prefix(tmp_path: Path) -> None:
    payload = tmp_path / "payload.txt"
    payload.write_bytes(b"valid-prefix\xff")
    with ArtifactPayloadService(tmp_path) as service:
        snapshot = _issue(service, payload)
        with pytest.raises(ValueError, match="UTF-8"):
            service.read_utf8_prefix(
                snapshot.payload_handle,
                "payload.txt",
                max_chars=1,
                max_bytes=1,
            )


def test_read_stops_immediately_when_stream_exceeds_snapshot_size(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = tmp_path / "payload.txt"
    payload.write_bytes(b"text")
    with ArtifactPayloadService(tmp_path) as service:
        snapshot = _issue(service, payload)

        def oversized_stream(_fd: int):
            yield b"text"
            yield b"x"
            raise AssertionError("verified reread continued after expected size")

        monkeypatch.setattr(artifact_payloads, "_stream_fd_chunks", oversized_stream)
        with pytest.raises(ValueError, match="size|drift"):
            service.read_utf8_prefix(
                snapshot.payload_handle,
                "payload.txt",
                max_chars=10,
                max_bytes=10,
            )


def test_read_rejects_unknown_handle_path_and_invalid_limits(tmp_path: Path) -> None:
    payload = tmp_path / "payload.txt"
    payload.write_text("text", encoding="utf-8")
    with ArtifactPayloadService(tmp_path) as service:
        snapshot = _issue(service, payload)
        with pytest.raises(ValueError, match="handle"):
            service.read_utf8_prefix("payload-unknown", "payload.txt", max_chars=1, max_bytes=1)
        with pytest.raises(ValueError, match="path"):
            service.read_utf8_prefix(
                snapshot.payload_handle, "missing.txt", max_chars=1, max_bytes=1
            )
        for max_chars, max_bytes in ((-1, 1), (1, -1), (artifact_payloads.MAX_CONTRIBUTION_TEXT + 1, 1)):
            with pytest.raises(ValueError, match="limits"):
                service.read_utf8_prefix(
                    snapshot.payload_handle,
                    "payload.txt",
                    max_chars=max_chars,
                    max_bytes=max_bytes,
                )


def test_close_invalidates_handles_and_service(tmp_path: Path) -> None:
    payload = tmp_path / "payload.txt"
    payload.write_text("text", encoding="utf-8")
    service = ArtifactPayloadService(tmp_path)
    snapshot = _issue(service, payload)
    service.close()
    service.close()

    with pytest.raises(RuntimeError, match="closed"):
        service.read_utf8_prefix(
            snapshot.payload_handle, "payload.txt", max_chars=1, max_bytes=1
        )
    with pytest.raises(RuntimeError, match="closed"):
        _issue(service, payload)


def test_handle_collision_never_rebinds_existing_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    first.write_text("first", encoding="utf-8")
    second.write_text("second", encoding="utf-8")
    monkeypatch.setattr(artifact_payloads.secrets, "token_hex", lambda _size: "a" * 48)

    with ArtifactPayloadService(tmp_path) as service:
        snapshot = _issue(service, first)
        with pytest.raises(RuntimeError, match="unique payload handle"):
            _issue(service, second)
        assert service.read_utf8_prefix(
            snapshot.payload_handle,
            "first.txt",
            max_chars=10,
            max_bytes=10,
        ) == "first"


def test_concurrent_handle_collision_is_atomic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    first.write_text("first", encoding="utf-8")
    second.write_text("second", encoding="utf-8")
    monkeypatch.setattr(artifact_payloads.secrets, "token_hex", lambda _size: "b" * 48)
    original = ArtifactPayloadService._allocate_handle
    rendezvous = Barrier(2)

    def synchronize_after_allocation(self: ArtifactPayloadService) -> str:
        handle = original(self)
        try:
            rendezvous.wait(timeout=0.2)
        except BrokenBarrierError:
            pass
        return handle

    monkeypatch.setattr(
        ArtifactPayloadService,
        "_allocate_handle",
        synchronize_after_allocation,
    )
    with ArtifactPayloadService(tmp_path) as service, ThreadPoolExecutor(
        max_workers=2
    ) as executor:
        futures = [executor.submit(_issue, service, path) for path in (first, second)]
        results = []
        errors = []
        for future in futures:
            try:
                results.append(future.result())
            except RuntimeError as exc:
                errors.append(exc)

        assert len(results) == 1
        assert len(errors) == 1
        assert "unique payload handle" in str(errors[0])


def test_fstat_failure_does_not_leak_root_or_relative_fd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = tmp_path / "payload.txt"
    payload.write_text("payload", encoding="utf-8")
    original = artifact_payloads.os.fstat

    def fd_count() -> int:
        return len(os.listdir("/proc/self/fd"))

    fail_next = True

    def fail_once(fd: int):
        nonlocal fail_next
        if fail_next:
            fail_next = False
            raise OSError("injected fstat failure")
        return original(fd)

    before = fd_count()
    monkeypatch.setattr(artifact_payloads.os, "fstat", fail_once)
    with pytest.raises(OSError, match="injected"):
        ArtifactPayloadService(tmp_path)
    assert fd_count() == before

    monkeypatch.setattr(artifact_payloads.os, "fstat", original)
    with ArtifactPayloadService(tmp_path) as service:
        fail_next = True
        monkeypatch.setattr(artifact_payloads.os, "fstat", fail_once)
        before = fd_count()
        with pytest.raises(OSError, match="injected"):
            _issue(service, tmp_path)
        assert fd_count() == before


def test_path_fd_close_failure_reclaims_unreturned_readable_fd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = tmp_path / "payload.txt"
    payload.write_text("payload", encoding="utf-8")
    original_open = artifact_payloads._open_at
    original_close = artifact_payloads.os.close
    opened: list[int] = []

    def tracked_open(*args, **kwargs) -> int:
        fd = original_open(*args, **kwargs)
        opened.append(fd)
        return fd

    def close_path_fd_then_fail(fd: int) -> None:
        original_close(fd)
        if opened and fd == opened[0]:
            raise InterruptedError("injected path fd close failure")

    with ArtifactPayloadService(tmp_path) as service:
        expected = artifact_payloads._identity(payload.stat())
        before = len(os.listdir("/proc/self/fd"))
        monkeypatch.setattr(artifact_payloads, "_open_at", tracked_open)
        monkeypatch.setattr(artifact_payloads.os, "close", close_path_fd_then_fail)
        with pytest.raises(InterruptedError, match="path fd close"):
            service._open_verified_node(
                service._root_fd,
                payload.name,
                expected,
                directory=False,
            )
        assert len(os.listdir("/proc/self/fd")) == before


def test_parent_fd_close_failure_reclaims_untransferred_child_fd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = tmp_path / "payload.txt"
    payload.write_text("payload", encoding="utf-8")
    original_open = artifact_payloads._open_at
    original_close = artifact_payloads.os.close
    opened: list[int] = []

    def tracked_open(*args, **kwargs) -> int:
        fd = original_open(*args, **kwargs)
        opened.append(fd)
        return fd

    def close_parent_fd_then_fail(fd: int) -> None:
        original_close(fd)
        if opened and fd == opened[0]:
            raise InterruptedError("injected parent fd close failure")

    with ArtifactPayloadService(tmp_path) as service:
        before = len(os.listdir("/proc/self/fd"))
        monkeypatch.setattr(artifact_payloads, "_open_at", tracked_open)
        monkeypatch.setattr(artifact_payloads.os, "close", close_parent_fd_then_fail)
        with pytest.raises(InterruptedError, match="parent fd close"):
            service._open_relative((payload.name,))
        assert len(os.listdir("/proc/self/fd")) == before
