from __future__ import annotations

import errno
import hashlib
import io
import os
import pickle
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import unquote, urlsplit

import pytest

from openevo.evolution import context_materialization
from openevo.evolution.artifact_payloads import (
    ArtifactPayloadService as _RealArtifactPayloadService,
)
from openevo.evolution.context_materialization import ContextMaterializer
from openevo.evolution.context_projection import (
    ContextProjectionResolveRequest,
    ContextProjectionResolveResponse,
    ContextProjectionSelection,
)
from openevo.evolution.framework import (
    EvolutionExecutionProfile,
    PayloadManifestEntry,
    RuntimeDestinationRoots,
    TrustedArtifactSnapshot,
    canonical_digest,
    canonical_json,
    payload_tree_digest,
)
from openevo.evolution.framework.contributions import TargetHandlerOutput


_REAL_REQUIRE_VERIFIED_REGISTRY = context_materialization.require_verified_executable_registry


def _registry():
    target_to_handler = {
        "alpha": "alpha_handler",
        "beta": "beta_handler",
        "gamma": "gamma_handler",
        "left": "left_handler",
        "right": "right_handler",
        "limited": "limited_handler",
        "long": "long_handler",
        "env": "env_handler",
        "bytes": "bytes_handler",
        "copy": "copy_handler",
    }
    preambles = {"alpha_handler": "Alpha:", "beta_handler": "Beta:"}
    return SimpleNamespace(
        snapshot=SimpleNamespace(
            registry_digest="a" * 64,
            targets={
                target_id: SimpleNamespace(handler_id=handler_id)
                for target_id, handler_id in target_to_handler.items()
            },
            target_handlers={
                handler_id: SimpleNamespace(
                    target_id=target_id,
                    instruction_preamble=preambles.get(handler_id, ""),
                )
                for target_id, handler_id in target_to_handler.items()
            },
        )
    )


class _TestContextMaterializer(ContextMaterializer):
    def materialize(self, request, response, promoted_rows, **kwargs):
        if kwargs:
            return super().materialize(request, response, promoted_rows, **kwargs)
        self._materialization_root.mkdir(parents=True, mode=0o700, exist_ok=True)
        self._materialization_root.chmod(0o700)
        descriptor = os.open(
            self._materialization_root,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_DIRECTORY,
        )
        try:
            return super().materialize(
                request,
                response,
                promoted_rows,
                materialization_root_descriptor=descriptor,
            )
        finally:
            os.close(descriptor)

    def discard(self, context_id: str, **kwargs) -> None:
        if kwargs:
            return super().discard(context_id, **kwargs)
        descriptor = os.open(
            self._materialization_root,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_DIRECTORY,
        )
        try:
            super().discard(
                context_id,
                materialization_root_descriptor=descriptor,
            )
        finally:
            os.close(descriptor)

    def materialize_for_publication(self, request, response, promoted_rows, **kwargs):
        if kwargs:
            return super().materialize_for_publication(request, response, promoted_rows, **kwargs)
        self._materialization_root.mkdir(parents=True, mode=0o700, exist_ok=True)
        self._materialization_root.chmod(0o700)
        descriptor = os.open(
            self._materialization_root,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_DIRECTORY,
        )
        try:
            return super().materialize_for_publication(
                request,
                response,
                promoted_rows,
                materialization_root_descriptor=descriptor,
            )
        finally:
            os.close(descriptor)

    def verify_publication(self, receipt, **kwargs) -> None:
        if kwargs:
            return super().verify_publication(receipt, **kwargs)
        descriptor = os.open(
            self._materialization_root,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_DIRECTORY,
        )
        try:
            return super().verify_publication(
                receipt,
                materialization_root_descriptor=descriptor,
            )
        finally:
            os.close(descriptor)

    def discard_publication(self, receipt, **kwargs):
        if kwargs:
            return super().discard_publication(receipt, **kwargs)
        descriptor = os.open(
            self._materialization_root,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_DIRECTORY,
        )
        try:
            return super().discard_publication(
                receipt,
                materialization_root_descriptor=descriptor,
            )
        finally:
            os.close(descriptor)

    def verify_persisted_materialization(self, manifest, **kwargs) -> None:
        if kwargs:
            return super().verify_persisted_materialization(manifest, **kwargs)
        descriptor = os.open(
            self._materialization_root,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_DIRECTORY,
        )
        try:
            return super().verify_persisted_materialization(
                manifest,
                materialization_root_descriptor=descriptor,
            )
        finally:
            os.close(descriptor)


def _materializer(artifact_root: Path, materialization_root: Path) -> ContextMaterializer:
    return _TestContextMaterializer(artifact_root, materialization_root, _registry())


@contextmanager
def _open_blob(
    materializer: ContextMaterializer,
    manifest,
    blob_id: str,
):
    descriptor = os.open(
        materializer._materialization_root,
        os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_DIRECTORY,
    )
    try:
        with materializer._open_blob(
            manifest.context_id,
            blob_id,
            expected_manifest=manifest,
            materialization_root_descriptor=descriptor,
        ) as lease:
            yield lease
    finally:
        os.close(descriptor)


class _PayloadService:
    mutate_after_issue: Path | None = None
    fail_after_copies: int | None = None

    def __init__(self, allowed_root: str | os.PathLike[str]) -> None:
        self.root = Path(allowed_root).resolve()
        self.handles: dict[str, tuple[Path, tuple[PayloadManifestEntry, ...]]] = {}
        self.copy_count = 0

    def __enter__(self) -> _PayloadService:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def issue_snapshot(
        self,
        *,
        artifact_id: str,
        artifact_type: str,
        name: str,
        uri: str,
        manifest: dict[str, object],
        scores: dict[str, object],
        rank_index: int,
    ) -> TrustedArtifactSnapshot:
        path = Path(unquote(urlsplit(uri).path)).resolve()
        path.relative_to(self.root)
        files = (
            [path]
            if path.is_file()
            else sorted(item for item in path.rglob("*") if item.is_file())
        )
        base = path.parent if path.is_file() else path
        entries = tuple(
            PayloadManifestEntry(
                relative_path=item.relative_to(base).as_posix(),
                media_type="text/plain" if item.suffix == ".txt" else "application/octet-stream",
                size_bytes=item.stat().st_size,
                sha256=hashlib.sha256(item.read_bytes()).hexdigest(),
            )
            for item in files
        )
        handle = f"payload-{artifact_id}"
        self.handles[handle] = (base, entries)
        snapshot = TrustedArtifactSnapshot(
            artifact_id=artifact_id,
            artifact_type=artifact_type,
            name=name,
            uri_scheme="file",
            payload_handle=handle,
            payload_entries=entries,
            payload_manifest_digest=payload_tree_digest(entries),
            manifest_json=canonical_json(manifest),
            scores_json=canonical_json(scores),
            rank_index=rank_index,
        )
        if self.mutate_after_issue is not None:
            self.mutate_after_issue.write_bytes(b"drift")
        return snapshot

    def copy_verified_file(
        self,
        payload_handle: str,
        relative_path: str,
        destination_fd: int,
    ) -> None:
        self.copy_count += 1
        if self.fail_after_copies is not None and self.copy_count > self.fail_after_copies:
            os.write(destination_fd, b"partial")
            raise ValueError("injected copy failure")
        base, entries = self.handles[payload_handle]
        expected = next(item for item in entries if item.relative_path == relative_path)
        source = base / relative_path
        data = source.read_bytes()
        if len(data) != expected.size_bytes or hashlib.sha256(data).hexdigest() != expected.sha256:
            raise ValueError("payload source drift")
        os.write(destination_fd, data)

    def verify_inventory_identity(self, payload_handle: str) -> None:
        base, entries = self.handles[payload_handle]
        for entry in entries:
            data = (base / entry.relative_path).read_bytes()
            if len(data) != entry.size_bytes or hashlib.sha256(data).hexdigest() != entry.sha256:
                raise ValueError("payload inventory drift")

    def verify_payload_content(self, payload_handle: str) -> None:
        self.verify_inventory_identity(payload_handle)


