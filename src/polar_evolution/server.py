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

    return app
