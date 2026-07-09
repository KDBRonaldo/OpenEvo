# OpenEvo Desktop-Only Shell Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make packaged `openevo desktop serve` assets render an OpenEvo-only Desktop shell while preserving the shared Polar dashboard build for development.

**Architecture:** Add a build-time Vite mode flag consumed by `web/src/App.tsx`. The default app keeps the shared dashboard routes. The OpenEvo build renders only the existing `OpenEvoDesktop` experience and packages that build output into `src/openevo/desktop/web/`.

**Tech Stack:** React 19, React Router, Vite mode env files, Vitest SSR assertions, pytest package-script regression, FastAPI static serving unchanged.

---

## File Structure

- Create `web/.env.openevo-desktop`: sets `VITE_OPENEVO_DESKTOP_ONLY=true`.
- Create `web/src/App.test.tsx`: verifies shared and OpenEvo-only shell rendering.
- Modify `web/src/App.tsx`: export `AppShell`, branch shell by `desktopOnly`.
- Modify `web/package.json`: add `build:openevo`.
- Modify `tests/openevo/test_cli.py`: assert the OpenEvo build script and mode flag exist.
- Modify `docs/architecture/openevo-desktop-science-foundation.md`: document `npm run build:openevo`.
- Modify `docs/superpowers/specs/2026-07-07-openevo-desktop-only-shell-design.md`: keep design aligned with implementation details.
- Refresh `src/openevo/desktop/web/` from `web/dist/` after `npm run build:openevo`.

## Task 1: Desktop-Only Shell Tests

**Files:**
- Create: `web/src/App.test.tsx`
- Modify: `tests/openevo/test_cli.py`

- [ ] **Step 1: Add failing web tests**

Create `web/src/App.test.tsx`:

```tsx
// @vitest-environment happy-dom

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderToString } from "react-dom/server";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";
import { AppShell } from "./App";

function renderShell(path: string, desktopOnly: boolean) {
  const queryClient = new QueryClient();
  return renderToString(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[path]}>
        <AppShell desktopOnly={desktopOnly} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("AppShell", () => {
  it("renders the shared Polar dashboard shell by default", () => {
    const html = renderShell("/", false);

    expect(html).toContain("Polar Dashboard");
    expect(html).toContain('href="/tasks"');
    expect(html).toContain(">Dashboard<");
  });

  it("renders OpenEvo Desktop without shared dashboard navigation in desktop-only mode", () => {
    const html = renderShell("/openevo", true);

    expect(html).toContain("Protein Folding Literature Sprint");
    expect(html).toContain("Remote ready");
    expect(html).not.toContain("Polar Dashboard");
    expect(html).not.toContain('href="/tasks"');
    expect(html).not.toContain(">Dashboard<");
  });

  it("renders OpenEvo Desktop at the root path in desktop-only mode", () => {
    const html = renderShell("/", true);

    expect(html).toContain("Protein Folding Literature Sprint");
    expect(html).not.toContain("Not found");
  });
});
```

- [ ] **Step 2: Run the failing web tests**

Run:

```bash
cd web && npm test -- --run src/App.test.tsx
```

Expected: fail because `AppShell` is not exported and desktop-only mode does not exist.

- [ ] **Step 3: Add failing package-script test**

In `tests/openevo/test_cli.py`, append:

```python
def test_web_package_defines_openevo_desktop_build_mode() -> None:
    package = json.loads(Path("web/package.json").read_text(encoding="utf-8"))
    env_file = Path("web/.env.openevo-desktop")

    assert package["scripts"]["build:openevo"] == "vite build --mode openevo-desktop"
    assert env_file.read_text(encoding="utf-8").strip() == (
        "VITE_OPENEVO_DESKTOP_ONLY=true"
    )
```

- [ ] **Step 4: Run the failing package-script test**

Run:

```bash
PYTHONPATH=src /home/ziyi/ProRL-Agent-Server/.venv/bin/python -m pytest tests/openevo/test_cli.py::test_web_package_defines_openevo_desktop_build_mode -q
```

Expected: fail because `build:openevo` and `web/.env.openevo-desktop` do not exist.

## Task 2: Desktop-Only Shell Implementation

**Files:**
- Modify: `web/src/App.tsx`
- Modify: `web/package.json`
- Create: `web/.env.openevo-desktop`

- [ ] **Step 1: Implement `AppShell`**

Refactor `web/src/App.tsx`:

```tsx
const isOpenEvoDesktopOnlyBuild =
  import.meta.env.VITE_OPENEVO_DESKTOP_ONLY === "true";

export function AppShell({ desktopOnly = isOpenEvoDesktopOnlyBuild }: { desktopOnly?: boolean }) {
  const location = useLocation();
  const queryClient = useQueryClient();

  useEffect(() => {
    if (desktopOnly) {
      return;
    }
    const controller = new AbortController();
    subscribePolarEvents((event) => {
      // existing switch body unchanged
    }, controller);
    return () => controller.abort();
  }, [desktopOnly, queryClient]);

  if (desktopOnly) {
    return (
      <div className="min-h-full bg-slate-50 text-slate-900">
        <main className="mx-auto w-full max-w-7xl px-4 py-4">
          <Routes>
            <Route path="/" element={<OpenEvoDesktop />} />
            <Route path="/openevo/*" element={<OpenEvoDesktop />} />
            <Route path="*" element={<OpenEvoDesktop />} />
          </Routes>
        </main>
      </div>
    );
  }

  return (
    // existing shared dashboard shell unchanged
  );
}

export default function App() {
  return <AppShell />;
}
```

Keep the existing shared shell markup exactly as it is inside the non-desktop branch.

- [ ] **Step 2: Add Vite mode env and script**

Create `web/.env.openevo-desktop`:

```text
VITE_OPENEVO_DESKTOP_ONLY=true
```

Modify `web/package.json` scripts:

```json
"build:openevo": "vite build --mode openevo-desktop"
```

- [ ] **Step 3: Run tests**

Run:

```bash
cd web && npm test -- --run src/App.test.tsx
PYTHONPATH=src /home/ziyi/ProRL-Agent-Server/.venv/bin/python -m pytest tests/openevo/test_cli.py::test_web_package_defines_openevo_desktop_build_mode -q
```

Expected: both pass.

## Task 3: Package OpenEvo-Only Assets and Docs

**Files:**
- Modify: `src/openevo/desktop/web/*`
- Modify: `docs/architecture/openevo-desktop-science-foundation.md`
- Modify: `docs/superpowers/specs/2026-07-07-openevo-desktop-only-shell-design.md`

- [ ] **Step 1: Build and sync OpenEvo assets**

Run:

```bash
cd web && npm run build:openevo
rsync -a --delete web/dist/ src/openevo/desktop/web/
```

Expected: `src/openevo/desktop/web/` matches the OpenEvo-only build output.

- [ ] **Step 2: Verify packaged assets do not contain shared dashboard shell strings**

Run:

```bash
rg "Polar Dashboard|href=\\\"/tasks\\\"|>Dashboard<" src/openevo/desktop/web
```

Expected: no output.

- [ ] **Step 3: Update docs**

Update `docs/architecture/openevo-desktop-science-foundation.md` local Desktop serve section to say packaged Desktop assets are built with:

```bash
cd web && npm run build:openevo
rsync -a --delete web/dist/ src/openevo/desktop/web/
```

Update the spec to mention that implementation uses `.env.openevo-desktop` and `vite build --mode openevo-desktop`.

- [ ] **Step 4: Run focused verification**

Run:

```bash
PYTHONPATH=src /home/ziyi/ProRL-Agent-Server/.venv/bin/python -m pytest tests/openevo/test_cli.py tests/openevo/desktop/test_app.py tests/openevo/sidecar -q
/home/ziyi/ProRL-Agent-Server/.venv/bin/ruff check src/openevo/desktop src/openevo/cli.py tests/openevo/desktop tests/openevo/test_cli.py
cd web && npm test -- --run
cd web && npm run build:openevo
diff -qr web/dist src/openevo/desktop/web
/home/ziyi/ProRL-Agent-Server/.venv/bin/python -m build --wheel
git diff --check openevo/stable...HEAD
```

Expected: all commands pass. The final `diff -qr` and `git diff --check` produce no output.

## Task 4: PR and Merge

**Files:**
- Review all changed files.

- [ ] **Step 1: Commit implementation**

Run:

```bash
git add web/src/App.tsx web/src/App.test.tsx web/package.json web/.env.openevo-desktop tests/openevo/test_cli.py src/openevo/desktop/web docs/architecture/openevo-desktop-science-foundation.md docs/superpowers/specs/2026-07-07-openevo-desktop-only-shell-design.md
GIT_AUTHOR_NAME='ivowang' GIT_AUTHOR_EMAIL='ziyiwang@ieee.org' GIT_COMMITTER_NAME='ivowang' GIT_COMMITTER_EMAIL='ziyiwang@ieee.org' git commit -m "feat: add openevo desktop-only shell build"
```

- [ ] **Step 2: Push and open PR**

Run:

```bash
git push -u openevo codex/openevo-remote-bootstrap-doctor
```

Open a PR against `stable` with `Resolves #65` and include verification output.

## Self-Review

- Spec coverage: Desktop-only shell rendering, build mode, packaged asset refresh, docs, and tests are all covered.
- Placeholder scan: no TODO/TBD or unspecified commands.
- Type consistency: `AppShell`, `desktopOnly`, `.env.openevo-desktop`, and `build:openevo` are named consistently across tasks.
