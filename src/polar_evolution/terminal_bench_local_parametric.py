from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
import json
import os
from pathlib import Path
import signal
import subprocess
import time
from typing import Any

import httpx

from polar_evolution.terminal_bench_per_task import (
    DEFAULT_TERMINAL_BENCH_ENVIRONMENT_IMPORT_PATH,
    DEFAULT_TERMINAL_BENCH_EXTRA_DOCKER_COMPOSE,
    DEFAULT_TERMINAL_BENCH_PACKAGE_ROOT,
    _attempt_reward,
    _locate_evolved_attempt_trials,
)

DEFAULT_LOCAL_MODEL = "Qwen/Qwen3.6-35B-A3B"
DEFAULT_LOCAL_PARAMETRIC_ADAPTER_ID = "tb-parametric-memory"
DEFAULT_LOCAL_PARAMETRIC_DISABLED_ARTIFACTS = [
    "text_memory",
    "skill_bundle",
    "agent_system",
]
LOCAL_PARAMETRIC_AUTH_MODES = {"local", "proxy"}
DEFAULT_VLLM_EXECUTABLE = "/root/evolab-vllm/bin/vllm"
DEFAULT_VLLM_GPUS = ["1", "2", "3", "4"]
DEFAULT_LOCAL_PARAMETRIC_MAX_OUTPUT_TOKENS = 4096
_SECRET_ENV_MARKERS = (
    "key",
    "token",
    "secret",
    "password",
    "pass",
    "auth",
    "authorization",
    "cookie",
    "credential",
)


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


CommandRunner = Callable[..., Any]


def _default_command_runner(command, *, cwd=None, env=None):
    return subprocess.run(command, cwd=cwd, env=env, check=True)


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
    max_output_tokens: int = DEFAULT_LOCAL_PARAMETRIC_MAX_OUTPUT_TOKENS,
) -> dict[str, str]:
    env = dict(os.environ if base_env is None else base_env)
    env["EVOLAB_TB_LLM_API"] = "openai-chat-completions"
    env["EVOLAB_TB_MODE"] = "direct_solver"
    env["EVOLAB_TB_MAX_OUTPUT_TOKENS"] = str(max(1, int(max_output_tokens)))
    env["EVOLAB_TB_MODEL"] = model
    env["OPENAI_BASE_URL"] = server_url
    env["AIGOCODE_GPT_BASE_URL"] = server_url
    env["OPENAI_API_KEY"] = "dummy-local-key"
    return env


def _path_with_executable_parent(executable: str, base_path: str | None) -> str | None:
    executable_path = Path(executable)
    if not executable_path.is_absolute():
        return None
    executable_parent = str(executable_path.parent)
    path_parts = [
        part for part in (base_path or "").split(os.pathsep) if part and part != executable_parent
    ]
    return os.pathsep.join([executable_parent, *path_parts])


def build_vllm_command(
    *,
    model: str = DEFAULT_LOCAL_MODEL,
    served_model_name: str = DEFAULT_LOCAL_MODEL,
    port: int = 8000,
    tensor_parallel_size: int | None = None,
    gpu_memory_utilization: float = 0.75,
    max_model_len: int = 16384,
    vllm_executable: str = DEFAULT_VLLM_EXECUTABLE,
    gpus: list[str] | None = None,
    adapter_id: str | None = None,
    adapter_path: Path | None = None,
) -> BuiltVLLMCommand:
    if adapter_id is not None or adapter_path is not None:
        if not adapter_id or adapter_path is None:
            raise ValueError("adapter_id and adapter_path must be provided together")

    selected_gpus = list(DEFAULT_VLLM_GPUS if gpus is None else gpus)
    if not selected_gpus:
        raise ValueError("at least one GPU must be selected")
    effective_tensor_parallel_size = (
        len(selected_gpus) if tensor_parallel_size is None else int(tensor_parallel_size)
    )
    if effective_tensor_parallel_size < 1:
        raise ValueError("tensor_parallel_size must be at least 1")
    if len(selected_gpus) < effective_tensor_parallel_size:
        raise ValueError(
            "tensor_parallel_size cannot exceed selected GPU count: "
            f"tensor_parallel_size={effective_tensor_parallel_size}, "
            f"gpus={len(selected_gpus)}"
        )

    visible_gpus = ",".join(selected_gpus)
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
        str(effective_tensor_parallel_size),
        "--gpu-memory-utilization",
        str(gpu_memory_utilization),
        "--max-model-len",
        str(max_model_len),
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
    if adapter_id and adapter_path is not None:
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
    env = {"CUDA_VISIBLE_DEVICES": visible_gpus}
    executable_path = _path_with_executable_parent(vllm_executable, os.environ.get("PATH"))
    if executable_path is not None:
        env["PATH"] = executable_path
    return BuiltVLLMCommand(command=command, env=env)


