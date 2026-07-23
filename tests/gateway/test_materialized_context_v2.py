from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile

import pytest

from openevo.backend.contracts.v2.models import (
    EffectiveExecutionSnapshotRefV2,
    EvolutionRevisionRefV2,
    ProjectHeadRefV2,
    RuntimeContextSnapshotRefV2,
    WorkspaceSnapshotRefV2,
)
from openevo.backend.runtime_context_binding_v2 import RuntimeContextBindingV2
from openevo.config import EvolutionConfig
from openevo.evolution.context_materialization import (
    MaterializedBlob,
    MaterializedContext,
    MaterializedEnvironmentBinding,
)
from openevo.evolution.framework import canonical_digest
from openevo.gateway.dispatcher import ManagedSession
from openevo.gateway.node import (
    GatewayNodeManager,
    _EvolutionInjection,
    _runtime_injection_receipt_from_readback,
    _stage_materialized_runtime_context,
)
from openevo.harness.models import AgentSpec
from openevo.internal_auth import InternalServiceIdentity
from openevo.rollout.models import SessionDispatchRequest
from openevo.rollout.timer import StageTimer
from openevo.runtime import base as runtime_base
from openevo.runtime.base import RUNTIME_READBACK_MAX_BYTES


class _Runtime:
    def __init__(self) -> None:
        self.uploads: dict[str, bytes] = {}

    async def upload_file(self, local_path: str, remote_path: str) -> None:
        self.uploads[remote_path] = Path(local_path).read_bytes()

    async def download_dir(self, remote_path: str, local_path: str) -> None:
        prefix = f"{remote_path.rstrip('/')}/"
        target_root = Path(local_path)
        for path, payload in self.uploads.items():
            if not path.startswith(prefix):
                continue
            target = target_root / path.removeprefix(prefix)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)


class _Evolution:
    def __init__(self, context: MaterializedContext, blob: bytes) -> None:
        self.context = context
        self.blob = blob
        self.resolve_calls = 0
        self.blob_calls: list[tuple[str, str]] = []

    async def resolve_context(self, _payload):
        self.resolve_calls += 1
        raise AssertionError("v2 runtime injection must not call the legacy resolver")

    async def get_materialized_context(self, context_id: str):
        assert context_id == self.context.context_id
        return self.context.model_dump(mode="json")

    async def get_materialized_blob(self, context_id: str, blob_id: str) -> bytes:
        self.blob_calls.append((context_id, blob_id))
        return self.blob


def _binding(context: MaterializedContext) -> RuntimeContextBindingV2:
    project_id = "project-runtime-context"
    evolution = EvolutionRevisionRefV2(
        evolution_revision_id="evolution-successor",
        project_id=project_id,
        manifest_sha256="2" * 64,
        artifact_count=1,
    )
    runtime = RuntimeContextSnapshotRefV2(
        runtime_context_snapshot_id="runtime-context-successor",
        project_id=project_id,
        evolution_revision_id=evolution.evolution_revision_id,
        evolution_revision_manifest_sha256=evolution.manifest_sha256,
        registry_sha256=context.registry_digest,
        runtime_contract_sha256="3" * 64,
        manifest_sha256="4" * 64,
    )
    head = ProjectHeadRefV2(
        project_head_id="project-head-successor",
        project_id=project_id,
        generation=1,
        predecessor_project_head_id="project-head-genesis",
        workspace_snapshot=WorkspaceSnapshotRefV2(
            workspace_snapshot_id="workspace-successor",
            project_id=project_id,
            manifest_sha256="5" * 64,
            entry_count=1,
            byte_size=8,
        ),
        evolution_revision=evolution,
        runtime_context_snapshot=runtime,
        effective_execution_snapshot=EffectiveExecutionSnapshotRefV2(
            effective_execution_snapshot_id="execution-successor",
            project_id=project_id,
            execution_mode="codex_subscription_transcript",
            capture_mode="transcript",
            token_level_metrics_available=False,
            producer_id="subscription-snapshot-issuer-v1",
            snapshot_sha256="6" * 64,
        ),
        registry_sha256=context.registry_digest,
        manifest_sha256="7" * 64,
    )
    return RuntimeContextBindingV2(
        source="materialized_successor",
        project_head=head,
        service_generation_sha256="8" * 64,
        framework_lock_sha256="9" * 64,
        successor_transition_id=context.successor_transition_id,
        source_predecessor_project_head_id=context.predecessor_project_head_id,
        materialized_context_id=context.context_id,
        materialized_context_manifest_sha256=canonical_digest(context),
        selected_artifact_ids=context.selection.artifact_ids,
    )


