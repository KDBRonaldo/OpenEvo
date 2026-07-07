# OpenEvo Desktop Sidecar Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the first publishable OpenEvo Desktop sidecar contract for remote profiles, proxy settings, workspace preparation dry-runs, Science Project compilation integration, and preflight settings.

**Architecture:** The sidecar layer is local/Desktop-facing and deterministic. It validates a remote profile YAML, plans how local or git task sources become prepared remote workspaces, maps the plan into `openevo.science.PreparedWorkspace`, and derives remote preflight settings from the Science Project execution mode. It does not open SSH connections, upload files, start Docker, start vLLM, store secrets, or render the Desktop UI.

**Tech Stack:** Python 3, Pydantic v2, existing `openevo.science` and `openevo.remote.preflight` contracts, pytest, argparse CLI.

Tracked by issue #39.

---

## File Structure

- Create `src/openevo/sidecar/__init__.py`: public exports for sidecar models, planning helpers, and Science Project plan builder.
- Create `src/openevo/sidecar/models.py`: strict remote profile, SSH auth, and proxy/mirror configuration models plus YAML loader.
- Create `src/openevo/sidecar/workspace.py`: workspace preparation action/plan models and deterministic planning for all Science task source types.
- Create `src/openevo/sidecar/planner.py`: high-level `SidecarSciencePlan` that combines workspace preparation, proxy env, preflight settings, and compiled `ExperimentConfig`.
- Modify `src/openevo/cli.py`: add `openevo sidecar plan CONFIG --remote-profile PROFILE [--json]`.
- Create `tests/openevo/sidecar/test_models.py`: validation tests for remote profiles and proxy rendering.
- Create `tests/openevo/sidecar/test_workspace.py`: workspace preparation tests and science compiler mapping tests.
- Create `tests/openevo/sidecar/test_planner.py`: preflight mode and high-level plan tests.
- Modify `tests/openevo/test_cli.py`: CLI JSON test for `sidecar plan`.
- Create `docs/architecture/openevo-desktop-sidecar-foundation.md`: architecture boundary, config contracts, and limitations.

---

### Task 1: Remote Profile And Proxy Models

**Files:**
- Create: `src/openevo/sidecar/__init__.py`
- Create: `src/openevo/sidecar/models.py`
- Test: `tests/openevo/sidecar/test_models.py`

- [ ] **Step 1: Write failing tests**

Create `tests/openevo/sidecar/test_models.py` with:

```python
from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from openevo.sidecar import (
    ProxySettings,
    RemoteProfileConfig,
    load_remote_profile_config,
)


def test_remote_profile_defaults_to_ssh_agent_and_user_home_workspace() -> None:
    profile = RemoteProfileConfig.model_validate(
        {
            "version": 1,
            "id": "lab-gpu",
            "host": "gpu.example.edu",
            "user": "alice",
        }
    )

    assert profile.auth.method == "ssh_agent"
    assert profile.port == 22
    assert profile.effective_workspace_root == "/home/alice/.openevo/workspaces"
    assert profile.proxy.to_env() == {}


def test_remote_profile_rejects_raw_secret_fields() -> None:
    with pytest.raises(ValidationError):
        RemoteProfileConfig.model_validate(
            {
                "version": 1,
                "id": "lab-gpu",
                "host": "gpu.example.edu",
                "user": "alice",
                "auth": {
                    "method": "password_ref",
                    "password": "plain-text-secret",
                },
            }
        )


def test_private_key_auth_requires_path_and_allows_passphrase_ref() -> None:
    profile = RemoteProfileConfig.model_validate(
        {
            "version": 1,
            "id": "lab-gpu",
            "host": "gpu.example.edu",
            "user": "alice",
            "auth": {
                "method": "private_key",
                "private_key_path": "~/.ssh/id_ed25519",
                "passphrase_ref": "vault:ssh-passphrase",
            },
        }
    )

    assert profile.auth.private_key_path == "~/.ssh/id_ed25519"
    assert profile.auth.passphrase_ref == "vault:ssh-passphrase"


def test_password_ref_auth_requires_reference_not_value() -> None:
    profile = RemoteProfileConfig.model_validate(
        {
            "version": 1,
            "id": "lab-gpu",
            "host": "gpu.example.edu",
            "user": "alice",
            "auth": {
                "method": "password_ref",
                "password_ref": "keyring:openevo/lab-gpu",
            },
        }
    )

    assert profile.auth.password_ref == "keyring:openevo/lab-gpu"


def test_proxy_settings_render_network_environment() -> None:
    proxy = ProxySettings.model_validate(
        {
            "http_proxy": "http://127.0.0.1:7890",
            "https_proxy": "http://127.0.0.1:7890",
            "no_proxy": "localhost,127.0.0.1",
            "pip_index_url": "https://pypi.tuna.tsinghua.edu.cn/simple",
            "huggingface_endpoint": "https://hf-mirror.com",
            "hf_home": "/data/hf-cache",
            "extra_env": {"CUSTOM_TOKEN_PATH": "/run/secrets/token"},
        }
    )

    assert proxy.to_env() == {
        "CUSTOM_TOKEN_PATH": "/run/secrets/token",
        "HTTP_PROXY": "http://127.0.0.1:7890",
        "http_proxy": "http://127.0.0.1:7890",
        "HTTPS_PROXY": "http://127.0.0.1:7890",
        "https_proxy": "http://127.0.0.1:7890",
        "NO_PROXY": "localhost,127.0.0.1",
        "no_proxy": "localhost,127.0.0.1",
        "PIP_INDEX_URL": "https://pypi.tuna.tsinghua.edu.cn/simple",
        "HF_ENDPOINT": "https://hf-mirror.com",
        "HF_HOME": "/data/hf-cache",
    }


def test_load_remote_profile_config_sets_path(tmp_path: Path) -> None:
    profile_path = tmp_path / "remote.yaml"
    profile_path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "id": "lab-gpu",
                "host": "gpu.example.edu",
                "user": "alice",
            }
        ),
        encoding="utf-8",
    )

    profile = load_remote_profile_config(profile_path)

    assert profile.path == profile_path
```

- [ ] **Step 2: Run tests to verify RED**

Run:

```bash
PYTHONPATH=src /home/ziyi/ProRL-Agent-Server/.venv/bin/python -m pytest tests/openevo/sidecar/test_models.py -q
```

Expected: fails because `openevo.sidecar` does not exist.

- [ ] **Step 3: Implement models**

Create `src/openevo/sidecar/models.py` with strict Pydantic models:

```python
class SSHAuthConfig(_StrictModel):
    method: Literal["ssh_agent", "private_key", "password_ref"] = "ssh_agent"
    private_key_path: str | None = None
    password_ref: str | None = None
    passphrase_ref: str | None = None

class ProxySettings(_StrictModel):
    http_proxy: str | None = None
    https_proxy: str | None = None
    no_proxy: str | None = None
    docker_registry_mirror: str | None = None
    pip_index_url: str | None = None
    huggingface_endpoint: str | None = None
    hf_home: str | None = None
    extra_env: dict[str, str] = Field(default_factory=dict)

    def to_env(self) -> dict[str, str]:
        env = dict(self.extra_env)
        ...
        return env

class RemoteProfileConfig(_StrictModel):
    version: Literal[1] = 1
    id: str = Field(min_length=1)
    name: str | None = None
    host: str = Field(min_length=1)
    port: int = Field(default=22, ge=1, le=65535)
    user: str = Field(min_length=1)
    auth: SSHAuthConfig = Field(default_factory=SSHAuthConfig)
    proxy: ProxySettings = Field(default_factory=ProxySettings)
    workspace_root: str | None = None
    min_home_available_kb: int = Field(default=20_000_000, ge=0)
    path: Path | None = None

    @property
    def effective_workspace_root(self) -> str:
        if self.workspace_root is not None:
            return self.workspace_root
        return f"/home/{self.user}/.openevo/workspaces"
```

Use validators to strip non-empty strings, reject relative `workspace_root`, require `private_key_path` for private-key auth, require `password_ref` for password-ref auth, and forbid irrelevant auth fields. Use `extra="forbid"` so raw `password` and `private_key` fields fail validation.

