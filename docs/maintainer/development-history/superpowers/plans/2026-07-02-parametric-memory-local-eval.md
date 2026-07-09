# Parametric Memory Local Eval Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and verify a local/proxy Terminal Bench 2.1 path that compares Qwen local inference with no adapter against the same model with a `parametric_memory` LoRA adapter.

**Architecture:** Keep the existing Codex subscription runner unchanged and add a separate local parametric-memory runner. The new runner builds Harbor `mode=evolab` commands, manages or targets a local vLLM OpenAI-compatible endpoint, runs baseline and adapter conditions, and writes a summary with only `parametric_memory` enabled in the treatment condition.

**Tech Stack:** Python stdlib subprocess/process-groups, `httpx` for OpenAI-compatible health checks, Harbor Terminal Bench package, vLLM OpenAI-compatible server, existing `polar_evolution` artifact/job contracts.

---

## File Structure

- Create `src/polar_evolution/terminal_bench_local_parametric.py`: local/proxy Terminal Bench parametric-memory orchestration, vLLM command building, server lifecycle, Harbor env propagation, matrix summary.
- Modify `src/polar_evolution/terminal_bench_per_task.py`: only add parametric default metadata in generic artifact discovery if needed by the local path.
- Modify `src/polar_evolution/cli.py`: add `terminal-bench-parametric-memory-job` and `terminal-bench-local-parametric-memory-eval` subcommands.
- Create `tests/evolution/test_terminal_bench_local_parametric.py`: unit tests for command builders, env propagation, server lifecycle, dry-run, fake live orchestration, and CLI wiring.
- Modify `tests/evolution/test_terminal_bench_per_task.py`: add one regression that the existing subscription CLI still rejects `parametric_memory`.
- Modify `docs/dev/terminal-bench-memory-eval.md`: add local parametric-memory runbook and controlled-subset reporting instructions.

## Task 1: Local Harbor And vLLM Builder Tests

**Files:**
- Create: `tests/evolution/test_terminal_bench_local_parametric.py`
- Create later: `src/polar_evolution/terminal_bench_local_parametric.py`

- [ ] **Step 1: Write failing tests for command and env builders**

Create `tests/evolution/test_terminal_bench_local_parametric.py` with:

```python
from __future__ import annotations

import json
from pathlib import Path

import pytest

from polar_evolution.terminal_bench_local_parametric import (
    DEFAULT_LOCAL_PARAMETRIC_DISABLED_ARTIFACTS,
    LocalParametricCondition,
    build_evolab_harbor_env,
    build_local_harbor_command,
    build_vllm_command,
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


def test_build_evolab_harbor_env_sets_openai_chat_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
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
    assert baseline.command[:3] == ["/root/evolab-vllm/bin/vllm", "serve", "Qwen/Qwen3.6-35B-A3B"]
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


def test_local_parametric_dry_run_reports_matrix_and_disabled_artifacts(tmp_path: Path) -> None:
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
```

- [ ] **Step 2: Run the failing tests**

Run:

```bash
pytest tests/evolution/test_terminal_bench_local_parametric.py -q
```

Expected: import failure for `polar_evolution.terminal_bench_local_parametric`.

- [ ] **Step 3: Commit the failing tests if using strict TDD checkpointing**

```bash
git add tests/evolution/test_terminal_bench_local_parametric.py
git commit -m "test: cover local parametric memory builders"
```

## Task 2: Command Builders And Dry-Run Implementation

**Files:**
- Create: `src/polar_evolution/terminal_bench_local_parametric.py`
- Test: `tests/evolution/test_terminal_bench_local_parametric.py`

- [ ] **Step 1: Create the local parametric module**

