from __future__ import annotations

import json
from pathlib import Path

import pytest

import polar_evolution.cli as cli_module
import polar_evolution.terminal_bench_per_task as per_task_module
from polar_evolution.cli import _parse_key_value_entries, main
from polar_evolution.models import ArtifactRegisterRequest, ArtifactType
from polar_evolution.terminal_bench_per_task import (
    ArtifactMaterializer,
    EvolutionArtifact,
    _find_baseline_trial,
    _run_worker_once_local,
    _trial_reward,
    build_harbor_command,
    discover_agent_system_artifact_path,
    run_per_task_evolution,
    summarize_transition,
)


def test_agent_system_materializer_sets_harbor_kwargs(tmp_path: Path):
    artifact_path = tmp_path / "AGENTS.md"
    artifact_path.write_text("Inspect files first.\n", encoding="utf-8")
    materializer = ArtifactMaterializer()

    kwargs = materializer.materialize(
        EvolutionArtifact(
            artifact_type="agent_system",
            artifact_id="art-agent",
            path=artifact_path,
            task_id="fix-git",
            round=1,
            method="agent_system_reflector",
            source_dataset_artifact_ids=["dataset-r0"],
        )
    )

    assert kwargs == {"agent_system_path": str(artifact_path)}


def test_skill_and_memory_materializers_are_explicitly_skipped(tmp_path: Path):
    materializer = ArtifactMaterializer()
    skill = EvolutionArtifact(
        artifact_type="skill_bundle",
        artifact_id="art-skill",
        path=tmp_path / "skills",
        task_id="fix-git",
        round=1,
        method="skill_bundle",
        source_dataset_artifact_ids=[],
    )
    text_memory = EvolutionArtifact(
        artifact_type="text_memory",
        artifact_id="art-text-memory",
        path=tmp_path / "text_memory.md",
        task_id="fix-git",
        round=1,
        method="text_memory",
        source_dataset_artifact_ids=[],
    )
    parametric_memory = EvolutionArtifact(
        artifact_type="parametric_memory",
        artifact_id="art-param-memory",
        path=tmp_path / "parametric_memory.md",
        task_id="fix-git",
        round=1,
        method="parametric_memory",
        source_dataset_artifact_ids=[],
    )

    assert materializer.materialize(skill) == {}
    assert materializer.materialize(text_memory) == {}
    assert materializer.materialize(parametric_memory) == {}
    assert materializer.skipped == [
        {
            "artifact_id": "art-skill",
            "artifact_type": "skill_bundle",
            "reason": "skill_bundle materialization is not implemented for Harbor Codex runs",
        },
        {
            "artifact_id": "art-text-memory",
            "artifact_type": "text_memory",
            "reason": "text_memory materialization is not implemented for Harbor Codex runs",
        },
        {
            "artifact_id": "art-param-memory",
            "artifact_type": "parametric_memory",
            "reason": "parametric_memory materialization is not implemented for Harbor Codex runs",
        },
    ]


def test_build_harbor_command_includes_agent_system_path_and_subscription_env():
    command = build_harbor_command(
        job_name="tb21-evolved-fix-git-r1",
        task_root=Path("/root/datasets/terminal-bench-2-1/tasks"),
        task_id="fix-git",
        jobs_dir=Path("/tmp/tb21/fix-git/r1/harbor_jobs"),
        model="gpt-5.5",
        env_json={"NO_PROXY": "localhost"},
        agent_kwargs={"agent_system_path": "/tmp/AGENTS.md"},
        verifier_env={"UV_NO_INDEX": "1"},
        n_concurrent=1,
    )

    assert command[:2] == ["harbor", "run"]
    assert "--include-task-name" in command
    assert "fix-git" in command
    assert "--jobs-dir" in command
    assert "/tmp/tb21/fix-git/r1/harbor_jobs" in command
    assert "--environment-import-path" in command
    assert (
        "task_packages.terminal_bench_v1.harbor_environment:DockerCpHarborEnvironment" in command
    )
    assert "--no-delete" in command
    assert command[command.index("--cpus") + 1] == "ignore"
    assert command[command.index("--memory") + 1] == "ignore"
    compose_values = [
        command[index + 1]
        for index, value in enumerate(command)
        if value == "--extra-docker-compose"
    ]
    assert compose_values == [
        "/root/EvoLabCore-terminal-bench-task-package/task_packages/terminal_bench_v1/harbor/pull-never.yaml",
        "/root/EvoLabCore-terminal-bench-task-package/task_packages/terminal_bench_v1/harbor/docker-cp-host-network.yaml",
    ]
    assert "--ak" in command
    assert "mode=codex_subscription" in command
    assert "agent_system_path=/tmp/AGENTS.md" in command
    assert 'env_json={"NO_PROXY":"localhost"}' in command
    assert "--verifier-env" in command
    assert "UV_NO_INDEX=1" in command


