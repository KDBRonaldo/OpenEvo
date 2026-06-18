from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import httpx
import pytest

from polar_evolution.cli import _parse_capabilities
from polar_evolution.methods import run_method
from polar_evolution.models import ArtifactType, ContextResolveRequest, WorkerClaimedJob
from polar_evolution.store import EvolutionStore
from polar_evolution.worker import run_once


def _dataset_artifact(tmp_path: Path) -> dict[str, Any]:
    dataset_dir = tmp_path / "dataset"
    dataset_dir.mkdir()
    records_path = dataset_dir / "records.jsonl"
    records_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "event_id": "evt_1",
                        "task_id": "task_alpha",
                        "session_id": "session_1",
                        "status": "COMPLETED",
                        "reward": 1.0,
                        "payload": {"summary": "solved calculator task"},
                    }
                ),
                json.dumps(
                    {
                        "event_id": "evt_2",
                        "task_id": "task_beta",
                        "session_id": "session_2",
                        "status": "ERROR",
                        "reward": 0.1,
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    manifest_path = dataset_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "dataset_id": "ds_test",
                "name": "test dataset",
                "records_path": "records.jsonl",
                "records_uri": records_path.as_uri(),
                "event_count": 2,
            }
        ),
        encoding="utf-8",
    )
    return {
        "artifact_id": "art_dataset",
        "type": "dataset",
        "uri": manifest_path.as_uri(),
        "name": "test dataset",
    }


def _reflector_dataset_artifact(tmp_path: Path) -> dict[str, Any]:
    dataset_dir = tmp_path / "reflector-dataset"
    dataset_dir.mkdir()
    records_path = dataset_dir / "records.jsonl"
    records_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "event_id": "evt_success",
                        "task_id": "task_parser_success",
                        "session_id": "session_success",
                        "status": "COMPLETED",
                        "reward": 1.0,
                        "traces": [
                            {
                                "prompt_messages": [
                                    {"role": "user", "content": "Fix the parser precedence bug."}
                                ],
                                "response_messages": [
                                    {
                                        "role": "assistant",
                                        "content": (
                                            "Added a regression test first, then fixed parser "
                                            "precedence."
                                        ),
                                    }
                                ],
                                "metadata": {
                                    "capture_mode": "transcript",
                                    "token_level_metrics_available": False,
                                },
                            }
                        ],
                    }
                ),
                json.dumps(
                    {
                        "event_id": "evt_failure",
                        "task_id": "task_packaging_failure",
                        "session_id": "session_failure",
                        "status": "ERROR",
                        "reward": 0.0,
                        "traces": [
                            {
                                "prompt_messages": [
                                    {"role": "user", "content": "Fix the package import bug."}
                                ],
                                "response_messages": [
                                    {
                                        "role": "assistant",
                                        "content": (
                                            "Changed files without running the focused test."
                                        ),
                                    }
                                ],
                                "metadata": {
                                    "capture_mode": "transcript",
                                    "token_level_metrics_available": False,
                                    "transcript": "pytest failed after an unverified edit",
                                },
                            }
                        ],
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    manifest_path = dataset_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "dataset_id": "ds_reflector",
                "name": "reflector dataset",
                "records_path": "records.jsonl",
                "records_uri": records_path.as_uri(),
                "event_count": 2,
                "trace_count": 2,
            }
        ),
        encoding="utf-8",
    )
    return {
        "artifact_id": "art_dataset_reflector",
        "type": "dataset",
        "uri": manifest_path.as_uri(),
        "name": "reflector dataset",
    }


