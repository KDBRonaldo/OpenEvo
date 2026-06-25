from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from openevo.experiment import runner as openevo_runner
from openevo.experiment.models import ExperimentConfig
from openevo.experiment.runner import dry_run_experiment, run_experiment


def _config(**overrides: object) -> ExperimentConfig:
    payload = {
        "version": 1,
        "experiment": {"name": "biology-components"},
        "agent": {"preset": "codex", "model": "gpt-5.1-codex-mini"},
        "runtime": {"image": "runtime:latest"},
        "tasks": [
            {
                "id": "component-extraction-train",
                "instruction": "Extract biological components into final_components.json.",
                "workspace": "/root/codex54minitest/five_article_agentic_workflow_subset",
            }
        ],
    }
    payload.update(overrides)
    return ExperimentConfig.model_validate(payload)


def test_dry_run_emits_three_evolution_jobs_per_task_round() -> None:
    plan = dry_run_experiment(_config(), rounds_override=2)

    rounds = plan["tasks"][0]["rounds"]

    assert len(rounds) == 2
    assert [job["method"] for job in rounds[0]["evolution_jobs"]] == [
        "text_memory_reflector",
        "skill_bundle_reflector",
        "agent_system_reflector",
    ]
    assert [job["method"] for job in rounds[1]["evolution_jobs"]] == [
        "text_memory_reflector",
        "skill_bundle_reflector",
        "agent_system_history_reflector",
    ]


def test_dry_run_shows_multi_round_context_placeholders() -> None:
    plan = dry_run_experiment(_config(), rounds_override=2)

    round_1 = plan["tasks"][0]["rounds"][1]

    assert round_1["rollout_payload"]["metadata"]["evolution"][
        "context_artifact_ids"
    ] == [
        "<text_memory_artifact:component-extraction-train:round-0>",
        "<skill_bundle_artifact:component-extraction-train:round-0>",
        "<agent_system_artifact:component-extraction-train:round-0>",
    ]
    assert round_1["evolution_jobs"][2]["input_artifact_ids"] == [
        "<dataset_artifact:component-extraction-train:round-1>",
        "<dataset_artifact:component-extraction-train:round-0>",
        "<agent_system_artifact:component-extraction-train:round-0>",
    ]


def test_dry_run_task_filter_limits_tasks() -> None:
    config = _config(
        tasks=[
            {"id": "task-a", "instruction": "Do A.", "workspace": "/tmp/a"},
            {"id": "task-b", "instruction": "Do B.", "workspace": "/tmp/b"},
        ],
    )

    plan = dry_run_experiment(config, task_ids=["task-b"])

    assert [task["task_id"] for task in plan["tasks"]] == ["task-b"]


def test_live_runner_calls_services_and_worker_in_order(tmp_path: Path) -> None:
    rollout = FakeRolloutClient()
    evolution = FakeEvolutionClient()
    worker = FakeWorkerRunner()

    result = run_experiment(
        _config(),
        output_dir=tmp_path / "run",
        rollout_client=rollout,
        evolution_client=evolution,
        worker_runner=worker,
        poll_interval_seconds=0.0,
        max_poll_attempts=1,
    )

    assert result["status"] == "completed"
    policy_version = rollout.submitted[0]["metadata"]["policy_version"]
    assert policy_version.startswith(
        "openevo:biology-components:component-extraction-train:run-"
    )
    assert policy_version.endswith(":round-0")
    assert evolution.datasets[0]["query"]["policy_version"] == policy_version
    assert [job["method"] for job in evolution.jobs] == [
        "text_memory_reflector",
        "skill_bundle_reflector",
        "agent_system_reflector",
    ]
    assert all(
        call["capabilities"][0] == job["job_type"]
        for call, job in zip(worker.calls, evolution.jobs, strict=True)
    )
    round_result = result["tasks"][0]["rounds"][0]
    assert round_result["artifact_ids"]["dataset"] == ["dataset-artifact-1"]
    assert round_result["artifact_ids"]["text_memory"] == ["artifact-text-memory"]
    assert round_result["artifact_ids"]["skill_bundle"] == ["artifact-skill-bundle"]
    assert round_result["artifact_ids"]["agent_system"] == ["artifact-agent-system"]
    assert (tmp_path / "run" / "summary.json").exists()


