"""Internal atomic materialization of generic context projections."""

from __future__ import annotations

import ctypes
import errno
import hashlib
import io
import json
import os
import posixpath
import secrets
import stat
import sys
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import Field, field_validator, model_validator

from openevo.evolution.artifact_payloads import ArtifactPayloadService
from openevo.evolution.context_projection import (
    ContextProjectionResolveRequest,
    ContextProjectionResolveResponse,
    ContextProjectionSelection,
)
from openevo.evolution.framework.contracts import (
    MAX_CONTRIBUTION_TEXT,
    MAX_PAYLOAD_TOTAL_BYTES,
    DestinationScope,
    EnvironmentValueKind,
    PayloadKind,
    _Contract,
    _digest,
    _environment_name,
    _mime_type,
    _stable_id,
    canonical_digest,
    canonical_json,
    paths_conflict,
    validate_relative_path,
)
from openevo.evolution.framework.contributions import (
    InlineTextPayloadContribution,
    StagedPayloadContribution,
    TargetHandlerOutput,
)
from openevo.evolution.framework.builtins import (
    VerifiedExecutableRegistry,
    require_verified_executable_registry,
)
from openevo.evolution.framework.handlers import (
    PayloadManifestEntry,
    TrustedArtifactSnapshot,
    payload_entries_under_root,
    payload_tree_digest,
    payload_tree_size,
)


MAX_CONTEXT_BLOBS = 4096
MAX_CONTEXT_ENVIRONMENT_BINDINGS = 4096
MAX_CONTEXT_ENVIRONMENT_BYTES = MAX_CONTRIBUTION_TEXT
MAX_CONTEXT_INSTRUCTION_CHARS = MAX_CONTRIBUTION_TEXT
MAX_CONTEXT_INSTRUCTION_BYTES = MAX_CONTRIBUTION_TEXT
MAX_CONTEXT_MANIFEST_BYTES = 64 * 1024 * 1024
_PRESERVED_ENTRY_PREFIX = ".openevo-preserved-"
_QUARANTINE_ENTRY_PREFIX = ".openevo-quarantine-"
# Kept so startup leaves tombstones created by the earlier cleanup implementation alone.
_TRASH_ENTRY_PREFIX = ".openevo-trash-"
_RENAME_NOREPLACE = 1

MaterializedEntryRemovalResult = Literal[
    "missing",
    "mismatch",
    "preserved",
]


class MaterializedBlob(_Contract):
    """One private blob, addressed without exposing its Core filesystem path."""

    blob_id: str
    target_id: str
    handler_id: str
    contribution_id: str
    source_artifact_ids: tuple[str, ...] = Field(min_length=1, max_length=128)
    destination_scope: DestinationScope
    destination_relative_path: str
    media_type: str
    size_bytes: int = Field(ge=0, le=MAX_PAYLOAD_TOTAL_BYTES)
    sha256: str

    _ids = field_validator("blob_id", "target_id", "handler_id", "contribution_id")(_stable_id)
    _destination = field_validator("destination_relative_path")(validate_relative_path)
    _mime = field_validator("media_type")(_mime_type)
    _sha = field_validator("sha256")(_digest)


class MaterializedEnvironmentBinding(_Contract):
    target_id: str
    handler_id: str
    name: str
    value_kind: EnvironmentValueKind
    value: str = Field(max_length=MAX_CONTEXT_ENVIRONMENT_BYTES)
    contribution_ids: tuple[str, ...] = Field(default=(), max_length=256)
    destination_scope: DestinationScope | None = None

    _ids = field_validator("target_id", "handler_id")(_stable_id)
    _name = field_validator("name")(_environment_name)


class MaterializedAdapter(_Contract):
    target_id: str
    handler_id: str
    contribution_id: str
    source_artifact_id: str = Field(min_length=1, max_length=256)
    source_payload_digest: str
    source_size_bytes: int = Field(ge=0, le=MAX_PAYLOAD_TOTAL_BYTES)
    adapter_id: str
    adapter_format: str
    base_model: str
    weight: float = Field(gt=0.0, le=100.0)

    _ids = field_validator(
        "target_id", "handler_id", "contribution_id", "adapter_id", "adapter_format"
    )(_stable_id)
    _digest = field_validator("source_payload_digest")(_digest)


class MaterializedAdapterMergeSpec(_Contract):
    base_model: str | None = None
    merge_mode: Literal["reference_only", "runtime_lora"] = "reference_only"
    adapters: tuple[MaterializedAdapter, ...] = Field(default=(), max_length=128)

    @model_validator(mode="after")
    def _mode_matches_adapters(self) -> MaterializedAdapterMergeSpec:
        expected = "runtime_lora" if self.adapters else "reference_only"
        if self.merge_mode != expected:
            raise ValueError("adapter merge mode does not match adapter contributions")
        return self


class MaterializedContext(_Contract):
    materialization_contract_version: Literal["1"] = "1"
    context_id: str
    request_digest: str
    registry_digest: str
    successor_transition_id: str | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    predecessor_project_head_id: str | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    base_model: str | None = None
    projections: tuple[TargetHandlerOutput, ...] = Field(default=(), max_length=128)
    selection: ContextProjectionSelection
    blobs: tuple[MaterializedBlob, ...] = Field(default=(), max_length=MAX_CONTEXT_BLOBS)
    environment: tuple[MaterializedEnvironmentBinding, ...] = Field(
        default=(), max_length=MAX_CONTEXT_ENVIRONMENT_BINDINGS
    )
    instruction: str = Field(default="", max_length=MAX_CONTEXT_INSTRUCTION_CHARS)
    adapter_merge_spec: MaterializedAdapterMergeSpec

    _context = field_validator("context_id")(_stable_id)
    _digests = field_validator("request_digest", "registry_digest")(_digest)

    @field_validator("successor_transition_id", "predecessor_project_head_id")
    @classmethod
    def _optional_successor_identity(cls, value: str | None) -> str | None:
        return None if value is None else _stable_id(value)

    @model_validator(mode="after")
    def _unique_blob_and_environment_ids(self) -> MaterializedContext:
        if (self.successor_transition_id is None) != (
            self.predecessor_project_head_id is None
        ):
            raise ValueError(
                "successor transition and predecessor project head must be provided together"
            )
        blob_ids = tuple(item.blob_id for item in self.blobs)
        if len(blob_ids) != len(set(blob_ids)):
            raise ValueError("materialized blob IDs must be unique")
        names = tuple(item.name for item in self.environment)
        if len(names) != len(set(names)):
            raise ValueError("materialized environment names must be unique")
        return self


@dataclass(frozen=True, slots=True)
class MaterializedBlobLease:
    """One verified blob held open without exposing its Core host path."""

    blob: MaterializedBlob
    stream: MaterializedBlobStream


class MaterializedBlobStream:
    """Controlled read-only stream that deliberately exposes no file descriptor."""

    __slots__ = ("__stream",)

    def __init__(self, descriptor: int) -> None:
        self.__stream = io.FileIO(descriptor, mode="rb", closefd=True)

    @property
    def closed(self) -> bool:
        return self.__stream.closed

    def read(self, size: int = -1) -> bytes:
        return self.__stream.read(size)

    def readinto(self, buffer) -> int | None:
        return self.__stream.readinto(buffer)

    def seek(self, offset: int, whence: int = os.SEEK_SET) -> int:
        return self.__stream.seek(offset, whence)

    def tell(self) -> int:
        return self.__stream.tell()

    def readable(self) -> bool:
        return self.__stream.readable()

    def seekable(self) -> bool:
        return self.__stream.seekable()

    def close(self) -> None:
        self.__stream.close()

    def fileno(self) -> int:
        raise io.UnsupportedOperation("materialized blob stream does not expose its fd")


