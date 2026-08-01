"""FastAPI gateway proxy server and gateway-node lifecycle entry point."""

from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse

from openevo.backend.workspace_handoff_v2 import (
    WORKSPACE_HANDOFF_ROOT_ENV,
    WorkspaceHandoffStoreV2,
)
from openevo.config import GatewayNodeConfig, TopologyConfig
from openevo.gateway.completion_writer import CompletionWriter
from openevo.gateway.detection import APIType, detect, extract_model
from openevo.gateway.engine import get_engine
from openevo.gateway.node import (
    CancelAuthorityPersistenceError,
    GatewayNodeManager,
    GatewayReadinessError,
)
from openevo.gateway.session_files import (
    HeldCodexCredentialAuthority,
    PreparedCodexCredentialSnapshot,
    SessionFileSecurityError,
)
from openevo.gateway.proxy import (
    InferenceClient,
    UpstreamError,
    UpstreamHTTPError,
    UpstreamTimeoutError,
)
from openevo.gateway.session import (
    InvalidSessionIdError,
    SessionCreateRequest,
    SessionCreateResponse,
    SessionDeleteResponse,
    SessionRegistry,
    SessionStatusResponse,
    clean_session_id,
    generate_session_id,
    resolve_session_id,
)
from openevo.gateway.storage import SessionStore
from openevo.gateway.transform import TransformManager
from openevo.gateway.transform.base import BaseTransformer
from openevo.platform.events import SSE_HEADERS, EventBus
from openevo.rollout.models import SessionDispatchRequest, SessionDispatchResponse, SessionStatus
from openevo.trajectory.registry import default_builder_registry, default_evaluator_registry
from openevo.evolution.client import EvolutionClient
from openevo.internal_auth import (
    GenerationBoundRunAdmissionVerifier,
    InternalServiceIdentity,
    configured_run_admission_verifier,
    RunAdmissionOperation,
    health_identity_payload,
    inherited_listen_fd,
    install_internal_auth,
    read_internal_service_identity,
    require_generation_bound_run_admission,
    RunAdmissionError,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


@dataclass(slots=True)
class GatewayState:
    topology: TopologyConfig
    node: GatewayNodeConfig
    inference: InferenceClient
    storage: SessionStore
    transform_manager: TransformManager
    session_registry: SessionRegistry
    node_manager: GatewayNodeManager
    completion_writer: CompletionWriter
    event_bus: EventBus


_state: GatewayState | None = None
_configured_topology_path: str | None = None
_configured_node_id: str | None = None
_internal_identity: InternalServiceIdentity | None = None
_run_admission_verifier: GenerationBoundRunAdmissionVerifier | None = None
_credential_authority: (
    HeldCodexCredentialAuthority | PreparedCodexCredentialSnapshot | None
) = None
_managed_execution_mode: str | None = None

_CODEX_SUBSCRIPTION_EXECUTION_MODE = "codex_subscription_transcript"
_SELF_DEPLOYED_EXECUTION_MODE = "self-deployed"
_MANAGED_EXECUTION_MODES = frozenset(
    {_CODEX_SUBSCRIPTION_EXECUTION_MODE, _SELF_DEPLOYED_EXECUTION_MODE}
)


def _require_credential_authority() -> None:
    if (
        _internal_identity is None
        or _managed_execution_mode == _SELF_DEPLOYED_EXECUTION_MODE
    ):
        return
    authority = _credential_authority
    try:
        if authority is None:
            raise SessionFileSecurityError("credential authority is unavailable")
        authority.verify()
    except SessionFileSecurityError as exc:
        raise RunAdmissionError(
            "credential_authority_changed",
            "The managed subscription credential authority changed after readiness.",
            status_code=503,
            retryable=True,
        ) from exc


def configure_server(
    topology_path: str = "topology.yaml",
    *,
    node_id: str | None = None,
    internal_identity: InternalServiceIdentity | None = None,
    run_admission_verifier: GenerationBoundRunAdmissionVerifier | None = None,
    credential_authority: (
        HeldCodexCredentialAuthority | PreparedCodexCredentialSnapshot | None
    ) = None,
    managed_execution_mode: str | None = None,
) -> None:
    global _configured_topology_path, _configured_node_id
    global _internal_identity, _run_admission_verifier, _credential_authority
    global _managed_execution_mode, _state
    if (
        managed_execution_mode is not None
        and managed_execution_mode not in _MANAGED_EXECUTION_MODES
    ):
        raise ValueError("managed Gateway execution mode is invalid")
    _configured_topology_path = topology_path
    _configured_node_id = node_id
    _internal_identity = internal_identity
    _run_admission_verifier = run_admission_verifier
    _credential_authority = credential_authority
    _managed_execution_mode = managed_execution_mode
    _state = None


def _build_state(topology: TopologyConfig, node_id: str | None) -> GatewayState:
    node = topology.select_gateway_node(node_id)
    inference = InferenceClient(node.inference_base_url, get_engine(node.engine))
    persistence_config = topology.gateway.completion_persistence
    save_dir = topology.rollout.save_dir
    completion_writer = CompletionWriter(
        save_dir=save_dir if save_dir else None,
        max_field_bytes=persistence_config.max_field_bytes,
        queue_size=persistence_config.queue_size,
        enabled=persistence_config.enabled and bool(save_dir),
    )
    storage = SessionStore(completion_writer=completion_writer)
    transform_manager = TransformManager()
    session_registry = SessionRegistry()
    builder_registry = default_builder_registry()
    evaluator_registry = default_evaluator_registry()
    event_bus = EventBus()
    workspace_handoff_root = os.environ.get(WORKSPACE_HANDOFF_ROOT_ENV)
    workspace_handoff_store = (
        None
        if workspace_handoff_root is None
        else WorkspaceHandoffStoreV2(workspace_handoff_root)
    )
    # Wrap session_registry methods to emit events on state changes.
    _wrap_registry_for_events(session_registry, event_bus)
    node_manager = GatewayNodeManager(
        node_id=node.id,
        gateway_url=node.public_url,
        max_init_workers=node.max_init_workers,
        max_run_workers=node.max_run_workers,
        max_postrun_workers=node.max_postrun_workers,
        storage=storage,
        session_registry=session_registry,
        builders=builder_registry,
        evaluators=evaluator_registry,
        default_runtime=node.default_runtime,
        docker_host_path=node.docker_host_path,
        rollout_server_url=topology.gateway.rollout_server_url or None,
        heartbeat_interval_seconds=topology.gateway.heartbeat_interval_seconds,
        model_served=node.model_served,
        evolution=topology.evolution,
        evolution_client=(
            EvolutionClient(
                topology.evolution.backend_url,
                timeout_seconds=topology.evolution.context.timeout_seconds,
                headers=(
                    _internal_identity.request_headers()
                    if _internal_identity is not None
                    else None
                ),
            )
            if topology.evolution is not None and topology.evolution.enabled
            else None
        ),
        internal_headers=(
            _internal_identity.request_headers() if _internal_identity is not None else None
        ),
        credential_authority=_credential_authority,
        workspace_handoff_store=workspace_handoff_store,
        service_identity=_internal_identity,
    )
    return GatewayState(
        topology=topology,
        node=node,
        inference=inference,
        storage=storage,
        transform_manager=transform_manager,
        session_registry=session_registry,
        node_manager=node_manager,
        completion_writer=completion_writer,
        event_bus=event_bus,
    )


def _wrap_registry_for_events(registry: SessionRegistry, bus: EventBus) -> None:
    """Monkey-patch set_status / set_result so changes emit events to the bus."""
    original_set_status = registry.set_status
    original_set_result = registry.set_result

    def _bus_publish(event_type: str, payload: dict[str, Any]) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        bus.publish_threadsafe(loop, event_type, payload)

    def patched_set_status(session_id: str, status: str):
        info = original_set_status(session_id, status)
        if info is not None:
            _bus_publish(
                "session.state_changed",
                {
                    "session_id": session_id,
                    "task_id": info.task_id,
                    "status": status,
                },
            )
        return info

    def patched_set_result(session_id: str, result):
        info = original_set_result(session_id, result)
        if info is not None:
            _bus_publish(
                "session.state_changed",
                {
                    "session_id": session_id,
                    "task_id": info.task_id,
                    "status": str(info.status),
                },
            )
        return info

    registry.set_status = patched_set_status  # type: ignore[assignment]
    registry.set_result = patched_set_result  # type: ignore[assignment]


def get_state() -> GatewayState:
    global _state
    if _state is None:
        topology_path = _configured_topology_path or os.environ.get(
            "OPENEVO_TOPOLOGY",
            "topology.yaml",
        )
        node_id = _configured_node_id or os.environ.get("OPENEVO_GATEWAY_NODE_ID")
        _state = _build_state(TopologyConfig.load(topology_path), node_id)
    return _state


@asynccontextmanager
async def _lifespan(_: FastAPI):
    state = get_state()
    await state.completion_writer.start()
    await state.node_manager.start()
    try:
        yield
    finally:
        await state.node_manager.close()
        await state.inference.close()
        state.storage.close()
        await state.completion_writer.close()


app = FastAPI(title="OpenEvo Gateway", version="0.1.0", lifespan=_lifespan)
install_internal_auth(
    app,
    lambda: _internal_identity,
    protected_path=lambda path: (
        path == "/health"
        or path == "/events"
        or path == "/v1/models"
        or path.startswith("/admin/")
        or path == "/sessions"
        or path.startswith("/sessions/")
    ),
)


@app.api_route("/", methods=["GET", "HEAD"])
async def root() -> dict[str, str]:
    return {"status": "ok", "service": "openevo-gateway"}


def _format_anthropic_events(events: list[dict[str, Any]]) -> str:
    parts = []
    for event in events:
        event_type = event.get("type", "unknown")
        parts.append(f"event: {event_type}\ndata: {json.dumps(event)}\n\n")
    return "".join(parts)


def _format_openai_sse(chunk: dict[str, Any]) -> str:
    return f"data: {json.dumps(chunk, default=str)}\n\n"


def _format_responses_events(events: list[dict[str, Any]]) -> str:
    parts = []
    for event in events:
        event_type = event.get("type", "unknown")
        parts.append(f"event: {event_type}\ndata: {json.dumps(event)}\n\n")
    return "".join(parts)


def _format_google_sse(chunk: dict[str, Any]) -> str:
    return f"data: {json.dumps(chunk)}\n\n"


def _error_type_name(exc: Exception) -> str:
    if isinstance(exc, UpstreamTimeoutError):
        return "timeout_error"
    if isinstance(exc, UpstreamHTTPError):
        return "upstream_http_error"
    if isinstance(exc, UpstreamError):
        return "upstream_error"
    return type(exc).__name__


def _build_error_body(
    api_type: APIType,
    message: str,
    *,
    error_type: str,
    upstream_body: dict[str, Any] | str | None = None,
) -> dict[str, Any]:
    if api_type == APIType.ANTHROPIC:
        if isinstance(upstream_body, dict):
            if upstream_body.get("type") == "error" and isinstance(upstream_body.get("error"), dict):
                return upstream_body
            error = upstream_body.get("error")
            if isinstance(error, dict):
                return {
                    "type": "error",
                    "error": {
                        "type": error.get("type", "api_error"),
                        "message": error.get("message", message),
                    },
                }
        return {"type": "error", "error": {"type": "api_error", "message": message}}

    if isinstance(upstream_body, dict) and "error" in upstream_body:
        return upstream_body

    if api_type == APIType.GOOGLE:
        status = "DEADLINE_EXCEEDED" if error_type == "timeout_error" else "INTERNAL"
        return {"error": {"message": message, "status": status}}

    return {"error": {"message": message, "type": error_type}}


def _upstream_error_response(api_type: APIType, exc: Exception) -> JSONResponse:
    if isinstance(exc, UpstreamHTTPError):
        status_code = exc.status_code
        upstream_body = exc.body
    elif isinstance(exc, UpstreamTimeoutError):
        status_code = 504
        upstream_body = None
    elif isinstance(exc, UpstreamError):
        status_code = 502
        upstream_body = None
    else:
        status_code = 502
        upstream_body = None

    return JSONResponse(
        _build_error_body(
            api_type,
            str(exc),
            error_type=_error_type_name(exc),
            upstream_body=upstream_body,
        ),
        status_code=status_code,
    )


def _stream_error_output(api_type: APIType, exc: Exception) -> str:
    message = str(exc)
    error_type = _error_type_name(exc)

    if api_type == APIType.ANTHROPIC:
        return _format_anthropic_events([{
            "type": "error",
            "error": {"type": error_type, "message": message},
        }])
    if api_type == APIType.OPENAI_RESPONSES:
        return _format_responses_events([{"type": "error", "message": message}])
    if api_type == APIType.GOOGLE:
        status = "DEADLINE_EXCEEDED" if error_type == "timeout_error" else "INTERNAL"
        return _format_google_sse({"error": {"message": message, "status": status}})
    return _format_openai_sse({"error": {"message": message, "type": error_type}})


def _resolve_session_id(
    headers: dict[str, str],
    body: dict[str, Any],
    *,
    query_session_id: str | None = None,
    registry: SessionRegistry | None = None,
) -> str:
    return resolve_session_id(
        registry or get_state().session_registry,
        headers,
        body,
        query_session_id=query_session_id,
    )


def _coerce_datetime(value: str | None) -> datetime:
    if value:
        return datetime.fromisoformat(value)
    return datetime.now(timezone.utc)


def _session_response(session_id: str) -> SessionStatusResponse:
    state = get_state()
    metadata = state.storage.get_session_metadata(session_id)
    info = state.session_registry.get(session_id)
    result = info.result if info is not None else None
    if info is None and metadata is None and result is None:
        raise HTTPException(status_code=404, detail="Session not found")

    if info is not None:
        task_id = info.task_id
        created_at = info.created_at
        status = info.status
    else:
        task_id = (metadata or {}).get("task_id") or (result.task_id if result else None)
        created_at = _coerce_datetime((metadata or {}).get("created_at"))
        status = result.status if result is not None else SessionStatus.REGISTERED

    completion_count = int((metadata or {}).get("completion_count", 0))
    return SessionStatusResponse(
        session_id=session_id,
        task_id=task_id,
        created_at=created_at,
        completion_count=completion_count,
        status=status,
        result=result,
    )


def _format_stream_events(api_type: APIType, events: list[dict[str, Any]]) -> str:
    if api_type == APIType.ANTHROPIC:
        return _format_anthropic_events(events)
    if api_type == APIType.OPENAI_RESPONSES:
        return _format_responses_events(events)
    if api_type == APIType.GOOGLE:
        return _format_google_sse(events[0]) if events else ""
    return _format_openai_sse(events[0]) if events else ""


def _completion_metadata(session_info: Any | None) -> dict[str, Any]:
    metadata = dict(getattr(session_info, "metadata", None) or {})
    if session_info is not None:
        metadata.setdefault("session_id", session_info.session_id)
        if session_info.task_id is not None:
            metadata.setdefault("task_id", session_info.task_id)
    return metadata


def format_stream_output(
    api_type: APIType,
    transformer: BaseTransformer,
    chunk: dict[str, Any],
    original_request: dict[str, Any],
    is_first: bool,
) -> str:
    transformed = transformer.transform_stream_chunk(chunk, original_request, is_first=is_first)
    if api_type == APIType.ANTHROPIC:
        return _format_anthropic_events(transformed)
    if api_type == APIType.OPENAI_RESPONSES:
        if isinstance(transformed, list):
            return _format_responses_events(transformed)
        return _format_responses_events([transformed]) if transformed else ""
    if api_type == APIType.GOOGLE:
        return _format_google_sse(transformed)
    return _format_openai_sse(transformed)


@app.get("/v1/models")
async def list_models():
    state = get_state()
    try:
        return await state.inference.list_models()
    except Exception as exc:
        logger.error("Failed to list models: %s", exc)
        return JSONResponse({"error": str(exc)}, status_code=502)


@app.get("/health")
async def health():
    state = get_state()
    metrics = await state.node_manager.stage_metrics()
    if _internal_identity is not None:
        rollout_ready, rollout_message = await state.node_manager.internal_rollout_readiness()
        payload = {
            "status": "ok" if rollout_ready else "not_ready",
            "capture_mode": "transcript",
            "direct_model_api": False,
            "rollout_connected": rollout_ready,
            "rollout_message": rollout_message,
            "token_level_metrics_available": False,
        }
        payload.update(health_identity_payload(_internal_identity))
        return JSONResponse(payload, status_code=200 if rollout_ready else 503)
    try:
        upstream = await state.inference.health()
    except Exception as exc:
        upstream = {"status": "error", "error": str(exc)}
    return {
        "status": "ok",
        "node_id": state.node.id,
        "gateway_url": state.node.public_url,
        "inference": upstream,
        "metrics": metrics.model_dump(mode="json"),
        "active_status_counts": state.session_registry.active_status_counts(),
        "active_sessions": state.session_registry.active_sessions(),
        "available_init": max(0, state.node.max_init_workers - metrics.init_inflight),
        "available_run": max(0, state.node.max_run_workers - metrics.run_inflight),
        "available_postrun": max(0, state.node.max_postrun_workers - metrics.postrun_inflight),
    }


@app.get("/admin/inference/status")
async def inference_generation_status():
    return get_state().inference.generation_status()


@app.post("/admin/inference/pause")
async def pause_inference_generation(timeout_seconds: float = 300.0):
    state = get_state()
    try:
        status = await state.inference.pause_generation(timeout_seconds=timeout_seconds)
    except TimeoutError as exc:
        raise HTTPException(
            status_code=504,
            detail=f"Timed out waiting for inference requests to drain after {timeout_seconds}s",
        ) from exc
    logger.info(
        "Paused inference generation proxy for weight update; inflight=%s",
        status["inflight"],
    )
    return status


@app.post("/admin/inference/resume")
async def resume_inference_generation():
    status = await get_state().inference.resume_generation()
    logger.info("Resumed inference generation proxy")
    return status


@app.get("/sessions")
async def list_sessions(
    status: str | None = Query(default=None),
    task_id: str | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=1000),
) -> dict[str, Any]:
    """List sessions on this gateway (active + recently terminal)."""
    state = get_state()
    active = state.session_registry.active_sessions()
    rows: list[dict[str, Any]] = []
    for entry in active:
        if status and entry.get("status") != status:
            continue
        if task_id and entry.get("task_id") != task_id:
            continue
        metadata = state.storage.get_session_metadata(entry["session_id"]) or {}
        rows.append({
            **entry,
            "completion_count": int(metadata.get("completion_count") or 0),
            "model_requested": metadata.get("model_requested"),
            "model_used": metadata.get("model_used"),
            "api_type": metadata.get("api_type"),
            "created_at": metadata.get("created_at"),
            "node_id": state.node.id,
        })
    return {"sessions": rows[:limit], "node_id": state.node.id}


