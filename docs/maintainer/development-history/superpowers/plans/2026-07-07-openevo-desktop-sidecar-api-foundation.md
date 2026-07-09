# OpenEvo Desktop Sidecar API Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the first local OpenEvo Desktop sidecar HTTP API and connect the `/openevo` web shell to it with a fixture fallback.

**Architecture:** The Python sidecar owns a narrow FastAPI app under `/openevo-api`, separate from the existing Polar `/api` proxy. It returns typed Desktop shell status JSON only; it does not run SSH, bootstrap, or remote daemons yet. The web app fetches that endpoint through a Vite dev proxy and maps snake_case sidecar JSON to the existing camelCase shell model.

**Tech Stack:** Python 3.11, FastAPI, Pydantic v2, uvicorn, pytest, TypeScript, React, Vite, Vitest.

---

Tracked by #49.

## File Structure

- Create `src/openevo/sidecar/api.py`: FastAPI app factory, sidecar shell status models, default status builder, and server runner helper.
- Modify `src/openevo/sidecar/__init__.py`: export app factory and status models.
- Modify `src/openevo/cli.py`: add `openevo sidecar serve` CLI command.
- Create `tests/openevo/sidecar/test_api.py`: API endpoint and CLI runner tests.
- Create `web/src/api/openevo.ts`: sidecar fetcher and API-to-shell-model mapper.
- Create `web/src/api/openevo.test.ts`: mapper tests.
- Modify `web/src/routes/OpenEvoDesktop.tsx`: fetch sidecar shell status on mount and fall back to fixture state.
- Modify `web/vite.config.ts`: proxy `/openevo-api` to `http://127.0.0.1:3766`.
- Modify `docs/architecture/openevo-desktop-science-foundation.md`: document the local sidecar API boundary.

## Task 1: Python Sidecar API Contract

**Files:**
- Create: `tests/openevo/sidecar/test_api.py`
- Create: `src/openevo/sidecar/api.py`
- Modify: `src/openevo/sidecar/__init__.py`

- [ ] **Step 1: Write failing API tests**

Create `tests/openevo/sidecar/test_api.py`:

```python
from __future__ import annotations

from fastapi.testclient import TestClient

from openevo.sidecar import create_sidecar_app, default_desktop_shell_status


def test_sidecar_health_endpoint() -> None:
    client = TestClient(create_sidecar_app())

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"service": "openevo-sidecar", "status": "ok"}


def test_desktop_shell_endpoint_preserves_subscription_readiness() -> None:
    client = TestClient(create_sidecar_app())

    response = client.get("/openevo-api/desktop/shell")

    assert response.status_code == 200
    payload = response.json()
    assert payload["remote"]["id"] == "lab-gpu"
    assert payload["execution"]["mode"] == "codex_subscription_transcript"
    assert payload["execution"]["token_metrics_available"] is False
    assert payload["bootstrap"]["ready"] is True
    assert payload["bootstrap"]["readiness_notes"] == [
        "Codex subscription login available"
    ]


def test_default_desktop_status_round_trips_as_json() -> None:
    status = default_desktop_shell_status()

    restored = type(status).model_validate(status.model_dump(mode="json"))

    assert restored == status
```

- [ ] **Step 2: Run tests to verify RED**

Run:

```bash
PYTHONPATH=src /home/ziyi/ProRL-Agent-Server/.venv/bin/python -m pytest tests/openevo/sidecar/test_api.py -q
```

Expected: FAIL because `create_sidecar_app` and `default_desktop_shell_status` are not exported.

- [ ] **Step 3: Implement the API contract**

Create `src/openevo/sidecar/api.py` with strict Pydantic models:

