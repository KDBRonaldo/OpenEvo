"""Core-owned inventory issuance with contained, revalidated payload reads."""

from __future__ import annotations

import codecs
import hashlib
import os
import re
import secrets
import stat
import threading
import unicodedata
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import TracebackType
from urllib.parse import unquote_to_bytes, urlsplit

from .framework.contracts import (
    MAX_CONTRIBUTION_TEXT,
    MAX_HANDLER_ARTIFACTS,
    MAX_PAYLOAD_ENTRIES,
    MAX_PAYLOAD_ENTRY_BYTES,
    MAX_PAYLOAD_TOTAL_BYTES,
    MAX_PAYLOAD_TREE_DEPTH,
    _bounded_canonical_json_object,
    _stable_id,
    validate_relative_path,
)
from .framework.handlers import (
    PayloadManifestEntry,
    TrustedArtifactSnapshot,
    payload_tree_digest,
)


_CHUNK_BYTES = 1024 * 1024
_MAX_HANDLE_ATTEMPTS = 32
_MAX_PAYLOAD_NODES = MAX_PAYLOAD_ENTRIES * MAX_PAYLOAD_TREE_DEPTH + 1
_BAD_PERCENT_ESCAPE = re.compile(r"%(?![0-9A-Fa-f]{2})")
_O_PATH = getattr(os, "O_PATH", None)
_READ_BASE_FLAGS = (
    os.O_RDONLY
    | os.O_CLOEXEC
    | os.O_NOFOLLOW
    | os.O_NONBLOCK
    | getattr(os, "O_NOCTTY", 0)
)
_READ_FIXED_FD_FLAGS = (
    os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK | getattr(os, "O_NOCTTY", 0)
)
_MIME_BY_SUFFIX = {
    ".json": "application/json",
    ".toml": "application/toml",
    ".yaml": "application/yaml",
    ".yml": "application/yaml",
    ".jpeg": "image/jpeg",
    ".jpg": "image/jpeg",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".css": "text/css",
    ".csv": "text/csv",
    ".htm": "text/html",
    ".html": "text/html",
    ".cjs": "text/javascript",
    ".js": "text/javascript",
    ".mjs": "text/javascript",
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".py": "text/x-python",
    ".sh": "application/x-sh",
    ".text": "text/plain",
    ".txt": "text/plain",
}


class ArtifactPayloadBudgetExceeded(ValueError):
    """The request-scoped payload service exhausted an aggregate budget."""


@dataclass(frozen=True, slots=True)
class _FileIdentity:
    device: int
    inode: int
    mode: int
    link_count: int
    size: int
    mtime_ns: int
    ctime_ns: int


@dataclass(frozen=True, slots=True)
class _FileRecord:
    relative_path: str
    identity: _FileIdentity
    size_bytes: int
    sha256: str


@dataclass(frozen=True, slots=True)
class _DirectoryRecord:
    relative_components: tuple[str, ...]
    identity: _FileIdentity


@dataclass(slots=True)
class _ScanBudget:
    nodes: int = 0
    files: int = 0
    total_bytes: int = 0


@dataclass(frozen=True, slots=True)
class _HandleRecord:
    root_components: tuple[str, ...]
    files: Mapping[str, _FileRecord]


def _identity(value: os.stat_result) -> _FileIdentity:
    return _FileIdentity(
        device=value.st_dev,
        inode=value.st_ino,
        mode=value.st_mode,
        link_count=value.st_nlink,
        size=value.st_size,
        mtime_ns=value.st_mtime_ns,
        ctime_ns=value.st_ctime_ns,
    )


def _stream_fd_chunks(fd: int) -> Iterator[bytes]:
    """Private streaming hook used by scanner TOCTOU tests."""

    while chunk := os.read(fd, _CHUNK_BYTES):
        yield chunk


def _open_at(path: str | os.PathLike[str], flags: int, *, dir_fd: int | None = None) -> int:
    """Private open hook kept narrow for deterministic race tests."""

    return os.open(path, flags, dir_fd=dir_fd)


def _close_untransferred_fd(fd: int) -> None:
    """Best-effort cleanup that must not mask an earlier ownership error."""

    try:
        os.close(fd)
    except OSError:
        pass


def _media_type(relative_path: str) -> str:
    return _MIME_BY_SUFFIX.get(
        PurePosixPath(relative_path).suffix.lower(),
        "application/octet-stream",
    )


def _stat_at(name: str, directory_fd: int) -> os.stat_result:
    try:
        return os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except OSError as exc:
        raise ValueError("payload path could not be opened or inspected safely") from exc


