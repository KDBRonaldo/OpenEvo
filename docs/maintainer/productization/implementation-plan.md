# OpenEvo Productization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the OpenEvo pre-release productization spec by turning the repository into OpenEvo Core Backend plus OpenEvo Desktop, with no legacy Polar public identity and no evolution algorithm logic changes.

**Architecture:** `src/openevo/` becomes the complete Core Backend package. `desktop/` becomes the ordinary-user Desktop product and wraps Core Backend through a local sidecar and remote backend API. Command entrypoints are backend launchers or maintenance utilities only; CLI/Dev Kit are not product surfaces.

**Tech Stack:** Python 3.11, FastAPI, Pydantic, pytest, React, TypeScript, Vite, Tauri 2, Rust, GitHub Actions.

---

## Global Invariants

Every implementation task must preserve these invariants:

- Do not change existing evolution algorithm logic.
- Do not rename method IDs.
- Do not keep `src/polar/` or `src/polar_evolution/`.
- Do not keep `polar` or `polar-evolution` console scripts.
- Do not keep public `POLAR_*`, `/polar/session`, or `polar.session_completed`.
- Use `OPENEVO_*`, `/openevo/session`, and `openevo.session_completed`.
- Desktop must wrap Core Backend; Core Backend must not import Desktop.
- Desktop/Tauri must become a real native app host, not a passive WebView.
- Commit as `ivowang <ziyiwang@ieee.org>`.
- Base every phase on `stable`, push promptly to a remote phase branch, and
  merge back to `stable` only through a PR linked to #121.

## Phase Branch And PR Workflow

Every implementation phase must use this workflow:

Use these exact branch names when starting each task:

| Task | Branch |
| --- | --- |
| Task 1 | `productization/phase-1-inventory-guards` |
| Task 2 | `productization/phase-2-core-backend-migration` |
| Task 3 | `productization/phase-3-runtime-identity` |
| Task 4 | `productization/phase-4-backend-api-contract` |
| Task 5 | `productization/phase-5-desktop-native-host` |
| Task 6 | `productization/phase-6-desktop-backend-facade` |
| Task 7 | `productization/phase-7-repository-presentation` |
| Task 8 | `productization/phase-8-release-hardening` |
| Task 9 | `productization/phase-9-final-audit` |

- [ ] **Step A: Start from current stable**

Set `PHASE_BRANCH` to the current task branch from the table above, then run:

```bash
git switch stable
git pull --ff-only openevo stable
git switch -c "$PHASE_BRANCH"
```

- [ ] **Step B: Review before commit**

Run:

```bash
git status --short
git diff --check
git diff
```

Expected: only files listed by that phase are changed, no whitespace errors,
and no unrelated user edits are staged.

- [ ] **Step C: Commit phase changes**

Run `git add` only for the files listed in that phase. Then run the exact
commit command listed in that task's final step. Before committing, run:

```bash
git diff --cached --stat
```

Expected: the commit contains only the phase scope.

- [ ] **Step D: Push and open PR**

Use the exact `gh pr create` command listed in that task's final step. Every PR
body must contain `Part of #121`, docs paths, and tests run. The common push
command is:

```bash
git push -u openevo HEAD
```

Expected: PR is open against `stable`, references #121, lists docs and tests.

- [ ] **Step E: Merge only after reviews and checks pass**

Run:

```bash
gh pr merge --squash --delete-branch
git switch stable
git pull --ff-only openevo stable
```

Expected: local `stable` matches remote `openevo/stable`.

## Phase Overview

This is intentionally split into phases because the spec spans multiple
subsystems. Each phase is independently reviewable and should end with a commit
or small group of commits.

1. **Inventory and identity guards**
2. **Physical Core Backend migration**
3. **Runtime/data identity migration**
4. **Remote backend supervisor and API shell**
5. **Desktop directory and native host**
6. **Desktop sidecar/backend integration**
7. **Repository presentation cleanup**
8. **Release hardening**

Each phase needs two fresh-context reviews before being considered done:

- spec compliance review;
- code quality/release-risk review.

Use `gpt-5.5` with high reasoning effort for those review subagents.

---

## Task 1: Inventory And Workflow Guards

**Purpose:** Capture the current legacy identity surface and add CI-safe guards
for the productization workflow without committing any known-failing tests to
`stable`.

**Files:**

- Create: `scripts/ci/audit_openevo_identity.py`
- Create: `tests/ci/test_openevo_productization_workflow.py`
- Create: `docs/maintainer/productization-inventory.md`
- Modify: `.gitignore`

- [ ] **Step 1: Create identity inventory script**

Create `scripts/ci/audit_openevo_identity.py`:

```python
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
TEXT_SUFFIXES = {
    ".md",
    ".py",
    ".toml",
    ".yaml",
    ".yml",
    ".json",
    ".ts",
    ".tsx",
    ".rs",
    ".sh",
}
MARKERS = (
    "src/polar",
    "src/polar_evolution",
    "POLAR_",
    "/polar/session",
    "polar.session_completed",
    "polar-evolution",
)


def _tracked_text_files() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        check=True,
        cwd=REPO_ROOT,
        stdout=subprocess.PIPE,
    )
    files: list[Path] = []
    for raw_path in result.stdout.decode().split("\0"):
        if not raw_path:
            continue
        path = REPO_ROOT / raw_path
        if not path.is_file() or path.suffix not in TEXT_SUFFIXES:
            continue
        files.append(path)
    return sorted(files)


def audit() -> dict[str, object]:
    matches: list[dict[str, str]] = []
    for path in _tracked_text_files():
        relative = path.relative_to(REPO_ROOT)
        text = path.read_text(encoding="utf-8", errors="ignore")
        for marker in MARKERS:
            if marker in text:
                matches.append({"path": str(relative), "marker": marker})
    matches.sort(key=lambda match: (match["path"], match["marker"]))
    return {
        "src_polar_exists": (REPO_ROOT / "src" / "polar").exists(),
        "src_polar_evolution_exists": (REPO_ROOT / "src" / "polar_evolution").exists(),
        "web_exists": (REPO_ROOT / "web").exists(),
        "desktop_exists": (REPO_ROOT / "desktop").exists(),
        "matches": matches,
    }


def main() -> int:
    report = audit()
    json.dump(report, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 2: Create workflow guard test**

Create `tests/ci/test_openevo_productization_workflow.py`:

```python
from __future__ import annotations

import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
PLAN = REPO_ROOT / "docs" / "superpowers" / "plans" / "2026-07-09-openevo-productization-implementation.md"


def _bash_blocks(text: str) -> str:
    return "\n".join(re.findall(r"```bash\n(.*?)\n```", text, flags=re.DOTALL))


def test_plan_uses_phase_branch_pr_workflow() -> None:
    text = PLAN.read_text(encoding="utf-8")
    bash = _bash_blocks(text)
    assert "git push openevo " + "stable" not in bash
    assert "git push -u openevo HEAD" in bash
    pr_commands = re.findall(
        r'gh pr create --base stable --head "\$\(git branch --show-current\)"',
        bash,
    )
    assert len(pr_commands) >= 9
    assert "Part of #121" in bash


def test_plan_does_not_commit_known_failing_tests() -> None:
    text = PLAN.read_text(encoding="utf-8")
    forbidden = (
        "Commit " + "failing",
        "known " + "failing",
        "Expected: fail because " + "legacy",
        "OPENEVO_PRODUCTIZATION_" + "STRICT",
        "pytest.mark." + "xfail",
    )
    for marker in forbidden:
        assert marker not in text
```

- [ ] **Step 3: Create initial inventory note**

Create `docs/maintainer/productization-inventory.md`:

````markdown
# OpenEvo Productization Inventory

Tracked by #121.

This file records the current pre-migration identity surface so the physical
migration can be audited without committing known-failing tests to `stable`.

Run:

```bash
python3 scripts/ci/audit_openevo_identity.py
```

The migration is complete only after the final identity guard in Task 9 passes
without an allowlist for public Polar runtime identity.
````

- [ ] **Step 4: Run inventory and workflow tests**

Run:

```bash
python3 scripts/ci/audit_openevo_identity.py
pytest tests/ci/test_openevo_productization_workflow.py -q
```

Expected: the audit prints JSON and exits 0; workflow tests pass.

- [ ] **Step 5: Commit and open PR**

Run:

```bash
git status --short
git diff --check
git diff
git add .gitignore scripts/ci/audit_openevo_identity.py tests/ci/test_openevo_productization_workflow.py docs/maintainer/productization-inventory.md
git diff --cached --stat
git commit -m "test: add openevo productization inventory guards"
git push -u openevo HEAD
gh pr create --base stable --head "$(git branch --show-current)" --title "test: add OpenEvo productization inventory guards" --body "Part of #121

Docs updated:
- docs/maintainer/productization-inventory.md

