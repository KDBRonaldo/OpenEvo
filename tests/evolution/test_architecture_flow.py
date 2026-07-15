from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import httpx

from openevo.evolution.models import (
    ContextResolveRequest,
    DatasetCreateRequest,
    EventIngestRequest,
    JobCreateRequest,
    WorkerClaimRequest,
    WorkerCompleteRequest,
    WorkerFailRequest,
    WorkerHeartbeatRequest,
)
from openevo.evolution.store import EvolutionStore
from openevo.evolution.worker import run_once
from openevo.gateway.node import (
    build_evolution_session_event,
    write_evolution_context_files,
)
from openevo.rollout.models import SessionResult, SessionStatus
from openevo.trajectory.builder.agent_transcript import AgentTranscriptBuilder
from openevo.trajectory.models import CompletionSession


class StoreWorkerClient:
    """Adapt the in-process store to the worker's transport-neutral client protocol."""

    def __init__(self, store: EvolutionStore) -> None:
        self.store = store
        self.completed: dict[str, Any] | None = None

    def claim(
        self,
        worker_id: str,
        capabilities: list[str],
        *,
        lease_seconds: int | None = None,
        method_capabilities: list[str] | None = None,
        method_identity_capabilities: dict[str, str] | None = None,
    ) -> dict[str, Any] | None:
        request: dict[str, Any] = {
            "worker_id": worker_id,
            "capabilities": capabilities,
        }
        if lease_seconds is not None:
            request["lease_seconds"] = lease_seconds
        if method_capabilities is not None:
            request["method_capabilities"] = method_capabilities
        if method_identity_capabilities is not None:
            request["method_identity_capabilities"] = method_identity_capabilities
        claimed = self.store.claim_job(WorkerClaimRequest.model_validate(request)).job
        return None if claimed is None else claimed.model_dump(mode="json")

    def heartbeat(
        self,
        job_id: str,
        lease_id: str,
        *,
        progress: float | None = None,
        message: str | None = None,
    ) -> dict[str, Any]:
        return dict(
            self.store.heartbeat_job(
                job_id,
                WorkerHeartbeatRequest(
                    lease_id=lease_id,
                    progress=progress,
                    message=message,
                ),
            )
        )

    def complete(
        self,
        job_id: str,
        lease_id: str,
        artifacts: list[dict[str, Any]],
        *,
        report: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.completed = dict(
            self.store.complete_job(
                job_id,
                WorkerCompleteRequest(
                    lease_id=lease_id,
                    artifacts=artifacts,
                    report=report or {},
                ),
            )
        )
        return self.completed

    def fail(
        self,
        job_id: str,
        lease_id: str,
        error: str,
        *,
        retryable: bool = True,
    ) -> dict[str, Any]:
        return dict(
            self.store.fail_job(
                job_id,
                WorkerFailRequest(
                    lease_id=lease_id,
                    error=error,
                    retryable=retryable,
                ),
            )
        )


class RecordingUploadRuntime:
    def __init__(self) -> None:
        self.uploads: dict[str, str] = {}

    async def upload_file(self, source: str, target: str) -> None:
        self.uploads[target] = Path(source).read_text(encoding="utf-8")

    async def upload_dir(self, source: str, target: str) -> None:
        self.uploads[target] = str(source)


def _file_uri_path(uri: str) -> Path:
    parsed = urlparse(uri)
    assert parsed.scheme == "file"
    assert parsed.netloc in {"", "localhost"}
    return Path(unquote(parsed.path))


def test_transcript_to_resolved_text_memory_architecture_flow(
    tmp_path: Path,
    monkeypatch,
) -> None:
    transcript_text = "Validate parser precedence before returning the fix."
    log_dir = tmp_path / "runtime" / "logs" / "agent"
    log_dir.mkdir(parents=True)
    (log_dir / "step.00.stdout.log").write_text(
        json.dumps(
            {
                "type": "item.completed",
                "item": {
                    "type": "agent_message",
                    "text": transcript_text,
                },
            }
        ),
        encoding="utf-8",
    )
    completion_session = CompletionSession(
        session_id="session_architecture_flow",
        task_id="task_parser",
        metadata={
            "agent_instruction": "Fix the parser.",
            "agent_result": {"metadata": {"log_dir": str(log_dir), "last_step": 0}},
            "policy_version": "policy_architecture_flow",
            "rollout_step": 1,
        },
    )

    trajectory = asyncio.run(AgentTranscriptBuilder().build(completion_session))

    assert trajectory.status == "COMPLETED"
    assert trajectory.metadata["capture_mode"] == "transcript"
    assert trajectory.metadata["token_level_metrics_available"] is False
    assert len(trajectory.traces) == 1
    trace = trajectory.traces[0]
    assert trace.prompt_ids == []
    assert trace.response_ids == []
    assert trace.loss_mask == []
    assert trace.response_logprobs is None
    assert trace.metadata["token_level_metrics_available"] is False

    session_result = SessionResult(
        session_id=completion_session.session_id,
        task_id=completion_session.task_id or "task_parser",
        status=SessionStatus.COMPLETED,
        trajectory=trajectory,
        node_id="node-architecture-flow",
        metadata={
            "agent": {"harness": "codex", "model_name": "gpt-5"},
            "policy_version": "policy_architecture_flow",
            "rollout_step": 1,
        },
    )
    event_payload = build_evolution_session_event(session_result)

    assert event_payload["source"] == "openevo"
    assert event_payload["event_type"] == "openevo.session_completed"
    assert event_payload["source_event_id"] == "session:session_architecture_flow"
    assert event_payload["session_id"] == "session_architecture_flow"

    store = EvolutionStore(
        db_path=tmp_path / "evolution.db",
        artifact_root=tmp_path / "store",
    )
    store.initialize()
    ingested = store.ingest_event(EventIngestRequest.model_validate(event_payload))
    dataset = store.create_dataset(
        DatasetCreateRequest(
            name="architecture-flow-transcripts",
            purpose="text_memory_mining",
            query={
                "event_types": ["openevo.session_completed"],
                "status": ["COMPLETED"],
                "policy_version": "policy_architecture_flow",
            },
        )
    )

    assert ingested.ingested is True
    assert dataset.event_count == 1
    assert dataset.trace_count == 1
    dataset_artifact = store.get_artifact(dataset.artifact_id)
    assert dataset_artifact.manifest["event_ids"] == [ingested.event_id]
    assert dataset_artifact.manifest["trace_count"] == 1

    records_path = _file_uri_path(dataset_artifact.uri).with_name("records.jsonl")
    record = json.loads(records_path.read_text(encoding="utf-8").strip())
    dataset_trace = record["traces"][0]
    assert dataset_trace["prompt_messages"] == [{"role": "user", "content": "Fix the parser."}]
    assert dataset_trace["response_messages"] == [
        {
            "role": "assistant",
            "content": transcript_text,
        }
    ]
    assert dataset_trace["response_ids"] == []
    assert dataset_trace["loss_mask"] == []
    assert dataset_trace["response_logprobs"] is None
    assert dataset_trace["metadata"]["token_level_metrics_available"] is False

    reflected_memory = (
        "# Memory\n\n"
        "## Do\n"
        f"- task=task_parser session=session_architecture_flow: {transcript_text}\n\n"
        "## Avoid\n- Avoid unverified parser edits.\n\n"
        "## Validate\n- Run parser precedence tests.\n\n"
        "## When Applicable\n- Apply when parser behavior changes.\n\n"
        "## Retired Or Superseded\n- Retire stale parser advice.\n"
    )

    class FakeReflectorClient:
        def __init__(self, **kwargs: Any) -> None:
            assert kwargs["trust_env"] is False

        def __enter__(self) -> "FakeReflectorClient":
            return self

        def __exit__(self, *_args: Any) -> None:
            return None

        def post(
            self,
            url: str,
            *,
            headers: dict[str, str],
            json: dict[str, Any],
        ) -> httpx.Response:
            del headers
            assert transcript_text in json["messages"][1]["content"]
            return httpx.Response(
                200,
                request=httpx.Request("POST", url),
                json={"choices": [{"message": {"content": reflected_memory}}]},
            )

    monkeypatch.setattr(
        "openevo.evolution.methods.httpx.Client",
        FakeReflectorClient,
    )

    job = store.create_job(
        JobCreateRequest(
            method="text_memory_expel_reflector",
            job_type="text_memory_mining",
            input_artifact_ids=[dataset.artifact_id],
            config={
                "name": "architecture-flow-memory",
                "promoted": True,
                "tags": ["architecture-flow"],
                "reflector_llm": {
                    "model": "reflector-model",
                    "base_url": "http://reflector.test/v1",
                    "api_key": "test-key",
                },
            },
        )
    )
    worker_client = StoreWorkerClient(store)
    ran = run_once(
        worker_client,
        worker_id="worker-architecture-flow",
        capabilities=["text_memory_mining"],
        artifact_root=tmp_path / "store" / "worker-artifacts",
    )

    assert ran is True
    assert worker_client.completed is not None
    assert worker_client.completed["job_id"] == job.job_id
    assert worker_client.completed["state"] == "succeeded"
    [memory_artifact_id] = worker_client.completed["artifact_ids"]

    memory_artifact = store.get_artifact(memory_artifact_id)
    assert memory_artifact.manifest["source_dataset_artifact_id"] == dataset.artifact_id
    assert memory_artifact.manifest["method"] == "text_memory_expel_reflector"
    assert memory_artifact.compatibility == {}
    assert memory_artifact.promoted is True
    memory_text = _file_uri_path(memory_artifact.uri).read_text(encoding="utf-8")
    assert "task_parser" in memory_text
    assert "session_architecture_flow" in memory_text
    assert transcript_text in memory_text

    context = store.resolve_context(
        ContextResolveRequest(
            task_id="task_parser_followup",
            instruction="Fix another parser edge case.",
            agent={"harness": "codex"},
            metadata={"task_tags": ["parser"]},
        )
    )

    assert context.memory["artifact_ids"] == [memory_artifact_id]
    assert memory_artifact_id in context.selection["artifact_ids"]
    assert context.memory["rendered_text"] == memory_text

    runtime = RecordingUploadRuntime()
    env = asyncio.run(
        write_evolution_context_files(
            runtime=runtime,
            context=context.model_dump(mode="json"),
            host_dir=tmp_path,
            target_dir="/openevo/session/evolution",
        )
    )
    assert runtime.uploads["/openevo/session/evolution/memory.md"] == memory_text
    assert env["OPENEVO_MEMORY_FILE"] == "/openevo/session/evolution/memory.md"