def test_live_runner_rejects_non_positive_max_poll_attempts(tmp_path: Path) -> None:
    rollout = FakeRolloutClient()

    try:
        run_experiment(
            _config(),
            output_dir=tmp_path / "run",
            rollout_client=rollout,
            evolution_client=FakeEvolutionClient(),
            worker_runner=FakeWorkerRunner(),
            poll_interval_seconds=0.0,
            max_poll_attempts=0,
        )
    except ValueError as exc:
        assert "max_poll_attempts must be at least 1" in str(exc)
    else:
        raise AssertionError("expected ValueError")

    assert rollout.submitted == []


def test_live_runner_rejects_non_integer_max_poll_attempts(tmp_path: Path) -> None:
    rollout = FakeRolloutClient()

    try:
        run_experiment(
            _config(),
            output_dir=tmp_path / "run",
            rollout_client=rollout,
            evolution_client=FakeEvolutionClient(),
            worker_runner=FakeWorkerRunner(),
            poll_interval_seconds=0.0,
            max_poll_attempts=1.5,  # type: ignore[arg-type]
        )
    except ValueError as exc:
        assert "max_poll_attempts must be an integer" in str(exc)
    else:
        raise AssertionError("expected ValueError")

    assert rollout.submitted == []


def test_live_runner_rejects_negative_poll_interval(tmp_path: Path) -> None:
    rollout = FakeRolloutClient()

    try:
        run_experiment(
            _config(),
            output_dir=tmp_path / "run",
            rollout_client=rollout,
            evolution_client=FakeEvolutionClient(),
            worker_runner=FakeWorkerRunner(),
            poll_interval_seconds=-1.0,
            max_poll_attempts=1,
        )
    except ValueError as exc:
        assert "poll_interval_seconds must be non-negative" in str(exc)
    else:
        raise AssertionError("expected ValueError")

    assert rollout.submitted == []


def test_live_runner_rejects_non_numeric_poll_interval(tmp_path: Path) -> None:
    rollout = FakeRolloutClient()

    try:
        run_experiment(
            _config(),
            output_dir=tmp_path / "run",
            rollout_client=rollout,
            evolution_client=FakeEvolutionClient(),
            worker_runner=FakeWorkerRunner(),
            poll_interval_seconds="fast",  # type: ignore[arg-type]
            max_poll_attempts=1,
        )
    except ValueError as exc:
        assert "poll_interval_seconds must be a number" in str(exc)
    else:
        raise AssertionError("expected ValueError")

    assert rollout.submitted == []


def test_live_runner_scopes_policy_versions_to_each_run(tmp_path: Path) -> None:
    rollout = FakeRolloutClient()
    evolution = FakeEvolutionClient()

    run_experiment(
        _config(),
        output_dir=tmp_path / "run-1",
        rollout_client=rollout,
        evolution_client=evolution,
        worker_runner=FakeWorkerRunner(),
        poll_interval_seconds=0.0,
        max_poll_attempts=1,
    )
    run_experiment(
        _config(),
        output_dir=tmp_path / "run-2",
        rollout_client=rollout,
        evolution_client=evolution,
        worker_runner=FakeWorkerRunner(),
        poll_interval_seconds=0.0,
        max_poll_attempts=1,
    )

    first_policy_version = evolution.datasets[0]["query"]["policy_version"]
    second_policy_version = evolution.datasets[1]["query"]["policy_version"]

    assert first_policy_version != second_policy_version
    assert ":run-" in first_policy_version
    assert ":run-" in second_policy_version
    assert rollout.submitted[0]["metadata"]["policy_version"] == first_policy_version
    assert rollout.submitted[1]["metadata"]["policy_version"] == second_policy_version


def test_live_runner_default_output_dir_is_run_scoped(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)

    result = run_experiment(
        _config(),
        rollout_client=FakeRolloutClient(),
        evolution_client=FakeEvolutionClient(),
        worker_runner=FakeWorkerRunner(),
        poll_interval_seconds=0.0,
        max_poll_attempts=1,
    )

    summary_path = Path(result["summary_path"]).resolve()

    assert result["run_id"]
    assert summary_path == (
        tmp_path
        / ".openevo"
        / "runs"
        / "biology-components"
        / result["run_id"]
        / "summary.json"
    )
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert summary["summary_path"] == result["summary_path"]