def test_discover_agent_system_artifact_path_reads_worker_manifest(tmp_path: Path):
    content = tmp_path / "workers" / "job-1" / "agent_system_reflector" / "agents.md"
    content.parent.mkdir(parents=True)
    content.write_text("rules\n", encoding="utf-8")
    job_payload = {
        "job": {
            "input_artifact_ids": ["dataset-r0"],
        }
    }
    completed_artifacts = [
        {
            "artifact_id": "art-agent",
            "type": "agent_system",
            "uri": content.resolve().as_uri(),
            "manifest": {"method": "agent_system_reflector"},
        }
    ]

    artifact = discover_agent_system_artifact_path(
        completed_artifacts,
        task_id="fix-git",
        round_number=1,
        job_payload=job_payload,
    )

    assert artifact.artifact_type == "agent_system"
    assert artifact.artifact_id == "art-agent"
    assert artifact.path == content
    assert artifact.source_dataset_artifact_ids == ["dataset-r0"]


def test_discover_agent_system_artifact_path_decodes_spaces_in_file_uri(tmp_path: Path):
    content = tmp_path / "workers with spaces" / "job-1" / "agent system" / "agents.md"
    content.parent.mkdir(parents=True)
    content.write_text("rules\n", encoding="utf-8")

    artifact = discover_agent_system_artifact_path(
        [
            {
                "artifact_id": "art-agent",
                "type": "agent_system",
                "uri": content.resolve().as_uri(),
                "manifest": {"method": "agent_system_reflector"},
            }
        ],
        task_id="fix-git",
        round_number=1,
        job_payload={"job": {"input_artifact_ids": []}},
    )

    assert artifact.path == content


def test_discover_agent_system_artifact_path_rejects_missing_artifact_id(tmp_path: Path):
    content = tmp_path / "agents.md"
    content.write_text("rules\n", encoding="utf-8")

    with pytest.raises(ValueError, match="artifact_id"):
        discover_agent_system_artifact_path(
            [
                {
                    "type": "agent_system",
                    "uri": content.resolve().as_uri(),
                    "manifest": {"method": "agent_system_reflector"},
                }
            ],
            task_id="fix-git",
            round_number=1,
            job_payload={"job": {"input_artifact_ids": []}},
        )


def test_discover_agent_system_artifact_path_rejects_non_list_input_artifact_ids(tmp_path: Path):
    content = tmp_path / "agents.md"
    content.write_text("rules\n", encoding="utf-8")

    with pytest.raises(ValueError, match="input_artifact_ids"):
        discover_agent_system_artifact_path(
            [
                {
                    "artifact_id": "art-agent",
                    "type": "agent_system",
                    "uri": content.resolve().as_uri(),
                    "manifest": {"method": "agent_system_reflector"},
                }
            ],
            task_id="fix-git",
            round_number=1,
            job_payload={"job": {"input_artifact_ids": "dataset-r0"}},
        )


def test_discover_agent_system_artifact_path_rejects_non_string_input_artifact_ids(tmp_path: Path):
    content = tmp_path / "agents.md"
    content.write_text("rules\n", encoding="utf-8")

    with pytest.raises(ValueError, match="input_artifact_ids"):
        discover_agent_system_artifact_path(
            [
                {
                    "artifact_id": "art-agent",
                    "type": "agent_system",
                    "uri": content.resolve().as_uri(),
                    "manifest": {"method": "agent_system_reflector"},
                }
            ],
            task_id="fix-git",
            round_number=1,
            job_payload={"job": {"input_artifact_ids": [None, 42, "dataset-r0"]}},
        )


def test_summarize_transition_classifies_pass_fail_changes():
    assert summarize_transition(0.0, 1.0) == "fail_to_pass"
    assert summarize_transition(1.0, 0.0) == "pass_to_fail"
    assert summarize_transition(1.0, 1.0) == "pass_to_pass"
    assert summarize_transition(0.0, 0.0) == "fail_to_fail"