Create `src/openevo/sidecar/__init__.py` exporting `ProxySettings`, `RemoteProfileConfig`, `SSHAuthConfig`, and `load_remote_profile_config`.

- [ ] **Step 4: Run tests to verify GREEN**

Run:

```bash
PYTHONPATH=src /home/ziyi/ProRL-Agent-Server/.venv/bin/python -m pytest tests/openevo/sidecar/test_models.py -q
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/openevo/sidecar/__init__.py src/openevo/sidecar/models.py tests/openevo/sidecar/test_models.py
git commit -m "feat: add desktop remote profile contract"
```

---

### Task 2: Workspace Preparation Planning

**Files:**
- Create: `src/openevo/sidecar/workspace.py`
- Modify: `src/openevo/sidecar/__init__.py`
- Test: `tests/openevo/sidecar/test_workspace.py`

- [ ] **Step 1: Write failing tests**

Create `tests/openevo/sidecar/test_workspace.py` with:

```python
from __future__ import annotations

from pathlib import Path

from openevo.science import ScienceProjectConfig, compile_science_project
from openevo.sidecar import (
    RemoteProfileConfig,
    plan_workspace_preparation,
)


def _profile() -> RemoteProfileConfig:
    return RemoteProfileConfig.model_validate(
        {
            "version": 1,
            "id": "lab-gpu",
            "host": "gpu.example.edu",
            "user": "alice",
        }
    )


def _project(source: dict[str, str], tmp_path: Path | None = None) -> ScienceProjectConfig:
    path = None if tmp_path is None else tmp_path / "science.yaml"
    if path is not None:
        path.write_text("version: 1\n", encoding="utf-8")
    return ScienceProjectConfig.model_validate(
        {
            "version": 1,
            "project": {"name": "Protein Design"},
            "remote_profile": "lab-gpu",
            "task": {
                "id": "folding-baseline",
                "objective": "Improve the folding baseline.",
                "source": source,
            },
            "path": path,
        }
    )


def test_local_folder_plan_uploads_to_deterministic_remote_workspace(tmp_path: Path) -> None:
    project = _project(
        {"type": "local_folder", "path": "workflows/folding"},
        tmp_path=tmp_path,
    )

    plan = plan_workspace_preparation(project, _profile())

    assert plan.remote_profile_id == "lab-gpu"
    assert len(plan.actions) == 1
    action = plan.actions[0]
    assert action.type == "upload_dir"
    assert action.task_id == "folding-baseline"
    assert action.source == str(tmp_path / "workflows/folding")
    assert action.target.startswith(
        "/home/alice/.openevo/workspaces/protein-design/folding-baseline/"
    )
    prepared = plan.to_prepared_workspaces()
    assert prepared["folding-baseline"].path == action.target
    assert prepared["folding-baseline"].source_fingerprint.startswith("sha256:")


def test_git_repository_plan_records_clone_command() -> None:
    project = _project(
        {
            "type": "git_repository",
            "url": "https://github.com/example/research.git",
            "branch": "main",
        }
    )

    plan = plan_workspace_preparation(project, _profile())

    action = plan.actions[0]
    assert action.type == "git_clone"
    assert action.source == "https://github.com/example/research.git"
    assert action.branch == "main"
    assert "git clone --depth 1 --branch main" in str(action.command)
    assert action.target.startswith(
        "/home/alice/.openevo/workspaces/protein-design/folding-baseline/"
    )


def test_remote_path_plan_uses_existing_remote_workspace() -> None:
    project = _project({"type": "remote_path", "path": "/datasets/folding"})

    plan = plan_workspace_preparation(project, _profile())

    assert plan.actions[0].type == "use_remote_path"
    assert plan.actions[0].source == "/datasets/folding"
    assert plan.to_prepared_workspaces()["folding-baseline"].path == "/datasets/folding"


def test_scratch_plan_has_no_actions_and_no_prepared_workspace() -> None:
    project = _project({"type": "scratch"})

    plan = plan_workspace_preparation(project, _profile())

    assert plan.actions == []
    assert plan.to_prepared_workspaces() == {}


def test_workspace_plan_compiles_local_folder_science_project(tmp_path: Path) -> None:
    project = _project(
        {"type": "local_folder", "path": "workflows/folding"},
        tmp_path=tmp_path,
    )
    plan = plan_workspace_preparation(project, _profile())

    compiled = compile_science_project(
        project,
        prepared_workspaces=plan.to_prepared_workspaces(),
    )

    assert compiled.tasks[0].workspace == plan.actions[0].target
    assert compiled.tasks[0].metadata["openevo"]["source_fingerprint"].startswith(
        "sha256:"
    )
```