def test_live_runner_default_output_dir_sanitizes_experiment_name(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)

    result = run_experiment(
        _config(experiment={"name": "../../unsafe experiment/name"}),
        rollout_client=FakeRolloutClient(),
        evolution_client=FakeEvolutionClient(),
        worker_runner=FakeWorkerRunner(),
        poll_interval_seconds=0.0,
        max_poll_attempts=1,
    )

    summary_path = Path(result["summary_path"]).resolve()

    assert result["experiment_name"] == "../../unsafe experiment/name"
    assert result["run_id"]
    assert summary_path == (
        tmp_path
        / ".openevo"
        / "runs"
        / "unsafe-experiment-name"
        / result["run_id"]
        / "summary.json"
    )


def test_live_runner_uses_run_scoped_job_types_for_worker_claims(tmp_path: Path) -> None:
    evolution = FakeEvolutionClient()
    worker = FakeWorkerRunner()

    run_experiment(
        _config(),
        output_dir=tmp_path / "run",
        rollout_client=FakeRolloutClient(),
        evolution_client=evolution,
        worker_runner=worker,
        poll_interval_seconds=0.0,
        max_poll_attempts=1,
    )

    assert len(evolution.jobs) == len(worker.calls)
    for job, call in zip(evolution.jobs, worker.calls, strict=True):
        capability = call["capabilities"][0]
        assert job["job_type"] == capability
        assert job["job_type"] != job["method"]
        assert job["method"] in job["job_type"]


def test_live_runner_rejects_worker_artifacts_from_unexpected_job_id(
    tmp_path: Path,
) -> None:
    result = run_experiment(
        _config(),
        output_dir=tmp_path / "run",
        rollout_client=FakeRolloutClient(),
        evolution_client=FakeEvolutionClient(),
        worker_runner=lambda **_: [
            {"job_id": "unrelated-job", "artifact_ids": ["wrong-artifact"]}
        ],
        poll_interval_seconds=0.0,
        max_poll_attempts=1,
    )

    round_result = result["tasks"][0]["rounds"][0]

    assert result["status"] == "failed"
    assert round_result["jobs"][0]["worker_status"] == "unexpected_job"
    assert round_result["jobs"][0]["artifact_ids"] == []
    assert round_result["artifact_ids"]["text_memory"] == []


def test_live_runner_fails_mixed_worker_results_with_unexpected_job_id(
    tmp_path: Path,
) -> None:
    def mixed_worker(**kwargs: Any) -> list[dict[str, Any]]:
        return [
            {"job_id": kwargs["expected_job_id"], "artifact_ids": ["expected-artifact"]},
            {"job_id": "unrelated-job", "artifact_ids": ["wrong-artifact"]},
        ]

    result = run_experiment(
        _config(),
        output_dir=tmp_path / "run",
        rollout_client=FakeRolloutClient(),
        evolution_client=FakeEvolutionClient(),
        worker_runner=mixed_worker,
        poll_interval_seconds=0.0,
        max_poll_attempts=1,
    )

    round_result = result["tasks"][0]["rounds"][0]

    assert result["status"] == "failed"
    assert round_result["jobs"][0]["worker_status"] == "unexpected_job"
    assert round_result["jobs"][0]["artifact_ids"] == ["expected-artifact"]
    assert round_result["jobs"][0]["unexpected_job_ids"] == ["unrelated-job"]
    assert round_result["artifact_ids"]["text_memory"] == []


def test_live_runner_does_not_create_jobs_for_empty_dataset(tmp_path: Path) -> None:
    evolution = FakeEvolutionClient(dataset_event_count=0, dataset_trace_count=0)

    result = run_experiment(
        _config(),
        output_dir=tmp_path / "run",
        rollout_client=FakeRolloutClient(),
        evolution_client=evolution,
        worker_runner=FakeWorkerRunner(),
        poll_interval_seconds=0.0,
        max_poll_attempts=1,
    )

    round_result = result["tasks"][0]["rounds"][0]

    assert result["status"] == "failed"
    assert round_result["dataset_status"] == "empty"
    assert round_result["jobs"] == []
    assert evolution.jobs == []
    assert round_result["artifact_ids"]["dataset"] == []


def test_live_runner_stops_task_after_empty_dataset_in_multi_round_run(
    tmp_path: Path,
) -> None:
    evolution = FakeEvolutionClient(dataset_event_count=0, dataset_trace_count=0)

    result = run_experiment(
        _config(),
        rounds_override=2,
        output_dir=tmp_path / "run",
        rollout_client=FakeRolloutClient(),
        evolution_client=evolution,
        worker_runner=FakeWorkerRunner(),
        poll_interval_seconds=0.0,
        max_poll_attempts=1,
    )

    rounds = result["tasks"][0]["rounds"]

    assert result["status"] == "failed"
    assert len(rounds) == 1
    assert rounds[0]["dataset_status"] == "empty"


