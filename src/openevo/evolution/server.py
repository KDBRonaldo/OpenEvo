from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException

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
from openevo.evolution.store import EvolutionStore


def _review_write_error(exc: ValueError) -> HTTPException:
    status_code = 404 if str(exc).startswith("unknown review:") else 422
    return HTTPException(status_code=status_code, detail=str(exc))


def _review_create_error(exc: ValueError) -> HTTPException:
    status_code = 404 if str(exc).startswith("unknown query decision:") else 422
    return HTTPException(status_code=status_code, detail=str(exc))


def _feedback_application_create_error(exc: ValueError) -> HTTPException:
    status_code = 404 if str(exc).startswith("unknown feedback:") else 422
    return HTTPException(status_code=status_code, detail=str(exc))


def create_app(*, db_path: str | Path, artifact_root: str | Path) -> FastAPI:
    root = Path(artifact_root)
    root.mkdir(parents=True, exist_ok=True)
    store = EvolutionStore(db_path=db_path, artifact_root=root)
    store.initialize()
    app = FastAPI(title="Polar Evolution Backend", version="0.1.0")
    app.state.db_path = Path(db_path)
    app.state.artifact_root = root
    app.state.store = store

    @app.get("/v1/health")
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "db": "ok",
            "artifact_root": str(root),
        }

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

    return app
