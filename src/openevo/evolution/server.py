from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request

from openevo.evolution.models import (
    ArtifactPromotionUpdateRequest,
    ArtifactRegisterRequest,
    ArtifactResponse,
    ContextResolveRequest,
    ContextResolveResponse,
    DatasetCreateRequest,
    DatasetCreateResponse,
    EventIngestRequest,
    EventIngestResponse,
    FeedbackApplicationCreateRequest,
    FeedbackApplicationResponse,
    HumanFeedbackCreateRequest,
    HumanFeedbackResponse,
    HumanQueryDecisionCreateRequest,
    HumanQueryDecisionResponse,
    JobCreateRequest,
    JobCreateResponse,
    ReviewAdjudicationRequest,
    ReviewClaimRequest,
    ReviewPacketResponse,
    ReviewRequestCreateRequest,
    ReviewRequestResponse,
    ReviewStatus,
    WorkerClaimRequest,
    WorkerClaimResponse,
    WorkerCompleteRequest,
    WorkerFailRequest,
    WorkerHeartbeatRequest,
)
from openevo.evolution.planned_jobs import PlanBoundJobCreateRequest
from openevo.evolution.store import EvolutionStore
from openevo.evolution.framework.builtins import (
    VerifiedExecutableRegistry,
    require_verified_executable_registry,
)
from openevo.evolution.framework.registry import RegistrySnapshot
from openevo.internal_auth import (
    INTERNAL_SERVICE_HEADER,
    InternalServiceIdentity,
    health_identity_payload,
    install_internal_auth,
)


def _review_write_error(exc: ValueError) -> HTTPException:
    status_code = 404 if str(exc).startswith("unknown review:") else 422
    return HTTPException(status_code=status_code, detail=str(exc))


def _review_create_error(exc: ValueError) -> HTTPException:
    status_code = 404 if str(exc).startswith("unknown query decision:") else 422
    return HTTPException(status_code=status_code, detail=str(exc))


def _feedback_application_create_error(exc: ValueError) -> HTTPException:
    status_code = 404 if str(exc).startswith("unknown feedback:") else 422
    return HTTPException(status_code=status_code, detail=str(exc))