def _golden_feedback_dataset_artifact(tmp_path: Path) -> dict[str, Any]:
    dataset_dir = tmp_path / "golden-feedback-dataset"
    dataset_dir.mkdir()
    records_path = dataset_dir / "records.jsonl"
    records_path.write_text(
        json.dumps(
            {
                "event_id": "evt_golden_feedback",
                "task_id": "task_biology",
                "session_id": "session_golden_feedback",
                "status": "COMPLETED",
                "reward": 1.0,
                "payload": {
                    "session_result": {
                        "metadata": {
                            "evolution_feedback": {
                                "golden_standard": (
                                    "## Shared Golden Standard Evaluation (Sanitized)\n\n"
                                    "- Aggregate fit: precision=0.250, recall=0.900, f1=0.391.\n"
                                    "- Primary gap: over-inclusion. Generalize the component-boundary "
                                    "method without copying held-out literals."
                                )
                            }
                        }
                    }
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    manifest_path = dataset_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "dataset_id": "ds_golden_feedback",
                "name": "golden feedback dataset",
                "records_path": "records.jsonl",
                "records_uri": records_path.as_uri(),
                "event_count": 1,
            }
        ),
        encoding="utf-8",
    )
    return {
        "artifact_id": "art_dataset_golden_feedback",
        "type": "dataset",
        "uri": manifest_path.as_uri(),
        "name": "golden feedback dataset",
    }


def _job(
    method: str,
    tmp_path: Path,
    *,
    config: dict[str, Any] | None = None,
    input_artifacts: list[dict[str, Any]] | None = None,
) -> WorkerClaimedJob:
    return WorkerClaimedJob(
        job_id=f"job_{method}",
        lease_id=f"lease_{method}",
        job_type="reference",
        method=method,
        input_artifacts=input_artifacts or [],
        config=config or {"promoted": True},
    )


def _patch_reflector_llm(
    monkeypatch: pytest.MonkeyPatch,
    content: str = "# Evolved Agent System\n\nUse reflected lessons.",
) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    class FakeClient:
        def __init__(self, **kwargs: Any) -> None:
            captured["client_kwargs"] = kwargs

        def __enter__(self) -> "FakeClient":
            return self

        def __exit__(self, *args: Any) -> None:
            return None

        def post(
            self,
            url: str,
            *,
            headers: dict[str, str],
            json: dict[str, Any],
        ) -> httpx.Response:
            captured["url"] = url
            captured["headers"] = headers
            captured["json"] = json
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": content}}]},
                request=httpx.Request("POST", url),
            )

    monkeypatch.setattr(httpx, "Client", FakeClient)
    return captured


def test_text_memory_method_writes_markdown_from_dataset(tmp_path):
    job = _job(
        "text_memory",
        tmp_path,
        input_artifacts=[_dataset_artifact(tmp_path)],
    )

    artifacts = run_method(job, artifact_root=tmp_path / "artifacts")

    assert len(artifacts) == 1
    artifact = artifacts[0]
    assert artifact.type == ArtifactType.TEXT_MEMORY
    assert artifact.promoted is True
    assert artifact.uri.endswith("/memory.md")
    memory_path = Path(artifact.uri.removeprefix("file://"))
    memory = memory_path.read_text(encoding="utf-8")
    assert "task_alpha" in memory
    assert "session_1" in memory
    assert "reward=1.0" in memory
    assert "solved calculator task" in memory
    assert artifact.manifest["source_dataset_artifact_id"] == "art_dataset"
    assert artifact.manifest["record_count"] == 2


def test_text_memory_method_rejects_non_dataset_input_artifact(tmp_path):
    report_path = tmp_path / "report.json"
    report_path.write_text(
        json.dumps({"records_path": "records.jsonl"}),
        encoding="utf-8",
    )
    job = _job(
        "text_memory",
        tmp_path,
        input_artifacts=[
            {
                "artifact_id": "art_report",
                "type": "report",
                "uri": report_path.as_uri(),
                "name": "report",
            }
        ],
    )

    try:
        run_method(job, artifact_root=tmp_path / "artifacts")
    except ValueError as exc:
        assert "requires an input dataset artifact" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_skill_bundle_method_writes_skill_markdown(tmp_path):
    job = _job(
        "skill_bundle",
        tmp_path,
        config={
            "name": "calculator-helper",
            "skill_markdown": "# Calculator Helper\n\nUse exact arithmetic.",
        },
    )

    artifacts = run_method(job, artifact_root=tmp_path / "artifacts")

    artifact = artifacts[0]
    assert artifact.type == ArtifactType.SKILL_BUNDLE
    assert artifact.name == "calculator-helper"
    bundle_path = Path(artifact.uri.removeprefix("file://"))
    assert bundle_path.is_dir()
    assert (bundle_path / "SKILL.md").read_text(encoding="utf-8") == (
        "# Calculator Helper\n\nUse exact arithmetic.\n"
    )
    assert artifact.manifest["entrypoint"] == "SKILL.md"