@app.get("/sessions/{session_id}/completions")
async def list_session_completions(session_id: str) -> dict[str, Any]:
    """In-memory completions for an active or recently-completed session."""
    state = get_state()
    try:
        safe = clean_session_id(session_id)
    except InvalidSessionIdError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if safe is None:
        raise HTTPException(status_code=400, detail="Session id required")
    completions = state.storage.get_completions(safe)
    return {
        "session_id": safe,
        "completions": completions,
        "node_id": state.node.id,
    }


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


@app.post("/sessions", response_model=SessionCreateResponse | SessionDispatchResponse)
async def create_session(request: Request):
    try:
        body = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON body") from exc
    if "agent" in body and "session_id" in body:
        dispatch_request = SessionDispatchRequest.model_validate(body)
        _require_credential_authority()
        await require_generation_bound_run_admission(
            identity=_internal_identity,
            verifier=_run_admission_verifier,
            operation=RunAdmissionOperation.GATEWAY_SESSION_DISPATCH,
            payload=dispatch_request.model_dump(mode="json"),
            task_id=dispatch_request.task_id,
            session_id=dispatch_request.session_id,
        )
        state = get_state()
        try:
            await state.node_manager.dispatch(dispatch_request)
        except GatewayReadinessError as exc:
            raise RunAdmissionError(
                "gateway_readiness_failed",
                "Gateway security prerequisites were not ready for session admission.",
                status_code=503,
                retryable=True,
            ) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return SessionDispatchResponse(
            session_id=dispatch_request.session_id,
            task_id=dispatch_request.task_id,
            status=SessionStatus.REGISTERED,
            node_id=state.node.id,
        )

    create_request = SessionCreateRequest.model_validate(body)
    try:
        session_id = clean_session_id(create_request.session_id) or generate_session_id()
    except InvalidSessionIdError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    effective_request = create_request.model_copy(update={"session_id": session_id})
    _require_credential_authority()
    await require_generation_bound_run_admission(
        identity=_internal_identity,
        verifier=_run_admission_verifier,
        operation=RunAdmissionOperation.GATEWAY_SESSION_CREATE,
        payload=effective_request.model_dump(mode="json"),
        task_id=create_request.task_id,
        session_id=session_id,
    )
    state = get_state()

    info = state.session_registry.register(
        session_id,
        task_id=create_request.task_id,
        registered=True,
        status=SessionStatus.REGISTERED,
    )
    metadata = state.storage.ensure_session(
        info.session_id,
        model_requested=None,
        model_used=None,
        api_type=None,
        task_id=info.task_id,
        created_at=info.created_at.isoformat(),
    )
    return SessionCreateResponse(
        session_id=info.session_id,
        task_id=info.task_id,
        created_at=info.created_at,
        completion_count=int(metadata.get("completion_count", 0)),
        status=info.status,
    )


