from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import polar_evolution.terminal_bench_local_parametric as local_parametric
from polar_evolution.terminal_bench_local_parametric import (
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


def test_build_evolab_harbor_env_sets_openai_chat_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("OPENAI_API_KEY", "host-secret")

    env = build_evolab_harbor_env(
        base_env={"PATH": "/usr/bin"},
        server_url="http://127.0.0.1:8000/v1",
        model="tb-parametric-memory",
    )

    assert env["PATH"] == "/usr/bin"
    assert env["EVOLAB_TB_LLM_API"] == "openai-chat-completions"
    assert env["EVOLAB_TB_MODEL"] == "tb-parametric-memory"
    assert env["OPENAI_BASE_URL"] == "http://127.0.0.1:8000/v1"
    assert env["AIGOCODE_GPT_BASE_URL"] == "http://127.0.0.1:8000/v1"
    assert env["OPENAI_API_KEY"] == "dummy-local-key"


def test_build_evolab_harbor_env_empty_base_does_not_inherit_host_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("POLAR_SENTINEL_HOST_ENV", "host-value")

    env = build_evolab_harbor_env(
        base_env={},
        server_url="http://127.0.0.1:8000/v1",
        model="tb-parametric-memory",
    )

    assert "POLAR_SENTINEL_HOST_ENV" not in env
    assert env["EVOLAB_TB_LLM_API"] == "openai-chat-completions"


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


def test_run_local_parametric_memory_eval_compares_baseline_and_adapter(
    tmp_path: Path,
) -> None:
    commands: list[list[str]] = []
    envs: list[dict[str, str]] = []

    def fake_command_runner(command, *, cwd=None, env=None):
        del cwd
        commands.append(command)
        envs.append(dict(env or {}))
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
        terminal_bench_package_root=tmp_path / "terminal-bench-package",
        model="Qwen/Qwen3.6-35B-A3B",
        adapter_path=tmp_path / "adapter",
        adapter_id="tb-parametric-memory",
        adapter_artifact_id="art-parametric",
        server_url="http://127.0.0.1:8000/v1",
        n_attempts=2,
        verifier_env={},
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
    assert f"tb-parametric-memory={tmp_path / 'adapter'}" in treatment_spec.command


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
