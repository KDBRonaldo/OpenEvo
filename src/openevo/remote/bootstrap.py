from __future__ import annotations

import json
import posixpath
import re
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

from openevo.remote.executor import RemoteExecutorTransport
from openevo.remote.preflight import (
    PreflightCheck,
    PreflightReport,
    RemotePreflightSettings,
    run_preflight,
)
from openevo.remote.redaction import sanitize_remote_text
from openevo.science.compiler import MANAGED_RUNTIME_IMAGES

if TYPE_CHECKING:
    from openevo.sidecar import SidecarSciencePlan


_MANAGED_RUNTIME_IMAGE_SET = frozenset(MANAGED_RUNTIME_IMAGES.values())
_MANAGED_RUNTIME_BASE_IMAGE = "node:22-bookworm-slim"
_MANAGED_RUNTIME_PYTHON_IMAGE = "python:3.12-slim-bookworm"
_MANAGED_RUNTIME_CODEX_PACKAGE = "@openai/codex@0.121.0"
_DOCKER_PROXY_BUILD_ARGS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
)


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        validate_default=True,
    )


class RemoteBootstrapStepKind(StrEnum):
    ENSURE_DIR = "ensure_dir"
    WRITE_FILE = "write_file"
    CHECK_COMMAND = "check_command"
    DOCKER_PULL = "docker_pull"
    HF_SNAPSHOT_DOWNLOAD = "hf_snapshot_download"
    HEALTH_CHECK = "health_check"


class RemoteBootstrapStepStatus(StrEnum):
    PASS = "pass"
    SKIP = "skip"
    WARN = "warn"
    FAIL = "fail"


class RemoteBootstrapStep(_StrictFrozenModel):
    id: str
    kind: RemoteBootstrapStepKind
    command: str
    cwd: str | None = None
    env: dict[str, str] = Field(default_factory=dict)
    timeout_seconds: float = Field(default=30.0, gt=0)
    network: bool = False
    required: bool = True
    remediation_kind: Literal[
        "none", "openevo_retry", "openevo_install", "user_action"
    ] = "none"
    manifest: dict[str, Any] = Field(default_factory=dict)

    @field_validator("kind", mode="before")
    @classmethod
    def _coerce_kind(cls, value) -> RemoteBootstrapStepKind:
        if isinstance(value, str):
            return RemoteBootstrapStepKind(value)
        return value

    @field_validator("id", "command", "cwd")
    @classmethod
    def _strip_optional_text(cls, value: str | None, info) -> str | None:
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


class RemoteBootstrapStepExecution(_StrictFrozenModel):
    id: str
    kind: RemoteBootstrapStepKind
    status: RemoteBootstrapStepStatus
    message: str
    command: str
    required: bool = True
    remediation_kind: Literal[
        "none", "openevo_retry", "openevo_install", "user_action"
    ] = "none"
    return_code: int | None = None
    stdout: str = ""
    stderr: str = ""

    @field_validator("kind", mode="before")
    @classmethod
    def _coerce_kind(cls, value) -> RemoteBootstrapStepKind:
        if isinstance(value, str):
            return RemoteBootstrapStepKind(value)
        return value

    @field_validator("status", mode="before")
    @classmethod
    def _coerce_status(cls, value) -> RemoteBootstrapStepStatus:
        if isinstance(value, str):
            return RemoteBootstrapStepStatus(value)
        return value

    @field_validator("id", "message", "command")
    @classmethod
    def _strip_required_text(cls, value: str, info) -> str:
        return _strip_non_empty(value, info.field_name)


