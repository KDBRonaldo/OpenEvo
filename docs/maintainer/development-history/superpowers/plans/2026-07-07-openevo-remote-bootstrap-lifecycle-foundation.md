# OpenEvo Remote Bootstrap Lifecycle Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a fakeable remote bootstrap and lifecycle-status foundation so OpenEvo Desktop can prepare a remote Science run directory, execute mode-specific preparation checks, and render structured readiness reports.

**Architecture:** Keep `openevo.sidecar` as the planner for Science inputs and add `openevo.remote.bootstrap` as a consumer of `SidecarSciencePlan`. Bootstrap execution uses the existing `RemoteExecutorTransport`, so tests use fakes and CLI can choose dry-run or SSH. Lifecycle models are data/report contracts for Desktop rendering; this slice does not start a real daemon, vLLM server, Docker Compose stack, gateway, rollout server, or evolution worker.

**Tech Stack:** Python 3.11+, Pydantic v2, existing `RemoteExecutorTransport`, argparse CLI, pytest, YAML/JSON docs.

Tracked by issue #45.

---

## File Structure

- Create `src/openevo/remote/bootstrap.py`: plan/report models, bootstrap planner, bootstrap executor.
- Create `src/openevo/remote/lifecycle.py`: data-only launch/status/event models for Desktop rendering.
- Modify `src/openevo/remote/__init__.py`: export bootstrap and lifecycle contracts.
- Modify `src/openevo/cli.py`: add `openevo sidecar bootstrap CONFIG --remote-profile PROFILE [--transport dry-run|ssh] [--skip-preflight] [--json]`.
- Create `tests/openevo/remote/test_bootstrap.py`: plan generation, execution, proxy env, failure gating, JSON round-trip.
- Create `tests/openevo/remote/test_lifecycle.py`: lifecycle model round-trip and readiness semantics.
- Modify `tests/openevo/test_cli.py`: CLI bootstrap tests.
- Create `docs/architecture/openevo-desktop-remote-bootstrap-lifecycle-foundation.md`: scope, contracts, proxy behavior, limitations, verification.
- Modify `docs/architecture/openevo-desktop-remote-executor-foundation.md`: point to bootstrap lifecycle as the next layer above executor/SSH.

---

### Task 1: Bootstrap Plan and Report Models

**Files:**
- Create: `src/openevo/remote/bootstrap.py`
- Modify: `src/openevo/remote/__init__.py`
- Test: `tests/openevo/remote/test_bootstrap.py`

- [ ] **Step 1: Write failing model tests**

Create `tests/openevo/remote/test_bootstrap.py` with:

```python
from __future__ import annotations

from pathlib import Path

import pytest

from openevo.remote.bootstrap import (
    RemoteBootstrapPlan,
    RemoteBootstrapReport,
    RemoteBootstrapStep,
    RemoteBootstrapStepExecution,
    RemoteBootstrapStepKind,
    RemoteBootstrapStepStatus,
)
from openevo.remote.preflight import PreflightCheck, PreflightReport


def _step(**overrides) -> RemoteBootstrapStep:
    payload = {
        "id": "ensure_state_dir",
        "kind": "ensure_dir",
        "command": "mkdir -p /home/alice/.openevo/runs/protein-design/folding-baseline",
        "timeout_seconds": 30.0,
        "network": False,
        "required": True,
        "remediation_kind": "openevo_retry",
    }
    payload.update(overrides)
    return RemoteBootstrapStep.model_validate(payload)


def test_bootstrap_step_strips_and_validates_fields() -> None:
    step = _step(id="  docker_pull  ", kind="docker_pull", command="  docker pull image  ")

    assert step.id == "docker_pull"
    assert step.kind == RemoteBootstrapStepKind.DOCKER_PULL
    assert step.command == "docker pull image"

    with pytest.raises(ValueError, match="id"):
        _step(id="   ")

    with pytest.raises(ValueError, match="command"):
        _step(command="   ")


def test_bootstrap_plan_is_tuple_backed_and_json_round_trips() -> None:
    plan = RemoteBootstrapPlan(
        remote_profile_id="lab-gpu",
        project_name="protein-design",
        task_id="folding-baseline",
        proxy_env={"HTTPS_PROXY": "http://127.0.0.1:7890"},
        state_root="/home/alice/.openevo/runs/protein-design/folding-baseline",
        workspace_root="/home/alice/.openevo/workspaces",
        experiment_snapshot={"experiment": {"name": "protein-design"}},
        steps=[_step()],
    )

    dumped = plan.model_dump(mode="json")
    restored = RemoteBootstrapPlan.model_validate(dumped)

    assert isinstance(plan.steps, tuple)
    assert restored == plan
    with pytest.raises(AttributeError):
        plan.steps.append(_step())


def test_bootstrap_report_ready_and_status_are_computed_and_round_trip() -> None:
    preflight = PreflightReport(
        checks=[
            PreflightCheck(
                name="ssh",
                status="pass",
                message="Remote command execution is available.",
            )
        ]
    )
    report = RemoteBootstrapReport(
        remote_profile_id="lab-gpu",
        project_name="protein-design",
        task_id="folding-baseline",
        preflight=preflight,
        steps=[
            RemoteBootstrapStepExecution(
                id="ensure_state_dir",
                kind=RemoteBootstrapStepKind.ENSURE_DIR,
                status=RemoteBootstrapStepStatus.PASS,
                message="ok",
                command="mkdir -p /tmp/run",
                return_code=0,
            )
        ],
        prepared_paths={"state_root": "/tmp/run"},
        next_actions=["Start remote OpenEvo services."],
    )

    dumped = report.model_dump(mode="json")
    restored = RemoteBootstrapReport.model_validate(dumped)

    assert dumped["ready"] is True
    assert dumped["status"] == "pass"
    assert isinstance(report.steps, tuple)
    assert restored == report


def test_bootstrap_report_fails_when_preflight_or_required_step_fails() -> None:
    report = RemoteBootstrapReport(
        remote_profile_id="lab-gpu",
        project_name="protein-design",
        task_id="folding-baseline",
        preflight=PreflightReport(
            checks=[
                PreflightCheck(
                    name="docker",
                    status="fail",
                    message="Docker unavailable.",
                )
            ]
        ),
        steps=[],
    )

    assert report.ready is False
    assert report.status == "fail"
```