@dataclass(frozen=True, slots=True)
class _BlobPlan:
    blob: MaterializedBlob
    payload_handle: str | None
    source_relative_path: str | None
    inline_bytes: bytes | None


@dataclass(frozen=True, slots=True)
class _PrivateFileIdentity:
    device: int
    inode: int
    mode: int
    link_count: int
    size: int
    mtime_ns: int
    ctime_ns: int


@dataclass(frozen=True, slots=True)
class ContextPublicationReceipt:
    """Ephemeral capability binding one publication to its verified filesystem objects."""

    materialized_context: MaterializedContext
    canonical_manifest_bytes: bytes
    context_directory_identity: _PrivateFileIdentity
    blobs_directory_identity: _PrivateFileIdentity
    blob_identities: tuple[tuple[str, _PrivateFileIdentity], ...]
    _issuer: object

    def __reduce_ex__(self, _protocol: int):
        raise TypeError("context publication receipts are ephemeral and cannot be persisted")


def _private_file_identity(value: os.stat_result) -> _PrivateFileIdentity:
    return _PrivateFileIdentity(
        device=value.st_dev,
        inode=value.st_ino,
        mode=value.st_mode,
        link_count=value.st_nlink,
        size=value.st_size,
        mtime_ns=value.st_mtime_ns,
        ctime_ns=value.st_ctime_ns,
    )


def _open_materialized_blob_file(directory_fd: int, blob_id: str) -> int:
    """Private open hook used by deterministic publish-race tests."""

    return os.open(
        blob_id,
        os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
        dir_fd=directory_fd,
    )


def _after_materialized_context_rename(root_descriptor: int, context_id: str) -> None:
    """Private no-op hook used by deterministic final-path race tests."""

    del root_descriptor, context_id


def _before_materialized_context_rename(root_descriptor: int, context_id: str) -> None:
    """Private no-op hook used by deterministic publication-collision tests."""

    del root_descriptor, context_id


def _after_materialized_manifest_write(temporary_descriptor: int) -> None:
    """Private no-op hook used by deterministic manifest-race tests."""

    del temporary_descriptor


def _before_materialized_context_discard(
    root_descriptor: int,
    context_id: str,
) -> None:
    """Private no-op hook used by deterministic discard-race tests."""

    del root_descriptor, context_id


def _before_temporary_context_cleanup(
    root_descriptor: int,
    temporary_name: str,
) -> None:
    """Private no-op hook used by deterministic cleanup-race tests."""

    del root_descriptor, temporary_name


def _registered_object(row: Mapping[str, object], field: str) -> dict[str, object]:
    encoded = row.get(field)
    if not isinstance(encoded, str) or not encoded:
        raise ValueError(f"selected artifact {field} is missing")
    try:
        value = json.loads(encoded)
    except ValueError as exc:
        raise ValueError(f"selected artifact {field} is invalid") from exc
    if not isinstance(value, dict):
        raise ValueError(f"selected artifact {field} must be an object")
    canonical_json(value)
    return value


def _new_blob_id(existing: set[str]) -> str:
    for _ in range(128):
        candidate = f"blob-{secrets.token_hex(24)}"
        if candidate not in existing:
            existing.add(candidate)
            return candidate
    raise RuntimeError("could not allocate a unique materialized blob ID")


def _runtime_path(
    request: ContextProjectionResolveRequest,
    scope: DestinationScope,
    relative_path: str | None = None,
) -> str:
    root = getattr(request.destination_roots, scope.value)
    return root if relative_path is None else posixpath.join(root, relative_path)


def _private_entry_name(prefix: str) -> str:
    return f"{prefix}{secrets.token_hex(24)}"


def _rename_noreplace(
    source: str,
    destination: str,
    *,
    directory_fd: int,
) -> None:
    if sys.platform != "linux":
        raise OSError(errno.ENOSYS, "safe no-replace rename requires Linux renameat2")
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise OSError(errno.ENOSYS, "safe no-replace rename requires renameat2")
    renameat2.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    renameat2.restype = ctypes.c_int
    result = renameat2(
        directory_fd,
        os.fsencode(source),
        directory_fd,
        os.fsencode(destination),
        _RENAME_NOREPLACE,
    )
    if result != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), destination)


def _preserve_materialized_entry(root_descriptor: int, name: str) -> None:
    for _ in range(128):
        preserved = _private_entry_name(_PRESERVED_ENTRY_PREFIX)
        try:
            os.rename(
                name,
                preserved,
                src_dir_fd=root_descriptor,
                dst_dir_fd=root_descriptor,
            )
        except FileNotFoundError:
            return
        except FileExistsError:
            continue
        os.fsync(root_descriptor)
        return
    raise RuntimeError("could not allocate a preserved materialization name")


def _same_filesystem_object(
    left: _PrivateFileIdentity,
    right: _PrivateFileIdentity,
) -> bool:
    return (
        left.device,
        left.inode,
        stat.S_IFMT(left.mode),
    ) == (
        right.device,
        right.inode,
        stat.S_IFMT(right.mode),
    )


def _open_quarantined_entry(
    root_descriptor: int,
    name: str,
    identity: _PrivateFileIdentity,
) -> int:
    flags = os.O_CLOEXEC | os.O_NOFOLLOW
    if stat.S_ISDIR(identity.mode):
        flags |= os.O_RDONLY | os.O_DIRECTORY
    elif stat.S_ISREG(identity.mode):
        flags |= os.O_WRONLY | os.O_NONBLOCK
    else:
        path_flag = getattr(os, "O_PATH", None)
        if path_flag is None:
            raise ValueError("platform cannot safely hold a quarantined non-directory")
        flags |= path_flag
    descriptor = os.open(name, flags, dir_fd=root_descriptor)
    try:
        opened = _private_file_identity(os.fstat(descriptor))
        if not _same_filesystem_object(opened, identity):
            raise ValueError("quarantined materialization identity changed")
        observed = _private_file_identity(
            os.stat(name, dir_fd=root_descriptor, follow_symlinks=False)
        )
        if not _same_filesystem_object(observed, opened):
            raise ValueError("quarantined materialization path identity changed")
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _truncate_quarantined_regular_file(
    descriptor: int,
    expected: _PrivateFileIdentity,
) -> None:
    opened = _private_file_identity(os.fstat(descriptor))
    if (
        not _same_filesystem_object(opened, expected)
        or not stat.S_ISREG(opened.mode)
        or opened.link_count != 1
    ):
        return
    os.ftruncate(descriptor, 0)
    os.fsync(descriptor)


def _sanitize_quarantined_directory(directory_descriptor: int) -> None:
    """Best-effort truncate fixed regular files without deleting directory entries."""

    for name in os.listdir(directory_descriptor):
        try:
            identity = _private_file_identity(
                os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
            )
        except OSError:
            continue
        if stat.S_ISDIR(identity.mode):
            try:
                child_descriptor = os.open(
                    name,
                    os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_DIRECTORY,
                    dir_fd=directory_descriptor,
                )
            except OSError:
                continue
            try:
                opened = _private_file_identity(os.fstat(child_descriptor))
                if _same_filesystem_object(opened, identity):
                    _sanitize_quarantined_directory(child_descriptor)
            finally:
                os.close(child_descriptor)
            continue

        if not stat.S_ISREG(identity.mode) or identity.link_count != 1:
            continue
        try:
            child_descriptor = os.open(
                name,
                os.O_WRONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
                dir_fd=directory_descriptor,
            )
        except OSError:
            continue
        try:
            _truncate_quarantined_regular_file(child_descriptor, identity)
        finally:
            os.close(child_descriptor)
    os.fsync(directory_descriptor)


