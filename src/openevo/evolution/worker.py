from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from threading import Event, Thread
from types import TracebackType
from typing import Any, Protocol

import httpx

from openevo.evolution.methods import UnknownEvolutionMethodError, run_method
from openevo.evolution.models import ArtifactRegisterRequest, WorkerClaimedJob
from openevo.evolution.framework.builtins import VerifiedExecutableRegistry
from openevo.evolution.framework.contracts import (
    DescriptorKind,
    MethodInvocationABI,
    canonical_digest,
)
from openevo.evolution.framework.execution import (
    HarnessInferenceRequest,
    HarnessInferenceResponse,
    MethodExecutionContext,
    MethodExecutionEnvelope,
    MethodExecutionServices,
    invoke_legacy_method,
)
from openevo.evolution.framework.plan import EvolutionPlan
from openevo.evolution.planned_jobs import validate_plan_against_snapshot


_DEFAULT_LEASE_SECONDS = 600
_MAX_HEARTBEAT_INTERVAL_SECONDS = 5.0


class _WorkerCancellationSignal:
    def __init__(self, event: Event, upstream: object | None) -> None:
        self._event = event
        self._upstream = upstream

    def is_set(self) -> bool:
        upstream_is_set = getattr(self._upstream, "is_set", None)
        return self._event.is_set() or bool(
            upstream_is_set is not None and upstream_is_set()
        )

    def wait(self, timeout: float | None = None) -> bool:
        if self.is_set():
            return True
        self._event.wait(timeout)
        return self.is_set()


class WorkerClient(Protocol):
    def claim(
        self,
        worker_id: str,
        capabilities: list[str],
        *,
        lease_seconds: int | None = None,
        method_capabilities: list[str] | None = None,
        method_identity_capabilities: dict[str, str] | None = None,
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
    executable_registry: VerifiedExecutableRegistry | None = None,
    method_services: MethodExecutionServices | None = None,
) -> bool:
    claimed = client.claim(
        worker_id,
        capabilities,
        lease_seconds=lease_seconds,
        method_capabilities=(
            list(executable_registry.method_handles)
            if executable_registry is not None
            else None
        ),
        method_identity_capabilities=(
            {
                method_id: executable_registry.snapshot.identity_digest_for(
                    DescriptorKind.METHOD,
                    method_id,
                )
                for method_id in executable_registry.method_handles
            }
            if executable_registry is not None
            else None
        ),
    )
    if claimed is None:
        return False

    try:
        job = WorkerClaimedJob.model_validate(claimed)
        client.heartbeat(job.job_id, job.lease_id, progress=0.0, message="claimed")
        effective_lease_seconds = (
            _DEFAULT_LEASE_SECONDS if lease_seconds is None else lease_seconds
        )
        heartbeat_interval = _heartbeat_interval_seconds(effective_lease_seconds)
        if not 0 < heartbeat_interval < effective_lease_seconds / 2:
            raise ValueError("heartbeat interval must be positive and earlier than half the lease")
        artifacts = _run_method_with_heartbeats(
            client,
            job,
            artifact_root=artifact_root,
            heartbeat_interval=heartbeat_interval,
            executable_registry=executable_registry,
            method_services=method_services,
        )
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
        try:
            client.fail(
                job_id,
                lease_id,
                str(exc),
                retryable=isinstance(exc, UnknownEvolutionMethodError),
            )
        except Exception as fail_exc:
            exc.add_note(f"job failure reporting also failed: {fail_exc}")
            raise exc from fail_exc
    return True


def _heartbeat_interval_seconds(lease_seconds: int) -> float:
    return min(lease_seconds / 3, _MAX_HEARTBEAT_INTERVAL_SECONDS)


def _run_method_with_heartbeats(
    client: WorkerClient,
    job: WorkerClaimedJob,
    *,
    artifact_root: Path,
    heartbeat_interval: float,
    executable_registry: VerifiedExecutableRegistry | None,
    method_services: MethodExecutionServices | None,
) -> list[ArtifactRegisterRequest]:
    stop = Event()
    cancellation = Event()
    heartbeat_errors: list[Exception] = []

    def heartbeat_until_stopped() -> None:
        while not stop.wait(heartbeat_interval):
            try:
                client.heartbeat(job.job_id, job.lease_id, message="running")
            except Exception as exc:
                heartbeat_errors.append(exc)
                cancellation.set()
                stop.set()
                return

    heartbeat_thread = Thread(
        target=heartbeat_until_stopped,
        name=f"openevo-heartbeat-{job.job_id}",
    )
    heartbeat_thread.start()
    method_error: Exception | None = None
    artifacts: list[ArtifactRegisterRequest] = []
    effective_services = (
        replace(
            method_services,
            cancellation=_WorkerCancellationSignal(
                cancellation,
                method_services.cancellation,
            ),
        )
        if method_services is not None
        else None
    )
    try:
        artifacts = _run_claimed_method(
            job,
            artifact_root=artifact_root,
            executable_registry=executable_registry,
            method_services=effective_services,
        )
    except Exception as exc:
        method_error = exc
    finally:
        stop.set()
        heartbeat_thread.join()

    if heartbeat_errors:
        raise heartbeat_errors[0]
    if method_error is not None:
        raise method_error
    return artifacts


class _UnavailableHarnessService:
    def infer(self, request: HarnessInferenceRequest) -> HarnessInferenceResponse:
        del request
        raise RuntimeError("Core harness inference service is unavailable")


