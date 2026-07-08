# OpenEvo Desktop Science Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the first testable foundation for OpenEvo Desktop Science Projects: user-facing project config, compilation to existing OpenEvo experiment config, runtime prepare support, remote preflight contracts, and a dry-run CLI.

**Architecture:** Add a new `openevo.science` package above the existing `openevo.experiment` runner. The science layer hides runtime images from ordinary users, resolves managed environment profiles, and compiles prepared workspaces into the existing `ExperimentConfig` and `compile_experiment()` path. Add a small `openevo.remote` preflight contract used by the future Desktop sidecar and remote backend, without implementing a full SSH client in this slice.

**Tech Stack:** Python 3.11, Pydantic v2, PyYAML, existing OpenEvo experiment compiler, pytest.

---

## Scope Check

The approved Desktop spec spans several independent subsystems: Desktop UI, local sidecar, SSH/vault/tunnel management, remote backend, Docker Compose lifecycle, model-serving lifecycle, and science project compilation. This plan implements only the foundation slice that can be tested entirely in the existing Python package.

Subsequent plans should cover:

- local Desktop sidecar storage, vault, SSH, and tunnel lifecycle;
- remote backend API and Docker Compose stack lifecycle;
- Tauri/React UI for Science Projects;
- evolution timeline API and artifact diff rendering;
- managed image release metadata and update flow.

## File Structure

- Create `src/openevo/science/__init__.py`
  - Public exports for science project models and compiler.
- Create `src/openevo/science/models.py`
  - Strict Pydantic models for Desktop-level Science Project YAML.
- Create `src/openevo/science/compiler.py`
  - Convert a Science Project plus prepared workspace mapping into `ExperimentConfig`.
- Create `src/openevo/remote/__init__.py`
  - Public exports for remote preflight contracts.
- Create `src/openevo/remote/preflight.py`
  - Preflight check result models, fakeable command probe protocol, and default checks.
- Modify `src/openevo/experiment/models.py`
  - Expose `runtime.prepare` in the existing low-level config.
- Modify `src/openevo/experiment/compiler.py`
  - Pass configured prepare actions through and append workspace upload actions.
- Modify `src/openevo/cli.py`
  - Add `openevo science compile` dry-run command for the foundation contract.
- Create `tests/openevo/science/test_models.py`
  - Validate Science Project model defaults and guardrails.
- Create `tests/openevo/science/test_compiler.py`
  - Validate science-to-experiment compilation.
- Create `tests/openevo/remote/test_preflight.py`
  - Validate preflight classification using a fake probe.
- Modify `tests/openevo/test_experiment_models.py`
  - Cover low-level `runtime.prepare` validation.
- Modify `tests/openevo/test_experiment_compiler.py`
  - Cover prepare pass-through and ordering with upload actions.
- Modify `tests/openevo/test_cli.py`
  - Cover `openevo science compile --json`.
- Create `docs/architecture/openevo-desktop-science-foundation.md`
  - Document the new science project contract and foundation limitations.

## Task 1: Expose Runtime Prepare In Low-Level Experiment Config

**Files:**
- Modify: `src/openevo/experiment/models.py`
- Modify: `src/openevo/experiment/compiler.py`
- Modify: `tests/openevo/test_experiment_models.py`
- Modify: `tests/openevo/test_experiment_compiler.py`

- [ ] **Step 1: Write failing model tests for `runtime.prepare`**

Append these tests to `tests/openevo/test_experiment_models.py`:

```python
def test_runtime_prepare_accepts_exec_actions() -> None:
    payload = _minimal_payload()
    payload["runtime"]["prepare"] = [
        {
            "type": "exec",
            "command": "pip install -r requirements.txt",
            "cwd": "/polar/session/workspace",
            "env": {"PIP_INDEX_URL": "https://pypi.example/simple"},
        }
    ]

    config = ExperimentConfig.model_validate(payload)

    [action] = config.runtime.prepare
    assert action.type == "exec"
    assert action.command == "pip install -r requirements.txt"
    assert action.cwd == "/polar/session/workspace"
    assert action.env == {"PIP_INDEX_URL": "https://pypi.example/simple"}


def test_runtime_prepare_rejects_upload_without_target() -> None:
    payload = _minimal_payload()
    payload["runtime"]["prepare"] = [{"type": "upload_dir", "source": "/tmp/src"}]

    with pytest.raises(ValidationError, match="upload_dir requires source and target"):
        ExperimentConfig.model_validate(payload)
```

- [ ] **Step 2: Run model tests and verify they fail**

Run:

```bash
pytest tests/openevo/test_experiment_models.py::test_runtime_prepare_accepts_exec_actions tests/openevo/test_experiment_models.py::test_runtime_prepare_rejects_upload_without_target -q
```

Expected: FAIL because `RuntimeConfig` rejects the unknown `prepare` field.

- [ ] **Step 3: Add `RuntimePrepareActionConfig` to `models.py`**

In `src/openevo/experiment/models.py`, add this class above `RuntimeConfig`:

```python
class RuntimePrepareActionConfig(_StrictModel):
    type: Literal["upload_file", "upload_dir", "exec"]
    source: str | None = None
    target: str | None = None
    command: str | None = None
    cwd: str | None = None
    env: dict[str, str] | None = None

    @model_validator(mode="after")
    def _validate_fields(self) -> RuntimePrepareActionConfig:
        if self.type in {"upload_file", "upload_dir"}:
            if not self.source or not self.target:
                raise ValueError(f"{self.type} requires source and target")
            if self.command is not None or self.cwd is not None or self.env is not None:
                raise ValueError(f"{self.type} must not set command, cwd, or env")
        elif self.type == "exec":
            if not self.command:
                raise ValueError("exec requires command")
            if self.source is not None or self.target is not None:
                raise ValueError("exec must not set source or target")
        return self
```

Then add this field to `RuntimeConfig`:

```python
    prepare: list[RuntimePrepareActionConfig] = Field(default_factory=list)
```

Update `_runtime_has_non_default_overrides()` so prepare actions require an image:

```python
def _runtime_has_non_default_overrides(runtime: RuntimeConfig) -> bool:
    return (
        runtime.kind != "docker"
        or runtime.workdir != "/polar/session/workspace"
        or bool(runtime.env)
        or bool(runtime.prepare)
    )
```

- [ ] **Step 4: Run model tests and verify they pass**

Run:

```bash
pytest tests/openevo/test_experiment_models.py::test_runtime_prepare_accepts_exec_actions tests/openevo/test_experiment_models.py::test_runtime_prepare_rejects_upload_without_target -q
```

Expected: PASS.

- [ ] **Step 5: Write failing compiler test for prepare ordering**

Append this test to `tests/openevo/test_experiment_compiler.py`:

```python
def test_runtime_prepare_actions_precede_workspace_upload() -> None:
    config = _config(
        runtime={
            "image": "runtime:latest",
            "prepare": [
                {
                    "type": "exec",
                    "command": "python -m pip install -r requirements.txt",
                    "cwd": "/polar/session/workspace",
                }
            ],
        }
    )
    compiled = compile_experiment(config)

    payload = compiled.tasks[0].rollout_payload_for_round(0, context_artifact_ids=[])

    assert payload["runtime"]["prepare"] == [
        {
            "type": "exec",
            "command": "python -m pip install -r requirements.txt",
            "cwd": "/polar/session/workspace",
            "env": None,
            "source": None,
            "target": None,
        },
        {
            "type": "upload_dir",
            "source": "/root/codex54minitest/five_article_agentic_workflow_subset",
            "target": "/polar/session/workspace",
        },
    ]
```

- [ ] **Step 6: Run compiler test and verify it fails**

Run:

```bash
pytest tests/openevo/test_experiment_compiler.py::test_runtime_prepare_actions_precede_workspace_upload -q
```

Expected: FAIL because `_runtime_payload()` does not include configured prepare actions.

- [ ] **Step 7: Pass prepare actions through the compiler**

In `src/openevo/experiment/compiler.py`, update `_runtime_payload()`:

```python
def _runtime_payload(config: ExperimentConfig) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "backend": config.runtime.kind,
        "workdir": config.runtime.workdir,
    }
    if config.runtime.image is not None:
        payload["image"] = config.runtime.image
    if config.runtime.env:
        payload["env"] = dict(config.runtime.env)
    if config.runtime.prepare:
        payload["prepare"] = [
            action.model_dump(mode="json")
            for action in config.runtime.prepare
        ]
    return payload
```

Update `_runtime_for_task()` so workspace upload is appended after configured actions:

```python
def _runtime_for_task(
    runtime: dict[str, Any],
    task: TaskConfig,
    *,
    config_path: Path | None,
) -> dict[str, Any] | None:
    if not task.workspace and "image" not in runtime:
        return None

    payload = dict(runtime)
    if task.workspace:
        if "image" not in payload:
            raise ValueError("runtime.image is required when tasks[].workspace is set")
        prepare = list(payload.get("prepare") or [])
        prepare.append(
            {
                "type": "upload_dir",
                "source": _workspace_source(task, config_path),
                "target": payload["workdir"],
            }
        )
        payload["prepare"] = prepare
    return payload
```

- [ ] **Step 8: Run focused tests for Task 1**

Run:

```bash
pytest tests/openevo/test_experiment_models.py tests/openevo/test_experiment_compiler.py -q
```

Expected: PASS.

- [ ] **Step 9: Commit Task 1**

Run:

```bash
git status --short
git add src/openevo/experiment/models.py src/openevo/experiment/compiler.py tests/openevo/test_experiment_models.py tests/openevo/test_experiment_compiler.py
git diff --cached --check
git commit -m "feat: expose openevo runtime prepare actions"
```

Expected: commit includes only the four files listed above.

## Task 2: Add Science Project Models

**Files:**
- Create: `src/openevo/science/__init__.py`
- Create: `src/openevo/science/models.py`
- Create: `tests/openevo/science/test_models.py`

- [ ] **Step 1: Create test package directory**

Run:

```bash
mkdir -p tests/openevo/science
```

- [ ] **Step 2: Write failing Science Project model tests**

Create `tests/openevo/science/test_models.py`:

```python
from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from openevo.science.models import (
    ScienceProjectConfig,
    load_science_project_config,
)


def _minimal_payload() -> dict:
    return {
        "version": 1,
        "project": {"name": "literature-extraction"},
        "remote_profile": "lab-a100",
        "task": {
            "id": "extract-components",
            "objective": "Read papers, extract components, run checks, and write report.md.",
            "source": {
                "type": "remote_path",
                "path": "/data/projects/component-extraction",
            },
        },
    }


def test_minimal_science_project_defaults_to_managed_runtime_and_subscription() -> None:
    config = ScienceProjectConfig.model_validate(_minimal_payload())

    assert config.project.name == "literature-extraction"
    assert config.remote_profile == "lab-a100"
    assert config.task.id == "extract-components"
    assert config.task.source.type == "remote_path"
    assert config.environment.profile == "managed_science"
    assert config.environment.custom_image is None
    assert config.execution.mode == "codex_subscription_transcript"
    assert config.execution.codex_model == "gpt-5.1-codex-mini"
    assert config.execution.hf_model is None
    assert config.evolution.text_memory is True
    assert config.evolution.skill_bundle is True
    assert config.evolution.agent_system is True
    assert config.evolution.parametric_memory is False


def test_local_inference_requires_hf_model() -> None:
    payload = _minimal_payload()
    payload["execution"] = {"mode": "codex_managed_local_inference"}

    with pytest.raises(ValidationError, match="execution.hf_model is required"):
        ScienceProjectConfig.model_validate(payload)


def test_custom_image_profile_requires_custom_image() -> None:
    payload = _minimal_payload()
    payload["environment"] = {"profile": "custom_image"}

    with pytest.raises(ValidationError, match="environment.custom_image is required"):
        ScienceProjectConfig.model_validate(payload)


def test_subscription_mode_rejects_parametric_memory() -> None:
    payload = _minimal_payload()
    payload["evolution"] = {"parametric_memory": True}

    with pytest.raises(ValidationError, match="parametric_memory requires managed local inference"):
        ScienceProjectConfig.model_validate(payload)


def test_load_science_project_config_reads_yaml(tmp_path: Path) -> None:
    path = tmp_path / "science.yaml"
    path.write_text(yaml.safe_dump(_minimal_payload()), encoding="utf-8")

    config = load_science_project_config(path)

    assert config.path == path
    assert config.project.name == "literature-extraction"
```

