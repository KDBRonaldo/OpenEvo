from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException

from polar_evolution.models import (
    ArtifactRegisterRequest,
    ArtifactResponse,
    DatasetCreateRequest,
    DatasetCreateResponse,
    EventIngestRequest,
    EventIngestResponse,
    JobCreateRequest,
    JobCreateResponse,
    WorkerClaimRequest,
    WorkerClaimResponse,
    WorkerCompleteRequest,
    WorkerFailRequest,
    WorkerHeartbeatRequest,
)
from polar_evolution.store import EvolutionStore


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

    @app.post("/v1/datasets", response_model=DatasetCreateResponse)
    def create_dataset(request: DatasetCreateRequest) -> DatasetCreateResponse:
        try:
            return store.create_dataset(request)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

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