- [ ] **Step 2: Verify RED**

Run:

```bash
PYTHONPATH=src /home/ziyi/ProRL-Agent-Server/.venv/bin/python -m pytest tests/openevo/remote/test_bootstrap.py -q
```

Expected: fail because `openevo.remote.bootstrap` does not exist.

- [ ] **Step 3: Implement bootstrap models**

Create `src/openevo/remote/bootstrap.py` with:

```python
from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    computed_field,
    field_validator,
    model_validator,
)

from openevo.remote.preflight import PreflightReport


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
```

Add `RemoteBootstrapStep`, `RemoteBootstrapStepExecution`, `RemoteBootstrapPlan`, and `RemoteBootstrapReport`.

`RemoteBootstrapStep` fields:

```python
id: str
kind: RemoteBootstrapStepKind
command: str
cwd: str | None = None
env: dict[str, str] = Field(default_factory=dict)
timeout_seconds: float = Field(default=30.0, gt=0)
network: bool = False
required: bool = True
remediation_kind: Literal["none", "openevo_retry", "openevo_install", "user_action"] = "none"
manifest: dict[str, Any] = Field(default_factory=dict)
```

`RemoteBootstrapStepExecution` mirrors the step plus:

```python
status: RemoteBootstrapStepStatus
message: str
return_code: int | None = None
stdout: str = ""
stderr: str = ""
```

`RemoteBootstrapPlan` fields:

```python
version: Literal[1] = 1
remote_profile_id: str
project_name: str
task_id: str
proxy_env: dict[str, str] = Field(default_factory=dict)
state_root: str
workspace_root: str
experiment_snapshot: dict[str, Any]
steps: tuple[RemoteBootstrapStep, ...] = Field(default_factory=tuple)
```

`RemoteBootstrapReport` fields:

```python
remote_profile_id: str
project_name: str
task_id: str
preflight: PreflightReport | None = None
steps: tuple[RemoteBootstrapStepExecution, ...] = Field(default_factory=tuple)
prepared_paths: dict[str, str] = Field(default_factory=dict)
next_actions: tuple[str, ...] = Field(default_factory=tuple)
```

Validation:

- Strip required strings and reject empty.
- Convert list-backed `steps` and `next_actions` to tuples.
- Reject dumped computed fields `ready` and `status` in `mode="before"` validators.
- Validate env values are non-empty strings.
- `RemoteBootstrapReport.ready` is true only if preflight is absent or ready and no required step failed.
- `RemoteBootstrapReport.status` is `fail` if not ready, `warn` if any non-required step warns/fails, otherwise `pass`.

Export these symbols from `src/openevo/remote/__init__.py`.

- [ ] **Step 4: Verify GREEN**

Run:

```bash
PYTHONPATH=src /home/ziyi/ProRL-Agent-Server/.venv/bin/python -m pytest tests/openevo/remote/test_bootstrap.py -q
```

