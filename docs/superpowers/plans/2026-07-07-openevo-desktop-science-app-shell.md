# OpenEvo Desktop Science App Shell Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the first OpenEvo Desktop Science workspace shell to the existing Vite/React web app without replacing the Polar dashboard.

**Architecture:** Keep existing Polar routes intact and add a dedicated `/openevo` route. The route renders a local fixture-backed Desktop Science workspace model that mirrors the Python Science/sidecar/bootstrap contracts, while explicitly avoiding real sidecar, SSH, Tauri, or remote service integration in this slice.

**Tech Stack:** React 19, Vite, TypeScript, Tailwind CSS, lucide-react icons, Vitest for focused frontend model tests.

Tracked by issue #47.

---

## File Structure

- Modify `web/package.json` and `web/package-lock.json`: add `lucide-react`, add `vitest`, and add `npm test`.
- Create `web/src/routes/openevoDesktopModel.ts`: typed fixture/state model for the `/openevo` shell.
- Create `web/src/routes/openevoDesktopModel.test.ts`: focused tests for the fixture contract and status counts.
- Create `web/src/routes/OpenEvoDesktop.tsx`: route component for the Desktop Science workspace.
- Modify `web/src/App.tsx`: add nav entry and route for `/openevo`.
- Modify `docs/architecture/openevo-desktop-science-foundation.md`: document the UI shell route and its fixture-only boundary.

## Task 1: Frontend Model Fixture And Tests

**Files:**
- Modify: `web/package.json`
- Modify: `web/package-lock.json`
- Create: `web/src/routes/openevoDesktopModel.ts`
- Create: `web/src/routes/openevoDesktopModel.test.ts`

- [ ] **Step 1: Add frontend test and icon dependencies**

Run:

```bash
cd web
npm install lucide-react
npm install --save-dev vitest
```

Then add the test script to `web/package.json`:

```json
"test": "vitest"
```

- [ ] **Step 2: Write the failing model test**

Create `web/src/routes/openevoDesktopModel.test.ts`:

```ts
import { describe, expect, it } from "vitest";
import {
  getOpenEvoDesktopShellModel,
  getOpenEvoTimelineSummary,
} from "./openevoDesktopModel";

describe("OpenEvo Desktop shell model", () => {
  it("describes the science user flow without benchmark controls", () => {
    const model = getOpenEvoDesktopShellModel();

    expect(model.project.name).toBe("Protein Folding Literature Sprint");
    expect(model.execution.mode).toBe("codex_subscription_transcript");
    expect(model.execution.tokenMetricsAvailable).toBe(false);
    expect(model.developerMode.enabled).toBe(false);
    expect(model.developerMode.benchmarkControlsVisible).toBe(false);
    expect(model.bootstrap.ready).toBe(true);
    expect(model.bootstrap.readinessNotes).toEqual([
      "Codex subscription login available",
    ]);
    expect(model.remote.proxy.httpsProxy).toBe("http://127.0.0.1:7890");
  });

  it("summarizes readiness and evolution progress for the route", () => {
    const model = getOpenEvoDesktopShellModel();
    const summary = getOpenEvoTimelineSummary(model);

    expect(summary.readyServices).toBe(3);
    expect(summary.totalServices).toBe(4);
    expect(summary.bootstrapReady).toBe(true);
    expect(summary.completedEvolutionSteps).toBe(2);
    expect(summary.totalEvolutionSteps).toBe(4);
    expect(summary.readinessNotes).toEqual([
      "Codex subscription login available",
    ]);
  });
});
```

- [ ] **Step 3: Run the test to verify RED**

Run:

```bash
cd web
npm test -- --run src/routes/openevoDesktopModel.test.ts
```

Expected: FAIL because `./openevoDesktopModel` does not exist.

- [ ] **Step 4: Implement the shell model**

Create `web/src/routes/openevoDesktopModel.ts` with:

```ts
export type RemoteServiceState = "ready" | "running" | "planned" | "blocked";

export type EvolutionStepState = "complete" | "running" | "planned" | "blocked";

export interface OpenEvoDesktopShellModel {
  remote: {
    id: string;
    host: string;
    user: string;
    proxy: {
      httpsProxy: string;
      huggingFaceEndpoint: string;
    };
  };
  project: {
    name: string;
    taskId: string;
    source: string;
    objective: string;
  };
  execution: {
    mode: "codex_subscription_transcript" | "codex_managed_local_inference";
    model: string;
    tokenMetricsAvailable: boolean;
  };
  bootstrap: {
    ready: boolean;
    stateRoot: string;
    workspaceRoot: string;
    readinessNotes: string[];
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
  developerMode: {
    enabled: boolean;
    benchmarkControlsVisible: boolean;
  };
}

export interface OpenEvoTimelineSummary {
  readyServices: number;
  totalServices: number;
  bootstrapReady: boolean;
  completedEvolutionSteps: number;
  totalEvolutionSteps: number;
  readinessNotes: string[];
}

export function getOpenEvoDesktopShellModel(): OpenEvoDesktopShellModel {
  return {
    remote: {
      id: "lab-gpu",
      host: "gpu.example.edu",
      user: "alice",
      proxy: {
        httpsProxy: "http://127.0.0.1:7890",
        huggingFaceEndpoint: "https://hf-mirror.com",
      },
    },
    project: {
      name: "Protein Folding Literature Sprint",
      taskId: "folding-baseline",
      source: "Git repository: github.com/example/protein-workflows",
      objective: "Survey recent folding papers, extract benchmark tables, and run the baseline analysis notebook.",
    },
    execution: {
      mode: "codex_subscription_transcript",
      model: "gpt-5.1-codex-mini",
      tokenMetricsAvailable: false,
    },
    bootstrap: {
      ready: true,
      stateRoot: "/home/alice/.openevo/runs/protein-folding-literature-sprint/folding-baseline",
      workspaceRoot: "/home/alice/.openevo/workspaces",
      readinessNotes: ["Codex subscription login available"],
    },
    services: [
      {
        id: "ssh",
        label: "SSH transport",
        state: "ready",
        detail: "Remote command execution available",
      },
      {
        id: "workspace",
        label: "Workspace",
        state: "ready",
        detail: "Repository materialized in managed workspace",
      },
      {
        id: "bootstrap",
        label: "Bootstrap",
        state: "ready",
        detail: "Runtime image and manifests prepared",
      },
      {
        id: "openevo-backend",
        label: "OpenEvo backend",
        state: "planned",
        detail: "Service supervisor integration is next",
      },
    ],
    evolution: [
      {
        id: "transcript",
        label: "Transcript capture",
        state: "complete",
        detail: "Codex subscription mode uses transcript trajectory data",
      },
      {
        id: "memory",
        label: "Text memory",
        state: "complete",
        detail: "Two durable research notes promoted",
      },
      {
        id: "skills",
        label: "Skill bundle",
        state: "running",
        detail: "Extracting reusable literature-review workflow",
      },
      {
        id: "agent-system",
        label: "Agent system",
        state: "planned",
        detail: "Instruction diff will be reviewed after this round",
      },
    ],
    developerMode: {
      enabled: false,
      benchmarkControlsVisible: false,
    },
  };
}

export function getOpenEvoTimelineSummary(
  model: OpenEvoDesktopShellModel,
): OpenEvoTimelineSummary {
  return {
    readyServices: model.services.filter((service) => service.state === "ready").length,
    totalServices: model.services.length,
    bootstrapReady: model.bootstrap.ready,
    completedEvolutionSteps: model.evolution.filter((step) => step.state === "complete").length,
    totalEvolutionSteps: model.evolution.length,
    readinessNotes: model.bootstrap.readinessNotes,
  };
}
```

