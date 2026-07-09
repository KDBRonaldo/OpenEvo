from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

import openevo.evolution.cli as cli_module
import openevo.evolution.terminal_bench_local_parametric as local_parametric
from openevo.evolution.cli import main
from openevo.evolution.terminal_bench_local_parametric import (
    DEFAULT_LOCAL_PARAMETRIC_DISABLED_ARTIFACTS,
    build_evolab_harbor_env,
    build_local_harbor_command,
    build_vllm_command,
    run_local_parametric_memory_eval,
    run_local_parametric_memory_eval_dry_run,
)


def test_build_local_harbor_command_uses_evolab_mode_and_attempts() -> None:
    command = build_local_harbor_command(
        job_name="query-optimize-baseline",
        task_root=Path("/root/datasets/terminal-bench-2-1/tasks"),
        task_id="query-optimize",
        jobs_dir=Path("/tmp/tb21-local/baseline/harbor_jobs"),
        model="Qwen/Qwen3.6-35B-A3B",
        verifier_env={"UV_NO_INDEX": "1"},
        n_attempts=5,
        n_concurrent=1,
    )

    assert command[:2] == ["harbor", "run"]
    assert command[command.index("--include-task-name") + 1] == "query-optimize"
    assert command[command.index("--jobs-dir") + 1] == (
        "/tmp/tb21-local/baseline/harbor_jobs"
    )
    assert command[command.index("--model") + 1] == "Qwen/Qwen3.6-35B-A3B"
    assert command[command.index("--n-attempts") + 1] == "5"
    assert "mode=evolab" in command
    assert not any(part.startswith("env_json=") for part in command)
    assert "mode=codex_subscription" not in command
    assert "UV_NO_INDEX=1" in command


def test_build_local_harbor_command_includes_timeout_multipliers() -> None:
    command = build_local_harbor_command(
        job_name="train-fasttext-parametric",
        task_root=Path("/root/datasets/terminal-bench-2-1/tasks"),
        task_id="train-fasttext",
        jobs_dir=Path("/tmp/tb21-local/parametric/harbor_jobs"),
        model="tb-parametric-memory",
        verifier_env={},
        timeout_multiplier=2.0,
        agent_timeout_multiplier=3.5,
    )

    assert command[command.index("--timeout-multiplier") + 1] == "2.0"
    assert command[command.index("--agent-timeout-multiplier") + 1] == "3.5"


def test_build_evolab_harbor_env_sets_openai_chat_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("OPENAI_API_KEY", "host-secret")

    env = build_evolab_harbor_env(
        base_env={"PATH": "/usr/bin"},
        server_url="http://127.0.0.1:8000/v1",
        model="tb-parametric-memory",
        tool_result_prompt_max_chars=2048,
    )

    assert env["PATH"] == "/usr/bin"
    assert env["EVOLAB_TB_LLM_API"] == "openai-chat-completions"
    assert env["EVOLAB_TB_MODEL"] == "tb-parametric-memory"
    assert env["OPENAI_BASE_URL"] == "http://127.0.0.1:8000/v1"
    assert env["AIGOCODE_GPT_BASE_URL"] == "http://127.0.0.1:8000/v1"
    assert env["OPENAI_API_KEY"] == "dummy-local-key"
    assert env["EVOLAB_TB_MODE"] == "direct_solver"
    assert env["EVOLAB_TB_CONTEXT_WINDOW_TOKENS"] == "16320"
    assert env["EVOLAB_TB_CONTEXT_RESERVE_TOKENS"] == "1536"
    assert env["EVOLAB_TB_MAX_OUTPUT_TOKENS"] == "1536"
    assert env["EVOLAB_TB_LLM_TEMPERATURE"] == "0.0"
    assert env["EVOLAB_TB_TOOL_RESULT_PROMPT_MAX_CHARS"] == "2048"


def test_build_evolab_harbor_env_clamps_output_tokens_to_context_reserve() -> None:
    env = build_evolab_harbor_env(
        base_env={},
        server_url="http://127.0.0.1:8000/v1",
        model="tb-parametric-memory",
        max_output_tokens=4096,
        context_window_tokens=32768,
        context_reserve_tokens=1024,
    )

    assert env["EVOLAB_TB_CONTEXT_WINDOW_TOKENS"] == "32704"
    assert env["EVOLAB_TB_CONTEXT_RESERVE_TOKENS"] == "1024"
    assert env["EVOLAB_TB_MAX_OUTPUT_TOKENS"] == "1024"


def test_build_evolab_harbor_env_leaves_context_safety_margin() -> None:
    env = build_evolab_harbor_env(
        base_env={},
        server_url="http://127.0.0.1:8000/v1",
        model="tb-parametric-memory",
        context_window_tokens=8192,
        context_reserve_tokens=1800,
        max_output_tokens=1800,
    )

    assert env["EVOLAB_TB_CONTEXT_WINDOW_TOKENS"] == "8128"
    assert env["EVOLAB_TB_CONTEXT_RESERVE_TOKENS"] == "1800"
    assert env["EVOLAB_TB_MAX_OUTPUT_TOKENS"] == "1800"


def test_build_evolab_harbor_env_allows_explicit_solver_temperature() -> None:
    env = build_evolab_harbor_env(
        base_env={},
        server_url="http://127.0.0.1:8000/v1",
        model="tb-parametric-memory",
        solver_temperature=0.2,
    )

    assert env["EVOLAB_TB_LLM_TEMPERATURE"] == "0.2"


def test_build_evolab_harbor_env_empty_base_does_not_inherit_host_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENEVO_SENTINEL_HOST_ENV", "host-value")

    env = build_evolab_harbor_env(
        base_env={},
        server_url="http://127.0.0.1:8000/v1",
        model="tb-parametric-memory",
    )

    assert "OPENEVO_SENTINEL_HOST_ENV" not in env
    assert env["EVOLAB_TB_LLM_API"] == "openai-chat-completions"


def test_build_evolab_harbor_env_applies_agent_env_knobs() -> None:
    env = build_evolab_harbor_env(
        base_env={},
        server_url="http://127.0.0.1:8000/v1",
        model="tb-parametric-memory",
        agent_env={
            "EVOLAB_TB_REQUIRE_SUCCESSFUL_COLLECT": "1",
            "EVOLAB_TB_DIRECT_SOLVER_COMPLETION_GUARD": "successful_collect",
        },
    )

    assert env["EVOLAB_TB_REQUIRE_SUCCESSFUL_COLLECT"] == "1"
    assert env["EVOLAB_TB_DIRECT_SOLVER_COMPLETION_GUARD"] == "successful_collect"


def test_build_evolab_harbor_env_sets_internal_budget_knobs() -> None:
    env = build_evolab_harbor_env(
        base_env={},
        server_url="http://127.0.0.1:8000/v1",
        model="tb-parametric-memory",
        exec_timeout_cap_seconds=1800,
        exec_timeout_min_seconds=1800,
        max_subagent_runtime_seconds=2400,
    )

    assert env["EVOLAB_TB_EXEC_TIMEOUT_CAP_SECONDS"] == "1800"
    assert env["EVOLAB_TB_EXEC_TIMEOUT_MIN_SECONDS"] == "1800"
    assert env["EVOLAB_TB_MAX_SUBAGENT_RUNTIME_SECONDS"] == "2400"


def test_build_evolab_harbor_env_applies_artifact_path_guard() -> None:
    env = build_evolab_harbor_env(
        base_env={},
        server_url="http://127.0.0.1:8000/v1",
        model="tb-parametric-memory",
        artifact_path_guard="repair",
        required_artifact_paths=["/app/out.txt", "/app/model.bin"],
    )

    assert env["EVOLAB_TB_ARTIFACT_PATH_GUARD"] == "repair"
    assert json.loads(env["EVOLAB_TB_REQUIRED_ARTIFACT_PATHS"]) == [
        "/app/out.txt",
        "/app/model.bin",
    ]


def test_build_evolab_harbor_env_rejects_unsafe_agent_env() -> None:
    with pytest.raises(ValueError, match="EVOLAB_TB_"):
        build_evolab_harbor_env(
            base_env={},
            server_url="http://127.0.0.1:8000/v1",
            model="tb-parametric-memory",
            agent_env={"OPENAI_API_KEY": "secret"},
        )

    with pytest.raises(ValueError, match="controlled"):
        build_evolab_harbor_env(
            base_env={},
            server_url="http://127.0.0.1:8000/v1",
            model="tb-parametric-memory",
            agent_env={"EVOLAB_TB_MODEL": "other-model"},
        )


def test_build_vllm_command_baseline_and_lora() -> None:
    baseline = build_vllm_command(
        model="Qwen/Qwen3.6-35B-A3B",
        served_model_name="Qwen/Qwen3.6-35B-A3B",
        port=8000,
        tensor_parallel_size=4,
        gpu_memory_utilization=0.75,
        max_model_len=16384,
        vllm_executable="/root/evolab-vllm/bin/vllm",
    )
    assert baseline.env["CUDA_VISIBLE_DEVICES"] == "1,2,3,4"
    assert baseline.command[:3] == [
        "/root/evolab-vllm/bin/vllm",
        "serve",
        "Qwen/Qwen3.6-35B-A3B",
    ]
    assert baseline.command[baseline.command.index("--generation-config") + 1] == "vllm"
    assert "--enable-lora" not in baseline.command

    adapter = build_vllm_command(
        model="Qwen/Qwen3.6-35B-A3B",
        served_model_name="Qwen/Qwen3.6-35B-A3B",
        port=8000,
        tensor_parallel_size=4,
        gpu_memory_utilization=0.75,
        max_model_len=16384,
        vllm_executable="/root/evolab-vllm/bin/vllm",
        adapter_id="tb-parametric-memory",
        adapter_path=Path("/tmp/adapter"),
    )
    assert "--enable-lora" in adapter.command
    assert "--lora-modules" in adapter.command
    assert "tb-parametric-memory=/tmp/adapter" in adapter.command
    assert adapter.command[adapter.command.index("--generation-config") + 1] == "vllm"


def test_build_vllm_command_prefixes_absolute_executable_bin_to_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PATH", "/usr/bin:/bin")

    spec = build_vllm_command(vllm_executable="/root/evolab-vllm/bin/vllm")

    assert spec.env["PATH"].split(os.pathsep)[:3] == [
        "/root/evolab-vllm/bin",
        "/usr/bin",
        "/bin",
    ]