```python
from __future__ import annotations

from typing import Literal

from fastapi import FastAPI
from pydantic import BaseModel, ConfigDict, Field, field_validator


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        validate_default=True,
    )


class SidecarHealth(_StrictFrozenModel):
    service: Literal["openevo-sidecar"] = "openevo-sidecar"
    status: Literal["ok"] = "ok"


class DesktopRemoteProxy(_StrictFrozenModel):
    https_proxy: str | None = None
    huggingface_endpoint: str | None = None


class DesktopRemoteProfile(_StrictFrozenModel):
    id: str
    host: str
    user: str
    proxy: DesktopRemoteProxy = Field(default_factory=DesktopRemoteProxy)


class DesktopScienceProject(_StrictFrozenModel):
    name: str
    task_id: str
    source: str
    objective: str


class DesktopExecutionStatus(_StrictFrozenModel):
    mode: Literal["codex_subscription_transcript", "codex_managed_local_inference"]
    model: str
    token_metrics_available: bool


class DesktopBootstrapStatus(_StrictFrozenModel):
    ready: bool
    state_root: str
    workspace_root: str
    readiness_notes: tuple[str, ...] = Field(default_factory=tuple)

    @field_validator("readiness_notes", mode="before")
    @classmethod
    def _coerce_notes(cls, value):
        if isinstance(value, list):
            return tuple(value)
        return value


class DesktopServiceStatus(_StrictFrozenModel):
    id: str
    label: str
    state: Literal["ready", "running", "planned", "blocked"]
    detail: str


class DesktopEvolutionStep(_StrictFrozenModel):
    id: str
    label: str
    state: Literal["complete", "running", "planned", "blocked"]
    detail: str


class DesktopDeveloperMode(_StrictFrozenModel):
    enabled: bool = False
    benchmark_controls_visible: bool = False


class OpenEvoDesktopShellStatus(_StrictFrozenModel):
    remote: DesktopRemoteProfile
    project: DesktopScienceProject
    execution: DesktopExecutionStatus
    bootstrap: DesktopBootstrapStatus
    services: tuple[DesktopServiceStatus, ...] = Field(default_factory=tuple)
    evolution: tuple[DesktopEvolutionStep, ...] = Field(default_factory=tuple)
    developer_mode: DesktopDeveloperMode = Field(default_factory=DesktopDeveloperMode)

    @field_validator("services", "evolution", mode="before")
    @classmethod
    def _coerce_tuples(cls, value):
        if isinstance(value, list):
            return tuple(value)
        return value


def default_desktop_shell_status() -> OpenEvoDesktopShellStatus:
    return OpenEvoDesktopShellStatus(
        remote=DesktopRemoteProfile(
            id="lab-gpu",
            host="gpu.example.edu",
            user="alice",
            proxy=DesktopRemoteProxy(
                https_proxy="http://127.0.0.1:7890",
                huggingface_endpoint="https://hf-mirror.com",
            ),
        ),
        project=DesktopScienceProject(
            name="Protein Folding Literature Sprint",
            task_id="folding-baseline",
            source="Git repository: github.com/example/protein-workflows",
            objective=(
                "Survey recent folding papers, extract benchmark tables, "
                "and run the baseline analysis notebook."
            ),
        ),
        execution=DesktopExecutionStatus(
            mode="codex_subscription_transcript",
            model="gpt-5.1-codex-mini",
            token_metrics_available=False,
        ),
        bootstrap=DesktopBootstrapStatus(
            ready=True,
            state_root=(
                "/home/alice/.openevo/runs/"
                "protein-folding-literature-sprint/folding-baseline"
            ),
            workspace_root="/home/alice/.openevo/workspaces",
            readiness_notes=("Codex subscription login available",),
        ),
        services=(
            DesktopServiceStatus(
                id="ssh",
                label="SSH transport",
                state="ready",
                detail="Remote command execution available",
            ),
            DesktopServiceStatus(
                id="workspace",
                label="Workspace",
                state="ready",
                detail="Repository materialized in managed workspace",
            ),
            DesktopServiceStatus(
                id="bootstrap",
                label="Bootstrap",
                state="ready",
                detail="Runtime image and manifests prepared",
            ),
            DesktopServiceStatus(
                id="openevo-backend",
                label="OpenEvo backend",
                state="planned",
                detail="Service supervisor integration is next",
            ),
        ),
        evolution=(
            DesktopEvolutionStep(
                id="transcript",
                label="Transcript capture",
                state="complete",
                detail="Codex subscription mode uses transcript trajectory data",
            ),
            DesktopEvolutionStep(
                id="memory",
                label="Text memory",
                state="complete",
                detail="Two durable research notes promoted",
            ),
            DesktopEvolutionStep(
                id="skills",
                label="Skill bundle",
                state="running",
                detail="Extracting reusable literature-review workflow",
            ),
            DesktopEvolutionStep(
                id="agent-system",
                label="Agent system",
                state="planned",
                detail="Instruction diff will be reviewed after this round",
            ),
        ),
    )


def create_sidecar_app(
    status: OpenEvoDesktopShellStatus | None = None,
) -> FastAPI:
    desktop_status = status or default_desktop_shell_status()
    app = FastAPI(title="OpenEvo Desktop Sidecar", version="0.1.0")

    @app.get("/health", response_model=SidecarHealth)
    def health() -> SidecarHealth:
        return SidecarHealth()

    @app.get(
        "/openevo-api/desktop/shell",
        response_model=OpenEvoDesktopShellStatus,
    )
    def desktop_shell() -> OpenEvoDesktopShellStatus:
        return desktop_status

    return app
```