- [ ] **Step 3: Run tests and verify import failure**

Run:

```bash
pytest tests/openevo/science/test_models.py -q
```

Expected: FAIL with `ModuleNotFoundError: No module named 'openevo.science'`.

- [ ] **Step 4: Create science package exports**

Create `src/openevo/science/__init__.py`:

```python
"""User-facing Science Project configuration for OpenEvo Desktop."""

from openevo.science.models import (
    EnvironmentConfig,
    EvolutionTargetsConfig,
    ExecutionConfig,
    ProjectInfo,
    ScienceProjectConfig,
    ScienceTaskConfig,
    TaskSourceConfig,
    load_science_project_config,
)

__all__ = [
    "EnvironmentConfig",
    "EvolutionTargetsConfig",
    "ExecutionConfig",
    "ProjectInfo",
    "ScienceProjectConfig",
    "ScienceTaskConfig",
    "TaskSourceConfig",
    "load_science_project_config",
]
```

- [ ] **Step 5: Implement `models.py`**

Create `src/openevo/science/models.py`:

```python
from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class ProjectInfo(_StrictModel):
    name: str = Field(min_length=1)
    description: str | None = None

    @field_validator("name", "description")
    @classmethod
    def _strip_text(cls, value: str | None, info) -> str | None:
        if value is None:
            return None
        return _strip_non_empty(value, f"project.{info.field_name}")


class TaskSourceConfig(_StrictModel):
    type: Literal["local_folder", "git_repository", "remote_path", "scratch"]
    path: str | None = None
    url: str | None = None
    branch: str | None = None

    @model_validator(mode="after")
    def _validate_source_fields(self) -> TaskSourceConfig:
        if self.type in {"local_folder", "remote_path"} and not self.path:
            raise ValueError(f"task.source.path is required for {self.type}")
        if self.type == "git_repository" and not self.url:
            raise ValueError("task.source.url is required for git_repository")
        if self.type == "scratch" and (self.path or self.url or self.branch):
            raise ValueError("scratch source must not set path, url, or branch")
        if self.type != "git_repository" and self.branch is not None:
            raise ValueError("task.source.branch is only valid for git_repository")
        return self

    @field_validator("path", "url", "branch")
    @classmethod
    def _strip_optional_text(cls, value: str | None, info) -> str | None:
        if value is None:
            return None
        return _strip_non_empty(value, f"task.source.{info.field_name}")


class ScienceTaskConfig(_StrictModel):
    id: str = Field(min_length=1)
    objective: str = Field(min_length=1)
    source: TaskSourceConfig = Field(default_factory=lambda: TaskSourceConfig(type="scratch"))
    setup_commands: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("id", "objective")
    @classmethod
    def _strip_required_text(cls, value: str, info) -> str:
        text = _strip_non_empty(value, f"task.{info.field_name}")
        if info.field_name == "id" and "/" in text:
            raise ValueError("task.id must not contain '/'")
        return text

    @field_validator("setup_commands")
    @classmethod
    def _strip_setup_commands(cls, value: list[str]) -> list[str]:
        return [
            _strip_non_empty(command, "task.setup_commands[]")
            for command in value
        ]


class EnvironmentConfig(_StrictModel):
    profile: Literal["managed_science", "python_research", "custom_image"] = "managed_science"
    custom_image: str | None = None
    env: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_custom_image(self) -> EnvironmentConfig:
        if self.profile == "custom_image" and not self.custom_image:
            raise ValueError("environment.custom_image is required for custom_image profile")
        if self.profile != "custom_image" and self.custom_image is not None:
            raise ValueError("environment.custom_image is only valid for custom_image profile")
        return self

    @field_validator("custom_image")
    @classmethod
    def _strip_custom_image(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _strip_non_empty(value, "environment.custom_image")


class ExecutionConfig(_StrictModel):
    mode: Literal[
        "codex_subscription_transcript",
        "codex_managed_local_inference",
    ] = "codex_subscription_transcript"
    codex_model: str = "gpt-5.1-codex-mini"
    hf_model: str | None = None

    @model_validator(mode="after")
    def _validate_mode_fields(self) -> ExecutionConfig:
        if self.mode == "codex_managed_local_inference" and not self.hf_model:
            raise ValueError("execution.hf_model is required for managed local inference")
        return self

    @field_validator("codex_model", "hf_model")
    @classmethod
    def _strip_models(cls, value: str | None, info) -> str | None:
        if value is None:
            return None
        return _strip_non_empty(value, f"execution.{info.field_name}")


class EvolutionTargetsConfig(_StrictModel):
    text_memory: bool = True
    skill_bundle: bool = True
    agent_system: bool = True
    parametric_memory: bool = False


class ScienceProjectConfig(_StrictModel):
    version: Literal[1] = 1
    project: ProjectInfo
    remote_profile: str = Field(min_length=1)
    task: ScienceTaskConfig
    environment: EnvironmentConfig = Field(default_factory=EnvironmentConfig)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    evolution: EvolutionTargetsConfig = Field(default_factory=EvolutionTargetsConfig)
    path: Path | None = None

    @field_validator("remote_profile")
    @classmethod
    def _strip_remote_profile(cls, value: str) -> str:
        return _strip_non_empty(value, "remote_profile")

    @model_validator(mode="after")
    def _validate_cross_fields(self) -> ScienceProjectConfig:
        if (
            self.execution.mode == "codex_subscription_transcript"
            and self.evolution.parametric_memory
        ):
            raise ValueError("parametric_memory requires managed local inference")
        return self


def load_science_project_config(path: Path) -> ScienceProjectConfig:
    if not path.exists():
        raise FileNotFoundError(f"Science Project config not found: {path}")
    if not path.is_file():
        raise ValueError(f"Science Project config path is not a file: {path}")
    try:
        with path.open(encoding="utf-8") as handle:
            loaded = yaml.safe_load(handle) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"Invalid YAML in {path}: {exc}") from exc
    if not isinstance(loaded, dict):
        raise ValueError(f"Science Project config {path} must contain a top-level mapping")
    return ScienceProjectConfig.model_validate({**loaded, "path": path})


def _strip_non_empty(value: str, field_name: str) -> str:
    text = str(value).strip()
    if not text:
        raise ValueError(f"{field_name} must be a non-empty string")
    return text
```

