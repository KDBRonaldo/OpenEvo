from __future__ import annotations

import posixpath
import shlex
from collections.abc import Mapping
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    computed_field,
    field_validator,
    model_validator,
)
import yaml

from openevo.remote.executor import RemoteExecutorTransport
from openevo.remote.redaction import sanitize_remote_text

if TYPE_CHECKING:
    from openevo.remote.bootstrap import RemoteBootstrapPlan


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        validate_default=True,
    )


class RemoteServiceStepStatus(StrEnum):
    PASS = "pass"
    FAIL = "fail"


class RemoteServiceStep(_StrictFrozenModel):
    id: str
    label: str
    command: str
    health_command: str | None = None
    cwd: str | None = None
    env: dict[str, str] = Field(default_factory=dict)
    timeout_seconds: float = Field(default=30.0, gt=0)
    health_timeout_seconds: float = Field(default=30.0, gt=0)
    required: bool = True
    manifest: dict[str, Any] = Field(default_factory=dict)

    @field_validator("id", "label", "command", "health_command", "cwd")
    @classmethod
    def _strip_text(cls, value: str | None, info) -> str | None:
        if value is None:
            return None
        text = value.strip()
        if not text:
            raise ValueError(f"{info.field_name} must be a non-empty string")
        return text

    @field_validator("env")
    @classmethod
    def _validate_env(cls, value: dict[str, str]) -> dict[str, str]:
        return _strip_string_mapping(value, "env")


class RemoteServiceStepExecution(_StrictFrozenModel):
    id: str
    label: str
    status: RemoteServiceStepStatus
    message: str
    command: str
    health_command: str | None = None
    required: bool = True
    return_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    health_return_code: int | None = None
    health_stdout: str = ""
    health_stderr: str = ""

    @field_validator("status", mode="before")
    @classmethod
    def _coerce_status(cls, value) -> RemoteServiceStepStatus:
        if isinstance(value, str):
            return RemoteServiceStepStatus(value)
        return value

    @field_validator("id", "label", "message", "command", "health_command")
    @classmethod
    def _strip_text(cls, value: str | None, info) -> str | None:
        if value is None:
            return None
        text = value.strip()
        if not text:
            raise ValueError(f"{info.field_name} must be a non-empty string")
        return text


class RemoteServicesPlan(_StrictFrozenModel):
    version: Literal[1] = 1
    remote_profile_id: str
    project_name: str
    task_id: str
    state_root: str
    topology_path: str
    proxy_env: dict[str, str] = Field(default_factory=dict)
    steps: tuple[RemoteServiceStep, ...] = Field(default_factory=tuple)

    @field_validator(
        "remote_profile_id",
        "project_name",
        "task_id",
        "state_root",
        "topology_path",
    )
    @classmethod
    def _strip_required_text(cls, value: str, info) -> str:
        return _strip_non_empty(value, info.field_name)

    @field_validator("proxy_env")
    @classmethod
    def _validate_proxy_env(cls, value: dict[str, str]) -> dict[str, str]:
        return _strip_string_mapping(value, "proxy_env")

    @field_validator("steps", mode="before")
    @classmethod
    def _coerce_steps(cls, value):
        if isinstance(value, list):
            return tuple(value)
        return value

    def step_by_id(self, step_id: str) -> RemoteServiceStep:
        for step in self.steps:
            if step.id == step_id:
                return step
        raise KeyError(step_id)