Create `src/polar_evolution/terminal_bench_local_parametric.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json
import os
from typing import Any

from polar_evolution.terminal_bench_per_task import (
    DEFAULT_TERMINAL_BENCH_ENVIRONMENT_IMPORT_PATH,
    DEFAULT_TERMINAL_BENCH_EXTRA_DOCKER_COMPOSE,
    DEFAULT_TERMINAL_BENCH_PACKAGE_ROOT,
)

DEFAULT_LOCAL_MODEL = "Qwen/Qwen3.6-35B-A3B"
DEFAULT_LOCAL_PARAMETRIC_ADAPTER_ID = "tb-parametric-memory"
DEFAULT_LOCAL_PARAMETRIC_DISABLED_ARTIFACTS = [
    "text_memory",
    "skill_bundle",
    "agent_system",
]
DEFAULT_VLLM_EXECUTABLE = "/root/evolab-vllm/bin/vllm"
DEFAULT_VLLM_GPUS = ["1", "2", "3", "4"]


@dataclass(frozen=True)
class LocalParametricCondition:
    name: str
    model: str
    adapter_id: str | None = None
    adapter_path: Path | None = None


@dataclass(frozen=True)
class BuiltVLLMCommand:
    command: list[str]
    env: dict[str, str]


def build_local_harbor_command(
    *,
    job_name: str,
    task_root: Path,
    task_id: str,
    jobs_dir: Path,
    model: str,
    verifier_env: dict[str, str],
    n_attempts: int = 1,
    n_concurrent: int = 1,
    environment_import_path: str | None = DEFAULT_TERMINAL_BENCH_ENVIRONMENT_IMPORT_PATH,
    extra_docker_compose: list[Path] | None = None,
    preserve_environment: bool = True,
    cpu_policy: str | None = "ignore",
    memory_policy: str | None = "ignore",
) -> list[str]:
    command = [
        "harbor",
        "run",
        "--job-name",
        job_name,
        "--path",
        str(task_root),
        "--jobs-dir",
        str(jobs_dir),
        "--include-task-name",
        task_id,
        "--n-attempts",
        str(max(1, int(n_attempts))),
        "--n-concurrent",
        str(max(1, int(n_concurrent))),
        "--agent-import-path",
        "task_packages.terminal_bench_v1.harbor_agent:EvoLabHarborAgent",
        "--model",
        model,
        "--ak",
        "mode=evolab",
    ]
    if environment_import_path:
        command.extend(["--environment-import-path", environment_import_path])
    if preserve_environment:
        command.append("--no-delete")
    if cpu_policy:
        command.extend(["--cpus", cpu_policy])
    if memory_policy:
        command.extend(["--memory", memory_policy])
    compose_files = (
        list(extra_docker_compose)
        if extra_docker_compose is not None
        else list(DEFAULT_TERMINAL_BENCH_EXTRA_DOCKER_COMPOSE)
    )
    for compose_file in compose_files:
        command.extend(["--extra-docker-compose", str(compose_file)])
    for key in sorted(verifier_env):
        command.extend(["--verifier-env", f"{key}={verifier_env[key]}"])
    return command


def build_evolab_harbor_env(
    *,
    base_env: dict[str, str] | None = None,
    server_url: str,
    model: str,
) -> dict[str, str]:
    env = dict(base_env or os.environ)
    env["EVOLAB_TB_LLM_API"] = "openai-chat-completions"
    env["EVOLAB_TB_MODEL"] = model
    env["OPENAI_BASE_URL"] = server_url
    env["AIGOCODE_GPT_BASE_URL"] = server_url
    env["OPENAI_API_KEY"] = "dummy-local-key"
    return env


def build_vllm_command(
    *,
    model: str = DEFAULT_LOCAL_MODEL,
    served_model_name: str = DEFAULT_LOCAL_MODEL,
    port: int = 8000,
    tensor_parallel_size: int = 4,
    gpu_memory_utilization: float = 0.75,
    max_model_len: int = 16384,
    vllm_executable: str = DEFAULT_VLLM_EXECUTABLE,
    gpus: list[str] | None = None,
    adapter_id: str | None = None,
    adapter_path: Path | None = None,
) -> BuiltVLLMCommand:
    visible_gpus = ",".join(gpus or DEFAULT_VLLM_GPUS)
    command = [
        vllm_executable,
        "serve",
        model,
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--served-model-name",
        served_model_name,
        "--tensor-parallel-size",
        str(tensor_parallel_size),
        "--max-model-len",
        str(max_model_len),
        "--gpu-memory-utilization",
        str(gpu_memory_utilization),
        "--dtype",
        "bfloat16",
        "--reasoning-parser",
        "qwen3",
        "--language-model-only",
        "--enforce-eager",
        "--enable-auto-tool-choice",
        "--tool-call-parser",
        "qwen3_xml",
    ]
    if adapter_id is not None or adapter_path is not None:
        if not adapter_id or adapter_path is None:
            raise ValueError("adapter_id and adapter_path must be provided together")
        command.extend(
            [
                "--enable-lora",
                "--max-loras",
                "1",
                "--max-lora-rank",
                "64",
                "--lora-modules",
                f"{adapter_id}={adapter_path}",
            ]
        )
    return BuiltVLLMCommand(command=command, env={"CUDA_VISIBLE_DEVICES": visible_gpus})


def run_local_parametric_memory_eval_dry_run(
    *,
    task_root: Path,
    task_ids: list[str],
    run_root: Path,
    model: str,
    adapter_path: Path,
    adapter_id: str,
    server_url: str,
    n_attempts: int,
    manage_server: bool,
) -> dict[str, Any]:
    conditions = [
        LocalParametricCondition(name="baseline", model=model),
        LocalParametricCondition(
            name="parametric_memory",
            model=adapter_id,
            adapter_id=adapter_id,
            adapter_path=adapter_path,
        ),
    ]
    return {
        "dry_run": True,
        "benchmark": "terminal-bench-2.1",
        "auth_mode": "local",
        "task_root": str(task_root),
        "run_root": str(run_root),
        "base_model": model,
        "server_url": server_url,
        "n_attempts": max(1, int(n_attempts)),
        "manage_server": manage_server,
        "enabled_artifacts": ["parametric_memory"],
        "disabled_artifacts": list(DEFAULT_LOCAL_PARAMETRIC_DISABLED_ARTIFACTS),
        "conditions": [
            {
                "name": condition.name,
                "model": condition.model,
                "adapter_id": condition.adapter_id,
                "adapter_path": str(condition.adapter_path) if condition.adapter_path else None,
                "task_ids": list(task_ids),
            }
            for condition in conditions
        ],
    }
```

- [ ] **Step 2: Run builder tests**

Run:

```bash
pytest tests/evolution/test_terminal_bench_local_parametric.py -q
```

Expected: builder and dry-run tests pass; later tests are not present yet.

- [ ] **Step 3: Commit the builder implementation**

```bash
git add src/polar_evolution/terminal_bench_local_parametric.py tests/evolution/test_terminal_bench_local_parametric.py
git commit -m "feat: add local parametric memory command builders"
```

## Task 3: Managed vLLM Server Lifecycle

**Files:**
- Modify: `tests/evolution/test_terminal_bench_local_parametric.py`
- Modify: `src/polar_evolution/terminal_bench_local_parametric.py`

- [ ] **Step 1: Add fake-process tests for server lifecycle metadata and cleanup**

Append to `tests/evolution/test_terminal_bench_local_parametric.py`:

```python
from types import SimpleNamespace

import polar_evolution.terminal_bench_local_parametric as local_parametric


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
        calls["start_new_session"] = start_new_session
        return fake_process

    monkeypatch.setattr(local_parametric.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(local_parametric, "wait_for_openai_server", lambda **kwargs: None)
    monkeypatch.setattr(local_parametric.os, "killpg", lambda pid, signal: calls["killed"].append((pid, signal)))

    spec = local_parametric.build_vllm_command(
        model="Qwen/Qwen3.6-35B-A3B",
        served_model_name="Qwen/Qwen3.6-35B-A3B",
    )
    with local_parametric.managed_vllm_server(
        spec=spec,
        run_root=tmp_path,
        server_url="http://127.0.0.1:8000/v1",
        expected_model="Qwen/Qwen3.6-35B-A3B",
        timeout_seconds=1.0,
    ) as metadata:
        assert metadata["pid"] == 4242
        assert metadata["server_url"] == "http://127.0.0.1:8000/v1"
        assert (tmp_path / "vllm" / "server.json").is_file()

    assert calls["start_new_session"] is True
    assert calls["killed"]


def test_wait_for_openai_server_rejects_missing_expected_model(monkeypatch: pytest.MonkeyPatch) -> None:
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
```

