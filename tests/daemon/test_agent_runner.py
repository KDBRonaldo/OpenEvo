from __future__ import annotations

from pathlib import Path
import time
from typing import Any

import openevo.daemon.agent_runner as agent_runner_module
from openevo.backend.harness_adapter import (
    HarnessCancellation,
    HarnessRunCancelled,
    HarnessRunError,
)
from openevo.daemon.agent_runner import AgentSessionExecutor, CodexAgentRunner
from openevo.daemon.errors import AgentRunError
from openevo.daemon.workspace_store import ProjectWorkspaceStore


class _Store:
    def __init__(self, root: Path) -> None:
        self.workspaces = ProjectWorkspaceStore(root / "workspaces")
        self.contexts = [{"artifact_id": "memory-1"}]
        self.logs: list[str] = []
        self.completed: dict[str, Any] | None = None
        self.cancelled: list[dict[str, Any]] | None = None
        self.failed: tuple[str, list[dict[str, Any]] | None] | None = None

    def workspace_path(self, project_id: str) -> Path:
        return self.workspaces.project_path(project_id)

    def workspace_snapshot(self, project_id: str) -> dict[str, Any]:
        return self.workspaces.snapshot(project_id)

    def latest_context_artifacts(self, project_id: str) -> list[dict[str, Any]]:
        assert project_id == "project-1"
        return self.contexts

    def artifact(self, artifact_id: str) -> dict[str, Any]:
        return next(item for item in self.contexts if item["artifact_id"] == artifact_id)

    def append_session_log(self, session_id: str, message: str) -> None:
        assert session_id == "session-1"
        self.logs.append(message)

    def apply_workspace_mutations(self, project_id: str, mutations: object) -> None:
        self.workspaces.apply_mutations(project_id, mutations)

    def complete_session(self, session_id: str, result: dict[str, Any]) -> None:
        assert session_id == "session-1"
        self.completed = result

    def cancel_session(
        self,
        session_id: str,
        workspace_changes: list[dict[str, Any]],
    ) -> None:
        assert session_id == "session-1"
        self.cancelled = workspace_changes

    def fail_session(
        self,
        session_id: str,
        error: str,
        workspace_changes: list[dict[str, Any]] | None = None,
    ) -> None:
        assert session_id == "session-1"
        self.failed = (error, workspace_changes)


REQUEST = {
    "project_id": "project-1",
    "project_name": "Project",
    "task_title": "Task",
    "instruction": "Create a result",
}