@app.get("/sessions/{session_id}", response_model=SessionStatusResponse)
async def get_session(session_id: str):
    try:
        safe_session_id = clean_session_id(session_id)
    except InvalidSessionIdError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if safe_session_id is None:
        raise HTTPException(status_code=400, detail="Session ID cannot be empty")
    return _session_response(safe_session_id)


@app.delete("/sessions/{session_id}", response_model=SessionDeleteResponse)
async def delete_session(session_id: str):
    state = get_state()
    try:
        safe_session_id = clean_session_id(session_id)
    except InvalidSessionIdError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if safe_session_id is None:
        raise HTTPException(status_code=400, detail="Session ID cannot be empty")

    try:
        await state.node_manager.cancel_and_wait(safe_session_id)
    except CancelAuthorityPersistenceError as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "type": "cancel_authority_persistence_failed",
                "message": str(exc),
            },
        ) from exc
    info = state.session_registry.get(safe_session_id)
    deleted_count = state.storage.delete_session(safe_session_id)
    if info is None and deleted_count == 0:
        raise HTTPException(status_code=404, detail="Session not found")

    state.session_registry.remove(safe_session_id)
    return SessionDeleteResponse(
        session_id=safe_session_id,
        deleted=True,
        messages_deleted=deleted_count,
    )