- [ ] **Step 2: Run lifecycle tests to verify failure**

Run:

```bash
pytest tests/evolution/test_terminal_bench_local_parametric.py -k "managed_vllm or wait_for_openai" -q
```

Expected: failures for missing `managed_vllm_server` and `wait_for_openai_server`.

- [ ] **Step 3: Implement server lifecycle helpers**

Add imports and functions to `src/polar_evolution/terminal_bench_local_parametric.py`:

```python
from contextlib import contextmanager
import signal
import subprocess
import time

import httpx


def wait_for_openai_server(
    *,
    server_url: str,
    expected_model: str,
    timeout_seconds: float,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with httpx.Client(base_url=server_url, timeout=5.0) as client:
                models_response = client.get("/models")
                models_response.raise_for_status()
                payload = models_response.json()
                model_ids = [
                    str(item.get("id"))
                    for item in payload.get("data", [])
                    if isinstance(item, dict)
                ]
                if expected_model not in model_ids:
                    raise ValueError(
                        f"expected model {expected_model!r} not listed by {server_url}: {model_ids}"
                    )
                completion = client.post(
                    "/chat/completions",
                    json={
                        "model": expected_model,
                        "messages": [{"role": "user", "content": "Reply with ok."}],
                        "max_tokens": 4,
                        "temperature": 0,
                    },
                )
                completion.raise_for_status()
                return
        except ValueError:
            raise
        except Exception as exc:
            last_error = exc
            time.sleep(1.0)
    raise TimeoutError(
        f"OpenAI-compatible server at {server_url} did not become ready for "
        f"{expected_model!r} within {timeout_seconds:g} seconds"
    ) from last_error


def _redacted_env(env: dict[str, str]) -> dict[str, str]:
    redacted: dict[str, str] = {}
    for key, value in env.items():
        lowered = key.lower()
        if "key" in lowered or "token" in lowered or "secret" in lowered:
            redacted[key] = "<redacted>"
        else:
            redacted[key] = value
    return redacted


@contextmanager
def managed_vllm_server(
    *,
    spec: BuiltVLLMCommand,
    run_root: Path,
    server_url: str,
    expected_model: str,
    timeout_seconds: float,
):
    server_dir = run_root / "vllm"
    server_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = server_dir / "stdout.txt"
    stderr_path = server_dir / "stderr.txt"
    metadata_path = server_dir / "server.json"
    env = {**os.environ, **spec.env}
    stdout = stdout_path.open("w", encoding="utf-8")
    stderr = stderr_path.open("w", encoding="utf-8")
    process = subprocess.Popen(
        spec.command,
        cwd=run_root,
        env=env,
        stdout=stdout,
        stderr=stderr,
        start_new_session=True,
    )
    metadata = {
        "pid": process.pid,
        "command": list(spec.command),
        "env": _redacted_env(spec.env),
        "server_url": server_url,
        "expected_model": expected_model,
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
    }
    metadata_path.write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    try:
        wait_for_openai_server(
            server_url=server_url,
            expected_model=expected_model,
            timeout_seconds=timeout_seconds,
        )
        yield metadata
    finally:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=15)
        stdout.close()
        stderr.close()
```

- [ ] **Step 4: Run lifecycle tests**

Run:

```bash
pytest tests/evolution/test_terminal_bench_local_parametric.py -k "managed_vllm or wait_for_openai" -q
```

Expected: PASS.

- [ ] **Step 5: Commit server lifecycle helpers**

```bash
git add src/polar_evolution/terminal_bench_local_parametric.py tests/evolution/test_terminal_bench_local_parametric.py
git commit -m "feat: manage local vllm server lifecycle"
```

## Task 4: Fake Live Evaluation Orchestration

**Files:**
- Modify: `tests/evolution/test_terminal_bench_local_parametric.py`
- Modify: `src/polar_evolution/terminal_bench_local_parametric.py`

- [ ] **Step 1: Add fake Harbor orchestration test**

Append:

```python
def test_run_local_parametric_memory_eval_compares_baseline_and_adapter(tmp_path: Path) -> None:
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
            (trial / "verifier" / "reward.txt").write_text(f"{reward}\n", encoding="utf-8")
        return {}

    summary = local_parametric.run_local_parametric_memory_eval(
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
```

- [ ] **Step 2: Run the fake orchestration test**

Run:

```bash
pytest tests/evolution/test_terminal_bench_local_parametric.py::test_run_local_parametric_memory_eval_compares_baseline_and_adapter -q
```

Expected: FAIL because `run_local_parametric_memory_eval` is not implemented.

- [ ] **Step 3: Implement fakeable local evaluation**

Add to `src/polar_evolution/terminal_bench_local_parametric.py`:

```python
from polar_evolution.terminal_bench_per_task import (
    _attempt_reward,
    _locate_evolved_attempt_trials,
)


def _default_command_runner(command: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None) -> dict[str, Any]:
    import subprocess

    subprocess.run(command, cwd=cwd, env=env, check=True)
    return {}


def run_local_parametric_memory_eval(
    *,
    task_root: Path,
    task_ids: list[str],
    run_root: Path,
    terminal_bench_package_root: Path = DEFAULT_TERMINAL_BENCH_PACKAGE_ROOT,
    model: str = DEFAULT_LOCAL_MODEL,
    adapter_path: Path,
    adapter_id: str = DEFAULT_LOCAL_PARAMETRIC_ADAPTER_ID,
    adapter_artifact_id: str | None = None,
    server_url: str = "http://127.0.0.1:8000/v1",
    n_attempts: int = 1,
    verifier_env: dict[str, str] | None = None,
    command_runner=_default_command_runner,
    manage_server: bool = False,
    server_timeout_seconds: float = 600.0,
    vllm_executable: str = DEFAULT_VLLM_EXECUTABLE,
    gpus: list[str] | None = None,
    port: int = 8000,
) -> dict[str, Any]:
    if not adapter_path:
        raise ValueError("local parametric-memory eval requires adapter_path")
    run_root.mkdir(parents=True, exist_ok=True)
    attempt_count = max(1, int(n_attempts))
    conditions = [
        LocalParametricCondition(name="baseline", model=model),
        LocalParametricCondition(
            name="parametric_memory",
            model=adapter_id,
            adapter_id=adapter_id,
            adapter_path=adapter_path,
        ),
    ]
    summary: dict[str, Any] = {
        "dry_run": False,
        "benchmark": "terminal-bench-2.1",
        "auth_mode": "local",
        "task_root": str(task_root),
        "run_root": str(run_root),
        "base_model": model,
        "server_url": server_url,
        "n_attempts": attempt_count,
        "enabled_artifacts": ["parametric_memory"],
        "disabled_artifacts": list(DEFAULT_LOCAL_PARAMETRIC_DISABLED_ARTIFACTS),
        "conditions": [],
    }
    for condition in conditions:
        condition_summary = _run_local_condition(
            condition=condition,
            task_root=task_root,
            task_ids=task_ids,
            run_root=run_root / condition.name,
            terminal_bench_package_root=terminal_bench_package_root,
            server_url=server_url,
            n_attempts=attempt_count,
            verifier_env=verifier_env or {},
            command_runner=command_runner,
            manage_server=manage_server,
            server_timeout_seconds=server_timeout_seconds,
            model=model,
            adapter_artifact_id=adapter_artifact_id,
            vllm_executable=vllm_executable,
            gpus=gpus,
            port=port,
        )
        summary["conditions"].append(condition_summary)
    summary["delta"] = _condition_delta(summary["conditions"][0], summary["conditions"][1])
    return summary


def _run_local_condition(
    *,
    condition: LocalParametricCondition,
    task_root: Path,
    task_ids: list[str],
    run_root: Path,
    terminal_bench_package_root: Path,
    server_url: str,
    n_attempts: int,
    verifier_env: dict[str, str],
    command_runner,
    manage_server: bool,
    server_timeout_seconds: float,
    model: str,
    adapter_artifact_id: str | None,
    vllm_executable: str,
    gpus: list[str] | None,
    port: int,
) -> dict[str, Any]:
    run_root.mkdir(parents=True, exist_ok=True)
    server_metadata: dict[str, Any] | None = None
    if manage_server:
        spec = build_vllm_command(
            model=model,
            served_model_name=model,
            port=port,
            vllm_executable=vllm_executable,
            gpus=gpus,
            adapter_id=condition.adapter_id,
            adapter_path=condition.adapter_path,
        )
        with managed_vllm_server(
            spec=spec,
            run_root=run_root,
            server_url=server_url,
            expected_model=condition.model,
            timeout_seconds=server_timeout_seconds,
        ) as metadata:
            server_metadata = dict(metadata)
            return _run_local_condition_harbor(
                condition=condition,
                task_root=task_root,
                task_ids=task_ids,
                run_root=run_root,
                terminal_bench_package_root=terminal_bench_package_root,
                server_url=server_url,
                n_attempts=n_attempts,
                verifier_env=verifier_env,
                command_runner=command_runner,
                server_metadata=server_metadata,
                adapter_artifact_id=adapter_artifact_id,
            )
    return _run_local_condition_harbor(
        condition=condition,
        task_root=task_root,
        task_ids=task_ids,
        run_root=run_root,
        terminal_bench_package_root=terminal_bench_package_root,
        server_url=server_url,
        n_attempts=n_attempts,
        verifier_env=verifier_env,
        command_runner=command_runner,
        server_metadata=server_metadata,
        adapter_artifact_id=adapter_artifact_id,
    )


def _run_local_condition_harbor(
    *,
    condition: LocalParametricCondition,
    task_root: Path,
    task_ids: list[str],
    run_root: Path,
    terminal_bench_package_root: Path,
    server_url: str,
    n_attempts: int,
    verifier_env: dict[str, str],
    command_runner,
    server_metadata: dict[str, Any] | None,
    adapter_artifact_id: str | None,
) -> dict[str, Any]:
    tasks = []
    for task_id in task_ids:
        job_name = f"{task_id}-{condition.name}"
        jobs_dir = run_root / "harbor_jobs"
        command = build_local_harbor_command(
            job_name=job_name,
            task_root=task_root,
            task_id=task_id,
            jobs_dir=jobs_dir,
            model=condition.model,
            verifier_env=verifier_env,
            n_attempts=n_attempts,
            n_concurrent=1,
        )
        env = build_evolab_harbor_env(
            base_env=os.environ,
            server_url=server_url,
            model=condition.model,
        )
        command_runner(command, cwd=terminal_bench_package_root, env=env)
        attempts = _local_attempt_summaries(
            task_id=task_id,
            job_root=jobs_dir / job_name,
        )
        tasks.append(
            {
                "task_id": task_id,
                "attempts": attempts,
                "pass_at_1": _attempt_passed(attempts[:1]),
                "pass_at_k": _attempt_passed(attempts[:n_attempts]),
            }
        )
    condition_summary: dict[str, Any] = {
        "name": condition.name,
        "model": condition.model,
        "tasks": tasks,
        "pass_at_1": _pass_summary(tasks, "pass_at_1"),
        "pass_at_k": {**_pass_summary(tasks, "pass_at_k"), "k": n_attempts},
    }
    if server_metadata is not None:
        condition_summary["server"] = server_metadata
    if condition.adapter_id or condition.adapter_path:
        condition_summary["adapter"] = {
            "artifact_id": adapter_artifact_id,
            "adapter_id": condition.adapter_id,
            "path": str(condition.adapter_path) if condition.adapter_path else None,
        }
    return condition_summary


def _local_attempt_summaries(*, task_id: str, job_root: Path) -> list[dict[str, Any]]:
    trials = _locate_evolved_attempt_trials(task_id=task_id, job_root=job_root)
    return [
        {
            "attempt_index": index,
            "trial_dir": str(trial),
            "reward": _attempt_reward(trial),
        }
        for index, trial in enumerate(trials, start=1)
    ]


def _attempt_passed(attempts: list[dict[str, Any]]) -> bool:
    return any(
        isinstance(attempt.get("reward"), int | float) and float(attempt["reward"]) >= 1.0
        for attempt in attempts
    )


def _pass_summary(tasks: list[dict[str, Any]], key: str) -> dict[str, int]:
    return {
        "passed": sum(1 for task in tasks if task.get(key) is True),
        "total": len(tasks),
    }


def _condition_delta(baseline: dict[str, Any], treatment: dict[str, Any]) -> dict[str, int]:
    return {
        "pass_at_1": int(treatment["pass_at_1"]["passed"]) - int(baseline["pass_at_1"]["passed"]),
        "pass_at_k": int(treatment["pass_at_k"]["passed"]) - int(baseline["pass_at_k"]["passed"]),
    }
```