def test_agent_system_method_writes_harness_instruction_file(tmp_path):
    job = _job(
        "agent_system",
        tmp_path,
        config={
            "name": "codex-agent-system",
            "agent_system_markdown": "Prefer repository-local conventions.",
            "target_path": "AGENTS.md",
            "promoted": True,
        },
    )

    artifacts = run_method(job, artifact_root=tmp_path / "artifacts")

    artifact = artifacts[0]
    assert artifact.type == ArtifactType.AGENT_SYSTEM
    assert artifact.name == "codex-agent-system"
    instruction_path = Path(artifact.uri.removeprefix("file://"))
    assert instruction_path.name == "AGENTS.md"
    assert instruction_path.read_text(encoding="utf-8") == (
        "Prefer repository-local conventions.\n"
    )
    assert artifact.manifest["target_path"] == "AGENTS.md"
    assert artifact.manifest["content_path"] == "AGENTS.md"
    assert artifact.promoted is True


def test_agent_system_method_preserves_selection_metadata(tmp_path):
    job = _job(
        "agent_system",
        tmp_path,
        config={
            "agent_system_markdown": "Prefer Codex repository instructions.",
            "compatibility": {"agent_harness": ["codex"], "task_tags": ["calculator"]},
            "scores": {"quality": 0.7},
            "lineage": {"source": "unit-test"},
            "promoted": True,
        },
    )
    artifact_request = run_method(job, artifact_root=tmp_path / "artifacts")[0]

    assert artifact_request.compatibility == {
        "agent_harness": ["codex"],
        "task_tags": ["calculator"],
    }
    assert artifact_request.scores == {"quality": 0.7}
    assert artifact_request.lineage == {"source": "unit-test"}

    store = EvolutionStore(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "store")
    store.initialize()
    artifact = store.register_artifact(artifact_request)

    other_context = store.resolve_context(
        ContextResolveRequest(
            task_id="task_other",
            instruction="fix parser",
            agent={"harness": "other"},
            metadata={"task_tags": ["calculator"]},
        )
    )
    codex_context = store.resolve_context(
        ContextResolveRequest(
            task_id="task_codex",
            instruction="fix parser",
            agent={"harness": "codex"},
            metadata={"task_tags": ["calculator"]},
        )
    )

    assert artifact.artifact_id not in other_context.selection["artifact_ids"]
    assert artifact.artifact_id in codex_context.agent_system["artifact_ids"]


def test_agent_system_reflector_writes_agents_md_from_dataset_trajectories(
    tmp_path,
    monkeypatch,
):
    _patch_reflector_llm(
        monkeypatch,
        content=(
            "# Evolved Agent System\n\n"
            "## Reflections From Prior Trajectories\n\n"
            "- prompt: Fix the parser precedence bug.\n"
            "- observed: Added a regression test first.\n"
            "- prompt: Fix the package import bug.\n"
            "- failure_signal: pytest failed after an unverified edit\n"
        ),
    )
    job = _job(
        "agent_system_reflector",
        tmp_path,
        input_artifacts=[_reflector_dataset_artifact(tmp_path)],
        config={
            "name": "codex-reflector",
            "target_path": "agents.md",
            "reflector_llm": {
                "model": "reflector-model",
                "base_url": "http://reflector.test/v1",
                "api_key": "test-key",
            },
            "compatibility": {"agent_harness": ["codex"]},
            "scores": {"quality": 0.42},
            "tags": ["swe"],
            "promoted": True,
        },
    )

    artifacts = run_method(job, artifact_root=tmp_path / "artifacts")

    artifact = artifacts[0]
    assert artifact.type == ArtifactType.AGENT_SYSTEM
    assert artifact.name == "codex-reflector"
    assert artifact.manifest["target_path"] == "agents.md"
    assert artifact.manifest["content_path"] == "agents.md"
    assert artifact.manifest["source_dataset_artifact_id"] == "art_dataset_reflector"
    assert artifact.manifest["record_count"] == 2
    assert artifact.manifest["reflected_record_count"] == 2
    assert artifact.manifest["success_count"] == 1
    assert artifact.manifest["failure_count"] == 1
    assert artifact.compatibility == {"agent_harness": ["codex"]}
    assert artifact.scores == {"quality": 0.42}
    assert artifact.tags == ["swe"]
    assert artifact.promoted is True
    assert artifact.lineage["method"] == "agent_system_reflector"
    assert artifact.lineage["input_artifact_ids"] == ["art_dataset_reflector"]

    instruction_path = Path(artifact.uri.removeprefix("file://"))
    assert instruction_path.name == "agents.md"
    text = instruction_path.read_text(encoding="utf-8")
    assert "# Evolved Agent System" in text
    assert "## Reflections From Prior Trajectories" in text
    assert "Fix the parser precedence bug." in text
    assert "Added a regression test first" in text
    assert "Fix the package import bug." in text
    assert "pytest failed after an unverified edit" in text

    store = EvolutionStore(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "store")
    store.initialize()
    registered = store.register_artifact(artifact)
    context = store.resolve_context(
        ContextResolveRequest(
            task_id="task_reflected",
            instruction="fix import",
            agent={"harness": "codex"},
        )
    )
    assert registered.artifact_id in context.agent_system["artifact_ids"]
    assert context.agent_system["target_path"] == "agents.md"
    assert "pytest failed after an unverified edit" in context.agent_system["rendered_text"]


