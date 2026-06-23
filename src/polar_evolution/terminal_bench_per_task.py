from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse


@dataclass(frozen=True)
class EvolutionArtifact:
    artifact_type: str
    artifact_id: str
    path: Path
    task_id: str
    round: int
    method: str
    source_dataset_artifact_ids: list[str] = field(default_factory=list)


class ArtifactMaterializer:
    def __init__(self) -> None:
        self.skipped: list[dict[str, str]] = []

    def materialize(self, artifact: EvolutionArtifact) -> dict[str, str]:
        if artifact.artifact_type == "agent_system":
            return {"agent_system_path": str(artifact.path)}
        if artifact.artifact_type in {"skill_bundle", "text_memory", "parametric_memory", "memory"}:
            self.skipped.append(
                {
                    "artifact_id": artifact.artifact_id,
                    "artifact_type": artifact.artifact_type,
                    "reason": f"{artifact.artifact_type} materialization is not implemented for Harbor Codex runs",
                }
            )
            return {}
        raise ValueError(f"unsupported evolution artifact type: {artifact.artifact_type}")


def build_harbor_command(
    *,
    job_name: str,
    task_root: Path,
    task_id: str,
    model: str,
    env_json: dict[str, str],
    agent_kwargs: dict[str, str],
    verifier_env: dict[str, str],
    n_concurrent: int,
) -> list[str]:
    command = [
        "harbor",
        "run",
        "--job-name",
        job_name,
        "--path",
        str(task_root),
        "--include-task-name",
        task_id,
        "--n-attempts",
        "1",
        "--n-concurrent",
        str(n_concurrent),
        "--agent-import-path",
        "task_packages.terminal_bench_v1.harbor_agent:EvoLabHarborAgent",
        "--model",
        model,
        "--ak",
        "mode=codex_subscription",
        "--ak",
        f"env_json={json.dumps(env_json, sort_keys=True, separators=(',', ':'))}",
    ]
    for key in sorted(agent_kwargs):
        command.extend(["--ak", f"{key}={agent_kwargs[key]}"])
    for key in sorted(verifier_env):
        command.extend(["--verifier-env", f"{key}={verifier_env[key]}"])
    return command


def discover_agent_system_artifact_path(
    completed_artifacts: list[dict[str, Any]],
    *,
    task_id: str,
    round_number: int,
    job_payload: dict[str, Any],
) -> EvolutionArtifact:
    job = job_payload.get("job")
    if not isinstance(job, dict):
        raise ValueError("job_payload['job'] must be a dict")
    input_artifact_ids = job.get("input_artifact_ids")
    if not isinstance(input_artifact_ids, list):
        raise ValueError("job_payload['job']['input_artifact_ids'] must be a list")

    for artifact in completed_artifacts:
        if artifact.get("type") != "agent_system":
            continue
        uri = artifact.get("uri")
        if not isinstance(uri, str):
            raise ValueError(f"agent_system artifact has unsupported uri: {uri!r}")
        parsed = urlparse(uri)
        if parsed.scheme != "file":
            raise ValueError(f"agent_system artifact has unsupported uri: {uri!r}")
        if parsed.netloc not in {"", "localhost"}:
            raise ValueError(f"agent_system artifact has unsupported file URI host: {uri!r}")
        path_text = unquote(parsed.path)
        if not path_text:
            raise ValueError(f"agent_system artifact has empty path in uri: {uri!r}")
        manifest = artifact.get("manifest")
        if not isinstance(manifest, dict):
            manifest = {}
        artifact_id = artifact.get("artifact_id")
        if not isinstance(artifact_id, str) or not artifact_id.strip():
            raise ValueError("completed agent_system artifact missing required artifact_id")
        return EvolutionArtifact(
            artifact_type="agent_system",
            artifact_id=artifact_id,
            path=Path(path_text),
            task_id=task_id,
            round=round_number,
            method=str(manifest.get("method") or "agent_system_reflector"),
            source_dataset_artifact_ids=list(input_artifact_ids),
        )
    raise ValueError("completed job did not produce an agent_system artifact")


def summarize_transition(before: float | None, after: float | None) -> str:
    before_passed = (before or 0.0) >= 1.0
    after_passed = (after or 0.0) >= 1.0
    if before_passed and after_passed:
        return "pass_to_pass"
    if before_passed and not after_passed:
        return "pass_to_fail"
    if not before_passed and after_passed:
        return "fail_to_pass"
    return "fail_to_fail"
