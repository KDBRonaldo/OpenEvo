from __future__ import annotations

import asyncio
import hashlib
import json
import time
from datetime import datetime, timezone

import httpx
import pytest

from openevo.harness.models import AgentSpec
from openevo.backend.contracts.v2.models import (
    WorkspaceArchiveDeclarationV2,
    WorkspaceSnapshotRefV2,
)
from openevo.backend.workspace_handoff_v2 import WorkspaceHandoffBindingV2
from openevo.rollout.balancer import NodeScheduler
from openevo.rollout.models import (
    NodeRegistrationRequest,
    NodeStageMetrics,
    SessionContext,
    SessionDispatchRequest,
    SessionResult,
    SessionStatus,
    SessionTiming,
    TaskRequest,
)
from openevo.rollout.pipeline import Pipeline
from openevo.runtime.models import PrepareAction, RuntimeSpec
from openevo.trajectory.models import EvaluatorSpec, StrategySpec, Trace, Trajectory


@pytest.mark.asyncio
async def test_run_batch_preserves_gateway_contract_and_cleans_up(monkeypatch) -> None:
    gateway_url = "http://gateway.test"
    scheduler = NodeScheduler()
    scheduler.register_node(
        NodeRegistrationRequest(
            node_id="node-a",
            gateway_url=gateway_url,
            max_init_workers=1,
            max_run_workers=1,
            max_postrun_workers=1,
            heartbeat_interval_seconds=30,
        )
    )

    runtime = RuntimeSpec(
        backend="docker",
        image="openevo/test-runtime:latest",
        prepare=[PrepareAction(type="exec", command="mkdir -p /workspace")],
        env={"TASK_ENV": "enabled"},
        workdir="/workspace",
        cpus=2,
        memory_mb=1024,
    )
    agent = AgentSpec(
        harness="codex",
        model_name="test-model",
        settings={"capture_mode": "transcript", "reasoning_effort": "high"},
        env={"AGENT_ENV": "enabled"},
    )
    builder = StrategySpec(strategy="agent_transcript", config={"include_stderr": True})
    evaluator = EvaluatorSpec(
        strategy="command",
        config={"command": "python evaluate.py"},
        env={"EVAL_ENV": "enabled"},
        refresh_runtime=True,
    )
    metadata = {"sample_index": 3, "tags": ["contract", "rollout"]}
    empty_archive = WorkspaceArchiveDeclarationV2(
        format="openevo_deterministic_tar_v1",
        media_type="application/vnd.openevo.workspace-tar",
        content_sha256=hashlib.sha256(bytes(1024)).hexdigest(),
        byte_size=1024,
        entry_count=0,
        extracted_byte_size=0,
    )
    workspace_handoff = WorkspaceHandoffBindingV2(
        handoff_id="workspace-handoff-contract",
        task_id="task-contract",
        attempt_id="attempt-contract",
        task_admission_id="task-admission-contract",
        admission_sha256="1" * 64,
        project_id="project-contract",
        input_workspace_snapshot=WorkspaceSnapshotRefV2(
            workspace_snapshot_id="workspace-contract",
            project_id="project-contract",
            manifest_sha256="2" * 64,
            entry_count=0,
            byte_size=0,
        ),
        input_archive=empty_archive,
        service_generation_sha256="3" * 64,
        registry_sha256="4" * 64,
        framework_lock_sha256="5" * 64,
        created_at=datetime(2026, 7, 23, 4, tzinfo=timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z"),
    )
    task_request = TaskRequest(
        task_id="task-contract",
        instruction="Solve the contract probe.",
        timeout_seconds=10.0,
        runtime=runtime,
        agent=agent,
        builder=builder,
        evaluator=evaluator,
        metadata=metadata,
        workspace_handoff=workspace_handoff,
    )
    session = SessionContext(
        session_id="session-contract",
        task_id=task_request.task_id,
        request=task_request,
        deadline_monotonic=time.monotonic() + task_request.timeout_seconds,
    )
    expected_trajectory = Trajectory(
        status=SessionStatus.COMPLETED,
        metadata={"builder": builder.strategy, "record_count": 1},
        traces=[
            Trace(
                prompt_ids=[11, 12],
                response_ids=[21, 22],
                loss_mask=[1, 1],
                response_logprobs=[-0.2, -0.1],
                reward=0.75,
            )
        ],
    )
    expected_result = SessionResult(
        session_id=session.session_id,
        task_id=session.task_id,
        status=SessionStatus.COMPLETED,
        trajectory=expected_trajectory,
        timing=SessionTiming(init_ms=4.0, run_ms=8.0, postrun_ms=2.0),
        node_id="node-a",
        metadata={"gateway": "typed-result"},
    )

    received_dispatch: SessionDispatchRequest | None = None
    cleanup_observed = False
    reservation_seen_on_dispatch = False

    async def gateway_handler(request: httpx.Request) -> httpx.Response:
        nonlocal received_dispatch, cleanup_observed, reservation_seen_on_dispatch
        if request.method == "POST" and request.url.path == "/sessions":
            received_dispatch = SessionDispatchRequest.model_validate(json.loads(request.content))
            node = scheduler.get_node("node-a")
            assert node is not None
            assert node.dispatch_reservations == 1
            reservation_seen_on_dispatch = True
            # A real gateway heartbeat reconciles accepted dispatch reservations.
            heartbeat = scheduler.heartbeat(
                "node-a",
                metrics=NodeStageMetrics(init_queue_depth=1),
            )
            assert heartbeat.dispatch_reservations == 0
            return httpx.Response(202, json={"status": SessionStatus.REGISTERED})
        if request.method == "GET" and request.url.path == f"/sessions/{session.session_id}":
            return httpx.Response(
                200,
                json={
                    "status": SessionStatus.COMPLETED,
                    "result": expected_result.model_dump(mode="json"),
                },
            )
        if request.method == "DELETE" and request.url.path == f"/sessions/{session.session_id}":
            cleanup_observed = True
            return httpx.Response(200)
        return httpx.Response(404)

    async_client = httpx.AsyncClient
    monkeypatch.setattr(
        "openevo.rollout.pipeline.httpx.AsyncClient",
        lambda **kwargs: async_client(transport=httpx.MockTransport(gateway_handler), **kwargs),
    )
    pipeline = Pipeline(
        callback_url="http://rollout.test/session-results",
        save_dir=None,
        scheduler=scheduler,
        dispatch_poll_interval_seconds=0.001,
        callback_grace_seconds=0.1,
    )

    try:
        results = await pipeline.run_batch([session])
    finally:
        await pipeline.close()

    assert received_dispatch is not None
    assert received_dispatch.runtime == runtime
    assert received_dispatch.agent == agent
    assert received_dispatch.builder == builder
    assert received_dispatch.evaluator == evaluator
    assert received_dispatch.metadata == metadata
    assert received_dispatch.workspace_handoff == workspace_handoff

    assert len(results) == 1
    assert isinstance(results[0], SessionResult)
    assert isinstance(results[0].trajectory, Trajectory)
    assert results[0] == expected_result
    assert results[0].trajectory == expected_trajectory
    assert session.rollout_result is results[0]

    assert cleanup_observed is True
    assert reservation_seen_on_dispatch is True
    node = scheduler.get_node("node-a")
    assert node is not None
    assert node.dispatch_reservations == 0


@pytest.mark.asyncio
async def test_cancel_task_waits_for_gateway_session_termination(monkeypatch) -> None:
    gateway_url = "http://gateway.test"
    scheduler = NodeScheduler()
    scheduler.register_node(
        NodeRegistrationRequest(
            node_id="node-cancel",
            gateway_url=gateway_url,
            max_init_workers=1,
            max_run_workers=1,
            max_postrun_workers=1,
            heartbeat_interval_seconds=30,
        )
    )
    request = TaskRequest(
        task_id="task-cancel",
        instruction="Wait for cancellation.",
        timeout_seconds=10,
        runtime=RuntimeSpec(backend="docker", image="runtime:test"),
        agent=AgentSpec(harness="codex", settings={"capture_mode": "transcript"}),
    )
    session = SessionContext(
        session_id="session-cancel",
        task_id=request.task_id,
        request=request,
        deadline_monotonic=time.monotonic() + request.timeout_seconds,
    )
    dispatched = asyncio.Event()
    delete_entered = asyncio.Event()
    allow_termination = asyncio.Event()
    delete_calls = 0

    async def gateway_handler(http_request: httpx.Request) -> httpx.Response:
        nonlocal delete_calls
        if http_request.method == "POST" and http_request.url.path == "/sessions":
            dispatched.set()
            return httpx.Response(202, json={"status": SessionStatus.REGISTERED})
        if http_request.method == "GET":
            return httpx.Response(200, json={"status": SessionStatus.REGISTERED})
        if http_request.method == "DELETE":
            delete_calls += 1
            if delete_calls == 1:
                delete_entered.set()
                await allow_termination.wait()
                return httpx.Response(200)
            return httpx.Response(404)
        return httpx.Response(404)

    async_client = httpx.AsyncClient
    monkeypatch.setattr(
        "openevo.rollout.pipeline.httpx.AsyncClient",
        lambda **kwargs: async_client(transport=httpx.MockTransport(gateway_handler), **kwargs),
    )
    pipeline = Pipeline(
        callback_url="http://rollout.test/session-results",
        save_dir=None,
        scheduler=scheduler,
        dispatch_poll_interval_seconds=0.001,
        callback_grace_seconds=0.1,
    )
    batch = asyncio.create_task(pipeline.run_batch([session]))
    try:
        await asyncio.wait_for(dispatched.wait(), timeout=1)
        cancellation = asyncio.create_task(pipeline.cancel_task(request.task_id))
        await asyncio.wait_for(delete_entered.wait(), timeout=1)
        assert cancellation.done() is False
        allow_termination.set()
        await asyncio.wait_for(cancellation, timeout=1)
        results = await asyncio.wait_for(batch, timeout=1)
        assert len(results) == 1
        assert results[0].status is SessionStatus.ERROR
        assert results[0].error == "session cancelled"
        assert delete_calls >= 2
    finally:
        allow_termination.set()
        if not batch.done():
            batch.cancel()
            await asyncio.gather(batch, return_exceptions=True)
        await pipeline.close()