Update `src/openevo/sidecar/__init__.py` to export `create_sidecar_app`, `default_desktop_shell_status`, and `OpenEvoDesktopShellStatus`.

- [ ] **Step 4: Run tests to verify GREEN**

Run:

```bash
PYTHONPATH=src /home/ziyi/ProRL-Agent-Server/.venv/bin/python -m pytest tests/openevo/sidecar/test_api.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/openevo/sidecar/api.py src/openevo/sidecar/__init__.py tests/openevo/sidecar/test_api.py
git commit -m "feat: add openevo sidecar api contract"
```

## Task 2: Sidecar Serve CLI

**Files:**
- Modify: `tests/openevo/test_cli.py`
- Modify: `src/openevo/cli.py`

- [ ] **Step 1: Write failing CLI test**

Append to `tests/openevo/test_cli.py`:

```python
def test_cli_sidecar_serve_invokes_runner(monkeypatch) -> None:
    calls = []

    def fake_runner(app, *, host: str, port: int) -> None:
        calls.append((app.title, host, port))

    monkeypatch.setattr("openevo.cli._run_sidecar_server", fake_runner)

    exit_code = main(
        [
            "sidecar",
            "serve",
            "--host",
            "127.0.0.1",
            "--port",
            "3766",
        ]
    )

    assert exit_code == 0
    assert calls == [("OpenEvo Desktop Sidecar", "127.0.0.1", 3766)]
```

- [ ] **Step 2: Run test to verify RED**

Run:

```bash
PYTHONPATH=src /home/ziyi/ProRL-Agent-Server/.venv/bin/python -m pytest tests/openevo/test_cli.py::test_cli_sidecar_serve_invokes_runner -q
```

Expected: FAIL because `sidecar serve` is not registered or `_run_sidecar_server` does not exist.

- [ ] **Step 3: Implement CLI serve command**

Modify `src/openevo/cli.py`:

```python
from openevo.sidecar import (
    build_sidecar_science_plan,
    create_sidecar_app,
    load_remote_profile_config,
)
```

Add parser:

```python
serve_parser = sidecar_subparsers.add_parser(
    "serve",
    help="Run the local OpenEvo Desktop sidecar API.",
)
serve_parser.add_argument("--host", default="127.0.0.1", help="Host to bind.")
serve_parser.add_argument("--port", type=int, default=3766, help="Port to bind.")
```

Route handler:

```python
if args.sidecar_command == "serve":
    return _handle_sidecar_serve(args)
```

Add helpers:

```python
def _handle_sidecar_serve(args: argparse.Namespace) -> int:
    _run_sidecar_server(create_sidecar_app(), host=args.host, port=args.port)
    return 0


def _run_sidecar_server(app, *, host: str, port: int) -> None:
    import uvicorn

    uvicorn.run(app, host=host, port=port)
```

- [ ] **Step 4: Run CLI test to verify GREEN**

Run:

```bash
PYTHONPATH=src /home/ziyi/ProRL-Agent-Server/.venv/bin/python -m pytest tests/openevo/test_cli.py::test_cli_sidecar_serve_invokes_runner -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/openevo/cli.py tests/openevo/test_cli.py
git commit -m "feat: expose openevo sidecar api server"
```

## Task 3: Web Sidecar Client and Route Fallback

**Files:**
- Create: `web/src/api/openevo.ts`
- Create: `web/src/api/openevo.test.ts`
- Modify: `web/src/routes/OpenEvoDesktop.tsx`
- Modify: `web/vite.config.ts`

- [ ] **Step 1: Write failing mapper test**

Create `web/src/api/openevo.test.ts`:

