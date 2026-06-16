from __future__ import annotations

from types import TracebackType
from pathlib import Path
from typing import Any, Protocol

import httpx

from polar_evolution.methods import UnknownEvolutionMethodError, run_method
from polar_evolution.models import ArtifactRegisterRequest, WorkerClaimedJob


class WorkerClient(Protocol):
    def claim(
        self,
        worker_id: str,
        capabilities: list[str],
        *,
        lease_seconds: int | None = None,
    ) -> dict[str, Any] | None: ...

    def heartbeat(
        self,
        job_id: str,
        lease_id: str,
        *,
        progress: float | None = None,
        message: str | None = None,
    ) -> dict[str, Any]: ...

    def complete(
        self,
        job_id: str,
        lease_id: str,
        artifacts: list[dict[str, Any]],
        *,
        report: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...

    def fail(
        self,
        job_id: str,
        lease_id: str,
        error: str,
        *,
        retryable: bool = True,
    ) -> dict[str, Any]: ...


def run_once(
    client: WorkerClient,
    *,
    worker_id: str,
    capabilities: list[str],
    artifact_root: Path,
    lease_seconds: int | None = None,
) -> bool:
    claimed = client.claim(worker_id, capabilities, lease_seconds=lease_seconds)
    if claimed is None:
        return False

    try:
        job = WorkerClaimedJob.model_validate(claimed)
        client.heartbeat(job.job_id, job.lease_id, progress=0.0, message="claimed")
        artifacts = run_method(job, artifact_root=artifact_root)
        client.heartbeat(job.job_id, job.lease_id, progress=1.0, message="completed")
        client.complete(
            job.job_id,
            job.lease_id,
            [_artifact_to_json(artifact) for artifact in artifacts],
            report={"method": job.method, "artifact_count": len(artifacts)},
        )
    except Exception as exc:
        job_id, lease_id = _claim_identity(claimed)
        if job_id is None or lease_id is None:
            raise
        client.fail(
            job_id,
            lease_id,
            str(exc),
            retryable=isinstance(exc, UnknownEvolutionMethodError),
        )
    return True


def _artifact_to_json(artifact: ArtifactRegisterRequest) -> dict[str, Any]:
    return artifact.model_dump(mode="json")


def _claim_identity(claimed: dict[str, Any]) -> tuple[str | None, str | None]:
    job_id = claimed.get("job_id")
    lease_id = claimed.get("lease_id")
    return (
        job_id if isinstance(job_id, str) and job_id else None,
        lease_id if isinstance(lease_id, str) and lease_id else None,
    )


class EvolutionWorkerClient:
    def __init__(
        self,
        base_url: str,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(timeout=30.0, trust_env=False, transport=transport)

    def __enter__(self) -> EvolutionWorkerClient:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def claim(
        self,
        worker_id: str,
        capabilities: list[str],
        *,
        lease_seconds: int | None = None,
    ) -> dict[str, Any] | None:
        payload: dict[str, Any] = {
            "worker_id": worker_id,
            "capabilities": capabilities,
        }
        if lease_seconds is not None:
            payload["lease_seconds"] = lease_seconds
        response = self._client.post(
            f"{self.base_url}/v1/jobs/claim",
            json=payload,
        )
        response.raise_for_status()
        return response.json().get("job")

    def heartbeat(
        self,
        job_id: str,
        lease_id: str,
        *,
        progress: float | None = None,
        message: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"lease_id": lease_id}
        if progress is not None:
            payload["progress"] = progress
        if message is not None:
            payload["message"] = message
        response = self._client.post(
            f"{self.base_url}/v1/jobs/{job_id}/heartbeat",
            json=payload,
        )
        response.raise_for_status()
        return response.json()

    def complete(
        self,
        job_id: str,
        lease_id: str,
        artifacts: list[dict[str, Any]],
        *,
        report: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "lease_id": lease_id,
            "artifacts": artifacts,
        }
        if report is not None:
            payload["report"] = report
        response = self._client.post(
            f"{self.base_url}/v1/jobs/{job_id}/complete",
            json=payload,
        )
        response.raise_for_status()
        return response.json()

    def fail(
        self,
        job_id: str,
        lease_id: str,
        error: str,
        *,
        retryable: bool = True,
    ) -> dict[str, Any]:
        response = self._client.post(
            f"{self.base_url}/v1/jobs/{job_id}/fail",
            json={"lease_id": lease_id, "error": error, "retryable": retryable},
        )
        response.raise_for_status()
        return response.json()

    def close(self) -> None:
        self._client.close()