Expected: model tests pass.

- [ ] **Step 5: Commit Task 1**

```bash
git add src/openevo/remote/bootstrap.py src/openevo/remote/__init__.py tests/openevo/remote/test_bootstrap.py
git commit -m "feat: add remote bootstrap report models"
```

---

### Task 2: Bootstrap Plan Builder

**Files:**
- Modify: `src/openevo/remote/bootstrap.py`
- Test: `tests/openevo/remote/test_bootstrap.py`

- [ ] **Step 1: Write failing plan builder tests**

Add test helpers:

```python
from openevo.remote.bootstrap import build_remote_bootstrap_plan
from openevo.science import ScienceProjectConfig
from openevo.sidecar import RemoteProfileConfig, build_sidecar_science_plan


def _profile(**extra) -> RemoteProfileConfig:
    payload = {
        "version": 1,
        "id": "lab-gpu",
        "host": "gpu.example.edu",
        "user": "alice",
        "proxy": {
            "https_proxy": "http://127.0.0.1:7890",
            "pip_index_url": "https://pypi.example/simple",
            "huggingface_endpoint": "https://hf-mirror.example",
            "hf_home": "/data/hf",
        },
    }
    payload.update(extra)
    return RemoteProfileConfig.model_validate(payload)


def _project(**extra) -> ScienceProjectConfig:
    payload = {
        "version": 1,
        "project": {"name": "protein-design"},
        "remote_profile": "lab-gpu",
        "task": {
            "id": "folding-baseline",
            "objective": "Improve folding.",
            "source": {"type": "remote_path", "path": "/datasets/folding"},
        },
    }
    payload.update(extra)
    return ScienceProjectConfig.model_validate(payload)
```

Add tests:

```python
def test_build_bootstrap_plan_for_subscription_mode() -> None:
    sidecar_plan = build_sidecar_science_plan(_project(), _profile())

    plan = build_remote_bootstrap_plan(sidecar_plan)

    assert plan.state_root == (
        "/home/alice/.openevo/runs/protein-design/folding-baseline"
    )
    assert plan.workspace_root == "/home/alice/.openevo/workspaces"
    assert plan.proxy_env["HTTPS_PROXY"] == "http://127.0.0.1:7890"
    assert [step.id for step in plan.steps] == [
        "ensure_state_root",
        "write_experiment_snapshot",
        "write_bootstrap_manifest",
        "check_codex_cli",
        "check_codex_auth",
        "docker_pull_runtime",
    ]
    docker_step = plan.steps[-1]
    assert docker_step.kind == RemoteBootstrapStepKind.DOCKER_PULL
    assert docker_step.network is True
    assert docker_step.env["HTTPS_PROXY"] == "http://127.0.0.1:7890"
    assert "docker pull openevo/science-runtime:0.1.0" in docker_step.command


def test_build_bootstrap_plan_for_managed_local_inference_includes_hf_prefetch() -> None:
    project = _project(
        execution={
            "mode": "codex_managed_local_inference",
            "hf_model": "Qwen/Qwen2.5-7B-Instruct",
        }
    )
    sidecar_plan = build_sidecar_science_plan(project, _profile())

    plan = build_remote_bootstrap_plan(sidecar_plan)

    step_ids = [step.id for step in plan.steps]
    assert "hf_snapshot_download" in step_ids
    hf_step = next(step for step in plan.steps if step.id == "hf_snapshot_download")
    assert hf_step.network is True
    assert hf_step.required is False
    assert hf_step.env["HF_ENDPOINT"] == "https://hf-mirror.example"
    assert "Qwen/Qwen2.5-7B-Instruct" in hf_step.command
```

- [ ] **Step 2: Verify RED**

Run:

```bash
PYTHONPATH=src /home/ziyi/ProRL-Agent-Server/.venv/bin/python -m pytest tests/openevo/remote/test_bootstrap.py::test_build_bootstrap_plan_for_subscription_mode tests/openevo/remote/test_bootstrap.py::test_build_bootstrap_plan_for_managed_local_inference_includes_hf_prefetch -q
```

Expected: fail because `build_remote_bootstrap_plan` is not implemented.

- [ ] **Step 3: Implement plan builder**

Implement:

```python
def build_remote_bootstrap_plan(plan: SidecarSciencePlan) -> RemoteBootstrapPlan:
    state_root = _state_root(plan)
    runtime_image = str(plan.experiment["runtime"]["image"])
    experiment_json = _json_document(plan.experiment)
    manifest = {
        "version": 1,
        "remote_profile_id": plan.remote_profile_id,
        "project_name": plan.project_name,
        "task_id": plan.task_id,
        "state_root": state_root,
        "workspace_root": plan.workspace.workspace_root,
        "runtime_image": runtime_image,
    }
    steps = [
        RemoteBootstrapStep(
            id="ensure_state_root",
            kind=RemoteBootstrapStepKind.ENSURE_DIR,
            command=f"mkdir -p {shlex.quote(state_root)}",
            timeout_seconds=30.0,
            network=False,
            required=True,
            remediation_kind="openevo_retry",
        ),
        RemoteBootstrapStep(
            id="write_experiment_snapshot",
            kind=RemoteBootstrapStepKind.WRITE_FILE,
            command=_write_file_command(
                posixpath.join(state_root, "experiment.json"),
                experiment_json,
            ),
            timeout_seconds=30.0,
            network=False,
            required=True,
            remediation_kind="openevo_retry",
        ),
        RemoteBootstrapStep(
            id="write_bootstrap_manifest",
            kind=RemoteBootstrapStepKind.WRITE_FILE,
            command=_write_file_command(
                posixpath.join(state_root, "bootstrap-manifest.json"),
                _json_document(manifest),
            ),
            timeout_seconds=30.0,
            network=False,
            required=True,
            remediation_kind="openevo_retry",
        ),
        RemoteBootstrapStep(
            id="check_codex_cli",
            kind=RemoteBootstrapStepKind.CHECK_COMMAND,
            command="codex --version",
            timeout_seconds=30.0,
            network=False,
            required=True,
            remediation_kind="user_action",
        ),
    ]
    if _auth_mode(plan) == "subscription":
        steps.append(
            RemoteBootstrapStep(
                id="check_codex_auth",
                kind=RemoteBootstrapStepKind.CHECK_COMMAND,
                command="test -f ~/.codex/auth.json",
                timeout_seconds=30.0,
                network=False,
                required=True,
                remediation_kind="user_action",
            )
        )
    steps.append(
        RemoteBootstrapStep(
            id="docker_pull_runtime",
            kind=RemoteBootstrapStepKind.DOCKER_PULL,
            command=f"docker pull {shlex.quote(runtime_image)}",
            env=dict(plan.proxy_env),
            timeout_seconds=300.0,
            network=True,
            required=True,
            remediation_kind="user_action",
        )
    )
    hf_model = _managed_hf_model(plan)
    if hf_model is not None:
        steps.append(
            RemoteBootstrapStep(
                id="hf_snapshot_download",
                kind=RemoteBootstrapStepKind.HF_SNAPSHOT_DOWNLOAD,
                command=(
                    "python3 -c "
                    + shlex.quote(
                        "from huggingface_hub import snapshot_download; "
                        f"snapshot_download({hf_model!r})"
                    )
                ),
                env=dict(plan.proxy_env),
                timeout_seconds=1800.0,
                network=True,
                required=False,
                remediation_kind="openevo_install",
            )
        )
    return RemoteBootstrapPlan(
        remote_profile_id=plan.remote_profile_id,
        project_name=plan.project_name,
        task_id=plan.task_id,
        proxy_env=dict(plan.proxy_env),
        state_root=state_root,
        workspace_root=plan.workspace.workspace_root,
        experiment_snapshot=dict(plan.experiment),
        steps=tuple(steps),
    )
```

Command details:

- `ensure_state_root`: `mkdir -p <state_root>` with `openevo_retry`.
- `write_experiment_snapshot`: `cat > <state_root>/experiment.json <<'OPENEVO_JSON'\n<json>\nOPENEVO_JSON`.
- `write_bootstrap_manifest`: same pattern for `bootstrap-manifest.json`.
- `check_codex_cli`: `codex --version`.
- `check_codex_auth`: `test -f ~/.codex/auth.json`.
- `docker_pull_runtime`: `docker pull <runtime_image>`, `network=True`, `env=dict(plan.proxy_env)`, remediation `user_action`.
- `hf_snapshot_download`: `python3 -c "from huggingface_hub import snapshot_download; snapshot_download('<model>')"`, `network=True`, `required=False`, remediation `openevo_install`.

Use a `_json_document(value)` helper implemented as
`json.dumps(value, indent=2, sort_keys=True, allow_nan=False)` and use
`shlex.quote()` for shell path/model/image quoting. Use safe slug roots:

```python
state_root = f"{workspace_root_root}/../runs/<project>/<task>" is not allowed.
state_root = "/home/alice/.openevo/runs/<slug project>/<slug task>"
```

