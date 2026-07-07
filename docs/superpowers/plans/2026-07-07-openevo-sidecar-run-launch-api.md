# OpenEvo Sidecar Run Launch API Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let OpenEvo Desktop launch the configured Science task after workspace sync and bootstrap readiness, without introducing a full background supervisor in this slice.

**Architecture:** Add a protected `POST /openevo-api/desktop/run` endpoint beside the workspace and bootstrap endpoints. The endpoint requires a config-backed session, sidecar mutation token, ready workspace service, and ready bootstrap status with a bootstrap report. It builds a deterministic `openevo run <experiment_snapshot> --output-dir <state_root>/runs/latest --json` command, executes it through the existing `RemoteExecutorTransport`, returns a structured launch report, and refreshes shell status. The web shell stores the sidecar token from `/desktop/shell`, sends it on Start Run, and updates visible status from the response.

**Tech Stack:** Python 3.11+, FastAPI, Pydantic v2, existing `openevo.sidecar` API contracts, existing `RemoteExecutorTransport`, React 19, Vite, Vitest, happy-dom.

---

### Task 1: Sidecar Run Endpoint

**Files:**
- Modify: `src/openevo/sidecar/api.py`
- Modify: `src/openevo/sidecar/__init__.py`
- Modify: `tests/openevo/sidecar/test_api.py`

- [ ] **Step 1: Write failing API tests**

Add tests to `tests/openevo/sidecar/test_api.py`:

- no-config session returns 409 when token is valid;
- missing/invalid token returns 403;
- not-ready workspace/bootstrap returns 409;
- run launch after workspace+bootstrap executes `openevo run` command and marks backend/transcript running/complete;
- command failure returns 200 report with blocked status;
- concurrent run launch returns 409.

- [ ] **Step 2: Verify red**

Run:

```bash
PYTHONPATH=src /home/ziyi/ProRL-Agent-Server/.venv/bin/python -m pytest tests/openevo/sidecar/test_api.py -q
```

Expected: FAIL because `/openevo-api/desktop/run` is missing.

- [ ] **Step 3: Implement endpoint**

In `src/openevo/sidecar/api.py`:

- Add `OpenEvoDesktopRunResponse`.
- Add `last_run_report` and `run_lock` to `OpenEvoSidecarSession`.
- Add `POST /openevo-api/desktop/run`.
- Validate mutation token before any work.
- Require config-backed session.
- Require workspace service ready and bootstrap ready with `last_bootstrap_report.prepared_paths.experiment_snapshot` and `state_root`.
- Build command:

```bash
openevo run <experiment_snapshot> --output-dir <state_root>/runs/latest --json
```

- Run with `cwd=state_root`, timeout 86400 seconds.
- Return a strict dict report with `ready`, `status`, `command`, `return_code`, `stdout`, `stderr`, `output_dir`, and `started_at`.
- Update `openevo-backend` service and `transcript` evolution row from report.

- [ ] **Step 4: Verify green**

Run:

```bash
PYTHONPATH=src /home/ziyi/ProRL-Agent-Server/.venv/bin/python -m pytest tests/openevo/sidecar/test_api.py -q
/home/ziyi/ProRL-Agent-Server/.venv/bin/ruff check src/openevo/sidecar tests/openevo/sidecar/test_api.py
```

Expected: tests pass and ruff reports `All checks passed!`.

- [ ] **Step 5: Commit**

```bash
git add src/openevo/sidecar/api.py src/openevo/sidecar/__init__.py tests/openevo/sidecar/test_api.py
GIT_AUTHOR_NAME='ivowang' GIT_AUTHOR_EMAIL='ziyiwang@ieee.org' GIT_COMMITTER_NAME='ivowang' GIT_COMMITTER_EMAIL='ziyiwang@ieee.org' git commit -m "feat: add openevo sidecar run endpoint"
```

### Task 2: Web Start Run Action

**Files:**
- Modify: `web/src/api/openevo.ts`
- Modify: `web/src/api/openevo.test.ts`
- Modify: `web/src/routes/OpenEvoDesktop.tsx`
- Modify: `web/src/routes/OpenEvoDesktop.test.tsx`