- [ ] **Step 6: Run Science model tests**

Run:

```bash
pytest tests/openevo/science/test_models.py -q
```

Expected: PASS.

- [ ] **Step 7: Commit Task 2**

Run:

```bash
git status --short
git add src/openevo/science/__init__.py src/openevo/science/models.py tests/openevo/science/test_models.py
git diff --cached --check
git commit -m "feat: add openevo science project models"
```

Expected: commit includes only the three files listed above.

## Task 3: Compile Science Projects To Existing Experiment Config

**Files:**
- Modify: `src/openevo/science/__init__.py`
- Create: `src/openevo/science/compiler.py`
- Create: `tests/openevo/science/test_compiler.py`

- [ ] **Step 1: Write failing compiler tests**

Create `tests/openevo/science/test_compiler.py`:

```python
from __future__ import annotations

import pytest

from openevo.science.compiler import PreparedWorkspace, compile_science_project
from openevo.science.models import ScienceProjectConfig


def _project(**overrides: object) -> ScienceProjectConfig:
    payload = {
        "version": 1,
        "project": {"name": "literature-extraction"},
        "remote_profile": "lab-a100",
        "task": {
            "id": "extract-components",
            "objective": "Read papers, extract components, run checks, and write report.md.",
            "source": {"type": "remote_path", "path": "/data/projects/component-extraction"},
            "setup_commands": ["python -m pip install -r requirements.txt"],
        },
    }
    payload.update(overrides)
    return ScienceProjectConfig.model_validate(payload)


def test_subscription_science_project_compiles_to_transcript_experiment() -> None:
    experiment = compile_science_project(_project())

    assert experiment.experiment.name == "literature-extraction"
    assert experiment.agent.preset == "codex"
    assert experiment.agent.auth == "subscription"
    assert experiment.agent.settings["capture_mode"] == "transcript"
    assert experiment.agent.model == "gpt-5.1-codex-mini"
    assert experiment.runtime.image == "openevo/science-runtime:0.1.0"
    assert experiment.runtime.workdir == "/polar/session/workspace"
    assert experiment.runtime.prepare[0].type == "exec"
    assert experiment.runtime.prepare[0].command == "python -m pip install -r requirements.txt"
    assert experiment.tasks[0].workspace == "/data/projects/component-extraction"
    assert experiment.tasks[0].instruction.startswith("Read papers")
    assert experiment.artifacts.parametric_memory.enabled is False


def test_local_inference_compiles_to_proxy_auth_and_hf_model_metadata() -> None:
    project = _project(
        execution={
            "mode": "codex_managed_local_inference",
            "codex_model": "Qwen/Qwen3.6-Coder",
            "hf_model": "Qwen/Qwen3.6-Coder",
        }
    )

    experiment = compile_science_project(project)

    assert experiment.agent.auth == "proxy"
    assert experiment.agent.model == "Qwen/Qwen3.6-Coder"
    assert experiment.agent.settings["auth_mode"] == "proxy"
    assert experiment.runtime.env["OPENEVO_MANAGED_HF_MODEL"] == "Qwen/Qwen3.6-Coder"
    assert experiment.tasks[0].metadata["openevo"]["execution_mode"] == (
        "codex_managed_local_inference"
    )


def test_local_folder_requires_prepared_workspace_mapping() -> None:
    project = _project(
        task={
            "id": "extract-components",
            "objective": "Do the extraction.",
            "source": {"type": "local_folder", "path": "/Users/me/project"},
        }
    )

    with pytest.raises(ValueError, match="prepared workspace is required"):
        compile_science_project(project)


def test_local_folder_uses_prepared_workspace_mapping() -> None:
    project = _project(
        task={
            "id": "extract-components",
            "objective": "Do the extraction.",
            "source": {"type": "local_folder", "path": "/Users/me/project"},
        }
    )

    experiment = compile_science_project(
        project,
        prepared_workspaces={
            "extract-components": PreparedWorkspace(
                path="/home/user/.openevo/workspaces/extract-components",
                source_fingerprint="sha256:abc",
            )
        },
    )

    assert experiment.tasks[0].workspace == "/home/user/.openevo/workspaces/extract-components"
    assert experiment.tasks[0].metadata["openevo"]["source_fingerprint"] == "sha256:abc"


def test_custom_image_profile_controls_runtime_image() -> None:
    experiment = compile_science_project(
        _project(environment={"profile": "custom_image", "custom_image": "my/runtime:cuda"})
    )

    assert experiment.runtime.image == "my/runtime:cuda"
```

- [ ] **Step 2: Run compiler tests and verify import failure**

Run:

```bash
pytest tests/openevo/science/test_compiler.py -q
```

Expected: FAIL because `openevo.science.compiler` does not exist.

- [ ] **Step 3: Implement science compiler**