Derive user home from `plan.workspace.workspace_root` if it starts with `/home/<user>/.openevo/workspaces`; otherwise use `<workspace_root>/../runs` normalized only via POSIX path helpers. For this slice, implement:

```python
def _state_root(plan):
    workspace_root = plan.workspace.workspace_root.rstrip("/")
    if workspace_root.endswith("/workspaces"):
        base = workspace_root.removesuffix("/workspaces") + "/runs"
    else:
        base = workspace_root + "/.openevo-runs"
    return posixpath.join(base, _slugify(plan.project_name), _slugify(plan.task_id))
```

- [ ] **Step 4: Verify GREEN**

Run:

```bash
PYTHONPATH=src /home/ziyi/ProRL-Agent-Server/.venv/bin/python -m pytest tests/openevo/remote/test_bootstrap.py -q
```

Expected: bootstrap model and plan tests pass.

- [ ] **Step 5: Commit Task 2**

```bash
git add src/openevo/remote/bootstrap.py tests/openevo/remote/test_bootstrap.py
git commit -m "feat: build remote bootstrap plans"
```

---

### Task 3: Bootstrap Executor

**Files:**
- Modify: `src/openevo/remote/bootstrap.py`
- Test: `tests/openevo/remote/test_bootstrap.py`

- [ ] **Step 1: Write failing executor tests**

Add a fake transport:

```python
from openevo.remote import RemoteCommandResult
from openevo.remote.bootstrap import execute_remote_bootstrap_plan


class BootstrapTransport:
    def __init__(self, *, fail_commands: set[str] | None = None) -> None:
        self.fail_commands = fail_commands or set()
        self.commands: list[tuple[str, dict[str, str] | None, float]] = []

    def run(self, command, *, cwd=None, env=None, timeout_seconds=30.0):
        self.commands.append((command, env, timeout_seconds))
        if command == 'df -Pk "$HOME"':
            return RemoteCommandResult(
                command=command,
                return_code=0,
                stdout=(
                    "Filesystem 1024-blocks Used Available Capacity Mounted on\n"
                    "/dev/root 100000000 1 99999999 1% /home\n"
                ),
            )
        if command in self.fail_commands:
            return RemoteCommandResult(command=command, return_code=42, stderr="boom")
        return RemoteCommandResult(command=command, return_code=0, stdout="ok")

    def upload_dir(self, local_path, remote_path):
        raise AssertionError("bootstrap should not upload directories")
```

Add tests:

```python
def test_execute_bootstrap_plan_runs_preflight_then_steps_with_proxy_env() -> None:
    plan = build_remote_bootstrap_plan(build_sidecar_science_plan(_project(), _profile()))
    transport = BootstrapTransport()

    report = execute_remote_bootstrap_plan(plan, transport)

    assert report.ready is True
    assert report.status == "pass"
    assert report.preflight is not None
    assert [step.status for step in report.steps] == [RemoteBootstrapStepStatus.PASS] * len(plan.steps)
    docker_command = next(step.command for step in plan.steps if step.id == "docker_pull_runtime")
    assert (docker_command, plan.proxy_env, 300.0) in transport.commands
    assert report.prepared_paths["state_root"] == plan.state_root


def test_execute_bootstrap_plan_blocks_steps_when_preflight_fails() -> None:
    plan = build_remote_bootstrap_plan(build_sidecar_science_plan(_project(), _profile()))
    transport = BootstrapTransport(fail_commands={"docker info"})

    report = execute_remote_bootstrap_plan(plan, transport)

    assert report.ready is False
    assert report.status == "fail"
    assert report.steps == ()
    assert report.next_actions == ("Fix remote preflight failures, then retry bootstrap.",)


def test_execute_bootstrap_plan_stops_after_required_step_failure() -> None:
    plan = build_remote_bootstrap_plan(build_sidecar_science_plan(_project(), _profile()))
    failing = next(step.command for step in plan.steps if step.id == "check_codex_cli")
    transport = BootstrapTransport(fail_commands={failing})

    report = execute_remote_bootstrap_plan(plan, transport, run_remote_preflight=False)

    assert report.ready is False
    assert report.status == "fail"
    assert [step.id for step in report.steps][-1] == "check_codex_cli"
    assert report.steps[-1].return_code == 42
    assert report.steps[-1].stderr == "boom"
    assert report.next_actions == ("Fix required bootstrap step check_codex_cli, then retry.",)


def test_execute_bootstrap_plan_continues_after_optional_hf_prefetch_failure() -> None:
    project = _project(
        execution={
            "mode": "codex_managed_local_inference",
            "hf_model": "Qwen/Qwen2.5-7B-Instruct",
        }
    )
    plan = build_remote_bootstrap_plan(build_sidecar_science_plan(project, _profile()))
    failing = next(step.command for step in plan.steps if step.id == "hf_snapshot_download")
    report = execute_remote_bootstrap_plan(
        plan,
        BootstrapTransport(fail_commands={failing}),
        run_remote_preflight=False,
    )

    assert report.ready is True
    assert report.status == "warn"
    assert report.steps[-1].status == RemoteBootstrapStepStatus.WARN
```

