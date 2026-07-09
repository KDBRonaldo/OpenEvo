# OpenEvo Desktop Remote Executor Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a fakeable remote executor foundation that turns OpenEvo Desktop sidecar plans into structured preflight and workspace-preparation execution reports.

**Architecture:** The executor layer consumes the immutable `SidecarSciencePlan` produced by `openevo sidecar plan`, then runs remote preflight and workspace preparation through a transport protocol. The transport is deliberately small and fakeable: command execution plus directory upload. This slice does not implement a real SSH client, credential vault, remote backend server, Docker Compose lifecycle, vLLM lifecycle, or UI.

**Tech Stack:** Python 3.11+, Pydantic v2, existing `openevo.sidecar` and `openevo.remote.preflight` contracts, pytest, argparse CLI.

Tracked by issue #41.

---

## File Structure

- Create `src/openevo/remote/executor.py`: transport protocol, result models, workspace action executor, preflight integration, and `execute_sidecar_plan()`.
- Modify `src/openevo/remote/__init__.py`: export executor symbols.
- Modify `src/openevo/cli.py`: add `openevo sidecar execute CONFIG --remote-profile PROFILE [--skip-preflight] [--json]`.
- Create `tests/openevo/remote/test_executor.py`: fake transport tests for upload, git clone, remote path, preflight, proxy env, and failure reporting.
- Modify `tests/openevo/test_cli.py`: CLI JSON test for `sidecar execute`.
- Create `docs/architecture/openevo-desktop-remote-executor-foundation.md`: boundary, transport contract, execution semantics, limitations, and verification.

---

### Task 1: Remote Transport And Workspace Execution

**Files:**
- Create: `src/openevo/remote/executor.py`
- Modify: `src/openevo/remote/__init__.py`
- Test: `tests/openevo/remote/test_executor.py`

- [ ] **Step 1: Write failing tests**

Create `tests/openevo/remote/test_executor.py` with:

```python
from __future__ import annotations

from pathlib import Path

import pytest

from openevo.remote import RemoteCommandResult
from openevo.remote.executor import (
    RemoteExecutorTransport,
    WorkspaceActionStatus,
    execute_workspace_plan,
)
from openevo.science import ScienceProjectConfig
from openevo.sidecar import RemoteProfileConfig, build_sidecar_science_plan


class FakeTransport:
    def __init__(self, *, fail_commands: set[str] | None = None) -> None:
        self.fail_commands = fail_commands or set()
        self.commands: list[tuple[str, str | None, dict[str, str] | None]] = []
        self.uploads: list[tuple[str, str]] = []

    def run(
        self,
        command: str,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_seconds: float = 30.0,
    ) -> RemoteCommandResult:
        self.commands.append((command, cwd, env))
        if command in self.fail_commands:
            return RemoteCommandResult(
                command=command,
                return_code=23,
                stderr="boom",
            )
        return RemoteCommandResult(command=command, return_code=0, stdout="ok")

    def upload_dir(self, local_path: str, remote_path: str) -> None:
        self.uploads.append((local_path, remote_path))


def _profile() -> RemoteProfileConfig:
    return RemoteProfileConfig.model_validate(
        {
            "version": 1,
            "id": "lab-gpu",
            "host": "gpu.example.edu",
            "user": "alice",
            "proxy": {
                "https_proxy": "http://127.0.0.1:7890",
                "huggingface_endpoint": "https://hf-mirror.com",
            },
        }
    )


def _project(source: dict[str, str], tmp_path: Path | None = None) -> ScienceProjectConfig:
    path = None if tmp_path is None else tmp_path / "science.yaml"
    if path is not None:
        path.write_text("version: 1\n", encoding="utf-8")
    return ScienceProjectConfig.model_validate(
        {
            "version": 1,
            "project": {"name": "protein-design"},
            "remote_profile": "lab-gpu",
            "task": {
                "id": "folding-baseline",
                "objective": "Improve the folding baseline.",
                "source": source,
            },
            "path": path,
        }
    )


def test_transport_protocol_accepts_fake_transport() -> None:
    assert isinstance(FakeTransport(), RemoteExecutorTransport)


def test_execute_workspace_plan_uploads_local_folder(tmp_path: Path) -> None:
    source_dir = tmp_path / "workspace"
    source_dir.mkdir()
    plan = build_sidecar_science_plan(
        _project({"type": "local_folder", "path": "workspace"}, tmp_path),
        _profile(),
    )
    transport = FakeTransport()

    report = execute_workspace_plan(plan, transport)

    assert report.ready is True
    assert [item.status for item in report.actions] == [WorkspaceActionStatus.PASS]
    assert transport.uploads == [
        (str(source_dir), plan.workspace.actions[0].target)
    ]
    assert transport.commands == []


def test_execute_workspace_plan_runs_git_clone_with_proxy_env() -> None:
    plan = build_sidecar_science_plan(
        _project(
            {
                "type": "git_repository",
                "url": "https://github.com/example/research.git",
                "branch": "main",
            }
        ),
        _profile(),
    )
    transport = FakeTransport()

    report = execute_workspace_plan(plan, transport)

    assert report.ready is True
    action = plan.workspace.actions[0]
    assert transport.commands == [
        (
            str(action.command),
            None,
            {
                "HTTPS_PROXY": "http://127.0.0.1:7890",
                "https_proxy": "http://127.0.0.1:7890",
                "HF_ENDPOINT": "https://hf-mirror.com",
            },
        )
    ]


def test_execute_workspace_plan_marks_remote_path_as_skipped() -> None:
    plan = build_sidecar_science_plan(
        _project({"type": "remote_path", "path": "/datasets/folding"}),
        _profile(),
    )

    report = execute_workspace_plan(plan, FakeTransport())

    assert report.ready is True
    assert report.actions[0].status == WorkspaceActionStatus.SKIP
    assert report.actions[0].message == "Remote path already exists by contract."


def test_execute_workspace_plan_reports_command_failure() -> None:
    plan = build_sidecar_science_plan(
        _project({"type": "git_repository", "url": "https://github.com/example/research.git"}),
        _profile(),
    )
    command = str(plan.workspace.actions[0].command)

    report = execute_workspace_plan(plan, FakeTransport(fail_commands={command}))

    assert report.ready is False
    assert report.actions[0].status == WorkspaceActionStatus.FAIL
    assert report.actions[0].return_code == 23
    assert report.actions[0].stderr == "boom"
```

- [ ] **Step 2: Run tests to verify RED**

Run:

```bash
PYTHONPATH=src /home/ziyi/ProRL-Agent-Server/.venv/bin/python -m pytest tests/openevo/remote/test_executor.py -q
```

Expected: fail because `openevo.remote.executor` does not exist.

- [ ] **Step 3: Implement executor models and workspace execution**

Create `src/openevo/remote/executor.py` with:

```python
@runtime_checkable
class RemoteExecutorTransport(Protocol):
    def run(
        self,
        command: str,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_seconds: float = 30.0,
    ) -> RemoteCommandResult: ...

    def upload_dir(self, local_path: str, remote_path: str) -> None: ...

class WorkspaceActionStatus(StrEnum):
    PASS = "pass"
    SKIP = "skip"
    FAIL = "fail"

class WorkspaceActionExecution(_StrictFrozenModel):
    type: Literal["upload_dir", "git_clone", "use_remote_path"]
    task_id: str
    status: WorkspaceActionStatus
    message: str
    source: str | None = None
    target: str
    command: str | None = None
    return_code: int | None = None
    stdout: str = ""
    stderr: str = ""

class WorkspaceExecutionReport(_StrictFrozenModel):
    actions: tuple[WorkspaceActionExecution, ...] = Field(default_factory=tuple)

    @property
    def ready(self) -> bool:
        return all(action.status != WorkspaceActionStatus.FAIL for action in self.actions)
```

Implement:

```python
def execute_workspace_plan(
    plan: SidecarSciencePlan,
    transport: RemoteExecutorTransport,
) -> WorkspaceExecutionReport:
    ...
```

Semantics:

- `upload_dir`: call `transport.upload_dir(source, target)`, mark pass; if upload raises, mark fail with exception text.
- `git_clone`: call `transport.run(command, env=dict(plan.proxy_env))`, mark pass/fail from return code.
- `use_remote_path`: no transport call; mark skip with message `Remote path already exists by contract.`
- Preserve action type/task/source/target/command in execution records.