def _decoded_file_uri(uri: str) -> str:
    if not isinstance(uri, str) or "\x00" in uri:
        raise ValueError("artifact URI must be NUL-free text")
    parsed = urlsplit(uri)
    if parsed.scheme != "file":
        raise ValueError("artifact URI scheme must be file")
    if parsed.netloc or parsed.query or parsed.fragment:
        raise ValueError("file URI must not contain authority, query, or fragment")
    if _BAD_PERCENT_ESCAPE.search(parsed.path):
        raise ValueError("file URI contains an invalid percent escape")
    try:
        decoded = unquote_to_bytes(parsed.path).decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ValueError("file URI path must be valid UTF-8") from exc
    if (
        not decoded.startswith("/")
        or "\x00" in decoded
        or "\\" in decoded
        or "//" in decoded
        or unicodedata.normalize("NFC", decoded) != decoded
        or any(ord(character) < 32 or ord(character) == 127 for character in decoded)
        or os.path.normpath(decoded) != decoded
    ):
        raise ValueError("file URI must contain a normalized absolute percent-decoded path")
    return decoded


class ArtifactPayloadService:
    """Issue opaque inventories and perform verified contained text reads.

    Issuance does not copy mutable source bytes. Every byte-consuming operation
    must revalidate identity, size, and digest against the issued inventory.
    """

    def __init__(self, allowed_root: str | os.PathLike[str]) -> None:
        if _O_PATH is None:
            raise RuntimeError("artifact payload scanning requires Linux O_PATH support")
        try:
            root = Path(allowed_root).resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ValueError("allowed root must resolve to an existing directory") from exc
        if not root.is_dir():
            raise ValueError("allowed root must resolve to a directory")
        try:
            root_fd = _open_at(root, _READ_BASE_FLAGS | os.O_DIRECTORY)
        except OSError as exc:
            raise ValueError("allowed root could not be opened safely") from exc
        try:
            opened = os.fstat(root_fd)
        except BaseException:
            os.close(root_fd)
            raise
        if not stat.S_ISDIR(opened.st_mode):
            os.close(root_fd)
            raise ValueError("allowed root must be a directory")
        self._allowed_root = os.fspath(root)
        self._root_fd = root_fd
        self._handles: dict[str, _HandleRecord] = {}
        self._attempted_nodes = 0
        self._attempted_files = 0
        self._attempted_bytes = 0
        self._closed = False
        self._lock = threading.RLock()

    def __enter__(self) -> ArtifactPayloadService:
        with self._lock:
            self._require_open()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback
        self.close()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._handles.clear()
            os.close(self._root_fd)

    def issue_snapshot(
        self,
        *,
        artifact_id: str,
        artifact_type: str,
        name: str,
        uri: str,
        manifest: Mapping[str, object],
        scores: Mapping[str, object],
        rank_index: int,
    ) -> TrustedArtifactSnapshot:
        with self._lock:
            return self._issue_snapshot(
                artifact_id=artifact_id,
                artifact_type=artifact_type,
                name=name,
                uri=uri,
                manifest=manifest,
                scores=scores,
                rank_index=rank_index,
            )

    def _issue_snapshot(
        self,
        *,
        artifact_id: str,
        artifact_type: str,
        name: str,
        uri: str,
        manifest: Mapping[str, object],
        scores: Mapping[str, object],
        rank_index: int,
    ) -> TrustedArtifactSnapshot:
        self._require_open()
        if (
            not isinstance(artifact_id, str)
            or not artifact_id.strip()
            or len(artifact_id) > 256
        ):
            raise ValueError("artifact ID must be bounded non-empty text")
        _stable_id(artifact_type)
        if not isinstance(name, str) or not name.strip() or len(name) > 4096:
            raise ValueError("artifact name must be bounded non-empty text")
        if (
            not isinstance(rank_index, int)
            or isinstance(rank_index, bool)
            or not 0 <= rank_index < MAX_HANDLER_ARTIFACTS
        ):
            raise ValueError("artifact rank must be within the handler input budget")
        manifest_json = _bounded_canonical_json_object(
            dict(manifest),
            label="artifact manifest",
        )
        scores_json = _bounded_canonical_json_object(
            dict(scores),
            label="artifact scores",
        )

        decoded_path = _decoded_file_uri(uri)
        path_components = self._contained_components(decoded_path)
        node_fd, node_identity = self._open_relative(path_components)
        records: list[_FileRecord] = []
        directories: list[_DirectoryRecord] = []
        budget = _ScanBudget()
        try:
            mode = node_identity.mode
            if stat.S_ISLNK(mode):
                raise ValueError("payload root must not be a symlink")
            if stat.S_ISDIR(mode):
                budget.nodes += 1
                self._record_attempted(nodes=1)
                if budget.nodes > _MAX_PAYLOAD_NODES:
                    raise ValueError("payload exceeds maximum node budget")
                self._require_attempted_budget()
                root_components = path_components
                self._scan_directory(
                    node_fd,
                    (),
                    records,
                    directories,
                    node_identity,
                    budget,
                )
            elif stat.S_ISREG(mode):
                budget.nodes += 1
                budget.files += 1
                budget.total_bytes += node_identity.size
                self._record_attempted(nodes=1, files=1)
                if budget.nodes > _MAX_PAYLOAD_NODES:
                    raise ValueError("payload exceeds maximum node budget")
                if budget.files > MAX_PAYLOAD_ENTRIES:
                    raise ValueError("payload exceeds maximum entries")
                if budget.total_bytes > MAX_PAYLOAD_TOTAL_BYTES:
                    raise ValueError("payload exceeds maximum total bytes")
                self._require_attempted_budget()
                content_path = manifest.get("content_path")
                if content_path is None:
                    logical_path = path_components[-1] if path_components else Path(decoded_path).name
                else:
                    if not isinstance(content_path, str):
                        raise ValueError("manifest content_path must be a normalized relative path")
                    try:
                        logical_path = validate_relative_path(content_path)
                    except ValueError as exc:
                        raise ValueError(
                            "manifest content_path must be a normalized relative path"
                        ) from exc
                logical_components = tuple(PurePosixPath(logical_path).parts)
                if len(logical_components) > MAX_PAYLOAD_TREE_DEPTH:
                    raise ValueError("payload exceeds maximum tree depth")
                if len(logical_components) > len(path_components):
                    raise ValueError("manifest content_path cannot reconstruct payload root")
                root_components = path_components[: len(path_components) - len(logical_components)]
                if root_components + logical_components != path_components:
                    raise ValueError("manifest content_path does not resolve to artifact URI")
                records.append(
                    self._scan_open_file(node_fd, logical_path, node_identity)
                )
            else:
                raise ValueError("payload root must be a regular file or directory")
        finally:
            os.close(node_fd)

        if not records:
            raise ValueError("payload must contain at least one regular file")
        if sum(record.size_bytes for record in records) > MAX_PAYLOAD_TOTAL_BYTES:
            raise ValueError("payload exceeds maximum total bytes")
        # This stability pass rejects observed scan-time churn. It is not a byte
        # lease: later reads/materialization still revalidate the issued digest.
        self._verify_path_identity(path_components, node_identity, mutation_label="payload root")
        for directory in directories:
            self._verify_path_identity(
                root_components + directory.relative_components,
                directory.identity,
                mutation_label="payload directory drift",
            )
        for record in records:
            self._verify_path_identity(
                root_components + tuple(PurePosixPath(record.relative_path).parts),
                record.identity,
                mutation_label="payload file drift",
            )
        entries = tuple(
            PayloadManifestEntry(
                relative_path=record.relative_path,
                media_type=_media_type(record.relative_path),
                size_bytes=record.size_bytes,
                sha256=record.sha256,
            )
            for record in sorted(records, key=lambda item: item.relative_path)
        )
        handle = self._allocate_handle()
        snapshot = TrustedArtifactSnapshot(
            artifact_id=artifact_id,
            artifact_type=artifact_type,
            name=name,
            uri_scheme="file",
            payload_handle=handle,
            payload_entries=entries,
            payload_manifest_digest=payload_tree_digest(entries),
            manifest_json=manifest_json,
            scores_json=scores_json,
            rank_index=rank_index,
        )
        self._handles[handle] = _HandleRecord(
            root_components=root_components,
            files={record.relative_path: record for record in records},
        )
        return snapshot

    def read_utf8_prefix(
        self,
        payload_handle: str,
        relative_path: str,
        *,
        max_chars: int,
        max_bytes: int,
    ) -> str:
        with self._lock:
            return self._read_utf8_prefix(
                payload_handle,
                relative_path,
                max_chars=max_chars,
                max_bytes=max_bytes,
            )

    def _read_utf8_prefix(
        self,
        payload_handle: str,
        relative_path: str,
        *,
        max_chars: int,
        max_bytes: int,
    ) -> str:
        self._require_open()
        if not (
            isinstance(max_chars, int)
            and not isinstance(max_chars, bool)
            and isinstance(max_bytes, int)
            and not isinstance(max_bytes, bool)
            and 0 <= max_chars <= MAX_CONTRIBUTION_TEXT
            and 0 <= max_bytes <= MAX_CONTRIBUTION_TEXT
        ):
            raise ValueError("text read limits must be integers from zero to MAX_CONTRIBUTION_TEXT")
        try:
            handle = self._handles[payload_handle]
        except (KeyError, TypeError) as exc:
            raise ValueError("unknown payload handle") from exc
        try:
            normalized_path = validate_relative_path(relative_path)
            expected = handle.files[normalized_path]
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("unknown payload path for handle") from exc

        self._consume_attempted(nodes=1, files=1)
        components = handle.root_components + tuple(PurePosixPath(normalized_path).parts)
        fd, opened_identity = self._open_relative(components)
        digest = hashlib.sha256()
        size = 0
        decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")
        prefix: list[str] = []
        prefix_chars = 0
        prefix_bytes = 0
        collecting = True
        try:
            if not stat.S_ISREG(opened_identity.mode) or opened_identity != expected.identity:
                raise ValueError("payload file identity drifted from issued inventory")
            try:
                for chunk in _stream_fd_chunks(fd):
                    self._consume_attempted(total_bytes=len(chunk))
                    digest.update(chunk)
                    size += len(chunk)
                    if size > expected.size_bytes:
                        raise ValueError("payload file size drifted from issued inventory")
                    decoded = decoder.decode(chunk, final=False)
                    if collecting:
                        for character in decoded:
                            encoded_size = len(character.encode("utf-8"))
                            if prefix_chars + 1 > max_chars or prefix_bytes + encoded_size > max_bytes:
                                collecting = False
                                break
                            prefix.append(character)
                            prefix_chars += 1
                            prefix_bytes += encoded_size
                tail = decoder.decode(b"", final=True)
            except UnicodeDecodeError as exc:
                raise ValueError("payload file is not valid UTF-8") from exc
            if collecting:
                for character in tail:
                    encoded_size = len(character.encode("utf-8"))
                    if prefix_chars + 1 > max_chars or prefix_bytes + encoded_size > max_bytes:
                        break
                    prefix.append(character)
                    prefix_chars += 1
                    prefix_bytes += encoded_size
            after_read = _identity(os.fstat(fd))
        finally:
            os.close(fd)

        if (
            after_read != expected.identity
            or size != expected.size_bytes
            or digest.hexdigest() != expected.sha256
        ):
            raise ValueError("payload file digest or identity drifted from issued inventory")
        self._verify_path_identity(components, expected.identity, mutation_label="payload file drift")
        return "".join(prefix)

    def _require_open(self) -> None:
        if self._closed:
            raise RuntimeError("artifact payload service is closed")

    def _consume_attempted(
        self,
        *,
        nodes: int = 0,
        files: int = 0,
        total_bytes: int = 0,
    ) -> None:
        """Consume request-wide scan resources without rollback on failure."""

        self._record_attempted(
            nodes=nodes,
            files=files,
            total_bytes=total_bytes,
        )
        self._require_attempted_budget()

    def _record_attempted(
        self,
        *,
        nodes: int = 0,
        files: int = 0,
        total_bytes: int = 0,
    ) -> None:
        self._attempted_nodes += nodes
        self._attempted_files += files
        self._attempted_bytes += total_bytes

    def _require_attempted_budget(self) -> None:
        if self._attempted_nodes > _MAX_PAYLOAD_NODES:
            raise ArtifactPayloadBudgetExceeded(
                "payload service exceeds aggregate node budget"
            )
        if self._attempted_files > MAX_PAYLOAD_ENTRIES:
            raise ArtifactPayloadBudgetExceeded(
                "payload service exceeds aggregate entries"
            )
        if self._attempted_bytes > MAX_PAYLOAD_TOTAL_BYTES:
            raise ArtifactPayloadBudgetExceeded(
                "payload service exceeds aggregate total bytes"
            )

    def _allocate_handle(self) -> str:
        for _ in range(_MAX_HANDLE_ATTEMPTS):
            handle = f"payload-{secrets.token_hex(24)}"
            if handle not in self._handles:
                return handle
        raise RuntimeError("could not allocate a unique payload handle")

    def _contained_components(self, decoded_path: str) -> tuple[str, ...]:
        real_path = os.path.realpath(decoded_path)
        try:
            lexical_common = os.path.commonpath((self._allowed_root, decoded_path))
            real_common = os.path.commonpath((self._allowed_root, real_path))
        except ValueError as exc:
            raise ValueError("artifact payload is outside allowed root") from exc
        if lexical_common != self._allowed_root or real_common != self._allowed_root:
            raise ValueError("artifact payload is outside allowed root")
        relative = os.path.relpath(decoded_path, self._allowed_root)
        return () if relative == "." else tuple(PurePosixPath(relative).parts)

    def _open_relative(self, components: tuple[str, ...]) -> tuple[int, _FileIdentity]:
        current_fd: int | None = _open_at(
            ".", _READ_BASE_FLAGS | os.O_DIRECTORY, dir_fd=self._root_fd
        )
        try:
            if not components:
                current_identity = _identity(os.fstat(current_fd))
                result_fd = current_fd
                current_fd = None
                return result_fd, current_identity
            for index, component in enumerate(components):
                before = _stat_at(component, current_fd)
                if stat.S_ISLNK(before.st_mode):
                    raise ValueError("payload path must not contain a symlink")
                is_final = index == len(components) - 1
                if is_final and not (
                    stat.S_ISREG(before.st_mode) or stat.S_ISDIR(before.st_mode)
                ):
                    raise ValueError("payload root must be a regular file or directory")
                if not is_final and not stat.S_ISDIR(before.st_mode):
                    raise ValueError("payload path parent must be a directory")
                expected_identity = _identity(before)
                next_fd = self._open_verified_node(
                    current_fd,
                    component,
                    expected_identity,
                    directory=stat.S_ISDIR(before.st_mode),
                )
                opened = expected_identity
                parent_fd = current_fd
                current_fd = None
                try:
                    os.close(parent_fd)
                except BaseException:
                    _close_untransferred_fd(next_fd)
                    raise
                current_fd = next_fd
            result_fd = current_fd
            current_fd = None
            return result_fd, opened
        finally:
            if current_fd is not None:
                _close_untransferred_fd(current_fd)

    def _scan_directory(
        self,
        directory_fd: int,
        prefix: tuple[str, ...],
        records: list[_FileRecord],
        directories: list[_DirectoryRecord],
        initial_identity: _FileIdentity,
        budget: _ScanBudget,
    ) -> None:
        try:
            names: list[str] = []
            with os.scandir(directory_fd) as entries:
                for entry in entries:
                    budget.nodes += 1
                    self._record_attempted(nodes=1)
                    if budget.nodes > _MAX_PAYLOAD_NODES:
                        raise ValueError("payload exceeds maximum nodes or entries")
                    self._require_attempted_budget()
                    names.append(entry.name)
        except OSError as exc:
            raise ValueError("payload directory could not be listed") from exc
        for name in sorted(names):
            relative_parts = prefix + (name,)
            relative_path = "/".join(relative_parts)
            try:
                validate_relative_path(relative_path)
            except ValueError as exc:
                raise ValueError("payload contains a non-canonical entry path") from exc
            if len(relative_parts) > MAX_PAYLOAD_TREE_DEPTH:
                raise ValueError("payload exceeds maximum tree depth")
            before = _stat_at(name, directory_fd)
            if stat.S_ISLNK(before.st_mode):
                raise ValueError("payload must not contain a descendant symlink")
            before_identity = _identity(before)
            if stat.S_ISDIR(before.st_mode):
                directories.append(
                    _DirectoryRecord(
                        relative_components=relative_parts,
                        identity=before_identity,
                    )
                )
                child_fd = self._open_child(directory_fd, name, before_identity, directory=True)
                try:
                    self._scan_directory(
                        child_fd,
                        relative_parts,
                        records,
                        directories,
                        before_identity,
                        budget,
                    )
                    after_fd = _identity(os.fstat(child_fd))
                finally:
                    os.close(child_fd)
                after_path = _identity(_stat_at(name, directory_fd))
                if after_fd != before_identity or after_path != before_identity:
                    raise ValueError("payload directory mutated during scan")
            elif stat.S_ISREG(before.st_mode):
                budget.files += 1
                self._record_attempted(files=1)
                if budget.files > MAX_PAYLOAD_ENTRIES:
                    raise ValueError("payload exceeds maximum entries")
                self._require_attempted_budget()
                if before.st_size > MAX_PAYLOAD_ENTRY_BYTES:
                    raise ValueError("payload file exceeds maximum entry bytes")
                budget.total_bytes += before.st_size
                if budget.total_bytes > MAX_PAYLOAD_TOTAL_BYTES:
                    raise ValueError("payload exceeds maximum total bytes")
                child_fd = self._open_child(directory_fd, name, before_identity, directory=False)
                try:
                    record = self._scan_open_file(child_fd, relative_path, before_identity)
                finally:
                    os.close(child_fd)
                after_path = _identity(_stat_at(name, directory_fd))
                if after_path != before_identity:
                    raise ValueError("payload file mutated during scan")
                records.append(record)
            else:
                raise ValueError("payload entries must be a regular file or directory")
        if _identity(os.fstat(directory_fd)) != initial_identity:
            raise ValueError("payload directory mutated during scan")

    def _open_child(
        self,
        parent_fd: int,
        name: str,
        expected: _FileIdentity,
        *,
        directory: bool,
    ) -> int:
        return self._open_verified_node(
            parent_fd,
            name,
            expected,
            directory=directory,
        )

    def _open_verified_node(
        self,
        parent_fd: int,
        name: str,
        expected: _FileIdentity,
        *,
        directory: bool,
    ) -> int:
        if _O_PATH is None:  # Constructor already rejects this platform.
            raise RuntimeError("artifact payload scanning requires Linux O_PATH support")
        path_flags = _O_PATH | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
        try:
            path_fd = _open_at(name, path_flags, dir_fd=parent_fd)
        except OSError as exc:
            raise ValueError("payload entry could not be fixed without following links") from exc
        readable_fd: int | None = None
        try:
            fixed_identity = _identity(os.fstat(path_fd))
            if fixed_identity != expected:
                raise ValueError("payload entry mutated while being fixed")
            if stat.S_ISREG(fixed_identity.mode) and fixed_identity.link_count != 1:
                raise ValueError("payload regular file must have link count one")
            if directory != stat.S_ISDIR(fixed_identity.mode):
                raise ValueError("payload entry type mutated while being fixed")
            if not directory and not stat.S_ISREG(fixed_identity.mode):
                raise ValueError("payload entry is not a regular file")
            try:
                if directory:
                    readable_fd = _open_at(
                        ".",
                        _READ_BASE_FLAGS | os.O_DIRECTORY,
                        dir_fd=path_fd,
                    )
                else:
                    readable_fd = _open_at(
                        f"/proc/self/fd/{path_fd}",
                        _READ_FIXED_FD_FLAGS,
                    )
            except OSError as exc:
                raise ValueError("fixed payload entry could not be opened for reading") from exc
            readable_identity = _identity(os.fstat(readable_fd))
            if readable_identity != expected:
                raise ValueError("payload entry mutated while being opened")
            owned_path_fd = path_fd
            path_fd = -1
            os.close(owned_path_fd)
            result_fd = readable_fd
            readable_fd = None
            return result_fd
        finally:
            if readable_fd is not None:
                _close_untransferred_fd(readable_fd)
            if path_fd >= 0:
                _close_untransferred_fd(path_fd)

    def _scan_open_file(
        self,
        fd: int,
        relative_path: str,
        initial_identity: _FileIdentity,
    ) -> _FileRecord:
        if initial_identity.size > MAX_PAYLOAD_ENTRY_BYTES:
            raise ValueError("payload file exceeds maximum entry bytes")
        digest = hashlib.sha256()
        size = 0
        for chunk in _stream_fd_chunks(fd):
            self._consume_attempted(total_bytes=len(chunk))
            digest.update(chunk)
            size += len(chunk)
            if size > initial_identity.size:
                raise ValueError("payload file mutated during scan")
            if size > MAX_PAYLOAD_ENTRY_BYTES:
                raise ValueError("payload file exceeds maximum entry bytes")
        after = _identity(os.fstat(fd))
        if after != initial_identity or size != initial_identity.size:
            raise ValueError("payload file mutated during scan")
        return _FileRecord(
            relative_path=relative_path,
            identity=initial_identity,
            size_bytes=size,
            sha256=digest.hexdigest(),
        )

    def _verify_path_identity(
        self,
        components: tuple[str, ...],
        expected: _FileIdentity,
        *,
        mutation_label: str,
    ) -> None:
        fd, current = self._open_relative(components)
        os.close(fd)
        if current != expected:
            raise ValueError(f"{mutation_label} mutated")


__all__ = ["ArtifactPayloadBudgetExceeded", "ArtifactPayloadService"]