- [ ] **Step 4: Run local parametric tests**

Run:

```bash
pytest tests/evolution/test_terminal_bench_local_parametric.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit fakeable orchestration**

```bash
git add src/polar_evolution/terminal_bench_local_parametric.py tests/evolution/test_terminal_bench_local_parametric.py
git commit -m "feat: evaluate local parametric memory on terminal bench"
```

## Task 5: CLI For Local Evaluation

**Files:**
- Modify: `src/polar_evolution/cli.py`
- Modify: `tests/evolution/test_terminal_bench_local_parametric.py`

- [ ] **Step 1: Add CLI tests**

Append:

```python
import polar_evolution.cli as cli_module
from polar_evolution.cli import main


def test_terminal_bench_local_parametric_cli_dry_run_writes_output(tmp_path: Path) -> None:
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


def test_terminal_bench_local_parametric_cli_rejects_subscription_auth(tmp_path: Path) -> None:
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
            "--verifier-env",
            "UV_NO_INDEX=1",
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
    assert captured["verifier_env"] == {"UV_NO_INDEX": "1"}
    assert json.loads(output.read_text(encoding="utf-8")) == {
        "dry_run": False,
        "conditions": [],
    }
```

- [ ] **Step 2: Run CLI tests to verify failure**

Run:

```bash
pytest tests/evolution/test_terminal_bench_local_parametric.py -k "cli" -q
```

Expected: FAIL because the CLI subcommand does not exist.

- [ ] **Step 3: Wire CLI parser and main branch**

In `src/polar_evolution/cli.py`, add imports:

```python
from polar_evolution.terminal_bench_local_parametric import (
    DEFAULT_LOCAL_MODEL,
    DEFAULT_LOCAL_PARAMETRIC_ADAPTER_ID,
    DEFAULT_VLLM_EXECUTABLE,
    run_local_parametric_memory_eval,
    run_local_parametric_memory_eval_dry_run,
)
```

In `build_parser()`, add:

```python
    tb_local_parametric = subparsers.add_parser(
        "terminal-bench-local-parametric-memory-eval",
        help="Run or plan local/proxy Terminal Bench parametric-memory evaluation.",
    )
    tb_local_parametric.add_argument("--task-root", required=True)
    tb_local_parametric.add_argument("--task-id", action="append", default=[], required=True)
    tb_local_parametric.add_argument("--run-root", required=True)
    tb_local_parametric.add_argument(
        "--terminal-bench-package-root",
        default=str(DEFAULT_TERMINAL_BENCH_PACKAGE_ROOT),
    )
    tb_local_parametric.add_argument("--model", default=DEFAULT_LOCAL_MODEL)
    tb_local_parametric.add_argument("--adapter-path", required=True)
    tb_local_parametric.add_argument(
        "--adapter-id",
        default=DEFAULT_LOCAL_PARAMETRIC_ADAPTER_ID,
    )
    tb_local_parametric.add_argument("--adapter-artifact-id")
    tb_local_parametric.add_argument("--server-url", default="http://127.0.0.1:8000/v1")
    tb_local_parametric.add_argument("--server-port", type=int, default=8000)
    tb_local_parametric.add_argument("--vllm-executable", default=DEFAULT_VLLM_EXECUTABLE)
    tb_local_parametric.add_argument("--gpu", action="append", default=[])
    tb_local_parametric.add_argument("--n-attempts", type=int, default=1)
    tb_local_parametric.add_argument("--manage-server", action="store_true")
    tb_local_parametric.add_argument("--server-timeout-seconds", type=float, default=600.0)
    tb_local_parametric.add_argument(
        "--auth-mode",
        choices=["local", "proxy", "subscription"],
        default="local",
    )
    tb_local_parametric.add_argument("--verifier-env", action="append", default=[])
    tb_local_parametric.add_argument("--dry-run", action="store_true")
    tb_local_parametric.add_argument("--output", required=True)
```

In `main()`, before the group-evolution branch, add:

```python
    if args.command == "terminal-bench-local-parametric-memory-eval":
        if args.auth_mode == "subscription":
            raise ValueError("parametric_memory requires local or proxy auth")
        if args.dry_run:
            payload = run_local_parametric_memory_eval_dry_run(
                task_root=Path(args.task_root),
                task_ids=args.task_id,
                run_root=Path(args.run_root),
                model=args.model,
                adapter_path=Path(args.adapter_path),
                adapter_id=args.adapter_id,
                server_url=args.server_url,
                n_attempts=args.n_attempts,
                manage_server=args.manage_server,
            )
            _write_json_output(payload, args.output)
            return 0
        payload = run_local_parametric_memory_eval(
            task_root=Path(args.task_root),
            task_ids=args.task_id,
            run_root=Path(args.run_root),
            terminal_bench_package_root=Path(args.terminal_bench_package_root),
            model=args.model,
            adapter_path=Path(args.adapter_path),
            adapter_id=args.adapter_id,
            adapter_artifact_id=args.adapter_artifact_id,
            server_url=args.server_url,
            n_attempts=args.n_attempts,
            verifier_env=_parse_key_value_entries(args.verifier_env),
            manage_server=args.manage_server,
            server_timeout_seconds=args.server_timeout_seconds,
            vllm_executable=args.vllm_executable,
            gpus=args.gpu or None,
            port=args.server_port,
        )
        _write_json_output(payload, args.output)
        return 0
```

- [ ] **Step 4: Run CLI tests**

Run:

```bash
pytest tests/evolution/test_terminal_bench_local_parametric.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit CLI eval wiring**

```bash
git add src/polar_evolution/cli.py src/polar_evolution/terminal_bench_local_parametric.py tests/evolution/test_terminal_bench_local_parametric.py
git commit -m "feat: expose local parametric memory eval cli"
```

## Task 6: Parametric Memory Job CLI