class RemoteServicesReport(_StrictFrozenModel):
    remote_profile_id: str
    project_name: str
    task_id: str
    state_root: str
    topology_path: str
    steps: tuple[RemoteServiceStepExecution, ...] = Field(default_factory=tuple)
    next_actions: tuple[str, ...] = Field(default_factory=tuple)

    @model_validator(mode="before")
    @classmethod
    def _ignore_dumped_ready(cls, value):
        if isinstance(value, dict) and "ready" in value:
            return {key: item for key, item in value.items() if key != "ready"}
        return value

    @field_validator(
        "remote_profile_id",
        "project_name",
        "task_id",
        "state_root",
        "topology_path",
    )
    @classmethod
    def _strip_required_text(cls, value: str, info) -> str:
        return _strip_non_empty(value, info.field_name)

    @field_validator("steps", "next_actions", mode="before")
    @classmethod
    def _coerce_tuples(cls, value):
        if isinstance(value, list):
            return tuple(value)
        return value

    @field_validator("next_actions")
    @classmethod
    def _validate_next_actions(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(_strip_non_empty(item, "next_actions") for item in value)

    @computed_field
    @property
    def ready(self) -> bool:
        return all(
            not (step.required and step.status == RemoteServiceStepStatus.FAIL)
            for step in self.steps
        )

    def step_by_id(self, step_id: str) -> RemoteServiceStepExecution:
        for step in self.steps:
            if step.id == step_id:
                return step
        raise KeyError(step_id)


def build_remote_services_plan(bootstrap_plan: RemoteBootstrapPlan) -> RemoteServicesPlan:
    state_root = bootstrap_plan.state_root
    services_root = posixpath.join(state_root, "services")
    topology_path = posixpath.join(services_root, "topology.yaml")
    log_dir = posixpath.join(services_root, "logs")
    pid_dir = posixpath.join(services_root, "pids")
    experiment_snapshot = bootstrap_plan.experiment_snapshot
    model = _agent_model(experiment_snapshot)
    managed_hf_model = _managed_hf_model(experiment_snapshot)
    topology = _topology_yaml(
        state_root=state_root,
        model=model,
        inference_engine="vllm",
    )

    steps = [
        RemoteServiceStep(
            id="write_topology",
            label="Topology",
            command=_write_text_command(topology_path, topology),
            timeout_seconds=30.0,
            manifest={"path": topology_path},
        ),
    ]
    if managed_hf_model is not None:
        steps.append(
            RemoteServiceStep(
                id="vllm",
                label="vLLM model server",
                command=_vllm_command(
                    managed_hf_model,
                    log_dir=log_dir,
                    pid_dir=pid_dir,
                ),
                health_command=_http_health_command(
                    "http://127.0.0.1:8000/v1/models",
                    wait_seconds=900,
                    expected_model=managed_hf_model,
                ),
                env=dict(bootstrap_plan.proxy_env),
                timeout_seconds=900.0,
                health_timeout_seconds=905.0,
                manifest={"model": managed_hf_model, "port": 8000},
            )
        )
    steps.extend(
        [
            RemoteServiceStep(
                id="evolution_backend",
                label="Evolution backend",
                command=_daemon_command(
                    "evolution_backend",
                    [
                        "polar-evolution",
                        "serve",
                        "--host",
                        "127.0.0.1",
                        "--port",
                        "8200",
                        "--db",
                        posixpath.join(state_root, "evolution", "evolution.db"),
                        "--artifact-root",
                        posixpath.join(state_root, "evolution", "artifacts"),
                    ],
                    log_dir=log_dir,
                    pid_dir=pid_dir,
                ),
	                health_command=_http_health_command(
	                    "http://127.0.0.1:8200/v1/health"
	                ),
                env=dict(bootstrap_plan.proxy_env),
                timeout_seconds=60.0,
                health_timeout_seconds=35.0,
                manifest={"port": 8200},
            ),
            RemoteServiceStep(
                id="rollout",
                label="Rollout server",
                command=_daemon_command(
                    "rollout",
                    ["polar", "serve_rollout", topology_path],
                    log_dir=log_dir,
                    pid_dir=pid_dir,
                ),
	                health_command=_http_health_command("http://127.0.0.1:8080/health"),
                env=dict(bootstrap_plan.proxy_env),
                timeout_seconds=60.0,
                health_timeout_seconds=35.0,
                manifest={"port": 8080},
            ),
            RemoteServiceStep(
                id="gateway",
                label="Gateway",
                command=_daemon_command(
                    "gateway",
                    [
                        "polar",
                        "serve_gateway",
                        topology_path,
                        "--node-id",
                        "desktop-node",
                    ],
                    log_dir=log_dir,
                    pid_dir=pid_dir,
                ),
	                health_command=_http_health_command("http://127.0.0.1:8100/health"),
                env=dict(bootstrap_plan.proxy_env),
                timeout_seconds=60.0,
                health_timeout_seconds=35.0,
                manifest={"port": 8100},
            ),
            RemoteServiceStep(
                id="evolution_worker",
                label="Evolution worker",
                command=_daemon_command(
                    "evolution_worker",
                    [
                        "polar-evolution",
                        "worker",
                        "--base-url",
                        "http://127.0.0.1:8200",
                        "--worker-id",
                        "openevo-desktop-worker",
                        "--artifact-root",
                        posixpath.join(state_root, "evolution", "artifacts"),
                    ],
                    log_dir=log_dir,
                    pid_dir=pid_dir,
                ),
	                health_command=_pid_health_command(
	                    posixpath.join(pid_dir, "evolution_worker.pid")
	                ),
                env=dict(bootstrap_plan.proxy_env),
                timeout_seconds=60.0,
                health_timeout_seconds=35.0,
            ),
        ]
    )
    return RemoteServicesPlan(
        remote_profile_id=bootstrap_plan.remote_profile_id,
        project_name=bootstrap_plan.project_name,
        task_id=bootstrap_plan.task_id,
        state_root=state_root,
        topology_path=topology_path,
        proxy_env=dict(bootstrap_plan.proxy_env),
        steps=tuple(steps),
    )


def execute_remote_services_plan(
    plan: RemoteServicesPlan,
    transport: RemoteExecutorTransport,
) -> RemoteServicesReport:
    executions: list[RemoteServiceStepExecution] = []
    for step in plan.steps:
        execution = _execute_service_step(step, transport)
        executions.append(execution)
        if execution.required and execution.status == RemoteServiceStepStatus.FAIL:
            break
    return RemoteServicesReport(
        remote_profile_id=plan.remote_profile_id,
        project_name=plan.project_name,
        task_id=plan.task_id,
        state_root=plan.state_root,
        topology_path=plan.topology_path,
        steps=tuple(executions),
        next_actions=_next_actions(executions),
    )


def _execute_service_step(
    step: RemoteServiceStep,
    transport: RemoteExecutorTransport,
) -> RemoteServiceStepExecution:
    try:
        result = transport.run(
            step.command,
            cwd=step.cwd,
            env=dict(step.env),
            timeout_seconds=step.timeout_seconds,
        )
    except Exception as exc:
        message = _sanitize_service_text(str(exc), step.env)
        return _step_execution(
            step,
            status=RemoteServiceStepStatus.FAIL,
            message=message,
            stderr=message,
        )
    if not result.ok:
        return _step_execution(
            step,
            status=RemoteServiceStepStatus.FAIL,
            message=f"{step.label} failed to start.",
            return_code=result.return_code,
            stdout=_sanitize_service_text(result.stdout, step.env),
            stderr=_sanitize_service_text(result.stderr, step.env),
        )
    if step.health_command is None:
        return _step_execution(
            step,
            status=RemoteServiceStepStatus.PASS,
            message=f"{step.label} prepared.",
            return_code=result.return_code,
            stdout=_sanitize_service_text(result.stdout, step.env),
            stderr=_sanitize_service_text(result.stderr, step.env),
        )

    try:
        health = transport.run(
            step.health_command,
            cwd=step.cwd,
            env=dict(step.env),
            timeout_seconds=step.health_timeout_seconds,
        )
    except Exception as exc:
        message = _sanitize_service_text(str(exc), step.env)
        return _step_execution(
            step,
            status=RemoteServiceStepStatus.FAIL,
            message=f"{step.label} health check failed.",
            return_code=result.return_code,
            stdout=_sanitize_service_text(result.stdout, step.env),
            stderr=_sanitize_service_text(result.stderr, step.env),
            health_stderr=message,
        )
    if not health.ok:
        return _step_execution(
            step,
            status=RemoteServiceStepStatus.FAIL,
            message=f"{step.label} health check failed.",
            return_code=result.return_code,
            stdout=_sanitize_service_text(result.stdout, step.env),
            stderr=_sanitize_service_text(result.stderr, step.env),
            health_return_code=health.return_code,
            health_stdout=_sanitize_service_text(health.stdout, step.env),
            health_stderr=_sanitize_service_text(health.stderr, step.env),
        )
    return _step_execution(
        step,
        status=RemoteServiceStepStatus.PASS,
        message=f"{step.label} is ready.",
        return_code=result.return_code,
        stdout=_sanitize_service_text(result.stdout, step.env),
        stderr=_sanitize_service_text(result.stderr, step.env),
        health_return_code=health.return_code,
        health_stdout=_sanitize_service_text(health.stdout, step.env),
        health_stderr=_sanitize_service_text(health.stderr, step.env),
    )


def _step_execution(
    step: RemoteServiceStep,
    *,
    status: RemoteServiceStepStatus,
    message: str,
    return_code: int | None = None,
    stdout: str = "",
    stderr: str = "",
    health_return_code: int | None = None,
    health_stdout: str = "",
    health_stderr: str = "",
) -> RemoteServiceStepExecution:
    return RemoteServiceStepExecution(
        id=step.id,
        label=step.label,
        status=status,
        message=message,
        command=step.command,
        health_command=step.health_command,
        required=step.required,
        return_code=return_code,
        stdout=stdout,
        stderr=stderr,
        health_return_code=health_return_code,
        health_stdout=health_stdout,
        health_stderr=health_stderr,
    )


def _next_actions(
    executions: list[RemoteServiceStepExecution],
) -> tuple[str, ...]:
    if any(
        step.required and step.status == RemoteServiceStepStatus.FAIL
        for step in executions
    ):
        return ("Fix remote service failure and restart services.",)
    return ()


def _sanitize_service_text(value: str, env: Mapping[str, str]) -> str:
    return sanitize_remote_text(value, env)


def _topology_yaml(
    *,
    state_root: str,
    model: str,
    inference_engine: Literal["vllm"],
) -> str:
    payload = {
        "rollout": {
            "host": "127.0.0.1",
            "port": 8080,
            "public_url": "http://127.0.0.1:8080",
            "save_dir": posixpath.join(state_root, "rollout"),
        },
        "gateway": {
            "rollout_server_url": "http://127.0.0.1:8080",
            "nodes": [
                {
                    "id": "desktop-node",
                    "host": "127.0.0.1",
                    "port": 8100,
                    "public_url": "http://127.0.0.1:8100",
                    "model_served": model,
                    "inference": {
                        "engine": inference_engine,
                        "base_url": "http://127.0.0.1:8000",
                    },
                }
            ],
        },
        "evolution": {
            "enabled": True,
            "backend_url": "http://127.0.0.1:8200",
            "context": {
                "target_dir": "/polar/session/evolution",
                "timeout_seconds": 10.0,
                "fail_open": True,
            },
            "event_export": {
                "enabled": True,
                "timeout_seconds": 10.0,
                "fail_open": True,
            },
        },
    }
    return yaml.safe_dump(payload, sort_keys=True)


def _write_text_command(remote_path: str, text: str) -> str:
    return "\n".join(
        [
            "python3 - <<'PY'",
            "from pathlib import Path",
            f"path = Path({remote_path!r})",
            "path.parent.mkdir(parents=True, exist_ok=True)",
            f"path.write_text({text!r}, encoding='utf-8')",
            "PY",
        ]
    )


def _daemon_command(
    service_id: str,
    argv: list[str],
    *,
    log_dir: str,
    pid_dir: str,
    prelude: list[str] | None = None,
) -> str:
    pid_path = posixpath.join(pid_dir, f"{service_id}.pid")
    log_path = posixpath.join(log_dir, f"{service_id}.log")
    command = " ".join(shlex.quote(part) for part in argv)
    return "\n".join(
        [
            f"mkdir -p {shlex.quote(log_dir)} {shlex.quote(pid_dir)}",
            (
                f"if [ -s {shlex.quote(pid_path)} ] "
                f"&& kill -0 \"$(cat {shlex.quote(pid_path)})\" 2>/dev/null; "
                "then exit 0; fi"
            ),
            *(prelude or []),
            (
                'nohup env PATH="$HOME/.local/bin:$PATH" '
                f"{command} > {shlex.quote(log_path)} 2>&1 < /dev/null &"
            ),
            f"echo $! > {shlex.quote(pid_path)}",
        ]
    )


def _vllm_command(model: str, *, log_dir: str, pid_dir: str) -> str:
    install_prelude = [
        "\n".join(
            [
                "python3 - <<'PY'",
                "import importlib.util",
                "import subprocess",
                "import sys",
                "if importlib.util.find_spec('vllm') is None:",
                "    subprocess.check_call([",
                "        sys.executable, '-m', 'pip',",
                "        '--disable-pip-version-check', 'install',",
                "        '--user', '--upgrade', '--no-input', 'vllm',",
                "    ])",
                "PY",
            ]
        )
    ]
    return _daemon_command(
        "vllm",
        [
            "python3",
            "-m",
            "vllm.entrypoints.openai.api_server",
            "--host",
            "127.0.0.1",
            "--port",
            "8000",
            "--model",
            model,
            "--served-model-name",
            model,
        ],
        log_dir=log_dir,
        pid_dir=pid_dir,
        prelude=install_prelude,
    )


def _http_health_command(
    url: str,
    *,
    wait_seconds: int = 30,
    expected_model: str | None = None,
) -> str:
    expected_model_block = [
        f"expected_model = {expected_model!r}",
        "if expected_model is not None:",
        "    payload = json.load(response)",
        "    data = payload.get('data') if isinstance(payload, dict) else None",
        "    if not isinstance(data, list):",
        "        data = []",
        "    models = {item.get('id') for item in data if isinstance(item, dict)}",
        "    if expected_model not in models:",
        "        raise RuntimeError(f'model {expected_model!r} is not served')",
    ]
    return "\n".join(
        [
            "python3 - <<'PY'",
            "import json",
            "import sys",
            "import time",
            "from urllib.error import HTTPError, URLError",
            "from urllib.request import urlopen",
            f"url = {url!r}",
            f"deadline = time.monotonic() + {wait_seconds}",
            "last_error = None",
            "while True:",
            "    try:",
            "        with urlopen(url, timeout=5) as response:",
            "            if response.status >= 400:",
            "                raise RuntimeError(f'HTTP {response.status}')",
            *[f"            {line}" for line in expected_model_block],
            "            raise SystemExit(0)",
            "    except Exception as exc:",
            "        last_error = exc",
            "        if time.monotonic() >= deadline:",
            "            print(f'health check failed: {last_error}', file=sys.stderr)",
            "            raise SystemExit(1)",
            "        time.sleep(2)",
            "PY",
        ]
    )


def _pid_health_command(pid_path: str, *, wait_seconds: int = 30) -> str:
    return "\n".join(
        [
            "python3 - <<'PY'",
            "import os",
            "import sys",
            "import time",
            f"pid_path = {pid_path!r}",
            f"deadline = time.monotonic() + {wait_seconds}",
            "last_error = 'pid file not ready'",
            "while True:",
            "    try:",
            "        with open(pid_path, encoding='utf-8') as handle:",
            "            pid = int(handle.read().strip())",
            "        os.kill(pid, 0)",
            "        raise SystemExit(0)",
            "    except Exception as exc:",
            "        last_error = exc",
            "        if time.monotonic() >= deadline:",
            "            print(f'pid health check failed: {last_error}', file=sys.stderr)",
            "            raise SystemExit(1)",
            "        time.sleep(1)",
            "PY",
        ]
    )


def _agent_model(experiment_snapshot: Mapping[str, Any]) -> str:
    agent = experiment_snapshot.get("agent")
    if not isinstance(agent, Mapping):
        raise ValueError("experiment snapshot missing agent block")
    model = agent.get("model")
    if not isinstance(model, str) or not model.strip():
        raise ValueError("experiment snapshot missing agent.model")
    return model.strip()


def _managed_hf_model(experiment_snapshot: Mapping[str, Any]) -> str | None:
    runtime = experiment_snapshot.get("runtime")
    if not isinstance(runtime, Mapping):
        return None
    env = runtime.get("env")
    if not isinstance(env, Mapping):
        return None
    model = env.get("OPENEVO_MANAGED_HF_MODEL")
    return model if isinstance(model, str) and model.strip() else None


def _strip_non_empty(value: str, field_name: str) -> str:
    text = value.strip()
    if not text:
        raise ValueError(f"{field_name} must be a non-empty string")
    return text


def _strip_string_mapping(value: dict[str, str], field_name: str) -> dict[str, str]:
    stripped: dict[str, str] = {}
    for key, item in value.items():
        stripped_key = _strip_non_empty(key, f"{field_name} key")
        stripped[stripped_key] = _strip_non_empty(item, f"{field_name}.{stripped_key}")
    return stripped