@app.api_route("/{path:path}", methods=["POST"])
async def proxy_request(request: Request, path: str):
    state = get_state()
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    headers = {k: v for k, v in request.headers.items()}
    full_path = request.url.path
    api_type = detect(full_path, headers, body)
    try:
        session_id = _resolve_session_id(
            headers,
            body,
            query_session_id=(
                request.query_params.get("session_id")
                or request.query_params.get("key")
            ),
        )
    except InvalidSessionIdError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    original_model = extract_model(api_type, body)
    transformer = state.transform_manager.get(api_type)
    session_info = state.session_registry.get(session_id)

    logger.info(
        "← %s %s | api=%s model=%s session=%s",
        request.method, full_path, api_type.value, original_model, session_id,
    )

    if api_type == APIType.GOOGLE and "streamGenerateContent" in full_path:
        body["_streaming"] = True

    transformed_body = body.copy()
    transformed_body["_openevo_model_served"] = state.node.model_served
    openai_request = transformer.transform_request(transformed_body)
    openai_request["model"] = state.node.model_served
    _apply_session_adapter_merge_spec(
        openai_request,
        session_info=session_info,
        engine=state.inference.engine,
    )
    is_streaming = openai_request.get("stream", False)

    if is_streaming:
        return await _handle_streaming(
            api_type,
            transformer,
            openai_request,
            body,
            session_id,
            original_model=original_model,
            session_info=session_info,
        )
    return await _handle_non_streaming(
        api_type,
        transformer,
        openai_request,
        body,
        session_id,
        original_model=original_model,
        session_info=session_info,
    )