- [ ] **Step 2: Run tests to verify RED**

Run:

```bash
PYTHONPATH=src /home/ziyi/ProRL-Agent-Server/.venv/bin/python -m pytest tests/openevo/sidecar/test_workspace.py -q
```

Expected: fails because `plan_workspace_preparation` is missing.

- [ ] **Step 3: Implement workspace planning**

Create strict frozen models in `src/openevo/sidecar/workspace.py`:

```python
class WorkspacePreparationAction(_StrictModel):
    type: Literal["upload_dir", "git_clone", "use_remote_path"]
    task_id: str
    source: str | None = None
    target: str
    branch: str | None = None
    command: str | None = None
    source_fingerprint: str | None = None

class WorkspacePreparationPlan(_StrictModel):
    project_name: str
    remote_profile_id: str
    workspace_root: str
    actions: list[WorkspacePreparationAction] = Field(default_factory=list)

    def to_prepared_workspaces(self) -> dict[str, PreparedWorkspace]:
        ...
```

Implement:

```python
def plan_workspace_preparation(
    project: ScienceProjectConfig,
    profile: RemoteProfileConfig,
) -> WorkspacePreparationPlan:
    source = project.task.source
    if source.type == "scratch":
        return WorkspacePreparationPlan(..., actions=[])
    if source.type == "remote_path":
        return WorkspacePreparationPlan(..., actions=[...])
    if source.type == "local_folder":
        resolved_source = _resolve_local_path(project, source.path)
        target = _target_workspace(project, profile, "local_folder", resolved_source)
        return WorkspacePreparationPlan(..., actions=[...])
    if source.type == "git_repository":
        target = _target_workspace(project, profile, "git_repository", source.url)
        return WorkspacePreparationPlan(..., actions=[...])
```

Use slugified project/task path segments, deterministic SHA-256 fingerprints from source type plus source string plus branch, `shlex.quote` for clone command display, and `profile.effective_workspace_root` as the base path.

Export `WorkspacePreparationAction`, `WorkspacePreparationPlan`, and `plan_workspace_preparation` from `src/openevo/sidecar/__init__.py`.

- [ ] **Step 4: Run tests to verify GREEN**

Run:

```bash
PYTHONPATH=src /home/ziyi/ProRL-Agent-Server/.venv/bin/python -m pytest tests/openevo/sidecar/test_workspace.py -q
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/openevo/sidecar/__init__.py src/openevo/sidecar/workspace.py tests/openevo/sidecar/test_workspace.py
git commit -m "feat: plan desktop workspace preparation"
```

---

### Task 3: Sidecar Science Plan And Preflight Mapping

**Files:**
- Create: `src/openevo/sidecar/planner.py`
- Modify: `src/openevo/sidecar/__init__.py`
- Test: `tests/openevo/sidecar/test_planner.py`

- [ ] **Step 1: Write failing tests**

Create `tests/openevo/sidecar/test_planner.py` with:

```python
from __future__ import annotations

from openevo.science import ScienceProjectConfig
from openevo.sidecar import RemoteProfileConfig, build_sidecar_science_plan


def _profile(**overrides: object) -> RemoteProfileConfig:
    payload = {
        "version": 1,
        "id": "lab-gpu",
        "host": "gpu.example.edu",
        "user": "alice",
        "proxy": {"http_proxy": "http://127.0.0.1:7890"},
    }
    payload.update(overrides)
    return RemoteProfileConfig.model_validate(payload)


def _project(**overrides: object) -> ScienceProjectConfig:
    payload = {
        "version": 1,
        "project": {"name": "protein-design"},
        "remote_profile": "lab-gpu",
        "task": {
            "id": "folding-baseline",
            "objective": "Improve the folding baseline.",
            "source": {"type": "scratch"},
        },
    }
    payload.update(overrides)
    return ScienceProjectConfig.model_validate(payload)


def test_subscription_plan_requires_codex_preflight() -> None:
    plan = build_sidecar_science_plan(_project(), _profile())

    assert plan.remote_profile_id == "lab-gpu"
    assert plan.preflight.require_codex_subscription is True
    assert plan.preflight.min_home_available_kb == 20_000_000
    assert plan.proxy_env["HTTP_PROXY"] == "http://127.0.0.1:7890"
    assert plan.experiment.agent.auth == "subscription"


def test_local_inference_plan_does_not_require_codex_subscription() -> None:
    plan = build_sidecar_science_plan(
        _project(
            execution={
                "mode": "codex_managed_local_inference",
                "hf_model": "Qwen/Qwen3-Coder-30B-A3B-Instruct",
            }
        ),
        _profile(min_home_available_kb=42),
    )

    assert plan.preflight.require_codex_subscription is False
    assert plan.preflight.min_home_available_kb == 42
    assert plan.experiment.agent.auth == "proxy"
    assert plan.experiment.runtime.env["OPENEVO_MANAGED_HF_MODEL"] == (
        "Qwen/Qwen3-Coder-30B-A3B-Instruct"
    )


def test_profile_id_must_match_science_project_remote_profile() -> None:
    profile = _profile(id="other-gpu")

    try:
        build_sidecar_science_plan(_project(), profile)
    except ValueError as exc:
        assert "remote_profile" in str(exc)
    else:
        raise AssertionError("expected remote_profile mismatch to fail")
```

- [ ] **Step 2: Run tests to verify RED**

Run:

```bash
PYTHONPATH=src /home/ziyi/ProRL-Agent-Server/.venv/bin/python -m pytest tests/openevo/sidecar/test_planner.py -q
```

Expected: fails because `build_sidecar_science_plan` is missing.

- [ ] **Step 3: Implement high-level planner**

Create `src/openevo/sidecar/planner.py` with:

```python
class SidecarSciencePlan(_StrictModel):
    project_name: str
    task_id: str
    remote_profile_id: str
    proxy_env: dict[str, str]
    preflight: RemotePreflightSettings
    workspace: WorkspacePreparationPlan
    experiment: ExperimentConfig

def preflight_settings_for_project(
    project: ScienceProjectConfig,
    profile: RemoteProfileConfig,
) -> RemotePreflightSettings:
    return RemotePreflightSettings(
        require_codex_subscription=(
            project.execution.mode == "codex_subscription_transcript"
        ),
        min_home_available_kb=profile.min_home_available_kb,
    )

def build_sidecar_science_plan(
    project: ScienceProjectConfig,
    profile: RemoteProfileConfig,
) -> SidecarSciencePlan:
    if project.remote_profile != profile.id:
        raise ValueError(...)
    workspace = plan_workspace_preparation(project, profile)
    experiment = compile_science_project(
        project,
        prepared_workspaces=workspace.to_prepared_workspaces(),
    )
    return SidecarSciencePlan(...)
```

Export `SidecarSciencePlan`, `build_sidecar_science_plan`, and `preflight_settings_for_project`.

- [ ] **Step 4: Run tests to verify GREEN**

Run:

```bash
PYTHONPATH=src /home/ziyi/ProRL-Agent-Server/.venv/bin/python -m pytest tests/openevo/sidecar/test_planner.py -q
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/openevo/sidecar/__init__.py src/openevo/sidecar/planner.py tests/openevo/sidecar/test_planner.py
git commit -m "feat: build desktop sidecar science plans"
```

---

### Task 4: CLI, Documentation, And Focused Regression

**Files:**
- Modify: `src/openevo/cli.py`
- Modify: `tests/openevo/test_cli.py`
- Create: `docs/architecture/openevo-desktop-sidecar-foundation.md`

- [ ] **Step 1: Write failing CLI test**