- [ ] **Step 1: Write failing web tests**

Add `runOpenEvoStartRun()` API test to verify the mutation token is sent to `/openevo-api/desktop/run`. Add route tests that click `Start Run`, assert pending text, success status refresh, and error rendering.

- [ ] **Step 2: Verify red**

Run:

```bash
cd web && npm test -- --run
```

Expected: FAIL because the API function and button behavior are missing.

- [ ] **Step 3: Implement web client and route**

Add:

```ts
export interface OpenEvoRunResponsePayload {
  run: Record<string, any>;
  status: OpenEvoDesktopShellStatusPayload;
}

export interface OpenEvoRunResponse {
  run: Record<string, any>;
  status: OpenEvoDesktopShellModel;
}
```

and `runOpenEvoStartRun()` posting `/openevo-api/desktop/run` with the token header.

Enable `Start Run` when sidecar is connected and no workspace/bootstrap/run action is currently running. On success, set model from `response.status`; on failure, render the error.

- [ ] **Step 4: Verify web**

Run:

```bash
cd web && npm test -- --run
cd web && npm run build
cd web && npm audit --omit=dev --audit-level=low
```

Expected: tests/build pass and production audit reports 0 vulnerabilities.

- [ ] **Step 5: Commit**

```bash
git add web/src/api/openevo.ts web/src/api/openevo.test.ts web/src/routes/OpenEvoDesktop.tsx web/src/routes/OpenEvoDesktop.test.tsx
GIT_AUTHOR_NAME='ivowang' GIT_AUTHOR_EMAIL='ziyiwang@ieee.org' GIT_COMMITTER_NAME='ivowang' GIT_COMMITTER_EMAIL='ziyiwang@ieee.org' git commit -m "feat: wire openevo start run action"
```

### Task 3: Documentation, Verification, Review, PR

**Files:**
- Modify: `docs/architecture/openevo-desktop-science-foundation.md`

- [ ] **Step 1: Document run launch**

Document `POST /openevo-api/desktop/run`, readiness prerequisites, synchronous execution boundary, dry-run vs SSH behavior, and unsupported background job/log streaming/cancel semantics.

- [ ] **Step 2: Full verification**

Run:

```bash
PYTHONPATH=src /home/ziyi/ProRL-Agent-Server/.venv/bin/python -m pytest tests/openevo/test_cli.py tests/openevo/sidecar tests/openevo/remote tests/openevo/science tests/evolution/test_models.py -q
/home/ziyi/ProRL-Agent-Server/.venv/bin/ruff check src/openevo/sidecar src/openevo/cli.py tests/openevo/sidecar tests/openevo/test_cli.py
cd web && npm test -- --run
cd web && npm run build
cd web && npm audit --omit=dev --audit-level=low
git diff --check openevo/stable...HEAD
```

- [ ] **Step 3: HTTP smoke**

Start sidecar serve with a remote-path Science Project, POST workspace, POST bootstrap, POST run using the sidecar token, and assert run report ready plus refreshed shell status.

- [ ] **Step 4: Commit docs**

```bash
git add docs/architecture/openevo-desktop-science-foundation.md
GIT_AUTHOR_NAME='ivowang' GIT_AUTHOR_EMAIL='ziyiwang@ieee.org' GIT_COMMITTER_NAME='ivowang' GIT_COMMITTER_EMAIL='ziyiwang@ieee.org' git commit -m "docs: document openevo run launch api"
```

- [ ] **Step 5: gpt-5.5 high-effort review**

Request review for `openevo/stable...HEAD` and fix Critical/Important findings.

- [ ] **Step 6: Push, PR, CI, merge**

Push branch, create PR with `Fixes #57`, wait for checks, squash merge, fetch stable.

---

## Self-Review

- Spec coverage: Issue #57 acceptance criteria map to Tasks 1-3.
- Placeholder scan: no TBD/TODO placeholders remain.
- Type consistency: Python response is `OpenEvoDesktopRunResponse`; TypeScript response is `OpenEvoRunResponse`; token header reuses `X-OpenEvo-Sidecar-Token`.