```ts
import { describe, expect, it } from "vitest";
import { toOpenEvoDesktopShellModel } from "./openevo";

describe("OpenEvo sidecar client", () => {
  it("maps sidecar shell status to the route model", () => {
    const model = toOpenEvoDesktopShellModel({
      remote: {
        id: "lab-gpu",
        host: "gpu.example.edu",
        user: "alice",
        proxy: {
          https_proxy: "http://127.0.0.1:7890",
          huggingface_endpoint: "https://hf-mirror.com",
        },
      },
      project: {
        name: "Protein Folding Literature Sprint",
        task_id: "folding-baseline",
        source: "Git repository: github.com/example/protein-workflows",
        objective: "Survey papers.",
      },
      execution: {
        mode: "codex_subscription_transcript",
        model: "gpt-5.1-codex-mini",
        token_metrics_available: false,
      },
      bootstrap: {
        ready: true,
        state_root: "/home/alice/.openevo/runs/protein/folding",
        workspace_root: "/home/alice/.openevo/workspaces",
        readiness_notes: ["Codex subscription login available"],
      },
      services: [
        {
          id: "ssh",
          label: "SSH transport",
          state: "ready",
          detail: "Remote command execution available",
        },
      ],
      evolution: [
        {
          id: "transcript",
          label: "Transcript capture",
          state: "complete",
          detail: "Transcript captured.",
        },
      ],
      developer_mode: {
        enabled: false,
        benchmark_controls_visible: false,
      },
    });

    expect(model.remote.proxy.httpsProxy).toBe("http://127.0.0.1:7890");
    expect(model.project.taskId).toBe("folding-baseline");
    expect(model.execution.tokenMetricsAvailable).toBe(false);
    expect(model.bootstrap.readinessNotes).toEqual([
      "Codex subscription login available",
    ]);
    expect(model.developerMode.benchmarkControlsVisible).toBe(false);
  });
});
```

- [ ] **Step 2: Run test to verify RED**

Run:

```bash
cd web
npm test -- --run src/api/openevo.test.ts
```

Expected: FAIL because `web/src/api/openevo.ts` does not exist.

- [ ] **Step 3: Implement client mapper and fetcher**

Create `web/src/api/openevo.ts` with:

```ts
import { api } from "./client";
import type {
  EvolutionStepState,
  OpenEvoDesktopShellModel,
  RemoteServiceState,
} from "../routes/openevoDesktopModel";

export interface OpenEvoDesktopShellStatusPayload {
  remote: {
    id: string;
    host: string;
    user: string;
    proxy: {
      https_proxy: string | null;
      huggingface_endpoint: string | null;
    };
  };
  project: {
    name: string;
    task_id: string;
    source: string;
    objective: string;
  };
  execution: {
    mode: OpenEvoDesktopShellModel["execution"]["mode"];
    model: string;
    token_metrics_available: boolean;
  };
  bootstrap: {
    ready: boolean;
    state_root: string;
    workspace_root: string;
    readiness_notes: string[];
  };
  services: Array<{
    id: string;
    label: string;
    state: RemoteServiceState;
    detail: string;
  }>;
  evolution: Array<{
    id: string;
    label: string;
    state: EvolutionStepState;
    detail: string;
  }>;
  developer_mode: {
    enabled: boolean;
    benchmark_controls_visible: boolean;
  };
}

export async function fetchOpenEvoDesktopShellModel(): Promise<OpenEvoDesktopShellModel> {
  const payload = await api.get<OpenEvoDesktopShellStatusPayload>(
    "/openevo-api/desktop/shell",
  );
  return toOpenEvoDesktopShellModel(payload);
}

export function toOpenEvoDesktopShellModel(
  payload: OpenEvoDesktopShellStatusPayload,
): OpenEvoDesktopShellModel {
  return {
    remote: {
      id: payload.remote.id,
      host: payload.remote.host,
      user: payload.remote.user,
      proxy: {
        httpsProxy: payload.remote.proxy.https_proxy ?? "not configured",
        huggingFaceEndpoint:
          payload.remote.proxy.huggingface_endpoint ?? "not configured",
      },
    },
    project: {
      name: payload.project.name,
      taskId: payload.project.task_id,
      source: payload.project.source,
      objective: payload.project.objective,
    },
    execution: {
      mode: payload.execution.mode,
      model: payload.execution.model,
      tokenMetricsAvailable: payload.execution.token_metrics_available,
    },
    bootstrap: {
      ready: payload.bootstrap.ready,
      stateRoot: payload.bootstrap.state_root,
      workspaceRoot: payload.bootstrap.workspace_root,
      readinessNotes: payload.bootstrap.readiness_notes,
    },
    services: payload.services,
    evolution: payload.evolution,
    developerMode: {
      enabled: payload.developer_mode.enabled,
      benchmarkControlsVisible:
        payload.developer_mode.benchmark_controls_visible,
    },
  };
}
```