def _adapter_merge_spec_from_session(session_info: Any | None) -> dict[str, Any] | None:
    metadata = getattr(session_info, "metadata", None)
    if not isinstance(metadata, dict):
        return None
    spec = metadata.get("adapter_merge_spec")
    if isinstance(spec, dict):
        return spec
    evolution = metadata.get("evolution")
    if isinstance(evolution, dict):
        spec = evolution.get("adapter_merge_spec")
        if isinstance(spec, dict):
            return spec
    return None


def _apply_session_adapter_merge_spec(
    openai_request: dict[str, Any],
    *,
    session_info: Any | None,
    engine: Any,
) -> dict[str, Any]:
    return engine.apply_adapter_merge_spec(
        openai_request,
        _adapter_merge_spec_from_session(session_info),
    )


async def _handle_non_streaming(
    api_type: APIType,
    transformer: BaseTransformer,
    openai_request: dict[str, Any],
    original_request: dict[str, Any],
    session_id: str,
    *,
    original_model: str,
    session_info: Any | None,
) -> JSONResponse:
    state = get_state()
    try:
        response = await state.inference.completion(openai_request)
    except UpstreamError as exc:
        logger.warning("Non-streaming upstream error for session %s: %s", session_id, exc)
        return _upstream_error_response(api_type, exc)

    state.storage.save_message(
        session_id,
        openai_request,
        response,
        original_request=original_request,
        model_requested=original_model,
        model_used=openai_request["model"],
        api_type=api_type.value,
        task_id=session_info.task_id if session_info else None,
        created_at=session_info.created_at.isoformat() if session_info else None,
        metadata=_completion_metadata(session_info),
    )
    transformed = transformer.transform_response(response, original_request)
    return JSONResponse(transformed)