def test_terminal_bench_per_task_evolution_cli_dry_run_writes_plan(tmp_path: Path):
    output = tmp_path / "summary.json"
    run_root = tmp_path / "run"
    exit_code = main(
        [
            "terminal-bench-per-task-evolution",
            "--task-root",
            "/root/datasets/terminal-bench-2-1/tasks",
            "--task-id",
            "fix-git",
            "--run-root",
            str(run_root),
            "--model",
            "gpt-5.5",
            "--reflector-model",
            "gpt-5.5",
            "--dry-run",
            "--output",
            str(output),
        ]
    )

    assert exit_code == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["dry_run"] is True
    assert payload["reflector_provider"] == "codex_cli"
    assert payload["gepa_generations"] == 1
    assert payload["terminal_bench_package_root"] == "/root/EvoLabCore-terminal-bench-task-package"
    assert payload["tasks"] == [
        {
            "task_id": "fix-git",
            "rounds": 1,
            "artifact_types": ["agent_system"],
        }
    ]
    assert not run_root.exists()


def test_terminal_bench_per_task_evolution_cli_respects_explicit_artifact_type(
    tmp_path: Path,
):
    output = tmp_path / "summary.json"
    run_root = tmp_path / "run"
    exit_code = main(
        [
            "terminal-bench-per-task-evolution",
            "--task-root",
            "/root/datasets/terminal-bench-2-1/tasks",
            "--task-id",
            "fix-git",
            "--run-root",
            str(run_root),
            "--model",
            "gpt-5.5",
            "--reflector-model",
            "gpt-5.5",
            "--artifact-type",
            "memory",
            "--dry-run",
            "--output",
            str(output),
        ]
    )

    assert exit_code == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["tasks"] == [
        {
            "task_id": "fix-git",
            "rounds": 1,
            "artifact_types": ["memory"],
        }
    ]
    assert not run_root.exists()


def test_one_round_orchestration_uses_existing_baseline_and_runs_harbor(tmp_path: Path):
    baseline = tmp_path / "baseline" / "fix-git__abc"
    (baseline / "agent").mkdir(parents=True)
    (baseline / "verifier").mkdir()
    (baseline / "result.json").write_text(
        json.dumps(
            {
                "trial_name": "fix-git__abc",
                "task_name": "fix-git",
                "status": "COMPLETED",
                "agent_result": {
                    "metadata": {
                        "terminal_bench_harbor_agent": {
                            "task_id": "fix-git",
                            "model_name": "gpt-5.5",
                        }
                    }
                },
                "verifier_result": {"rewards": {"reward": 0.0}},
            }
        ),
        encoding="utf-8",
    )
    (baseline / "agent" / "instruction.txt").write_text(
        "Find the missing git changes.", encoding="utf-8"
    )
    (baseline / "agent" / "stdout.txt").write_text("missed the target branch\n", encoding="utf-8")
    (baseline / "verifier" / "reward.txt").write_text("0.0\n", encoding="utf-8")

    evolved_trial = tmp_path / "run" / "harbor" / "fix-git-r1" / "fix-git__r1"
    (evolved_trial / "agent").mkdir(parents=True)
    (evolved_trial / "verifier").mkdir()
    (evolved_trial / "result.json").write_text(
        json.dumps(
            {
                "trial_name": "fix-git__r1",
                "task_name": "fix-git",
                "status": "COMPLETED",
                "agent_result": {
                    "metadata": {
                        "terminal_bench_harbor_agent": {
                            "task_id": "fix-git",
                            "model_name": "gpt-5.5",
                        }
                    }
                },
                "verifier_result": {"rewards": {"reward": 1.0}},
            }
        ),
        encoding="utf-8",
    )
    (evolved_trial / "agent" / "instruction.txt").write_text(
        "Find the missing git changes.", encoding="utf-8"
    )
    (evolved_trial / "agent" / "stdout.txt").write_text("fixed it\n", encoding="utf-8")
    (evolved_trial / "verifier" / "reward.txt").write_text("1.0\n", encoding="utf-8")

    commands: list[list[str]] = []
    command_cwds: list[Path | None] = []

    def fake_run_command(command, cwd=None):
        commands.append(command)
        command_cwds.append(cwd)
        if "terminal-bench-agent-system-job" in command:
            job_path = Path(command[command.index("--output") + 1])
            job_path.parent.mkdir(parents=True, exist_ok=True)
            job_path.write_text(
                json.dumps(
                    {
                        "dataset": {"artifact_id": "dataset-r0"},
                        "job": {"input_artifact_ids": ["dataset-r0"]},
                    }
                ),
                encoding="utf-8",
            )
        return {}

    def fake_worker_runner(*, db_path, artifact_root):
        return [
            {
                "artifact_id": "art-agent",
                "type": "agent_system",
                "uri": (tmp_path / "AGENTS.md").resolve().as_uri(),
                "manifest": {"method": "agent_system_reflector"},
            }
        ]

    (tmp_path / "AGENTS.md").write_text("Inspect all branches before editing.\n", encoding="utf-8")
    summary = run_per_task_evolution(
        task_root=tmp_path / "tasks",
        task_ids=["fix-git"],
        run_root=tmp_path / "run",
        baseline_root=baseline.parent,
        model="gpt-5.5",
        reflector_model="gpt-5.5",
        rounds=1,
        env_json={},
        verifier_env={},
        command_runner=fake_run_command,
        worker_runner=fake_worker_runner,
        evolved_trial_locator=lambda task_id, round_number, run_root: evolved_trial,
    )

    assert summary["tasks"][0]["task_id"] == "fix-git"
    assert summary["tasks"][0]["baseline_reward"] == 0.0
    assert summary["tasks"][0]["rounds"][0]["reward"] == 1.0
    assert summary["tasks"][0]["rounds"][0]["transition"] == "fail_to_pass"
    assert any("agent_system_path=" in part for command in commands for part in command)
    job_commands = [
        command for command in commands if "terminal-bench-agent-system-job" in command
    ]
    assert len(job_commands) == 1
    job_command = job_commands[0]
    assert job_command[job_command.index("--reflector-provider") + 1] == "codex_cli"
    harbor_commands = [command for command in commands if command[:2] == ["harbor", "run"]]
    assert len(harbor_commands) == 1
    harbor_command = harbor_commands[0]
    assert harbor_command[harbor_command.index("--jobs-dir") + 1] == str(
        tmp_path / "run" / "tasks" / "fix-git" / "r1" / "harbor_jobs"
    )
    assert command_cwds[commands.index(harbor_command)] == Path(
        "/root/EvoLabCore-terminal-bench-task-package"
    )