- [ ] **Step 5: Run the model test to verify GREEN**

Run:

```bash
cd web
npm test -- --run src/routes/openevoDesktopModel.test.ts
```

Expected: PASS.

- [ ] **Step 6: Commit Task 1**

```bash
git add web/package.json web/package-lock.json web/src/routes/openevoDesktopModel.ts web/src/routes/openevoDesktopModel.test.ts
git commit -m "feat: add openevo desktop shell model"
```

## Task 2: OpenEvo Desktop Route

**Files:**
- Create: `web/src/routes/OpenEvoDesktop.tsx`
- Modify: `web/src/App.tsx`

- [ ] **Step 1: Write the failing route import**

Modify `web/src/App.tsx` to import and route `OpenEvoDesktop` before the component exists:

```ts
import { OpenEvoDesktop } from "./routes/OpenEvoDesktop";
```

Add navigation:

```tsx
<NavItem to="/openevo" label="OpenEvo" />
```

Add the route:

```tsx
<Route path="/openevo" element={<OpenEvoDesktop />} />
```

- [ ] **Step 2: Run build to verify RED**

Run:

```bash
cd web
npm run build
```

Expected: FAIL because `./routes/OpenEvoDesktop` does not exist.

- [ ] **Step 3: Implement the route component**

Create `web/src/routes/OpenEvoDesktop.tsx`. The component must:

- call `getOpenEvoDesktopShellModel()` and `getOpenEvoTimelineSummary()`;
- render a dense operational workspace, not a marketing landing page;
- show project configuration, remote/proxy settings, bootstrap paths, service states, and evolution timeline;
- state subscription mode as transcript-only and avoid token-level metrics;
- avoid benchmark controls in the ordinary science-user flow;
- use lucide-react icons for command buttons and compact status affordances.

- [ ] **Step 4: Run build to verify GREEN**

Run:

```bash
cd web
npm run build
```

Expected: PASS.

- [ ] **Step 5: Commit Task 2**

```bash
git add web/src/App.tsx web/src/routes/OpenEvoDesktop.tsx
git commit -m "feat: add openevo desktop route"
```

## Task 3: Docs, Visual Smoke, And Final Verification

**Files:**
- Modify: `docs/architecture/openevo-desktop-science-foundation.md`

- [ ] **Step 1: Document the route boundary**

Update `docs/architecture/openevo-desktop-science-foundation.md` with a `Desktop Web Shell` section:

```markdown
## Desktop Web Shell

The first web shell lives at `/openevo` in the existing Vite app. It is fixture/local-state backed in this slice and is meant to validate the ordinary science-user layout before the Python sidecar API is connected.

The shell intentionally keeps Terminal Bench and low-level runtime image fields out of the default flow. It displays the remote profile, proxy settings, Science Project summary, bootstrap paths, lifecycle readiness, and evolution timeline using the same concepts as the Python contracts.
```

- [ ] **Step 2: Run focused verification**

Run:

```bash
cd web
npm test -- --run
npm run build
```

Expected: both commands exit 0.

- [ ] **Step 3: Start the dev server for manual inspection**

Run:

```bash
cd web
npm run dev -- --host 0.0.0.0
```

Expected: Vite reports a local URL, usually `http://localhost:5173/`.

- [ ] **Step 4: Commit Task 3**

```bash
git add docs/architecture/openevo-desktop-science-foundation.md
git commit -m "docs: document openevo desktop shell"
```

- [ ] **Step 5: Final branch verification**

Run:

```bash
cd web
npm test -- --run
npm run build
git diff --check openevo/stable...HEAD
```

Expected: all pass.

- [ ] **Step 6: Review, push, PR, and merge**

Dispatch a gpt-5.5 high-effort reviewer for `openevo/stable...HEAD`. If no blocking issues remain, push the branch, create a PR against `stable` with `Fixes #47`, wait for checks, and merge when clean.