async def _handle_streaming(
    api_type: APIType,
    transformer: BaseTransformer,
    openai_request: dict[str, Any],
    original_request: dict[str, Any],
    session_id: str,
    *,
    original_model: str,
    session_info: Any | None,
) -> StreamingResponse | JSONResponse:
    state = get_state()
    non_stream_request = {k: v for k, v in openai_request.items() if k != "stream_options"}
    non_stream_request["stream"] = False
    try:
        response = await state.inference.completion(non_stream_request)
    except UpstreamError as exc:
        logger.warning("Upstream error for streaming session %s: %s", session_id, exc)
        return _upstream_error_response(api_type, exc)

    state.storage.save_message(
        session_id,
        openai_request,
        response,
        original_request=original_request,
        model_requested=original_model,
        model_used=openai_request["model"],
        api_type=api_type.value,
        task_id=session_info.task_id if session_info else None,
        created_at=session_info.created_at.isoformat() if session_info else None,
        metadata=_completion_metadata(session_info),
    )

    synthetic_chunk = _response_to_stream_chunk(response)
    stream_state = transformer.create_stream_state(original_request)

    async def generate():
        try:
            if stream_state is not None:
                events = stream_state.process_chunk(synthetic_chunk, is_first=True)
                if events:
                    yield _format_stream_events(api_type, events)
                final_events = stream_state.finalize()
                if final_events:
                    yield _format_stream_events(api_type, final_events)
            else:
                output = format_stream_output(
                    api_type, transformer, synthetic_chunk, original_request, True,
                )
                if output:
                    yield output
            if api_type == APIType.OPENAI_CHAT:
                yield "data: [DONE]\n\n"
        except Exception as exc:
            logger.error("Synthetic stream error: %s", exc)
            yield _stream_error_output(api_type, exc)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