def test_gepa_orchestration_evaluates_candidate_pool_and_selects_best(
    tmp_path: Path,
):
    baseline = tmp_path / "baseline" / "filter-js-from-html__base"
    (baseline / "agent").mkdir(parents=True)
    (baseline / "verifier").mkdir()
    (baseline / "result.json").write_text(
        json.dumps(
            {
                "trial_name": "filter-js-from-html__base",
                "task_name": "filter-js-from-html",
                "status": "COMPLETED",
                "verifier_result": {"rewards": {"reward": 0.0}},
            }
        ),
        encoding="utf-8",
    )
    (baseline / "agent" / "instruction.txt").write_text("Create /app/filter.py.", encoding="utf-8")
    (baseline / "agent" / "codex.txt").write_text(
        "Verifier failed: clean HTML changed and encoded javascript remained.",
        encoding="utf-8",
    )
    (baseline / "verifier" / "reward.txt").write_text("0.0\n", encoding="utf-8")

    candidate_trials = [
        tmp_path / "run" / "candidate-1" / "filter-js-from-html__c1",
        tmp_path / "run" / "candidate-2" / "filter-js-from-html__c2",
    ]
    for index, (trial, reward) in enumerate(zip(candidate_trials, [0.0, 1.0]), start=1):
        (trial / "agent").mkdir(parents=True)
        (trial / "verifier").mkdir()
        (trial / "result.json").write_text(
            json.dumps(
                {
                    "trial_name": f"filter-js-from-html__c{index}",
                    "task_name": "filter-js-from-html",
                    "status": "COMPLETED",
                    "verifier_result": {"rewards": {"reward": reward}},
                }
            ),
            encoding="utf-8",
        )
        (trial / "agent" / "instruction.txt").write_text(
            "Create /app/filter.py.", encoding="utf-8"
        )
        (trial / "agent" / "stdout.txt").write_text(
            f"candidate {index} completed\n", encoding="utf-8"
        )
        (trial / "verifier" / "reward.txt").write_text(f"{reward}\n", encoding="utf-8")

    commands: list[list[str]] = []

    def fake_run_command(command, cwd=None):
        del cwd
        commands.append(command)
        if "terminal-bench-agent-system-job" in command:
            job_path = Path(command[command.index("--output") + 1])
            job_path.parent.mkdir(parents=True, exist_ok=True)
            job_path.write_text(
                json.dumps(
                    {
                        "dataset": {"artifact_id": "dataset-r0"},
                        "job": {"input_artifact_ids": ["dataset-r0"]},
                    }
                ),
                encoding="utf-8",
            )
        return {}

    def fake_worker_runner(*, db_path, artifact_root):
        del db_path, artifact_root
        agent_paths = [tmp_path / "AGENTS-c1.md", tmp_path / "AGENTS-c2.md"]
        for index, path in enumerate(agent_paths, start=1):
            path.write_text(f"# Candidate {index}\n", encoding="utf-8")
        return [
            {
                "artifact_id": "art-agent-c1",
                "type": "agent_system",
                "uri": agent_paths[0].resolve().as_uri(),
                "manifest": {
                    "method": "agent_system_gepa_reflector",
                    "candidate_index": 1,
                    "candidate_strategy": "preservation_gate",
                },
            },
            {
                "artifact_id": "art-agent-c2",
                "type": "agent_system",
                "uri": agent_paths[1].resolve().as_uri(),
                "manifest": {
                    "method": "agent_system_gepa_reflector",
                    "candidate_index": 2,
                    "candidate_strategy": "xss_corpus",
                },
            },
        ]

    trial_iter = iter(candidate_trials)

    summary = run_per_task_evolution(
        task_root=tmp_path / "tasks",
        task_ids=["filter-js-from-html"],
        run_root=tmp_path / "run",
        baseline_root=baseline.parent,
        model="gpt-5.5",
        reflector_model="gpt-5.5",
        rounds=1,
        env_json={},
        verifier_env={},
        agent_system_method="agent_system_gepa_reflector",
        gepa_candidate_count=2,
        command_runner=fake_run_command,
        worker_runner=fake_worker_runner,
        evolved_trial_locator=lambda task_id, round_number, run_root: next(trial_iter),
    )

    job_command = next(
        command for command in commands if "terminal-bench-agent-system-job" in command
    )
    assert job_command[job_command.index("--method") + 1] == "agent_system_gepa_reflector"
    assert job_command[job_command.index("--candidate-count") + 1] == "2"

    harbor_commands = [command for command in commands if command[:2] == ["harbor", "run"]]
    assert len(harbor_commands) == 2
    assert harbor_commands[0][harbor_commands[0].index("--job-name") + 1].endswith("-c1")
    assert harbor_commands[1][harbor_commands[1].index("--job-name") + 1].endswith("-c2")
    assert "agent_system_path=" + str(tmp_path / "AGENTS-c1.md") in harbor_commands[0]
    assert "agent_system_path=" + str(tmp_path / "AGENTS-c2.md") in harbor_commands[1]

    round_summary = summary["tasks"][0]["rounds"][0]
    assert round_summary["reward"] == 1.0
    assert round_summary["transition"] == "fail_to_pass"
    assert round_summary["artifact"]["artifact_id"] == "art-agent-c2"
    assert round_summary["candidate_trials"] == [
        {
            "candidate_index": 1,
            "artifact_id": "art-agent-c1",
            "strategy": "preservation_gate",
            "reward": 0.0,
            "trial_dir": str(candidate_trials[0]),
        },
        {
            "candidate_index": 2,
            "artifact_id": "art-agent-c2",
            "strategy": "xss_corpus",
            "reward": 1.0,
            "trial_dir": str(candidate_trials[1]),
        },
    ]