**Files:**
- Modify: `src/polar_evolution/cli.py`
- Modify: `tests/evolution/test_terminal_bench_local_parametric.py`

- [ ] **Step 1: Add CLI tests for creating a LoRA SFT job from Terminal Bench inputs**

Append:

```python
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
            "--output",
            str(output),
        ]
    )

    assert exit_code == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["job"]["method"] == "parametric_memory_lora_sft"
    assert payload["job"]["config"]["base_model"] == "Qwen/Qwen3.6-35B-A3B"
    assert payload["job"]["config"]["output_adapter_id"] == "tb-parametric-memory"
    assert payload["job"]["config"]["trainer"]["command"] == "python"
    assert "{training_dataset}" in payload["job"]["config"]["trainer"]["args"]
    assert "{adapter_dir}" in payload["job"]["config"]["trainer"]["args"]
    assert "terminal-bench:query-optimize" in payload["job"]["config"]["compatibility"]["task_tags"]


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
    assert payload["completed_artifacts"][0]["manifest"]["adapter_id"] == "tb-parametric-memory"
```

- [ ] **Step 2: Run the job CLI test to verify failure**

Run:

```bash
pytest tests/evolution/test_terminal_bench_local_parametric.py -k "parametric_memory_job" -q
```

Expected: FAIL because `terminal-bench-parametric-memory-job` is not registered.

- [ ] **Step 3: Add parser and job creation function**

In `build_parser()`, add:

```python
    tb_parametric_job = subparsers.add_parser(
        "terminal-bench-parametric-memory-job",
        help="Ingest Terminal Bench results and create a parametric-memory LoRA SFT job.",
    )
    tb_parametric_job.add_argument("--input", action="append", default=[])
    tb_parametric_job.add_argument("--dataset-artifact-id", action="append", default=[])
    tb_parametric_job.add_argument("--db", default=".polar_evolution/evolution.db")
    tb_parametric_job.add_argument("--artifact-root", default=".polar_evolution")
    tb_parametric_job.add_argument("--dataset-name")
    tb_parametric_job.add_argument("--purpose", default="parametric_memory_lora_sft")
    tb_parametric_job.add_argument("--policy-version")
    tb_parametric_job.add_argument("--rollout-step", type=int)
    tb_parametric_job.add_argument("--status", action="append", default=["COMPLETED"])
    tb_parametric_job.add_argument("--output", help="Output JSON summary path. Defaults to stdout.")
    tb_parametric_job.add_argument("--max-transcript-chars", type=int, default=60000)
    tb_parametric_job.add_argument("--max-verifier-stdout-chars", type=int, default=12000)
    tb_parametric_job.add_argument("--base-model", required=True)
    tb_parametric_job.add_argument("--adapter-id", default=DEFAULT_LOCAL_PARAMETRIC_ADAPTER_ID)
    tb_parametric_job.add_argument("--adapter-format", default="lora")
    tb_parametric_job.add_argument("--trainer-command", required=True)
    tb_parametric_job.add_argument("--trainer-arg", action="append", default=[])
    tb_parametric_job.add_argument("--trainer-timeout-seconds", type=float, default=3600.0)
    tb_parametric_job.add_argument("--run-worker", action="store_true")
    tb_parametric_job.add_argument("--job-name")
    tb_parametric_job.add_argument("--priority", type=int, default=100)
    tb_parametric_job.add_argument("--max-records", type=int)
```

In `main()`, add:

```python
    if args.command == "terminal-bench-parametric-memory-job":
        payload = _create_terminal_bench_parametric_memory_job(args)
        _write_json_output(payload, args.output)
        return 0
```

Also import the local worker helper from the Terminal Bench per-task module:

```python
from polar_evolution.terminal_bench_per_task import (
    DEFAULT_TERMINAL_BENCH_PACKAGE_ROOT,
    TerminalBenchTaskGroup,
    _run_worker_once_local,
    run_group_evolution,
    run_group_evolution_dry_run,
    run_per_task_evolution,
    run_per_task_evolution_dry_run,
)
```

Add `_create_terminal_bench_parametric_memory_job()` near the existing job helpers:

```python
def _create_terminal_bench_parametric_memory_job(args: argparse.Namespace) -> dict[str, Any]:
    if not args.input and not args.dataset_artifact_id:
        raise ValueError("terminal-bench-parametric-memory-job requires --input or --dataset-artifact-id")
    if args.input and not args.dataset_name:
        raise ValueError("terminal-bench-parametric-memory-job requires --dataset-name with --input")
    if args.input and not args.policy_version:
        raise ValueError("terminal-bench-parametric-memory-job requires --policy-version with --input")
    trainer_args = [str(arg) for arg in args.trainer_arg]
    if not any("{training_dataset}" in arg for arg in trainer_args) or not any(
        "{adapter_dir}" in arg for arg in trainer_args
    ):
        raise ValueError("terminal-bench-parametric-memory-job trainer args must include {training_dataset} and {adapter_dir}")

    store = EvolutionStore(db_path=Path(args.db), artifact_root=Path(args.artifact_root))
    store.initialize()
    events: list[EventIngestRequest] = []
    for input_path in args.input:
        events.extend(
            build_terminal_bench_events(
                input_path,
                max_transcript_chars=args.max_transcript_chars,
                max_verifier_stdout_chars=args.max_verifier_stdout_chars,
                policy_version=args.policy_version,
                rollout_step=args.rollout_step,
            )
        )
    ingested_events = []
    for event in events:
        response = store.ingest_event(event)
        ingested_events.append(
            {
                "event_id": response.event_id,
                "ingested": response.ingested,
                "duplicate": response.duplicate,
                "task_id": event.task_id,
                "session_id": event.session_id,
            }
        )
    input_artifact_ids = list(args.dataset_artifact_id)
    dataset_payload: dict[str, Any] | None = None
    if events:
        dataset = store.create_dataset(
            DatasetCreateRequest(
                name=args.dataset_name,
                purpose=args.purpose,
                query={
                    "event_types": ["polar.session_completed"],
                    "status": args.status,
                    "policy_version": args.policy_version,
                },
            )
        )
        dataset_payload = {
            "dataset_id": dataset.dataset_id,
            "artifact_id": dataset.artifact_id,
            "name": args.dataset_name,
            "purpose": args.purpose,
            "event_count": dataset.event_count,
            "trace_count": dataset.trace_count,
            "manifest_uri": _artifact_uri(store, dataset.artifact_id),
        }
        input_artifact_ids.append(dataset.artifact_id)
    config: dict[str, Any] = {
        "name": args.job_name or "Terminal Bench parametric-memory LoRA SFT",
        "base_model": args.base_model,
        "output_adapter_id": args.adapter_id,
        "adapter_format": args.adapter_format,
        "trainer": {
            "command": args.trainer_command,
            "args": trainer_args,
            "timeout_seconds": args.trainer_timeout_seconds,
        },
        "compatibility": {
            "agent_harness": ["terminal-bench-harbor"],
            "task_tags": _terminal_bench_task_tags(store, input_artifact_ids, events),
            "base_model": [args.base_model],
        },
        "scores": {"quality": 0.0},
        "promoted": False,
    }
    if args.max_records is not None:
        config["max_records"] = args.max_records
    job = store.create_job(
        JobCreateRequest(
            method="parametric_memory_lora_sft",
            job_type="parametric_memory_lora_sft",
            input_artifact_ids=input_artifact_ids,
            config=config,
            priority=args.priority,
        )
    )
    payload = {
        "ingested_events": ingested_events,
        "dataset": dataset_payload,
        "job": {
            "job_id": job.job_id,
            "state": str(job.state),
            "job_type": "parametric_memory_lora_sft",
            "method": "parametric_memory_lora_sft",
            "input_artifact_ids": input_artifact_ids,
            "config": config,
        },
    }
    if args.run_worker:
        payload["completed_artifacts"] = _run_worker_once_local(
            db_path=Path(args.db),
            artifact_root=Path(args.artifact_root),
        )
    return payload
```