def test_agent_system_reflector_uses_configured_llm_response(tmp_path, monkeypatch):
    captured = _patch_reflector_llm(
        monkeypatch,
        content=(
            "# LLM Evolved Agent System\n\n"
            "Always run focused verification before broad cleanup."
        ),
    )
    job = _job(
        "agent_system_reflector",
        tmp_path,
        input_artifacts=[_reflector_dataset_artifact(tmp_path)],
        config={
            "reflector_llm": {
                "model": "reflector-model",
                "base_url": "http://reflector.test/v1",
                "api_key": "secret",
                "temperature": 0.1,
                "max_tokens": 1234,
                "timeout_seconds": 7,
            }
        },
    )

    artifact = run_method(job, artifact_root=tmp_path / "artifacts")[0]

    text = Path(artifact.uri.removeprefix("file://")).read_text(encoding="utf-8")
    assert text == (
        "# LLM Evolved Agent System\n\n"
        "Always run focused verification before broad cleanup.\n"
    )
    assert captured["url"] == "http://reflector.test/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer secret"
    assert captured["json"]["model"] == "reflector-model"
    assert captured["json"]["temperature"] == 0.1
    assert captured["json"]["max_tokens"] == 1234
    assert captured["client_kwargs"]["timeout"] == 7.0
    messages = captured["json"]["messages"]
    assert messages[0]["role"] == "system"
    assert messages[1]["role"] == "user"
    assert "Fix the parser precedence bug." in messages[1]["content"]
    assert "pytest failed after an unverified edit" in messages[1]["content"]


def test_agent_system_reflector_includes_shared_evolution_feedback(tmp_path, monkeypatch):
    captured = _patch_reflector_llm(monkeypatch)
    job = _job(
        "agent_system_reflector",
        tmp_path,
        input_artifacts=[_golden_feedback_dataset_artifact(tmp_path)],
        config={
            "reflector_llm": {
                "model": "reflector-model",
                "base_url": "http://reflector.test/v1",
                "api_key": "secret",
            }
        },
    )

    run_method(job, artifact_root=tmp_path / "artifacts")

    prompt = captured["json"]["messages"][1]["content"]
    assert "Shared Evolution Feedback" in prompt
    assert "Shared Golden Standard Evaluation (Sanitized)" in prompt
    assert "over-inclusion" in prompt
    assert "held-out literals" in prompt
    assert "AAAACCCCGGGGTTTTAAAACCCC" not in prompt