Export symbols from `src/openevo/remote/__init__.py`.

- [ ] **Step 4: Run tests to verify GREEN**

Run:

```bash
PYTHONPATH=src /home/ziyi/ProRL-Agent-Server/.venv/bin/python -m pytest tests/openevo/remote/test_executor.py tests/openevo/remote/test_preflight.py -q
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/openevo/remote/__init__.py src/openevo/remote/executor.py tests/openevo/remote/test_executor.py
git commit -m "feat: execute sidecar workspace plans"
```

---

### Task 2: Preflight Integration And Full Sidecar Execution Report

**Files:**
- Modify: `src/openevo/remote/executor.py`
- Test: `tests/openevo/remote/test_executor.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/openevo/remote/test_executor.py`:

```python
from openevo.remote.executor import execute_sidecar_plan


def test_execute_sidecar_plan_runs_preflight_before_workspace() -> None:
    plan = build_sidecar_science_plan(
        _project({"type": "remote_path", "path": "/datasets/folding"}),
        _profile(),
    )
    transport = FakeTransport()

    report = execute_sidecar_plan(plan, transport)

    assert report.ready is False
    assert [check.name for check in report.preflight.checks] == [
        "ssh",
        "docker",
        "docker_compose",
        "gpu",
        "disk",
        "codex_cli",
        "codex_subscription",
    ]
    assert transport.commands[0][0] == "true"
    assert report.workspace.actions == ()


def test_execute_sidecar_plan_can_skip_preflight() -> None:
    plan = build_sidecar_science_plan(
        _project({"type": "remote_path", "path": "/datasets/folding"}),
        _profile(),
    )

    report = execute_sidecar_plan(plan, FakeTransport(), run_remote_preflight=False)

    assert report.ready is True
    assert report.preflight is None
    assert report.workspace.actions[0].status == WorkspaceActionStatus.SKIP
```

This uses the current `run_preflight()` behavior: the fake transport returns `ok` for command execution but disk parsing fails for stdout `ok`, so the report should not be ready and workspace execution should not run when preflight fails.

- [ ] **Step 2: Run tests to verify RED**

Run:

```bash
PYTHONPATH=src /home/ziyi/ProRL-Agent-Server/.venv/bin/python -m pytest tests/openevo/remote/test_executor.py::test_execute_sidecar_plan_runs_preflight_before_workspace tests/openevo/remote/test_executor.py::test_execute_sidecar_plan_can_skip_preflight -q
```

Expected: fail because `execute_sidecar_plan` is missing.

- [ ] **Step 3: Implement full execution report**

Add:

```python
class SidecarExecutionReport(_StrictFrozenModel):
    remote_profile_id: str
    project_name: str
    task_id: str
    preflight: PreflightReport | None = None
    workspace: WorkspaceExecutionReport

    @property
    def ready(self) -> bool:
        return (self.preflight is None or self.preflight.ready) and self.workspace.ready

def execute_sidecar_plan(
    plan: SidecarSciencePlan,
    transport: RemoteExecutorTransport,
    *,
    run_remote_preflight: bool = True,
) -> SidecarExecutionReport:
    preflight = run_preflight(transport, plan.preflight) if run_remote_preflight else None
    if preflight is not None and not preflight.ready:
        workspace = WorkspaceExecutionReport(actions=())
    else:
        workspace = execute_workspace_plan(plan, transport)
    return SidecarExecutionReport(...)
```

Export `SidecarExecutionReport` and `execute_sidecar_plan`.

- [ ] **Step 4: Run tests to verify GREEN**

Run:

```bash
PYTHONPATH=src /home/ziyi/ProRL-Agent-Server/.venv/bin/python -m pytest tests/openevo/remote/test_executor.py tests/openevo/remote/test_preflight.py -q
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/openevo/remote/__init__.py src/openevo/remote/executor.py tests/openevo/remote/test_executor.py
git commit -m "feat: run sidecar preflight execution"
```

---

### Task 3: CLI And Documentation