def _genesis_binding(registry_sha256: str) -> RuntimeContextBindingV2:
    project_id = "project-runtime-context"
    evolution = EvolutionRevisionRefV2(
        evolution_revision_id="evolution-genesis",
        project_id=project_id,
        manifest_sha256="1" * 64,
        artifact_count=0,
    )
    runtime = RuntimeContextSnapshotRefV2(
        runtime_context_snapshot_id="runtime-context-genesis",
        project_id=project_id,
        evolution_revision_id=evolution.evolution_revision_id,
        evolution_revision_manifest_sha256=evolution.manifest_sha256,
        registry_sha256=registry_sha256,
        runtime_contract_sha256="2" * 64,
        manifest_sha256="3" * 64,
    )
    return RuntimeContextBindingV2(
        source="empty_genesis",
        project_head=ProjectHeadRefV2(
            project_head_id="project-head-genesis",
            project_id=project_id,
            generation=0,
            predecessor_project_head_id=None,
            workspace_snapshot=WorkspaceSnapshotRefV2(
                workspace_snapshot_id="workspace-genesis",
                project_id=project_id,
                manifest_sha256="4" * 64,
                entry_count=0,
                byte_size=0,
            ),
            evolution_revision=evolution,
            runtime_context_snapshot=runtime,
            effective_execution_snapshot=EffectiveExecutionSnapshotRefV2(
                effective_execution_snapshot_id="execution-genesis",
                project_id=project_id,
                execution_mode="codex_subscription_transcript",
                capture_mode="transcript",
                token_level_metrics_available=False,
                producer_id="subscription-snapshot-issuer-v1",
                snapshot_sha256="5" * 64,
            ),
            registry_sha256=registry_sha256,
            manifest_sha256="6" * 64,
        ),
        service_generation_sha256="7" * 64,
        framework_lock_sha256="8" * 64,
    )


def _context(blob: bytes) -> MaterializedContext:
    return MaterializedContext(
        context_id="context-successor",
        request_digest="a" * 64,
        registry_digest="b" * 64,
        successor_transition_id="successor-transition",
        predecessor_project_head_id="project-head-genesis",
        base_model="gpt-5.5",
        projections=(),
        selection={
            "artifact_ids": ("artifact-memory",),
            "skipped_artifacts": (),
            "reasons": ("explicit_artifact_ids",),
        },
        blobs=(
            MaterializedBlob(
                blob_id="blob-memory",
                target_id="text_memory",
                handler_id="text_memory_handler",
                contribution_id="memory_file",
                source_artifact_ids=("artifact-memory",),
                destination_scope="target_data",
                destination_relative_path="memory.md",
                media_type="text/markdown",
                size_bytes=len(blob),
                sha256=hashlib.sha256(blob).hexdigest(),
            ),
        ),
        environment=(
            MaterializedEnvironmentBinding(
                target_id="text_memory",
                handler_id="text_memory_handler",
                name="OPENEVO_MEMORY_FILE",
                value_kind="path",
                value="/openevo/session/evolution/memory.md",
                contribution_ids=("memory_file",),
                destination_scope="target_data",
            ),
        ),
        instruction="Use the following long-term memory for this task:\nRemember it.",
        adapter_merge_spec={"merge_mode": "reference_only", "adapters": ()},
    )