@pytest.fixture(autouse=True)
def _payload_service(monkeypatch: pytest.MonkeyPatch):
    _PayloadService.mutate_after_issue = None
    _PayloadService.fail_after_copies = None
    monkeypatch.setattr(context_materialization, "ArtifactPayloadService", _PayloadService)
    monkeypatch.setattr(
        context_materialization,
        "require_verified_executable_registry",
        lambda registry: registry,
    )


def _request(
    *,
    successor_transition_id: str | None = None,
    predecessor_project_head_id: str | None = None,
) -> ContextProjectionResolveRequest:
    roots = RuntimeDestinationRoots(
        target_data="/runtime/data",
        harness_skills="/runtime/extensions",
        harness_instruction="/runtime/work",
    )
    return ContextProjectionResolveRequest(
        task_id="task",
        instruction="run",
        successor_transition_id=successor_transition_id,
        predecessor_project_head_id=predecessor_project_head_id,
        agent={"harness": "runner"},
        base_model="base",
        execution_profile=EvolutionExecutionProfile(
            execution_mode="self_deployed",
            capture_mode="transcript",
            harness_id="runner",
            runtime_capabilities=("adapter_serving",),
        ),
        destination_roots=roots,
    )


def _renderer(source: str) -> dict[str, object]:
    return {
        "kind": "structured_summary",
        "title": "Projection",
        "source_contribution_ids": (source,),
        "data": {"fields": ({"source_contribution_id": source, "label": "Value", "value": "ok"},)},
    }


def _inline_projection(*, text: str = "private") -> TargetHandlerOutput:
    return TargetHandlerOutput(
        target_id="alpha",
        handler_id="alpha_handler",
        artifact_ids=("artifact-a",),
        staged_payloads=(
            {
                "source_kind": "inline_text",
                "contribution_id": "inline",
                "source_artifact_ids": ("artifact-a",),
                "text": text,
                "media_type": "text/plain",
                "destination_scope": "target_data",
                "destination_relative_path": "private.txt",
            },
        ),
        renderer=_renderer("inline"),
    )


def _row(artifact_id: str, path: Path) -> dict[str, object]:
    return {
        "artifact_id": artifact_id,
        "type": "generic",
        "name": artifact_id,
        "uri": path.as_uri(),
        "manifest_json": "{}",
        "scores_json": "{}",
        "promoted": 1,
        "state": "active",
    }


def _response(
    request: ContextProjectionResolveRequest,
    *projections: TargetHandlerOutput,
    context_id: str = "ctx-test",
) -> ContextProjectionResolveResponse:
    artifact_ids = tuple(
        dict.fromkeys(
            artifact_id for projection in projections for artifact_id in projection.artifact_ids
        )
    )
    return ContextProjectionResolveResponse(
        context_id=context_id,
        request_digest=canonical_digest(request),
        registry_digest="a" * 64,
        base_model=request.base_model,
        destination_roots=request.destination_roots,
        projections=projections,
        selection=ContextProjectionSelection(
            artifact_ids=artifact_ids,
            reasons=("selected",),
        ),
    )


def test_materializer_requires_verified_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        context_materialization,
        "require_verified_executable_registry",
        _REAL_REQUIRE_VERIFIED_REGISTRY,
    )
    with pytest.raises((TypeError, ValueError), match="verified|registry"):
        ContextMaterializer(tmp_path / "artifacts", tmp_path / "contexts", object())