def test_build_vllm_command_derives_tensor_parallel_size_from_gpus() -> None:
    spec = build_vllm_command(
        model="Qwen/Qwen3.6-35B-A3B",
        served_model_name="Qwen/Qwen3.6-35B-A3B",
        gpus=["0", "1"],
    )

    assert spec.env["CUDA_VISIBLE_DEVICES"] == "0,1"
    assert spec.command[spec.command.index("--tensor-parallel-size") + 1] == "2"


def test_build_vllm_command_rejects_tensor_parallel_size_larger_than_gpus() -> None:
    with pytest.raises(ValueError, match="tensor_parallel_size"):
        build_vllm_command(
            model="Qwen/Qwen3.6-35B-A3B",
            served_model_name="Qwen/Qwen3.6-35B-A3B",
            gpus=["0", "1"],
            tensor_parallel_size=4,
        )


def test_build_vllm_command_rejects_empty_gpu_list() -> None:
    with pytest.raises(ValueError, match="at least one GPU"):
        build_vllm_command(gpus=[])


def test_build_vllm_command_rejects_lora_served_model_name_collision() -> None:
    with pytest.raises(ValueError, match="served_model_name"):
        build_vllm_command(
            model="Qwen/Qwen3.6-35B-A3B",
            served_model_name="tb-parametric-memory",
            adapter_id="tb-parametric-memory",
            adapter_path=Path("/tmp/adapter"),
        )


def test_local_parametric_dry_run_reports_matrix_and_disabled_artifacts(
    tmp_path: Path,
) -> None:
    payload = run_local_parametric_memory_eval_dry_run(
        task_root=Path("/root/datasets/terminal-bench-2-1/tasks"),
        task_ids=["train-fasttext", "query-optimize", "make-mips-interpreter"],
        run_root=tmp_path / "run",
        model="Qwen/Qwen3.6-35B-A3B",
        adapter_path=Path("/tmp/adapter"),
        adapter_id="tb-parametric-memory",
        server_url="http://127.0.0.1:8000/v1",
        n_attempts=5,
        timeout_multiplier=2.0,
        agent_timeout_multiplier=3.5,
        exec_timeout_cap_seconds=1800,
        exec_timeout_min_seconds=1800,
        max_subagent_runtime_seconds=2400,
        tool_result_prompt_max_chars=2048,
        agent_env={"EVOLAB_TB_REQUIRE_SUCCESSFUL_COLLECT": "1"},
        manage_server=True,
    )

    assert payload["dry_run"] is True
    assert payload["benchmark"] == "terminal-bench-2.1"
    assert payload["auth_mode"] == "local"
    assert payload["enabled_artifacts"] == ["parametric_memory"]
    assert payload["disabled_artifacts"] == DEFAULT_LOCAL_PARAMETRIC_DISABLED_ARTIFACTS
    assert [condition["name"] for condition in payload["conditions"]] == [
        "baseline",
        "parametric_memory",
    ]
    assert payload["conditions"][0]["model"] == "Qwen/Qwen3.6-35B-A3B"
    assert payload["conditions"][1]["model"] == "tb-parametric-memory"
    assert payload["requested_max_output_tokens"] == 4096
    assert payload["max_output_tokens"] == 1536
    assert payload["context_window_tokens"] == 16384
    assert payload["context_reserve_tokens"] == 1536
    assert payload["solver_temperature"] == 0.0
    assert payload["vllm_generation_config"] == "vllm"
    assert payload["tool_result_prompt_max_chars"] == 2048
    assert payload["timeout_multiplier"] == 2.0
    assert payload["agent_timeout_multiplier"] == 3.5
    assert payload["exec_timeout_cap_seconds"] == 1800
    assert payload["exec_timeout_min_seconds"] == 1800
    assert payload["max_subagent_runtime_seconds"] == 2400
    assert payload["agent_env"] == {"EVOLAB_TB_REQUIRE_SUCCESSFUL_COLLECT": "1"}
    assert payload["artifact_path_guard"] == "off"
    assert payload["required_artifact_paths"] == []


def test_local_parametric_dry_run_records_artifact_path_guard(
    tmp_path: Path,
) -> None:
    payload = run_local_parametric_memory_eval_dry_run(
        task_root=Path("/root/datasets/terminal-bench-2-1/tasks"),
        task_ids=["gcode"],
        run_root=tmp_path / "run",
        model="Qwen/Qwen3.6-35B-A3B",
        adapter_path=Path("/tmp/adapter"),
        adapter_id="tb-parametric-memory",
        server_url="http://127.0.0.1:8000/v1",
        n_attempts=1,
        manage_server=False,
        artifact_path_guard="audit",
        required_artifact_paths=["/app/out.txt"],
    )

    assert payload["artifact_path_guard"] == "audit"
    assert payload["required_artifact_paths"] == ["/app/out.txt"]


