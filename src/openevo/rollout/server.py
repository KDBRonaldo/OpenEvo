"""FastAPI server for rollout orchestration."""

from __future__ import annotations

import logging
import os
import argparse
from contextlib import asynccontextmanager
from dataclasses import dataclass

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from openevo.config import RolloutServiceConfig, TopologyConfig
from openevo.platform.events import SSE_HEADERS, EventBus
from openevo.rollout.balancer import NodeScheduler
from openevo.rollout.manager import RolloutManager
from openevo.rollout.models import (
    GatewayNodeInfo,
    NodeHeartbeatRequest,
    NodeRegistrationRequest,
    SessionResult,
    TaskRequest,
    TaskStatus,
    canonicalize_task_request,
)
from openevo.rollout.pipeline import Pipeline
from openevo.internal_auth import (
    GenerationBoundRunAdmissionVerifier,
    INTERNAL_SERVICE_HEADER,
    InternalServiceIdentity,
    configured_run_admission_verifier,
    RunAdmissionOperation,
    health_identity_payload,
    inherited_listen_fd,
    install_internal_auth,
    read_internal_service_identity,
    require_generation_bound_run_admission,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


@dataclass(slots=True)
class RolloutState:
    topology: TopologyConfig
    rollout: RolloutServiceConfig
    scheduler: NodeScheduler
    pipeline: Pipeline
    manager: RolloutManager
    event_bus: EventBus
    registered_nodes: set[str]


_state: RolloutState | None = None
_configured_topology_path: str | None = None
_internal_identity: InternalServiceIdentity | None = None
_run_admission_verifier: GenerationBoundRunAdmissionVerifier | None = None


def configure_server(
    topology_path: str = "topology.yaml",
    *,
    internal_identity: InternalServiceIdentity | None = None,
    run_admission_verifier: GenerationBoundRunAdmissionVerifier | None = None,
) -> None:
    global _configured_topology_path, _internal_identity, _run_admission_verifier, _state
    _configured_topology_path = topology_path
    _internal_identity = internal_identity
    _run_admission_verifier = run_admission_verifier
    _state = None


def _build_state(topology: TopologyConfig) -> RolloutState:
    rollout = topology.rollout
    scheduler = NodeScheduler(bootstrap_nodes=topology.bootstrap_nodes)
    event_bus = EventBus()
    pipeline = Pipeline(
        callback_url=f"{rollout.public_url}/callbacks/session_result",
        save_dir=rollout.save_dir,
        scheduler=scheduler,
        dispatch_poll_interval_seconds=rollout.dispatch_poll_interval_seconds,
        callback_grace_seconds=rollout.callback_grace_seconds,
        event_bus=event_bus,
        internal_headers=(
            _internal_identity.request_headers() if _internal_identity is not None else None
        ),
    )
    manager = RolloutManager(pipeline=pipeline, scheduler=scheduler, event_bus=event_bus)
    return RolloutState(
        topology=topology,
        rollout=rollout,
        scheduler=scheduler,
        pipeline=pipeline,
        manager=manager,
        event_bus=event_bus,
        registered_nodes=set(),
    )


def get_state() -> RolloutState:
    global _state
    if _state is None:
        topology_path = _configured_topology_path or os.environ.get(
            "OPENEVO_TOPOLOGY",
            "topology.yaml",
        )
        _state = _build_state(TopologyConfig.load(topology_path))
    return _state


@asynccontextmanager
async def _lifespan(_: FastAPI):
    state = get_state()
    await state.pipeline.start()
    try:
        yield
    finally:
        await state.pipeline.close()


app = FastAPI(title="OpenEvo Rollout", version="0.1.0", lifespan=_lifespan)
install_internal_auth(app, lambda: _internal_identity)


@app.get("/health")
async def health():
    state = get_state()
    node = state.scheduler.get_node("core-gateway")
    registered = "core-gateway" in state.registered_nodes
    schedulable = bool(
        registered and node is not None and node.healthy and not node.draining
    )
    payload = {
        "status": "ok",
        "nodes": len(state.scheduler.list_nodes()),
        "gateway_registration": {
            "gateway_url": node.gateway_url if node is not None and registered else None,
            "node_id": "core-gateway",
            "registered": registered,
            "schedulable": schedulable,
        },
    }
    payload.update(health_identity_payload(_internal_identity))
    return payload


@app.post("/rollout/task/submit")
async def submit_task_async(request: TaskRequest):
    """Non-blocking task submission. Returns immediately with task_id.

    Poll ``GET /rollout/task/{task_id}`` until status becomes terminal.
    """
    canonical = canonicalize_task_request(request)
    await require_generation_bound_run_admission(
        identity=_internal_identity,
        verifier=_run_admission_verifier,
        operation=RunAdmissionOperation.ROLLOUT_TASK_SUBMIT,
        payload=canonical.payload,
        task_id=canonical.request.task_id,
        session_id=None,
    )
    state = get_state()
    try:
        task_id = await state.manager.submit_task(canonical.request)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"task_id": task_id, "status": "running"}