def test_agent_system_reflector_can_use_codex_cli_subscription_provider(tmp_path, monkeypatch):
    captured: dict[str, Any] = {}

    def fake_run(
        args: list[str],
        *,
        check: bool,
        capture_output: bool,
        env: dict[str, str],
        input: str,
        text: bool,
        timeout: float,
    ) -> subprocess.CompletedProcess[str]:
        captured["args"] = args
        captured["check"] = check
        captured["capture_output"] = capture_output
        captured["env"] = env
        captured["input"] = input
        captured["text"] = text
        captured["timeout"] = timeout
        output_path = Path(args[args.index("--output-last-message") + 1])
        output_path.write_text(
            "# Codex CLI Evolved Agent System\n\n"
            "Use transcript evidence before changing the agent system.\n",
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(args, 0, stdout="{}", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    job = _job(
        "agent_system_reflector",
        tmp_path,
        input_artifacts=[_reflector_dataset_artifact(tmp_path)],
        config={
            "reflector_llm": {
                "provider": "codex_cli",
                "model": "gpt-5.4",
                "codex_home": "/tmp/codex-home",
                "timeout_seconds": 9,
            }
        },
    )

    artifact = run_method(job, artifact_root=tmp_path / "artifacts")[0]

    text = Path(artifact.uri.removeprefix("file://")).read_text(encoding="utf-8")
    assert text == (
        "# Codex CLI Evolved Agent System\n\n"
        "Use transcript evidence before changing the agent system.\n"
    )
    assert captured["args"][:2] == ["codex", "exec"]
    assert "--json" in captured["args"]
    assert "--dangerously-bypass-approvals-and-sandbox" not in captured["args"]
    assert "--ignore-user-config" in captured["args"]
    assert "--ephemeral" in captured["args"]
    assert "--ask-for-approval" in captured["args"]
    assert captured["args"][captured["args"].index("--ask-for-approval") + 1] == "never"
    assert "--sandbox" in captured["args"]
    assert captured["args"][captured["args"].index("--sandbox") + 1] == "read-only"
    assert "--disable" in captured["args"]
    disabled_features = {
        captured["args"][index + 1]
        for index, arg in enumerate(captured["args"])
        if arg == "--disable"
    }
    assert "shell_tool" in disabled_features
    assert "--output-last-message" in captured["args"]
    assert "--model" in captured["args"]
    assert captured["args"][captured["args"].index("--model") + 1] == "gpt-5.4"
    assert captured["args"][-1] == "-"
    assert captured["input"].startswith(
        "Return only the Markdown agent-system instruction file."
    )
    assert captured["input"] not in captured["args"]
    assert captured["env"]["CODEX_HOME"] == "/tmp/codex-home"
    assert "OPENAI_API_KEY" not in captured["env"]
    assert "OPENAI_BASE_URL" not in captured["env"]
    assert captured["timeout"] == 9.0
    assert artifact.manifest["reflector_provider"] == "codex_cli"
    assert artifact.manifest["reflector_model"] == "gpt-5.4"


def test_agent_system_reflector_preserves_existing_agent_system_as_base(tmp_path, monkeypatch):
    _patch_reflector_llm(
        monkeypatch,
        content="# Base Agent System\n\nKeep changes minimal.\n\n## Reflections From Prior Trajectories",
    )
    base_path = tmp_path / "base-agents.md"
    base_path.write_text("# Base Agent System\n\nKeep changes minimal.\n", encoding="utf-8")
    job = _job(
        "agent_system_reflector",
        tmp_path,
        input_artifacts=[
            _reflector_dataset_artifact(tmp_path),
            {
                "artifact_id": "art_previous_agent_system",
                "type": "agent_system",
                "uri": base_path.as_uri(),
                "name": "previous agent system",
            },
        ],
        config={
            "name": "reflected-with-base",
            "reflector_llm": {
                "model": "reflector-model",
                "base_url": "http://reflector.test/v1",
                "api_key": "test-key",
            },
        },
    )

    artifact = run_method(job, artifact_root=tmp_path / "artifacts")[0]

    assert artifact.manifest["target_path"] == "agents.md"
    assert artifact.promoted is False
    assert artifact.lineage["input_artifact_ids"] == [
        "art_dataset_reflector",
        "art_previous_agent_system",
    ]
    text = Path(artifact.uri.removeprefix("file://")).read_text(encoding="utf-8")
    assert text.startswith("# Base Agent System\n\nKeep changes minimal.")
    assert "## Reflections From Prior Trajectories" in text


def test_agent_system_reflector_rejects_missing_dataset(tmp_path):
    job = _job("agent_system_reflector", tmp_path)

    try:
        run_method(job, artifact_root=tmp_path / "artifacts")
    except ValueError as exc:
        assert "agent_system_reflector requires an input dataset artifact" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_parametric_memory_register_returns_adapter_artifact(tmp_path):
    adapter_dir = tmp_path / "adapter"
    adapter_dir.mkdir()
    job = _job(
        "parametric_memory_register",
        tmp_path,
        config={
            "adapter_uri": adapter_dir.as_uri(),
            "base_model": "Qwen/Qwen3.6-27B",
        },
    )

    artifacts = run_method(job, artifact_root=tmp_path / "artifacts")

    artifact = artifacts[0]
    assert artifact.type == ArtifactType.PARAMETRIC_MEMORY
    assert artifact.uri == adapter_dir.as_uri()
    assert artifact.manifest["base_model"] == "Qwen/Qwen3.6-27B"
    assert artifact.manifest["adapter_format"] == "lora"
    assert artifact.manifest["adapter_id"] == "adapter"


def test_parametric_memory_register_preserves_configured_adapter_id(tmp_path):
    adapter_dir = tmp_path / "adapter"
    adapter_dir.mkdir()
    job = _job(
        "parametric_memory_register",
        tmp_path,
        config={
            "adapter_uri": adapter_dir.as_uri(),
            "base_model": "Qwen/Qwen3.6-27B",
            "adapter_id": "parser-memory",
        },
    )

    artifacts = run_method(job, artifact_root=tmp_path / "artifacts")

    assert artifacts[0].name == "parser-memory"
    assert artifacts[0].manifest["adapter_id"] == "parser-memory"


def test_parse_capabilities_defaults_to_reference_job_types():
    assert _parse_capabilities([]) == [
        "text_memory",
        "skill_bundle",
        "agent_system",
        "agent_system_reflector",
        "parametric_memory_register",
    ]


class FakeClient:
    def __init__(self, job: dict[str, Any] | None) -> None:
        self.job = job
        self.heartbeats: list[dict[str, Any]] = []
        self.completed: list[dict[str, Any]] = []
        self.failed: list[dict[str, Any]] = []

    def claim(
        self,
        worker_id: str,
        capabilities: list[str],
        *,
        lease_seconds: int | None = None,
    ) -> dict[str, Any] | None:
        return self.job

    def heartbeat(
        self,
        job_id: str,
        lease_id: str,
        *,
        progress: float | None = None,
        message: str | None = None,
    ) -> dict[str, Any]:
        self.heartbeats.append(
            {"job_id": job_id, "lease_id": lease_id, "progress": progress, "message": message}
        )
        return {}

    def complete(
        self,
        job_id: str,
        lease_id: str,
        artifacts: list[dict[str, Any]],
        *,
        report: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.completed.append(
            {"job_id": job_id, "lease_id": lease_id, "artifacts": artifacts, "report": report}
        )
        return {}

    def fail(
        self,
        job_id: str,
        lease_id: str,
        error: str,
        *,
        retryable: bool = True,
    ) -> dict[str, Any]:
        self.failed.append(
            {"job_id": job_id, "lease_id": lease_id, "error": error, "retryable": retryable}
        )
        return {}


def test_run_once_claims_and_completes_job(tmp_path):
    job = _job("skill_bundle", tmp_path, config={"name": "demo"}).model_dump(mode="json")
    client = FakeClient(job)

    result = run_once(
        client,
        worker_id="worker-1",
        capabilities=["skill_bundle"],
        artifact_root=tmp_path / "artifacts",
    )

    assert result is True
    assert client.heartbeats[0]["progress"] == 0.0
    assert client.completed[0]["job_id"] == "job_skill_bundle"
    assert client.completed[0]["artifacts"][0]["type"] == "skill_bundle"
    assert client.failed == []


def test_run_once_fails_unknown_method(tmp_path):
    job = _job("unknown_method", tmp_path).model_dump(mode="json")
    client = FakeClient(job)

    result = run_once(
        client,
        worker_id="worker-1",
        capabilities=["unknown_method"],
        artifact_root=tmp_path / "artifacts",
    )

    assert result is True
    assert client.completed == []
    assert client.failed[0]["job_id"] == "job_unknown_method"
    assert "Unknown evolution method" in client.failed[0]["error"]
    assert client.failed[0]["retryable"] is True


def test_run_once_fails_invalid_claim_payload_with_job_identity(tmp_path):
    client = FakeClient(
        {
            "job_id": "job_invalid",
            "lease_id": "lease_invalid",
            "job_type": "reference",
        }
    )

    result = run_once(
        client,
        worker_id="worker-1",
        capabilities=["reference"],
        artifact_root=tmp_path / "artifacts",
    )

    assert result is True
    assert client.completed == []
    assert client.failed[0]["job_id"] == "job_invalid"
    assert client.failed[0]["lease_id"] == "lease_invalid"