- [ ] **Step 4: Run job CLI tests and worker parametric tests**

Run:

```bash
pytest tests/evolution/test_terminal_bench_local_parametric.py -k "parametric_memory_job" -q
pytest tests/evolution/test_worker_methods.py -k "parametric_memory_lora_sft or parametric_memory_register" -q
```

Expected: PASS.

- [ ] **Step 5: Commit parametric job CLI**

```bash
git add src/polar_evolution/cli.py tests/evolution/test_terminal_bench_local_parametric.py
git commit -m "feat: create terminal bench parametric memory jobs"
```

## Task 7: Keep Subscription Rejection Explicit

**Files:**
- Modify: `tests/evolution/test_terminal_bench_per_task.py`
- Modify if needed: `src/polar_evolution/terminal_bench_per_task.py`

- [ ] **Step 1: Add regression that the existing subscription runner still rejects parametric memory**

Append beside the existing rejection tests if no direct function-level test exists:

```python
def test_subscription_live_artifact_type_rejects_parametric_memory_directly() -> None:
    with pytest.raises(ValueError, match="Codex subscription runs do not support parametric_memory"):
        per_task_module._single_live_artifact_type(["parametric_memory"])
```

- [ ] **Step 2: Run the regression**

Run:

```bash
pytest tests/evolution/test_terminal_bench_per_task.py -k "parametric_memory" -q
```

Expected: PASS. If it fails because of wording, adjust only the assertion to match the current explicit rejection.

- [ ] **Step 3: Commit the regression if it was added**

```bash
git add tests/evolution/test_terminal_bench_per_task.py
git commit -m "test: keep subscription parametric memory rejection explicit"
```

## Task 8: Documentation And Runbook

**Files:**
- Modify: `docs/dev/terminal-bench-memory-eval.md`

- [ ] **Step 1: Add a local parametric-memory section**

Append to the `## Parametric Memory` section:

```markdown
### Local Qwen/vLLM Evaluation Path

Parametric memory evaluation uses Harbor `mode=evolab` and an OpenAI-compatible
local endpoint. It does not use Codex subscription. Keep text memory,
skill bundles, and agent-system evolution disabled for the controlled
parametric-memory comparison.

Preflight:

```sh
du -sh /root/.cache/huggingface/hub/models--Qwen--Qwen3.6-35B-A3B
/root/evolab-vllm/bin/vllm --version
nvidia-smi --query-gpu=index,name,memory.used,memory.total,utilization.gpu --format=csv,noheader,nounits
```

The first controlled subset is:

```text
train-fasttext
query-optimize
make-mips-interpreter
```

Create a parametric-memory job from successful Terminal Bench trajectories and
run the local worker once in the same process:

```sh
uv run polar-evolution terminal-bench-parametric-memory-job \
  --input /tmp/tb21-text-memory-train-fasttext-20260701-073824/run/tasks/train-fasttext/r1/harbor_jobs/train-fasttext-r1/train-fasttext__attempt1 \
  --input /tmp/tb21-text-memory-query-optimize-20260701-021002/run/tasks/query-optimize/r1/harbor_jobs/query-optimize-r1/query-optimize__attempt1 \
  --input /tmp/tb21-text-memory-mips-interpreter-20260701-040524/run/tasks/make-mips-interpreter/r1/harbor_jobs/make-mips-interpreter-r1/make-mips-interpreter__attempt2 \
  --db /tmp/tb21-parametric-memory/evolution.db \
  --artifact-root /tmp/tb21-parametric-memory/artifacts \
  --dataset-name tb21-parametric-memory-subset \
  --policy-version tb21-qwen36-local-parametric-memory \
  --base-model Qwen/Qwen3.6-35B-A3B \
  --adapter-id tb-parametric-memory \
  --trainer-command python \
  --trainer-arg /path/to/train_lora.py \
  --trainer-arg --train-file \
  --trainer-arg '{training_dataset}' \
  --trainer-arg --output-dir \
  --trainer-arg '{adapter_dir}' \
  --run-worker \
  --output /tmp/tb21-parametric-memory/job.json