def test_live_runner_stops_task_after_worker_failure_in_multi_round_run(
    tmp_path: Path,
) -> None:
    result = run_experiment(
        _config(),
        rounds_override=2,
        output_dir=tmp_path / "run",
        rollout_client=FakeRolloutClient(),
        evolution_client=FakeEvolutionClient(),
        worker_runner=lambda **_: [{"job_id": "wrong-job", "artifact_ids": ["bad"]}],
        poll_interval_seconds=0.0,
        max_poll_attempts=1,
    )

    rounds = result["tasks"][0]["rounds"]

    assert result["status"] == "failed"
    assert len(rounds) == 1
    assert rounds[0]["jobs"][0]["worker_status"] == "unexpected_job"


def test_live_runner_passes_prior_datasets_to_history_reflector(tmp_path: Path) -> None:
    rollout = FakeRolloutClient()
    evolution = FakeEvolutionClient()
    worker = FakeWorkerRunner()

    run_experiment(
        _config(),
        rounds_override=2,
        output_dir=tmp_path / "run",
        rollout_client=rollout,
        evolution_client=evolution,
        worker_runner=worker,
        poll_interval_seconds=0.0,
        max_poll_attempts=1,
    )

    agent_system_jobs = [
        job for job in evolution.jobs if job["method"].startswith("agent_system_")
    ]
    assert [job["method"] for job in agent_system_jobs] == [
        "agent_system_reflector",
        "agent_system_history_reflector",
    ]
    assert agent_system_jobs[1]["input_artifact_ids"][:2] == [
        "dataset-artifact-2",
        "dataset-artifact-1",
    ]


def test_live_runner_rollouts_use_only_latest_evolved_artifacts(
    tmp_path: Path,
) -> None:
    rollout = FakeRolloutClient()
    worker = UniqueArtifactWorkerRunner()

    run_experiment(
        _config(),
        rounds_override=3,
        output_dir=tmp_path / "run",
        rollout_client=rollout,
        evolution_client=FakeEvolutionClient(),
        worker_runner=worker,
        poll_interval_seconds=0.0,
        max_poll_attempts=1,
    )

    first_context = rollout.submitted[0]["metadata"].get("evolution")
    second_context = rollout.submitted[1]["metadata"]["evolution"]["context_artifact_ids"]
    third_context = rollout.submitted[2]["metadata"]["evolution"]["context_artifact_ids"]

    assert first_context == {"context_artifact_ids": []}
    assert second_context == [
        "text_memory_reflector-artifact-1",
        "skill_bundle_reflector-artifact-1",
        "agent_system_reflector-artifact-1",
    ]
    assert third_context == [
        "text_memory_reflector-artifact-2",
        "skill_bundle_reflector-artifact-2",
        "agent_system_history_reflector-artifact-1",
    ]


def test_live_runner_snapshots_round_artifact_ids(tmp_path: Path) -> None:
    result = run_experiment(
        _config(),
        rounds_override=2,
        output_dir=tmp_path / "run",
        rollout_client=FakeRolloutClient(),
        evolution_client=FakeEvolutionClient(),
        worker_runner=FakeWorkerRunner(),
        poll_interval_seconds=0.0,
        max_poll_attempts=1,
    )

    first_round = result["tasks"][0]["rounds"][0]
    second_round = result["tasks"][0]["rounds"][1]

    assert first_round["artifact_ids"]["dataset"] == ["dataset-artifact-1"]
    assert first_round["artifact_ids"]["agent_system"] == ["artifact-agent-system"]
    assert second_round["artifact_ids"]["dataset"] == [
        "dataset-artifact-1",
        "dataset-artifact-2",
    ]
    assert second_round["artifact_ids"]["agent_system"] == [
        "artifact-agent-system",
        "artifact-agent-system-history",
    ]