Create `src/openevo/science/compiler.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from openevo.experiment.models import ExperimentConfig
from openevo.science.models import ScienceProjectConfig

MANAGED_RUNTIME_IMAGES = {
    "managed_science": "openevo/science-runtime:0.1.0",
    "python_research": "openevo/python-research-runtime:0.1.0",
}


@dataclass(frozen=True)
class PreparedWorkspace:
    path: str
    source_fingerprint: str | None = None


def compile_science_project(
    project: ScienceProjectConfig,
    *,
    prepared_workspaces: Mapping[str, PreparedWorkspace] | None = None,
) -> ExperimentConfig:
    prepared = dict(prepared_workspaces or {})
    workspace = _workspace_for_project(project, prepared)
    runtime_env = dict(project.environment.env)
    if project.execution.mode == "codex_managed_local_inference":
        runtime_env["OPENEVO_MANAGED_HF_MODEL"] = str(project.execution.hf_model)

    payload = {
        "version": 1,
        "experiment": {"name": project.project.name},
        "agent": _agent_payload(project),
        "runtime": {
            "image": _runtime_image(project),
            "workdir": "/polar/session/workspace",
            "env": runtime_env,
            "prepare": _prepare_actions(project),
        },
        "tasks": [
            {
                "id": project.task.id,
                "instruction": project.task.objective,
                "workspace": workspace,
                "metadata": _task_metadata(project, prepared.get(project.task.id)),
            }
        ],
        "artifacts": _artifact_controls(project),
    }
    return ExperimentConfig.model_validate(payload)


def _agent_payload(project: ScienceProjectConfig) -> dict[str, object]:
    if project.execution.mode == "codex_subscription_transcript":
        return {
            "preset": "codex",
            "model": project.execution.codex_model,
            "auth": "subscription",
            "settings": {
                "auth_mode": "subscription",
                "capture_mode": "transcript",
            },
        }
    return {
        "preset": "codex",
        "model": str(project.execution.hf_model),
        "auth": "proxy",
        "settings": {"auth_mode": "proxy"},
    }


def _runtime_image(project: ScienceProjectConfig) -> str:
    if project.environment.profile == "custom_image":
        return str(project.environment.custom_image)
    return MANAGED_RUNTIME_IMAGES[project.environment.profile]


def _prepare_actions(project: ScienceProjectConfig) -> list[dict[str, object]]:
    return [
        {
            "type": "exec",
            "command": command,
            "cwd": "/polar/session/workspace",
        }
        for command in project.task.setup_commands
    ]


def _workspace_for_project(
    project: ScienceProjectConfig,
    prepared: Mapping[str, PreparedWorkspace],
) -> str | None:
    source = project.task.source
    if source.type == "remote_path":
        return str(source.path)
    if source.type == "scratch":
        return None
    workspace = prepared.get(project.task.id)
    if workspace is None:
        raise ValueError(
            f"prepared workspace is required for task {project.task.id} "
            f"with source type {source.type}"
        )
    return workspace.path


def _task_metadata(
    project: ScienceProjectConfig,
    prepared_workspace: PreparedWorkspace | None,
) -> dict[str, object]:
    metadata = dict(project.task.metadata)
    openevo_metadata: dict[str, object] = {
        "project_name": project.project.name,
        "remote_profile": project.remote_profile,
        "source_type": project.task.source.type,
        "environment_profile": project.environment.profile,
        "execution_mode": project.execution.mode,
    }
    if prepared_workspace and prepared_workspace.source_fingerprint:
        openevo_metadata["source_fingerprint"] = prepared_workspace.source_fingerprint
    metadata["openevo"] = {
        **dict(metadata.get("openevo") or {}),
        **openevo_metadata,
    }
    return metadata


def _artifact_controls(project: ScienceProjectConfig) -> dict[str, object]:
    return {
        "text_memory": {"enabled": project.evolution.text_memory},
        "skill_bundle": {"enabled": project.evolution.skill_bundle},
        "agent_system": {"enabled": project.evolution.agent_system},
        "parametric_memory": {"enabled": project.evolution.parametric_memory},
    }
```

- [ ] **Step 4: Export compiler symbols**

Update `src/openevo/science/__init__.py`:

```python
from openevo.science.compiler import (
    MANAGED_RUNTIME_IMAGES,
    PreparedWorkspace,
    compile_science_project,
)
from openevo.science.models import (
    EnvironmentConfig,
    EvolutionTargetsConfig,
    ExecutionConfig,
    ProjectInfo,
    ScienceProjectConfig,
    ScienceTaskConfig,
    TaskSourceConfig,
    load_science_project_config,
)

__all__ = [
    "EnvironmentConfig",
    "EvolutionTargetsConfig",
    "ExecutionConfig",
    "MANAGED_RUNTIME_IMAGES",
    "PreparedWorkspace",
    "ProjectInfo",
    "ScienceProjectConfig",
    "ScienceTaskConfig",
    "TaskSourceConfig",
    "compile_science_project",
    "load_science_project_config",
]
```

- [ ] **Step 5: Run science compiler tests**

Run:

```bash
pytest tests/openevo/science/test_compiler.py -q
```

Expected: PASS.

- [ ] **Step 6: Run low-level compiler tests to catch integration breakage**

Run:

```bash
pytest tests/openevo/test_experiment_models.py tests/openevo/test_experiment_compiler.py tests/openevo/science -q
```

Expected: PASS.

- [ ] **Step 7: Commit Task 3**

Run:

```bash
git status --short
git add src/openevo/science/__init__.py src/openevo/science/compiler.py tests/openevo/science/test_compiler.py
git diff --cached --check
git commit -m "feat: compile science projects to openevo experiments"
```

Expected: commit includes only the three files listed above.

## Task 4: Add Remote Preflight Contracts

**Files:**
- Create: `src/openevo/remote/__init__.py`
- Create: `src/openevo/remote/preflight.py`
- Create: `tests/openevo/remote/test_preflight.py`

- [ ] **Step 1: Create test package directory**

Run:

```bash
mkdir -p tests/openevo/remote
```

- [ ] **Step 2: Write failing preflight tests**

Create `tests/openevo/remote/test_preflight.py`:

```python
from __future__ import annotations

from dataclasses import dataclass

from openevo.remote.preflight import (
    RemoteCommandResult,
    RemotePreflightSettings,
    run_preflight,
)


@dataclass
class FakeProbe:
    results: dict[str, RemoteCommandResult]

    def run(self, command: str, *, timeout_seconds: float = 30.0) -> RemoteCommandResult:
        return self.results.get(
            command,
            RemoteCommandResult(command=command, return_code=127, stderr="not found"),
        )


def _ok(command: str, stdout: str = "ok") -> RemoteCommandResult:
    return RemoteCommandResult(command=command, return_code=0, stdout=stdout)


def test_preflight_reports_ready_server() -> None:
    probe = FakeProbe(
        {
            "true": _ok("true"),
            "docker --version": _ok("docker --version", "Docker version 27.0.0"),
            "docker compose version": _ok("docker compose version", "Docker Compose version v2.28.0"),
            "nvidia-smi -L": _ok("nvidia-smi -L", "GPU 0: A100"),
            "df -Pk $HOME": _ok("df -Pk $HOME", "Filesystem 1024-blocks Used Available Capacity Mounted on\nx 100000000 1 99999999 1% /home"),
            "codex --version": _ok("codex --version", "codex 0.121.0"),
            "test -f ~/.codex/auth.json": _ok("test -f ~/.codex/auth.json", ""),
        }
    )

    report = run_preflight(
        probe,
        RemotePreflightSettings(require_codex_subscription=True),
    )

    assert report.ready is True
    assert {check.name: check.status for check in report.checks} == {
        "ssh": "pass",
        "docker": "pass",
        "docker_compose": "pass",
        "gpu": "pass",
        "disk": "pass",
        "codex_cli": "pass",
        "codex_subscription": "pass",
    }


def test_preflight_classifies_user_action_required_for_docker_permission() -> None:
    probe = FakeProbe(
        {
            "true": _ok("true"),
            "docker --version": RemoteCommandResult(
                command="docker --version",
                return_code=1,
                stderr="permission denied while trying to connect to Docker daemon",
            ),
            "docker compose version": _ok("docker compose version"),
            "nvidia-smi -L": _ok("nvidia-smi -L"),
            "df -Pk $HOME": _ok("df -Pk $HOME"),
        }
    )

    report = run_preflight(probe, RemotePreflightSettings())

    docker = report.by_name("docker")
    assert report.ready is False
    assert docker.status == "fail"
    assert docker.remediation_kind == "user_action"
    assert "Docker permission denied" in docker.message


def test_preflight_marks_missing_codex_login_when_subscription_required() -> None:
    probe = FakeProbe(
        {
            "true": _ok("true"),
            "docker --version": _ok("docker --version"),
            "docker compose version": _ok("docker compose version"),
            "nvidia-smi -L": _ok("nvidia-smi -L"),
            "df -Pk $HOME": _ok("df -Pk $HOME"),
            "codex --version": _ok("codex --version"),
            "test -f ~/.codex/auth.json": RemoteCommandResult(
                command="test -f ~/.codex/auth.json",
                return_code=1,
            ),
        }
    )

    report = run_preflight(
        probe,
        RemotePreflightSettings(require_codex_subscription=True),
    )

    codex_subscription = report.by_name("codex_subscription")
    assert codex_subscription.status == "fail"
    assert codex_subscription.remediation_kind == "user_action"
    assert "Codex subscription login was not found" in codex_subscription.message
```

- [ ] **Step 3: Run preflight tests and verify import failure**

Run:

```bash
pytest tests/openevo/remote/test_preflight.py -q
```

Expected: FAIL with `ModuleNotFoundError: No module named 'openevo.remote'`.

- [ ] **Step 4: Implement remote preflight package**

Create `src/openevo/remote/__init__.py`:

```python
"""Remote server management contracts for OpenEvo Desktop."""

from openevo.remote.preflight import (
    PreflightCheck,
    PreflightReport,
    RemoteCommandResult,
    RemotePreflightSettings,
    RemoteProbe,
    run_preflight,
)

__all__ = [
    "PreflightCheck",
    "PreflightReport",
    "RemoteCommandResult",
    "RemotePreflightSettings",
    "RemoteProbe",
    "run_preflight",
]
```

Create `src/openevo/remote/preflight.py`:

```python
from __future__ import annotations

from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field


CheckStatus = Literal["pass", "warn", "fail"]
RemediationKind = Literal["none", "openevo_retry", "openevo_install", "user_action"]


class RemoteCommandResult(BaseModel):
    command: str
    return_code: int
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.return_code == 0


class RemoteProbe(Protocol):
    def run(self, command: str, *, timeout_seconds: float = 30.0) -> RemoteCommandResult:
        ...


class RemotePreflightSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    require_codex_subscription: bool = False
    min_home_available_kb: int = Field(default=20_000_000, ge=0)


class PreflightCheck(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    status: CheckStatus
    message: str
    command: str | None = None
    remediation_kind: RemediationKind = "none"
    stdout: str = ""
    stderr: str = ""


class PreflightReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    checks: list[PreflightCheck]

    @property
    def ready(self) -> bool:
        return all(check.status != "fail" for check in self.checks)

    def by_name(self, name: str) -> PreflightCheck:
        for check in self.checks:
            if check.name == name:
                return check
        raise KeyError(name)


def run_preflight(
    probe: RemoteProbe,
    settings: RemotePreflightSettings | None = None,
) -> PreflightReport:
    config = settings or RemotePreflightSettings()
    checks = [
        _check_ssh(probe),
        _check_docker(probe),
        _check_docker_compose(probe),
        _check_gpu(probe),
        _check_disk(probe, config.min_home_available_kb),
    ]
    if config.require_codex_subscription:
        checks.append(_check_codex_cli(probe))
        checks.append(_check_codex_subscription(probe))
    return PreflightReport(checks=checks)


def _check_ssh(probe: RemoteProbe) -> PreflightCheck:
    result = probe.run("true", timeout_seconds=10.0)
    if result.ok:
        return _pass("ssh", "SSH command execution succeeded.", result)
    return _fail("ssh", "SSH command execution failed.", result, "user_action")


def _check_docker(probe: RemoteProbe) -> PreflightCheck:
    result = probe.run("docker --version")
    if result.ok:
        return _pass("docker", "Docker is available.", result)
    stderr = result.stderr.lower()
    if "permission denied" in stderr:
        return _fail(
            "docker",
            "Docker permission denied for the remote user.",
            result,
            "user_action",
        )
    return _fail("docker", "Docker is not available.", result, "openevo_install")


def _check_docker_compose(probe: RemoteProbe) -> PreflightCheck:
    result = probe.run("docker compose version")
    if result.ok:
        return _pass("docker_compose", "Docker Compose is available.", result)
    return _fail("docker_compose", "Docker Compose is not available.", result, "openevo_install")


def _check_gpu(probe: RemoteProbe) -> PreflightCheck:
    result = probe.run("nvidia-smi -L")
    if result.ok:
        return _pass("gpu", "NVIDIA GPU is visible.", result)
    return _fail("gpu", "NVIDIA GPU is not visible to the remote shell.", result, "user_action")


def _check_disk(probe: RemoteProbe, min_available_kb: int) -> PreflightCheck:
    result = probe.run("df -Pk $HOME")
    if not result.ok:
        return _fail("disk", "Could not inspect remote home disk space.", result, "user_action")
    available = _parse_df_available_kb(result.stdout)
    if available is None:
        return _fail("disk", "Could not parse remote disk space output.", result, "user_action")
    if available < min_available_kb:
        return _fail(
            "disk",
            f"Remote home has {available} KiB available; {min_available_kb} KiB required.",
            result,
            "user_action",
        )
    return _pass("disk", "Remote home has enough available disk space.", result)


def _check_codex_cli(probe: RemoteProbe) -> PreflightCheck:
    result = probe.run("codex --version")
    if result.ok:
        return _pass("codex_cli", "Codex CLI is available.", result)
    return _fail("codex_cli", "Codex CLI is not installed on the remote server.", result, "user_action")


def _check_codex_subscription(probe: RemoteProbe) -> PreflightCheck:
    result = probe.run("test -f ~/.codex/auth.json")
    if result.ok:
        return _pass("codex_subscription", "Codex subscription login file exists.", result)
    return _fail(
        "codex_subscription",
        "Codex subscription login was not found at ~/.codex/auth.json.",
        result,
        "user_action",
    )


def _parse_df_available_kb(stdout: str) -> int | None:
    lines = [line.split() for line in stdout.splitlines() if line.split()]
    if len(lines) < 2 or len(lines[1]) < 4:
        return None
    try:
        return int(lines[1][3])
    except ValueError:
        return None


def _pass(name: str, message: str, result: RemoteCommandResult) -> PreflightCheck:
    return PreflightCheck(
        name=name,
        status="pass",
        message=message,
        command=result.command,
        stdout=result.stdout,
        stderr=result.stderr,
    )


def _fail(
    name: str,
    message: str,
    result: RemoteCommandResult,
    remediation_kind: RemediationKind,
) -> PreflightCheck:
    return PreflightCheck(
        name=name,
        status="fail",
        message=message,
        command=result.command,
        remediation_kind=remediation_kind,
        stdout=result.stdout,
        stderr=result.stderr,
    )
```