- [ ] **Step 2: Verify RED**

Run:

```bash
PYTHONPATH=src /home/ziyi/ProRL-Agent-Server/.venv/bin/python -m pytest tests/openevo/remote/test_bootstrap.py::test_execute_bootstrap_plan_runs_preflight_then_steps_with_proxy_env -q
```

Expected: fail because `execute_remote_bootstrap_plan` does not exist.

- [ ] **Step 3: Implement executor**

Implement:

```python
def execute_remote_bootstrap_plan(
    plan: RemoteBootstrapPlan,
    transport: RemoteExecutorTransport,
    *,
    run_remote_preflight: bool = True,
) -> RemoteBootstrapReport:
```

Semantics:

- If `run_remote_preflight`, call `run_preflight(transport, RemotePreflightSettings())`.
- If preflight not ready, return report with no step executions and next action telling user to fix preflight.
- For each step:
  - `env = step.env if step.network else step.env`.
  - run `transport.run(step.command, cwd=step.cwd, env=step.env or None, timeout_seconds=step.timeout_seconds)`.
  - Return `PASS` for rc 0.
  - Return `FAIL` for required rc != 0 and stop.
  - Return `WARN` for optional rc != 0 and continue.
  - Catch exceptions into fail/warn depending on required.
- `prepared_paths` includes `state_root`, `experiment_snapshot`, `bootstrap_manifest`.
- `next_actions`: ready pass -> `("Start remote OpenEvo services.",)`, warn -> `("Review bootstrap warnings before starting services.",)`, fail -> specific action.

- [ ] **Step 4: Verify GREEN**

Run:

```bash
PYTHONPATH=src /home/ziyi/ProRL-Agent-Server/.venv/bin/python -m pytest tests/openevo/remote/test_bootstrap.py -q
```

Expected: bootstrap executor tests pass.

- [ ] **Step 5: Commit Task 3**

```bash
git add src/openevo/remote/bootstrap.py tests/openevo/remote/test_bootstrap.py
git commit -m "feat: execute remote bootstrap plans"
```

---

### Task 4: Lifecycle Status Models

**Files:**
- Create: `src/openevo/remote/lifecycle.py`
- Modify: `src/openevo/remote/__init__.py`
- Test: `tests/openevo/remote/test_lifecycle.py`

- [ ] **Step 1: Write failing lifecycle tests**

Create `tests/openevo/remote/test_lifecycle.py`:

```python
from __future__ import annotations

import pytest

from openevo.remote.lifecycle import (
    RemoteDaemonLaunchSpec,
    RemoteLifecycleEvent,
    RemoteLifecycleStatus,
    RemoteServiceStatus,
    RemoteStatusReport,
)


def test_daemon_launch_spec_round_trips() -> None:
    spec = RemoteDaemonLaunchSpec(
        service_id="openevo-backend",
        kind="openevo_backend",
        command="openevo run remote.yaml",
        cwd="/home/alice/.openevo/runs/protein/folding",
        env={"OPENEVO_MODE": "science"},
        ports={"api": 18080},
        pid_file="/tmp/openevo.pid",
        log_path="/tmp/openevo.log",
        health_check="curl -fsS http://127.0.0.1:18080/health",
        depends_on=["vllm"],
    )

    restored = RemoteDaemonLaunchSpec.model_validate(spec.model_dump(mode="json"))

    assert restored == spec


def test_status_report_ready_semantics_and_tuple_backing() -> None:
    report = RemoteStatusReport(
        remote_profile_id="lab-gpu",
        project_name="protein",
        task_id="folding",
        bootstrap_ready=True,
        workspace_ready=True,
        services=[
            RemoteServiceStatus(
                service_id="openevo-backend",
                status=RemoteLifecycleStatus.RUNNING,
                message="healthy",
            )
        ],
        events=[
            RemoteLifecycleEvent(
                level="info",
                message="bootstrap completed",
                source="bootstrap",
            )
        ],
    )

    dumped = report.model_dump(mode="json")
    restored = RemoteStatusReport.model_validate(dumped)

    assert dumped["ready"] is True
    assert isinstance(report.services, tuple)
    assert isinstance(report.events, tuple)
    assert restored == report
    with pytest.raises(AttributeError):
        report.services.append(report.services[0])


def test_status_report_not_ready_when_service_failed() -> None:
    report = RemoteStatusReport(
        remote_profile_id="lab-gpu",
        project_name="protein",
        task_id="folding",
        bootstrap_ready=True,
        workspace_ready=True,
        services=[
            RemoteServiceStatus(
                service_id="openevo-backend",
                status=RemoteLifecycleStatus.FAILED,
                message="crashed",
            )
        ],
        actionable_errors=["Restart openevo-backend after reviewing logs."],
    )

    assert report.ready is False
```

