from __future__ import annotations

import hashlib
import os
from datetime import datetime, timezone

import pytest

from openevo.backend.contracts.v2.models import WorkspaceSnapshotRefV2
from openevo.backend.workspace_handoff_v2 import (
    WorkspaceHandoffRequestV2,
    WorkspaceHandoffStoreV2,
)
from openevo.gateway.dispatcher import ManagedSession
from openevo.gateway.node import GatewayNodeManager
from openevo.gateway.session import SessionRegistry
from openevo.gateway.session_files import capture_session_root_identity
from openevo.gateway.storage import SessionStore
from openevo.harness.models import AgentSpec
from openevo.internal_auth import InternalServiceIdentity
from openevo.rollout.models import SessionDispatchRequest, SessionResult, SessionStatus
from openevo.rollout.timer import StageTimer
from openevo.trajectory.models import Trace, Trajectory
from openevo.trajectory.registry import default_builder_registry, default_evaluator_registry
from openevo.workspace_archive import write_workspace_archive


@pytest.mark.asyncio
async def test_gateway_publishes_workspace_before_terminal_cleanup_and_retries(
    tmp_path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "input.txt").write_text("before\n", encoding="utf-8")
    probe = tmp_path / "probe.tar"
    descriptor = os.open(probe, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        input_archive = write_workspace_archive(source, descriptor)
    finally:
        os.close(descriptor)
        probe.unlink()

    identity = InternalServiceIdentity(
        service_id="gateway",
        generation_digest="3" * 64,
        registry_digest="4" * 64,
        framework_lock_digest="5" * 64,
        credential="gateway-workspace-handoff-credential-0123456789",
    )
    store = WorkspaceHandoffStoreV2(tmp_path / "handoffs")
    handoff_request = WorkspaceHandoffRequestV2(
        task_id="task-handoff",
        attempt_id="attempt-handoff",
        task_admission_id="task-admission-handoff",
        admission_sha256="1" * 64,
        project_id="project-handoff",
        input_workspace_snapshot=WorkspaceSnapshotRefV2(
            workspace_snapshot_id="workspace-input",
            project_id="project-handoff",
            manifest_sha256="2" * 64,
            entry_count=input_archive.entry_count,
            byte_size=input_archive.extracted_byte_size,
        ),
        input_archive=input_archive,
        service_generation_sha256=identity.generation_digest,
        registry_sha256=identity.registry_digest,
        framework_lock_sha256=identity.framework_lock_digest,
    )
    binding = store.reserve(
        handoff_request,
        source,
        now=datetime.now(timezone.utc),
    )
    session_id = "sk-openevo-session-1"
    store.claim(
        binding,
        session_id=session_id,
        generation_sha256=identity.generation_digest,
        registry_sha256=identity.registry_digest,
        framework_lock_sha256=identity.framework_lock_digest,
    )
    session_dir = tmp_path / "session"
    workspace = session_dir / "workspace"
    artifacts = session_dir / "artifacts"
    workspace.mkdir(parents=True)
    artifacts.mkdir()
    (workspace / "answer.txt").write_text("after\n", encoding="utf-8")
    dispatch = SessionDispatchRequest(
        session_id=session_id,
        task_id=binding.task_id,
        instruction="Produce a result.",
        remaining_timeout_seconds=30,
        agent=AgentSpec(
            harness="codex",
            settings={"auth_mode": "proxy", "capture_mode": "transcript"},
        ),
        workspace_handoff=binding,
    )
    managed = ManagedSession(
        request=dispatch,
        timer=StageTimer(),
        session_dir=session_dir,
        artifacts_dir=artifacts,
        session_root_identity=capture_session_root_identity(session_dir),
    )
    result = SessionResult(
        session_id=session_id,
        task_id=binding.task_id,
        status=SessionStatus.COMPLETED,
        trajectory=Trajectory(
            status=SessionStatus.COMPLETED,
            metadata={"capture_mode": "transcript", "record_count": 1},
            traces=[
                Trace(
                    prompt_messages=[{"role": "user", "content": "Produce a result."}],
                    response_messages=[{"role": "assistant", "content": "Done."}],
                )
            ],
        ),
    )
    manager = GatewayNodeManager(
        node_id="gateway-handoff",
        gateway_url="http://gateway.test",
        max_init_workers=1,
        max_run_workers=1,
        max_postrun_workers=1,
        storage=SessionStore(),
        session_registry=SessionRegistry(),
        builders=default_builder_registry(),
        evaluators=default_evaluator_registry(),
        session_base_dir=str(tmp_path / "sessions"),
        workspace_handoff_store=store,
        service_identity=identity,
    )
    try:
        attached = await manager._attach_workspace_result_after_runtime_absence(
            managed,
            result,
        )
        assert attached.workspace_result is not None
        assert attached.workspace_result.output_archive.entry_count == 1
        assert attached.workspace_result.output_archive.extracted_byte_size == 6
        with store.open_result(attached.workspace_result) as stream:
            assert hashlib.sha256(stream.read()).hexdigest() == (
                attached.workspace_result.output_archive.content_sha256
            )

        (workspace / "answer.txt").unlink()
        workspace.rmdir()
        replay = await manager._attach_workspace_result_after_runtime_absence(
            managed,
            result,
        )
        assert replay.workspace_result == attached.workspace_result

        manager._cleanup_journal_dir = tmp_path / "journal"
        managed.final_result = result
        manager._register_cleanup_retry(managed, finalize_terminal=True)
        ownership = manager._cleanup_retries[session_id]
        assert ownership.finalization_state is not None
        assert ownership.finalization_state.request.workspace_handoff == binding
        ownership.managed = None
        restored = manager._restore_terminal_finalization(ownership)
        assert restored.request.workspace_handoff == binding
        assert restored.credential_dir is None
        assert restored.credential_mount is None
    finally:
        await manager._client.aclose()
        store.close()