def test_gepa_orchestration_feeds_candidate_feedback_into_next_generation(
    tmp_path: Path,
):
    baseline = tmp_path / "baseline" / "filter-js-from-html__base"
    (baseline / "agent").mkdir(parents=True)
    (baseline / "verifier").mkdir()
    (baseline / "result.json").write_text(
        json.dumps(
            {
                "trial_name": "filter-js-from-html__base",
                "task_name": "filter-js-from-html",
                "status": "COMPLETED",
                "verifier_result": {"rewards": {"reward": 0.0}},
            }
        ),
        encoding="utf-8",
    )
    (baseline / "agent" / "instruction.txt").write_text("Create /app/filter.py.", encoding="utf-8")
    (baseline / "verifier" / "reward.txt").write_text("0.0\n", encoding="utf-8")

    candidate_trials = [
        tmp_path / "run" / "candidate-1" / "filter-js-from-html__g1c1",
        tmp_path / "run" / "candidate-2" / "filter-js-from-html__g1c2",
        tmp_path / "run" / "candidate-3" / "filter-js-from-html__g2c1",
        tmp_path / "run" / "candidate-4" / "filter-js-from-html__g2c2",
    ]
    rewards = [0.0, 0.0, 0.0, 1.0]
    for index, (trial, reward) in enumerate(zip(candidate_trials, rewards), start=1):
        (trial / "agent").mkdir(parents=True)
        (trial / "verifier").mkdir()
        (trial / "result.json").write_text(
            json.dumps(
                {
                    "trial_name": trial.name,
                    "task_name": "filter-js-from-html",
                    "status": "COMPLETED",
                    "verifier_result": {"rewards": {"reward": reward}},
                }
            ),
            encoding="utf-8",
        )
        (trial / "agent" / "stdout.txt").write_text(
            f"candidate {index} completed\n", encoding="utf-8"
        )
        (trial / "verifier" / "reward.txt").write_text(f"{reward}\n", encoding="utf-8")

    commands: list[list[str]] = []
    job_counter = 0

    def fake_run_command(command, cwd=None):
        del cwd
        nonlocal job_counter
        commands.append(command)
        if "terminal-bench-agent-system-job" in command:
            job_counter += 1
            dataset_id = f"dataset-g{job_counter}"
            prior_ids = [
                command[index + 1]
                for index, part in enumerate(command)
                if part == "--dataset-artifact-id"
            ]
            job_path = Path(command[command.index("--output") + 1])
            job_path.parent.mkdir(parents=True, exist_ok=True)
            job_path.write_text(
                json.dumps(
                    {
                        "dataset": {"artifact_id": dataset_id},
                        "job": {"input_artifact_ids": [*prior_ids, dataset_id]},
                    }
                ),
                encoding="utf-8",
            )
        return {}

    worker_call = 0

    def fake_worker_runner(*, db_path, artifact_root):
        del db_path, artifact_root
        nonlocal worker_call
        worker_call += 1
        artifacts = []
        for candidate_index, strategy in enumerate(
            ["failure_targeted", "verification_gate"], start=1
        ):
            path = tmp_path / f"AGENTS-g{worker_call}-c{candidate_index}.md"
            path.write_text(f"# Candidate g{worker_call} c{candidate_index}\n", encoding="utf-8")
            artifacts.append(
                {
                    "artifact_id": f"art-agent-g{worker_call}-c{candidate_index}",
                    "type": "agent_system",
                    "uri": path.resolve().as_uri(),
                    "manifest": {
                        "method": "agent_system_gepa_reflector",
                        "candidate_index": candidate_index,
                        "candidate_strategy": strategy,
                    },
                }
            )
        return artifacts

    trial_iter = iter(candidate_trials)

    summary = run_per_task_evolution(
        task_root=tmp_path / "tasks",
        task_ids=["filter-js-from-html"],
        run_root=tmp_path / "run",
        baseline_root=baseline.parent,
        model="gpt-5.5",
        reflector_model="gpt-5.5",
        rounds=1,
        env_json={},
        verifier_env={},
        agent_system_method="agent_system_gepa_reflector",
        gepa_candidate_count=2,
        gepa_generations=2,
        command_runner=fake_run_command,
        worker_runner=fake_worker_runner,
        evolved_trial_locator=lambda task_id, round_number, run_root: next(trial_iter),
    )

    job_commands = [
        command for command in commands if "terminal-bench-agent-system-job" in command
    ]
    assert len(job_commands) == 2
    first_job, second_job = job_commands
    assert first_job[first_job.index("--input") + 1] == str(baseline)
    assert "--dataset-artifact-id" not in first_job

    second_inputs = [
        second_job[index + 1] for index, part in enumerate(second_job) if part == "--input"
    ]
    assert second_inputs == [str(candidate_trials[0]), str(candidate_trials[1])]
    second_dataset_ids = [
        second_job[index + 1]
        for index, part in enumerate(second_job)
        if part == "--dataset-artifact-id"
    ]
    assert second_dataset_ids == ["dataset-g1"]

    harbor_commands = [command for command in commands if command[:2] == ["harbor", "run"]]
    assert len(harbor_commands) == 4
    assert harbor_commands[0][harbor_commands[0].index("--job-name") + 1].endswith("-g1-c1")
    assert harbor_commands[1][harbor_commands[1].index("--job-name") + 1].endswith("-g1-c2")
    assert harbor_commands[2][harbor_commands[2].index("--job-name") + 1].endswith("-g2-c1")
    assert harbor_commands[3][harbor_commands[3].index("--job-name") + 1].endswith("-g2-c2")

    round_summary = summary["tasks"][0]["rounds"][0]
    assert round_summary["reward"] == 1.0
    assert round_summary["transition"] == "fail_to_pass"
    assert round_summary["artifact"]["artifact_id"] == "art-agent-g2-c2"
    assert round_summary["gepa_generations"] == 2
    assert round_summary["dataset_artifact_ids"] == ["dataset-g1", "dataset-g2"]
    assert [trial["reward"] for trial in round_summary["candidate_trials"]] == rewards
    assert [trial["generation"] for trial in round_summary["candidate_trials"]] == [1, 1, 2, 2]