Append to `tests/openevo/test_cli.py`:

```python
def test_cli_sidecar_plan_outputs_workspace_and_preflight(
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
                    "type": "local_folder",
                    "path": "workflows/local-task",
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
            "proxy": {"https_proxy": "http://127.0.0.1:7890"},
        },
    )

    exit_code = main(
        [
            "sidecar",
            "plan",
            str(science_path),
            "--remote-profile",
            str(profile_path),
            "--json",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["remote_profile_id"] == "science-team"
    assert payload["preflight"]["require_codex_subscription"] is True
    assert payload["proxy_env"]["HTTPS_PROXY"] == "http://127.0.0.1:7890"
    assert payload["workspace"]["actions"][0]["type"] == "upload_dir"
    assert payload["experiment"]["tasks"][0]["workspace"].startswith(
        "/home/alice/.openevo/workspaces/protein-design/local-task/"
    )
```

- [ ] **Step 2: Run CLI test to verify RED**

Run:

```bash
PYTHONPATH=src /home/ziyi/ProRL-Agent-Server/.venv/bin/python -m pytest tests/openevo/test_cli.py::test_cli_sidecar_plan_outputs_workspace_and_preflight -q
```

Expected: fails because the `sidecar` command does not exist.

- [ ] **Step 3: Add CLI command**

Modify `src/openevo/cli.py`:

```python
from openevo.sidecar import build_sidecar_science_plan, load_remote_profile_config

sidecar_parser = subparsers.add_parser(
    "sidecar",
    help="Work with OpenEvo Desktop sidecar plans.",
)
sidecar_subparsers = sidecar_parser.add_subparsers(
    dest="sidecar_command",
    required=True,
)
plan_parser = sidecar_subparsers.add_parser(
    "plan",
    help="Build a Desktop sidecar dry-run plan for a Science Project.",
)
plan_parser.add_argument("config", help="Path to science project YAML.")
plan_parser.add_argument(
    "--remote-profile",
    required=True,
    help="Path to remote profile YAML.",
)
plan_parser.add_argument("--json", action="store_true", help="Print JSON output.")
```

Add `_handle_sidecar` and `_handle_sidecar_plan`, loading both YAML files and printing `plan.model_dump(mode="json")` as JSON or YAML.

- [ ] **Step 4: Write architecture docs**

Create `docs/architecture/openevo-desktop-sidecar-foundation.md` explaining:

- tracked by #39;
- sidecar boundary;
- remote profile fields and secret-reference-only auth;
- proxy/mirror options including server-side proxy ports for mainland China or restricted networks;
- workspace source behavior for `local_folder`, `git_repository`, `remote_path`, and `scratch`;
- preflight mapping for subscription vs managed local inference;
- CLI example;
- explicit limitations: no real SSH/SFTP yet, no vault implementation, no Docker/vLLM lifecycle, no UI, no parametric-memory adapter lifecycle.

- [ ] **Step 5: Run focused regression**

Run:

```bash
PYTHONPATH=src /home/ziyi/ProRL-Agent-Server/.venv/bin/python -m pytest tests/openevo/test_experiment_models.py tests/openevo/test_experiment_compiler.py tests/openevo/test_cli.py tests/openevo/science tests/openevo/remote tests/openevo/sidecar -q
git diff --check
```

Expected: all tests pass and diff check reports no whitespace errors.

- [ ] **Step 6: Commit**

```bash
git add src/openevo/cli.py tests/openevo/test_cli.py docs/architecture/openevo-desktop-sidecar-foundation.md
git commit -m "feat: expose desktop sidecar plan cli"
```

---

## Final Review

After all tasks are complete:

1. Run `git status --short`.
2. Run the focused regression command from Task 4.
3. Run `git diff --check`.
4. Review `git diff openevo/stable...HEAD`.
5. Push the branch with HTTPS:

```bash
git push https://github.com/CompLifeLab-ZJU/OpenEvo.git HEAD:refs/heads/codex/openevo-desktop-sidecar-foundation
```

6. Open a PR against `stable` with `Fixes #39`, docs changed, and test results.
7. Merge when checks and local review are acceptable.