def _redacted_env(env: dict[str, str]) -> dict[str, str]:
    redacted: dict[str, str] = {}
    for key in sorted(env):
        normalized_key = key.lower()
        if any(marker in normalized_key for marker in _SECRET_ENV_MARKERS):
            redacted[key] = "<redacted>"
        else:
            redacted[key] = env[key]
    return redacted


def _raise_if_process_exited(
    *,
    process_poll: Callable[[], int | None] | None,
    process_exit_message: Callable[[int], str] | None,
) -> None:
    if process_poll is None:
        return
    return_code = process_poll()
    if return_code is None:
        return
    if process_exit_message is None:
        raise RuntimeError(f"process exited during startup with return code {return_code}")
    raise RuntimeError(process_exit_message(return_code))


def wait_for_openai_server(
    *,
    server_url: str,
    expected_model: str,
    timeout_seconds: float,
    poll_interval_seconds: float = 1.0,
    process_poll: Callable[[], int | None] | None = None,
    process_exit_message: Callable[[int], str] | None = None,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    last_error: Exception | None = None

    while True:
        _raise_if_process_exited(
            process_poll=process_poll,
            process_exit_message=process_exit_message,
        )
        try:
            with httpx.Client(
                base_url=server_url,
                timeout=5.0,
                trust_env=False,
            ) as client:
                models_response = client.get("/models")
                models_response.raise_for_status()
                model_ids = {
                    model["id"]
                    for model in models_response.json().get("data", [])
                    if isinstance(model, dict) and isinstance(model.get("id"), str)
                }
                if expected_model not in model_ids:
                    raise ValueError(
                        f"OpenAI server did not expose expected model "
                        f"{expected_model!r}; available models: {sorted(model_ids)!r}"
                    )

                _raise_if_process_exited(
                    process_poll=process_poll,
                    process_exit_message=process_exit_message,
                )
                completion_response = client.post(
                    "/chat/completions",
                    json={
                        "model": expected_model,
                        "messages": [{"role": "user", "content": "ping"}],
                        "max_tokens": 1,
                    },
                )
                completion_response.raise_for_status()
                return
        except ValueError:
            raise
        except (httpx.HTTPError, OSError) as exc:
            last_error = exc
            _raise_if_process_exited(
                process_poll=process_poll,
                process_exit_message=process_exit_message,
            )
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"Timed out waiting for OpenAI server at {server_url!r}"
                ) from last_error
            time.sleep(min(poll_interval_seconds, max(0.0, deadline - time.monotonic())))


def _terminate_process_group(
    process: subprocess.Popen[Any],
    *,
    wait_timeout_seconds: float = 10.0,
) -> RuntimeError | None:
    if process.poll() is not None:
        return None

    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass

    try:
        process.wait(timeout=wait_timeout_seconds)
        return None
    except subprocess.TimeoutExpired:
        pass

    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass

    try:
        process.wait(timeout=wait_timeout_seconds)
    except subprocess.TimeoutExpired:
        return RuntimeError(
            "vLLM teardown for process group "
            f"{process.pid} hit timeout after SIGKILL"
        )
    return None