def test_per_task_evolution_passes_reflector_timeout_to_agent_system_job(
    tmp_path: Path,
):
    baseline = tmp_path / "baseline" / "filter-js-from-html__base"
    (baseline / "agent").mkdir(parents=True)
    (baseline / "verifier").mkdir()
    (baseline / "result.json").write_text(
        json.dumps(
            {
                "trial_name": "filter-js-from-html__base",
                "task_name": "filter-js-from-html",
                "verifier_result": {"rewards": {"reward": 0.0}},
            }
        ),
        encoding="utf-8",
    )
    (baseline / "verifier" / "reward.txt").write_text("0.0\n", encoding="utf-8")
    evolved_trial = tmp_path / "run" / "candidate" / "filter-js-from-html__c1"
    (evolved_trial / "agent").mkdir(parents=True)
    (evolved_trial / "verifier").mkdir()
    (evolved_trial / "result.json").write_text(
        json.dumps(
            {
                "trial_name": "filter-js-from-html__c1",
                "task_name": "filter-js-from-html",
                "verifier_result": {"rewards": {"reward": 0.0}},
            }
        ),
        encoding="utf-8",
    )
    (evolved_trial / "verifier" / "reward.txt").write_text("0.0\n", encoding="utf-8")

    commands: list[list[str]] = []

    def fake_run_command(command, cwd=None):
        del cwd
        commands.append(command)
        if "terminal-bench-agent-system-job" in command:
            job_path = Path(command[command.index("--output") + 1])
            job_path.parent.mkdir(parents=True, exist_ok=True)
            job_path.write_text(
                json.dumps(
                    {
                        "dataset": {"artifact_id": "dataset-r0"},
                        "job": {"input_artifact_ids": ["dataset-r0"]},
                    }
                ),
                encoding="utf-8",
            )
        return {}

    def fake_worker_runner(*, db_path, artifact_root):
        del db_path, artifact_root
        path = tmp_path / "AGENTS.md"
        path.write_text("# Candidate\n", encoding="utf-8")
        return [
            {
                "artifact_id": "art-agent",
                "type": "agent_system",
                "uri": path.resolve().as_uri(),
                "manifest": {"method": "agent_system_gepa_reflector"},
            }
        ]

    run_per_task_evolution(
        task_root=tmp_path / "tasks",
        task_ids=["filter-js-from-html"],
        run_root=tmp_path / "run",
        baseline_root=baseline.parent,
        model="gpt-5.5",
        reflector_model="gpt-5.5",
        reflector_timeout_seconds=180.0,
        rounds=1,
        env_json={},
        verifier_env={},
        agent_system_method="agent_system_gepa_reflector",
        gepa_candidate_count=1,
        command_runner=fake_run_command,
        worker_runner=fake_worker_runner,
        evolved_trial_locator=lambda task_id, round_number, run_root: evolved_trial,
    )

    job_command = next(
        command for command in commands if "terminal-bench-agent-system-job" in command
    )
    assert job_command[job_command.index("--reflector-timeout-seconds") + 1] == "180.0"