def _is_materialization_quarantine_name(name: str) -> bool:
    return name.startswith((_QUARANTINE_ENTRY_PREFIX, _TRASH_ENTRY_PREFIX))


def _remove_materialized_entry_if_identity(
    root_descriptor: int,
    name: str,
    expected: _PrivateFileIdentity,
    *,
    preserve_mismatch: bool = True,
) -> MaterializedEntryRemovalResult:
    """Move one fixed entry to quarantine and never delete through a pathname."""

    if _is_materialization_quarantine_name(name):
        # Startup may enumerate these again. They are maintenance-owned tombstones,
        # not candidates for another rename or automatic pathname cleanup.
        return "preserved"

    try:
        current = _private_file_identity(
            os.stat(name, dir_fd=root_descriptor, follow_symlinks=False)
        )
    except FileNotFoundError:
        return "missing"
    if current != expected:
        if preserve_mismatch:
            _preserve_materialized_entry(root_descriptor, name)
        return "mismatch"

    quarantine = _private_entry_name(_QUARANTINE_ENTRY_PREFIX)
    try:
        os.rename(
            name,
            quarantine,
            src_dir_fd=root_descriptor,
            dst_dir_fd=root_descriptor,
        )
    except FileNotFoundError:
        return "missing"
    except OSError:
        return "preserved"
    os.fsync(root_descriptor)
    quarantine_descriptor: int | None = None
    try:
        try:
            moved = _private_file_identity(
                os.stat(quarantine, dir_fd=root_descriptor, follow_symlinks=False)
            )
        except OSError:
            return "preserved"
        if not _same_filesystem_object(moved, expected) or moved.link_count != expected.link_count:
            return "preserved"
        quarantine_descriptor = _open_quarantined_entry(
            root_descriptor,
            quarantine,
            moved,
        )
        if stat.S_ISDIR(moved.mode):
            _sanitize_quarantined_directory(quarantine_descriptor)
        elif stat.S_ISREG(moved.mode):
            _truncate_quarantined_regular_file(quarantine_descriptor, moved)
        return "preserved"
    except (OSError, ValueError):
        return "preserved"
    finally:
        if quarantine_descriptor is not None:
            os.close(quarantine_descriptor)