@pytest.mark.asyncio
async def test_gateway_stages_exact_committed_materialization_without_v1_fallback() -> None:
    memory = b"Remember it.\n"
    context = _context(memory)
    binding = _binding(context)
    evolution = _Evolution(context, memory)
    runtime = _Runtime()

    staged = await _stage_materialized_runtime_context(
        runtime=runtime,
        evolution_client=evolution,
        binding=binding,
        instruction="Do the next task.",
        target_dir="/openevo/session/evolution",
        base_model="gpt-5.5",
    )

    assert evolution.resolve_calls == 0
    assert evolution.blob_calls == [(context.context_id, "blob-memory")]
    assert runtime.uploads["/openevo/session/evolution/memory.md"] == memory
    assert (
        json.loads(runtime.uploads["/openevo/session/evolution/context.json"])["context_id"]
        == context.context_id
    )
    assert staged.env["OPENEVO_MEMORY_FILE"] == ("/openevo/session/evolution/memory.md")
    assert staged.injection_plan is not None
    assert staged.injection_plan.effective_instruction == (
        f"{context.instruction}\n\nTask:\nDo the next task."
    )
    assert str(binding.model_dump(mode="json")).find("file://") == -1


@pytest.mark.asyncio
async def test_gateway_rejects_materialized_blob_transport_drift() -> None:
    context = _context(b"expected")
    evolution = _Evolution(context, b"tampered")
    with pytest.raises(ValueError, match="blob.*changed"):
        await _stage_materialized_runtime_context(
            runtime=_Runtime(),
            evolution_client=evolution,
            binding=_binding(context),
            instruction="next",
            target_dir="/openevo/session/evolution",
            base_model="gpt-5.5",
        )


@pytest.mark.asyncio
async def test_gateway_rejects_an_oversized_context_before_blob_transport() -> None:
    context = _context(b"small")
    oversized_blob = context.blobs[0].model_copy(
        update={
            "size_bytes": RUNTIME_READBACK_MAX_BYTES,
            "sha256": "0" * 64,
        }
    )
    oversized = context.model_copy(update={"blobs": (oversized_blob,)})
    evolution = _Evolution(oversized, b"")

    with pytest.raises(ValueError, match="runtime readback budget"):
        await _stage_materialized_runtime_context(
            runtime=_Runtime(),
            evolution_client=evolution,
            binding=_binding(oversized),
            instruction="next",
            target_dir="/openevo/session/evolution",
            base_model="gpt-5.5",
        )

    assert evolution.blob_calls == []


@pytest.mark.asyncio
async def test_materialized_context_runtime_readback_seals_the_v2_receipt(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))

    def rename_noreplace(source_fd, source, target_fd, target):
        with pytest.raises(FileNotFoundError):
            os.stat(target, dir_fd=target_fd, follow_symlinks=False)
        os.rename(
            source,
            target,
            src_dir_fd=source_fd,
            dst_dir_fd=target_fd,
        )

    monkeypatch.setattr(
        runtime_base,
        "_rename_readback_cleanup_noreplace",
        rename_noreplace,
    )
    memory = b"Remember it.\n"
    context = _context(memory)
    runtime = _Runtime()
    staged = await _stage_materialized_runtime_context(
        runtime=runtime,
        evolution_client=_Evolution(context, memory),
        binding=_binding(context),
        instruction="next",
        target_dir="/openevo/session/evolution",
        base_model="gpt-5.5",
    )
    assert staged.injection_plan is not None
    receipt = await _runtime_injection_receipt_from_readback(
        runtime=runtime,
        target_dir="/openevo/session/evolution",
        plan=staged.injection_plan,
    )
    assert receipt == staged.injection_plan.authority
    assert receipt["schema_version"] == "4"
    assert receipt["context_id"] == context.context_id