def test_live_runner_fails_when_worker_produces_no_artifact(tmp_path: Path) -> None:
    result = run_experiment(
        _config(),
        output_dir=tmp_path / "run",
        rollout_client=FakeRolloutClient(),
        evolution_client=FakeEvolutionClient(),
        worker_runner=lambda **_: [{"claimed": True, "artifact_ids": []}],
        poll_interval_seconds=0.0,
        max_poll_attempts=1,
    )

    assert result["status"] == "failed"
    assert result["tasks"][0]["rounds"][0]["jobs"][0]["worker_status"] == (
        "missing_artifacts"
    )


def test_live_runner_reports_expected_worker_failure(tmp_path: Path) -> None:
    result = run_experiment(
        _config(),
        output_dir=tmp_path / "run",
        rollout_client=FakeRolloutClient(),
        evolution_client=FakeEvolutionClient(),
        worker_runner=lambda **kwargs: [
            {
                "job_id": kwargs["expected_job_id"],
                "state": "failed",
                "error": "reflector crashed",
            }
        ],
        poll_interval_seconds=0.0,
        max_poll_attempts=1,
    )

    job_result = result["tasks"][0]["rounds"][0]["jobs"][0]

    assert result["status"] == "failed"
    assert job_result["worker_status"] == "failed"
    assert job_result["worker_error"] == "reflector crashed"
    assert job_result["artifact_ids"] == []


def test_local_worker_runner_returns_recorded_failures(
    tmp_path: Path,
    monkeypatch,
) -> None:
    def fake_parent_fail(self, job_id, lease_id, error, *, retryable=True):
        return {"job_id": job_id, "state": "failed", "error": error}

    def fake_run_once(client, **kwargs):
        client.fail("job-1", "lease-1", "reflector crashed", retryable=False)
        return True

    monkeypatch.setattr(openevo_runner.EvolutionWorkerClient, "fail", fake_parent_fail)
    monkeypatch.setattr(openevo_runner, "run_once", fake_run_once)

    result = openevo_runner._run_local_worker_once(
        base_url="http://evolution.test",
        artifact_root=tmp_path / "artifacts",
        capabilities=["openevo:run:task:round-0:text_memory_reflector"],
    )

    assert result == [{"job_id": "job-1", "state": "failed", "error": "reflector crashed"}]


class FakeRolloutClient:
    def __init__(self) -> None:
        self.submitted: list[dict[str, Any]] = []

    def submit_task(self, payload: dict[str, Any]) -> str:
        self.submitted.append(payload)
        return "rollout-task-1"

    def get_task(self, task_id: str) -> dict[str, Any]:
        return {"task_id": task_id, "status": "completed", "results": []}


class FakeEvolutionClient:
    def __init__(
        self,
        *,
        dataset_event_count: int = 1,
        dataset_trace_count: int = 1,
    ) -> None:
        self.datasets: list[dict[str, Any]] = []
        self.jobs: list[dict[str, Any]] = []
        self.dataset_event_count = dataset_event_count
        self.dataset_trace_count = dataset_trace_count

    def create_dataset(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.datasets.append(payload)
        index = len(self.datasets)
        return {
            "dataset_id": f"dataset-{index}",
            "artifact_id": f"dataset-artifact-{index}",
            "event_count": self.dataset_event_count,
            "trace_count": self.dataset_trace_count,
        }

    def create_job(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.jobs.append(payload)
        return {"job_id": f"job-{len(self.jobs)}", "state": "pending"}


class FakeWorkerRunner:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.calls.append(kwargs)
        capability = kwargs["capabilities"][0]
        method = capability.rsplit(":", maxsplit=1)[-1]
        artifact_id = {
            "text_memory_reflector": "artifact-text-memory",
            "skill_bundle_reflector": "artifact-skill-bundle",
            "agent_system_reflector": "artifact-agent-system",
            "agent_system_history_reflector": "artifact-agent-system-history",
        }[method]
        return [
            {
                "claimed": True,
                "job_id": kwargs["expected_job_id"],
                "artifact_ids": [artifact_id],
            }
        ]


class UniqueArtifactWorkerRunner:
    def __init__(self) -> None:
        self.method_counts: dict[str, int] = {}

    def __call__(self, **kwargs: Any) -> list[dict[str, Any]]:
        capability = kwargs["capabilities"][0]
        method = capability.rsplit(":", maxsplit=1)[-1]
        self.method_counts[method] = self.method_counts.get(method, 0) + 1
        return [
            {
                "claimed": True,
                "job_id": kwargs["expected_job_id"],
                "artifact_ids": [f"{method}-artifact-{self.method_counts[method]}"],
            }
        ]