- [ ] **Step 2: Verify RED**

Run:

```bash
PYTHONPATH=src /home/ziyi/ProRL-Agent-Server/.venv/bin/python -m pytest tests/openevo/remote/test_lifecycle.py -q
```

Expected: fail because `openevo.remote.lifecycle` does not exist.

- [ ] **Step 3: Implement lifecycle models**

Create strict frozen models:

- `RemoteLifecycleStatus`: `planned | starting | running | stopped | failed | unknown`.
- `RemoteDaemonLaunchSpec`: fields from the test.
- `RemoteServiceStatus`: `service_id`, `status`, `message`, optional `pid`, `log_path`, `health_check`, `last_checked_at`.
- `RemoteLifecycleEvent`: `level: info|warn|error`, `message`, `source`, optional `created_at`.
- `RemoteStatusReport`: `remote_profile_id`, `project_name`, `task_id`, `bootstrap_ready`, `workspace_ready`, tuple-backed `services`, `events`, `actionable_errors`; computed `ready`.

Ready is true only when bootstrap/workspace are ready, all services are `running` or `planned`, and no actionable errors exist.

Export symbols from `src/openevo/remote/__init__.py`.

- [ ] **Step 4: Verify GREEN**

Run:

```bash
PYTHONPATH=src /home/ziyi/ProRL-Agent-Server/.venv/bin/python -m pytest tests/openevo/remote/test_lifecycle.py -q
```

Expected: lifecycle tests pass.

- [ ] **Step 5: Commit Task 4**

```bash
git add src/openevo/remote/lifecycle.py src/openevo/remote/__init__.py tests/openevo/remote/test_lifecycle.py
git commit -m "feat: add remote lifecycle status models"
```

---

### Task 5: CLI Bootstrap Command

**Files:**
- Modify: `src/openevo/cli.py`
- Modify: `tests/openevo/test_cli.py`

- [ ] **Step 1: Write failing CLI tests**

Add imports:

```python
from openevo.remote import RemoteCommandResult
```

Add this CLI bootstrap fake transport:

```python
class _CliBootstrapTransport:
    profiles = []

    def __init__(self, profile=None) -> None:
        if profile is not None:
            self.profiles.append(profile)

    def run(
        self,
        command,
        *,
        cwd=None,
        env=None,
        timeout_seconds=30.0,
    ):
        if command == 'df -Pk "$HOME"':
            return RemoteCommandResult(
                command=command,
                return_code=0,
                stdout=(
                    "Filesystem 1024-blocks Used Available Capacity Mounted on\n"
                    "/dev/root 100000000 1 99999999 1% /home\n"
                ),
            )
        return RemoteCommandResult(command=command, return_code=0, stdout="ok")

    def upload_dir(self, local_path, remote_path):
        return None
```

Tests:

```python
def test_cli_sidecar_bootstrap_default_dry_run_outputs_report(
    tmp_path: Path,
    capsys,
) -> None:
    science_path = _write_config(tmp_path / "science.yaml", _minimal_science_payload())
    profile_path = _write_config(
        tmp_path / "remote.yaml",
        {
            "version": 1,
            "id": "science-team",
            "host": "gpu.example.edu",
            "user": "alice",
        },
    )

    exit_code = main(
        [
            "sidecar",
            "bootstrap",
            str(science_path),
            "--remote-profile",
            str(profile_path),
            "--json",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["ready"] is True
    assert payload["prepared_paths"]["state_root"].endswith(
        "/protein-design/folding-baseline"
    )


def test_cli_sidecar_bootstrap_transport_ssh_uses_ssh_transport(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    _CliRecordingSshTransport.profiles = []
    monkeypatch.setattr("openevo.cli.SshRemoteExecutorTransport", _CliRecordingSshTransport)
    science_path = _write_config(tmp_path / "science.yaml", _minimal_science_payload())
    profile_path = _write_config(
        tmp_path / "remote.yaml",
        {
            "version": 1,
            "id": "science-team",
            "host": "gpu.example.edu",
            "user": "alice",
        },
    )

    exit_code = main(
        [
            "sidecar",
            "bootstrap",
            str(science_path),
            "--remote-profile",
            str(profile_path),
            "--transport",
            "ssh",
            "--json",
        ]
    )

    assert exit_code == 0
    assert _CliRecordingSshTransport.profiles[0].id == "science-team"
```