def test_find_baseline_trial_accepts_concrete_trial_dir(tmp_path: Path):
    baseline = tmp_path / "fix-git__abc"
    baseline.mkdir()
    (baseline / "result.json").write_text("{}", encoding="utf-8")

    assert _find_baseline_trial(baseline, "fix-git") == baseline


def test_trial_reward_falls_back_to_result_json_without_transcript(tmp_path: Path):
    trial = tmp_path / "fix-git__abc"
    trial.mkdir()
    (trial / "result.json").write_text(
        json.dumps(
            {
                "trial_name": "fix-git__abc",
                "task_name": "fix-git",
                "verifier_result": {"rewards": {"reward": 0.75}},
            }
        ),
        encoding="utf-8",
    )

    assert _trial_reward(trial) == 0.75


def test_trial_reward_falls_back_to_reward_txt_without_transcript(tmp_path: Path):
    trial = tmp_path / "fix-git__abc"
    (trial / "verifier").mkdir(parents=True)
    (trial / "result.json").write_text(
        json.dumps(
            {
                "trial_name": "fix-git__abc",
                "task_name": "fix-git",
            }
        ),
        encoding="utf-8",
    )
    (trial / "verifier" / "reward.txt").write_text("0.5\n", encoding="utf-8")

    assert _trial_reward(trial) == 0.5