def test_run_local_parametric_memory_eval_compares_baseline_and_adapter(
    tmp_path: Path,
) -> None:
    commands: list[list[str]] = []
    envs: list[dict[str, str]] = []
    cwds: list[Path | None] = []
    package_root = tmp_path / "terminal-bench-package"

    def fake_command_runner(command, *, cwd=None, env=None):
        commands.append(command)
        envs.append(dict(env or {}))
        cwds.append(cwd)
        jobs_dir = Path(command[command.index("--jobs-dir") + 1])
        job_name = command[command.index("--job-name") + 1]
        model = command[command.index("--model") + 1]
        task_id = command[command.index("--include-task-name") + 1]
        reward = 1.0 if model == "tb-parametric-memory" else 0.0
        for attempt_index in range(1, 3):
            trial = jobs_dir / job_name / f"{task_id}__attempt{attempt_index}"
            (trial / "agent").mkdir(parents=True)
            (trial / "verifier").mkdir()
            (trial / "result.json").write_text(
                json.dumps(
                    {
                        "trial_name": trial.name,
                        "task_name": task_id,
                        "started_at": f"2026-07-02T00:00:0{attempt_index}Z",
                        "status": "COMPLETED",
                        "verifier_result": {"rewards": {"reward": reward}},
                        "agent_result": {
                            "metadata": {
                                "terminal_bench_harbor_agent": {
                                    "model_name": model,
                                    "task_id": task_id,
                                }
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            (trial / "agent" / "stdout.txt").write_text("done\n", encoding="utf-8")
            (trial / "verifier" / "reward.txt").write_text(
                f"{reward}\n",
                encoding="utf-8",
            )
        return {}

    summary = run_local_parametric_memory_eval(
        task_root=tmp_path / "tasks",
        task_ids=["query-optimize"],
        run_root=tmp_path / "run",
        terminal_bench_package_root=package_root,
        model="Qwen/Qwen3.6-35B-A3B",
        adapter_path=tmp_path / "adapter",
        adapter_id="tb-parametric-memory",
        adapter_artifact_id="art-parametric",
        server_url="http://127.0.0.1:8000/v1",
        n_attempts=2,
        max_output_tokens=1536,
        context_reserve_tokens=1536,
        timeout_multiplier=2.0,
        agent_timeout_multiplier=3.5,
        exec_timeout_cap_seconds=1800,
        exec_timeout_min_seconds=1800,
        max_subagent_runtime_seconds=2400,
        tool_result_prompt_max_chars=2048,
        verifier_env={},
        agent_env={"EVOLAB_TB_REQUIRE_SUCCESSFUL_COLLECT": "1"},
        artifact_path_guard="repair",
        required_artifact_paths=["/app/out.txt"],
        command_runner=fake_command_runner,
        manage_server=False,
    )

    assert [condition["name"] for condition in summary["conditions"]] == [
        "baseline",
        "parametric_memory",
    ]
    baseline, treatment = summary["conditions"]
    assert baseline["pass_at_1"] == {"passed": 0, "total": 1}
    assert baseline["pass_at_k"] == {"passed": 0, "total": 1, "k": 2}
    assert treatment["pass_at_1"] == {"passed": 1, "total": 1}
    assert treatment["pass_at_k"] == {"passed": 1, "total": 1, "k": 2}
    assert summary["delta"]["pass_at_1"] == 1
    assert summary["delta"]["pass_at_k"] == 1
    assert treatment["adapter"]["artifact_id"] == "art-parametric"
    assert treatment["adapter"]["adapter_id"] == "tb-parametric-memory"
    assert all("mode=evolab" in command for command in commands)
    assert envs[0]["EVOLAB_TB_MODEL"] == "Qwen/Qwen3.6-35B-A3B"
    assert envs[1]["EVOLAB_TB_MODEL"] == "tb-parametric-memory"
    assert all(env["EVOLAB_TB_MODE"] == "direct_solver" for env in envs)
    assert all(env["EVOLAB_TB_MAX_OUTPUT_TOKENS"] == "1536" for env in envs)
    assert all(env["EVOLAB_TB_CONTEXT_WINDOW_TOKENS"] == "16320" for env in envs)
    assert all(env["EVOLAB_TB_CONTEXT_RESERVE_TOKENS"] == "1536" for env in envs)
    assert all(env["EVOLAB_TB_LLM_TEMPERATURE"] == "0.0" for env in envs)
    assert all(env["EVOLAB_TB_TOOL_RESULT_PROMPT_MAX_CHARS"] == "2048" for env in envs)
    assert all(env["EVOLAB_TB_EXEC_TIMEOUT_CAP_SECONDS"] == "1800" for env in envs)
    assert all(env["EVOLAB_TB_EXEC_TIMEOUT_MIN_SECONDS"] == "1800" for env in envs)
    assert all(env["EVOLAB_TB_MAX_SUBAGENT_RUNTIME_SECONDS"] == "2400" for env in envs)
    assert all(env["EVOLAB_TB_REQUIRE_SUCCESSFUL_COLLECT"] == "1" for env in envs)
    assert all(env["EVOLAB_TB_ARTIFACT_PATH_GUARD"] == "repair" for env in envs)
    assert all(command[command.index("--timeout-multiplier") + 1] == "2.0" for command in commands)
    assert all(
        command[command.index("--agent-timeout-multiplier") + 1] == "3.5"
        for command in commands
    )
    assert all(
        json.loads(env["EVOLAB_TB_REQUIRED_ARTIFACT_PATHS"]) == ["/app/out.txt"]
        for env in envs
    )
    assert summary["artifact_path_guard"] == "repair"
    assert summary["required_artifact_paths"] == ["/app/out.txt"]
    assert all(cwd == package_root for cwd in cwds)
    pythonpath_prefix = f"{package_root / 'src'}:{package_root}"
    assert all(env["PYTHONPATH"].startswith(pythonpath_prefix) for env in envs)
    compose_paths = [
        command[index + 1]
        for command in commands
        for index, token in enumerate(command)
        if token == "--extra-docker-compose"
    ]
    assert compose_paths == [
        str(package_root / "task_packages" / "terminal_bench_v1" / "harbor" / "pull-never.yaml"),
        str(
            package_root
            / "task_packages"
            / "terminal_bench_v1"
            / "harbor"
            / "docker-cp-host-network.yaml"
        ),
        str(package_root / "task_packages" / "terminal_bench_v1" / "harbor" / "pull-never.yaml"),
        str(
            package_root
            / "task_packages"
            / "terminal_bench_v1"
            / "harbor"
            / "docker-cp-host-network.yaml"
        ),
    ]


def test_run_local_parametric_memory_eval_scores_only_requested_attempts(
    tmp_path: Path,
) -> None:
    def fake_command_runner(command, *, cwd=None, env=None):
        del cwd, env
        jobs_dir = Path(command[command.index("--jobs-dir") + 1])
        job_name = command[command.index("--job-name") + 1]
        task_id = command[command.index("--include-task-name") + 1]
        for attempt_index, reward in enumerate([0.0, 0.0, 1.0], start=1):
            trial = jobs_dir / job_name / f"{task_id}__attempt{attempt_index}"
            (trial / "agent").mkdir(parents=True)
            (trial / "verifier").mkdir()
            (trial / "result.json").write_text(
                json.dumps(
                    {
                        "trial_name": trial.name,
                        "task_name": task_id,
                        "started_at": f"2026-07-02T00:00:0{attempt_index}Z",
                        "status": "COMPLETED",
                        "verifier_result": {"rewards": {"reward": reward}},
                    }
                ),
                encoding="utf-8",
            )
        return {}

    summary = run_local_parametric_memory_eval(
        task_root=tmp_path / "tasks",
        task_ids=["query-optimize"],
        run_root=tmp_path / "run",
        terminal_bench_package_root=tmp_path / "terminal-bench-package",
        adapter_path=tmp_path / "adapter",
        server_url="http://127.0.0.1:8000/v1",
        n_attempts=2,
        verifier_env={},
        command_runner=fake_command_runner,
        manage_server=False,
    )

    baseline = summary["conditions"][0]
    task = baseline["tasks"][0]
    assert len(task["attempts"]) == 3
    assert task["pass_at_k"] is False
    assert baseline["pass_at_k"] == {"passed": 0, "total": 1, "k": 2}


def test_run_local_parametric_memory_eval_requires_adapter_path(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="adapter_path"):
        run_local_parametric_memory_eval(
            task_root=tmp_path / "tasks",
            task_ids=["query-optimize"],
            run_root=tmp_path / "run",
            adapter_path=Path(),
        )


def test_run_local_parametric_memory_eval_manages_baseline_and_adapter_servers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    servers: list[dict[str, object]] = []

    @local_parametric.contextmanager
    def fake_managed_vllm_server(**kwargs):
        servers.append(kwargs)
        yield {"server_url": kwargs["server_url"], "expected_model": kwargs["expected_model"]}

    def fake_command_runner(command, *, cwd=None, env=None):
        jobs_dir = Path(command[command.index("--jobs-dir") + 1])
        job_name = command[command.index("--job-name") + 1]
        task_id = command[command.index("--include-task-name") + 1]
        model = command[command.index("--model") + 1]
        reward = 1.0 if model == "tb-parametric-memory" else 0.0
        trial = jobs_dir / job_name / f"{task_id}__attempt1"
        (trial / "agent").mkdir(parents=True)
        (trial / "verifier").mkdir()
        (trial / "result.json").write_text(
            json.dumps(
                {
                    "trial_name": trial.name,
                    "task_name": task_id,
                    "status": "COMPLETED",
                    "verifier_result": {"rewards": {"reward": reward}},
                }
            ),
            encoding="utf-8",
        )
        return {}

    monkeypatch.setattr(local_parametric, "managed_vllm_server", fake_managed_vllm_server)

    run_local_parametric_memory_eval(
        task_root=tmp_path / "tasks",
        task_ids=["query-optimize"],
        run_root=tmp_path / "run",
        model="Qwen/Qwen3.6-35B-A3B",
        adapter_path=tmp_path / "adapter",
        adapter_id="tb-parametric-memory",
        server_url="http://127.0.0.1:8000/v1",
        n_attempts=1,
        verifier_env={},
        command_runner=fake_command_runner,
        manage_server=True,
        gpus=["0"],
        vllm_executable="vllm",
    )

    assert [server["expected_model"] for server in servers] == [
        "Qwen/Qwen3.6-35B-A3B",
        "tb-parametric-memory",
    ]
    baseline_spec = servers[0]["spec"]
    treatment_spec = servers[1]["spec"]
    assert "--enable-lora" not in baseline_spec.command
    assert "--enable-lora" in treatment_spec.command
    assert treatment_spec.command[treatment_spec.command.index("--served-model-name") + 1] == (
        "Qwen/Qwen3.6-35B-A3B"
    )
    assert f"tb-parametric-memory={tmp_path / 'adapter'}" in treatment_spec.command


def test_run_local_parametric_memory_eval_rewrites_adapter_keys_for_managed_vllm(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter_dir = tmp_path / "adapter"
    adapter_dir.mkdir()
    (adapter_dir / "adapter_config.json").write_text("{}", encoding="utf-8")
    (adapter_dir / "adapter_model.safetensors").write_bytes(b"fake")

    loaded_state = {
        "base_model.model.model.layers.11.self_attn.q_proj.lora_A.weight": object(),
        "base_model.model.model.layers.11.self_attn.q_proj.lora_B.weight": object(),
        "base_model.model.lm_head.lora_A.weight": object(),
    }
    saved_state: dict[str, object] = {}
    prepared_adapter_path = (
        tmp_path
        / "run"
        / "prepared_adapters"
        / "tb-parametric-memory"
        / "qwen3_5_moe_vllm_language_model"
        / "adapter"
    )

    def fake_load_safetensors(path: Path) -> local_parametric.RawSafetensorsFile:
        assert path == prepared_adapter_path / "adapter_model.safetensors"
        return local_parametric.RawSafetensorsFile(header=dict(loaded_state), payload=b"payload")

    def fake_save_safetensors(raw_file: local_parametric.RawSafetensorsFile, path: Path) -> None:
        saved_state.update(raw_file.header)
        path.write_bytes(b"rewritten")

    monkeypatch.setattr(
        local_parametric,
        "_load_safetensors_file",
        fake_load_safetensors,
    )
    monkeypatch.setattr(
        local_parametric,
        "_save_safetensors_file",
        fake_save_safetensors,
    )

    servers: list[dict[str, object]] = []

    @local_parametric.contextmanager
    def fake_managed_vllm_server(**kwargs):
        servers.append(kwargs)
        yield {"server_url": kwargs["server_url"], "expected_model": kwargs["expected_model"]}

    def fake_command_runner(command, *, cwd=None, env=None):
        del cwd, env
        jobs_dir = Path(command[command.index("--jobs-dir") + 1])
        job_name = command[command.index("--job-name") + 1]
        task_id = command[command.index("--include-task-name") + 1]
        trial = jobs_dir / job_name / f"{task_id}__attempt1"
        trial.mkdir(parents=True)
        (trial / "result.json").write_text(
            json.dumps(
                {
                    "trial_name": trial.name,
                    "task_name": task_id,
                    "status": "COMPLETED",
                    "verifier_result": {"rewards": {"reward": 0.0}},
                }
            ),
            encoding="utf-8",
        )
        return {}

    monkeypatch.setattr(local_parametric, "managed_vllm_server", fake_managed_vllm_server)

    summary = run_local_parametric_memory_eval(
        task_root=tmp_path / "tasks",
        task_ids=["password-recovery"],
        run_root=tmp_path / "run",
        terminal_bench_package_root=tmp_path / "terminal-bench-package",
        model="Qwen/Qwen3.6-35B-A3B",
        adapter_path=adapter_dir,
        adapter_id="tb-parametric-memory",
        server_url="http://127.0.0.1:8000/v1",
        n_attempts=1,
        verifier_env={},
        command_runner=fake_command_runner,
        manage_server=True,
        gpus=["0"],
        vllm_executable="vllm",
        adapter_key_rewrite="qwen3_5_moe_vllm_language_model",
    )

    rewritten_key = (
        "base_model.model.model.language_model.layers.11.self_attn.q_proj.lora_A.weight"
    )
    assert rewritten_key in saved_state
    assert "base_model.model.model.layers.11.self_attn.q_proj.lora_A.weight" not in saved_state
    assert "base_model.model.lm_head.lora_A.weight" in saved_state

    treatment_spec = servers[1]["spec"]
    assert f"tb-parametric-memory={prepared_adapter_path}" in treatment_spec.command
    assert summary["adapter_key_rewrite"] == "qwen3_5_moe_vllm_language_model"
    assert summary["conditions"][1]["adapter"]["source_adapter_path"] == str(adapter_dir)
    assert summary["conditions"][1]["adapter"]["adapter_path"] == str(prepared_adapter_path)
    assert summary["conditions"][1]["adapter"]["rewritten_key_count"] == 2


def test_prepare_serving_adapter_rejects_rewrite_that_matches_no_keys(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter_dir = tmp_path / "adapter"
    adapter_dir.mkdir()
    (adapter_dir / "adapter_config.json").write_text("{}", encoding="utf-8")
    (adapter_dir / "adapter_model.safetensors").write_bytes(b"fake")

    monkeypatch.setattr(
        local_parametric,
        "_load_safetensors_file",
        lambda path: local_parametric.RawSafetensorsFile(
            header={"base_model.model.model.embed_tokens.weight": object()},
            payload=b"payload",
        ),
    )

    def fail_if_saved(raw_file: local_parametric.RawSafetensorsFile, path: Path) -> None:
        raise AssertionError("no-op adapter rewrite should not save a new safetensors file")

    monkeypatch.setattr(local_parametric, "_save_safetensors_file", fail_if_saved)

    with pytest.raises(ValueError, match="did not rewrite any adapter keys"):
        local_parametric.prepare_serving_adapter(
            adapter_path=adapter_dir,
            run_root=tmp_path / "run",
            adapter_id="tb-parametric-memory",
            adapter_key_rewrite="qwen3_5_moe_vllm_language_model",
        )


def test_prepare_serving_adapter_rewrites_safetensors_header_without_torch(
    tmp_path: Path,
) -> None:
    adapter_dir = tmp_path / "adapter"
    adapter_dir.mkdir()
    (adapter_dir / "adapter_config.json").write_text("{}", encoding="utf-8")
    payload = b"\x01\x02\x03\x04"
    header = {
        "__metadata__": {"format": "pt"},
        "base_model.model.model.layers.0.self_attn.q_proj.lora_A.weight": {
            "dtype": "F16",
            "shape": [1, 1],
            "data_offsets": [0, 2],
        },
        "base_model.model.lm_head.lora_A.weight": {
            "dtype": "F16",
            "shape": [1, 1],
            "data_offsets": [2, 4],
        },
    }
    header_bytes = json.dumps(header, separators=(",", ":")).encode("utf-8")
    (adapter_dir / "adapter_model.safetensors").write_bytes(
        len(header_bytes).to_bytes(8, "little") + header_bytes + payload
    )

    prepared = local_parametric.prepare_serving_adapter(
        adapter_path=adapter_dir,
        run_root=tmp_path / "run",
        adapter_id="tb-parametric-memory",
        adapter_key_rewrite="qwen3_5_moe_vllm_language_model",
    )

    rewritten_bytes = (prepared.adapter_path / "adapter_model.safetensors").read_bytes()
    rewritten_header_length = int.from_bytes(rewritten_bytes[:8], "little")
    rewritten_header = json.loads(rewritten_bytes[8 : 8 + rewritten_header_length])
    rewritten_payload = rewritten_bytes[8 + rewritten_header_length :]

    assert prepared.rewritten_key_count == 1
    assert rewritten_header["__metadata__"] == {"format": "pt"}
    assert (
        "base_model.model.model.language_model.layers.0.self_attn.q_proj.lora_A.weight"
        in rewritten_header
    )
    assert (
        "base_model.model.model.layers.0.self_attn.q_proj.lora_A.weight"
        not in rewritten_header
    )
    assert "base_model.model.lm_head.lora_A.weight" in rewritten_header
    assert rewritten_payload == payload


def test_prepare_serving_adapter_accepts_qwen35_vllm_language_model_alias(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter_dir = tmp_path / "adapter"
    adapter_dir.mkdir()
    (adapter_dir / "adapter_config.json").write_text("{}", encoding="utf-8")
    (adapter_dir / "adapter_model.safetensors").write_bytes(b"fake")

    saved_state: dict[str, object] = {}

    monkeypatch.setattr(
        local_parametric,
        "_load_safetensors_file",
        lambda path: local_parametric.RawSafetensorsFile(
            header={
                "base_model.model.model.layers.0.self_attn.q_proj.lora_A.weight": object(),
            },
            payload=b"payload",
        ),
    )

    def fake_save_safetensors(raw_file: local_parametric.RawSafetensorsFile, path: Path) -> None:
        saved_state.update(raw_file.header)
        path.write_bytes(b"rewritten")

    monkeypatch.setattr(local_parametric, "_save_safetensors_file", fake_save_safetensors)

    prepared = local_parametric.prepare_serving_adapter(
        adapter_path=adapter_dir,
        run_root=tmp_path / "run",
        adapter_id="tb-parametric-memory",
        adapter_key_rewrite="qwen3_5_vllm_language_model",
    )

    assert prepared.key_rewrite == "qwen3_5_vllm_language_model"
    assert prepared.rewritten_key_count == 1
    assert prepared.adapter_path == (
        tmp_path
        / "run"
        / "prepared_adapters"
        / "tb-parametric-memory"
        / "qwen3_5_vllm_language_model"
        / "adapter"
    )
    assert (
        "base_model.model.model.language_model.layers.0.self_attn.q_proj.lora_A.weight"
        in saved_state
    )


def test_prepare_serving_adapter_rejects_adapter_key_rewrite_collisions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter_dir = tmp_path / "adapter"
    adapter_dir.mkdir()
    (adapter_dir / "adapter_config.json").write_text("{}", encoding="utf-8")
    (adapter_dir / "adapter_model.safetensors").write_bytes(b"fake")

    monkeypatch.setattr(
        local_parametric,
        "_load_safetensors_file",
        lambda path: local_parametric.RawSafetensorsFile(
            header={
                "base_model.model.model.layers.0.self_attn.q_proj.lora_A.weight": object(),
                (
                    "base_model.model.model.language_model.layers.0.self_attn."
                    "q_proj.lora_A.weight"
                ): object(),
            },
            payload=b"payload",
        ),
    )

    def fail_if_saved(raw_file: local_parametric.RawSafetensorsFile, path: Path) -> None:
        raise AssertionError("colliding adapter rewrite should not save a safetensors file")

    monkeypatch.setattr(local_parametric, "_save_safetensors_file", fail_if_saved)

    with pytest.raises(ValueError, match="would overwrite adapter key"):
        local_parametric.prepare_serving_adapter(
            adapter_path=adapter_dir,
            run_root=tmp_path / "run",
            adapter_id="tb-parametric-memory",
            adapter_key_rewrite="qwen3_5_moe_vllm_language_model",
        )


def test_redacted_env_redacts_secret_values() -> None:
    redacted = local_parametric._redacted_env(
        {
            "OPENAI_API_KEY": "sk-secret",
            "HF_TOKEN": "hf-secret",
            "CLIENT_SECRET": "client-secret",
            "PASSWORD": "password-secret",
            "AUTHORIZATION": "bearer-secret",
            "CUDA_VISIBLE_DEVICES": "0,1",
        }
    )

    assert redacted == {
        "OPENAI_API_KEY": "<redacted>",
        "HF_TOKEN": "<redacted>",
        "CLIENT_SECRET": "<redacted>",
        "PASSWORD": "<redacted>",
        "AUTHORIZATION": "<redacted>",
        "CUDA_VISIBLE_DEVICES": "0,1",
    }


def test_managed_vllm_server_writes_metadata_and_terminates_process_group(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, object] = {"killed": []}

    class FakeProcess:
        pid = 4242

        def __init__(self) -> None:
            self.returncode = None

        def poll(self) -> None:
            return self.returncode

        def wait(self, timeout: float | None = None) -> int:
            self.returncode = 0
            return 0

    fake_process = FakeProcess()

    def fake_popen(command, *, cwd, env, stdout, stderr, start_new_session):
        calls["command"] = command
        calls["cwd"] = cwd
        calls["env"] = env
        calls["stdout"] = stdout
        calls["stderr"] = stderr
        calls["stdout_closed_during_popen"] = stdout.closed
        calls["stderr_closed_during_popen"] = stderr.closed
        calls["start_new_session"] = start_new_session
        return fake_process

    monkeypatch.setattr(local_parametric.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(local_parametric, "wait_for_openai_server", lambda **kwargs: None)
    monkeypatch.setattr(
        local_parametric.os,
        "killpg",
        lambda pid, signal: calls["killed"].append((pid, signal)),
    )

    spec = local_parametric.build_vllm_command(
        model="Qwen/Qwen3.6-35B-A3B",
        served_model_name="Qwen/Qwen3.6-35B-A3B",
    )
    spec.env["OPENAI_API_KEY"] = "host-secret"

    with local_parametric.managed_vllm_server(
        spec=spec,
        run_root=tmp_path,
        server_url="http://127.0.0.1:8000/v1",
        expected_model="Qwen/Qwen3.6-35B-A3B",
        timeout_seconds=1.0,
    ) as metadata:
        assert metadata["pid"] == 4242
        assert metadata["server_url"] == "http://127.0.0.1:8000/v1"
        assert metadata["command"] == spec.command
        assert metadata["env"]["OPENAI_API_KEY"] == "<redacted>"
        assert (tmp_path / "vllm" / "server.json").is_file()

    server_metadata = json.loads((tmp_path / "vllm" / "server.json").read_text())
    assert server_metadata["command"] == spec.command
    assert server_metadata["env"]["OPENAI_API_KEY"] == "<redacted>"
    assert calls["start_new_session"] is True
    assert calls["stdout_closed_during_popen"] is False
    assert calls["stderr_closed_during_popen"] is False
    assert calls["stdout"].closed
    assert calls["stderr"].closed
    assert calls["killed"]


def test_wait_for_openai_server_rejects_missing_expected_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeClient:
        def __init__(self, **kwargs) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args) -> None:
            return None

        def get(self, path: str):
            return SimpleNamespace(
                raise_for_status=lambda: None,
                json=lambda: {"data": [{"id": "Qwen/Qwen3.6-35B-A3B"}]},
            )

        def post(self, path: str, json: dict):
            return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {})

    monkeypatch.setattr(local_parametric.httpx, "Client", FakeClient)

    with pytest.raises(ValueError, match="expected model"):
        local_parametric.wait_for_openai_server(
            server_url="http://127.0.0.1:8000/v1",
            expected_model="tb-parametric-memory",
            timeout_seconds=0.01,
        )


def test_wait_for_openai_server_disables_host_proxy_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, object] = {}

    class FakeClient:
        def __init__(self, **kwargs) -> None:
            calls["kwargs"] = kwargs

        def __enter__(self):
            return self

        def __exit__(self, *args) -> None:
            return None

        def get(self, path: str):
            return SimpleNamespace(
                raise_for_status=lambda: None,
                json=lambda: {"data": [{"id": "tb-parametric-memory"}]},
            )

        def post(self, path: str, json: dict):
            return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {})

    monkeypatch.setattr(local_parametric.httpx, "Client", FakeClient)

    local_parametric.wait_for_openai_server(
        server_url="http://127.0.0.1:8000/v1",
        expected_model="tb-parametric-memory",
        timeout_seconds=0.01,
    )

    assert calls["kwargs"]["trust_env"] is False


def test_terminal_bench_local_parametric_cli_dry_run_writes_output(
    tmp_path: Path,
) -> None:
    output = tmp_path / "summary.json"
    exit_code = main(
        [
            "terminal-bench-local-parametric-memory-eval",
            "--task-root",
            "/root/datasets/terminal-bench-2-1/tasks",
            "--task-id",
            "train-fasttext",
            "--task-id",
            "query-optimize",
            "--run-root",
            str(tmp_path / "run"),
            "--model",
            "Qwen/Qwen3.6-35B-A3B",
            "--adapter-path",
            str(tmp_path / "adapter"),
            "--adapter-id",
            "tb-parametric-memory",
            "--server-url",
            "http://127.0.0.1:8000/v1",
            "--n-attempts",
            "5",
            "--timeout-multiplier",
            "2.0",
            "--agent-timeout-multiplier",
            "3.5",
            "--exec-timeout-cap-seconds",
            "1800",
            "--exec-timeout-min-seconds",
            "1800",
            "--max-subagent-runtime-seconds",
            "2400",
            "--max-output-tokens",
            "1536",
            "--tool-result-prompt-max-chars",
            "2048",
            "--artifact-path-guard",
            "audit",
            "--required-artifact-path",
            "/app/out.txt",
            "--manage-server",
            "--dry-run",
            "--output",
            str(output),
        ]
    )

    assert exit_code == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["dry_run"] is True
    assert payload["enabled_artifacts"] == ["parametric_memory"]
    assert payload["disabled_artifacts"] == ["text_memory", "skill_bundle", "agent_system"]
    assert [condition["name"] for condition in payload["conditions"]] == [
        "baseline",
        "parametric_memory",
    ]
    assert payload["max_output_tokens"] == 1536
    assert payload["tool_result_prompt_max_chars"] == 2048
    assert payload["timeout_multiplier"] == 2.0
    assert payload["agent_timeout_multiplier"] == 3.5
    assert payload["exec_timeout_cap_seconds"] == 1800
    assert payload["exec_timeout_min_seconds"] == 1800
    assert payload["max_subagent_runtime_seconds"] == 2400
    assert payload["artifact_path_guard"] == "audit"
    assert payload["required_artifact_paths"] == ["/app/out.txt"]


def test_terminal_bench_local_parametric_cli_dry_run_records_verifier_env(
    tmp_path: Path,
) -> None:
    output = tmp_path / "summary.json"
    exit_code = main(
        [
            "terminal-bench-local-parametric-memory-eval",
            "--task-root",
            "/root/datasets/terminal-bench-2-1/tasks",
            "--task-id",
            "regex-log",
            "--run-root",
            str(tmp_path / "run"),
            "--adapter-path",
            str(tmp_path / "adapter"),
            "--verifier-env",
            "UV_NO_INDEX=1",
            "--verifier-python-install-mirror",
            "http://172.17.0.8:8765/python-build-standalone",
            "--dry-run",
            "--output",
            str(output),
        ]
    )

    assert exit_code == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["verifier_env"] == {
        "UV_NO_INDEX": "1",
        "UV_PYTHON_INSTALL_MIRROR": (
            "http://172.17.0.8:8765/python-build-standalone/releases/download"
        ),
    }


def test_terminal_bench_local_parametric_cli_preserves_python_install_mirror_download_base(
    tmp_path: Path,
) -> None:
    output = tmp_path / "summary.json"
    exit_code = main(
        [
            "terminal-bench-local-parametric-memory-eval",
            "--task-root",
            "/root/datasets/terminal-bench-2-1/tasks",
            "--task-id",
            "regex-log",
            "--run-root",
            str(tmp_path / "run"),
            "--adapter-path",
            str(tmp_path / "adapter"),
            "--verifier-python-install-mirror",
            "http://172.17.0.8:8765/python-build-standalone/releases/download",
            "--dry-run",
            "--output",
            str(output),
        ]
    )

    assert exit_code == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["verifier_env"] == {
        "UV_PYTHON_INSTALL_MIRROR": (
            "http://172.17.0.8:8765/python-build-standalone/releases/download"
        )
    }


def test_terminal_bench_local_parametric_cli_dry_run_records_agent_env(
    tmp_path: Path,
) -> None:
    output = tmp_path / "summary.json"
    exit_code = main(
        [
            "terminal-bench-local-parametric-memory-eval",
            "--task-root",
            "/root/datasets/terminal-bench-2-1/tasks",
            "--task-id",
            "regex-log",
            "--run-root",
            str(tmp_path / "run"),
            "--adapter-path",
            str(tmp_path / "adapter"),
            "--agent-env",
            "EVOLAB_TB_REQUIRE_SUCCESSFUL_COLLECT=1",
            "--agent-env",
            "EVOLAB_TB_DIRECT_SOLVER_COMPLETION_GUARD=successful_collect",
            "--dry-run",
            "--output",
            str(output),
        ]
    )

    assert exit_code == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["agent_env"] == {
        "EVOLAB_TB_DIRECT_SOLVER_COMPLETION_GUARD": "successful_collect",
        "EVOLAB_TB_REQUIRE_SUCCESSFUL_COLLECT": "1",
    }


def test_terminal_bench_local_parametric_cli_dry_run_records_proxy_auth(
    tmp_path: Path,
) -> None:
    output = tmp_path / "summary.json"
    exit_code = main(
        [
            "terminal-bench-local-parametric-memory-eval",
            "--task-root",
            "/root/datasets/terminal-bench-2-1/tasks",
            "--task-id",
            "query-optimize",
            "--run-root",
            str(tmp_path / "run"),
            "--adapter-path",
            str(tmp_path / "adapter"),
            "--auth-mode",
            "proxy",
            "--dry-run",
            "--output",
            str(output),
        ]
    )

    assert exit_code == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["auth_mode"] == "proxy"


def test_terminal_bench_local_parametric_cli_dry_run_records_adapter_key_rewrite(
    tmp_path: Path,
) -> None:
    output = tmp_path / "summary.json"
    exit_code = main(
        [
            "terminal-bench-local-parametric-memory-eval",
            "--task-root",
            "/root/datasets/terminal-bench-2-1/tasks",
            "--task-id",
            "password-recovery",
            "--run-root",
            str(tmp_path / "run"),
            "--adapter-path",
            str(tmp_path / "adapter"),
            "--adapter-key-rewrite",
            "qwen3_5_moe_vllm_language_model",
            "--dry-run",
            "--output",
            str(output),
        ]
    )

    assert exit_code == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["adapter_key_rewrite"] == "qwen3_5_moe_vllm_language_model"
    assert payload["conditions"][1]["adapter_key_rewrite"] == (
        "qwen3_5_moe_vllm_language_model"
    )


def test_terminal_bench_local_parametric_cli_rejects_subscription_auth(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="requires local or proxy auth"):
        main(
            [
                "terminal-bench-local-parametric-memory-eval",
                "--task-root",
                "/root/datasets/terminal-bench-2-1/tasks",
                "--task-id",
                "query-optimize",
                "--run-root",
                str(tmp_path / "run"),
                "--model",
                "Qwen/Qwen3.6-35B-A3B",
                "--adapter-path",
                str(tmp_path / "adapter"),
                "--auth-mode",
                "subscription",
                "--output",
                str(tmp_path / "summary.json"),
            ]
        )


def test_terminal_bench_parametric_memory_job_creates_lora_sft_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "job.json"
    input_trial = tmp_path / "trial"
    (input_trial / "agent").mkdir(parents=True)
    (input_trial / "verifier").mkdir()
    (input_trial / "result.json").write_text(
        json.dumps(
            {
                "trial_name": "query-optimize__success",
                "task_name": "query-optimize",
                "status": "COMPLETED",
                "verifier_result": {"rewards": {"reward": 1.0}},
            }
        ),
        encoding="utf-8",
    )
    (input_trial / "agent" / "stdout.txt").write_text("solved\n", encoding="utf-8")
    (input_trial / "verifier" / "reward.txt").write_text("1.0\n", encoding="utf-8")

    exit_code = main(
        [
            "terminal-bench-parametric-memory-job",
            "--input",
            str(input_trial),
            "--db",
            str(tmp_path / "evolution.db"),
            "--artifact-root",
            str(tmp_path / "artifacts"),
            "--dataset-name",
            "tb21-parametric-query-optimize",
            "--policy-version",
            "tb21-qwen-local-query-optimize",
            "--base-model",
            "Qwen/Qwen3.6-35B-A3B",
            "--adapter-id",
            "tb-parametric-memory",
            "--trainer-command",
            "python",
            "--trainer-arg",
            "/opt/train_lora.py",
            "--trainer-arg",
            "--train-file",
            "--trainer-arg",
            "{training_dataset}",
            "--trainer-arg",
            "--output-dir",
            "--trainer-arg",
            "{adapter_dir}",
            "--training-projection",
            "response_tail",
            "--training-response-tail-chars",
            "4096",
            "--output",
            str(output),
        ]
    )

    assert exit_code == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["job"]["method"] == "parametric_memory_lora_sft"
    assert payload["job"]["config"]["base_model"] == "Qwen/Qwen3.6-35B-A3B"
    assert payload["job"]["config"]["output_adapter_id"] == "tb-parametric-memory"
    assert payload["job"]["config"]["training_projection"] == {
        "type": "response_tail",
        "response_tail_chars": 4096,
    }
    assert payload["job"]["config"]["trainer"]["command"] == "python"
    assert "{training_dataset}" in payload["job"]["config"]["trainer"]["args"]
    assert "{adapter_dir}" in payload["job"]["config"]["trainer"]["args"]
    assert (
        "terminal-bench:query-optimize"
        in payload["job"]["config"]["compatibility"]["task_tags"]
    )


def test_terminal_bench_parametric_memory_job_accepts_final_actions_projection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "job.json"
    input_trial = tmp_path / "trial"
    (input_trial / "agent").mkdir(parents=True)
    (input_trial / "verifier").mkdir()
    (input_trial / "result.json").write_text(
        json.dumps(
            {
                "trial_name": "password-recovery__success",
                "task_name": "password-recovery",
                "status": "COMPLETED",
                "verifier_result": {"rewards": {"reward": 1.0}},
            }
        ),
        encoding="utf-8",
    )
    (input_trial / "agent" / "stdout.txt").write_text("solved\n", encoding="utf-8")
    (input_trial / "verifier" / "reward.txt").write_text("1.0\n", encoding="utf-8")

    exit_code = main(
        [
            "terminal-bench-parametric-memory-job",
            "--input",
            str(input_trial),
            "--db",
            str(tmp_path / "evolution.db"),
            "--artifact-root",
            str(tmp_path / "artifacts"),
            "--dataset-name",
            "tb21-parametric-password-recovery",
            "--policy-version",
            "tb21-qwen-local-password-recovery",
            "--base-model",
            "Qwen/Qwen3.6-35B-A3B",
            "--adapter-id",
            "tb-parametric-memory",
            "--trainer-command",
            "python",
            "--trainer-arg",
            "/opt/train_lora.py",
            "--trainer-arg",
            "--train-file",
            "--trainer-arg",
            "{training_dataset}",
            "--trainer-arg",
            "--output-dir",
            "--trainer-arg",
            "{adapter_dir}",
            "--training-projection",
            "terminal_bench_final_actions",
            "--training-final-action-max-events",
            "8",
            "--training-final-action-output-chars",
            "2000",
            "--output",
            str(output),
        ]
    )

    assert exit_code == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["job"]["config"]["training_projection"] == {
        "type": "terminal_bench_final_actions",
        "max_events": 8,
        "max_output_chars": 2000,
    }


def test_terminal_bench_parametric_memory_job_accepts_tool_call_policy_projection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "job.json"
    input_trial = tmp_path / "trial"
    (input_trial / "agent").mkdir(parents=True)
    (input_trial / "verifier").mkdir()
    (input_trial / "result.json").write_text(
        json.dumps(
            {
                "trial_name": "password-recovery__success",
                "task_name": "password-recovery",
                "status": "COMPLETED",
                "verifier_result": {"rewards": {"reward": 1.0}},
            }
        ),
        encoding="utf-8",
    )
    (input_trial / "agent" / "stdout.txt").write_text("solved\n", encoding="utf-8")
    (input_trial / "verifier" / "reward.txt").write_text("1.0\n", encoding="utf-8")

    exit_code = main(
        [
            "terminal-bench-parametric-memory-job",
            "--input",
            str(input_trial),
            "--db",
            str(tmp_path / "evolution.db"),
            "--artifact-root",
            str(tmp_path / "artifacts"),
            "--dataset-name",
            "tb21-parametric-password-recovery",
            "--policy-version",
            "tb21-qwen-local-password-recovery",
            "--base-model",
            "Qwen/Qwen3.6-35B-A3B",
            "--adapter-id",
            "tb-parametric-memory",
            "--trainer-command",
            "python",
            "--trainer-arg",
            "/opt/train_lora.py",
            "--trainer-arg",
            "--train-file",
            "--trainer-arg",
            "{training_dataset}",
            "--trainer-arg",
            "--output-dir",
            "--trainer-arg",
            "{adapter_dir}",
            "--training-projection",
            "terminal_bench_tool_call_policy",
            "--training-tool-call-max-commands",
            "1",
            "--training-tool-call-command-contains",
            "recovered_passwords.txt",
            "--training-tool-call-exclude-command-contains",
            "/data1/containerd",
            "--training-tool-call-derive-password-recovery-command",
            "--output",
            str(output),
        ]
    )

    assert exit_code == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["job"]["config"]["training_projection"] == {
        "type": "terminal_bench_tool_call_policy",
        "max_commands": 1,
        "command_contains": ["recovered_passwords.txt"],
        "exclude_command_contains": ["/data1/containerd"],
        "derive_password_recovery_command": True,
    }


def test_terminal_bench_parametric_memory_job_accepts_corrective_tool_call_policy_projection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "job.json"
    input_trial = tmp_path / "trial"
    (input_trial / "agent").mkdir(parents=True)
    (input_trial / "verifier").mkdir()
    trajectory_dir = input_trial / "agent" / "evolab_lab" / ".evolab" / "registries" / "trajectory"
    trajectory_dir.mkdir(parents=True)
    (trajectory_dir / "llm_calls.jsonl").write_text(
        json.dumps(
            {
                "model": "Qwen/Qwen3.6-35B-A3B",
                "input_messages": [
                    {"role": "system", "content": "Use tb_read_task first."},
                    {"role": "tool", "content": '{"stdout": "PASSWORD=8XDP..."}'},
                ],
                "metadata": {
                    "step_index": 12,
                    "tool_specs": [
                        {
                            "name": "tb_exec",
                            "description": "Run a command.",
                            "parameters_schema": {
                                "type": "object",
                                "properties": {"command": {"type": "string"}},
                                "required": ["command"],
                            },
                        }
                    ],
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (input_trial / "result.json").write_text(
        json.dumps(
            {
                "trial_name": "password-recovery__failed",
                "task_name": "password-recovery",
                "status": "COMPLETED",
                "verifier_result": {"rewards": {"reward": 0.0}},
            }
        ),
        encoding="utf-8",
    )
    (input_trial / "agent" / "stdout.txt").write_text("budget exceeded\n", encoding="utf-8")
    (input_trial / "verifier" / "reward.txt").write_text("0.0\n", encoding="utf-8")

    exit_code = main(
        [
            "terminal-bench-parametric-memory-job",
            "--input",
            str(input_trial),
            "--status",
            "COMPLETED",
            "--db",
            str(tmp_path / "evolution.db"),
            "--artifact-root",
            str(tmp_path / "artifacts"),
            "--dataset-name",
            "tb21-parametric-password-recovery-corrective",
            "--policy-version",
            "tb21-qwen-local-password-recovery-corrective",
            "--base-model",
            "Qwen/Qwen3.6-35B-A3B",
            "--adapter-id",
            "tb-parametric-memory",
            "--trainer-command",
            "python",
            "--trainer-arg",
            "/opt/train_lora.py",
            "--trainer-arg",
            "--train-file",
            "--trainer-arg",
            "{training_dataset}",
            "--trainer-arg",
            "--output-dir",
            "--trainer-arg",
            "{adapter_dir}",
            "--training-projection",
            "terminal_bench_corrective_tool_call_policy",
            "--training-corrective-input-contains",
            "PASSWORD=.*",
            "--training-corrective-max-examples",
            "3",
            "--training-corrective-max-input-tool-messages",
            "4",
            "--training-corrective-strip-input-tool-result-payload",
            "--training-corrective-max-input-tool-content-chars",
            "384",
            "--training-corrective-target-command",
            "printf '%s\\n' 8XDP5Q2RT9ZK7VB3BV4WW54 > /app/recovered_passwords.txt",
            "--output",
            str(output),
        ]
    )

    assert exit_code == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["dataset"]["event_count"] == 1
    assert payload["job"]["config"]["training_projection"] == {
        "type": "terminal_bench_corrective_tool_call_policy",
        "input_contains": ["PASSWORD=.*"],
        "max_examples": 3,
        "max_input_tool_messages": 4,
        "strip_input_tool_result_payload": True,
        "max_input_tool_content_chars": 384,
        "target_tool_call": {
            "name": "tb_exec",
            "arguments": {
                "task_id": "terminal-bench-task",
                "command": (
                    "printf '%s\\n' 8XDP5Q2RT9ZK7VB3BV4WW54 "
                    "> /app/recovered_passwords.txt"
                ),
            },
        },
    }


def test_terminal_bench_parametric_memory_job_accepts_corrective_stage_json(
    tmp_path: Path,
) -> None:
    output = tmp_path / "job.json"
    input_trial = tmp_path / "trial"
    (input_trial / "agent").mkdir(parents=True)
    (input_trial / "verifier").mkdir()
    trajectory_dir = input_trial / "agent" / "evolab_lab" / ".evolab" / "registries" / "trajectory"
    trajectory_dir.mkdir(parents=True)
    (trajectory_dir / "llm_calls.jsonl").write_text(
        json.dumps(
            {
                "model": "Qwen/Qwen3.6-35B-A3B",
                "input_messages": [
                    {"role": "system", "content": "Use tb_read_task first."},
                    {"role": "user", "content": "Instruction: recover launchcode.txt"},
                ],
                "metadata": {
                    "step_index": 0,
                    "tool_specs": [
                        {
                            "name": "tb_read_task",
                            "description": "Read task.",
                            "parameters_schema": {
                                "type": "object",
                                "properties": {"task_id": {"type": "string"}},
                                "required": ["task_id"],
                            },
                        }
                    ],
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (input_trial / "result.json").write_text(
        json.dumps(
            {
                "trial_name": "password-recovery__failed",
                "task_name": "password-recovery",
                "status": "COMPLETED",
                "verifier_result": {"rewards": {"reward": 0.0}},
            }
        ),
        encoding="utf-8",
    )
    (input_trial / "agent" / "stdout.txt").write_text("budget exceeded\n", encoding="utf-8")
    (input_trial / "verifier" / "reward.txt").write_text("0.0\n", encoding="utf-8")

    exit_code = main(
        [
            "terminal-bench-parametric-memory-job",
            "--input",
            str(input_trial),
            "--status",
            "COMPLETED",
            "--db",
            str(tmp_path / "evolution.db"),
            "--artifact-root",
            str(tmp_path / "artifacts"),
            "--dataset-name",
            "tb21-parametric-password-recovery-stages",
            "--policy-version",
            "tb21-qwen-local-password-recovery-stages",
            "--base-model",
            "Qwen/Qwen3.6-35B-A3B",
            "--adapter-id",
            "tb-parametric-memory",
            "--trainer-command",
            "python",
            "--trainer-arg",
            "/opt/train_lora.py",
            "--trainer-arg",
            "--train-file",
            "--trainer-arg",
            "{training_dataset}",
            "--trainer-arg",
            "--output-dir",
            "--trainer-arg",
            "{adapter_dir}",
            "--training-projection",
            "terminal_bench_corrective_tool_call_policy",
            "--training-corrective-stage-json",
            json.dumps(
                {
                    "name": "read_task",
                    "input_contains": ["recover launchcode.txt"],
                    "target_tool_call": {
                        "name": "tb_read_task",
                        "arguments": {"task_id": "terminal-bench-task"},
                    },
                }
            ),
            "--training-corrective-stage-json",
            json.dumps(
                {
                    "name": "short_exec_after_read",
                    "input_contains": ["starts with 8XD"],
                    "max_examples": 2,
                    "repeat": 6,
                    "synthetic_tool_results": [
                        {
                            "name": "tb_run_tests",
                            "tool_call_id": "call-tests",
                            "content": '{"status": "passed"}',
                        }
                    ],
                    "target_tool_call": {
                        "name": "tb_exec",
                        "arguments": {
                            "task_id": "terminal-bench-task",
                            "command": "grep -ao '8XD[A-Z0-9]*' disk;true",
                        },
                    },
                }
            ),
            "--output",
            str(output),
        ]
    )

    assert exit_code == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["dataset"]["event_count"] == 1
    assert payload["job"]["config"]["training_projection"] == {
        "type": "terminal_bench_corrective_tool_call_policy",
        "stages": [
            {
                "name": "read_task",
                "input_contains": ["recover launchcode.txt"],
                "max_examples": 64,
                "repeat": 1,
                "target_tool_call": {
                    "name": "tb_read_task",
                    "arguments": {"task_id": "terminal-bench-task"},
                },
            },
            {
                "name": "short_exec_after_read",
                "input_contains": ["starts with 8XD"],
                "max_examples": 2,
                "repeat": 6,
                "synthetic_tool_results": [
                    {
                        "name": "tb_run_tests",
                        "tool_call_id": "call-tests",
                        "content": '{"status": "passed"}',
                    }
                ],
                "target_tool_call": {
                    "name": "tb_exec",
                    "arguments": {
                        "task_id": "terminal-bench-task",
                        "command": "grep -ao '8XD[A-Z0-9]*' disk;true",
                    },
                },
            },
        ],
    }


def test_terminal_bench_parametric_memory_job_accepts_password_recovery_recipe(
    tmp_path: Path,
) -> None:
    output = tmp_path / "job.json"
    input_trial = tmp_path / "trial"
    (input_trial / "agent").mkdir(parents=True)
    (input_trial / "verifier").mkdir()
    trajectory_dir = input_trial / "agent" / "evolab_lab" / ".evolab" / "registries" / "trajectory"
    trajectory_dir.mkdir(parents=True)
    (trajectory_dir / "llm_calls.jsonl").write_text(
        json.dumps(
            {
                "model": "Qwen/Qwen3.6-35B-A3B",
                "input_messages": [
                    {"role": "system", "content": "Use Terminal Bench tools."},
                    {
                        "role": "user",
                        "content": "static-terminal-bench-harbor password-recovery",
                    },
                ],
                "metadata": {
                    "step_index": 0,
                    "tool_specs": [
                        {
                            "name": "tb_read_task",
                            "description": "Read task.",
                            "parameters_schema": {
                                "type": "object",
                                "properties": {"task_id": {"type": "string"}},
                                "required": ["task_id"],
                            },
                        }
                    ],
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (input_trial / "result.json").write_text(
        json.dumps(
            {
                "trial_name": "password-recovery__failed",
                "task_name": "password-recovery",
                "status": "COMPLETED",
                "verifier_result": {"rewards": {"reward": 0.0}},
            }
        ),
        encoding="utf-8",
    )
    (input_trial / "agent" / "stdout.txt").write_text("budget exceeded\n", encoding="utf-8")
    (input_trial / "verifier" / "reward.txt").write_text("0.0\n", encoding="utf-8")

    exit_code = main(
        [
            "terminal-bench-parametric-memory-job",
            "--input",
            str(input_trial),
            "--status",
            "COMPLETED",
            "--db",
            str(tmp_path / "evolution.db"),
            "--artifact-root",
            str(tmp_path / "artifacts"),
            "--dataset-name",
            "tb21-parametric-password-recovery-recipe",
            "--policy-version",
            "tb21-qwen-local-password-recovery-recipe",
            "--base-model",
            "Qwen/Qwen3.6-35B-A3B",
            "--adapter-id",
            "tb-parametric-memory",
            "--trainer-command",
            "python",
            "--trainer-arg",
            "/opt/train_lora.py",
            "--trainer-arg",
            "--train-file",
            "--trainer-arg",
            "{training_dataset}",
            "--trainer-arg",
            "--output-dir",
            "--trainer-arg",
            "{adapter_dir}",
            "--training-projection",
            "terminal_bench_password_recovery_shorttarget_recipe",
            "--training-recipe-target-command",
            "derive-short-target > /app/recovered_passwords.txt",
            "--training-recipe-after-read-repeat",
            "2",
            "--training-recipe-correction-input-contains",
            "Dummy entry",
            "--training-recipe-correction-repeat",
            "1",
            "--training-recipe-max-input-tool-messages",
            "2",
            "--training-recipe-strip-input-tool-result-payload",
            "--training-recipe-max-input-tool-content-chars",
            "512",
            "--output",
            str(output),
        ]
    )

    assert exit_code == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["dataset"]["event_count"] == 1
    assert payload["job"]["config"]["training_projection"] == {
        "type": "terminal_bench_corrective_tool_call_policy",
        "recipe": {
            "type": "terminal_bench_password_recovery_shorttarget_recipe",
            "target_command": "derive-short-target > /app/recovered_passwords.txt",
            "target_task_id": "terminal-bench-task",
            "read_task_input_contains": ["static-terminal-bench-harbor"],
            "after_read_input_contains": ["recovered_passwords.txt"],
            "correction_input_contains": ["Dummy entry"],
            "read_task_max_examples": 1,
            "after_read_max_examples": 1,
            "after_read_repeat": 2,
            "correction_max_examples": 1,
            "correction_repeat": 1,
            "max_input_tool_messages": 2,
            "strip_input_tool_result_payload": True,
            "max_input_tool_content_chars": 512,
        },
        "stages": [
            {
                "name": "read_task",
                "input_contains": ["static-terminal-bench-harbor"],
                "max_examples": 1,
                "repeat": 1,
                "max_input_tool_messages": 2,
                "strip_input_tool_result_payload": True,
                "max_input_tool_content_chars": 512,
                "target_tool_call": {
                    "name": "tb_read_task",
                    "arguments": {"task_id": "terminal-bench-task"},
                },
            },
            {
                "name": "short_exec_after_read",
                "input_contains": ["recovered_passwords.txt"],
                "max_examples": 1,
                "repeat": 2,
                "max_input_tool_messages": 2,
                "strip_input_tool_result_payload": True,
                "max_input_tool_content_chars": 512,
                "target_tool_call": {
                    "name": "tb_exec",
                    "arguments": {
                        "task_id": "terminal-bench-task",
                        "command": "derive-short-target > /app/recovered_passwords.txt",
                    },
                },
            },
            {
                "name": "correct_back_to_short_exec",
                "input_contains": ["Dummy entry"],
                "max_examples": 1,
                "repeat": 1,
                "max_input_tool_messages": 2,
                "strip_input_tool_result_payload": True,
                "max_input_tool_content_chars": 512,
                "target_tool_call": {
                    "name": "tb_exec",
                    "arguments": {
                        "task_id": "terminal-bench-task",
                        "command": "derive-short-target > /app/recovered_passwords.txt",
                    },
                },
            },
        ],
    }


def test_terminal_bench_parametric_memory_job_can_run_local_worker_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "job.json"
    input_trial = tmp_path / "trial"
    adapter_dir = tmp_path / "adapter"
    adapter_dir.mkdir()
    (adapter_dir / "adapter_config.json").write_text("{}", encoding="utf-8")
    (input_trial / "agent").mkdir(parents=True)
    (input_trial / "verifier").mkdir()
    (input_trial / "result.json").write_text(
        json.dumps(
            {
                "trial_name": "query-optimize__success",
                "task_name": "query-optimize",
                "status": "COMPLETED",
                "verifier_result": {"rewards": {"reward": 1.0}},
            }
        ),
        encoding="utf-8",
    )
    (input_trial / "agent" / "stdout.txt").write_text("solved\n", encoding="utf-8")
    (input_trial / "verifier" / "reward.txt").write_text("1.0\n", encoding="utf-8")

    def fake_run_worker_once_local(*, db_path: Path, artifact_root: Path):
        assert db_path == tmp_path / "evolution.db"
        assert artifact_root == tmp_path / "artifacts"
        return [
            {
                "artifact_id": "art-parametric",
                "type": "parametric_memory",
                "uri": adapter_dir.as_uri(),
                "manifest": {
                    "adapter_id": "tb-parametric-memory",
                    "base_model": "Qwen/Qwen3.6-35B-A3B",
                    "adapter_format": "lora",
                },
            }
        ]

    monkeypatch.setattr(cli_module, "_run_worker_once_local", fake_run_worker_once_local)

    exit_code = main(
        [
            "terminal-bench-parametric-memory-job",
            "--input",
            str(input_trial),
            "--db",
            str(tmp_path / "evolution.db"),
            "--artifact-root",
            str(tmp_path / "artifacts"),
            "--dataset-name",
            "tb21-parametric-query-optimize",
            "--policy-version",
            "tb21-qwen-local-query-optimize",
            "--base-model",
            "Qwen/Qwen3.6-35B-A3B",
            "--adapter-id",
            "tb-parametric-memory",
            "--trainer-command",
            "python",
            "--trainer-arg",
            "{training_dataset}",
            "--trainer-arg",
            "{adapter_dir}",
            "--run-worker",
            "--output",
            str(output),
        ]
    )

    assert exit_code == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["completed_artifacts"][0]["artifact_id"] == "art-parametric"
    assert (
        payload["completed_artifacts"][0]["manifest"]["adapter_id"]
        == "tb-parametric-memory"
    )


def test_terminal_bench_local_parametric_cli_live_invokes_runner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "summary.json"
    captured: dict[str, object] = {}

    def fake_runner(**kwargs):
        captured.update(kwargs)
        return {"dry_run": False, "conditions": []}

    monkeypatch.setattr(cli_module, "run_local_parametric_memory_eval", fake_runner)

    exit_code = main(
        [
            "terminal-bench-local-parametric-memory-eval",
            "--task-root",
            "/root/datasets/terminal-bench-2-1/tasks",
            "--task-id",
            "query-optimize",
            "--run-root",
            str(tmp_path / "run"),
            "--terminal-bench-package-root",
            "/tmp/terminal-bench-package",
            "--model",
            "Qwen/Qwen3.6-35B-A3B",
            "--adapter-path",
            str(tmp_path / "adapter"),
            "--adapter-artifact-id",
            "art-parametric",
            "--gpu",
            "1",
            "--gpu",
            "2",
            "--server-port",
            "8011",
            "--timeout-multiplier",
            "2.0",
            "--agent-timeout-multiplier",
            "3.5",
            "--exec-timeout-cap-seconds",
            "1800",
            "--exec-timeout-min-seconds",
            "1800",
            "--max-subagent-runtime-seconds",
            "2400",
            "--auth-mode",
            "proxy",
            "--verifier-env",
            "UV_NO_INDEX=1",
            "--agent-env",
            "EVOLAB_TB_REQUIRE_SUCCESSFUL_COLLECT=1",
            "--artifact-path-guard",
            "repair",
            "--required-artifact-path",
            "/app/out.txt",
            "--output",
            str(output),
        ]
    )

    assert exit_code == 0
    assert captured["task_ids"] == ["query-optimize"]
    assert captured["terminal_bench_package_root"] == Path("/tmp/terminal-bench-package")
    assert captured["adapter_artifact_id"] == "art-parametric"
    assert captured["gpus"] == ["1", "2"]
    assert captured["port"] == 8011
    assert captured["timeout_multiplier"] == 2.0
    assert captured["agent_timeout_multiplier"] == 3.5
    assert captured["exec_timeout_cap_seconds"] == 1800
    assert captured["exec_timeout_min_seconds"] == 1800
    assert captured["max_subagent_runtime_seconds"] == 2400
    assert captured["server_url"] == "http://127.0.0.1:8011/v1"
    assert captured["auth_mode"] == "proxy"
    assert captured["verifier_env"] == {"UV_NO_INDEX": "1"}
    assert captured["agent_env"] == {"EVOLAB_TB_REQUIRE_SUCCESSFUL_COLLECT": "1"}
    assert captured["artifact_path_guard"] == "repair"
    assert captured["required_artifact_paths"] == ["/app/out.txt"]
    assert json.loads(output.read_text(encoding="utf-8")) == {
        "dry_run": False,
        "conditions": [],
    }


def test_terminal_bench_local_parametric_cli_adds_python_install_mirror(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "summary.json"
    captured: dict[str, object] = {}

    def fake_runner(**kwargs):
        captured.update(kwargs)
        return {"dry_run": False, "conditions": []}

    monkeypatch.setattr(cli_module, "run_local_parametric_memory_eval", fake_runner)

    exit_code = main(
        [
            "terminal-bench-local-parametric-memory-eval",
            "--task-root",
            "/root/datasets/terminal-bench-2-1/tasks",
            "--task-id",
            "regex-log",
            "--run-root",
            str(tmp_path / "run"),
            "--model",
            "Qwen/Qwen3.6-35B-A3B",
            "--adapter-path",
            str(tmp_path / "adapter"),
            "--verifier-env",
            "UV_NO_INDEX=1",
            "--verifier-python-install-mirror",
            "http://172.17.0.8:8765/python-build-standalone",
            "--output",
            str(output),
        ]
    )

    assert exit_code == 0
    assert captured["verifier_env"] == {
        "UV_NO_INDEX": "1",
        "UV_PYTHON_INSTALL_MIRROR": (
            "http://172.17.0.8:8765/python-build-standalone/releases/download"
        ),
    }


def test_managed_vllm_server_does_not_mask_body_exception_when_process_group_is_gone(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, object] = {"waited": False}

    class FakeProcess:
        pid = 4242
        returncode = None

        def poll(self) -> None:
            return None

        def wait(self, timeout: float | None = None) -> int:
            calls["waited"] = True
            self.returncode = 0
            return 0

    fake_process = FakeProcess()

    def fake_popen(command, *, cwd, env, stdout, stderr, start_new_session):
        return fake_process

    def fake_killpg(pid: int, kill_signal: int) -> None:
        raise ProcessLookupError(pid)

    monkeypatch.setattr(local_parametric.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(local_parametric, "wait_for_openai_server", lambda **kwargs: None)
    monkeypatch.setattr(local_parametric.os, "killpg", fake_killpg)

    spec = local_parametric.build_vllm_command()

    with pytest.raises(RuntimeError, match="body failed"):
        with local_parametric.managed_vllm_server(
            spec=spec,
            run_root=tmp_path,
            server_url="http://127.0.0.1:8000/v1",
            expected_model="Qwen/Qwen3.6-35B-A3B",
            timeout_seconds=1.0,
        ):
            raise RuntimeError("body failed")

    assert calls["waited"] is True


def test_managed_vllm_server_reports_process_exit_during_startup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeProcess:
        pid = 4242
        returncode = 17

        def poll(self) -> int:
            return self.returncode

        def wait(self, timeout: float | None = None) -> int:
            return self.returncode

    fake_process = FakeProcess()

    def fake_popen(command, *, cwd, env, stdout, stderr, start_new_session):
        return fake_process

    class FakeClient:
        def __init__(self, **kwargs) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args) -> None:
            return None

        def get(self, path: str):
            raise local_parametric.httpx.ConnectError("not listening")

    monkeypatch.setattr(local_parametric.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(local_parametric.httpx, "Client", FakeClient)

    spec = local_parametric.build_vllm_command()

    with pytest.raises(RuntimeError) as exc_info:
        with local_parametric.managed_vllm_server(
            spec=spec,
            run_root=tmp_path,
            server_url="http://127.0.0.1:8000/v1",
            expected_model="Qwen/Qwen3.6-35B-A3B",
            timeout_seconds=0.0,
        ):
            pass

    message = str(exc_info.value)
    assert "return code 17" in message
    assert str(tmp_path / "vllm" / "stdout.log") in message
    assert str(tmp_path / "vllm" / "stderr.log") in message


def test_managed_vllm_server_does_not_mask_body_exception_when_sigkill_group_is_gone(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, object] = {"signals": []}

    class FakeProcess:
        pid = 4242
        returncode = None

        def poll(self) -> None:
            return None

        def wait(self, timeout: float | None = None) -> int:
            calls["signals"].append("wait")
            if calls["signals"].count("wait") == 1:
                raise local_parametric.subprocess.TimeoutExpired("vllm", timeout)
            self.returncode = 0
            return 0

    fake_process = FakeProcess()

    def fake_popen(command, *, cwd, env, stdout, stderr, start_new_session):
        return fake_process

    def fake_killpg(pid: int, kill_signal: int) -> None:
        calls["signals"].append(kill_signal)
        if kill_signal == local_parametric.signal.SIGKILL:
            raise ProcessLookupError(pid)

    monkeypatch.setattr(local_parametric.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(local_parametric, "wait_for_openai_server", lambda **kwargs: None)
    monkeypatch.setattr(local_parametric.os, "killpg", fake_killpg)

    spec = local_parametric.build_vllm_command()

    with pytest.raises(RuntimeError, match="body failed"):
        with local_parametric.managed_vllm_server(
            spec=spec,
            run_root=tmp_path,
            server_url="http://127.0.0.1:8000/v1",
            expected_model="Qwen/Qwen3.6-35B-A3B",
            timeout_seconds=1.0,
        ):
            raise RuntimeError("body failed")

    assert local_parametric.signal.SIGTERM in calls["signals"]
    assert local_parametric.signal.SIGKILL in calls["signals"]


def test_managed_vllm_server_does_not_mask_body_exception_when_sigkill_wait_times_out(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, object] = {"signals": []}

    class FakeProcess:
        pid = 4242
        returncode = None

        def poll(self) -> None:
            return None

        def wait(self, timeout: float | None = None) -> int:
            calls["signals"].append("wait")
            raise local_parametric.subprocess.TimeoutExpired("vllm", timeout)

    fake_process = FakeProcess()

    def fake_popen(command, *, cwd, env, stdout, stderr, start_new_session):
        return fake_process

    def fake_killpg(pid: int, kill_signal: int) -> None:
        calls["signals"].append(kill_signal)

    monkeypatch.setattr(local_parametric.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(local_parametric, "wait_for_openai_server", lambda **kwargs: None)
    monkeypatch.setattr(local_parametric.os, "killpg", fake_killpg)

    spec = local_parametric.build_vllm_command()

    with pytest.raises(RuntimeError, match="body failed"):
        with local_parametric.managed_vllm_server(
            spec=spec,
            run_root=tmp_path,
            server_url="http://127.0.0.1:8000/v1",
            expected_model="Qwen/Qwen3.6-35B-A3B",
            timeout_seconds=1.0,
        ):
            raise RuntimeError("body failed")

    assert local_parametric.signal.SIGTERM in calls["signals"]
    assert local_parametric.signal.SIGKILL in calls["signals"]
    assert calls["signals"].count("wait") == 2


def test_managed_vllm_server_reports_teardown_timeout_when_body_succeeds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, object] = {"signals": []}

    class FakeProcess:
        pid = 4242
        returncode = None

        def poll(self) -> None:
            return None

        def wait(self, timeout: float | None = None) -> int:
            calls["signals"].append("wait")
            raise local_parametric.subprocess.TimeoutExpired("vllm", timeout)

    fake_process = FakeProcess()

    def fake_popen(command, *, cwd, env, stdout, stderr, start_new_session):
        return fake_process

    def fake_killpg(pid: int, kill_signal: int) -> None:
        calls["signals"].append(kill_signal)

    monkeypatch.setattr(local_parametric.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(local_parametric, "wait_for_openai_server", lambda **kwargs: None)
    monkeypatch.setattr(local_parametric.os, "killpg", fake_killpg)

    spec = local_parametric.build_vllm_command()

    with pytest.raises(RuntimeError, match="vLLM teardown.*process group.*timeout"):
        with local_parametric.managed_vllm_server(
            spec=spec,
            run_root=tmp_path,
            server_url="http://127.0.0.1:8000/v1",
            expected_model="Qwen/Qwen3.6-35B-A3B",
            timeout_seconds=1.0,
        ):
            pass

    assert local_parametric.signal.SIGTERM in calls["signals"]
    assert local_parametric.signal.SIGKILL in calls["signals"]
    assert calls["signals"].count("wait") == 2