- [ ] **Step 2: Verify RED**

Run:

```bash
PYTHONPATH=src /home/ziyi/ProRL-Agent-Server/.venv/bin/python -m pytest tests/openevo/test_cli.py::test_cli_sidecar_bootstrap_default_dry_run_outputs_report -q
```

Expected: fail because `sidecar bootstrap` does not exist.

- [ ] **Step 3: Implement CLI command**

Modify parser:

```python
bootstrap_parser = sidecar_subparsers.add_parser(
    "bootstrap",
    help="Prepare a remote OpenEvo run directory and return a bootstrap report.",
)
```

Arguments:

- `config`
- `--remote-profile`
- `--skip-preflight`
- `--transport`, choices `dry-run|ssh`, default `dry-run`
- `--json`

Import `build_remote_bootstrap_plan` and `execute_remote_bootstrap_plan`.

Add `_handle_sidecar_bootstrap(args)`:

```python
project = load_science_project_config(Path(args.config))
profile = load_remote_profile_config(Path(args.remote_profile))
sidecar_plan = build_sidecar_science_plan(project, profile)
bootstrap_plan = build_remote_bootstrap_plan(sidecar_plan)
report = execute_remote_bootstrap_plan(
    bootstrap_plan,
    _sidecar_transport(args, profile),
    run_remote_preflight=not args.skip_preflight,
)
```

Print JSON/YAML like execute. Return `0 if report.ready else 1`.

- [ ] **Step 4: Verify GREEN**

Run:

```bash
PYTHONPATH=src /home/ziyi/ProRL-Agent-Server/.venv/bin/python -m pytest tests/openevo/test_cli.py -q
```

Expected: CLI tests pass.

- [ ] **Step 5: Commit Task 5**

```bash
git add src/openevo/cli.py tests/openevo/test_cli.py
git commit -m "feat: expose sidecar bootstrap command"
```

---

### Task 6: Documentation and Final Verification

**Files:**
- Create: `docs/architecture/openevo-desktop-remote-bootstrap-lifecycle-foundation.md`
- Modify: `docs/architecture/openevo-desktop-remote-executor-foundation.md`

- [ ] **Step 1: Write docs**

Document:

- Bootstrap consumes `SidecarSciencePlan` and does not reimplement Science parsing.
- Bootstrap runs preflight, writes experiment snapshot/manifest, checks Codex subscription mode, pulls runtime image, and optionally prefetches HF model for managed local inference.
- Proxy env is applied to networked bootstrap steps. Docker daemon proxy/mirror is not configured automatically.
- Lifecycle status models are data/report contracts only; real daemon supervisor is out of scope for this slice.
- Unsupported: Docker/NVIDIA install, sudo/systemd, vLLM dynamic adapter lifecycle, credential vault, UI.
- Verification commands.

- [ ] **Step 2: Run final verification**

Run:

```bash
PYTHONPATH=src /home/ziyi/ProRL-Agent-Server/.venv/bin/python -m pytest tests/openevo/test_cli.py tests/openevo/sidecar tests/openevo/remote tests/openevo/science tests/evolution/test_models.py -q
PYTHONPATH=src /home/ziyi/ProRL-Agent-Server/.venv/bin/python -m pytest tests/openevo/science tests/evolution/test_models.py --collect-only -q >/tmp/openevo-remote-bootstrap-collect.txt
/home/ziyi/ProRL-Agent-Server/.venv/bin/ruff check src/openevo/remote src/openevo/cli.py tests/openevo/remote tests/openevo/test_cli.py
git diff --check openevo/stable...HEAD
```

- [ ] **Step 3: Commit docs**

```bash
git add docs/architecture/openevo-desktop-remote-bootstrap-lifecycle-foundation.md docs/architecture/openevo-desktop-remote-executor-foundation.md
git commit -m "docs: document remote bootstrap lifecycle"
```

- [ ] **Step 4: Final review**

Dispatch a fresh `gpt-5.5` high-effort reviewer to inspect `openevo/stable...HEAD` for model contracts, bootstrap command safety, proxy behavior, CLI docs, and test coverage.

- [ ] **Step 5: Publish**

Push branch, open PR against `stable`, include `Fixes #45`, test evidence, docs list, and merge when GitHub checks are clean.