def create_app(
    *,
    db_path: str | Path,
    artifact_root: str | Path,
    registry_snapshot: RegistrySnapshot | None = None,
    executable_registry: VerifiedExecutableRegistry | None = None,
    internal_identity: InternalServiceIdentity | None = None,
) -> FastAPI:
    verified_registry = (
        None
        if executable_registry is None
        else require_verified_executable_registry(executable_registry)
    )
    effective_snapshot = (
        verified_registry.snapshot if verified_registry is not None else registry_snapshot
    )
    root = Path(artifact_root)
    root.mkdir(parents=True, exist_ok=True)
    store = EvolutionStore(
        db_path=db_path,
        artifact_root=root,
        registry_snapshot=registry_snapshot,
        executable_registry=verified_registry,
    )
    store.initialize()
    app = FastAPI(title="OpenEvo Evolution Backend", version="0.1.0")
    app.state.db_path = Path(db_path)
    app.state.artifact_root = root
    app.state.store = store
    app.state.registry_snapshot = effective_snapshot
    app.state.evolution_registry = verified_registry
    app.state.internal_identity = internal_identity
    app.state.internal_workers = {}
    install_internal_auth(app, lambda: app.state.internal_identity)

    @app.get("/v1/health")
    async def health() -> dict[str, Any]:
        payload: dict[str, Any] = {
            "status": "ok",
            "db": "ok",
        }
        if internal_identity is None:
            payload["artifact_root"] = str(root)
        else:
            payload["registry_digest"] = (
                effective_snapshot.registry_digest if effective_snapshot is not None else None
            )
            payload["workers"] = list(app.state.internal_workers.values())
            payload.update(health_identity_payload(internal_identity))
        return payload

    @app.post("/v1/internal/workers/register")
    def register_internal_worker(
        payload: dict[str, Any],
        request: Request,
    ) -> dict[str, str]:
        if internal_identity is None:
            raise HTTPException(status_code=404, detail="internal worker registration is disabled")
        if request.headers.get(INTERNAL_SERVICE_HEADER) != "evolution-worker":
            raise HTTPException(status_code=403, detail="worker caller identity mismatch")
        if set(payload) != {
            "framework_lock_digest",
            "generation_digest",
            "registry_digest",
            "worker_id",
        }:
            raise HTTPException(
                status_code=422, detail="worker registration is not a closed object"
            )
        worker_id = payload.get("worker_id")
        generation_digest = payload.get("generation_digest")
        registry_digest = payload.get("registry_digest")
        framework_lock_digest = payload.get("framework_lock_digest")
        if (
            worker_id != "core-reference-worker"
            or generation_digest != internal_identity.generation_digest
            or registry_digest != internal_identity.registry_digest
            or framework_lock_digest != internal_identity.framework_lock_digest
        ):
            raise HTTPException(status_code=409, detail="worker registration identity mismatch")
        registration = {
            "framework_lock_digest": framework_lock_digest,
            "generation_digest": generation_digest,
            "registry_digest": registry_digest,
            "worker_id": worker_id,
        }
        app.state.internal_workers[worker_id] = registration
        return registration

    @app.post("/v1/events", response_model=EventIngestResponse)
    def ingest_event(request: EventIngestRequest) -> EventIngestResponse:
        return store.ingest_event(request)

    @app.post("/v1/artifacts", response_model=ArtifactResponse)
    def register_artifact(request: ArtifactRegisterRequest) -> ArtifactResponse:
        try:
            return store.register_artifact(request)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/v1/artifacts/{artifact_id}", response_model=ArtifactResponse)
    def get_artifact(artifact_id: str) -> ArtifactResponse:
        try:
            return store.get_artifact(artifact_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.patch("/v1/artifacts/{artifact_id}/promotion", response_model=ArtifactResponse)
    def update_artifact_promotion(
        artifact_id: str,
        request: ArtifactPromotionUpdateRequest,
    ) -> ArtifactResponse:
        try:
            return store.update_artifact_promotion_from_request(artifact_id, request)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/v1/contexts/resolve", response_model=ContextResolveResponse)
    def resolve_context(request: ContextResolveRequest) -> ContextResolveResponse:
        try:
            return store.resolve_context(request)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get(
        "/v1/internal/contexts/{context_id}/runtime-authority",
        response_model=ContextResolveResponse,
    )
    def get_context_runtime_authority(
        context_id: str,
        request: Request,
    ) -> ContextResolveResponse:
        if internal_identity is None:
            raise HTTPException(status_code=404, detail="context runtime authority is disabled")
        if request.headers.get(INTERNAL_SERVICE_HEADER) != "core-control":
            raise HTTPException(status_code=403, detail="context authority caller mismatch")
        try:
            return store.get_context_runtime_authority(context_id)
        except ValueError as exc:
            raise HTTPException(
                status_code=404, detail="context runtime authority unavailable"
            ) from exc

    @app.post("/v1/datasets", response_model=DatasetCreateResponse)
    def create_dataset(request: DatasetCreateRequest) -> DatasetCreateResponse:
        try:
            return store.create_dataset(request)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/v1/reviews", response_model=ReviewRequestResponse)
    def create_review(request: ReviewRequestCreateRequest) -> ReviewRequestResponse:
        try:
            return store.create_review_request(request)
        except ValueError as exc:
            raise _review_create_error(exc) from exc

    @app.get("/v1/review-packets", response_model=list[ReviewPacketResponse])
    def list_review_packets() -> list[ReviewPacketResponse]:
        return store.list_review_packets()

    @app.get("/v1/review-packets/{packet_id}", response_model=ReviewPacketResponse)
    def get_review_packet(packet_id: str) -> ReviewPacketResponse:
        try:
            return store.get_review_packet(packet_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/v1/reviews", response_model=list[ReviewRequestResponse])
    def list_reviews(
        status: ReviewStatus | None = None,
        task_id: str | None = None,
        assigned_to: str | None = None,
    ) -> list[ReviewRequestResponse]:
        return store.list_review_requests(
            status=status.value if status is not None else None,
            task_id=task_id,
            assigned_to=assigned_to,
        )

    @app.get("/v1/reviews/{review_id}", response_model=ReviewRequestResponse)
    def get_review(review_id: str) -> ReviewRequestResponse:
        try:
            return store.get_review_request(review_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/v1/reviews/{review_id}/claim", response_model=ReviewRequestResponse)
    def claim_review(
        review_id: str,
        request: ReviewClaimRequest,
    ) -> ReviewRequestResponse:
        try:
            return store.claim_review_request(review_id, request)
        except ValueError as exc:
            raise _review_write_error(exc) from exc

    @app.post("/v1/reviews/{review_id}/feedback", response_model=HumanFeedbackResponse)
    def submit_review_feedback(
        review_id: str,
        request: HumanFeedbackCreateRequest,
    ) -> HumanFeedbackResponse:
        try:
            return store.submit_human_feedback(review_id, request)
        except ValueError as exc:
            raise _review_write_error(exc) from exc

    @app.get("/v1/reviews/{review_id}/feedback", response_model=list[HumanFeedbackResponse])
    def list_review_feedback(review_id: str) -> list[HumanFeedbackResponse]:
        try:
            return store.list_human_feedback(review_id=review_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/v1/reviews/{review_id}/adjudicate", response_model=ReviewRequestResponse)
    def adjudicate_review(
        review_id: str,
        request: ReviewAdjudicationRequest,
    ) -> ReviewRequestResponse:
        try:
            return store.adjudicate_review_request(review_id, request)
        except ValueError as exc:
            raise _review_write_error(exc) from exc

    @app.post("/v1/reviews/{review_id}/resolve", response_model=ReviewRequestResponse)
    def resolve_review(review_id: str) -> ReviewRequestResponse:
        try:
            return store.resolve_review_request(review_id)
        except ValueError as exc:
            raise _review_write_error(exc) from exc

    @app.post("/v1/reviews/{review_id}/mark-stale", response_model=ReviewRequestResponse)
    def mark_review_stale(review_id: str) -> ReviewRequestResponse:
        try:
            return store.mark_review_stale(review_id)
        except ValueError as exc:
            raise _review_write_error(exc) from exc

    @app.post("/v1/query-decisions", response_model=HumanQueryDecisionResponse)
    def create_query_decision(
        request: HumanQueryDecisionCreateRequest,
    ) -> HumanQueryDecisionResponse:
        try:
            return store.create_human_query_decision(request)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get(
        "/v1/query-decisions/{query_decision_id}",
        response_model=HumanQueryDecisionResponse,
    )
    def get_query_decision(query_decision_id: str) -> HumanQueryDecisionResponse:
        try:
            return store.get_human_query_decision(query_decision_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/v1/feedback-applications", response_model=FeedbackApplicationResponse)
    def create_feedback_application(
        request: FeedbackApplicationCreateRequest,
    ) -> FeedbackApplicationResponse:
        try:
            return store.create_feedback_application(request)
        except ValueError as exc:
            raise _feedback_application_create_error(exc) from exc

    @app.get("/v1/feedback-applications", response_model=list[FeedbackApplicationResponse])
    def list_feedback_applications(
        feedback_id: str | None = None,
    ) -> list[FeedbackApplicationResponse]:
        try:
            return store.list_feedback_applications(feedback_id=feedback_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/v1/jobs", response_model=JobCreateResponse)
    def create_job(request: JobCreateRequest) -> JobCreateResponse:
        try:
            return store.create_job(request)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/v1/planned-jobs", response_model=JobCreateResponse)
    def create_plan_bound_job(request: PlanBoundJobCreateRequest) -> JobCreateResponse:
        if effective_snapshot is None:
            raise HTTPException(
                status_code=503,
                detail="plan-bound execution requires an active verified registry",
            )
        try:
            return store.create_plan_bound_job(request, snapshot=effective_snapshot)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/v1/jobs/claim", response_model=WorkerClaimResponse)
    def claim_job(request: WorkerClaimRequest) -> WorkerClaimResponse:
        return store.claim_job(request)

    @app.post("/v1/jobs/{job_id}/heartbeat")
    def heartbeat_job(job_id: str, request: WorkerHeartbeatRequest) -> dict[str, object]:
        try:
            return store.heartbeat_job(job_id, request)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/v1/jobs/{job_id}/complete")
    def complete_job(job_id: str, request: WorkerCompleteRequest) -> dict[str, object]:
        try:
            return store.complete_job(job_id, request)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/v1/jobs/{job_id}/fail")
    def fail_job(job_id: str, request: WorkerFailRequest) -> dict[str, object]:
        try:
            return store.fail_job(job_id, request)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/v1/internal/jobs/{job_id}")
    def get_internal_job_result(job_id: str) -> dict[str, object]:
        try:
            return store.get_internal_job_result(job_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail="job not found") from exc

    return app