def test_materialization_preserves_atomic_successor_ownership(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def test_noreplace(
        source: str,
        destination: str,
        *,
        directory_fd: int,
    ) -> None:
        with pytest.raises(FileNotFoundError):
            os.stat(destination, dir_fd=directory_fd, follow_symlinks=False)
        os.rename(
            source,
            destination,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )

    monkeypatch.setattr(context_materialization, "_rename_noreplace", test_noreplace)
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    source = artifacts / "source.txt"
    source.write_text("x", encoding="utf-8")
    request = _request(
        successor_transition_id="successor-1",
        predecessor_project_head_id="project-head-0",
    )
    projection = _inline_projection()
    response = _response(request, projection)

    result = _materializer(artifacts, tmp_path / "contexts").materialize(
        request,
        response,
        (_row("artifact-a", source),),
    )

    assert result.successor_transition_id == "successor-1"
    assert result.predecessor_project_head_id == "project-head-0"
    with pytest.raises(ValueError, match="together"):
        _request(successor_transition_id="successor-1")


def test_materializer_rejects_registry_or_handler_identity_mismatch(tmp_path: Path) -> None:
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    source = artifacts / "source.txt"
    source.write_text("x", encoding="utf-8")
    request = _request()
    projection = TargetHandlerOutput(
        target_id="alpha",
        handler_id="alpha_handler",
        artifact_ids=("artifact-a",),
        instructions=(
            {
                "contribution_id": "instruction-a",
                "source_artifact_ids": ("artifact-a",),
                "text": "value",
            },
        ),
        renderer=_renderer("instruction-a"),
    )
    response = _response(request, projection)
    bad_digest = response.model_copy(update={"registry_digest": "b" * 64})
    with pytest.raises(ValueError, match="registry"):
        _materializer(artifacts, tmp_path / "contexts-a").materialize(
            request, bad_digest, (_row("artifact-a", source),)
        )

    bad_request = response.model_copy(update={"request_digest": "b" * 64})
    with pytest.raises(ValueError, match="request digest"):
        _materializer(artifacts, tmp_path / "contexts-request").materialize(
            request, bad_request, (_row("artifact-a", source),)
        )

    bad_handler = response.model_copy(
        update={"projections": (projection.model_copy(update={"handler_id": "beta_handler"}),)}
    )
    with pytest.raises(ValueError, match="handler|target"):
        _materializer(artifacts, tmp_path / "contexts-b").materialize(
            request, bad_handler, (_row("artifact-a", source),)
        )


def test_materializes_inline_file_directory_environment_instruction_and_adapter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        context_materialization,
        "ArtifactPayloadService",
        _RealArtifactPayloadService,
    )
    artifacts = tmp_path / "artifacts"
    materialized = tmp_path / "contexts"
    artifacts.mkdir()
    single = artifacts / "single.txt"
    single.write_bytes(b"single")
    bundle = artifacts / "bundle"
    bundle.mkdir()
    (bundle / "a.txt").write_bytes(b"a")
    (bundle / "z.bin").write_bytes(b"zz")
    adapter = artifacts / "adapter"
    adapter.mkdir()
    (adapter / "weights.bin").write_bytes(b"weights")

    single_digest = hashlib.sha256(b"single").hexdigest()
    bundle_entries = (
        PayloadManifestEntry(
            relative_path="a.txt",
            media_type="text/plain",
            size_bytes=1,
            sha256=hashlib.sha256(b"a").hexdigest(),
        ),
        PayloadManifestEntry(
            relative_path="z.bin",
            media_type="application/octet-stream",
            size_bytes=2,
            sha256=hashlib.sha256(b"zz").hexdigest(),
        ),
    )
    adapter_entries = (
        PayloadManifestEntry(
            relative_path="weights.bin",
            media_type="application/octet-stream",
            size_bytes=7,
            sha256=hashlib.sha256(b"weights").hexdigest(),
        ),
    )
    first = TargetHandlerOutput(
        target_id="alpha",
        handler_id="alpha_handler",
        artifact_ids=("artifact-a",),
        instructions=(
            {
                "contribution_id": "instruction-a",
                "source_artifact_ids": ("artifact-a",),
                "text": "first",
            },
        ),
        staged_payloads=(
            {
                "contribution_id": "single-file",
                "source_artifact_id": "artifact-a",
                "source_relative_path": "single.txt",
                "source_sha256": single_digest,
                "source_size_bytes": 6,
                "media_type": "text/plain",
                "payload_kind": "file",
                "destination_scope": "target_data",
                "destination_relative_path": "single.txt",
            },
            {
                "source_kind": "inline_text",
                "contribution_id": "inline-file",
                "source_artifact_ids": ("artifact-a",),
                "text": "inline",
                "media_type": "text/plain",
                "destination_scope": "target_data",
                "destination_relative_path": "inline.txt",
            },
        ),
        environment=(
            {
                "name": "OPENEVO_ONE_PATH",
                "value_contribution_ids": ("single-file",),
                "value_kind": "path",
            },
            {
                "name": "OPENEVO_MANY_PATHS",
                "value_contribution_ids": ("single-file", "inline-file"),
                "value_kind": "json_paths",
            },
            {
                "name": "OPENEVO_DATA_ROOT",
                "value_kind": "scope_root",
                "destination_scope": "target_data",
            },
        ),
        renderer=_renderer("instruction-a"),
    )
    second = TargetHandlerOutput(
        target_id="beta",
        handler_id="beta_handler",
        artifact_ids=("artifact-b",),
        instructions=(
            {
                "contribution_id": "instruction-b",
                "source_artifact_ids": ("artifact-b",),
                "text": "second",
            },
        ),
        staged_payloads=(
            {
                "contribution_id": "bundle-dir",
                "source_artifact_id": "artifact-b",
                "source_relative_path": ".",
                "source_sha256": payload_tree_digest(bundle_entries),
                "source_size_bytes": 3,
                "media_type": "application/octet-stream",
                "payload_kind": "directory",
                "destination_scope": "harness_skills",
                "destination_relative_path": "bundle",
            },
        ),
        environment=(
            {
                "name": "OPENEVO_BUNDLE_DIR",
                "value_contribution_ids": ("bundle-dir",),
                "value_kind": "directory",
            },
        ),
        renderer=_renderer("instruction-b"),
    )
    third = TargetHandlerOutput(
        target_id="gamma",
        handler_id="gamma_handler",
        artifact_ids=("artifact-c",),
        adapters=(
            {
                "contribution_id": "adapter-ref",
                "source_artifact_id": "artifact-c",
                "source_payload_digest": payload_tree_digest(adapter_entries),
                "source_size_bytes": 7,
                "adapter_id": "adapter-a",
                "adapter_format": "lora",
                "base_model": "base",
            },
        ),
        renderer=_renderer("adapter-ref"),
    )
    request = _request()
    response = _response(request, first, second, third)

    result = _materializer(artifacts, materialized).materialize(
        request,
        response,
        (_row("artifact-a", single), _row("artifact-b", bundle), _row("artifact-c", adapter)),
    )

    assert result.request_digest == canonical_digest(request)
    assert result.instruction == "Alpha:\nfirst\n\nBeta:\nsecond"
    assert {item.name: item.value for item in result.environment} == {
        "OPENEVO_ONE_PATH": "/runtime/data/single.txt",
        "OPENEVO_MANY_PATHS": '["/runtime/data/single.txt","/runtime/data/inline.txt"]',
        "OPENEVO_DATA_ROOT": "/runtime/data",
        "OPENEVO_BUNDLE_DIR": "/runtime/extensions/bundle",
    }
    assert len(result.blobs) == 4
    assert [item.destination_relative_path for item in result.blobs] == [
        "single.txt",
        "inline.txt",
        "bundle/a.txt",
        "bundle/z.bin",
    ]
    assert result.adapter_merge_spec.merge_mode == "runtime_lora"
    assert result.adapter_merge_spec.adapters[0].source_payload_digest == payload_tree_digest(
        adapter_entries
    )
    assert result.projections == response.projections
    assert result.selection == response.selection
    reader = _materializer(artifacts, materialized)
    for blob in result.blobs:
        with _open_blob(reader, result, blob.blob_id) as lease:
            assert lease.blob == blob
            assert len(lease.stream.read()) == blob.size_bytes

    serialized = canonical_json(result)
    assert str(artifacts) not in serialized
    assert "file://" not in serialized
    assert "payload-artifact" not in serialized


def test_source_drift_fails_without_publishing_context(tmp_path: Path) -> None:
    artifacts = tmp_path / "artifacts"
    contexts = tmp_path / "contexts"
    artifacts.mkdir()
    payload = artifacts / "source.txt"
    payload.write_bytes(b"before")
    digest = hashlib.sha256(b"before").hexdigest()
    projection = TargetHandlerOutput(
        target_id="alpha",
        handler_id="alpha_handler",
        artifact_ids=("artifact-a",),
        staged_payloads=(
            {
                "contribution_id": "payload",
                "source_artifact_id": "artifact-a",
                "source_relative_path": "source.txt",
                "source_sha256": digest,
                "source_size_bytes": 6,
                "media_type": "text/plain",
                "payload_kind": "file",
                "destination_scope": "target_data",
                "destination_relative_path": "source.txt",
            },
        ),
        renderer=_renderer("payload"),
    )
    request = _request()
    response = _response(request, projection, context_id="ctx-drift")
    _PayloadService.mutate_after_issue = payload

    with pytest.raises(ValueError, match="drift"):
        _materializer(artifacts, contexts).materialize(
            request, response, (_row("artifact-a", payload),)
        )

    assert not (contexts / "ctx-drift").exists()
    assert not list(contexts.glob(".ctx-drift.*"))


def test_adapter_content_drift_fails_without_publishing_context(tmp_path: Path) -> None:
    artifacts = tmp_path / "artifacts"
    contexts = tmp_path / "contexts"
    adapter_dir = artifacts / "adapter"
    adapter_dir.mkdir(parents=True)
    weights = adapter_dir / "weights.bin"
    weights.write_bytes(b"before")
    entries = (
        PayloadManifestEntry(
            relative_path="weights.bin",
            media_type="application/octet-stream",
            size_bytes=6,
            sha256=hashlib.sha256(b"before").hexdigest(),
        ),
    )
    projection = TargetHandlerOutput(
        target_id="gamma",
        handler_id="gamma_handler",
        artifact_ids=("artifact-adapter",),
        adapters=(
            {
                "contribution_id": "adapter-ref",
                "source_artifact_id": "artifact-adapter",
                "source_payload_digest": payload_tree_digest(entries),
                "source_size_bytes": 6,
                "adapter_id": "adapter-a",
                "adapter_format": "lora",
                "base_model": "base",
            },
        ),
        renderer=_renderer("adapter-ref"),
    )
    request = _request()
    _PayloadService.mutate_after_issue = weights

    with pytest.raises(ValueError, match="drift"):
        _materializer(artifacts, contexts).materialize(
            request,
            _response(request, projection, context_id="ctx-adapter-drift"),
            (_row("artifact-adapter", adapter_dir),),
        )

    assert not (contexts / "ctx-adapter-drift").exists()