**Files:**
- Modify: `src/openevo/cli.py`
- Modify: `tests/openevo/test_cli.py`
- Create: `docs/architecture/openevo-desktop-remote-executor-foundation.md`

- [ ] **Step 1: Write failing CLI tests**

Append to `tests/openevo/test_cli.py`:

```python
def test_cli_sidecar_execute_skip_preflight_outputs_workspace_report(
    tmp_path: Path,
    capsys,
) -> None:
    science_path = _write_config(
        tmp_path / "science.yaml",
        _minimal_science_payload()
        | {
            "task": {
                "id": "local-task",
                "objective": "Run local workflow.",
                "source": {
                    "type": "remote_path",
                    "path": "/datasets/local-task",
                },
            }
        },
    )
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
            "execute",
            str(science_path),
            "--remote-profile",
            str(profile_path),
            "--skip-preflight",
            "--json",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ready"] is True
    assert payload["preflight"] is None
    assert payload["workspace"]["actions"][0]["status"] == "skip"
```

- [ ] **Step 2: Run test to verify RED**

Run:

```bash
PYTHONPATH=src /home/ziyi/ProRL-Agent-Server/.venv/bin/python -m pytest tests/openevo/test_cli.py::test_cli_sidecar_execute_skip_preflight_outputs_workspace_report -q
```

Expected: fail because `sidecar execute` does not exist.

- [ ] **Step 3: Implement CLI with a local fake transport only**

In `src/openevo/cli.py`:

- Add `sidecar execute` parser with `config`, `--remote-profile`, `--skip-preflight`, `--json`.
- Add a private `_CliDryRunTransport` implementing `RemoteExecutorTransport`:
  - `run()` returns `RemoteCommandResult(command=command, return_code=0, stdout="ok")`.
  - `upload_dir()` records nothing and does not touch the filesystem.
- Add `_handle_sidecar_execute()` that builds the same sidecar plan and calls:

```python
execute_sidecar_plan(
    plan,
    _CliDryRunTransport(),
    run_remote_preflight=not args.skip_preflight,
)
```

This CLI is intentionally execution-shaped but still transport-fake by default. It is for Desktop/backend integration and testability until real SSH transport lands.

- [ ] **Step 4: Write docs**

Create `docs/architecture/openevo-desktop-remote-executor-foundation.md` covering:

- tracked by #41;
- transport boundary and fakeability;
- workspace action execution semantics;
- preflight gating behavior;
- CLI `sidecar execute`;
- why this slice still does not implement real SSH/SFTP, vault, remote service startup, Docker/vLLM lifecycle, or UI;
- validation commands.

- [ ] **Step 5: Run focused regression**

Run:

```bash
PYTHONPATH=src /home/ziyi/ProRL-Agent-Server/.venv/bin/python -m pytest tests/openevo/test_cli.py tests/openevo/sidecar tests/openevo/remote tests/openevo/science -q
git diff --check
```

Expected: all tests pass and diff check is clean.

- [ ] **Step 6: Commit**

```bash
git add src/openevo/cli.py tests/openevo/test_cli.py docs/architecture/openevo-desktop-remote-executor-foundation.md
git commit -m "feat: expose sidecar execution reports"
```

---

## Final Review

After all tasks:

1. Run:

```bash
PYTHONPATH=src /home/ziyi/ProRL-Agent-Server/.venv/bin/python -m pytest tests/openevo/test_cli.py tests/openevo/sidecar tests/openevo/remote tests/openevo/science tests/evolution/test_models.py -q
PYTHONPATH=src /home/ziyi/ProRL-Agent-Server/.venv/bin/python -m pytest tests/openevo/science tests/evolution/test_models.py --collect-only -q
/home/ziyi/ProRL-Agent-Server/.venv/bin/ruff check src/openevo/remote src/openevo/cli.py tests/openevo/remote tests/openevo/test_cli.py
git diff --check
```

2. Request final code review for the full diff.
3. Push branch:

```bash
git push https://github.com/CompLifeLab-ZJU/OpenEvo.git HEAD:refs/heads/codex/openevo-desktop-remote-executor
```

4. Open PR against `stable` with `Fixes #41`.
5. Merge after checks pass.