class RemoteBootstrapPlan(_StrictFrozenModel):
    version: Literal[1] = 1
    remote_profile_id: str
    project_name: str
    task_id: str
    proxy_env: dict[str, str] = Field(default_factory=dict)
    preflight: RemotePreflightSettings = Field(default_factory=RemotePreflightSettings)
    state_root: str
    workspace_root: str
    experiment_snapshot: dict[str, Any]
    steps: tuple[RemoteBootstrapStep, ...] = Field(default_factory=tuple)

    @field_validator(
        "remote_profile_id",
        "project_name",
        "task_id",
        "state_root",
        "workspace_root",
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
    def _coerce_steps_tuple(cls, value):
        if isinstance(value, list):
            return tuple(value)
        return value


class RemoteBootstrapReport(_StrictFrozenModel):
    remote_profile_id: str
    project_name: str
    task_id: str
    preflight: PreflightReport | None = None
    steps: tuple[RemoteBootstrapStepExecution, ...] = Field(default_factory=tuple)
    prepared_paths: dict[str, str] = Field(default_factory=dict)
    next_actions: tuple[str, ...] = Field(default_factory=tuple)

    @model_validator(mode="before")
    @classmethod
    def _ignore_dumped_computed_fields(cls, value):
        if isinstance(value, dict):
            return {
                key: item
                for key, item in value.items()
                if key not in {"ready", "status"}
            }
        return value

    @field_validator("remote_profile_id", "project_name", "task_id")
    @classmethod
    def _strip_required_text(cls, value: str, info) -> str:
        return _strip_non_empty(value, info.field_name)

    @field_validator("steps", mode="before")
    @classmethod
    def _coerce_steps_tuple(cls, value):
        if isinstance(value, list):
            return tuple(value)
        return value

    @field_validator("next_actions", mode="before")
    @classmethod
    def _coerce_next_actions_tuple(cls, value):
        if isinstance(value, list):
            return tuple(value)
        return value

    @field_validator("prepared_paths")
    @classmethod
    def _validate_prepared_paths(cls, value: dict[str, str]) -> dict[str, str]:
        return _strip_string_mapping(value, "prepared_paths")

    @field_validator("next_actions")
    @classmethod
    def _validate_next_actions(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(_strip_non_empty(item, "next_actions") for item in value)

    @computed_field
    @property
    def ready(self) -> bool:
        if self.preflight is not None and not self.preflight.ready:
            return False
        return all(
            not (step.required and step.status == RemoteBootstrapStepStatus.FAIL)
            for step in self.steps
        )

    @computed_field
    @property
    def status(self) -> RemoteBootstrapStepStatus:
        if not self.ready:
            return RemoteBootstrapStepStatus.FAIL
        if any(
            step.status in {RemoteBootstrapStepStatus.WARN, RemoteBootstrapStepStatus.FAIL}
            for step in self.steps
        ):
            return RemoteBootstrapStepStatus.WARN
        return RemoteBootstrapStepStatus.PASS


def build_remote_bootstrap_plan(plan: SidecarSciencePlan) -> RemoteBootstrapPlan:
    experiment_snapshot = _thaw_json(plan.experiment)
    if not isinstance(experiment_snapshot, dict):
        raise ValueError("sidecar experiment snapshot must be a JSON object")

    state_root = _bootstrap_state_root(
        plan.workspace.workspace_root,
        project_name=plan.project_name,
        task_id=plan.task_id,
    )
    runtime_image = _runtime_image(experiment_snapshot)
    hf_model = _managed_hf_model(experiment_snapshot)
    proxy_env = dict(plan.proxy_env)
    experiment_path = posixpath.join(state_root, "experiment.json")
    manifest_path = posixpath.join(state_root, "bootstrap.json")

    steps = [
        RemoteBootstrapStep(
            id="ensure_workspace_root",
            kind=RemoteBootstrapStepKind.ENSURE_DIR,
            command=f"mkdir -p {shlex.quote(plan.workspace.workspace_root)}",
            timeout_seconds=30.0,
            remediation_kind="openevo_retry",
        ),
        RemoteBootstrapStep(
            id="ensure_state_root",
            kind=RemoteBootstrapStepKind.ENSURE_DIR,
            command=f"mkdir -p {shlex.quote(state_root)}",
            timeout_seconds=30.0,
            remediation_kind="openevo_retry",
        ),
        RemoteBootstrapStep(
            id="write_experiment_snapshot",
            kind=RemoteBootstrapStepKind.WRITE_FILE,
            command=_write_json_command(
                experiment_path,
                experiment_snapshot,
            ),
            timeout_seconds=30.0,
            remediation_kind="openevo_retry",
            manifest={"path": experiment_path},
        ),
        RemoteBootstrapStep(
            id="write_bootstrap_manifest",
            kind=RemoteBootstrapStepKind.WRITE_FILE,
            command=_write_json_command(
                manifest_path,
                _bootstrap_manifest(
                    plan,
                    state_root=state_root,
                    experiment_path=experiment_path,
                    runtime_image=runtime_image,
                    hf_model=hf_model,
                ),
            ),
            timeout_seconds=30.0,
            remediation_kind="openevo_retry",
            manifest={"path": manifest_path},
        ),
        RemoteBootstrapStep(
            id="ensure_openevo_cli",
            kind=RemoteBootstrapStepKind.CHECK_COMMAND,
            command=_openevo_cli_install_command(),
            env=proxy_env,
            timeout_seconds=300.0,
            network=True,
            required=True,
            remediation_kind="openevo_install",
            manifest={"package": "openevo"},
        ),
    ]

    if _uses_codex_subscription(experiment_snapshot):
        steps.extend(
            [
                RemoteBootstrapStep(
                    id="check_codex_cli",
                    kind=RemoteBootstrapStepKind.CHECK_COMMAND,
                    command="codex --version",
                    timeout_seconds=30.0,
                    required=True,
                    remediation_kind="user_action",
                ),
                RemoteBootstrapStep(
                    id="check_codex_subscription",
                    kind=RemoteBootstrapStepKind.CHECK_COMMAND,
                    command="test -f ~/.codex/auth.json",
                    timeout_seconds=30.0,
                    required=True,
                    remediation_kind="user_action",
                ),
            ]
        )

    if runtime_image is not None:
        is_managed_runtime = _is_managed_runtime_image(runtime_image)
        steps.append(
            RemoteBootstrapStep(
                id="docker_pull_runtime",
                kind=RemoteBootstrapStepKind.DOCKER_PULL,
                command=_runtime_image_command(
                    runtime_image,
                    state_root=state_root,
                ),
                env=proxy_env,
                timeout_seconds=900.0,
                network=True,
                required=True,
                remediation_kind="openevo_retry",
                manifest={
                    "image": runtime_image,
                    "managed_runtime": is_managed_runtime,
                },
            )
        )

    if hf_model is not None:
        steps.append(
            RemoteBootstrapStep(
                id="hf_snapshot_download",
                kind=RemoteBootstrapStepKind.HF_SNAPSHOT_DOWNLOAD,
                command=_hf_snapshot_download_command(hf_model),
                env=proxy_env,
                timeout_seconds=3600.0,
                network=True,
                required=True,
                remediation_kind="openevo_retry",
                manifest={"model": hf_model},
            )
        )

    return RemoteBootstrapPlan(
        remote_profile_id=plan.remote_profile_id,
        project_name=plan.project_name,
        task_id=plan.task_id,
        proxy_env=proxy_env,
        preflight=plan.preflight,
        state_root=state_root,
        workspace_root=plan.workspace.workspace_root,
        experiment_snapshot=experiment_snapshot,
        steps=tuple(steps),
    )


def execute_remote_bootstrap_plan(
    plan: RemoteBootstrapPlan,
    transport: RemoteExecutorTransport,
    *,
    run_remote_preflight: bool = True,
) -> RemoteBootstrapReport:
    if run_remote_preflight:
        try:
            preflight = run_preflight(transport, plan.preflight)
        except Exception as exc:
            preflight = _preflight_exception_report(exc)
    else:
        preflight = None

    if preflight is not None and not preflight.ready:
        return RemoteBootstrapReport(
            remote_profile_id=plan.remote_profile_id,
            project_name=plan.project_name,
            task_id=plan.task_id,
            preflight=preflight,
            prepared_paths=_prepared_paths(plan),
            next_actions=("Fix remote preflight failures and rerun bootstrap.",),
        )

    executions: list[RemoteBootstrapStepExecution] = []
    for step in plan.steps:
        execution = _execute_bootstrap_step(step, transport)
        executions.append(execution)
        if execution.required and execution.status == RemoteBootstrapStepStatus.FAIL:
            break

    return RemoteBootstrapReport(
        remote_profile_id=plan.remote_profile_id,
        project_name=plan.project_name,
        task_id=plan.task_id,
        preflight=preflight,
        steps=tuple(executions),
        prepared_paths=_prepared_paths(plan),
        next_actions=_next_actions(preflight, executions),
    )


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


def _bootstrap_state_root(
    workspace_root: str,
    *,
    project_name: str,
    task_id: str,
) -> str:
    root = workspace_root.rstrip("/") or "/"
    if posixpath.basename(root) == "workspaces":
        base = posixpath.join(posixpath.dirname(root), "runs")
    else:
        base = posixpath.join(root, ".openevo-runs")
    return posixpath.join(base, _slugify(project_name), _slugify(task_id))


def _runtime_image(experiment_snapshot: Mapping[str, Any]) -> str | None:
    runtime = experiment_snapshot.get("runtime")
    if not isinstance(runtime, Mapping):
        return None
    image = runtime.get("image")
    return image if isinstance(image, str) and image.strip() else None


def _is_managed_runtime_image(image: str) -> bool:
    return image in _MANAGED_RUNTIME_IMAGE_SET


def _runtime_image_command(runtime_image: str, *, state_root: str) -> str:
    if not _is_managed_runtime_image(runtime_image):
        return f"docker pull {shlex.quote(runtime_image)}"

    context_dir = posixpath.join(
        state_root,
        "runtime-images",
        _slugify(runtime_image),
    )
    dockerfile_path = posixpath.join(context_dir, "Dockerfile")
    build_args = " ".join(
        f"--build-arg {arg_name}" for arg_name in _DOCKER_PROXY_BUILD_ARGS
    )
    image_ref = shlex.quote(runtime_image)
    return "\n".join(
        [
            _write_text_command(
                dockerfile_path,
                _managed_runtime_dockerfile(),
            ),
            (
                f"docker pull {image_ref} || docker build {build_args} "
                f"--pull -t {image_ref} {shlex.quote(context_dir)}"
            ),
        ]
    )


def _managed_runtime_dockerfile() -> str:
    return f"""\
FROM {_MANAGED_RUNTIME_BASE_IMAGE} AS node

FROM {_MANAGED_RUNTIME_PYTHON_IMAGE}

COPY --from=node /usr/local/ /usr/local/

ENV DEBIAN_FRONTEND=noninteractive \\
    HOME=/home/polar \\
    NPM_CONFIG_PREFIX=/home/polar/.local \\
    NPM_CONFIG_UPDATE_NOTIFIER=false \\
    PATH=/home/polar/.local/bin:${{PATH}}

LABEL io.openevo.managed-runtime="true"

RUN apt-get update \\
    && apt-get install -y --no-install-recommends \\
        bash \\
        build-essential \\
        ca-certificates \\
        curl \\
        git \\
        python-is-python3 \\
        rsync \\
        sudo \\
        tmux \\
    && rm -rf /var/lib/apt/lists/*

RUN useradd -m -s /bin/bash polar \\
    && echo 'polar ALL=(ALL) NOPASSWD:ALL' >> /etc/sudoers \\
    && mkdir -p /polar/session/workspace /home/polar/.local \\
    && chown -R polar:polar /polar/session /home/polar

RUN echo 'export PATH=/home/polar/.local/bin:$PATH' > /etc/profile.d/polar-path.sh

USER polar
RUN npm install -g {_MANAGED_RUNTIME_CODEX_PACKAGE}

WORKDIR /polar/session/workspace
"""


def _bootstrap_manifest(
    plan: SidecarSciencePlan,
    *,
    state_root: str,
    experiment_path: str,
    runtime_image: str | None,
    hf_model: str | None,
) -> dict[str, Any]:
    return {
        "version": 1,
        "remote_profile_id": plan.remote_profile_id,
        "project_name": plan.project_name,
        "task_id": plan.task_id,
        "state_root": state_root,
        "workspace_root": plan.workspace.workspace_root,
        "experiment_snapshot": experiment_path,
        "runtime_image": runtime_image,
        "managed_hf_model": hf_model,
    }


def _uses_codex_subscription(experiment_snapshot: Mapping[str, Any]) -> bool:
    agent = experiment_snapshot.get("agent")
    if not isinstance(agent, Mapping):
        return False
    return agent.get("auth") == "subscription"


def _managed_hf_model(experiment_snapshot: Mapping[str, Any]) -> str | None:
    runtime = experiment_snapshot.get("runtime")
    if not isinstance(runtime, Mapping):
        return None
    env = runtime.get("env")
    if not isinstance(env, Mapping):
        return None
    model = env.get("OPENEVO_MANAGED_HF_MODEL")
    return model if isinstance(model, str) and model.strip() else None


def _write_json_command(remote_path: str, payload: Mapping[str, Any]) -> str:
    body = json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True)
    return "\n".join(
        [
            "python3 - <<'PY'",
            "from pathlib import Path",
            f"Path({remote_path!r}).write_text({body!r}, encoding='utf-8')",
            "PY",
        ]
    )


def _write_text_command(remote_path: str, contents: str) -> str:
    return "\n".join(
        [
            "python3 - <<'PY'",
            "from pathlib import Path",
            f"path = Path({remote_path!r})",
            "path.parent.mkdir(parents=True, exist_ok=True)",
            f"path.write_text({contents!r}, encoding='utf-8')",
            "PY",
        ]
    )


def _hf_snapshot_download_command(model: str) -> str:
    return "\n".join(
        [
            "python3 -m pip install --user huggingface_hub",
            "python3 - <<'PY'",
            "from huggingface_hub import snapshot_download",
            f"snapshot_download({model!r})",
            "PY",
        ]
    )


def _openevo_cli_install_command() -> str:
    return "\n".join(
        [
            "python3 - <<'PY'",
            "import os",
            "import shutil",
            "import subprocess",
            "import sys",
            "",
            "env = os.environ.copy()",
            "user_bin = os.path.expanduser('~/.local/bin')",
            "os.makedirs(user_bin, exist_ok=True)",
            "env['PATH'] = user_bin + os.pathsep + env.get('PATH', '')",
            "",
            "def run_command(args):",
            "    try:",
            (
                "        return subprocess.run(args, check=True, env=env, "
                "stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)"
            ),
            "    except subprocess.CalledProcessError as exc:",
            "        return exc",
            "",
            "def fail(label, result):",
            (
                "    print(f'{label} failed with exit code "
                "{result.returncode}', file=sys.stderr)"
            ),
            "    if result.stdout:",
            "        print(f'{label} stdout:\\n{result.stdout}', file=sys.stderr)",
            "    if result.stderr:",
            "        print(f'{label} stderr:\\n{result.stderr}', file=sys.stderr)",
            "    raise SystemExit(result.returncode or 1)",
            "",
            "def install_openevo():",
            (
                "    return run_command([sys.executable, '-m', 'pip', "
                "'--disable-pip-version-check', 'install', '--user', "
                "'--upgrade', '--no-input', 'openevo'])"
            ),
            "",
            "def check_openevo():",
            "    return run_command(['openevo', '--help'])",
            "",
            "if shutil.which('openevo', path=env.get('PATH')) is None:",
            "    install = install_openevo()",
            "    if install.returncode != 0:",
            "        fail('openevo install', install)",
            "check = check_openevo()",
            "if check.returncode != 0:",
            "    repair = install_openevo()",
            "    if repair.returncode != 0:",
            "        fail('openevo repair', repair)",
            "    check = check_openevo()",
            "    if check.returncode != 0:",
            "        fail('openevo check', check)",
            "PY",
        ]
    )


def _thaw_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [_thaw_json(item) for item in value]
    return value


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "item"


def _execute_bootstrap_step(
    step: RemoteBootstrapStep,
    transport: RemoteExecutorTransport,
) -> RemoteBootstrapStepExecution:
    try:
        result = transport.run(
            step.command,
            cwd=step.cwd,
            env=dict(step.env),
            timeout_seconds=step.timeout_seconds,
        )
    except Exception as exc:
        message = _sanitize_bootstrap_text(str(exc), step.env)
        return RemoteBootstrapStepExecution(
            id=step.id,
            kind=step.kind,
            status=RemoteBootstrapStepStatus.FAIL,
            message=f"Bootstrap step failed: {message}",
            command=step.command,
            required=step.required,
            remediation_kind=step.remediation_kind,
            stderr=message,
        )

    if result.ok:
        return RemoteBootstrapStepExecution(
            id=step.id,
            kind=step.kind,
            status=RemoteBootstrapStepStatus.PASS,
            message="Bootstrap step completed.",
            command=step.command,
            required=step.required,
            remediation_kind=step.remediation_kind,
            return_code=result.return_code,
            stdout=_sanitize_bootstrap_text(result.stdout, step.env),
            stderr=_sanitize_bootstrap_text(result.stderr, step.env),
        )

    status = (
        RemoteBootstrapStepStatus.FAIL
        if step.required
        else RemoteBootstrapStepStatus.WARN
    )
    return RemoteBootstrapStepExecution(
        id=step.id,
        kind=step.kind,
        status=status,
        message="Bootstrap step failed.",
        command=step.command,
        required=step.required,
        remediation_kind=step.remediation_kind,
        return_code=result.return_code,
        stdout=_sanitize_bootstrap_text(result.stdout, step.env),
        stderr=_sanitize_bootstrap_text(result.stderr, step.env),
    )


def _sanitize_bootstrap_text(value: str, env: Mapping[str, str]) -> str:
    return sanitize_remote_text(value, env)


def _preflight_exception_report(exc: Exception) -> PreflightReport:
    message = str(exc)
    return PreflightReport(
        checks=(
            PreflightCheck(
                name="preflight",
                status="fail",
                message=f"Remote preflight failed: {message}",
                remediation_kind="user_action",
                stderr=message,
            ),
        )
    )


def _prepared_paths(plan: RemoteBootstrapPlan) -> dict[str, str]:
    return {
        "state_root": plan.state_root,
        "workspace_root": plan.workspace_root,
        "experiment_snapshot": posixpath.join(plan.state_root, "experiment.json"),
        "bootstrap_manifest": posixpath.join(plan.state_root, "bootstrap.json"),
    }


def _next_actions(
    preflight: PreflightReport | None,
    executions: list[RemoteBootstrapStepExecution],
) -> tuple[str, ...]:
    if preflight is not None and not preflight.ready:
        return ("Fix remote preflight failures and rerun bootstrap.",)
    if any(
        execution.required and execution.status == RemoteBootstrapStepStatus.FAIL
        for execution in executions
    ):
        return ("Resolve failed bootstrap steps and rerun.",)
    if any(execution.status == RemoteBootstrapStepStatus.WARN for execution in executions):
        return ("Remote bootstrap finished with warnings.",)
    return ("Remote bootstrap is ready.",)