def test_conflicts_and_context_limits_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    source = artifacts / "source.txt"
    source.write_text("x", encoding="utf-8")
    request = _request()

    def inline(
        target: str, contribution: str, destination: str, text: str = "x"
    ) -> TargetHandlerOutput:
        return TargetHandlerOutput(
            target_id=target,
            handler_id=f"{target}_handler",
            artifact_ids=(f"artifact-{target}",),
            instructions=(
                {
                    "contribution_id": f"instruction-{target}",
                    "source_artifact_ids": (f"artifact-{target}",),
                    "text": text,
                },
            ),
            staged_payloads=(
                {
                    "source_kind": "inline_text",
                    "contribution_id": contribution,
                    "source_artifact_ids": (f"artifact-{target}",),
                    "text": text,
                    "media_type": "text/plain",
                    "destination_scope": "target_data",
                    "destination_relative_path": destination,
                },
            ),
            environment=(
                {
                    "name": "OPENEVO_SHARED",
                    "value_contribution_ids": (contribution,),
                    "value_kind": "path",
                },
            ),
            renderer=_renderer(f"instruction-{target}"),
        )

    left = inline("left", "left-file", "same.txt")
    right = inline("right", "right-file", "same.txt")
    rows = (_row("artifact-left", source), _row("artifact-right", source))
    with pytest.raises(ValueError, match="destination|environment"):
        _materializer(artifacts, tmp_path / "contexts-a").materialize(
            request, _response(request, left, right), rows
        )

    monkeypatch.setattr(context_materialization, "MAX_CONTEXT_BLOBS", 1)
    limited = inline("limited", "limited-file", "one.txt")
    limited = limited.model_copy(
        update={
            "staged_payloads": limited.staged_payloads
            + (
                limited.staged_payloads[0].model_copy(
                    update={
                        "contribution_id": "second-file",
                        "destination_relative_path": "two.txt",
                    }
                ),
            )
        }
    )
    with pytest.raises(ValueError, match="blob"):
        _materializer(artifacts, tmp_path / "contexts-b").materialize(
            request, _response(request, limited), (_row("artifact-limited", source),)
        )

    monkeypatch.setattr(context_materialization, "MAX_CONTEXT_BLOBS", 4096)
    monkeypatch.setattr(context_materialization, "MAX_CONTEXT_INSTRUCTION_BYTES", 1)
    with pytest.raises(ValueError, match="instruction"):
        _materializer(artifacts, tmp_path / "contexts-c").materialize(
            request,
            _response(request, inline("long", "long-file", "long.txt", "xx")),
            (_row("artifact-long", source),),
        )

    monkeypatch.setattr(context_materialization, "MAX_CONTEXT_INSTRUCTION_BYTES", 1_048_576)
    monkeypatch.setattr(context_materialization, "MAX_CONTEXT_ENVIRONMENT_BYTES", 1)
    with pytest.raises(ValueError, match="environment"):
        _materializer(artifacts, tmp_path / "contexts-d").materialize(
            request,
            _response(request, inline("env", "env-file", "env.txt")),
            (_row("artifact-env", source),),
        )

    monkeypatch.setattr(context_materialization, "MAX_CONTEXT_ENVIRONMENT_BYTES", 1_048_576)
    monkeypatch.setattr(context_materialization, "MAX_CONTEXT_MANIFEST_BYTES", 32)
    with pytest.raises(ValueError, match="manifest.*byte limit"):
        _materializer(artifacts, tmp_path / "contexts-e").materialize(
            request,
            _response(request, inline("bytes", "bytes-file", "bytes.txt", "xx")),
            (_row("artifact-bytes", source),),
        )
    assert not (tmp_path / "contexts-e" / "ctx-test").exists()

    monkeypatch.setattr(context_materialization, "MAX_CONTEXT_MANIFEST_BYTES", 64 * 1024 * 1024)
    monkeypatch.setattr(context_materialization, "MAX_PAYLOAD_TOTAL_BYTES", 1)
    with pytest.raises(ValueError, match="decoded/materialized bytes"):
        _materializer(artifacts, tmp_path / "contexts-f").materialize(
            request,
            _response(request, inline("bytes", "bytes-file", "bytes.txt", "xx")),
            (_row("artifact-bytes", source),),
        )


def test_partial_copy_failure_cleans_private_temp_and_blob_lease_verifies_manifest(
    tmp_path: Path,
) -> None:
    artifacts = tmp_path / "artifacts"
    contexts = tmp_path / "contexts"
    artifacts.mkdir()
    source = artifacts / "source.txt"
    source.write_bytes(b"x")
    request = _request()
    projection = TargetHandlerOutput(
        target_id="alpha",
        handler_id="alpha_handler",
        artifact_ids=("artifact-a",),
        staged_payloads=(
            {
                "source_kind": "inline_text",
                "contribution_id": "one",
                "source_artifact_ids": ("artifact-a",),
                "text": "one",
                "media_type": "text/plain",
                "destination_scope": "target_data",
                "destination_relative_path": "one.txt",
            },
            {
                "source_kind": "inline_text",
                "contribution_id": "two",
                "source_artifact_ids": ("artifact-a",),
                "text": "two",
                "media_type": "text/plain",
                "destination_scope": "target_data",
                "destination_relative_path": "two.txt",
            },
        ),
        renderer=_renderer("one"),
    )
    result = _materializer(artifacts, contexts).materialize(
        request,
        _response(request, projection, context_id="ctx-lookup"),
        (_row("artifact-a", source),),
    )
    assert result.adapter_merge_spec.merge_mode == "reference_only"
    materializer = _materializer(artifacts, contexts)
    path = contexts / "ctx-lookup" / "blobs" / result.blobs[0].blob_id
    with pytest.raises(ValueError, match="identity changed during transport"):
        with _open_blob(materializer, result, result.blobs[0].blob_id) as lease:
            replacement = contexts / "replacement"
            replacement.write_bytes(b"tampered")
            os.replace(replacement, path)
            assert lease.stream.read() == b"one"
    with pytest.raises(ValueError, match="manifest|digest|size"):
        with _open_blob(materializer, result, result.blobs[0].blob_id):
            pass

    file_projection = TargetHandlerOutput(
        target_id="copy",
        handler_id="copy_handler",
        artifact_ids=("artifact-a",),
        staged_payloads=(
            {
                "contribution_id": "copy-file",
                "source_artifact_id": "artifact-a",
                "source_relative_path": "source.txt",
                "source_sha256": hashlib.sha256(b"x").hexdigest(),
                "source_size_bytes": 1,
                "media_type": "text/plain",
                "payload_kind": "file",
                "destination_scope": "target_data",
                "destination_relative_path": "copy.txt",
            },
        ),
        renderer=_renderer("copy-file"),
    )
    _PayloadService.fail_after_copies = 0
    with pytest.raises(ValueError, match="copy failure"):
        _materializer(artifacts, contexts).materialize(
            request,
            _response(request, file_projection, context_id="ctx-partial"),
            (_row("artifact-a", source),),
        )
    assert not (contexts / "ctx-partial").exists()
    assert not list(contexts.glob(".ctx-partial.*"))


def test_publish_rejects_blob_hardlink_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifacts = tmp_path / "artifacts"
    contexts = tmp_path / "contexts"
    artifacts.mkdir()
    source = artifacts / "source.txt"
    source.write_text("source", encoding="utf-8")
    projection = TargetHandlerOutput(
        target_id="alpha",
        handler_id="alpha_handler",
        artifact_ids=("artifact-a",),
        staged_payloads=(
            {
                "source_kind": "inline_text",
                "contribution_id": "inline",
                "source_artifact_ids": ("artifact-a",),
                "text": "private",
                "media_type": "text/plain",
                "destination_scope": "target_data",
                "destination_relative_path": "private.txt",
            },
        ),
        renderer=_renderer("inline"),
    )
    original_open = context_materialization._open_materialized_blob_file
    external_link = tmp_path / "external-link"

    def hardlink_after_open(directory_fd: int, blob_id: str) -> int:
        descriptor = original_open(directory_fd, blob_id)
        os.link(
            blob_id,
            external_link,
            src_dir_fd=directory_fd,
            follow_symlinks=False,
        )
        return descriptor

    monkeypatch.setattr(
        context_materialization,
        "_open_materialized_blob_file",
        hardlink_after_open,
    )
    request = _request()
    with pytest.raises(ValueError, match="private regular blob|identity"):
        _materializer(artifacts, contexts).materialize(
            request,
            _response(request, projection, context_id="ctx-hardlink"),
            (_row("artifact-a", source),),
        )

    assert not (contexts / "ctx-hardlink").exists()
    assert not list(contexts.glob(".ctx-hardlink.*"))