def test_run_worker_once_local_claims_with_long_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    seen: dict[str, object] = {}

    class FakeStore:
        def __init__(self, *, db_path, artifact_root):
            seen["db_path"] = db_path
            seen["artifact_root"] = artifact_root

        def initialize(self):
            seen["initialized"] = True

        def claim_job(self, request):
            seen["lease_seconds"] = request.lease_seconds
            return type(
                "ClaimResponse",
                (),
                {
                    "job": type(
                        "Job",
                        (),
                        {
                            "job_id": "job-1",
                            "lease_id": "lease-1",
                            "method": "agent_system_reflector",
                        },
                    )()
                },
            )()

        def heartbeat_job(self, job_id, request):
            seen.setdefault("heartbeats", []).append((job_id, request.progress, request.message))
            return {"job_id": job_id, "state": "running"}

        def complete_job(self, job_id, request):
            seen["completed_job_id"] = job_id
            return {"artifact_ids": ["art-1"]}

        def fail_job(self, job_id, request):
            seen["failed_job_id"] = job_id
            return {"job_id": job_id, "state": "failed"}

    monkeypatch.setattr(per_task_module, "EvolutionStore", FakeStore)
    monkeypatch.setattr(
        per_task_module,
        "run_method",
        lambda job, *, artifact_root: [
            ArtifactRegisterRequest(
                type=ArtifactType.AGENT_SYSTEM,
                name="agent-system",
                uri=(tmp_path / "AGENTS.md").resolve().as_uri(),
                manifest={"method": "agent_system_reflector"},
            )
        ],
    )

    completed = _run_worker_once_local(
        db_path=tmp_path / "polar.db",
        artifact_root=tmp_path / "artifacts",
    )

    assert seen["initialized"] is True
    assert seen["lease_seconds"] == per_task_module.LOCAL_WORKER_LEASE_SECONDS
    assert seen["heartbeats"] == [("job-1", 0.0, "claimed"), ("job-1", 1.0, "completed")]
    assert completed == [
        {
            "artifact_id": "art-1",
            "type": "agent_system",
            "uri": (tmp_path / "AGENTS.md").resolve().as_uri(),
            "manifest": {"method": "agent_system_reflector"},
        }
    ]


def test_parse_key_value_entries_rejects_invalid_entry():
    with pytest.raises(ValueError, match="expected KEY=VALUE entry"):
        _parse_key_value_entries(["BROKEN"])


def test_terminal_bench_per_task_evolution_cli_live_mode_requires_agent_system_only(
    tmp_path: Path,
):
    output = tmp_path / "summary.json"

    with pytest.raises(
        ValueError,
        match="live per-task evolution currently supports only agent_system",
    ):
        main(
            [
                "terminal-bench-per-task-evolution",
                "--task-root",
                "/root/datasets/terminal-bench-2-1/tasks",
                "--task-id",
                "fix-git",
                "--run-root",
                str(tmp_path / "run"),
                "--baseline-root",
                str(tmp_path / "baseline"),
                "--model",
                "gpt-5.5",
                "--reflector-model",
                "gpt-5.5",
                "--artifact-type",
                "memory",
                "--output",
                str(output),
            ]
        )


def test_terminal_bench_per_task_evolution_cli_live_mode_parses_env_and_writes_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    output = tmp_path / "summary.json"
    captured: dict[str, object] = {}

    def fake_run_per_task_evolution(**kwargs):
        captured.update(kwargs)
        return {"mode": "live", "tasks": []}

    monkeypatch.setattr(cli_module, "run_per_task_evolution", fake_run_per_task_evolution)

    exit_code = main(
        [
            "terminal-bench-per-task-evolution",
            "--task-root",
            "/root/datasets/terminal-bench-2-1/tasks",
            "--task-id",
            "fix-git",
            "--run-root",
            str(tmp_path / "run"),
            "--baseline-root",
            str(tmp_path / "baseline"),
            "--terminal-bench-package-root",
            "/tmp/terminal-bench-package",
            "--model",
            "gpt-5.5",
            "--reflector-model",
            "gpt-5.5",
            "--reflector-provider",
            "openai_chat",
            "--codex-home",
            "/tmp/codex-home",
            "--gepa-generations",
            "2",
            "--env-json",
            '{"NO_PROXY":"localhost"}',
            "--verifier-env",
            "UV_NO_INDEX=1",
            "--output",
            str(output),
        ]
    )

    assert exit_code == 0
    assert captured["baseline_root"] == tmp_path / "baseline"
    assert captured["terminal_bench_package_root"] == Path("/tmp/terminal-bench-package")
    assert captured["reflector_provider"] == "openai_chat"
    assert captured["codex_home"] == "/tmp/codex-home"
    assert captured["gepa_generations"] == 2
    assert captured["env_json"] == {"NO_PROXY": "localhost"}
    assert captured["verifier_env"] == {"UV_NO_INDEX": "1"}
    assert json.loads(output.read_text(encoding="utf-8")) == {"mode": "live", "tasks": []}