- [ ] **Step 5: Run preflight tests**

Run:

```bash
pytest tests/openevo/remote/test_preflight.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit Task 4**

Run:

```bash
git status --short
git add src/openevo/remote/__init__.py src/openevo/remote/preflight.py tests/openevo/remote/test_preflight.py
git diff --cached --check
git commit -m "feat: add openevo remote preflight contracts"
```

Expected: commit includes only the three files listed above.

## Task 5: Add `openevo science compile` Dry-Run CLI

**Files:**
- Modify: `src/openevo/cli.py`
- Modify: `tests/openevo/test_cli.py`

- [ ] **Step 1: Write failing CLI test**

Append this test to `tests/openevo/test_cli.py`:

```python
def test_cli_science_compile_outputs_experiment_config(
    tmp_path: Path,
    capsys,
) -> None:
    config_path = tmp_path / "science.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "project": {"name": "literature-extraction"},
                "remote_profile": "lab-a100",
                "task": {
                    "id": "extract-components",
                    "objective": "Read papers and write report.md.",
                    "source": {"type": "remote_path", "path": "/data/project"},
                },
            }
        ),
        encoding="utf-8",
    )

    exit_code = main(["science", "compile", str(config_path), "--json"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["experiment"]["name"] == "literature-extraction"
    assert payload["agent"]["auth"] == "subscription"
    assert payload["agent"]["settings"]["capture_mode"] == "transcript"
    assert payload["runtime"]["image"] == "openevo/science-runtime:0.1.0"
    assert payload["tasks"][0]["workspace"] == "/data/project"


def test_cli_science_compile_accepts_prepared_workspace(
    tmp_path: Path,
    capsys,
) -> None:
    config_path = tmp_path / "science.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "project": {"name": "local-folder-project"},
                "remote_profile": "lab-a100",
                "task": {
                    "id": "local-task",
                    "objective": "Analyze local files.",
                    "source": {"type": "local_folder", "path": "/Users/me/project"},
                },
            }
        ),
        encoding="utf-8",
    )

    exit_code = main(
        [
            "science",
            "compile",
            str(config_path),
            "--prepared-workspace",
            "local-task=/home/user/.openevo/workspaces/local-task",
            "--json",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["tasks"][0]["workspace"] == "/home/user/.openevo/workspaces/local-task"
```

- [ ] **Step 2: Run CLI tests and verify parser failure**

Run:

```bash
pytest tests/openevo/test_cli.py::test_cli_science_compile_outputs_experiment_config tests/openevo/test_cli.py::test_cli_science_compile_accepts_prepared_workspace -q
```

Expected: FAIL because the `science` command is not registered.

- [ ] **Step 3: Add CLI parser for `science compile`**

In `src/openevo/cli.py`, add imports:

```python
from openevo.science.compiler import PreparedWorkspace, compile_science_project
from openevo.science.models import load_science_project_config
```

In `build_parser()`, after the `run` parser setup, add:

```python
    science_parser = subparsers.add_parser(
        "science",
        help="Work with OpenEvo Desktop Science Project configs.",
    )
    science_subparsers = science_parser.add_subparsers(
        dest="science_command",
        required=True,
    )
    compile_parser = science_subparsers.add_parser(
        "compile",
        help="Compile a Science Project config into an OpenEvo experiment config.",
    )
    compile_parser.add_argument("config", help="Path to Science Project YAML.")
    compile_parser.add_argument(
        "--prepared-workspace",
        action="append",
        default=[],
        help="Prepared workspace mapping in task_id=/remote/path form. Can be repeated.",
    )
    compile_parser.add_argument("--json", action="store_true", help="Print JSON output.")
```

In `main()`, add the command dispatch:

```python
        if args.command == "science":
            return _handle_science(args)
```

- [ ] **Step 4: Add CLI handlers**

Append these functions to `src/openevo/cli.py` above `_print_result()`:

```python
def _handle_science(args: argparse.Namespace) -> int:
    if args.science_command == "compile":
        return _handle_science_compile(args)
    raise ValueError(f"Unknown science command: {args.science_command}")


def _handle_science_compile(args: argparse.Namespace) -> int:
    config = load_science_project_config(Path(args.config))
    experiment = compile_science_project(
        config,
        prepared_workspaces=_parse_prepared_workspaces(args.prepared_workspace),
    )
    payload = experiment.model_dump(mode="json", exclude={"path"})
    if args.json:
        print(_json_dumps(payload), end="")
    else:
        print(yaml.safe_dump(payload, sort_keys=True), end="")
    return 0


def _parse_prepared_workspaces(values: list[str]) -> dict[str, PreparedWorkspace]:
    result: dict[str, PreparedWorkspace] = {}
    for value in values:
        if "=" not in value:
            raise ValueError("--prepared-workspace must use task_id=/remote/path")
        task_id, path = value.split("=", 1)
        task_id = task_id.strip()
        path = path.strip()
        if not task_id or not path:
            raise ValueError("--prepared-workspace must use task_id=/remote/path")
        result[task_id] = PreparedWorkspace(path=path)
    return result
```

Also add `import yaml` near the existing imports.

- [ ] **Step 5: Run focused CLI tests**

Run:

```bash
pytest tests/openevo/test_cli.py::test_cli_science_compile_outputs_experiment_config tests/openevo/test_cli.py::test_cli_science_compile_accepts_prepared_workspace -q
```

Expected: PASS.

- [ ] **Step 6: Run all OpenEvo CLI tests**

Run:

```bash
pytest tests/openevo/test_cli.py tests/openevo/science -q
```

Expected: PASS.

- [ ] **Step 7: Commit Task 5**

Run:

```bash
git status --short
git add src/openevo/cli.py tests/openevo/test_cli.py
git diff --cached --check
git commit -m "feat: add science project compile CLI"
```

Expected: commit includes only the two files listed above.

## Task 6: Document The Foundation Contract

**Files:**
- Create: `docs/architecture/openevo-desktop-science-foundation.md`

- [ ] **Step 1: Write architecture documentation**

Create `docs/architecture/openevo-desktop-science-foundation.md`:

```markdown
# OpenEvo Desktop Science Foundation

OpenEvo Desktop Science Projects are user-facing configs that compile to the
existing OpenEvo experiment runner. They are intentionally higher level than
`ExperimentConfig`: ordinary users choose a task source, execution mode,
environment profile, and evolution targets, while OpenEvo chooses the runtime
image and Polar capture settings.

## Boundary

The science layer does not run Codex, start Docker, open SSH connections, or call
model APIs. It only validates user-facing project config and compiles it into
the existing OpenEvo/Polar experiment contract.

## Task Sources

Supported task sources are:

- `remote_path`: a remote directory that the remote backend can copy into the
  runtime workspace.
- `scratch`: no uploaded source directory; the agent starts in an empty managed
  workspace.
- `local_folder`: requires the Desktop sidecar to upload files first and pass a
  prepared remote workspace path.
- `git_repository`: requires the remote backend to clone the repo first and pass
  a prepared remote workspace path.

## Environment Profiles

`managed_science` maps to `openevo/science-runtime:0.1.0`.
`python_research` maps to `openevo/python-research-runtime:0.1.0`.
`custom_image` is available for developer-mode overrides.

## Execution Modes

`codex_subscription_transcript` compiles to Codex subscription auth with explicit
`capture_mode=transcript`. Token-level metrics are unavailable in this mode.

`codex_managed_local_inference` compiles to proxy auth and records the Hugging
Face model in runtime environment as `OPENEVO_MANAGED_HF_MODEL`. The remote
backend is responsible for starting vLLM and wiring the Polar gateway.

## Preflight

The `openevo.remote.preflight` module defines fakeable contracts for remote
checks. It does not implement SSH. The future sidecar should provide a
`RemoteProbe` implementation that executes commands over SSH and feeds results
into `run_preflight()`.
```

- [ ] **Step 2: Verify docs formatting**

Run:

```bash
git diff --check -- docs/architecture/openevo-desktop-science-foundation.md
```

Expected: no output and exit code 0.

- [ ] **Step 3: Commit Task 6**

Run:

```bash
git status --short
git add docs/architecture/openevo-desktop-science-foundation.md
git diff --cached --check
git commit -m "docs: document openevo science foundation contract"
```

Expected: commit includes only the architecture doc.

## Task 7: Final Verification

**Files:**
- Review all files changed by Tasks 1-6.

- [ ] **Step 1: Run focused OpenEvo tests**

Run:

```bash
pytest tests/openevo/test_experiment_models.py tests/openevo/test_experiment_compiler.py tests/openevo/test_cli.py tests/openevo/science tests/openevo/remote -q
```

Expected: PASS.

- [ ] **Step 2: Run import smoke test in a fresh Python process**

Run:

```bash
python - <<'PY'
from openevo.science import ScienceProjectConfig, compile_science_project
from openevo.remote import RemotePreflightSettings, run_preflight

print(ScienceProjectConfig.__name__)
print(compile_science_project.__name__)
print(RemotePreflightSettings.__name__)
print(run_preflight.__name__)
PY
```

Expected output:

```text
ScienceProjectConfig
compile_science_project
RemotePreflightSettings
run_preflight
```

- [ ] **Step 3: Run patch whitespace check**

Run:

```bash
git diff --check HEAD
```

Expected: no output and exit code 0.

- [ ] **Step 4: Review final diff**

Run:

```bash
git status --short
git diff HEAD -- src/openevo/experiment/models.py src/openevo/experiment/compiler.py src/openevo/science src/openevo/remote src/openevo/cli.py tests/openevo docs/architecture/openevo-desktop-science-foundation.md
```

Expected: no unstaged changes if each task commit was completed. If there are unstaged changes, review that they belong to this plan before committing them.