def test_publish_does_not_replace_directory_created_before_final_rename(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifacts = tmp_path / "artifacts"
    contexts = tmp_path / "contexts"
    artifacts.mkdir()
    source = artifacts / "source.txt"
    source.write_text("source", encoding="utf-8")
    context_id = "ctx-publish-collision"
    replacement_identity = None

    def create_replacement(root_descriptor: int, observed_context_id: str) -> None:
        nonlocal replacement_identity
        assert observed_context_id == context_id
        os.mkdir(observed_context_id, dir_fd=root_descriptor)
        replacement_identity = context_materialization._private_file_identity(
            os.stat(observed_context_id, dir_fd=root_descriptor, follow_symlinks=False)
        )

    monkeypatch.setattr(
        context_materialization,
        "_before_materialized_context_rename",
        create_replacement,
        raising=False,
    )
    request = _request()
    with pytest.raises(FileExistsError):
        _materializer(artifacts, contexts).materialize(
            request,
            _response(request, _inline_projection(), context_id=context_id),
            (_row("artifact-a", source),),
        )

    assert replacement_identity is not None
    assert (
        context_materialization._private_file_identity(
            os.stat(contexts / context_id, follow_symlinks=False)
        )
        == replacement_identity
    )
    assert not any((contexts / context_id).iterdir())
    assert not list(contexts.glob(f".{context_id}.*"))
    quarantine = list(contexts.glob(".openevo-quarantine-*"))
    assert len(quarantine) == 1
    assert (quarantine[0] / "manifest.json").read_bytes() == b""
    assert list((quarantine[0] / "blobs").iterdir())
    assert all(path.read_bytes() == b"" for path in (quarantine[0] / "blobs").iterdir())


def test_publish_fails_closed_when_noreplace_is_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifacts = tmp_path / "artifacts"
    contexts = tmp_path / "contexts"
    artifacts.mkdir()
    source = artifacts / "source.txt"
    source.write_text("source", encoding="utf-8")
    context_id = "ctx-no-noreplace"

    def unsupported_noreplace(*_args, **_kwargs) -> None:
        raise OSError(errno.ENOSYS, "renameat2 is unavailable")

    monkeypatch.setattr(
        context_materialization,
        "_rename_noreplace",
        unsupported_noreplace,
        raising=False,
    )
    request = _request()
    with pytest.raises(OSError, match="renameat2 is unavailable"):
        _materializer(artifacts, contexts).materialize(
            request,
            _response(request, _inline_projection(), context_id=context_id),
            (_row("artifact-a", source),),
        )

    assert not (contexts / context_id).exists()
    assert not list(contexts.glob(f".{context_id}.*"))


def test_publish_rebinds_final_context_path_after_rename(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifacts = tmp_path / "artifacts"
    contexts = tmp_path / "contexts"
    artifacts.mkdir()
    source = artifacts / "source.txt"
    source.write_text("source", encoding="utf-8")
    projection = TargetHandlerOutput(
        target_id="alpha",
        handler_id="alpha_handler",
        artifact_ids=("artifact-a",),
        staged_payloads=(
            {
                "source_kind": "inline_text",
                "contribution_id": "inline",
                "source_artifact_ids": ("artifact-a",),
                "text": "private",
                "media_type": "text/plain",
                "destination_scope": "target_data",
                "destination_relative_path": "private.txt",
            },
        ),
        renderer=_renderer("inline"),
    )
    moved_name = "moved-context"

    def move_after_rename(root_descriptor: int, context_id: str) -> None:
        os.rename(
            context_id,
            moved_name,
            src_dir_fd=root_descriptor,
            dst_dir_fd=root_descriptor,
        )
        replacement = contexts / context_id
        replacement.mkdir()
        (replacement / "sentinel.txt").write_text("replacement", encoding="utf-8")

    monkeypatch.setattr(
        context_materialization,
        "_after_materialized_context_rename",
        move_after_rename,
    )
    request = _request()
    with pytest.raises(ValueError, match="published context path|identity"):
        _materializer(artifacts, contexts).materialize(
            request,
            _response(request, projection, context_id="ctx-publish-move"),
            (_row("artifact-a", source),),
        )

    assert (contexts / "ctx-publish-move" / "sentinel.txt").read_text(
        encoding="utf-8"
    ) == "replacement"
    assert (contexts / moved_name).is_dir()


def test_discard_does_not_delete_a_replacement_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifacts = tmp_path / "artifacts"
    contexts = tmp_path / "contexts"
    artifacts.mkdir()
    source = artifacts / "source.txt"
    source.write_text("source", encoding="utf-8")
    projection = TargetHandlerOutput(
        target_id="alpha",
        handler_id="alpha_handler",
        artifact_ids=("artifact-a",),
        staged_payloads=(
            {
                "source_kind": "inline_text",
                "contribution_id": "inline",
                "source_artifact_ids": ("artifact-a",),
                "text": "private",
                "media_type": "text/plain",
                "destination_scope": "target_data",
                "destination_relative_path": "private.txt",
            },
        ),
        renderer=_renderer("inline"),
    )
    materializer = _materializer(artifacts, contexts)
    request = _request()
    result = materializer.materialize(
        request,
        _response(request, projection, context_id="ctx-discard-race"),
        (_row("artifact-a", source),),
    )
    moved_name = "ctx-discard-original"

    def replace_before_discard(root_descriptor: int, context_id: str) -> None:
        os.rename(
            context_id,
            moved_name,
            src_dir_fd=root_descriptor,
            dst_dir_fd=root_descriptor,
        )
        replacement = contexts / context_id
        replacement.mkdir()
        (replacement / "sentinel.txt").write_text("replacement", encoding="utf-8")

    monkeypatch.setattr(
        context_materialization,
        "_before_materialized_context_discard",
        replace_before_discard,
        raising=False,
    )
    with pytest.raises(ValueError, match="discard|identity|invalid"):
        materializer.discard(result.context_id)

    preserved = list(contexts.glob(".openevo-preserved-*"))
    assert len(preserved) == 1
    assert (preserved[0] / "sentinel.txt").read_text(encoding="utf-8") == "replacement"
    assert (contexts / moved_name / "manifest.json").is_file()


def test_manifest_verification_rejects_symlink_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifacts = tmp_path / "artifacts"
    contexts = tmp_path / "contexts"
    artifacts.mkdir()
    source = artifacts / "source.txt"
    source.write_text("source", encoding="utf-8")
    outside = tmp_path / "outside-manifest.json"
    outside.write_text('{"outside":true}', encoding="utf-8")
    projection = TargetHandlerOutput(
        target_id="alpha",
        handler_id="alpha_handler",
        artifact_ids=("artifact-a",),
        instructions=(
            {
                "contribution_id": "instruction",
                "source_artifact_ids": ("artifact-a",),
                "text": "private",
            },
        ),
        renderer=_renderer("instruction"),
    )

    def replace_manifest(temporary_descriptor: int) -> None:
        os.rename(
            "manifest.json",
            "manifest-original.json",
            src_dir_fd=temporary_descriptor,
            dst_dir_fd=temporary_descriptor,
        )
        os.symlink(outside, "manifest.json", dir_fd=temporary_descriptor)

    monkeypatch.setattr(
        context_materialization,
        "_after_materialized_manifest_write",
        replace_manifest,
        raising=False,
    )
    request = _request()
    with pytest.raises(ValueError, match="manifest|regular|opened safely"):
        _materializer(artifacts, contexts).materialize(
            request,
            _response(request, projection, context_id="ctx-manifest-race"),
            (_row("artifact-a", source),),
        )
    assert outside.read_text(encoding="utf-8") == '{"outside":true}'


def test_failed_publish_cleanup_preserves_a_replacement_temp_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifacts = tmp_path / "artifacts"
    contexts = tmp_path / "contexts"
    artifacts.mkdir()
    source = artifacts / "source.txt"
    source.write_text("source", encoding="utf-8")
    projection = TargetHandlerOutput(
        target_id="alpha",
        handler_id="alpha_handler",
        artifact_ids=("artifact-a",),
        instructions=(
            {
                "contribution_id": "instruction",
                "source_artifact_ids": ("artifact-a",),
                "text": "private",
            },
        ),
        renderer=_renderer("instruction"),
    )

    def fail_after_manifest(_temporary_descriptor: int) -> None:
        raise RuntimeError("injected publish failure")

    def replace_before_cleanup(root_descriptor: int, temporary_name: str) -> None:
        os.rename(
            temporary_name,
            "moved-temp-tree",
            src_dir_fd=root_descriptor,
            dst_dir_fd=root_descriptor,
        )
        replacement = contexts / temporary_name
        replacement.mkdir()
        (replacement / "sentinel.txt").write_text("replacement", encoding="utf-8")

    monkeypatch.setattr(
        context_materialization,
        "_after_materialized_manifest_write",
        fail_after_manifest,
    )
    monkeypatch.setattr(
        context_materialization,
        "_before_temporary_context_cleanup",
        replace_before_cleanup,
    )
    request = _request()
    with pytest.raises(RuntimeError, match="injected publish failure"):
        _materializer(artifacts, contexts).materialize(
            request,
            _response(request, projection, context_id="ctx-temp-race"),
            (_row("artifact-a", source),),
        )

    preserved = list(contexts.glob(".openevo-preserved-*"))
    assert len(preserved) == 1
    assert (preserved[0] / "sentinel.txt").read_text(encoding="utf-8") == "replacement"
    assert (contexts / "moved-temp-tree" / "manifest.json").is_file()


def test_blob_lease_hides_fd_and_early_close_does_not_leak_internal_descriptors(
    tmp_path: Path,
) -> None:
    artifacts = tmp_path / "artifacts"
    contexts = tmp_path / "contexts"
    artifacts.mkdir()
    source = artifacts / "source.txt"
    source.write_text("source", encoding="utf-8")
    projection = TargetHandlerOutput(
        target_id="alpha",
        handler_id="alpha_handler",
        artifact_ids=("artifact-a",),
        staged_payloads=(
            {
                "source_kind": "inline_text",
                "contribution_id": "inline",
                "source_artifact_ids": ("artifact-a",),
                "text": "private",
                "media_type": "text/plain",
                "destination_scope": "target_data",
                "destination_relative_path": "private.txt",
            },
        ),
        renderer=_renderer("inline"),
    )
    materializer = _materializer(artifacts, contexts)
    request = _request()
    result = materializer.materialize(
        request,
        _response(request, projection, context_id="ctx-fd-lease"),
        (_row("artifact-a", source),),
    )
    before = len(os.listdir("/proc/self/fd"))

    with _open_blob(materializer, result, result.blobs[0].blob_id) as lease:
        with pytest.raises(io.UnsupportedOperation, match="does not expose"):
            lease.stream.fileno()
        lease.stream.close()

    assert len(os.listdir("/proc/self/fd")) <= before


def test_blob_reader_rejects_disk_manifest_that_self_signs_forged_content(
    tmp_path: Path,
) -> None:
    artifacts = tmp_path / "artifacts"
    contexts = tmp_path / "contexts"
    artifacts.mkdir()
    source = artifacts / "source.txt"
    source.write_text("source", encoding="utf-8")
    projection = TargetHandlerOutput(
        target_id="alpha",
        handler_id="alpha_handler",
        artifact_ids=("artifact-a",),
        staged_payloads=(
            {
                "source_kind": "inline_text",
                "contribution_id": "inline",
                "source_artifact_ids": ("artifact-a",),
                "text": "trusted",
                "media_type": "text/plain",
                "destination_scope": "target_data",
                "destination_relative_path": "private.txt",
            },
        ),
        renderer=_renderer("inline"),
    )
    materializer = _materializer(artifacts, contexts)
    request = _request()
    result = materializer.materialize(
        request,
        _response(request, projection, context_id="ctx-forged-manifest"),
        (_row("artifact-a", source),),
    )
    forged_bytes = b"forged"
    original_blob = result.blobs[0]
    forged_blob = original_blob.model_copy(
        update={
            "size_bytes": len(forged_bytes),
            "sha256": hashlib.sha256(forged_bytes).hexdigest(),
        }
    )
    (contexts / result.context_id / "blobs" / original_blob.blob_id).write_bytes(forged_bytes)
    (contexts / result.context_id / "manifest.json").write_text(
        canonical_json(result.model_copy(update={"blobs": (forged_blob,)})),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="persisted manifest"):
        with _open_blob(materializer, result, original_blob.blob_id):
            pass


def test_directory_open_closes_descriptor_when_fstat_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifacts = tmp_path / "artifacts"
    contexts = tmp_path / "contexts"
    artifacts.mkdir()
    contexts.mkdir()
    (contexts / "child").mkdir()
    materializer = _materializer(artifacts, contexts)
    parent_descriptor = os.open(contexts, os.O_RDONLY | os.O_DIRECTORY)
    before = len(list(Path("/proc/self/fd").iterdir()))

    def fail_fstat(_descriptor: int):
        raise OSError("injected fstat failure")

    monkeypatch.setattr(context_materialization.os, "fstat", fail_fstat)
    with pytest.raises(OSError, match="injected fstat failure"):
        materializer._open_directory_at(
            parent_descriptor,
            "child",
            label="child",
        )
    after = len(list(Path("/proc/self/fd").iterdir()))
    os.close(parent_descriptor)
    assert after == before


def test_private_directory_init_failure_preserves_replacement_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "contexts"
    root.mkdir(mode=0o700)
    root_descriptor = os.open(
        root,
        os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_DIRECTORY,
    )
    original_open = ContextMaterializer._open_directory_at
    replacement_identity = None

    def replace_after_open(parent_fd: int, name: str, *, label: str) -> int:
        nonlocal replacement_identity
        descriptor = original_open(parent_fd, name, label=label)
        os.rename(
            name,
            "moved-original",
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
        )
        os.mkdir(name, mode=0o700, dir_fd=parent_fd)
        replacement_identity = context_materialization._private_file_identity(
            os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        )
        return descriptor

    monkeypatch.setattr(context_materialization.secrets, "token_hex", lambda _size: "fixed")
    monkeypatch.setattr(
        ContextMaterializer,
        "_open_directory_at",
        staticmethod(replace_after_open),
    )
    try:
        with pytest.raises(ValueError, match="identity|stable"):
            ContextMaterializer._create_private_directory_at(
                root_descriptor,
                prefix=".temporary.",
            )
    finally:
        os.close(root_descriptor)

    replacement = root / ".temporary.fixed"
    assert replacement_identity is not None
    assert replacement.is_dir()
    assert (
        context_materialization._private_file_identity(os.stat(replacement, follow_symlinks=False))
        == replacement_identity
    )
    assert (root / "moved-original").is_dir()
    assert not list(root.glob(".openevo-preserved-*"))
    assert not list(root.glob(".openevo-quarantine-*"))


@pytest.mark.parametrize(
    ("failure_stage", "expected_entry"),
    [
        ("open", ".temporary.fixed"),
        ("identity", ".temporary.fixed"),
        ("mode", ".openevo-quarantine-fixed"),
        ("path_identity", ".openevo-quarantine-fixed"),
    ],
)
def test_private_directory_init_failure_cleans_only_a_bound_inode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_stage: str,
    expected_entry: str,
) -> None:
    root = tmp_path / "contexts"
    root.mkdir(mode=0o700)
    root_descriptor = os.open(
        root,
        os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_DIRECTORY,
    )
    original_open = ContextMaterializer._open_directory_at
    opened_descriptor: int | None = None

    def tracked_open(parent_fd: int, name: str, *, label: str) -> int:
        nonlocal opened_descriptor
        if failure_stage == "open":
            raise OSError("injected directory open failure")
        opened_descriptor = original_open(parent_fd, name, label=label)
        return opened_descriptor

    real_fstat = os.fstat

    def injected_fstat(descriptor: int):
        if failure_stage == "identity" and descriptor == opened_descriptor:
            raise OSError("injected identity failure")
        return real_fstat(descriptor)

    def injected_fchmod(_descriptor: int, _mode: int) -> None:
        raise OSError("injected mode failure")

    def injected_path_identity(*_args, **_kwargs) -> None:
        raise ValueError("injected path identity failure")

    monkeypatch.setattr(context_materialization.secrets, "token_hex", lambda _size: "fixed")
    monkeypatch.setattr(
        ContextMaterializer,
        "_open_directory_at",
        staticmethod(tracked_open),
    )
    if failure_stage == "identity":
        monkeypatch.setattr(context_materialization.os, "fstat", injected_fstat)
    elif failure_stage == "mode":
        monkeypatch.setattr(context_materialization.os, "fchmod", injected_fchmod)
    elif failure_stage == "path_identity":
        monkeypatch.setattr(
            ContextMaterializer,
            "_require_directory_path_identity",
            staticmethod(injected_path_identity),
        )
    try:
        with pytest.raises((OSError, ValueError), match="injected"):
            ContextMaterializer._create_private_directory_at(
                root_descriptor,
                prefix=".temporary.",
            )
    finally:
        os.close(root_descriptor)

    assert (root / expected_entry).is_dir()
    assert len(list(root.iterdir())) == 1


def test_manifest_zero_progress_write_fails_without_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifacts = tmp_path / "artifacts"
    contexts = tmp_path / "contexts"
    artifacts.mkdir()
    request = _request()
    monkeypatch.setattr(context_materialization.os, "write", lambda _fd, _data: 0)

    with pytest.raises(OSError, match="manifest write made no progress"):
        _materializer(artifacts, contexts).materialize(
            request,
            _response(request, context_id="ctx-zero-write"),
            (),
        )

    assert not (contexts / "ctx-zero-write").exists()
    assert not list(contexts.glob(".ctx-zero-write.*"))


def test_publication_receipt_detects_post_return_context_and_blob_replacement(
    tmp_path: Path,
) -> None:
    artifacts = tmp_path / "artifacts"
    contexts = tmp_path / "contexts"
    artifacts.mkdir()
    source = artifacts / "source.txt"
    source.write_text("source", encoding="utf-8")
    materializer = _materializer(artifacts, contexts)
    request = _request()
    receipt = materializer.materialize_for_publication(
        request,
        _response(request, _inline_projection(), context_id="ctx-publication-receipt"),
        (_row("artifact-a", source),),
    )

    assert receipt.materialized_context.context_id == "ctx-publication-receipt"
    assert receipt.canonical_manifest_bytes == canonical_json(receipt.materialized_context).encode(
        "utf-8"
    )
    assert set(dict(receipt.blob_identities)) == {receipt.materialized_context.blobs[0].blob_id}
    with pytest.raises(TypeError, match="ephemeral|persist"):
        pickle.dumps(receipt)
    materializer.verify_publication(receipt)

    context_dir = contexts / receipt.materialized_context.context_id
    blob = receipt.materialized_context.blobs[0]
    original_blob = context_dir / "blobs" / f"{blob.blob_id}.original"
    (context_dir / "blobs" / blob.blob_id).rename(original_blob)
    (context_dir / "blobs" / blob.blob_id).write_bytes(b"private")
    with pytest.raises(ValueError, match="publication|blob|identity"):
        materializer.verify_publication(receipt)

    moved_context = contexts / "moved-published-context"
    context_dir.rename(moved_context)
    context_dir.mkdir()
    (context_dir / "sentinel.txt").write_text("replacement", encoding="utf-8")
    with pytest.raises(ValueError, match="publication|context|identity"):
        materializer.verify_publication(receipt)

    assert (context_dir / "sentinel.txt").read_text(encoding="utf-8") == "replacement"
    assert (moved_context / "manifest.json").is_file()


def test_receipt_bound_discard_does_not_trust_entry_replaced_on_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifacts = tmp_path / "artifacts"
    contexts = tmp_path / "contexts"
    artifacts.mkdir()
    source = artifacts / "source.txt"
    source.write_text("source", encoding="utf-8")
    materializer = _materializer(artifacts, contexts)
    request = _request()
    receipt = materializer.materialize_for_publication(
        request,
        _response(request, _inline_projection(), context_id="ctx-receipt-discard"),
        (_row("artifact-a", source),),
    )

    def replace_before_discard(root_descriptor: int, context_id: str) -> None:
        os.rename(
            context_id,
            "moved-receipt-context",
            src_dir_fd=root_descriptor,
            dst_dir_fd=root_descriptor,
        )
        replacement = contexts / context_id
        replacement.mkdir()
        (replacement / "sentinel.txt").write_text("replacement", encoding="utf-8")

    monkeypatch.setattr(
        context_materialization,
        "_before_materialized_context_discard",
        replace_before_discard,
    )

    assert materializer.discard_publication(receipt) == "mismatch"
    replacement = contexts / receipt.materialized_context.context_id
    assert (replacement / "sentinel.txt").read_text(encoding="utf-8") == "replacement"
    assert (contexts / "moved-receipt-context" / "manifest.json").is_file()


def test_receipt_bound_discard_quarantines_original_without_path_deletion(
    tmp_path: Path,
) -> None:
    artifacts = tmp_path / "artifacts"
    contexts = tmp_path / "contexts"
    artifacts.mkdir()
    source = artifacts / "source.txt"
    source.write_text("source", encoding="utf-8")
    materializer = _materializer(artifacts, contexts)
    request = _request()
    receipt = materializer.materialize_for_publication(
        request,
        _response(request, _inline_projection(), context_id="ctx-receipt-quarantine"),
        (_row("artifact-a", source),),
    )

    assert materializer.discard_publication(receipt) == "preserved"
    assert not (contexts / receipt.materialized_context.context_id).exists()
    quarantine = list(contexts.glob(".openevo-quarantine-*"))
    assert len(quarantine) == 1
    assert (quarantine[0] / "manifest.json").read_bytes() == b""
    assert list((quarantine[0] / "blobs").iterdir())
    assert all(path.read_bytes() == b"" for path in (quarantine[0] / "blobs").iterdir())


def test_quarantine_cleanup_reports_preserved_missing_and_mismatch(tmp_path: Path) -> None:
    root = tmp_path / "contexts"
    root.mkdir(mode=0o700)
    root_descriptor = os.open(
        root,
        os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_DIRECTORY,
    )
    try:
        quarantined = root / "quarantined"
        quarantined.write_text("remove", encoding="utf-8")
        quarantined_identity = context_materialization._private_file_identity(
            os.stat("quarantined", dir_fd=root_descriptor, follow_symlinks=False)
        )
        assert (
            context_materialization._remove_materialized_entry_if_identity(
                root_descriptor,
                "quarantined",
                quarantined_identity,
            )
            == "preserved"
        )
        assert not quarantined.exists()
        assert (
            context_materialization._remove_materialized_entry_if_identity(
                root_descriptor,
                "absent",
                quarantined_identity,
            )
            == "missing"
        )

        expected = root / "expected"
        replacement = root / "replacement"
        expected.write_text("expected", encoding="utf-8")
        replacement.write_text("replacement", encoding="utf-8")
        expected_identity = context_materialization._private_file_identity(
            os.stat("expected", dir_fd=root_descriptor, follow_symlinks=False)
        )
        assert (
            context_materialization._remove_materialized_entry_if_identity(
                root_descriptor,
                "replacement",
                expected_identity,
            )
            == "mismatch"
        )
    finally:
        os.close(root_descriptor)

    quarantine = list(root.glob(".openevo-quarantine-*"))
    assert len(quarantine) == 1
    assert quarantine[0].read_bytes() == b""
    preserved = list(root.glob(".openevo-preserved-*"))
    assert len(preserved) == 1
    assert preserved[0].read_text(encoding="utf-8") == "replacement"


@pytest.mark.parametrize("replacement_kind", ["file", "directory", "symlink"])
def test_quarantine_never_deletes_path_replaced_after_held_fd_identity_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    replacement_kind: str,
) -> None:
    root = tmp_path / "contexts"
    root.mkdir(mode=0o700)
    candidate = root / "candidate"
    candidate.mkdir()
    (candidate / "nested.txt").write_text("remove", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "sentinel.txt").write_text("outside", encoding="utf-8")
    (candidate / "outside-link").symlink_to(outside, target_is_directory=True)
    root_descriptor = os.open(
        root,
        os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_DIRECTORY,
    )
    expected = context_materialization._private_file_identity(
        os.stat("candidate", dir_fd=root_descriptor, follow_symlinks=False)
    )
    observed_quarantine: list[Path] = []
    delete_calls: list[str] = []
    real_ftruncate = os.ftruncate

    def replace_during_held_file_truncate(descriptor: int, length: int) -> None:
        quarantine = next(root.glob(".openevo-quarantine-*"))
        observed_quarantine.append(quarantine)
        original = root / f"original-{replacement_kind}"
        quarantine.rename(original)
        held_child = original / "nested.txt"
        held_child.rename(original / "nested-original.txt")
        if replacement_kind == "file":
            quarantine.write_bytes(b"replacement")
            held_child.write_bytes(b"child replacement")
        elif replacement_kind == "directory":
            quarantine.mkdir()
            (quarantine / "sentinel.txt").write_bytes(b"replacement")
            held_child.mkdir()
            (held_child / "sentinel.txt").write_bytes(b"child replacement")
        else:
            quarantine.symlink_to(outside, target_is_directory=True)
            held_child.symlink_to(outside, target_is_directory=True)
        real_ftruncate(descriptor, length)

    def reject_path_delete(path, *args, **kwargs) -> None:
        del args, kwargs
        delete_calls.append(os.fspath(path))
        pytest.fail("quarantine cleanup attempted a pathname delete")

    try:
        with monkeypatch.context() as race:
            race.setattr(
                context_materialization.os, "ftruncate", replace_during_held_file_truncate
            )
            race.setattr(context_materialization.os, "unlink", reject_path_delete)
            race.setattr(context_materialization.os, "rmdir", reject_path_delete)
            result = context_materialization._remove_materialized_entry_if_identity(
                root_descriptor,
                "candidate",
                expected,
            )
    finally:
        os.close(root_descriptor)

    assert result == "preserved"
    assert not candidate.exists()
    assert len(observed_quarantine) == 1
    assert not delete_calls
    replacement = observed_quarantine[0]
    if replacement_kind == "file":
        assert replacement.read_bytes() == b"replacement"
    elif replacement_kind == "directory":
        assert (replacement / "sentinel.txt").read_bytes() == b"replacement"
    else:
        assert replacement.is_symlink()
        assert (outside / "sentinel.txt").read_text(encoding="utf-8") == "outside"
    original = root / f"original-{replacement_kind}"
    child_replacement = original / "nested.txt"
    if replacement_kind == "file":
        assert child_replacement.read_bytes() == b"child replacement"
    elif replacement_kind == "directory":
        assert (child_replacement / "sentinel.txt").read_bytes() == b"child replacement"
    else:
        assert child_replacement.is_symlink()
    assert (original / "nested-original.txt").read_bytes() == b""
    assert (original / "outside-link").is_symlink()
    assert (outside / "sentinel.txt").read_text(encoding="utf-8") == "outside"