@contextmanager
def managed_vllm_server(
    *,
    spec: BuiltVLLMCommand,
    run_root: Path,
    server_url: str,
    expected_model: str,
    timeout_seconds: float,
) -> Iterator[dict[str, Any]]:
    vllm_root = run_root / "vllm"
    vllm_root.mkdir(parents=True, exist_ok=True)
    stdout_path = vllm_root / "stdout.log"
    stderr_path = vllm_root / "stderr.log"
    metadata_path = vllm_root / "server.json"

    env = dict(os.environ)
    env.update(spec.env)
    stdout_handle = stdout_path.open("w")
    stderr_handle = stderr_path.open("w")
    process: subprocess.Popen[Any] | None = None
    original_exception = False

    try:
        process = subprocess.Popen(
            spec.command,
            cwd=run_root,
            env=env,
            stdout=stdout_handle,
            stderr=stderr_handle,
            start_new_session=True,
        )
        metadata: dict[str, Any] = {
            "pid": process.pid,
            "command": list(spec.command),
            "env": _redacted_env(spec.env),
            "server_url": server_url,
            "expected_model": expected_model,
            "stdout_path": str(stdout_path),
            "stderr_path": str(stderr_path),
        }
        metadata_path.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n")

        wait_for_openai_server(
            server_url=server_url,
            expected_model=expected_model,
            timeout_seconds=timeout_seconds,
            process_poll=process.poll,
            process_exit_message=lambda return_code: (
                "vLLM server exited during startup with return code "
                f"{return_code}; stdout={stdout_path}; stderr={stderr_path}"
            ),
        )
        yield metadata
    except BaseException:
        original_exception = True
        raise
    finally:
        stdout_handle.close()
        stderr_handle.close()
        if process is not None:
            teardown_error = _terminate_process_group(process)
            if teardown_error is not None and not original_exception:
                raise teardown_error


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
    max_output_tokens: int = DEFAULT_LOCAL_PARAMETRIC_MAX_OUTPUT_TOKENS,
    auth_mode: str = "local",
) -> dict[str, Any]:
    auth_mode = _validate_auth_mode(auth_mode)
    output_token_cap = max(1, int(max_output_tokens))
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
        "auth_mode": auth_mode,
        "task_root": str(task_root),
        "run_root": str(run_root),
        "base_model": model,
        "server_url": server_url,
        "n_attempts": max(1, int(n_attempts)),
        "max_output_tokens": output_token_cap,
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
    max_output_tokens: int = DEFAULT_LOCAL_PARAMETRIC_MAX_OUTPUT_TOKENS,
    verifier_env: dict[str, str] | None = None,
    command_runner: CommandRunner = _default_command_runner,
    manage_server: bool = True,
    server_timeout_seconds: float = 600.0,
    vllm_executable: str = DEFAULT_VLLM_EXECUTABLE,
    gpus: list[str] | None = None,
    port: int = 8000,
    auth_mode: str = "local",
) -> dict[str, Any]:
    _validate_adapter_path(adapter_path)
    auth_mode = _validate_auth_mode(auth_mode)
    attempt_count = max(1, int(n_attempts))
    output_token_cap = max(1, int(max_output_tokens))
    conditions = [
        LocalParametricCondition(name="baseline", model=model),
        LocalParametricCondition(
            name="parametric_memory",
            model=adapter_id,
            adapter_id=adapter_id,
            adapter_path=adapter_path,
        ),
    ]
    condition_summaries = [
        _run_local_parametric_condition(
            condition=condition,
            task_root=task_root,
            task_ids=task_ids,
            run_root=run_root,
            terminal_bench_package_root=terminal_bench_package_root,
            base_model=model,
            server_url=server_url,
            n_attempts=attempt_count,
            max_output_tokens=output_token_cap,
            verifier_env=dict(verifier_env or {}),
            command_runner=command_runner,
            manage_server=manage_server,
            server_timeout_seconds=server_timeout_seconds,
            vllm_executable=vllm_executable,
            gpus=gpus,
            port=port,
            adapter_artifact_id=adapter_artifact_id,
        )
        for condition in conditions
    ]
    baseline, treatment = condition_summaries
    return {
        "dry_run": False,
        "benchmark": "terminal-bench-2.1",
        "auth_mode": auth_mode,
        "task_root": str(task_root),
        "run_root": str(run_root),
        "terminal_bench_package_root": str(terminal_bench_package_root),
        "base_model": model,
        "server_url": server_url,
        "n_attempts": attempt_count,
        "max_output_tokens": output_token_cap,
        "manage_server": manage_server,
        "enabled_artifacts": ["parametric_memory"],
        "disabled_artifacts": list(DEFAULT_LOCAL_PARAMETRIC_DISABLED_ARTIFACTS),
        "conditions": condition_summaries,
        "delta": {
            "pass_at_1": (
                treatment["pass_at_1"]["passed"] - baseline["pass_at_1"]["passed"]
            ),
            "pass_at_k": (
                treatment["pass_at_k"]["passed"] - baseline["pass_at_k"]["passed"]
            ),
        },
    }


def _validate_adapter_path(adapter_path: Path) -> None:
    if str(adapter_path) in {"", "."}:
        raise ValueError("adapter_path must be a non-empty path")


def _validate_auth_mode(auth_mode: str) -> str:
    if auth_mode not in LOCAL_PARAMETRIC_AUTH_MODES:
        raise ValueError(
            "parametric_memory requires local or proxy auth: "
            f"auth_mode={auth_mode!r}"
        )
    return auth_mode