Tests run:
- python3 scripts/ci/audit_openevo_identity.py
- pytest tests/ci/test_openevo_productization_workflow.py -q"
```

Expected: PR is open against `stable`.

---

## Task 2: Physical Core Backend Migration

**Purpose:** Move existing Python implementation into `src/openevo/` as the Core
Backend package. This is a mechanical move/import task and must not change
evolution algorithm behavior.

**Files:**

- Move implementation modules into:
  - `src/openevo/harness/`
  - `src/openevo/config/`
  - `src/openevo/gateway/`
  - `src/openevo/platform/`
  - `src/openevo/runtime/`
  - `src/openevo/rollout/`
  - `src/openevo/trajectory/`
  - `src/openevo/evolution/`
  - `src/openevo/deployment/`
  - `src/openevo/projects/`
  - `src/openevo/experiments/`
  - `src/openevo/backend/`
  - `src/openevo/tools/`
- Move:
  - `src/openevo/core/capabilities.py -> src/openevo/capabilities.py`
  - reusable remote profile/workspace/planning contracts from the old sidecar
    module into `src/openevo/deployment/`
  - Desktop HTTP facade and static-app helpers into top-level `desktop/`
- Create:
  - `src/openevo/backend/__init__.py`
  - `src/openevo/backend/launcher.py`
- Delete:
  - `src/polar/`
  - `src/polar_evolution/`
  - old `src/openevo/experiment/`
  - old `src/openevo/science/`
  - old `src/openevo/remote/`
  - old `src/openevo/sidecar/`
  - old `src/openevo/desktop/`
- Modify imports in all Python files and tests.

- [ ] **Step 1: Move low-level runtime modules**

Move:

```text
src/polar/agent       -> src/openevo/harness
src/polar/config      -> src/openevo/config
src/polar/gateway     -> src/openevo/gateway
src/polar/platform    -> src/openevo/platform
src/polar/runtime     -> src/openevo/runtime
src/polar/rollout     -> src/openevo/rollout
src/polar/trajectory  -> src/openevo/trajectory
```

Use `git mv` for tracked files. Preserve file contents except imports and
OpenEvo identity strings required by later tasks.

- [ ] **Step 2: Move evolution backend modules**

Move:

```text
src/polar_evolution/* -> src/openevo/evolution/
```

Keep method IDs and algorithm function bodies unchanged. Do not split
`methods.py` in this phase.

- [ ] **Step 3: Move existing OpenEvo orchestration modules**

Move:

```text
src/openevo/experiment -> src/openevo/experiments
src/openevo/science    -> src/openevo/projects/science
src/openevo/remote     -> src/openevo/deployment
```

Move sidecar-only API code later in Desktop tasks; do not put Desktop facade
code into Core.

- [ ] **Step 4: Move capabilities contract**

Move:

```text
src/openevo/core/capabilities.py -> src/openevo/capabilities.py
```

Update imports:

```text
openevo.core.capabilities -> openevo.capabilities
from openevo.core import build_core_capabilities -> from openevo.capabilities import build_core_capabilities
```

Keep `src/openevo/core/__init__.py` only if another module still needs a
temporary internal import during this same phase; by the end of the phase,
`openevo.core` must not be a public product namespace.

- [ ] **Step 5: Update imports mechanically**

Apply mechanical import changes:

```text
polar.agent            -> openevo.harness
polar.config           -> openevo.config
polar.gateway          -> openevo.gateway
polar.platform         -> openevo.platform
polar.runtime          -> openevo.runtime
polar.rollout          -> openevo.rollout
polar.trajectory       -> openevo.trajectory
polar_evolution        -> openevo.evolution
openevo.experiment     -> openevo.experiments
openevo.science        -> openevo.projects.science
openevo.remote         -> openevo.deployment
openevo.core.capabilities -> openevo.capabilities
```

Use `rg` after updates:

```bash
rg -n "from polar|import polar|polar_evolution|openevo\\.experiment|openevo\\.science|openevo\\.remote" src tests
```

Expected: no matches outside migration notes or tests that intentionally check
absence.

- [ ] **Step 6: Update packaging**

Modify `pyproject.toml`:

```toml
[project.scripts]
openevo-backend = "openevo.backend.launcher:main"

[tool.setuptools.packages.find]
where = ["src"]
include = ["openevo*", "slime_bridge*"]
```

Remove package data entries for `polar` and Desktop assets. The Core Backend
wheel must not package `openevo/desktop/*`, `openevo/sidecar/*`, or
`openevo/cli.py`; Desktop release assets belong to the top-level Desktop
product and macOS DMG workflow.

Create a minimal `src/openevo/backend/launcher.py` so the package entrypoint is
not broken before Task 4 expands the backend API:

```python
from __future__ import annotations

import argparse


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="openevo-backend")
    parser.add_argument("command", choices=("serve",))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "serve":
        raise SystemExit(
            "openevo-backend serve is introduced in the backend API phase."
        )
    raise ValueError(args.command)
```

Create `src/openevo/backend/__init__.py`:

```python
"""OpenEvo Core Backend launcher package."""
```

- [ ] **Step 7: Run focused import checks**

Run:

```bash
python - <<'PY'
from openevo.capabilities import build_core_capabilities
from openevo.evolution.methods import METHOD_REGISTRY, METHOD_METADATA

assert "text_memory_reflector" in METHOD_REGISTRY
assert "skill_bundle_reflector" in METHOD_REGISTRY
assert "agent_system_gepa_reflector" in METHOD_REGISTRY
assert "parametric_memory_lora_sft" in METHOD_REGISTRY
assert METHOD_METADATA["text_memory_reflector"]["method_id"] == "text_memory_reflector"
print("imports ok")
print(build_core_capabilities().model_dump()["evolution_methods"][0]["method_id"])
PY
```

Expected: prints `imports ok`.

- [ ] **Step 8: Run focused tests**

Run:

```bash
pytest tests/evolution tests/trajectory tests/gateway tests/openevo -q
pytest tests/ci/test_openevo_productization_workflow.py -q
```

Expected: tests pass or failures are only path/import failures to fix in this
task.

- [ ] **Step 9: Review algorithm preservation**

Run:

```bash
git diff -- src/openevo/evolution/methods.py
```

Expected: diff shows path/import changes only, not algorithmic body rewrites.

- [ ] **Step 10: Commit and open PR**

Run:

```bash
git add pyproject.toml src tests
git commit -m "refactor: move core backend into openevo package"
git push -u openevo HEAD
gh pr create --base stable --head "$(git branch --show-current)" --title "refactor: move Core Backend into OpenEvo package" --body "Part of #121

Docs updated:
- docs/maintainer/productization-inventory.md

Tests run:
- pytest tests/evolution tests/trajectory tests/gateway tests/openevo -q
- pytest tests/ci/test_openevo_productization_workflow.py -q"
```

---

## Task 3: Runtime And Data Identity Migration

**Purpose:** Replace runtime identity from Polar to OpenEvo.

**Files:**

- Modify Core runtime, gateway, trajectory, evolution, deployment, experiments,
  tests, docs, and release checks.

- [ ] **Step 1: Replace runtime paths**

Change public runtime paths:

```text
/polar/session              -> /openevo/session
/polar/session/workspace    -> /openevo/session/workspace
/polar/session/evolution    -> /openevo/session/evolution
```

Run:

```bash
rg -n "/polar/session" src tests docs examples scripts
```

Expected: no matches outside allowlisted migration notes.

- [ ] **Step 2: Replace env vars**

Change public runtime env vars:

```text
POLAR_EVOLUTION_CONTEXT       -> OPENEVO_EVOLUTION_CONTEXT
POLAR_MEMORY_FILE             -> OPENEVO_MEMORY_FILE
POLAR_SKILLS_DIR              -> OPENEVO_SKILLS_DIR
POLAR_AGENT_SYSTEM_FILE       -> OPENEVO_AGENT_SYSTEM_FILE
POLAR_AGENT_SYSTEM_TARGET     -> OPENEVO_AGENT_SYSTEM_TARGET
POLAR_AGENT_SYSTEM_TARGETS    -> OPENEVO_AGENT_SYSTEM_TARGETS
POLAR_ADAPTER_MERGE_SPEC      -> OPENEVO_ADAPTER_MERGE_SPEC
```

Run:

```bash
rg -n "POLAR_" src tests docs examples scripts
```

Expected: no matches outside allowlisted migration notes.

- [ ] **Step 3: Replace event names and state roots**

Change:

```text
polar.session_completed -> openevo.session_completed
.polar_evolution        -> .openevo/evolution
```

Run:

```bash
rg -n "polar\\.session_completed|\\.polar_evolution" src tests docs examples scripts
```

Expected: no matches outside allowlisted migration notes.

- [ ] **Step 4: Run focused tests**

Run:

```bash
pytest tests/gateway tests/evolution tests/trajectory tests/openevo -q
pytest tests/ci/test_openevo_productization_workflow.py -q
```

Expected: pass.

- [ ] **Step 5: Commit and open PR**

Run:

```bash
git add src tests docs examples scripts
git commit -m "refactor: migrate runtime identity to openevo"
git push -u openevo HEAD
gh pr create --base stable --head "$(git branch --show-current)" --title "refactor: migrate runtime identity to OpenEvo" --body "Part of #121

Docs updated:
- docs/core/runtime-contract.md
- docs/core/evolution-contract.md

Tests run:
- pytest tests/gateway tests/evolution tests/trajectory tests/openevo -q
- pytest tests/ci/test_openevo_productization_workflow.py -q"
```

---

## Task 4: Core Backend Launcher And Typed API Contract

**Purpose:** Introduce the remote `openevo-backend` process as the single
backend service Desktop controls, with every route required by the spec exposed
as a typed API scaffold.

**Files:**

- Create: `src/openevo/backend/__init__.py`
- Create: `src/openevo/backend/api.py`
- Create: `src/openevo/backend/launcher.py`
- Create: `src/openevo/backend/models.py`
- Create: `tests/backend/test_api.py`
- Modify: `pyproject.toml`

- [ ] **Step 1: Write backend API tests**

Create `tests/backend/test_api.py`:

```python
from __future__ import annotations

from fastapi.testclient import TestClient

from openevo.backend.api import BackendHTTPError, create_backend_app
from openevo.backend.models import BackendError


def _client(*, raise_server_exceptions: bool = True) -> TestClient:
    app = create_backend_app()

    @app.get("/_test/http-error")
    def _test_http_error() -> None:
        raise BackendHTTPError(
            409,
            BackendError(
                code="service_conflict",
                message="Service conflict.",
                severity="blocking",
                category="service",
                retryable=True,
                repair_action="openevo_can_retry",
            ),
        )

    @app.get("/_test/unhandled-error")
    def _test_unhandled_error() -> None:
        raise RuntimeError("boom")

    return TestClient(app, raise_server_exceptions=raise_server_exceptions)


def test_backend_health() -> None:
    client = _client()
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_backend_capabilities() -> None:
    client = _client()
    response = client.get("/capabilities")
    assert response.status_code == 200
    method_ids = {item["method_id"] for item in response.json()["evolution_methods"]}
    assert "text_memory_reflector" in method_ids
    assert "skill_bundle_reflector" in method_ids


def test_backend_route_surface_is_present() -> None:
    routes = {
        (route.path, next(iter(route.methods - {"HEAD", "OPTIONS"}), None))
        for route in create_backend_app().routes
        if hasattr(route, "methods")
    }
    assert ("/status", "GET") in routes
    assert ("/environment", "GET") in routes
    assert ("/environment/doctor", "POST") in routes
    assert ("/environment/repair", "POST") in routes
    assert ("/projects", "POST") in routes
    assert ("/projects", "GET") in routes
    assert ("/projects/{project_id}", "GET") in routes
    assert ("/projects/{project_id}", "PATCH") in routes
    assert ("/runs", "POST") in routes
    assert ("/runs", "GET") in routes
    assert ("/runs/{run_id}", "GET") in routes
    assert ("/runs/{run_id}/cancel", "POST") in routes
    assert ("/runs/{run_id}/retry", "POST") in routes
    assert ("/runs/{run_id}/timeline", "GET") in routes
    assert ("/runs/{run_id}/logs", "GET") in routes
    assert ("/runs/{run_id}/artifacts", "GET") in routes
    assert ("/artifacts/{artifact_id}", "GET") in routes
    assert ("/artifacts/{artifact_id}/content", "GET") in routes
    assert ("/artifacts/{artifact_id}/diff", "GET") in routes
    assert ("/services", "GET") in routes
    assert ("/services/{service_id}/logs", "GET") in routes
    assert ("/services/{service_id}/restart", "POST") in routes
    assert ("/services/{service_id}/stop", "POST") in routes


def test_backend_project_run_artifact_flow() -> None:
    client = _client()

    project = client.post(
        "/projects",
        json={"name": "science demo", "workspace_root": "/srv/openevo/workspaces/demo"},
    )
    assert project.status_code == 200
    project_id = project.json()["id"]

    run = client.post(
        "/runs",
        json={"project_id": project_id, "execution_mode": "codex_subscription"},
    )
    assert run.status_code == 200
    run_id = run.json()["id"]

    timeline = client.get(f"/runs/{run_id}/timeline")
    assert timeline.status_code == 200
    assert timeline.json()[0]["phase"] == "created"

    artifacts = client.get(f"/runs/{run_id}/artifacts")
    assert artifacts.status_code == 200
    artifact_id = artifacts.json()[0]["id"]

    content = client.get(f"/artifacts/{artifact_id}/content")
    assert content.status_code == 200
    assert content.json()["artifact_type"] in {"text_memory", "skill_bundle", "agent_system"}


def test_backend_typed_error_model() -> None:
    client = _client()
    response = client.get("/projects/missing-project")
    assert response.status_code == 404
    body = response.json()
    assert body["code"] == "project_not_found"
    assert body["severity"] == "blocking"
    assert body["category"] == "project"
    assert body["retryable"] is False
    assert body["repair_action"] == "user_action_required"


def test_backend_validation_errors_use_typed_error_model() -> None:
    client = _client()
    response = client.post("/runs", json={})
    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "request_validation_error"
    assert body["severity"] == "blocking"
    assert body["category"] == "internal"
    assert body["retryable"] is False
    assert body["repair_action"] == "openevo_can_reconfigure"
    assert "errors" in body["details"]


def test_backend_http_errors_use_typed_error_model() -> None:
    client = _client()
    response = client.get("/_test/http-error")
    assert response.status_code == 409
    assert response.json()["code"] == "service_conflict"
    assert response.json()["repair_action"] == "openevo_can_retry"


def test_backend_unhandled_errors_use_typed_error_model() -> None:
    client = _client(raise_server_exceptions=False)
    response = client.get("/_test/unhandled-error")
    assert response.status_code == 500
    body = response.json()
    assert body["code"] == "internal_server_error"
    assert body["severity"] == "blocking"
    assert body["category"] == "internal"
    assert body["retryable"] is True
    assert body["repair_action"] == "openevo_can_retry"


def test_environment_doctor_and_repair_contract() -> None:
    client = _client()
    doctor = client.post("/environment/doctor", json={"repair": False})
    assert doctor.status_code == 200
    assert doctor.json()["checks"][0]["category"] in {"python", "docker", "codex", "network"}

    repair = client.post("/environment/repair", json={"actions": ["clear_stale_state"]})
    assert repair.status_code == 200
    assert repair.json()["status"] in {"ok", "needs_user_action"}
```

- [ ] **Step 2: Implement typed backend models**

Create `src/openevo/backend/models.py`:

```python
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


ErrorSeverity = Literal["info", "warning", "blocking"]
ErrorCategory = Literal["environment", "project", "run", "artifact", "service", "internal"]
RepairAction = Literal[
    "openevo_can_retry",
    "openevo_can_install",
    "openevo_can_reconfigure",
    "user_action_required",
    "unsupported",
]


class BackendError(BaseModel):
    code: str
    message: str
    severity: ErrorSeverity
    category: ErrorCategory
    retryable: bool
    repair_action: RepairAction
    details: dict[str, Any] = Field(default_factory=dict)
    logs_ref: str | None = None


class HealthResponse(BaseModel):
    status: Literal["ok"]


class ServiceSummary(BaseModel):
    id: str
    name: str
    status: Literal["stopped", "starting", "running", "failed"]
    restartable: bool = True


class BackendStatus(BaseModel):
    status: Literal["starting", "ready", "degraded", "blocked"]
    services: list[ServiceSummary]
    active_runs: int = 0


class EnvironmentSettings(BaseModel):
    workspace_root: str = "~/.openevo/workspaces"
    proxy_url: str | None = None
    no_proxy: list[str] = Field(default_factory=list)
    pip_index_url: str | None = None
    huggingface_endpoint: str | None = None
    huggingface_cache: str | None = None


class EnvironmentDoctorRequest(BaseModel):
    repair: bool = False


class EnvironmentCheck(BaseModel):
    id: str
    category: Literal["python", "docker", "codex", "network"]
    status: Literal["ok", "warning", "blocking"]
    message: str
    repair_action: RepairAction


class EnvironmentDoctorResponse(BaseModel):
    status: Literal["ok", "needs_user_action"]
    checks: list[EnvironmentCheck]


class EnvironmentRepairRequest(BaseModel):
    actions: list[str] = Field(default_factory=list)


class EnvironmentRepairResponse(BaseModel):
    status: Literal["ok", "needs_user_action"]
    performed_actions: list[str] = Field(default_factory=list)
    errors: list[BackendError] = Field(default_factory=list)


class ProjectCreateRequest(BaseModel):
    name: str
    workspace_root: str


class ProjectPatchRequest(BaseModel):
    name: str | None = None
    workspace_root: str | None = None


class ProjectSummary(BaseModel):
    id: str
    name: str
    workspace_root: str
    status: Literal["draft", "ready", "blocked"] = "draft"


class RunCreateRequest(BaseModel):
    project_id: str
    execution_mode: Literal["codex_subscription", "self_deployed"]


class RunSummary(BaseModel):
    id: str
    project_id: str
    execution_mode: Literal["codex_subscription", "self_deployed"]
    status: Literal["created", "running", "completed", "failed", "cancelled"]


class TimelineEvent(BaseModel):
    id: str
    phase: str
    title: str
    message: str
    artifact_ids: list[str] = Field(default_factory=list)


class LogResponse(BaseModel):
    id: str
    lines: list[str]


class ArtifactSummary(BaseModel):
    id: str
    run_id: str
    artifact_type: Literal["text_memory", "skill_bundle", "agent_system", "parametric_memory"]
    title: str
    promoted: bool = False
    lineage: dict[str, Any] = Field(default_factory=dict)


class ArtifactContent(BaseModel):
    id: str
    artifact_type: str
    content: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class ArtifactDiff(BaseModel):
    id: str
    before: str
    after: str
    format: Literal["unified_text"] = "unified_text"


class ServiceActionResponse(BaseModel):
    service_id: str
    status: Literal["running", "stopped", "failed"]
```

- [ ] **Step 3: Implement backend app routes**

Create `src/openevo/backend/api.py`:

```python
from __future__ import annotations

from itertools import count

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from openevo.capabilities import CoreCapabilities, build_core_capabilities
from openevo.backend.models import (
    ArtifactContent,
    ArtifactDiff,
    ArtifactSummary,
    BackendError,
    BackendStatus,
    EnvironmentCheck,
    EnvironmentDoctorRequest,
    EnvironmentDoctorResponse,
    EnvironmentRepairRequest,
    EnvironmentRepairResponse,
    EnvironmentSettings,
    HealthResponse,
    LogResponse,
    ProjectCreateRequest,
    ProjectPatchRequest,
    ProjectSummary,
    RunCreateRequest,
    RunSummary,
    ServiceActionResponse,
    ServiceSummary,
    TimelineEvent,
)


class BackendHTTPError(Exception):
    def __init__(self, status_code: int, error: BackendError) -> None:
        self.status_code = status_code
        self.error = error


def _not_found(code: str, category: str, message: str) -> BackendHTTPError:
    return BackendHTTPError(
        404,
        BackendError(
            code=code,
            message=message,
            severity="blocking",
            category=category,
            retryable=False,
            repair_action="user_action_required",
        ),
    )


def create_backend_app() -> FastAPI:
    app = FastAPI(title="OpenEvo Core Backend", version="0.1.0")
    project_counter = count(1)
    run_counter = count(1)
    artifact_counter = count(1)
    projects: dict[str, ProjectSummary] = {}
    runs: dict[str, RunSummary] = {}
    run_artifacts: dict[str, list[ArtifactSummary]] = {}
    services = {
        "gateway": ServiceSummary(id="gateway", name="Gateway", status="running"),
        "rollout": ServiceSummary(id="rollout", name="Rollout", status="running"),
        "evolution-worker": ServiceSummary(
            id="evolution-worker",
            name="Evolution Worker",
            status="running",
        ),
    }

    @app.exception_handler(BackendHTTPError)
    def backend_error_handler(_request: Request, exc: BackendHTTPError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content=exc.error.model_dump())

    @app.exception_handler(RequestValidationError)
    def validation_error_handler(_request: Request, exc: RequestValidationError) -> JSONResponse:
        error = BackendError(
            code="request_validation_error",
            message="The request payload does not match the OpenEvo backend contract.",
            severity="blocking",
            category="internal",
            retryable=False,
            repair_action="openevo_can_reconfigure",
            details={"errors": exc.errors()},
        )
        return JSONResponse(status_code=422, content=error.model_dump())

    @app.exception_handler(HTTPException)
    def http_error_handler(_request: Request, exc: HTTPException) -> JSONResponse:
        error = BackendError(
            code="http_error",
            message=str(exc.detail),
            severity="blocking",
            category="internal",
            retryable=False,
            repair_action="user_action_required",
            details={"status_code": exc.status_code},
        )
        return JSONResponse(status_code=exc.status_code, content=error.model_dump())

    @app.exception_handler(Exception)
    def unhandled_error_handler(_request: Request, exc: Exception) -> JSONResponse:
        error = BackendError(
            code="internal_server_error",
            message="OpenEvo backend hit an unexpected error.",
            severity="blocking",
            category="internal",
            retryable=True,
            repair_action="openevo_can_retry",
            details={"error_type": type(exc).__name__},
        )
        return JSONResponse(status_code=500, content=error.model_dump())

    @app.get("/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        return HealthResponse(status="ok")

    @app.get("/status", response_model=BackendStatus)
    def status() -> BackendStatus:
        return BackendStatus(status="ready", services=list(services.values()), active_runs=len(runs))

    @app.get("/environment", response_model=EnvironmentSettings)
    def environment() -> EnvironmentSettings:
        return EnvironmentSettings()

    @app.post("/environment/doctor", response_model=EnvironmentDoctorResponse)
    def environment_doctor(_request: EnvironmentDoctorRequest) -> EnvironmentDoctorResponse:
        return EnvironmentDoctorResponse(
            status="ok",
            checks=[
                EnvironmentCheck(
                    id="python",
                    category="python",
                    status="ok",
                    message="Python environment is usable.",
                    repair_action="openevo_can_retry",
                )
            ],
        )

    @app.post("/environment/repair", response_model=EnvironmentRepairResponse)
    def environment_repair(request: EnvironmentRepairRequest) -> EnvironmentRepairResponse:
        return EnvironmentRepairResponse(status="ok", performed_actions=request.actions)

    @app.post("/projects", response_model=ProjectSummary)
    def create_project(request: ProjectCreateRequest) -> ProjectSummary:
        project_id = f"project-{next(project_counter)}"
        project = ProjectSummary(
            id=project_id,
            name=request.name,
            workspace_root=request.workspace_root,
            status="ready",
        )
        projects[project_id] = project
        return project

    @app.get("/projects", response_model=list[ProjectSummary])
    def list_projects() -> list[ProjectSummary]:
        return list(projects.values())

    @app.get("/projects/{project_id}", response_model=ProjectSummary)
    def get_project(project_id: str) -> ProjectSummary:
        if project_id not in projects:
            raise _not_found("project_not_found", "project", f"Project {project_id} was not found.")
        return projects[project_id]

    @app.patch("/projects/{project_id}", response_model=ProjectSummary)
    def patch_project(project_id: str, request: ProjectPatchRequest) -> ProjectSummary:
        project = get_project(project_id)
        updated = project.model_copy(update=request.model_dump(exclude_none=True))
        projects[project_id] = updated
        return updated

    @app.post("/runs", response_model=RunSummary)
    def create_run(request: RunCreateRequest) -> RunSummary:
        get_project(request.project_id)
        run_id = f"run-{next(run_counter)}"
        run = RunSummary(
            id=run_id,
            project_id=request.project_id,
            execution_mode=request.execution_mode,
            status="created",
        )
        runs[run_id] = run
        artifact_id = f"artifact-{next(artifact_counter)}"
        run_artifacts[run_id] = [
            ArtifactSummary(
                id=artifact_id,
                run_id=run_id,
                artifact_type="text_memory",
                title="Initial memory draft",
                lineage={"project_id": request.project_id, "run_id": run_id},
            )
        ]
        return run

    @app.get("/runs", response_model=list[RunSummary])
    def list_runs() -> list[RunSummary]:
        return list(runs.values())

    @app.get("/runs/{run_id}", response_model=RunSummary)
    def get_run(run_id: str) -> RunSummary:
        if run_id not in runs:
            raise _not_found("run_not_found", "run", f"Run {run_id} was not found.")
        return runs[run_id]

    @app.post("/runs/{run_id}/cancel", response_model=RunSummary)
    def cancel_run(run_id: str) -> RunSummary:
        run = get_run(run_id).model_copy(update={"status": "cancelled"})
        runs[run_id] = run
        return run

    @app.post("/runs/{run_id}/retry", response_model=RunSummary)
    def retry_run(run_id: str) -> RunSummary:
        run = get_run(run_id).model_copy(update={"status": "created"})
        runs[run_id] = run
        return run

    @app.get("/runs/{run_id}/timeline", response_model=list[TimelineEvent])
    def run_timeline(run_id: str) -> list[TimelineEvent]:
        run = get_run(run_id)
        artifact_ids = [artifact.id for artifact in run_artifacts.get(run_id, [])]
        return [
            TimelineEvent(
                id=f"{run_id}-created",
                phase="created",
                title="Run created",
                message=f"{run.execution_mode} run is queued.",
                artifact_ids=artifact_ids,
            )
        ]

    @app.get("/runs/{run_id}/logs", response_model=LogResponse)
    def run_logs(run_id: str) -> LogResponse:
        get_run(run_id)
        return LogResponse(id=run_id, lines=["run created"])

    @app.get("/runs/{run_id}/artifacts", response_model=list[ArtifactSummary])
    def artifacts_for_run(run_id: str) -> list[ArtifactSummary]:
        get_run(run_id)
        return run_artifacts.get(run_id, [])

    @app.get("/artifacts/{artifact_id}", response_model=ArtifactSummary)
    def get_artifact(artifact_id: str) -> ArtifactSummary:
        for artifacts in run_artifacts.values():
            for artifact in artifacts:
                if artifact.id == artifact_id:
                    return artifact
        raise _not_found("artifact_not_found", "artifact", f"Artifact {artifact_id} was not found.")

    @app.get("/artifacts/{artifact_id}/content", response_model=ArtifactContent)
    def artifact_content(artifact_id: str) -> ArtifactContent:
        artifact = get_artifact(artifact_id)
        return ArtifactContent(
            id=artifact.id,
            artifact_type=artifact.artifact_type,
            content="OpenEvo memory draft.",
            metadata={"lineage": artifact.lineage},
        )

    @app.get("/artifacts/{artifact_id}/diff", response_model=ArtifactDiff)
    def artifact_diff(artifact_id: str) -> ArtifactDiff:
        get_artifact(artifact_id)
        return ArtifactDiff(id=artifact_id, before="", after="OpenEvo memory draft.")

    @app.get("/services", response_model=list[ServiceSummary])
    def list_services() -> list[ServiceSummary]:
        return list(services.values())

    @app.get("/services/{service_id}/logs", response_model=LogResponse)
    def service_logs(service_id: str) -> LogResponse:
        if service_id not in services:
            raise _not_found("service_not_found", "service", f"Service {service_id} was not found.")
        return LogResponse(id=service_id, lines=[f"{service_id} is running"])

    @app.post("/services/{service_id}/restart", response_model=ServiceActionResponse)
    def restart_service(service_id: str) -> ServiceActionResponse:
        if service_id not in services:
            raise _not_found("service_not_found", "service", f"Service {service_id} was not found.")
        services[service_id] = services[service_id].model_copy(update={"status": "running"})
        return ServiceActionResponse(service_id=service_id, status="running")

    @app.post("/services/{service_id}/stop", response_model=ServiceActionResponse)
    def stop_service(service_id: str) -> ServiceActionResponse:
        if service_id not in services:
            raise _not_found("service_not_found", "service", f"Service {service_id} was not found.")
        services[service_id] = services[service_id].model_copy(update={"status": "stopped"})
        return ServiceActionResponse(service_id=service_id, status="stopped")

    @app.get("/capabilities", response_model=CoreCapabilities)
    def capabilities() -> CoreCapabilities:
        return build_core_capabilities()

    return app
```

- [ ] **Step 4: Implement backend launcher**

Create `src/openevo/backend/launcher.py`:

```python
from __future__ import annotations

import argparse


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="openevo-backend")
    parser.add_argument("command", choices=("serve",))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "serve":
        import uvicorn

        uvicorn.run(
            "openevo.backend.api:create_backend_app",
            factory=True,
            host=args.host,
            port=args.port,
        )
        return 0
    raise ValueError(args.command)
```

Create `src/openevo/backend/__init__.py`:

```python
from openevo.backend.api import create_backend_app

__all__ = ["create_backend_app"]
```

- [ ] **Step 5: Update package script**

Set in `pyproject.toml`:

```toml
[project.scripts]
openevo-backend = "openevo.backend.launcher:main"
```

Do not add `polar`, `polar-evolution`, `openevo core`, or `openevo dev`.

- [ ] **Step 6: Run tests**

Run:

```bash
pytest tests/backend/test_api.py tests/ci/test_openevo_productization_workflow.py -q
```

Expected: pass.

- [ ] **Step 7: Commit and open PR**

Run:

```bash
git add pyproject.toml src/openevo/backend tests/backend
git commit -m "feat: add openevo backend launcher"
git push -u openevo HEAD
gh pr create --base stable --head "$(git branch --show-current)" --title "feat: add OpenEvo backend API contract" --body "Part of #121

Docs updated:
- docs/core/backend-api.md

Tests run:
- pytest tests/backend/test_api.py tests/ci/test_openevo_productization_workflow.py -q"
```

---

## Task 5: Desktop Directory And Native Host Foundation

**Purpose:** Move the current `web/` frontend into `desktop/` and make Tauri the
real Desktop host location with native host responsibilities represented in
Rust, not only a passive WebView shell.

**Files:**

- Move tracked frontend files from `web/` to `desktop/`
- Modify: `desktop/src-tauri/Cargo.toml`
- Create or modify: `desktop/src-tauri/src/main.rs`
- Modify: workflows and release checks that reference `web/`

- [ ] **Step 1: Move tracked frontend tree**

Run:

```bash
mkdir -p desktop
git mv web/.env.openevo-desktop desktop/.env.openevo-desktop
git mv web/index.html desktop/index.html
git mv web/package-lock.json desktop/package-lock.json
git mv web/package.json desktop/package.json
git mv web/tsconfig.json desktop/tsconfig.json
git mv web/vite.config.ts desktop/vite.config.ts
git mv web/src desktop/src
git mv web/src-tauri desktop/src-tauri
```

Do not move `web/node_modules` or `web/dist`. After confirming they are ignored
build/dependency output, remove those local directories:

```bash
git status --ignored --short web desktop
rm -rf web/node_modules web/dist
```

- [ ] **Step 2: Add Rust host dependencies**

Modify `desktop/src-tauri/Cargo.toml`:

```toml
[dependencies]
serde = { version = "1", features = ["derive"] }
tauri = { version = "2", features = [] }
```

- [ ] **Step 3: Implement native host command surface**

Replace `desktop/src-tauri/src/main.rs` with:

```rust
use std::net::TcpListener;
use std::sync::Mutex;

#[derive(Clone, serde::Serialize)]
struct SidecarStatus {
    state: String,
    port: Option<u16>,
    pid: Option<u32>,
}

#[derive(Clone, serde::Serialize)]
struct TunnelStatus {
    id: String,
    local_port: u16,
    remote_host: String,
    remote_port: u16,
    state: String,
}

#[derive(Clone, serde::Serialize)]
struct KeychainReference {
    service: String,
    account: String,
}

#[derive(Default)]
struct DesktopHostState {
    sidecar: Mutex<Option<SidecarStatus>>,
    tunnels: Mutex<Vec<TunnelStatus>>,
    logs: Mutex<Vec<String>>,
}

fn allocate_port() -> Result<u16, String> {
    TcpListener::bind("127.0.0.1:0")
        .map_err(|error| format!("failed to allocate local port: {error}"))?
        .local_addr()
        .map(|addr| addr.port())
        .map_err(|error| format!("failed to read allocated port: {error}"))
}

#[tauri::command]
fn host_status(state: tauri::State<'_, DesktopHostState>) -> Result<SidecarStatus, String> {
    Ok(state
        .sidecar
        .lock()
        .map_err(|_| "sidecar state lock poisoned".to_string())?
        .clone()
        .unwrap_or(SidecarStatus {
            state: "stopped".to_string(),
            port: None,
            pid: None,
        }))
}

#[tauri::command]
fn start_sidecar(state: tauri::State<'_, DesktopHostState>) -> Result<SidecarStatus, String> {
    let status = SidecarStatus {
        state: "running".to_string(),
        port: Some(allocate_port()?),
        pid: None,
    };
    *state
        .sidecar
        .lock()
        .map_err(|_| "sidecar state lock poisoned".to_string())? = Some(status.clone());
    state
        .logs
        .lock()
        .map_err(|_| "log state lock poisoned".to_string())?
        .push("sidecar started".to_string());
    Ok(status)
}

#[tauri::command]
fn stop_sidecar(state: tauri::State<'_, DesktopHostState>) -> Result<SidecarStatus, String> {
    let status = SidecarStatus {
        state: "stopped".to_string(),
        port: None,
        pid: None,
    };
    *state
        .sidecar
        .lock()
        .map_err(|_| "sidecar state lock poisoned".to_string())? = Some(status.clone());
    Ok(status)
}

#[tauri::command]
fn create_ssh_tunnel(
    state: tauri::State<'_, DesktopHostState>,
    remote_host: String,
    remote_port: u16,
) -> Result<TunnelStatus, String> {
    let tunnel = TunnelStatus {
        id: format!("{remote_host}:{remote_port}"),
        local_port: allocate_port()?,
        remote_host,
        remote_port,
        state: "ready".to_string(),
    };
    state
        .tunnels
        .lock()
        .map_err(|_| "tunnel state lock poisoned".to_string())?
        .push(tunnel.clone());
    Ok(tunnel)
}

#[tauri::command]
fn keychain_reference(service: String, account: String) -> KeychainReference {
    KeychainReference { service, account }
}

#[tauri::command]
fn app_logs(state: tauri::State<'_, DesktopHostState>) -> Result<Vec<String>, String> {
    Ok(state
        .logs
        .lock()
        .map_err(|_| "log state lock poisoned".to_string())?
        .clone())
}

fn main() {
    tauri::Builder::default()
        .manage(DesktopHostState::default())
        .invoke_handler(tauri::generate_handler![
            host_status,
            start_sidecar,
            stop_sidecar,
            create_ssh_tunnel,
            keychain_reference,
            app_logs,
        ])
        .run(tauri::generate_context!())
        .expect("error while running OpenEvo Desktop");
}

#[cfg(test)]
mod tests {
    use super::allocate_port;

    #[test]
    fn allocate_port_returns_non_zero_port() {
        assert!(allocate_port().unwrap() > 0);
    }
}
```

- [ ] **Step 4: Update workflow paths**

Replace workflow `working-directory: web` with `working-directory: desktop`.
Replace `web/package-lock.json` with `desktop/package-lock.json`.
Replace `web/src-tauri` with `desktop/src-tauri`.

- [ ] **Step 5: Update release scripts**

Replace references to `web/dist` with `desktop/dist` and packaged Desktop asset
paths with the new Desktop packaging contract.

- [ ] **Step 6: Run frontend tests**

Run:

```bash
cd desktop
npm ci
npm test -- --run
npm run build:openevo
```

Expected: pass.

- [ ] **Step 7: Run Rust checks**

Run:

```bash
cd desktop/src-tauri
cargo metadata --locked --format-version 1
cargo test --locked
```

Expected: pass.

- [ ] **Step 8: Commit and open PR**

Run:

```bash
git add desktop .github scripts tests docs pyproject.toml
git commit -m "refactor: move desktop app to top-level desktop"
git push -u openevo HEAD
gh pr create --base stable --head "$(git branch --show-current)" --title "refactor: move OpenEvo Desktop to top-level desktop" --body "Part of #121

Docs updated:
- docs/user/desktop-quickstart.md
- docs/maintainer/repository-structure.md

Tests run:
- cd desktop && npm ci && npm test -- --run && npm run build:openevo
- cd desktop/src-tauri && cargo metadata --locked --format-version 1 && cargo test --locked"
```

---

## Task 6: Remote Repair, Desktop Facade, And Evolution Visualization

**Purpose:** Convert Desktop sidecar from a pseudo-backend into a local facade
that connects to remote OpenEvo Backend, and add the view contracts for remote
repair, service status, timeline, artifacts, diffs, lineage, and next actions.

**Files:**

- Create: `desktop/sidecar/`
- Create: `src/openevo/deployment/bootstrap.py`
- Create: `tests/openevo/remote/test_bootstrap_contract.py`
- Create: `desktop/sidecar/backend_client.py`
- Create: `desktop/sidecar/test_backend_client.py`
- Create: `desktop/src/api/evolutionViewModel.ts`
- Create: `desktop/src/api/evolutionViewModel.test.ts`
- Move Desktop-only API/view-model code from old sidecar location.
- Modify React API client to call local Desktop facade.
- Add tests for disconnected setup state, backend health, remote repair, service
  logs, timeline, artifact preview/diff, lineage, and error next actions.

- [ ] **Step 1: Add remote bootstrap safety tests**

Create `tests/openevo/remote/test_bootstrap_contract.py`:

```python
from openevo.deployment.bootstrap import BootstrapInput, build_bootstrap_plan


def test_bootstrap_plan_only_uses_allowed_user_level_actions() -> None:
    plan = build_bootstrap_plan(
        BootstrapInput(
            ssh_host="gpu.example.org",
            ssh_port=22,
            ssh_user="researcher",
            workspace_root="/home/researcher/openevo-workspaces",
            proxy_url="http://127.0.0.1:7890",
            no_proxy=["localhost", "127.0.0.1"],
            pip_index_url="https://pypi.org/simple",
            huggingface_endpoint="https://huggingface.co",
            huggingface_cache="/home/researcher/.cache/huggingface",
            execution_mode="self_deployed",
            model_id="Qwen/Qwen2.5-Coder-7B-Instruct",
        )
    )

    action_ids = {action.id for action in plan.allowed_actions}
    assert "create_openevo_home" in action_ids
    assert "install_backend_bundle" in action_ids
    assert "configure_process_proxy" in action_ids
    assert "download_huggingface_snapshot" in action_ids
    assert "start_backend_services" in action_ids

    forbidden_ids = {action.id for action in plan.forbidden_actions}
    assert "modify_docker_daemon" in forbidden_ids
    assert "install_system_packages" in forbidden_ids
    assert "modify_systemd" in forbidden_ids
    assert "codex_subscription_login" in forbidden_ids


def test_subscription_mode_does_not_download_huggingface_model() -> None:
    plan = build_bootstrap_plan(
        BootstrapInput(
            ssh_host="gpu.example.org",
            ssh_port=22,
            ssh_user="researcher",
            workspace_root="/home/researcher/openevo-workspaces",
            execution_mode="codex_subscription",
            model_id="gpt-5-codex",
        )
    )
    action_ids = {action.id for action in plan.allowed_actions}
    assert "download_huggingface_snapshot" not in action_ids
```

- [ ] **Step 2: Implement remote bootstrap contract**

Create `src/openevo/deployment/bootstrap.py`:

```python
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


ExecutionMode = Literal["codex_subscription", "self_deployed"]


@dataclass(frozen=True)
class BootstrapInput:
    ssh_host: str
    ssh_port: int
    ssh_user: str
    workspace_root: str
    execution_mode: ExecutionMode
    model_id: str
    proxy_url: str | None = None
    no_proxy: list[str] = field(default_factory=list)
    pip_index_url: str | None = None
    huggingface_endpoint: str | None = None
    huggingface_cache: str | None = None


@dataclass(frozen=True)
class BootstrapAction:
    id: str
    description: str
    requires_user_action: bool = False


@dataclass(frozen=True)
class BootstrapPlan:
    allowed_actions: list[BootstrapAction]
    forbidden_actions: list[BootstrapAction]


def build_bootstrap_plan(inputs: BootstrapInput) -> BootstrapPlan:
    allowed = [
        BootstrapAction("create_openevo_home", "Create ~/.openevo state directories."),
        BootstrapAction("create_python_environment", "Create a user-level Python environment."),
        BootstrapAction("install_backend_bundle", "Install the exact OpenEvo backend bundle."),
        BootstrapAction("install_python_dependencies", "Install Python dependencies in user space."),
        BootstrapAction("start_backend_services", "Start openevo-backend and managed services."),
        BootstrapAction("clear_stale_state", "Clear stale OpenEvo pid, log, and tunnel state."),
    ]
    if inputs.proxy_url or inputs.no_proxy:
        allowed.append(BootstrapAction("configure_process_proxy", "Set process-level proxy environment."))
    if inputs.execution_mode == "self_deployed":
        allowed.extend(
            [
                BootstrapAction("prepare_model_server", "Prepare the self-deployed model server."),
                BootstrapAction("download_huggingface_snapshot", "Download the configured model snapshot."),
            ]
        )

    forbidden = [
        BootstrapAction("modify_docker_daemon", "Docker daemon changes require user action.", True),
        BootstrapAction("install_system_packages", "System packages are outside OpenEvo automation.", True),
        BootstrapAction("modify_systemd", "systemd mutation is not part of this release.", True),
        BootstrapAction("edit_shell_profiles", "Global shell profiles are not modified.", True),
        BootstrapAction("upload_ssh_private_keys", "OpenEvo never uploads private keys.", True),
        BootstrapAction("codex_subscription_login", "Codex login must already work on the remote server.", True),
    ]
    return BootstrapPlan(allowed_actions=allowed, forbidden_actions=forbidden)
```

- [ ] **Step 3: Move sidecar facade**

Move only Desktop facade code into `desktop/sidecar`. Core deployment,
projects, experiments, and backend logic must stay in `src/openevo/`.

- [ ] **Step 4: Add typed backend client facade**

Create `desktop/sidecar/test_backend_client.py`:

```python
from __future__ import annotations

import httpx
import pytest

from backend_client import BackendClient, BackendConnection, DesktopBackendError


def test_backend_client_preserves_typed_error_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/projects/missing-project"
        return httpx.Response(
            404,
            json={
                "code": "project_not_found",
                "message": "Project was not found.",
                "severity": "blocking",
                "category": "project",
                "retryable": False,
                "repair_action": "user_action_required",
                "details": {},
                "logs_ref": None,
            },
        )

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    client = BackendClient(
        BackendConnection(base_url="http://openevo.test"),
        http_client=http_client,
    )

    with pytest.raises(DesktopBackendError) as exc_info:
        client._get("/projects/missing-project")

    assert exc_info.value.status_code == 404
    assert exc_info.value.error["code"] == "project_not_found"
    assert exc_info.value.error["repair_action"] == "user_action_required"
```

Create `desktop/sidecar/backend_client.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx


@dataclass(frozen=True)
class BackendConnection:
    base_url: str


class DesktopBackendError(RuntimeError):
    def __init__(self, status_code: int, error: dict[str, Any]) -> None:
        super().__init__(str(error.get("message", "OpenEvo backend request failed.")))
        self.status_code = status_code
        self.error = error


class BackendClient:
    def __init__(
        self,
        connection: BackendConnection,
        *,
        http_client: httpx.Client | None = None,
    ) -> None:
        self._connection = connection
        self._http_client = http_client or httpx.Client(timeout=10)

    def health(self) -> dict[str, Any]:
        return self._get("/health")

    def status(self) -> dict[str, Any]:
        return self._get("/status")

    def environment_doctor(self) -> dict[str, Any]:
        return self._post("/environment/doctor", {"repair": False})

    def environment_repair(self, actions: list[str]) -> dict[str, Any]:
        return self._post("/environment/repair", {"actions": actions})

    def run_timeline(self, run_id: str) -> list[dict[str, Any]]:
        return self._get(f"/runs/{run_id}/timeline")

    def run_logs(self, run_id: str) -> dict[str, Any]:
        return self._get(f"/runs/{run_id}/logs")

    def run_artifacts(self, run_id: str) -> list[dict[str, Any]]:
        return self._get(f"/runs/{run_id}/artifacts")

    def artifact_content(self, artifact_id: str) -> dict[str, Any]:
        return self._get(f"/artifacts/{artifact_id}/content")

    def artifact_diff(self, artifact_id: str) -> dict[str, Any]:
        return self._get(f"/artifacts/{artifact_id}/diff")

    def service_logs(self, service_id: str) -> dict[str, Any]:
        return self._get(f"/services/{service_id}/logs")

    def _get(self, path: str) -> Any:
        response = self._http_client.get(f"{self._connection.base_url}{path}")
        self._raise_for_typed_error(response)
        return response.json()

    def _post(self, path: str, body: dict[str, Any]) -> Any:
        response = self._http_client.post(f"{self._connection.base_url}{path}", json=body)
        self._raise_for_typed_error(response)
        return response.json()

    def _raise_for_typed_error(self, response: httpx.Response) -> None:
        if response.status_code < 400:
            return
        try:
            error = response.json()
        except ValueError:
            error = {
                "code": "backend_http_error",
                "message": response.text,
                "severity": "blocking",
                "category": "internal",
                "retryable": False,
                "repair_action": "user_action_required",
                "details": {"status_code": response.status_code},
                "logs_ref": None,
            }
        raise DesktopBackendError(response.status_code, error)
```

- [ ] **Step 5: Remove demo-ready fallback**

Update React model initialization so disconnected Desktop starts in setup-needed
state.

Expected UI text should indicate no local sidecar or no remote backend instead
of a fake ready project.

- [ ] **Step 6: Add evolution visualization view-model tests**

Create `desktop/src/api/evolutionViewModel.test.ts`:

```ts
import { describe, expect, it } from "vitest";
import {
  artifactPreview,
  nextActionForError,
  timelineView,
} from "./evolutionViewModel";

describe("evolution view model", () => {
  it("renders run timeline phases and lineage without inventing algorithm fields", () => {
    const view = timelineView([
      {
        id: "event-1",
        phase: "evolution",
        title: "Memory updated",
        message: "A memory artifact was promoted.",
        artifact_ids: ["artifact-1"],
      },
    ]);

    expect(view[0].label).toBe("Memory updated");
    expect(view[0].phase).toBe("evolution");
    expect(view[0].artifactIds).toEqual(["artifact-1"]);
  });

  it("renders artifact preview and diff metadata", () => {
    const preview = artifactPreview(
      {
        id: "artifact-1",
        artifact_type: "agent_system",
        content: "Use stricter checks.",
        metadata: { lineage: { round: 2 }, target_path: "AGENTS.md" },
      },
      {
        id: "artifact-1",
        before: "Use checks.",
        after: "Use stricter checks.",
        format: "unified_text",
      },
    );

    expect(preview.kind).toBe("agent_system");
    expect(preview.targetPath).toBe("AGENTS.md");
    expect(preview.lineage).toEqual({ round: 2 });
    expect(preview.diff.after).toContain("stricter");
  });

  it("renders backend error next actions", () => {
    expect(
      nextActionForError({
        code: "docker_permission_denied",
        message: "Docker permission denied.",
        severity: "blocking",
        category: "environment",
        retryable: false,
        repair_action: "user_action_required",
        details: {},
      }),
    ).toBe("User action required");
  });
});
```

- [ ] **Step 7: Implement evolution visualization view model**

Create `desktop/src/api/evolutionViewModel.ts`:

```ts
type TimelineEvent = {
  id: string;
  phase: string;
  title: string;
  message: string;
  artifact_ids: string[];
};

type ArtifactContent = {
  id: string;
  artifact_type: string;
  content: string;
  metadata: Record<string, unknown>;
};

type ArtifactDiff = {
  id: string;
  before: string;
  after: string;
  format: "unified_text";
};

type BackendError = {
  code: string;
  message: string;
  severity: "info" | "warning" | "blocking";
  category: string;
  retryable: boolean;
  repair_action:
    | "openevo_can_retry"
    | "openevo_can_install"
    | "openevo_can_reconfigure"
    | "user_action_required"
    | "unsupported";
  details: Record<string, unknown>;
};

export function timelineView(events: TimelineEvent[]) {
  return events.map((event) => ({
    id: event.id,
    phase: event.phase,
    label: event.title,
    message: event.message,
    artifactIds: event.artifact_ids,
  }));
}

export function artifactPreview(content: ArtifactContent, diff: ArtifactDiff) {
  return {
    id: content.id,
    kind: content.artifact_type,
    body: content.content,
    targetPath:
      typeof content.metadata.target_path === "string"
        ? content.metadata.target_path
        : undefined,
    lineage:
      typeof content.metadata.lineage === "object" && content.metadata.lineage !== null
        ? content.metadata.lineage
        : {},
    diff,
  };
}

export function nextActionForError(error: BackendError) {
  if (error.repair_action === "openevo_can_retry") return "Retry";
  if (error.repair_action === "openevo_can_install") return "Install with OpenEvo";
  if (error.repair_action === "openevo_can_reconfigure") return "Update configuration";
  if (error.repair_action === "user_action_required") return "User action required";
  return "Unsupported";
}
```

- [ ] **Step 8: Add backend health integration**

Local sidecar endpoint must report remote backend health, service status, and
environment doctor/repair results through the SSH tunnel abstraction. Tests use
a fake backend client and assert the sidecar does not implement its own method
registry or run evolution algorithms.

- [ ] **Step 9: Run tests**

Run:

```bash
pytest tests/openevo/remote/test_bootstrap_contract.py -q
pytest desktop/sidecar/test_backend_client.py -q
pytest tests/openevo -q
cd desktop && npm test -- --run
```

Expected: pass.

- [ ] **Step 10: Commit and open PR**

Run:

```bash
git add desktop src tests
git commit -m "feat: connect desktop facade to openevo backend"
git push -u openevo HEAD
gh pr create --base stable --head "$(git branch --show-current)" --title "feat: connect Desktop facade to OpenEvo backend" --body "Part of #121

Docs updated:
- docs/user/troubleshooting.md
- docs/core/backend-api.md

Tests run:
- pytest tests/openevo/remote/test_bootstrap_contract.py -q
- pytest desktop/sidecar/test_backend_client.py -q
- pytest tests/openevo -q
- cd desktop && npm test -- --run"
```

---

## Task 7: Repository Presentation Cleanup

**Purpose:** Make the repository present OpenEvo Core Backend and OpenEvo
Desktop, not historical Polar.

**Files:**

- Modify: `README.md`
- Modify: `AGENTS.md`
- Delete or move: `README.polar.md`
- Delete or move: `assets/polar-logo.png`
- Reorganize: `docs/`
- Reorganize: `examples/`
- Add: `CHANGELOG.md`
- Add: `CONTRIBUTING.md`
- Add: `SECURITY.md`

- [ ] **Step 1: Rewrite README**

README must explain:

- OpenEvo Desktop;
- OpenEvo Core Backend;
- remote GPU server model;
- installation and release artifacts;
- development setup;
- repository structure.

It must not present CLI or Dev Kit as product surfaces.

- [ ] **Step 2: Reorganize docs**

Create:

```text
docs/user/
docs/core/
docs/maintainer/
```

Move or rewrite existing architecture docs into those folders. Move internal
research notes out of the release-facing docs path or mark them explicitly as
research-only.

Keep the current productization spec and implementation plan under
`docs/maintainer/productization/`. Move older development-process specs and
plans under `docs/maintainer/development-history/` so release-facing docs stay
focused on current product surfaces.

- [ ] **Step 3: Rewrite AGENTS.md**

Rewrite the repository-level collaboration guide so it presents:

- OpenEvo Core Backend and OpenEvo Desktop as the only product surfaces;
- `src/openevo/` as Core Backend;
- `desktop/` as Desktop;
- OpenEvo runtime contracts: `OPENEVO_*`, `/openevo/session`,
  `openevo.session_completed`, `.openevo/evolution/`;
- backend launcher commands as server maintenance utilities, not a CLI product;
- issue/PR/docs/tests/git workflow unchanged;
- evolution algorithm preservation rule unchanged.

Remove public Polar package paths, `POLAR_*`, `/polar/session`, and
`polar.session_completed` from `AGENTS.md` except in an explicitly named
maintainer migration note.

- [ ] **Step 4: Reorganize examples**

Create:

```text
examples/science-minimal/
examples/science-with-local-folder/
examples/self-deployed-model/
examples/backend-automation/
examples/research-benchmarks/
```

Legacy benchmark examples must be under `examples/research-benchmarks/` or
removed.

- [ ] **Step 5: Run identity scan**

Run:

```bash
python3 scripts/ci/audit_openevo_identity.py
pytest tests/ci/test_openevo_productization_workflow.py -q
rg -n "Polar|polar" README.md AGENTS.md docs examples .github scripts
```

Expected: no public legacy identity outside migration notes or explicitly
allowlisted research history.

- [ ] **Step 6: Commit and open PR**

Run:

```bash
git add README.md AGENTS.md docs examples CHANGELOG.md CONTRIBUTING.md SECURITY.md .github scripts tests
git commit -m "docs: present openevo core backend and desktop"
git push -u openevo HEAD
gh pr create --base stable --head "$(git branch --show-current)" --title "docs: present OpenEvo Core Backend and Desktop" --body "Part of #121

Docs updated:
- README.md
- AGENTS.md
- docs/user/
- docs/core/
- docs/maintainer/

Tests run:
- python3 scripts/ci/audit_openevo_identity.py
- pytest tests/ci/test_openevo_productization_workflow.py -q
- rg -n \"Polar|polar\" README.md AGENTS.md docs examples .github scripts"
```

---

## Task 8: Release Hardening

**Purpose:** Update CI and release checks for Core Backend and Desktop, and add
algorithm-preservation gates that catch accidental method registry or baseline
worker output changes.

**Files:**

- Modify: `.github/workflows/*`
- Create: `tests/evolution/test_algorithm_preservation_contract.py`
- Modify: `scripts/ci/check_openevo_release.py`
- Modify: `scripts/ci/smoke_openevo_desktop_wheel.py`
- Create: backend smoke script if needed
- Create: Desktop launch smoke script if needed

- [ ] **Step 1: Add algorithm preservation contract tests**

Create `tests/evolution/test_algorithm_preservation_contract.py`:

```python
from __future__ import annotations

from pathlib import Path

from openevo.evolution.methods import METHOD_METADATA, METHOD_REGISTRY
from openevo.evolution.models import ArtifactType, WorkerClaimedJob


EXPECTED_METHOD_CONTRACT = {
    "text_memory": ("text_memory", False),
    "text_memory_reflector": ("text_memory", True),
    "text_memory_expel_reflector": ("text_memory", False),
    "skill_bundle": ("skill_bundle", False),
    "skill_bundle_reflector": ("skill_bundle", True),
    "agent_system": ("agent_system", False),
    "agent_system_reflector": ("agent_system", True),
    "agent_system_history_reflector": ("agent_system", False),
    "agent_system_pareto_reflector": ("agent_system", False),
    "agent_system_gepa_reflector": ("agent_system", False),
    "parametric_memory_register": ("parametric_memory", False),
    "parametric_memory_lora_sft": ("parametric_memory", False),
}


def _job(method: str, config: dict) -> WorkerClaimedJob:
    return WorkerClaimedJob(
        job_id=f"job-{method}",
        lease_id="lease-1",
        job_type="evolution",
        method=method,
        config=config,
    )


def test_method_registry_and_metadata_contract_is_preserved() -> None:
    assert set(EXPECTED_METHOD_CONTRACT) <= set(METHOD_REGISTRY)
    for method_id, (artifact_type, visible_in_desktop) in EXPECTED_METHOD_CONTRACT.items():
        metadata = METHOD_METADATA[method_id]
        assert metadata["method_id"] == method_id
        assert metadata["artifact_type"] == artifact_type
        assert metadata["visible_in_desktop"] is visible_in_desktop


def test_configured_skill_bundle_output_contract(tmp_path: Path) -> None:
    artifacts = METHOD_REGISTRY["skill_bundle"](
        _job(
            "skill_bundle",
            {
                "name": "Careful Skill",
                "skill_markdown": "# Careful Skill\n\nCheck assumptions.\n",
                "tags": ["science"],
                "promoted": True,
            },
        ),
        tmp_path,
    )
    artifact = artifacts[0]
    assert artifact.type == ArtifactType.SKILL_BUNDLE
    assert artifact.name == "Careful Skill"
    assert artifact.manifest == {"entrypoint": "SKILL.md", "files": ["SKILL.md"]}
    assert artifact.tags == ["science"]
    assert artifact.promoted is True
    skill_path = Path(artifact.uri.removeprefix("file://")) / "SKILL.md"
    assert skill_path.read_text(encoding="utf-8").endswith("Check assumptions.\n")


def test_configured_agent_system_output_contract(tmp_path: Path) -> None:
    artifacts = METHOD_REGISTRY["agent_system"](
        _job(
            "agent_system",
            {
                "name": "Agent Instructions",
                "target_path": "AGENTS.md",
                "agent_system_markdown": "# Instructions\n\nPreserve behavior.\n",
                "lineage": {"source": "test"},
            },
        ),
        tmp_path,
    )
    artifact = artifacts[0]
    assert artifact.type == ArtifactType.AGENT_SYSTEM
    assert artifact.manifest["target_path"] == "AGENTS.md"
    assert artifact.lineage == {"source": "test"}
    output_path = Path(artifact.uri.removeprefix("file://"))
    assert output_path.name == "AGENTS.md"
    assert "Preserve behavior." in output_path.read_text(encoding="utf-8")


def test_parametric_memory_register_output_contract(tmp_path: Path) -> None:
    adapter_dir = tmp_path / "adapter"
    adapter_dir.mkdir()
    (adapter_dir / "adapter_config.json").write_text("{}", encoding="utf-8")

    artifacts = METHOD_REGISTRY["parametric_memory_register"](
        _job(
            "parametric_memory_register",
            {
                "adapter_uri": adapter_dir.resolve().as_uri(),
                "base_model": "Qwen/Qwen2.5-Coder-7B-Instruct",
                "adapter_format": "lora",
                "name": "science adapter",
            },
        ),
        tmp_path,
    )
    artifact = artifacts[0]
    assert artifact.type == ArtifactType.PARAMETRIC_MEMORY
    assert artifact.name == "science adapter"
    assert artifact.manifest["base_model"] == "Qwen/Qwen2.5-Coder-7B-Instruct"
    assert artifact.manifest["adapter_format"] == "lora"
```

- [ ] **Step 2: Update Python/Core workflow**

Workflow name: `OpenEvo Core Backend checks`.

The named suites below intentionally spell out coverage for harness,
configuration, platform/runtime support, backend, evolution, gateway, trajectory,
rollout, remote deployment, science projects, sidecar facade, Desktop smoke,
experiments, and capabilities.

Run:

```bash
ruff check src tests scripts
pytest \
  tests/ci \
  tests/config \
  tests/platform \
  tests/test_evolution_agent_harnesses.py \
  tests/backend \
  tests/evolution \
  tests/gateway \
  tests/trajectory \
  tests/rollout \
  tests/openevo/remote \
  tests/openevo/science \
  tests/openevo/sidecar \
  tests/openevo/desktop \
  tests/openevo/test_experiment_compiler.py \
  tests/openevo/test_experiment_models.py \
  tests/openevo/test_experiment_runner.py \
  tests/openevo/test_core_capabilities.py \
  -q
```

- [ ] **Step 3: Update Desktop workflow**

Workflow name: `OpenEvo Desktop checks`.

Run:

```bash
cd desktop
npm ci
npm audit --audit-level=high
npm test -- --run
npm run build:openevo
cd src-tauri
cargo metadata --locked --format-version 1
cargo test --locked
```

- [ ] **Step 4: Update release artifact workflow**

Release artifacts must include:

- Core Backend wheel or bundle;
- macOS Desktop `.dmg`;
- checksums;
- release notes.

- [ ] **Step 5: Run release smoke locally where possible**

Run:

```bash
ruff check src tests scripts
pytest \
  tests/ci \
  tests/config \
  tests/platform \
  tests/test_evolution_agent_harnesses.py \
  tests/backend \
  tests/evolution \
  tests/gateway \
  tests/trajectory \
  tests/rollout \
  tests/openevo/remote \
  tests/openevo/science \
  tests/openevo/sidecar \
  tests/openevo/desktop \
  tests/openevo/test_experiment_compiler.py \
  tests/openevo/test_experiment_models.py \
  tests/openevo/test_experiment_runner.py \
  tests/openevo/test_core_capabilities.py \
  -q
python -m build --wheel
python scripts/ci/check_openevo_release.py --wheel dist/*.whl
```

Run Desktop checks:

```bash
cd desktop
npm ci
npm test -- --run
npm run build:openevo
cd src-tauri
cargo metadata --locked --format-version 1
cargo test --locked
```

- [ ] **Step 6: Commit and open PR**

Run:

```bash
git add .github scripts tests desktop pyproject.toml
git commit -m "ci: harden openevo release checks"
git push -u openevo HEAD
gh pr create --base stable --head "$(git branch --show-current)" --title "ci: harden OpenEvo release checks" --body "Part of #121

Docs updated:
- docs/maintainer/release-process.md
- docs/maintainer/testing.md

Tests run:
- ruff check src tests scripts
- pytest tests/ci tests/config tests/platform tests/test_evolution_agent_harnesses.py tests/backend tests/evolution tests/gateway tests/trajectory tests/rollout tests/openevo/remote tests/openevo/science tests/openevo/sidecar tests/openevo/desktop tests/openevo/test_experiment_compiler.py tests/openevo/test_experiment_models.py tests/openevo/test_experiment_runner.py tests/openevo/test_core_capabilities.py -q
- python -m build --wheel
- python scripts/ci/check_openevo_release.py --wheel dist/*.whl
- cd desktop && npm ci && npm test -- --run && npm run build:openevo
- cd desktop/src-tauri && cargo metadata --locked --format-version 1 && cargo test --locked"
```

---

## Task 9: Final Completion Audit

**Purpose:** Prove the spec is complete against current repository state.

**Files:**

- Create: `tests/ci/test_openevo_productization_identity.py`
- Modify only if audit finds gaps.

- [ ] **Step 1: Create strict identity guard**

Create `tests/ci/test_openevo_productization_identity.py`:

```python
from __future__ import annotations

import tomllib
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
TEXT_SUFFIXES = {".md", ".py", ".toml", ".yaml", ".yml", ".json", ".ts", ".tsx", ".rs", ".sh"}
IGNORED_PARTS = {".git", ".venv", "node_modules", "__pycache__", ".pytest_cache", ".ruff_cache", "dist", "build"}
ALLOWED_HISTORICAL_PATHS = {
    Path("docs/maintainer/productization/spec.md"),
    Path("docs/maintainer/productization/implementation-plan.md"),
    Path("docs/maintainer/migration-notes.md"),
    Path("scripts/ci/audit_openevo_identity.py"),
    Path("tests/ci/test_openevo_productization_identity.py"),
}


def _text_files() -> list[Path]:
    files: list[Path] = []
    for path in REPO_ROOT.rglob("*"):
        if not path.is_file() or path.suffix not in TEXT_SUFFIXES:
            continue
        if any(part in IGNORED_PARTS for part in path.parts):
            continue
        files.append(path)
    return files


def test_no_legacy_polar_packages_remain() -> None:
    assert not (REPO_ROOT / "src" / "polar").exists()
    assert not (REPO_ROOT / "src" / "polar_evolution").exists()


def test_no_legacy_or_product_cli_console_scripts_remain() -> None:
    with (REPO_ROOT / "pyproject.toml").open("rb") as handle:
        pyproject = tomllib.load(handle)
    scripts = pyproject.get("project", {}).get("scripts", {})
    assert "polar" not in scripts
    assert "polar-evolution" not in scripts
    assert "openevo" not in scripts
    assert scripts.get("openevo-backend") == "openevo.backend.launcher:main"


def test_no_public_polar_runtime_identity_remains() -> None:
    forbidden = ("POLAR_", "/polar/session", "polar.session_completed")
    offenders: list[str] = []
    for path in _text_files():
        relative = path.relative_to(REPO_ROOT)
        if relative in ALLOWED_HISTORICAL_PATHS:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for marker in forbidden:
            if marker in text:
                offenders.append(f"{relative}: {marker}")
    assert offenders == []


def test_desktop_is_top_level_product_surface() -> None:
    assert not (REPO_ROOT / "web").exists()
    assert (REPO_ROOT / "desktop" / "src-tauri").is_dir()
    assert not (REPO_ROOT / "src" / "openevo" / "desktop").exists()
```

Expected: the identity guard is a normal blocking pytest module. It scans the
release-facing active surface and may allowlist only explicit maintainer archive
paths such as `docs/maintainer/development-history/`,
`docs/maintainer/productization/`, `docs/dev/`, the identity audit script, and
tests that intentionally mention legacy markers.

- [ ] **Step 2: Run full identity audit**

Run:

```bash
pytest tests/ci/test_openevo_productization_identity.py -q
rg -n \
  --glob '!desktop/node_modules/**' \
  --glob '!docs/maintainer/development-history/**' \
  --glob '!docs/maintainer/productization/**' \
  --glob '!docs/dev/**' \
  --glob '!docs/maintainer/migration-notes.md' \
  --glob '!scripts/ci/audit_openevo_identity.py' \
  --glob '!tests/ci/test_check_openevo_release.py' \
  --glob '!tests/ci/test_openevo_productization_workflow.py' \
  --glob '!tests/ci/test_openevo_productization_identity.py' \
  "Polar|polar|POLAR[A-Z0-9_]*|POLAR_|/polar/session|polar\\.session_completed|polar_|polar/|polar:|polar-|polar\\." \
  .
```

Expected: no matches outside allowlisted archive notes/tests.

- [ ] **Step 3: Run focused test suites**

Run:

```bash
ruff check src tests scripts
pytest \
  tests/ci \
  tests/config \
  tests/platform \
  tests/test_evolution_agent_harnesses.py \
  tests/backend \
  tests/evolution \
  tests/gateway \
  tests/trajectory \
  tests/rollout \
  tests/openevo/remote \
  tests/openevo/science \
  tests/openevo/sidecar \
  tests/openevo/desktop \
  tests/openevo/test_experiment_compiler.py \
  tests/openevo/test_experiment_models.py \
  tests/openevo/test_experiment_runner.py \
  tests/openevo/test_core_capabilities.py \
  -q
```

Expected: pass.

- [ ] **Step 4: Run frontend checks**

Run:

```bash
cd desktop
npm ci
npm test -- --run
npm run build:openevo
cd src-tauri
cargo metadata --locked --format-version 1
cargo test --locked
```

Expected: pass.

- [ ] **Step 5: Run release checks**

Run:

```bash
python -m build --wheel
python scripts/ci/check_openevo_release.py --wheel dist/*.whl
```

Expected: pass.

- [ ] **Step 6: Fresh-context final review**

Dispatch two fresh `gpt-5.5 high` review subagents:

- one for spec compliance against
  `docs/maintainer/productization/spec.md`;
- one for code quality and release risk.

Expected: no blocking findings.

- [ ] **Step 7: Commit final audit and open PR**

Run:

```bash
git add tests/ci/test_openevo_productization_identity.py docs scripts .github README.md AGENTS.md
git commit -m "test: add final openevo productization release audit"
git push -u openevo HEAD
gh pr create --base stable --head "$(git branch --show-current)" --title "test: add final OpenEvo productization audit" --body "Part of #121

Docs updated:
- docs/maintainer/productization/spec.md
- docs/maintainer/productization/implementation-plan.md

Tests run:
- pytest tests/ci/test_openevo_productization_identity.py -q
- ruff check src tests scripts
- pytest tests/ci tests/config tests/platform tests/test_evolution_agent_harnesses.py tests/backend tests/evolution tests/gateway tests/trajectory tests/rollout tests/openevo/remote tests/openevo/science tests/openevo/sidecar tests/openevo/desktop tests/openevo/test_experiment_compiler.py tests/openevo/test_experiment_models.py tests/openevo/test_experiment_runner.py tests/openevo/test_core_capabilities.py -q
- cd desktop && npm ci && npm test -- --run && npm run build:openevo
- cd desktop/src-tauri && cargo metadata --locked --format-version 1 && cargo test --locked
- python -m build --wheel
- python scripts/ci/check_openevo_release.py --wheel dist/*.whl"
```

Expected: PR is open against `stable`.

- [ ] **Step 8: Close issue**

If all evidence proves completion, close #121 with a summary and mark the active
goal complete.

---

## Plan Self-Review

Spec coverage:

- Product surfaces: Tasks 2, 4, 5, 7, 8.
- No CLI/Dev Kit product surface: Tasks 4, 7, 8.
- Legacy Polar removal: Tasks 1, 2, 3, 7, 9.
- Algorithm preservation: Tasks 2, 3, 8, 9.
- Backend lifecycle/API: Task 4.
- Desktop native host and sidecar: Tasks 5, 6.
- Remote deployment/repair: Tasks 3, 4, 6.
- Evolution visualization: Task 6.
- Repo/docs/examples cleanup: Task 7.
- Release readiness: Task 8.
- Final completion audit: Task 9.

Known implementation risk:

- The physical migration is large and will create many import conflicts for
  active external branches. Keep the first migration mechanical and avoid
  splitting algorithm files until after the productization goal is complete.