```

Evaluate baseline local Qwen and adapter local Qwen:

```sh
uv run polar-evolution terminal-bench-local-parametric-memory-eval \
  --task-root /root/datasets/terminal-bench-2-1/tasks \
  --task-id train-fasttext \
  --task-id query-optimize \
  --task-id make-mips-interpreter \
  --run-root /tmp/tb21-parametric-memory/local-eval \
  --model Qwen/Qwen3.6-35B-A3B \
  --adapter-path /tmp/tb21-parametric-memory/artifacts/workers/<job-id>/parametric_memory_lora_sft/adapter \
  --adapter-id tb-parametric-memory \
  --adapter-artifact-id <artifact-id> \
  --gpu 1 \
  --gpu 2 \
  --gpu 3 \
  --gpu 4 \
  --server-port 8000 \
  --manage-server \
  --n-attempts 5 \
  --output /tmp/tb21-parametric-memory/local-eval/summary.json
```

The summary reports `baseline`, `parametric_memory`, and `delta` sections.
Treat this as controlled-subset evidence until the same path is run across the
full Terminal Bench 2.1 task set.
```

- [ ] **Step 2: Run docs grep and focused tests**

Run:

```bash
rg -n "terminal-bench-local-parametric-memory-eval|terminal-bench-parametric-memory-job" docs/dev/terminal-bench-memory-eval.md
pytest tests/evolution/test_terminal_bench_local_parametric.py -q
git diff --check
```

Expected: docs entries found, tests pass, and `git diff --check` reports no whitespace errors.

- [ ] **Step 3: Commit docs**

```bash
git add docs/dev/terminal-bench-memory-eval.md
git commit -m "docs: add local parametric memory eval runbook"
```

## Task 9: Verification Before PR

**Files:**
- No new edits unless verification finds a defect.

- [ ] **Step 1: Run focused tests**

Run:

```bash
pytest tests/evolution/test_terminal_bench_local_parametric.py -q
pytest tests/evolution/test_terminal_bench_per_task.py -k "parametric_memory or text_memory_orchestration or build_harbor_command" -q
pytest tests/evolution/test_worker_methods.py -k "parametric_memory_lora_sft or parametric_memory_register" -q
pytest tests/gateway/test_server_parametric_memory.py -q
```

Expected: all PASS.

- [ ] **Step 2: Run patch checks**

Run:

```bash
git diff --check
git status --short
git diff
```

Expected: no whitespace errors; status includes only intended files; diff matches this plan.

- [ ] **Step 3: Run local environment preflight without launching the long benchmark**

Run:

```bash
du -sh /root/.cache/huggingface/hub/models--Qwen--Qwen3.6-35B-A3B
/root/evolab-vllm/bin/vllm --version
nvidia-smi --query-gpu=index,name,memory.used,memory.total,utilization.gpu --format=csv,noheader,nounits
```

Expected:

- model cache is present and large enough to be the full `Qwen/Qwen3.6-35B-A3B`;
- vLLM reports a usable version;
- GPUs `1,2,3,4` are available enough for the planned server;
- GPU `5` is not selected.

- [ ] **Step 4: Commit any verification-only doc corrections**

If no edits were made during verification, skip this commit. If a small doc correction was made:

```bash
git add docs/dev/terminal-bench-memory-eval.md
git commit -m "docs: clarify parametric memory verification"
```

## Task 10: Real Controlled-Subset Experiment

**Files:**
- No repository files should be edited unless command output reveals a code or docs defect.

- [ ] **Step 1: Create a timestamped run root**

Run:

```bash
RUN_ROOT=/tmp/tb21-parametric-memory-$(date -u +%Y%m%d-%H%M%S)
mkdir -p "$RUN_ROOT"
echo "$RUN_ROOT"
```

Expected: a unique `/tmp/tb21-parametric-memory-...` path.

- [ ] **Step 2: Create or identify the adapter path**

If a trained adapter already exists from the parametric-memory job, write its path:

```bash
ADAPTER_PATH=/path/to/adapter
test -f "$ADAPTER_PATH/adapter_config.json"
```

Expected: `adapter_config.json` exists.

If no adapter exists, run the job command documented in Task 8 with `--run-worker`
after choosing the trainer script path and confirming trainer dependencies are
installed in an isolated environment.

- [ ] **Step 3: Run a dry-run of the local eval command**

Run:

```bash
uv run polar-evolution terminal-bench-local-parametric-memory-eval \
  --task-root /root/datasets/terminal-bench-2-1/tasks \
  --task-id train-fasttext \
  --task-id query-optimize \
  --task-id make-mips-interpreter \
  --run-root "$RUN_ROOT/local-eval" \
  --model Qwen/Qwen3.6-35B-A3B \
  --adapter-path "$ADAPTER_PATH" \
  --adapter-id tb-parametric-memory \
  --gpu 1 \
  --gpu 2 \
  --gpu 3 \
  --gpu 4 \
  --server-port 8000 \
  --manage-server \
  --n-attempts 5 \
  --dry-run \
  --output "$RUN_ROOT/dry-run.json"
```

Expected: output JSON has baseline and parametric-memory conditions and disabled artifacts `["text_memory", "skill_bundle", "agent_system"]`.

- [ ] **Step 4: Run the real local eval**

Run:

```bash
uv run polar-evolution terminal-bench-local-parametric-memory-eval \
  --task-root /root/datasets/terminal-bench-2-1/tasks \
  --task-id train-fasttext \
  --task-id query-optimize \
  --task-id make-mips-interpreter \
  --run-root "$RUN_ROOT/local-eval" \
  --model Qwen/Qwen3.6-35B-A3B \
  --adapter-path "$ADAPTER_PATH" \
  --adapter-id tb-parametric-memory \
  --gpu 1 \
  --gpu 2 \
  --gpu 3 \
  --gpu 4 \
  --server-port 8000 \
  --manage-server \
  --n-attempts 5 \
  --output "$RUN_ROOT/summary.json"
```

Expected: summary JSON contains baseline, treatment, and delta. vLLM server metadata and logs are under the run root.

- [ ] **Step 5: Verify cleanup and summarize results**

Run:

```bash
nvidia-smi --query-gpu=index,memory.used,utilization.gpu --format=csv,noheader,nounits
python -m json.tool "$RUN_ROOT/summary.json" | sed -n '1,220p'
```

Expected: no lingering vLLM process on GPUs `1,2,3,4` after teardown; summary shows measured pass@1/pass@5 for baseline and parametric memory on the three-task subset.