def _response_to_stream_chunk(response: dict[str, Any]) -> dict[str, Any]:
    """Convert a non-streaming chat completion into a single 'delta' chunk
    suitable for a transformer's stream_state.process_chunk / transform_stream_chunk."""
    choices = response.get("choices") or [{}]
    choice = choices[0]
    message = choice.get("message", {}) or {}

    tool_calls_delta: list[dict[str, Any]] = []
    for i, tc in enumerate(message.get("tool_calls") or []):
        func = tc.get("function", {}) or {}
        tool_calls_delta.append({
            "index": i,
            "id": tc.get("id"),
            "type": tc.get("type", "function"),
            "function": {
                "name": func.get("name", ""),
                "arguments": func.get("arguments", ""),
            },
        })

    delta: dict[str, Any] = {"role": "assistant"}
    if message.get("content") is not None:
        delta["content"] = message.get("content")
    if message.get("reasoning_content") is not None:
        delta["reasoning_content"] = message.get("reasoning_content")
    if tool_calls_delta:
        delta["tool_calls"] = tool_calls_delta

    return {
        "id": response.get("id"),
        "object": "chat.completion.chunk",
        "created": response.get("created"),
        "model": response.get("model"),
        "choices": [{
            "index": 0,
            "delta": delta,
            "finish_reason": choice.get("finish_reason"),
        }],
        "usage": response.get("usage"),
    }