def test_agent_session_executor_commits_workspace_and_result(tmp_path: Path) -> None:
    store = _Store(tmp_path)
    captured: dict[str, Any] = {}

    class Runner:
        def run(self, request: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
            captured.update(request)
            kwargs["log"]("Harness running")
            return {
                "response": "Done",
                "model": "test-model",
                "duration_ms": 5,
                "logs": [],
                "file_mutations": {
                    "file_writes": [{"path": "answer.txt", "content": "persistent answer\n"}],
                    "delete_paths": [],
                },
            }

    executor = AgentSessionExecutor(store=store, runner=Runner())
    executor.execute("session-1", REQUEST, HarnessCancellation())

    assert captured["evolved_contexts"] == store.contexts
    assert captured["workspace_path"] == store.workspace_path("project-1")
    assert store.logs == ["Harness running"]
    assert store.completed is not None
    assert store.completed["response"] == "Done"
    assert store.completed["workspace_changes"][0]["path"] == "answer.txt"
    assert (store.workspace_path("project-1") / "answer.txt").read_text(
        encoding="utf-8"
    ) == "persistent answer\n"


def test_agent_session_executor_reports_self_deployed_model_start_progress(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    store = _Store(tmp_path)
    monkeypatch.setattr(agent_runner_module, "_MODEL_START_PROGRESS_SECONDS", 0.01)

    class Runner:
        def run(self, request: dict[str, Any], **_: Any) -> dict[str, Any]:
            assert request["inference"]["model"] == "local-model"
            return {"response": "Done", "model": "local-model", "duration_ms": 5, "logs": []}

    def prepare(_: dict[str, Any]) -> dict[str, str]:
        time.sleep(0.04)
        return {"base_url": "http://127.0.0.1:1/v1", "model": "local-model"}

    AgentSessionExecutor(
        store=store,
        runner=Runner(),
        execution_preparer=prepare,
    ).execute(
        "session-1",
        {**REQUEST, "execution": {"mode": "self-deployed"}},
        HarnessCancellation(),
    )

    assert store.logs[0].startswith("Starting or waiting")
    assert "The self-deployed model is still loading on the GPU." in store.logs
    assert store.logs[-1] == "The self-deployed model is ready; starting the Codex harness."


def test_agent_session_executor_uses_project_head_pinned_context(tmp_path: Path) -> None:
    store = _Store(tmp_path)
    store.contexts = [
        {
            "artifact_id": "memory-old",
            "project_id": "project-1",
            "artifact_type": "text_memory",
        },
        {
            "artifact_id": "memory-current",
            "project_id": "project-1",
            "artifact_type": "text_memory",
        },
    ]
    captured: dict[str, Any] = {}

    class Runner:
        def run(self, request: dict[str, Any], **_: Any) -> dict[str, Any]:
            captured.update(request)
            return {
                "response": "Done",
                "model": "test-model",
                "duration_ms": 5,
                "logs": [],
            }

    AgentSessionExecutor(store=store, runner=Runner()).execute(
        "session-1",
        {**REQUEST, "context_artifact_ids": ["memory-old"]},
        HarnessCancellation(),
    )

    assert [item["artifact_id"] for item in captured["evolved_contexts"]] == [
        "memory-old"
    ]


def test_agent_session_executor_records_cancellation_and_agent_failure(
    tmp_path: Path,
) -> None:
    store = _Store(tmp_path)

    class CancelledRunner:
        def run(self, *_: Any, **__: Any) -> dict[str, Any]:
            raise HarnessRunCancelled("Session cancelled by user")

    AgentSessionExecutor(store=store, runner=CancelledRunner()).execute(
        "session-1", REQUEST, HarnessCancellation()
    )
    assert store.cancelled == []
    assert store.failed is None

    class FailedRunner:
        def run(self, *_: Any, **__: Any) -> dict[str, Any]:
            raise AgentRunError("harness failed safely")

    AgentSessionExecutor(store=store, runner=FailedRunner()).execute(
        "session-1", REQUEST, HarnessCancellation()
    )
    assert store.failed == ("harness failed safely", [])


def test_evidence_failure_does_not_reopen_completed_session(tmp_path: Path) -> None:
    store = _Store(tmp_path)

    class Runner:
        def run(self, *_: Any, **__: Any) -> dict[str, Any]:
            return {
                "response": "Done",
                "model": "test-model",
                "duration_ms": 5,
                "logs": [],
            }

    def fail_sealing(
        session_id: str,
        request: dict[str, str],
        result: dict[str, Any],
    ) -> None:
        raise RuntimeError("dataset unavailable")

    AgentSessionExecutor(
        store=store,
        runner=Runner(),
        evidence_sealer=fail_sealing,
    ).execute("session-1", REQUEST, HarnessCancellation())

    assert store.completed is not None
    assert store.failed is None
    assert store.logs == [
        "Session completed, but Evolution evidence sealing failed: dataset unavailable"
    ]


def test_codex_agent_runner_normalizes_harness_errors() -> None:
    class Adapter:
        codex_binary = "codex"
        model = "test-model"

        def runtime_capabilities(self) -> dict[str, Any]:
            return {"harness": "codex"}

        def check_ready(self) -> None:
            raise HarnessRunError("not logged in")

        def run(self, *_: Any, **__: Any) -> dict[str, Any]:
            raise HarnessRunError("invalid response")

    runner = CodexAgentRunner(
        codex_binary="codex",
        timeout_seconds=30,
        model="test-model",
        context_materializer_factory=object,
        runtime_control_adapter=object(),
        extract_event_logs=lambda _: [],
        max_capture_bytes=10,
        max_response_bytes=10,
        max_workspace_context_bytes=10,
        adapter_factory=lambda **_: Adapter(),
    )

    assert runner.runtime_capabilities() == {"harness": "codex"}
    try:
        runner.check_ready()
    except AgentRunError as exc:
        assert str(exc) == "not logged in"
    else:
        raise AssertionError("readiness error was not normalized")

    try:
        runner.run({})
    except AgentRunError as exc:
        assert str(exc) == "invalid response"
    else:
        raise AssertionError("run error was not normalized")