def _run_claimed_method(
    job: WorkerClaimedJob,
    *,
    artifact_root: Path,
    executable_registry: VerifiedExecutableRegistry | None,
    method_services: MethodExecutionServices | None,
) -> list[ArtifactRegisterRequest]:
    if job.plan is None:
        return run_method(job, artifact_root=artifact_root)
    if executable_registry is None:
        raise ValueError("plan-bound job requires a verified executable registry")
    if (
        job.target_id is None
        or job.registry_snapshot_digest is None
        or job.method_identity_digest is None
        or job.execution_envelope is None
        or job.execution_envelope_digest is None
    ):
        raise ValueError("plan-bound job is missing execution identity fields")

    plan = EvolutionPlan.model_validate(job.plan)
    validate_plan_against_snapshot(plan, executable_registry.snapshot)
    if job.registry_snapshot_digest != plan.registry_snapshot_digest:
        raise ValueError("worker job registry snapshot does not match its plan")
    selections = tuple(
        selection
        for selection in plan.selections
        if selection.target_id == job.target_id
    )
    if len(selections) != 1:
        raise ValueError("worker job target is not selected by its plan")
    selection = selections[0]
    if selection.method_id != job.method:
        raise ValueError("worker job method does not match its plan selection")
    if selection.method_identity_digest != job.method_identity_digest:
        raise ValueError("worker job method identity does not match its plan")

    envelope = MethodExecutionEnvelope.model_validate(job.execution_envelope)
    if canonical_digest(envelope) != job.execution_envelope_digest:
        raise ValueError("worker execution envelope digest is invalid")
    if envelope.plan_id != plan.plan_id or envelope.target_id != job.target_id:
        raise ValueError("worker execution envelope does not match its plan target")
    if envelope.plan_digest != canonical_digest(plan):
        raise ValueError("worker execution envelope plan digest is invalid")
    if envelope.registry_snapshot_digest != plan.registry_snapshot_digest:
        raise ValueError("worker execution envelope registry identity is invalid")
    if envelope.method_id != selection.method_id:
        raise ValueError("worker execution envelope method does not match its plan")
    if envelope.method_identity_digest != selection.method_identity_digest:
        raise ValueError("worker execution envelope method identity is invalid")
    if envelope.user_config() != selection.config():
        raise ValueError("worker execution envelope config does not match its plan")
    if job.config != envelope.legacy_flat_config():
        raise ValueError("worker job config does not match its execution envelope")
    descriptor = executable_registry.snapshot.methods[job.method]
    if descriptor.output_artifact_types != envelope.output_artifact_types:
        raise ValueError("worker execution envelope output contract is invalid")
    try:
        method = executable_registry.method_handles[job.method]
    except KeyError as exc:
        raise ValueError(f"worker has no verified handle for method {job.method!r}") from exc
    services = method_services or MethodExecutionServices(
        harness=_UnavailableHarnessService()
    )
    context = MethodExecutionContext(
        job=job,
        artifact_root=artifact_root,
        envelope=envelope,
        services=services,
    )
    if descriptor.invocation_abi is MethodInvocationABI.LEGACY_WORKER_JOB_V1:
        artifacts = invoke_legacy_method(method, context)
    elif descriptor.invocation_abi is MethodInvocationABI.METHOD_CONTEXT_V1:
        if method_services is None:
            raise ValueError("context method requires Core execution services")
        artifacts = method(context)
    else:  # Enum validation makes this unreachable, but dispatch remains fail closed.
        raise ValueError(f"unsupported method invocation ABI: {descriptor.invocation_abi}")

    if not isinstance(artifacts, list):
        raise ValueError("evolution method must return a list of artifacts")
    validated: list[ArtifactRegisterRequest] = []
    for artifact in artifacts:
        validated_artifact = ArtifactRegisterRequest.model_validate(artifact)
        if str(validated_artifact.type) not in descriptor.output_artifact_types:
            raise ValueError(
                f"method {job.method!r} returned undeclared artifact type "
                f"{str(validated_artifact.type)!r}"
            )
        validated.append(validated_artifact)
    return validated


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
        headers: dict[str, str] | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(
            timeout=30.0,
            trust_env=False,
            transport=transport,
            headers=headers,
        )

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
        method_capabilities: list[str] | None = None,
        method_identity_capabilities: dict[str, str] | None = None,
    ) -> dict[str, Any] | None:
        payload: dict[str, Any] = {
            "worker_id": worker_id,
            "capabilities": capabilities,
        }
        if lease_seconds is not None:
            payload["lease_seconds"] = lease_seconds
        if method_capabilities is not None:
            payload["method_capabilities"] = method_capabilities
        if method_identity_capabilities is not None:
            payload["method_identity_capabilities"] = method_identity_capabilities
        response = self._client.post(
            f"{self.base_url}/v1/jobs/claim",
            json=payload,
        )
        response.raise_for_status()
        return response.json().get("job")

    def register_internal_worker(
        self,
        *,
        worker_id: str,
        framework_lock_digest: str,
        generation_digest: str,
        registry_digest: str,
    ) -> dict[str, str]:
        response = self._client.post(
            f"{self.base_url}/v1/internal/workers/register",
            json={
                "framework_lock_digest": framework_lock_digest,
                "generation_digest": generation_digest,
                "registry_digest": registry_digest,
                "worker_id": worker_id,
            },
        )
        response.raise_for_status()
        payload = response.json()
        if payload != {
            "framework_lock_digest": framework_lock_digest,
            "generation_digest": generation_digest,
            "registry_digest": registry_digest,
            "worker_id": worker_id,
        }:
            raise RuntimeError("evolution backend returned a mismatched worker registration")
        return payload

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