@app.get("/rollout/task/{task_id}", response_model=TaskStatus)
async def get_task(task_id: str):
    task = get_state().manager.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


@app.get("/rollout/status")
async def rollout_status():
    return get_state().manager.status()


@app.post("/nodes/register", response_model=GatewayNodeInfo)
async def register_node(request: NodeRegistrationRequest, http_request: Request):
    if _internal_identity is not None and http_request.headers.get(
        INTERNAL_SERVICE_HEADER
    ) != "gateway":
        raise HTTPException(status_code=403, detail="gateway caller identity mismatch")
    state = get_state()
    node = state.scheduler.register_node(request)
    state.registered_nodes.add(request.node_id)
    return node


@app.post("/nodes/{node_id}/heartbeat", response_model=GatewayNodeInfo)
async def node_heartbeat(
    node_id: str,
    request: NodeHeartbeatRequest,
    http_request: Request,
):
    if _internal_identity is not None and http_request.headers.get(
        INTERNAL_SERVICE_HEADER
    ) != "gateway":
        raise HTTPException(status_code=403, detail="gateway caller identity mismatch")
    state = get_state()
    if node_id not in state.registered_nodes:
        raise HTTPException(status_code=404, detail="Node has not registered")
    try:
        return state.scheduler.heartbeat(node_id, metrics=request.metrics)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/nodes", response_model=list[GatewayNodeInfo])
async def list_nodes():
    return get_state().scheduler.list_nodes()


@app.get("/nodes/{node_id}", response_model=GatewayNodeInfo)
async def get_node(node_id: str):
    node = get_state().scheduler.get_node(node_id)
    if node is None:
        raise HTTPException(status_code=404, detail="Node not found")
    return node


@app.delete("/nodes/{node_id}", response_model=GatewayNodeInfo)
async def drain_node(node_id: str):
    try:
        return get_state().scheduler.drain_node(node_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/callbacks/session_result")
async def session_result_callback(result: SessionResult, request: Request):
    if _internal_identity is not None and request.headers.get(
        INTERNAL_SERVICE_HEADER
    ) != "gateway":
        raise HTTPException(status_code=403, detail="gateway caller identity mismatch")
    await get_state().pipeline.accept_callback_result(result)
    return {"status": "accepted"}


@app.get("/tasks")
async def list_tasks(
    status: str | None = Query(default=None),
    harness: str | None = Query(default=None),
    since: float | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=1000),
):
    """List tasks tracked in-memory by RolloutManager."""
    state = get_state()
    tasks = state.manager.list_tasks()
    if status:
        tasks = [t for t in tasks if t["status"] == status]
    if harness:
        tasks = [t for t in tasks if t.get("harness") == harness]
    if since is not None:
        tasks = [t for t in tasks if (t.get("updated_at") or 0) >= since]
    tasks.sort(key=lambda t: (t.get("updated_at") or 0), reverse=True)
    return {"tasks": tasks[:limit]}


@app.get("/tasks/{task_id}/sessions")
async def list_task_sessions(task_id: str):
    """Per-session summaries for a task currently in memory."""
    state = get_state()
    sessions = state.manager.list_sessions_for(task_id)
    if sessions is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return {"task_id": task_id, "sessions": sessions}


@app.get("/events")
async def stream_events(request: Request):
    state = get_state()

    async def iterator():
        async for chunk in state.event_bus.stream_events(heartbeat_seconds=15.0):
            if await request.is_disconnected():
                break
            yield chunk

    return StreamingResponse(
        iterator(),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


def serve(topology_path: str = "topology.yaml", *, log_level: str = "info") -> None:
    import uvicorn

    internal_identity = read_internal_service_identity(
        required=False,
        expected_service_id="rollout",
    )
    configure_server(
        topology_path,
        internal_identity=internal_identity,
        run_admission_verifier=configured_run_admission_verifier(internal_identity),
    )
    state = get_state()
    listen_fd = inherited_listen_fd()
    if internal_identity is not None and listen_fd is None:
        raise RuntimeError("release-owned rollout requires an inherited listener")
    kwargs = {"app": app, "log_level": log_level}
    if listen_fd is None:
        kwargs.update(host=state.rollout.host, port=state.rollout.port)
    else:
        kwargs["fd"] = listen_fd
    uvicorn.run(**kwargs)


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m openevo.rollout.server")
    parser.add_argument("--config", default=os.environ.get("OPENEVO_TOPOLOGY", "topology.yaml"))
    parser.add_argument("--log-level", default="info")
    args = parser.parse_args()
    serve(args.config, log_level=args.log_level)


if __name__ == "__main__":
    main()