- [ ] **Step 4: Wire route fetch and Vite proxy**

Modify `web/src/routes/OpenEvoDesktop.tsx`:

```ts
import { useEffect, useState } from "react";
import { fetchOpenEvoDesktopShellModel } from "../api/openevo";
```

Inside `OpenEvoDesktop()`:

```ts
const [model, setModel] = useState(() => getOpenEvoDesktopShellModel());

useEffect(() => {
  let cancelled = false;

  fetchOpenEvoDesktopShellModel()
    .then((nextModel) => {
      if (!cancelled) {
        setModel(nextModel);
      }
    })
    .catch(() => undefined);

  return () => {
    cancelled = true;
  };
}, []);
```

Modify `web/vite.config.ts` proxy:

```ts
"/openevo-api": {
  target: "http://127.0.0.1:3766",
  changeOrigin: true,
},
```

- [ ] **Step 5: Run tests and build**

Run:

```bash
cd web
npm test -- --run src/api/openevo.test.ts src/routes/openevoDesktopModel.test.ts
npm run build
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add web/src/api/openevo.ts web/src/api/openevo.test.ts web/src/routes/OpenEvoDesktop.tsx web/vite.config.ts
git commit -m "feat: connect openevo web shell to sidecar api"
```

## Task 4: Documentation and Final Verification

**Files:**
- Modify: `docs/architecture/openevo-desktop-science-foundation.md`

- [ ] **Step 1: Update architecture documentation**

Add a subsection under `Desktop Web Shell`:

```markdown
### Local Sidecar API

OpenEvo Desktop starts a local sidecar API with
`openevo sidecar serve --host 127.0.0.1 --port 3766`.

The first endpoint is `GET /openevo-api/desktop/shell`. It returns typed shell
status for the `/openevo` route and keeps the same subscription transcript
semantics as the Python sidecar contracts: token-level metrics remain false in
subscription mode, bootstrap readiness is represented separately from
informational readiness notes, and no direct model API call is made.

This slice does not let the HTTP API start SSH bootstrap, vLLM, Polar gateway,
rollout, or evolution worker processes. Those operations remain behind the
existing CLI and remote lifecycle contracts until a supervisor is added.
```

- [ ] **Step 2: Run final verification**

Run:

```bash
PYTHONPATH=src /home/ziyi/ProRL-Agent-Server/.venv/bin/python -m pytest tests/openevo/test_cli.py tests/openevo/sidecar tests/openevo/remote tests/openevo/science tests/evolution/test_models.py -q
/home/ziyi/ProRL-Agent-Server/.venv/bin/ruff check src/openevo/sidecar src/openevo/cli.py tests/openevo/sidecar tests/openevo/test_cli.py
(cd web && npm test -- --run)
(cd web && npm run build)
(cd web && npm audit --omit=dev --audit-level=low)
git diff --check openevo/stable...HEAD
```

Expected: all commands exit 0.

- [ ] **Step 3: Commit docs**

```bash
git add docs/architecture/openevo-desktop-science-foundation.md
git commit -m "docs: document openevo sidecar api"
```

- [ ] **Step 4: Request final code review**

Dispatch a gpt-5.5 high-effort reviewer for `openevo/stable...HEAD`, requiring checks against issue #49 and the sidecar/web API boundary.

- [ ] **Step 5: Push, PR, and merge**

```bash
git push -u openevo codex/openevo-desktop-sidecar-api
```

Open a PR to `stable` with `Fixes #49`, list docs and tests, wait for checks, then squash merge if clean.

## Self-Review

- Spec coverage: the tasks cover Python API, CLI serve, web fetch/fallback, Vite proxy, tests, docs, review, and PR merge for issue #49.
- Placeholder scan: no placeholder markers remain.
- Type consistency: Python uses snake_case response fields; TypeScript mapper converts to existing camelCase shell model fields.
