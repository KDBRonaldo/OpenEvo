# OpenEvo Sidecar Config Status Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the local OpenEvo Desktop sidecar derive `/openevo-api/desktop/shell` status from a Science Project YAML and remote profile YAML.

**Architecture:** Add a pure status builder in the sidecar API layer that consumes existing science and remote profile contracts, then maps the compiled sidecar science plan to the existing Desktop shell status response. Extend `openevo sidecar serve` with optional config paths; without paths it keeps the built-in fixture.

**Tech Stack:** Python 3.11, Pydantic v2, FastAPI, pytest, ruff.

---

Tracked by #51.

## File Structure

- Modify `src/openevo/sidecar/api.py`: add `build_desktop_shell_status(project, profile)` and helper mapping functions.
- Modify `src/openevo/sidecar/__init__.py`: export the builder.
- Modify `src/openevo/cli.py`: add `sidecar serve --config --remote-profile` and load config-backed status.
- Modify `tests/openevo/sidecar/test_api.py`: builder tests for subscription and managed local inference.
- Modify `tests/openevo/test_cli.py`: CLI serve passes config-derived status into app factory.
- Modify `docs/architecture/openevo-desktop-science-foundation.md`: document config-backed sidecar status.

## Task 1: Config-Backed Shell Status Builder

**Files:**
- Modify: `tests/openevo/sidecar/test_api.py`
- Modify: `src/openevo/sidecar/api.py`
- Modify: `src/openevo/sidecar/__init__.py`

- [ ] **Step 1: Add failing builder tests**

Append tests that construct a minimal `ScienceProjectConfig` and `RemoteProfileConfig`, call `build_desktop_shell_status(project, profile)`, and assert:

```python
status.remote.id == "science-team"
status.remote.proxy.https_proxy == "http://127.0.0.1:7890"
status.project.task_id == "folding-baseline"
status.execution.mode == "codex_subscription_transcript"
status.execution.token_metrics_available is False
status.bootstrap.ready is False
status.bootstrap.workspace_root == "/home/alice/.openevo/workspaces"
status.bootstrap.readiness_notes == ("Remote bootstrap has not run yet.",)
status.services[-1].state == "planned"
```

Add a managed local inference test asserting:

```python
status.execution.mode == "codex_managed_local_inference"
status.execution.model == "Qwen/Qwen2.5-7B-Instruct"
status.execution.token_metrics_available is True
any(step.id == "parametric-memory" for step in status.evolution)
```

- [ ] **Step 2: Verify RED**

Run:

```bash
PYTHONPATH=src /home/ziyi/ProRL-Agent-Server/.venv/bin/python -m pytest tests/openevo/sidecar/test_api.py -q
```

Expected: FAIL because `build_desktop_shell_status` does not exist.

- [ ] **Step 3: Implement builder**

In `src/openevo/sidecar/api.py`:

- Import `ScienceProjectConfig`, `RemoteProfileConfig`, and `build_sidecar_science_plan`.
- Add `build_desktop_shell_status(project, profile)`:
  - build the sidecar science plan;
  - fill remote id/host/user/proxy from profile;
  - fill project name/task/source/objective from project;
  - set subscription mode token metrics false and managed local inference token metrics true;
  - set bootstrap ready false with the plan-derived workspace/state roots;
  - derive services as SSH planned, workspace ready for `remote_path` and planned otherwise, bootstrap planned, OpenEvo backend planned;
  - derive evolution steps from requested targets; append parametric-memory only for managed local inference if that target is present.

- [ ] **Step 4: Verify GREEN**

Run:

```bash
PYTHONPATH=src /home/ziyi/ProRL-Agent-Server/.venv/bin/python -m pytest tests/openevo/sidecar/test_api.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/openevo/sidecar/api.py src/openevo/sidecar/__init__.py tests/openevo/sidecar/test_api.py
git commit -m "feat: build openevo shell status from configs"
```

## Task 2: CLI Serve Config Loading

**Files:**
- Modify: `tests/openevo/test_cli.py`
- Modify: `src/openevo/cli.py`

- [ ] **Step 1: Add failing CLI test**

Add a test that writes `science.yaml` and `remote.yaml`, monkeypatches `create_sidecar_app` and `_run_sidecar_server`, runs:

```bash
openevo sidecar serve --config science.yaml --remote-profile remote.yaml
```

and asserts the app factory received a status whose `project.task_id == "folding-baseline"` and `remote.id == "science-team"`.

- [ ] **Step 2: Verify RED**

Run:

```bash
PYTHONPATH=src /home/ziyi/ProRL-Agent-Server/.venv/bin/python -m pytest tests/openevo/test_cli.py::test_cli_sidecar_serve_loads_config_status -q
```

Expected: FAIL because `serve` does not accept these options.

- [ ] **Step 3: Implement CLI options**

In `src/openevo/cli.py`:

- import `build_desktop_shell_status`;
- add `--config` and `--remote-profile` to `sidecar serve`;
- require both options together;
- when present, load science and profile configs and pass `create_sidecar_app(status)` into `_run_sidecar_server`;
- when absent, keep `create_sidecar_app()`.

- [ ] **Step 4: Verify GREEN**

Run:

```bash
PYTHONPATH=src /home/ziyi/ProRL-Agent-Server/.venv/bin/python -m pytest tests/openevo/test_cli.py::test_cli_sidecar_serve_invokes_runner tests/openevo/test_cli.py::test_cli_sidecar_serve_loads_config_status -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/openevo/cli.py tests/openevo/test_cli.py
git commit -m "feat: load sidecar serve status from configs"
```

## Task 3: Docs and Verification

**Files:**
- Modify: `docs/architecture/openevo-desktop-science-foundation.md`

- [ ] **Step 1: Document config-backed serve**

Add that `openevo sidecar serve --config SCIENCE.yaml --remote-profile PROFILE.yaml` reads local files only, derives status, and does not execute remote commands.

- [ ] **Step 2: Run final verification**

Run:

```bash
PYTHONPATH=src /home/ziyi/ProRL-Agent-Server/.venv/bin/python -m pytest tests/openevo/test_cli.py tests/openevo/sidecar tests/openevo/remote tests/openevo/science tests/evolution/test_models.py -q
/home/ziyi/ProRL-Agent-Server/.venv/bin/ruff check src/openevo/sidecar src/openevo/cli.py tests/openevo/sidecar tests/openevo/test_cli.py
git diff --check openevo/stable...HEAD
```

Expected: all commands exit 0.

- [ ] **Step 3: Commit docs**

```bash
git add docs/architecture/openevo-desktop-science-foundation.md
git commit -m "docs: document config-backed sidecar status"
```

- [ ] **Step 4: Review and PR**

Request gpt-5.5 high-effort review, push the branch, open a PR with `Fixes #51`, wait for checks, then squash merge if clean.

## Self-Review

- Spec coverage: covers builder, CLI config loading, docs, verification, review, and PR.
- Placeholder scan: no placeholder markers.
- Type consistency: uses existing Python sidecar status models and science/profile config models.