@pytest.mark.asyncio
async def test_v2_dispatch_selects_materialized_transport_instead_of_legacy_resolve(
    tmp_path,
) -> None:
    memory = b"Remember it.\n"
    context = _context(memory)
    evolution = _Evolution(context, memory)
    runtime = _Runtime()
    binding = _binding(context)
    request = SessionDispatchRequest.model_construct(
        session_id="session-materialized",
        task_id="rollout-attempt-materialized",
        instruction="Do the next task.",
        remaining_timeout_seconds=30.0,
        runtime=None,
        agent=AgentSpec(
            harness="codex",
            model_name="gpt-5.5",
            settings={"auth_mode": "subscription", "capture_mode": "transcript"},
        ),
        metadata={
            "openevo": {
                "evolution_revision_id": (
                    binding.project_head.evolution_revision.evolution_revision_id
                ),
                "project_head_id": binding.project_head.project_head_id,
                "project_id": binding.project_head.project_id,
                "runtime_context_snapshot_id": (
                    binding.project_head.runtime_context_snapshot.runtime_context_snapshot_id
                ),
            }
        },
        workspace_handoff=None,
        runtime_context_binding=binding,
    )
    session_dir = tmp_path / "session"
    artifacts_dir = session_dir / "artifacts"
    artifacts_dir.mkdir(parents=True)
    managed = ManagedSession(
        request=request,
        timer=StageTimer(),
        session_dir=session_dir,
        artifacts_dir=artifacts_dir,
        runtime=runtime,
    )
    manager = object.__new__(GatewayNodeManager)
    manager.evolution = EvolutionConfig(enabled=True)
    manager.evolution_client = evolution
    manager.model_served = "gpt-5.5"
    manager._service_identity = InternalServiceIdentity(
        service_id="gateway",
        generation_digest=binding.service_generation_sha256,
        registry_digest=binding.project_head.registry_sha256,
        framework_lock_digest=binding.framework_lock_sha256,
        credential="materialized-context-test-credential-0123456789",
    )

    async def await_direct(awaitable, _managed):
        return await awaitable

    manager._await_with_budget = await_direct
    staged = await manager._resolve_and_inject_evolution_context(
        managed,
        object(),
    )

    assert isinstance(staged, _EvolutionInjection)
    assert evolution.resolve_calls == 0
    assert request.metadata["evolution"] == {
        "context_id": context.context_id,
        "context_injected": True,
        "context_source": "materialized_successor",
        "runtime_context_snapshot_id": (
            request.runtime_context_binding.project_head.runtime_context_snapshot.runtime_context_snapshot_id
        ),
    }


@pytest.mark.asyncio
async def test_v2_genesis_explicitly_skips_the_legacy_context_resolver(tmp_path) -> None:
    context = _context(b"unused")
    evolution = _Evolution(context, b"unused")
    binding = _genesis_binding(context.registry_digest)
    request = SessionDispatchRequest.model_construct(
        session_id="session-genesis",
        task_id="rollout-attempt-genesis",
        instruction="Do the first task.",
        remaining_timeout_seconds=30.0,
        runtime=None,
        agent=AgentSpec(
            harness="codex",
            model_name="gpt-5.5",
            settings={"auth_mode": "subscription", "capture_mode": "transcript"},
        ),
        metadata={
            "openevo": {
                "evolution_revision_id": (
                    binding.project_head.evolution_revision.evolution_revision_id
                ),
                "project_head_id": binding.project_head.project_head_id,
                "project_id": binding.project_head.project_id,
                "runtime_context_snapshot_id": (
                    binding.project_head.runtime_context_snapshot.runtime_context_snapshot_id
                ),
            }
        },
        workspace_handoff=None,
        runtime_context_binding=binding,
    )
    session_dir = tmp_path / "session"
    artifacts_dir = session_dir / "artifacts"
    artifacts_dir.mkdir(parents=True)
    managed = ManagedSession(
        request=request,
        timer=StageTimer(),
        session_dir=session_dir,
        artifacts_dir=artifacts_dir,
        runtime=_Runtime(),
    )
    manager = object.__new__(GatewayNodeManager)
    manager.evolution = EvolutionConfig(enabled=True)
    manager.evolution_client = evolution
    manager.model_served = "gpt-5.5"
    manager._service_identity = InternalServiceIdentity(
        service_id="gateway",
        generation_digest=binding.service_generation_sha256,
        registry_digest=binding.project_head.registry_sha256,
        framework_lock_digest=binding.framework_lock_sha256,
        credential="genesis-context-test-credential-0123456789",
    )

    injected = await manager._resolve_and_inject_evolution_context(
        managed,
        object(),
    )

    assert injected == {}
    assert evolution.resolve_calls == 0
    assert evolution.blob_calls == []
    assert request.metadata["evolution"] == {
        "context_id": None,
        "context_injected": False,
        "context_source": "empty_genesis",
        "runtime_context_snapshot_id": (
            binding.project_head.runtime_context_snapshot.runtime_context_snapshot_id
        ),
    }