def _run_local_parametric_condition(
    *,
    condition: LocalParametricCondition,
    task_root: Path,
    task_ids: list[str],
    run_root: Path,
    terminal_bench_package_root: Path,
    base_model: str,
    server_url: str,
    n_attempts: int,
    max_output_tokens: int,
    verifier_env: dict[str, str],
    command_runner: CommandRunner,
    manage_server: bool,
    server_timeout_seconds: float,
    vllm_executable: str,
    gpus: list[str] | None,
    port: int,
    adapter_artifact_id: str | None,
) -> dict[str, Any]:
    condition_root = run_root / condition.name
    jobs_dir = condition_root / "harbor_jobs"
    condition_root.mkdir(parents=True, exist_ok=True)

    if manage_server:
        server_context = managed_vllm_server(
            spec=_build_condition_vllm_command(
                condition=condition,
                base_model=base_model,
                port=port,
                vllm_executable=vllm_executable,
                gpus=gpus,
            ),
            run_root=condition_root,
            server_url=server_url,
            expected_model=condition.model,
            timeout_seconds=server_timeout_seconds,
        )
    else:
        server_context = _unmanaged_server_context()

    with server_context as server_metadata:
        task_results = [
            _run_local_parametric_task(
                condition=condition,
                task_root=task_root,
                task_id=task_id,
                jobs_dir=jobs_dir,
                terminal_bench_package_root=terminal_bench_package_root,
                server_url=server_url,
                n_attempts=n_attempts,
                max_output_tokens=max_output_tokens,
                verifier_env=verifier_env,
                command_runner=command_runner,
            )
            for task_id in task_ids
        ]

    pass_at_1 = sum(1 for task in task_results if task["pass_at_1"])
    pass_at_k = sum(1 for task in task_results if task["pass_at_k"])
    summary: dict[str, Any] = {
        "name": condition.name,
        "model": condition.model,
        "jobs_dir": str(jobs_dir),
        "pass_at_1": {"passed": pass_at_1, "total": len(task_results)},
        "pass_at_k": {"passed": pass_at_k, "total": len(task_results), "k": n_attempts},
        "tasks": task_results,
    }
    if server_metadata is not None:
        summary["server"] = server_metadata
    if condition.adapter_id and condition.adapter_path is not None:
        summary["adapter"] = {
            "artifact_id": adapter_artifact_id,
            "adapter_id": condition.adapter_id,
            "adapter_path": str(condition.adapter_path),
        }
    return summary


@contextmanager
def _unmanaged_server_context() -> Iterator[None]:
    yield None


def _build_condition_vllm_command(
    *,
    condition: LocalParametricCondition,
    base_model: str,
    port: int,
    vllm_executable: str,
    gpus: list[str] | None,
) -> BuiltVLLMCommand:
    if condition.adapter_id and condition.adapter_path is not None:
        return build_vllm_command(
            model=base_model,
            served_model_name=condition.adapter_id,
            port=port,
            vllm_executable=vllm_executable,
            gpus=gpus,
            adapter_id=condition.adapter_id,
            adapter_path=condition.adapter_path,
        )
    return build_vllm_command(
        model=base_model,
        served_model_name=base_model,
        port=port,
        vllm_executable=vllm_executable,
        gpus=gpus,
    )


def _run_local_parametric_task(
    *,
    condition: LocalParametricCondition,
    task_root: Path,
    task_id: str,
    jobs_dir: Path,
    terminal_bench_package_root: Path,
    server_url: str,
    n_attempts: int,
    max_output_tokens: int,
    verifier_env: dict[str, str],
    command_runner: CommandRunner,
) -> dict[str, Any]:
    job_name = f"{condition.name}-{task_id}"
    command = build_local_harbor_command(
        job_name=job_name,
        task_root=task_root,
        task_id=task_id,
        jobs_dir=jobs_dir,
        model=condition.model,
        verifier_env=verifier_env,
        n_attempts=n_attempts,
    )
    env = build_evolab_harbor_env(
        base_env=os.environ,
        server_url=server_url,
        model=condition.model,
        max_output_tokens=max_output_tokens,
    )
    command_runner(command, cwd=terminal_bench_package_root, env=env)

    trials = _locate_evolved_attempt_trials(task_id=task_id, job_root=jobs_dir / job_name)
    attempts = [
        {
            "trial_dir": str(trial),
            "reward": reward,
            "passed": reward is not None and reward >= 1.0,
        }
        for trial in trials
        for reward in [_attempt_reward(trial)]
    ]
    scored_attempts = attempts[:n_attempts]
    return {
        "task_id": task_id,
        "job_name": job_name,
        "job_root": str(jobs_dir / job_name),
        "attempts": attempts,
        "pass_at_1": bool(attempts and attempts[0]["passed"]),
        "pass_at_k": any(attempt["passed"] for attempt in scored_attempts),
    }