@pytest.mark.parametrize(
    "prefix",
    [
        context_materialization._QUARANTINE_ENTRY_PREFIX,
        context_materialization._TRASH_ENTRY_PREFIX,
    ],
)
def test_startup_cleanup_skips_existing_core_quarantine_entries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    prefix: str,
) -> None:
    root = tmp_path / "contexts"
    root.mkdir(mode=0o700)
    quarantine = root / f"{prefix}fixed"
    quarantine.mkdir()
    sentinel = quarantine / "sentinel.txt"
    sentinel.write_text("maintenance", encoding="utf-8")
    root_descriptor = os.open(
        root,
        os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_DIRECTORY,
    )
    identity = context_materialization._private_file_identity(
        os.stat(quarantine.name, dir_fd=root_descriptor, follow_symlinks=False)
    )

    def reject_rename(*_args, **_kwargs) -> None:
        pytest.fail("startup attempted to rename a Core quarantine entry")

    try:
        with monkeypatch.context() as startup:
            startup.setattr(context_materialization.os, "rename", reject_rename)
            result = context_materialization._remove_materialized_entry_if_identity(
                root_descriptor,
                quarantine.name,
                identity,
            )
    finally:
        os.close(root_descriptor)

    assert result == "preserved"
    assert sentinel.read_text(encoding="utf-8") == "maintenance"


def test_persisted_manifest_verification_uses_db_authority_and_same_root_fd(
    tmp_path: Path,
) -> None:
    artifacts = tmp_path / "artifacts"
    contexts = tmp_path / "contexts"
    artifacts.mkdir()
    source = artifacts / "source.txt"
    source.write_text("source", encoding="utf-8")
    materializer = _materializer(artifacts, contexts)
    request = _request()
    result = materializer.materialize(
        request,
        _response(request, _inline_projection(), context_id="ctx-startup-verify"),
        (_row("artifact-a", source),),
    )

    materializer.verify_persisted_materialization(result)
    context_dir = contexts / result.context_id
    blob = result.blobs[0]
    (context_dir / "blobs" / blob.blob_id).write_bytes(b"tampered")
    with pytest.raises(ValueError, match="persisted|blob|digest|size"):
        materializer.verify_persisted_materialization(result)

    (context_dir / "manifest.json").write_text(
        canonical_json(result.model_copy(update={"instruction": "forged"})),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="persisted manifest"):
        materializer.verify_persisted_materialization(result)