def serve(
    topology_path: str = "topology.yaml",
    *,
    node_id: str | None = None,
    log_level: str = "info",
    managed_execution_mode: str | None = None,
) -> None:
    import uvicorn

    internal_identity = read_internal_service_identity(
        required=False,
        expected_service_id="gateway",
    )
    if internal_identity is not None and managed_execution_mode not in _MANAGED_EXECUTION_MODES:
        raise RuntimeError("release-owned Gateway is missing its managed execution mode")
    credential_authority = PreparedCodexCredentialSnapshot.from_inherited_environment(
        required=(
            internal_identity is not None
            and managed_execution_mode == _CODEX_SUBSCRIPTION_EXECUTION_MODE
        ),
    )
    if (
        internal_identity is not None
        and managed_execution_mode == _SELF_DEPLOYED_EXECUTION_MODE
        and credential_authority is not None
    ):
        credential_authority.close()
        raise RuntimeError("Self-Deployed Gateway received subscription credentials")
    configure_server(
        topology_path,
        node_id=node_id,
        internal_identity=internal_identity,
        run_admission_verifier=configured_run_admission_verifier(internal_identity),
        credential_authority=credential_authority,
        managed_execution_mode=managed_execution_mode,
    )
    state = get_state()
    listen_fd = inherited_listen_fd()
    if internal_identity is not None and listen_fd is None:
        raise RuntimeError("release-owned gateway requires an inherited listener")
    kwargs = {"app": app, "log_level": log_level}
    if listen_fd is None:
        kwargs.update(host=state.node.host, port=state.node.port)
    else:
        kwargs["fd"] = listen_fd
    try:
        uvicorn.run(**kwargs)
    finally:
        if credential_authority is not None:
            credential_authority.close()


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m openevo.gateway.server")
    parser.add_argument("--config", default=os.environ.get("OPENEVO_TOPOLOGY", "topology.yaml"))
    parser.add_argument("--node-id", default=os.environ.get("OPENEVO_GATEWAY_NODE_ID"))
    parser.add_argument("--log-level", default="info")
    parser.add_argument(
        "--managed-execution-mode",
        choices=sorted(_MANAGED_EXECUTION_MODES),
    )
    args = parser.parse_args()
    serve(
        args.config,
        node_id=args.node_id,
        log_level=args.log_level,
        managed_execution_mode=args.managed_execution_mode,
    )


if __name__ == "__main__":
    main()