class ContextMaterializer:
    """Reissue projection snapshots and atomically publish private context blobs."""

    def __init__(
        self,
        artifact_root: str | os.PathLike[str],
        materialization_root: str | os.PathLike[str],
        executable_registry: VerifiedExecutableRegistry,
    ) -> None:
        self._artifact_root = Path(artifact_root)
        self._materialization_root = Path(materialization_root)
        self._registry = require_verified_executable_registry(executable_registry)
        self._publication_issuer = object()

    def materialize(
        self,
        request: ContextProjectionResolveRequest,
        response: ContextProjectionResolveResponse,
        promoted_rows: Sequence[Mapping[str, object]],
        *,
        materialization_root_descriptor: int,
    ) -> MaterializedContext:
        """Compatibility API returning only the published context model."""

        return self.materialize_for_publication(
            request,
            response,
            promoted_rows,
            materialization_root_descriptor=materialization_root_descriptor,
        ).materialized_context

    def materialize_for_publication(
        self,
        request: ContextProjectionResolveRequest,
        response: ContextProjectionResolveResponse,
        promoted_rows: Sequence[Mapping[str, object]],
        *,
        materialization_root_descriptor: int,
    ) -> ContextPublicationReceipt:
        """Publish a bundle and return its ephemeral precommit capability."""

        self._validate_projection_pair(request, response)
        row_by_id = self._selected_rows(response, promoted_rows)
        needed_ids = {
            payload.source_artifact_id
            for projection in response.projections
            for payload in projection.staged_payloads
            if isinstance(payload, StagedPayloadContribution)
        }
        needed_ids.update(
            adapter.source_artifact_id
            for projection in response.projections
            for adapter in projection.adapters
        )

        with ArtifactPayloadService(self._artifact_root) as payloads:
            snapshots: dict[str, TrustedArtifactSnapshot] = {}
            for artifact_id in response.selection.artifact_ids:
                if artifact_id not in needed_ids:
                    continue
                row = row_by_id[artifact_id]
                snapshots[artifact_id] = payloads.issue_snapshot(
                    artifact_id=artifact_id,
                    artifact_type=self._required_text(row, "type"),
                    name=self._required_text(row, "name"),
                    uri=self._required_text(row, "uri"),
                    manifest=_registered_object(row, "manifest_json"),
                    scores=_registered_object(row, "scores_json"),
                    rank_index=0,
                )

            plans, destinations = self._plan_blobs(response, snapshots)
            adapters = self._plan_adapters(response, snapshots)
            self._validate_destinations(plans)
            instruction = self._materialize_instruction(response)
            environment = self._materialize_environment(request, response, destinations)
            total_bytes = (
                sum(plan.blob.size_bytes for plan in plans)
                + sum(adapter.source_size_bytes for adapter in adapters)
                + len(instruction.encode("utf-8"))
                + sum(len(binding.value.encode("utf-8")) for binding in environment)
            )
            if total_bytes > MAX_PAYLOAD_TOTAL_BYTES:
                raise ValueError("context exceeds maximum decoded/materialized bytes")

            result = MaterializedContext(
                context_id=response.context_id,
                request_digest=response.request_digest,
                registry_digest=response.registry_digest,
                successor_transition_id=request.successor_transition_id,
                predecessor_project_head_id=request.predecessor_project_head_id,
                base_model=response.base_model,
                projections=response.projections,
                selection=response.selection,
                blobs=tuple(plan.blob for plan in plans),
                environment=environment,
                instruction=instruction,
                adapter_merge_spec=MaterializedAdapterMergeSpec(
                    base_model=response.base_model,
                    merge_mode="runtime_lora" if adapters else "reference_only",
                    adapters=adapters,
                ),
            )
            return self._publish(
                result,
                plans,
                snapshots,
                payloads,
                materialization_root_descriptor,
            )

    def verify_publication(
        self,
        receipt: ContextPublicationReceipt,
        *,
        materialization_root_descriptor: int,
    ) -> None:
        """Fail closed unless the receipt still names the exact published objects."""

        self._require_publication_receipt(receipt)
        self._require_materialization_root_descriptor(materialization_root_descriptor)
        self._verify_context_bundle(
            materialization_root_descriptor,
            receipt.materialized_context,
            receipt.canonical_manifest_bytes,
            context_identity=receipt.context_directory_identity,
            blobs_identity=receipt.blobs_directory_identity,
            blob_identities=dict(receipt.blob_identities),
            label="publication",
        )

    def discard_publication(
        self,
        receipt: ContextPublicationReceipt,
        *,
        materialization_root_descriptor: int,
    ) -> MaterializedEntryRemovalResult:
        """Discard only the context directory bound by an ephemeral publication receipt."""

        self._require_publication_receipt(receipt)
        root_descriptor = materialization_root_descriptor
        self._require_materialization_root_descriptor(root_descriptor)
        context_id = receipt.materialized_context.context_id
        _before_materialized_context_discard(root_descriptor, context_id)
        return _remove_materialized_entry_if_identity(
            root_descriptor,
            context_id,
            receipt.context_directory_identity,
            preserve_mismatch=False,
        )

    def verify_persisted_materialization(
        self,
        expected_manifest: MaterializedContext,
        *,
        materialization_root_descriptor: int,
    ) -> None:
        """Verify one DB-authorized bundle relative to the Store's locked root FD."""

        manifest = MaterializedContext.model_validate(expected_manifest)
        manifest_bytes = canonical_json(manifest).encode("utf-8")
        if len(manifest_bytes) > MAX_CONTEXT_MANIFEST_BYTES:
            raise ValueError("persisted materialized context manifest exceeds its byte limit")
        self._require_materialization_root_descriptor(materialization_root_descriptor)
        self._verify_context_bundle(
            materialization_root_descriptor,
            manifest,
            manifest_bytes,
            context_identity=None,
            blobs_identity=None,
            blob_identities=None,
            label="persisted",
        )

    def _require_publication_receipt(self, receipt: ContextPublicationReceipt) -> None:
        if not isinstance(receipt, ContextPublicationReceipt):
            raise TypeError("publication receipt is invalid")
        if receipt._issuer is not self._publication_issuer:
            raise ValueError("publication receipt was not issued by this materializer")
        manifest = MaterializedContext.model_validate(receipt.materialized_context)
        expected_bytes = canonical_json(manifest).encode("utf-8")
        if receipt.canonical_manifest_bytes != expected_bytes:
            raise ValueError("publication receipt manifest binding is invalid")
        expected_blob_ids = tuple(blob.blob_id for blob in manifest.blobs)
        receipt_blob_ids = tuple(blob_id for blob_id, _identity in receipt.blob_identities)
        if receipt_blob_ids != expected_blob_ids:
            raise ValueError("publication receipt blob identity binding is invalid")

    def _verify_context_bundle(
        self,
        root_descriptor: int,
        manifest: MaterializedContext,
        manifest_bytes: bytes,
        *,
        context_identity: _PrivateFileIdentity | None,
        blobs_identity: _PrivateFileIdentity | None,
        blob_identities: Mapping[str, _PrivateFileIdentity] | None,
        label: str,
    ) -> None:
        context_descriptor: int | None = None
        blobs_descriptor: int | None = None
        try:
            try:
                context_descriptor = self._open_directory_at(
                    root_descriptor,
                    manifest.context_id,
                    label=f"{label} context",
                )
            except (FileNotFoundError, ValueError) as exc:
                raise ValueError(f"{label} context path is missing or invalid") from exc
            observed_context = _private_file_identity(os.fstat(context_descriptor))
            if context_identity is not None and observed_context != context_identity:
                raise ValueError(f"{label} context directory identity does not match")
            self._require_directory_path_identity(
                root_descriptor,
                manifest.context_id,
                observed_context,
            )
            observed_manifest = self._read_regular_file_at(
                context_descriptor,
                "manifest.json",
                maximum=MAX_CONTEXT_MANIFEST_BYTES,
                label=f"{label} manifest",
            )
            if observed_manifest != manifest_bytes:
                raise ValueError(f"{label} manifest differs from its authorized manifest")

            blobs_descriptor = self._open_directory_at(
                context_descriptor,
                "blobs",
                label=f"{label} blob directory",
            )
            observed_blobs = _private_file_identity(os.fstat(blobs_descriptor))
            if blobs_identity is not None and observed_blobs != blobs_identity:
                raise ValueError(f"{label} blob directory identity does not match")
            self._require_directory_path_identity(
                context_descriptor,
                "blobs",
                observed_blobs,
            )
            expected_names = {blob.blob_id for blob in manifest.blobs}
            if set(os.listdir(blobs_descriptor)) != expected_names:
                raise ValueError(f"{label} blob inventory differs from its authorized manifest")

            expected_identities = blob_identities or {}
            for blob in manifest.blobs:
                descriptor = _open_materialized_blob_file(
                    blobs_descriptor,
                    blob.blob_id,
                )
                try:
                    identity, size, digest = self._digest_open_private_file(descriptor)
                finally:
                    os.close(descriptor)
                expected_identity = expected_identities.get(blob.blob_id)
                if expected_identity is not None and identity != expected_identity:
                    raise ValueError(f"{label} blob identity does not match")
                self._require_private_path_identity(
                    blobs_descriptor,
                    blob.blob_id,
                    identity,
                )
                if size != blob.size_bytes or digest != blob.sha256:
                    raise ValueError(f"{label} blob size or digest does not match")

            self._require_directory_path_identity(
                context_descriptor,
                "blobs",
                observed_blobs,
            )
            self._require_directory_path_identity(
                root_descriptor,
                manifest.context_id,
                observed_context,
            )
            if (
                self._read_regular_file_at(
                    context_descriptor,
                    "manifest.json",
                    maximum=MAX_CONTEXT_MANIFEST_BYTES,
                    label=f"{label} manifest",
                )
                != manifest_bytes
            ):
                raise ValueError(f"{label} manifest differs from its authorized manifest")
            self._require_materialization_root_descriptor(root_descriptor)
        except OSError as exc:
            raise ValueError(f"{label} could not be verified safely") from exc
        finally:
            if blobs_descriptor is not None:
                os.close(blobs_descriptor)
            if context_descriptor is not None:
                os.close(context_descriptor)

    @contextmanager
    def _open_blob(
        self,
        context_id: str,
        blob_id: str,
        *,
        expected_manifest: MaterializedContext,
        materialization_root_descriptor: int,
    ) -> Iterator[MaterializedBlobLease]:
        """Hold one store-authorized blob fd for a controlled transport read."""

        normalized_context = _stable_id(context_id)
        normalized_blob = _stable_id(blob_id)
        manifest = MaterializedContext.model_validate(expected_manifest)
        if manifest.context_id != normalized_context:
            raise ValueError("persisted materialized context identity does not match")
        expected_manifest_bytes = canonical_json(manifest).encode("utf-8")
        if len(expected_manifest_bytes) > MAX_CONTEXT_MANIFEST_BYTES:
            raise ValueError("persisted materialized context manifest exceeds its byte limit")
        root_descriptor = materialization_root_descriptor
        self._require_materialization_root_descriptor(root_descriptor)
        context_descriptor: int | None = None
        blobs_descriptor: int | None = None
        blob_descriptor: int | None = None
        stream: MaterializedBlobStream | None = None
        try:
            context_descriptor = self._open_directory_at(
                root_descriptor,
                normalized_context,
                label="materialized context",
            )
            context_identity = _private_file_identity(os.fstat(context_descriptor))
            self._require_directory_path_identity(
                root_descriptor,
                normalized_context,
                context_identity,
            )
            observed_manifest_bytes = self._read_regular_file_at(
                context_descriptor,
                "manifest.json",
                maximum=MAX_CONTEXT_MANIFEST_BYTES,
                label="materialized context manifest",
            )
            if observed_manifest_bytes != expected_manifest_bytes:
                raise ValueError("materialized context manifest differs from persisted manifest")
            matches = [item for item in manifest.blobs if item.blob_id == normalized_blob]
            if len(matches) != 1:
                raise ValueError("blob ID is not present in the context manifest")
            blob = matches[0]
            blobs_descriptor = self._open_directory_at(
                context_descriptor,
                "blobs",
                label="materialized blob directory",
            )
            blobs_identity = _private_file_identity(os.fstat(blobs_descriptor))
            self._require_directory_path_identity(
                context_descriptor,
                "blobs",
                blobs_identity,
            )
            blob_descriptor = _open_materialized_blob_file(
                blobs_descriptor,
                normalized_blob,
            )
            identity, size, digest = self._digest_open_private_file(blob_descriptor)
            self._require_private_path_identity(
                blobs_descriptor,
                normalized_blob,
                identity,
            )
            if size != blob.size_bytes or digest != blob.sha256:
                raise ValueError("materialized blob digest does not match manifest")
            os.lseek(blob_descriptor, 0, os.SEEK_SET)
            transport_descriptor = os.dup(blob_descriptor)
            try:
                stream = MaterializedBlobStream(transport_descriptor)
            except BaseException:
                os.close(transport_descriptor)
                raise
            lease = MaterializedBlobLease(blob=blob, stream=stream)
            try:
                yield lease
            except BaseException:
                raise
            else:
                after = _private_file_identity(os.fstat(blob_descriptor))
                if after != identity:
                    raise ValueError("materialized blob identity changed during transport")
                self._require_private_path_identity(
                    blobs_descriptor,
                    normalized_blob,
                    identity,
                )
                self._require_directory_path_identity(
                    context_descriptor,
                    "blobs",
                    blobs_identity,
                )
                self._require_directory_path_identity(
                    root_descriptor,
                    normalized_context,
                    context_identity,
                )
                self._require_materialization_root_descriptor(root_descriptor)
                if (
                    self._read_regular_file_at(
                        context_descriptor,
                        "manifest.json",
                        maximum=MAX_CONTEXT_MANIFEST_BYTES,
                        label="materialized context manifest",
                    )
                    != expected_manifest_bytes
                ):
                    raise ValueError(
                        "materialized context manifest differs from persisted manifest"
                    )
            finally:
                try:
                    stream.close()
                except OSError:
                    pass
        except (OSError, ValueError) as exc:
            if isinstance(exc, ValueError):
                raise
            raise ValueError("materialized blob could not be opened safely") from exc
        finally:
            for descriptor in (
                blob_descriptor,
                blobs_descriptor,
                context_descriptor,
            ):
                if descriptor is not None:
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass

    def discard(
        self,
        context_id: str,
        *,
        materialization_root_descriptor: int,
    ) -> None:
        """Remove one bundle created by this materializer after persistence rollback."""

        normalized_context = _stable_id(context_id)
        root_descriptor = materialization_root_descriptor
        self._require_materialization_root_descriptor(root_descriptor)
        context_descriptor: int | None = None
        try:
            try:
                context_descriptor = self._open_directory_at(
                    root_descriptor,
                    normalized_context,
                    label="materialized context",
                )
            except FileNotFoundError:
                return
            manifest = MaterializedContext.model_validate_json(
                self._read_regular_file_at(
                    context_descriptor,
                    "manifest.json",
                    maximum=MAX_CONTEXT_MANIFEST_BYTES,
                    label="materialized context manifest",
                )
            )
            if manifest.context_id != normalized_context:
                raise ValueError("refusing to discard a mismatched materialized context")
            expected = _private_file_identity(os.fstat(context_descriptor))
            _before_materialized_context_discard(root_descriptor, normalized_context)
            removed = _remove_materialized_entry_if_identity(
                root_descriptor,
                normalized_context,
                expected,
            )
            if removed == "mismatch":
                raise ValueError("materialized context identity changed before discard")
        except (OSError, ValueError) as exc:
            raise ValueError("refusing to discard an invalid materialized context") from exc
        finally:
            if context_descriptor is not None:
                os.close(context_descriptor)

    def _validate_projection_pair(
        self,
        request: ContextProjectionResolveRequest,
        response: ContextProjectionResolveResponse,
    ) -> None:
        if request.destination_roots != response.destination_roots:
            raise ValueError("projection request and response destination roots differ")
        if request.base_model != response.base_model:
            raise ValueError("projection request and response base model differs")
        if canonical_digest(request) != response.request_digest:
            raise ValueError("projection response request digest does not match request")
        snapshot = self._registry.snapshot
        if response.registry_digest != snapshot.registry_digest:
            raise ValueError("projection response registry digest does not match materializer")
        for projection in response.projections:
            try:
                target = snapshot.targets[projection.target_id]
                handler = snapshot.target_handlers[projection.handler_id]
            except KeyError as exc:
                raise ValueError(
                    "projection references an unknown registry target or handler"
                ) from exc
            if (
                target.handler_id != projection.handler_id
                or handler.target_id != projection.target_id
            ):
                raise ValueError("projection target and handler do not match the registry")

    @staticmethod
    def _required_text(row: Mapping[str, object], field: str) -> str:
        value = row.get(field)
        if not isinstance(value, str) or not value:
            raise ValueError(f"selected artifact {field} is missing")
        return value

    @classmethod
    def _selected_rows(
        cls,
        response: ContextProjectionResolveResponse,
        rows: Sequence[Mapping[str, object]],
    ) -> dict[str, Mapping[str, object]]:
        selected = set(response.selection.artifact_ids)
        result: dict[str, Mapping[str, object]] = {}
        for row in rows:
            artifact_id = row.get("artifact_id")
            if not isinstance(artifact_id, str) or artifact_id not in selected:
                continue
            if artifact_id in result:
                raise ValueError("promoted artifact rows contain duplicate selected IDs")
            if row.get("promoted") not in (True, 1) or str(row.get("state") or "") not in {
                "active",
                "experimental",
            }:
                raise ValueError("selected artifact is no longer promoted and active")
            result[artifact_id] = row
        missing = selected.difference(result)
        if missing:
            raise ValueError("promoted artifact rows do not cover context selection")
        return result

    @staticmethod
    def _snapshot_entry(
        snapshot: TrustedArtifactSnapshot, relative_path: str
    ) -> PayloadManifestEntry:
        matches = [
            entry for entry in snapshot.payload_entries if entry.relative_path == relative_path
        ]
        if len(matches) != 1:
            raise ValueError("staged file is absent from the reissued snapshot")
        return matches[0]

    def _plan_blobs(
        self,
        response: ContextProjectionResolveResponse,
        snapshots: Mapping[str, TrustedArtifactSnapshot],
    ) -> tuple[list[_BlobPlan], dict[tuple[str, str], tuple[DestinationScope, str]]]:
        plans: list[_BlobPlan] = []
        destinations: dict[tuple[str, str], tuple[DestinationScope, str]] = {}
        blob_ids: set[str] = set()
        for projection in response.projections:
            for contribution in projection.staged_payloads:
                key = (projection.target_id, contribution.contribution_id)
                destinations[key] = (
                    contribution.destination_scope,
                    contribution.destination_relative_path,
                )
                if isinstance(contribution, InlineTextPayloadContribution):
                    data = contribution.text.encode("utf-8")
                    plans.append(
                        _BlobPlan(
                            blob=MaterializedBlob(
                                blob_id=_new_blob_id(blob_ids),
                                target_id=projection.target_id,
                                handler_id=projection.handler_id,
                                contribution_id=contribution.contribution_id,
                                source_artifact_ids=contribution.source_artifact_ids,
                                destination_scope=contribution.destination_scope,
                                destination_relative_path=contribution.destination_relative_path,
                                media_type=contribution.media_type,
                                size_bytes=len(data),
                                sha256=hashlib.sha256(data).hexdigest(),
                            ),
                            payload_handle=None,
                            source_relative_path=None,
                            inline_bytes=data,
                        )
                    )
                    self._require_blob_count(plans)
                    continue

                snapshot = snapshots[contribution.source_artifact_id]
                if contribution.payload_kind is PayloadKind.FILE:
                    entry = self._snapshot_entry(snapshot, contribution.source_relative_path)
                    if (
                        entry.size_bytes != contribution.source_size_bytes
                        or entry.sha256 != contribution.source_sha256
                    ):
                        raise ValueError("staged file size or digest differs from contribution")
                    entries = ((entry, contribution.destination_relative_path),)
                else:
                    selected = payload_entries_under_root(
                        snapshot.payload_entries,
                        root=contribution.source_relative_path,
                    )
                    if (
                        payload_tree_size(
                            snapshot.payload_entries,
                            root=contribution.source_relative_path,
                        )
                        != contribution.source_size_bytes
                        or payload_tree_digest(
                            snapshot.payload_entries,
                            root=contribution.source_relative_path,
                        )
                        != contribution.source_sha256
                    ):
                        raise ValueError(
                            "staged directory size or digest differs from contribution"
                        )
                    prefix = (
                        ""
                        if contribution.source_relative_path == "."
                        else f"{contribution.source_relative_path}/"
                    )
                    entries = tuple(
                        (
                            entry,
                            str(
                                PurePosixPath(contribution.destination_relative_path)
                                / entry.relative_path[len(prefix) :]
                            ),
                        )
                        for entry in selected
                    )
                for entry, destination in entries:
                    plans.append(
                        _BlobPlan(
                            blob=MaterializedBlob(
                                blob_id=_new_blob_id(blob_ids),
                                target_id=projection.target_id,
                                handler_id=projection.handler_id,
                                contribution_id=contribution.contribution_id,
                                source_artifact_ids=(contribution.source_artifact_id,),
                                destination_scope=contribution.destination_scope,
                                destination_relative_path=destination,
                                media_type=(
                                    contribution.media_type
                                    if contribution.payload_kind is PayloadKind.FILE
                                    else entry.media_type
                                ),
                                size_bytes=entry.size_bytes,
                                sha256=entry.sha256,
                            ),
                            payload_handle=snapshot.payload_handle,
                            source_relative_path=entry.relative_path,
                            inline_bytes=None,
                        )
                    )
                    self._require_blob_count(plans)
        return plans, destinations

    @staticmethod
    def _require_blob_count(plans: Sequence[_BlobPlan]) -> None:
        if len(plans) > MAX_CONTEXT_BLOBS:
            raise ValueError("context exceeds maximum materialized blob count")

    @staticmethod
    def _plan_adapters(
        response: ContextProjectionResolveResponse,
        snapshots: Mapping[str, TrustedArtifactSnapshot],
    ) -> tuple[MaterializedAdapter, ...]:
        result: list[MaterializedAdapter] = []
        for projection in response.projections:
            for adapter in projection.adapters:
                snapshot = snapshots[adapter.source_artifact_id]
                if (
                    snapshot.payload_manifest_digest != adapter.source_payload_digest
                    or sum(entry.size_bytes for entry in snapshot.payload_entries)
                    != adapter.source_size_bytes
                ):
                    raise ValueError("adapter inventory size or digest differs from contribution")
                result.append(
                    MaterializedAdapter(
                        target_id=projection.target_id,
                        handler_id=projection.handler_id,
                        contribution_id=adapter.contribution_id,
                        source_artifact_id=adapter.source_artifact_id,
                        source_payload_digest=adapter.source_payload_digest,
                        source_size_bytes=adapter.source_size_bytes,
                        adapter_id=adapter.adapter_id,
                        adapter_format=adapter.adapter_format,
                        base_model=adapter.base_model,
                        weight=adapter.weight,
                    )
                )
        return tuple(result)

    @staticmethod
    def _validate_destinations(plans: Sequence[_BlobPlan]) -> None:
        for index, left in enumerate(plans):
            for right in plans[index + 1 :]:
                if left.blob.destination_scope is right.blob.destination_scope and paths_conflict(
                    left.blob.destination_relative_path,
                    right.blob.destination_relative_path,
                ):
                    raise ValueError("materialized payload destinations conflict")

    def _materialize_instruction(self, response: ContextProjectionResolveResponse) -> str:
        values: list[str] = []
        for projection in response.projections:
            text = "\n\n".join(
                contribution.text for contribution in projection.instructions
            ).strip()
            if not text:
                continue
            preamble = self._registry.snapshot.target_handlers[
                projection.handler_id
            ].instruction_preamble
            values.append(f"{preamble}\n{text}" if preamble else text)
        instruction = "\n\n".join(values)
        if len(instruction) > MAX_CONTEXT_INSTRUCTION_CHARS:
            raise ValueError("materialized instruction exceeds character limit")
        if len(instruction.encode("utf-8")) > MAX_CONTEXT_INSTRUCTION_BYTES:
            raise ValueError("materialized instruction exceeds byte limit")
        return instruction

    @staticmethod
    def _materialize_environment(
        request: ContextProjectionResolveRequest,
        response: ContextProjectionResolveResponse,
        destinations: Mapping[tuple[str, str], tuple[DestinationScope, str]],
    ) -> tuple[MaterializedEnvironmentBinding, ...]:
        result: list[MaterializedEnvironmentBinding] = []
        names: set[str] = set()
        for projection in response.projections:
            for binding in projection.environment:
                if binding.name in names:
                    raise ValueError("materialized environment names conflict")
                names.add(binding.name)
                if binding.value_kind is EnvironmentValueKind.SCOPE_ROOT:
                    if binding.destination_scope is None:  # Contract guards this.
                        raise ValueError("scope-root environment binding lacks a scope")
                    value = _runtime_path(request, binding.destination_scope)
                    scopes = (binding.destination_scope,)
                else:
                    resolved = [
                        destinations[(projection.target_id, contribution_id)]
                        for contribution_id in binding.value_contribution_ids
                    ]
                    paths = [
                        _runtime_path(request, scope, relative_path)
                        for scope, relative_path in resolved
                    ]
                    value = (
                        canonical_json(paths)
                        if binding.value_kind is EnvironmentValueKind.JSON_PATHS
                        else paths[0]
                    )
                    scopes = tuple(scope for scope, _ in resolved)
                result.append(
                    MaterializedEnvironmentBinding(
                        target_id=projection.target_id,
                        handler_id=projection.handler_id,
                        name=binding.name,
                        value_kind=binding.value_kind,
                        value=value,
                        contribution_ids=binding.value_contribution_ids,
                        destination_scope=(scopes[0] if len(set(scopes)) == 1 else None),
                    )
                )
                if len(result) > MAX_CONTEXT_ENVIRONMENT_BINDINGS:
                    raise ValueError("context exceeds environment binding limit")
        if len(canonical_json(result).encode("utf-8")) > MAX_CONTEXT_ENVIRONMENT_BYTES:
            raise ValueError("materialized environment exceeds byte limit")
        return tuple(result)

    def _publish(
        self,
        result: MaterializedContext,
        plans: Sequence[_BlobPlan],
        snapshots: Mapping[str, TrustedArtifactSnapshot],
        payloads: ArtifactPayloadService,
        root_descriptor: int,
    ) -> ContextPublicationReceipt:
        manifest_bytes = canonical_json(result).encode("utf-8")
        if len(manifest_bytes) > MAX_CONTEXT_MANIFEST_BYTES:
            raise ValueError("materialized context manifest exceeds its byte limit")
        self._require_materialization_root_descriptor(root_descriptor)
        temporary_name: str | None = None
        temporary_identity: _PrivateFileIdentity | None = None
        published = False
        temporary_descriptor: int | None = None
        blobs_descriptor: int | None = None
        verified_identities: dict[str, _PrivateFileIdentity] = {}
        try:
            try:
                os.stat(
                    result.context_id,
                    dir_fd=root_descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                pass
            else:
                raise ValueError("materialized context already exists")
            temporary_name, temporary_descriptor = self._create_private_directory_at(
                root_descriptor,
                prefix=f".{result.context_id}.",
            )
            temporary_identity = _private_file_identity(os.fstat(temporary_descriptor))
            os.mkdir("blobs", mode=0o700, dir_fd=temporary_descriptor)
            blobs_descriptor = self._open_directory_at(
                temporary_descriptor,
                "blobs",
                label="temporary materialized blob directory",
            )
            for plan in plans:
                if plan.inline_bytes is not None:
                    descriptor = os.open(
                        plan.blob.blob_id,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
                        0o600,
                        dir_fd=blobs_descriptor,
                    )
                    try:
                        view = memoryview(plan.inline_bytes)
                        while view:
                            written = os.write(descriptor, view)
                            if written <= 0:
                                raise OSError("materialized blob write made no progress")
                            view = view[written:]
                        os.fsync(descriptor)
                    finally:
                        os.close(descriptor)
                else:
                    if plan.payload_handle is None or plan.source_relative_path is None:
                        raise RuntimeError("artifact blob plan lacks source identity")
                    descriptor = os.open(
                        plan.blob.blob_id,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
                        0o600,
                        dir_fd=blobs_descriptor,
                    )
                    try:
                        payloads.copy_verified_file(
                            plan.payload_handle,
                            plan.source_relative_path,
                            descriptor,
                        )
                        os.fsync(descriptor)
                    finally:
                        os.close(descriptor)
                verified_identities[plan.blob.blob_id] = self._verify_materialized_file(
                    blobs_descriptor,
                    plan.blob,
                )
            content_verified_handles: set[str] = set()
            for adapter in result.adapter_merge_spec.adapters:
                handle = snapshots[adapter.source_artifact_id].payload_handle
                payloads.verify_payload_content(handle)
                content_verified_handles.add(handle)
            for snapshot in snapshots.values():
                if snapshot.payload_handle not in content_verified_handles:
                    payloads.verify_inventory_identity(snapshot.payload_handle)
            self._verify_materialized_blob_identities(
                blobs_descriptor,
                verified_identities,
            )
            os.fsync(blobs_descriptor)

            descriptor = os.open(
                "manifest.json",
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
                0o600,
                dir_fd=temporary_descriptor,
            )
            try:
                view = memoryview(manifest_bytes)
                while view:
                    written = os.write(descriptor, view)
                    if written <= 0:
                        raise OSError("materialized manifest write made no progress")
                    view = view[written:]
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            _after_materialized_manifest_write(temporary_descriptor)
            observed_manifest = self._read_regular_file_at(
                temporary_descriptor,
                "manifest.json",
                maximum=MAX_CONTEXT_MANIFEST_BYTES,
                label="temporary materialized context manifest",
            )
            if observed_manifest != manifest_bytes:
                raise ValueError("temporary materialized context manifest does not match")
            MaterializedContext.model_validate_json(observed_manifest)
            os.fsync(temporary_descriptor)
            self._verify_materialized_blob_identities(
                blobs_descriptor,
                verified_identities,
            )
            _before_materialized_context_rename(root_descriptor, result.context_id)
            _rename_noreplace(
                temporary_name,
                result.context_id,
                directory_fd=root_descriptor,
            )
            published = True
            _after_materialized_context_rename(root_descriptor, result.context_id)
            self._verify_published_context(
                root_descriptor,
                temporary_descriptor,
                blobs_descriptor,
                result,
                manifest_bytes,
                verified_identities,
            )
            os.fsync(root_descriptor)
            return ContextPublicationReceipt(
                materialized_context=result,
                canonical_manifest_bytes=manifest_bytes,
                context_directory_identity=_private_file_identity(os.fstat(temporary_descriptor)),
                blobs_directory_identity=_private_file_identity(os.fstat(blobs_descriptor)),
                blob_identities=tuple(
                    (blob.blob_id, verified_identities[blob.blob_id]) for blob in result.blobs
                ),
                _issuer=self._publication_issuer,
            )
        except BaseException:
            # A published name can be replaced after verification fails. Startup
            # orphan recovery performs the safe, store-bound cleanup instead.
            raise
        finally:
            if blobs_descriptor is not None:
                os.close(blobs_descriptor)
            if temporary_descriptor is not None:
                if not published:
                    try:
                        temporary_identity = _private_file_identity(os.fstat(temporary_descriptor))
                    except OSError:
                        pass
                os.close(temporary_descriptor)
            if not published and temporary_name is not None and temporary_identity is not None:
                try:
                    _before_temporary_context_cleanup(
                        root_descriptor,
                        temporary_name,
                    )
                    _remove_materialized_entry_if_identity(
                        root_descriptor,
                        temporary_name,
                        temporary_identity,
                    )
                except (OSError, RuntimeError, ValueError):
                    pass

    @staticmethod
    def _verify_materialized_file(
        directory_fd: int,
        blob: MaterializedBlob,
    ) -> _PrivateFileIdentity:
        try:
            descriptor = _open_materialized_blob_file(directory_fd, blob.blob_id)
        except OSError as exc:
            raise ValueError("payload service did not create a private regular blob") from exc
        digest = hashlib.sha256()
        size = 0
        try:
            opened = _private_file_identity(os.fstat(descriptor))
            if (
                not stat.S_ISREG(opened.mode)
                or opened.link_count != 1
                or opened.size != blob.size_bytes
            ):
                raise ValueError("payload service did not create a private regular blob")
            while chunk := os.read(descriptor, 1024 * 1024):
                size += len(chunk)
                if size > blob.size_bytes:
                    raise ValueError("copied blob exceeds contribution size")
                digest.update(chunk)
            after = _private_file_identity(os.fstat(descriptor))
        finally:
            os.close(descriptor)
        ContextMaterializer._require_private_path_identity(
            directory_fd,
            blob.blob_id,
            after,
        )
        if (
            size != blob.size_bytes
            or digest.hexdigest() != blob.sha256
            or opened != after
            or after.link_count != 1
        ):
            raise ValueError("copied blob size or digest does not match contribution")
        return after

    @staticmethod
    def _verify_materialized_blob_identities(
        directory_fd: int,
        expected: Mapping[str, _PrivateFileIdentity],
    ) -> None:
        for blob_id, identity in expected.items():
            ContextMaterializer._require_private_path_identity(
                directory_fd,
                blob_id,
                identity,
            )

    @classmethod
    def _verify_published_context(
        cls,
        root_descriptor: int,
        context_descriptor: int,
        blobs_descriptor: int,
        result: MaterializedContext,
        manifest_bytes: bytes,
        blob_identities: Mapping[str, _PrivateFileIdentity],
    ) -> None:
        expected_context = _private_file_identity(os.fstat(context_descriptor))
        expected_blobs = _private_file_identity(os.fstat(blobs_descriptor))
        final_descriptor: int | None = None
        final_blobs_descriptor: int | None = None
        try:
            try:
                final_descriptor = cls._open_directory_at(
                    root_descriptor,
                    result.context_id,
                    label="published context path",
                )
            except (FileNotFoundError, ValueError) as exc:
                raise ValueError("published context path is missing or invalid") from exc
            observed_context = _private_file_identity(os.fstat(final_descriptor))
            if observed_context != expected_context:
                raise ValueError("published context path identity does not match")
            observed_manifest = cls._read_regular_file_at(
                final_descriptor,
                "manifest.json",
                maximum=MAX_CONTEXT_MANIFEST_BYTES,
                label="published context manifest",
            )
            if observed_manifest != manifest_bytes:
                raise ValueError("published context manifest does not match")
            final_blobs_descriptor = cls._open_directory_at(
                final_descriptor,
                "blobs",
                label="published blob directory",
            )
            if _private_file_identity(os.fstat(final_blobs_descriptor)) != expected_blobs:
                raise ValueError("published blob directory identity does not match")
            cls._verify_materialized_blob_identities(
                final_blobs_descriptor,
                blob_identities,
            )
            cls._require_directory_path_identity(
                root_descriptor,
                result.context_id,
                expected_context,
            )
        finally:
            if final_blobs_descriptor is not None:
                os.close(final_blobs_descriptor)
            if final_descriptor is not None:
                os.close(final_descriptor)

    def _require_materialization_root_descriptor(self, descriptor: int) -> None:
        if not isinstance(descriptor, int) or isinstance(descriptor, bool):
            raise ValueError("materialization root descriptor is invalid")
        try:
            opened_stat = os.fstat(descriptor)
            current_stat = os.stat(self._materialization_root, follow_symlinks=False)
            opened = _private_file_identity(opened_stat)
            current = _private_file_identity(current_stat)
        except OSError as exc:
            raise ValueError("materialization root descriptor could not be verified") from exc
        if (
            opened != current
            or opened_stat.st_uid != os.geteuid()
            or not stat.S_ISDIR(opened.mode)
            or stat.S_IMODE(opened.mode) != 0o700
        ):
            raise ValueError("materialization root descriptor identity does not match")

    @classmethod
    def _create_private_directory_at(
        cls,
        root_descriptor: int,
        *,
        prefix: str,
    ) -> tuple[str, int]:
        for _ in range(128):
            name = f"{prefix}{secrets.token_hex(24)}"
            try:
                os.mkdir(name, mode=0o700, dir_fd=root_descriptor)
            except FileExistsError:
                continue
            descriptor: int | None = None
            cleanup_identity: _PrivateFileIdentity | None = None
            try:
                descriptor = cls._open_directory_at(
                    root_descriptor,
                    name,
                    label="temporary materialized context",
                )
                cleanup_identity = _private_file_identity(os.fstat(descriptor))
                os.fchmod(descriptor, 0o700)
                expected = _private_file_identity(os.fstat(descriptor))
                if not _same_filesystem_object(cleanup_identity, expected):
                    raise ValueError("temporary materialized context identity changed")
                cleanup_identity = expected
                cls._require_directory_path_identity(
                    root_descriptor,
                    name,
                    expected,
                )
                return name, descriptor
            except BaseException:
                if descriptor is not None:
                    try:
                        try:
                            observed = _private_file_identity(os.fstat(descriptor))
                        except OSError:
                            pass
                        else:
                            if cleanup_identity is None or _same_filesystem_object(
                                cleanup_identity,
                                observed,
                            ):
                                cleanup_identity = observed
                            else:
                                cleanup_identity = None
                        if cleanup_identity is not None:
                            try:
                                _remove_materialized_entry_if_identity(
                                    root_descriptor,
                                    name,
                                    cleanup_identity,
                                    preserve_mismatch=False,
                                )
                            except (OSError, RuntimeError, ValueError):
                                pass
                    finally:
                        os.close(descriptor)
                raise
        raise RuntimeError("could not allocate a temporary materialization directory")

    @staticmethod
    def _open_directory_at(parent_fd: int, name: str, *, label: str) -> int:
        try:
            descriptor = os.open(
                name,
                os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_DIRECTORY,
                dir_fd=parent_fd,
            )
        except FileNotFoundError:
            raise
        except OSError as exc:
            raise ValueError(f"{label} could not be opened safely") from exc
        try:
            opened = os.fstat(descriptor)
        except BaseException:
            os.close(descriptor)
            raise
        if not stat.S_ISDIR(opened.st_mode):
            os.close(descriptor)
            raise ValueError(f"{label} is not a directory")
        return descriptor

    @staticmethod
    def _require_private_path_identity(
        directory_fd: int,
        name: str,
        expected: _PrivateFileIdentity,
    ) -> None:
        try:
            observed = _private_file_identity(
                os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            )
        except OSError as exc:
            raise ValueError("materialized blob path changed or disappeared") from exc
        if observed != expected or not stat.S_ISREG(observed.mode) or observed.link_count != 1:
            raise ValueError("materialized blob path identity is not private and stable")

    @staticmethod
    def _require_directory_path_identity(
        directory_fd: int,
        name: str,
        expected: _PrivateFileIdentity,
    ) -> None:
        try:
            observed = _private_file_identity(
                os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            )
        except OSError as exc:
            raise ValueError("published context path changed or disappeared") from exc
        if observed != expected or not stat.S_ISDIR(observed.mode):
            raise ValueError("published context path identity is not stable")

    @staticmethod
    def _read_regular_file_at(
        directory_fd: int,
        name: str,
        *,
        maximum: int,
        label: str,
    ) -> bytes:
        try:
            descriptor = _open_materialized_blob_file(directory_fd, name)
        except OSError as exc:
            raise ValueError(f"{label} could not be opened safely") from exc
        chunks: list[bytes] = []
        size = 0
        try:
            opened = _private_file_identity(os.fstat(descriptor))
            if not stat.S_ISREG(opened.mode) or opened.link_count != 1:
                raise ValueError(f"{label} is not a private regular file")
            while chunk := os.read(descriptor, 1024 * 1024):
                size += len(chunk)
                if size > maximum:
                    raise ValueError(f"{label} exceeds its byte limit")
                chunks.append(chunk)
            after = _private_file_identity(os.fstat(descriptor))
        finally:
            os.close(descriptor)
        ContextMaterializer._require_private_path_identity(
            directory_fd,
            name,
            after,
        )
        if size != opened.size or opened != after:
            raise ValueError(f"{label} changed while being read")
        return b"".join(chunks)

    @staticmethod
    def _digest_open_private_file(
        descriptor: int,
    ) -> tuple[_PrivateFileIdentity, int, str]:
        opened = _private_file_identity(os.fstat(descriptor))
        if not stat.S_ISREG(opened.mode) or opened.link_count != 1:
            raise ValueError("materialized blob is not a private regular file")
        digest = hashlib.sha256()
        size = 0
        while chunk := os.read(descriptor, 1024 * 1024):
            size += len(chunk)
            if size > MAX_PAYLOAD_TOTAL_BYTES:
                raise ValueError("materialized blob exceeds the context byte limit")
            digest.update(chunk)
        after = _private_file_identity(os.fstat(descriptor))
        if size != opened.size or opened != after or after.link_count != 1:
            raise ValueError("materialized blob changed while being read")
        return after, size, digest.hexdigest()


__all__ = [
    "MAX_CONTEXT_BLOBS",
    "MAX_CONTEXT_ENVIRONMENT_BINDINGS",
    "MAX_CONTEXT_ENVIRONMENT_BYTES",
    "MAX_CONTEXT_INSTRUCTION_BYTES",
    "MAX_CONTEXT_INSTRUCTION_CHARS",
    "MAX_CONTEXT_MANIFEST_BYTES",
    "ContextPublicationReceipt",
    "ContextMaterializer",
    "MaterializedEntryRemovalResult",
    "MaterializedAdapter",
    "MaterializedAdapterMergeSpec",
    "MaterializedBlob",
    "MaterializedBlobLease",
    "MaterializedBlobStream",
    "MaterializedContext",
    "MaterializedEnvironmentBinding",
]
