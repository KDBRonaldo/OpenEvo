# OpenEvo Sidecar Bootstrap API Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let OpenEvo Desktop trigger the existing remote bootstrap executor through the local sidecar API and update the `/openevo` shell state.

**Architecture:** Add a small sidecar session object around existing contracts. A config-backed session owns a `ScienceProjectConfig`, `RemoteProfileConfig`, selected transport factory, current shell status, and last bootstrap report. `POST /openevo-api/desktop/bootstrap` builds and executes the existing remote bootstrap plan, then refreshes shell status; the web route calls this endpoint from the Bootstrap button.

**Tech Stack:** Python 3.11, FastAPI, Pydantic v2, pytest, TypeScript, React, Vite, Vitest.

---

Tracked by #53.

## File Structure

- Modify `src/openevo/sidecar/api.py`: add session model, bootstrap endpoint, report-to-status refresh.
- Modify `src/openevo/cli.py`: add `sidecar serve --transport dry-run|ssh` and pass a transport factory to the app.
- Modify `tests/openevo/sidecar/test_api.py`: endpoint tests for no-config and config-backed dry-run bootstrap.
- Modify `tests/openevo/test_cli.py`: CLI serve transport wiring tests.
- Modify `web/src/api/openevo.ts`: add bootstrap response types and `runOpenEvoBootstrap()`.
- Modify `web/src/api/openevo.test.ts`: mapper/client tests for bootstrap response.
- Modify `web/src/routes/OpenEvoDesktop.tsx`: enable Bootstrap button when sidecar-loaded model is present, call bootstrap endpoint, show loading/error.
- Modify `web/src/routes/OpenEvoDesktop.test.tsx`: server-render fallback still works with new props/state.
- Modify `docs/architecture/openevo-desktop-science-foundation.md`: document bootstrap endpoint boundary.

## Task 1: Sidecar Bootstrap Endpoint

**Files:**
- Modify: `tests/openevo/sidecar/test_api.py`
- Modify: `src/openevo/sidecar/api.py`

- [ ] **Step 1: Add failing API tests**

Add tests:

```python
def test_bootstrap_endpoint_requires_config_backed_session() -> None:
    client = TestClient(create_sidecar_app())

    response = client.post("/openevo-api/desktop/bootstrap")

    assert response.status_code == 409
    assert response.json()["detail"] == "Desktop bootstrap requires a config-backed sidecar session."
```

and a config-backed dry-run test that creates `ScienceProjectConfig` + `RemoteProfileConfig`, calls `create_sidecar_app_for_project(project, profile, transport_factory=lambda profile: _ApiDryRunTransport())`, posts `/openevo-api/desktop/bootstrap`, and asserts:

```python
payload["bootstrap"]["ready"] is True
payload["status"]["bootstrap"]["ready"] is True
payload["status"]["bootstrap"]["readiness_notes"] == ["Remote bootstrap is ready."]
payload["report"]["prepared_paths"]["bootstrap_manifest"].endswith("/bootstrap.json")
```

- [ ] **Step 2: Verify RED**

Run:

```bash
PYTHONPATH=src /home/ziyi/ProRL-Agent-Server/.venv/bin/python -m pytest tests/openevo/sidecar/test_api.py -q
```

Expected: FAIL because bootstrap app/session helpers do not exist.

- [ ] **Step 3: Implement session and endpoint**

In `src/openevo/sidecar/api.py`:

- import `HTTPException`, `build_remote_bootstrap_plan`, `execute_remote_bootstrap_plan`, `RemoteBootstrapReport`, and `RemoteExecutorTransport`;
- add `OpenEvoDesktopBootstrapResponse` with `bootstrap`, `report`, and `status`;
- add `OpenEvoSidecarSession` with fields `project`, `profile`, `transport_factory`, `status`, `last_bootstrap_report`;
- add `create_sidecar_app_for_project(project, profile, transport_factory)`;
- update `create_sidecar_app(status=None, session=None)`;
- add `POST /openevo-api/desktop/bootstrap`;
- for no session, raise 409 with the exact detail above;
- for session, build sidecar plan, build remote bootstrap plan, execute report, update `session.status` using a helper that sets bootstrap ready and notes from `report.next_actions`.

- [ ] **Step 4: Verify GREEN**

Run:

```bash
PYTHONPATH=src /home/ziyi/ProRL-Agent-Server/.venv/bin/python -m pytest tests/openevo/sidecar/test_api.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/openevo/sidecar/api.py tests/openevo/sidecar/test_api.py
git commit -m "feat: add openevo sidecar bootstrap endpoint"
```

## Task 2: CLI Serve Transport Selection

**Files:**
- Modify: `tests/openevo/test_cli.py`
- Modify: `src/openevo/cli.py`

- [ ] **Step 1: Add failing CLI tests**

Add tests that:

- `openevo sidecar serve --config science.yaml --remote-profile remote.yaml --transport dry-run` creates an app through `create_sidecar_app_for_project`;
- `--transport ssh` passes a factory that builds `SshRemoteExecutorTransport(profile)` without constructing it during serve startup;
- `--transport ssh` without config paths is rejected because fixture sessions cannot perform bootstrap.

- [ ] **Step 2: Verify RED**

Run:

```bash
PYTHONPATH=src /home/ziyi/ProRL-Agent-Server/.venv/bin/python -m pytest tests/openevo/test_cli.py -q
```

Expected: FAIL because serve does not accept `--transport` and does not call `create_sidecar_app_for_project`.

- [ ] **Step 3: Implement CLI transport**

In `src/openevo/cli.py`:

- import `create_sidecar_app_for_project`;
- add `serve --transport` choices `dry-run|ssh`, default `dry-run`;
- if no config/profile and transport is `ssh`, raise a clear `ValueError`;
- if config/profile are present, pass a factory to `create_sidecar_app_for_project`;
- reuse `_CliDryRunTransport` for dry-run and `SshRemoteExecutorTransport(profile)` for SSH.

- [ ] **Step 4: Verify GREEN**

Run:

```bash
PYTHONPATH=src /home/ziyi/ProRL-Agent-Server/.venv/bin/python -m pytest tests/openevo/test_cli.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/openevo/cli.py tests/openevo/test_cli.py
git commit -m "feat: wire sidecar serve bootstrap transport"
```

## Task 3: Web Bootstrap Action

**Files:**
- Modify: `web/src/api/openevo.ts`
- Modify: `web/src/api/openevo.test.ts`
- Modify: `web/src/routes/OpenEvoDesktop.tsx`
- Modify: `web/src/routes/OpenEvoDesktop.test.tsx`

- [ ] **Step 1: Add failing web tests**

Add a mapper test for `toOpenEvoBootstrapResponse()` that maps `status` to `OpenEvoDesktopShellModel` and exposes `bootstrap.ready`.

- [ ] **Step 2: Verify RED**

Run:

```bash
cd web
npm test -- --run src/api/openevo.test.ts
```

Expected: FAIL because bootstrap client helpers do not exist.

- [ ] **Step 3: Implement web client and route action**

In `web/src/api/openevo.ts`:

- add `OpenEvoBootstrapResponsePayload`;
- add `runOpenEvoBootstrap()` posting `/openevo-api/desktop/bootstrap`;
- add `toOpenEvoBootstrapResponse()`.

In `OpenEvoDesktop.tsx`:

- track `sidecarConnected`, `bootstrapRunning`, `bootstrapError`;
- mark `sidecarConnected=true` after shell fetch success;
- make the Bootstrap button enabled only when `sidecarConnected && !bootstrapRunning`;
- on click, call `runOpenEvoBootstrap()`, update model with response status, show errors in the Bootstrap panel;
- keep Sync Workspace and Start Run disabled.

- [ ] **Step 4: Verify GREEN**

Run:

```bash
cd web
npm test -- --run
npm run build
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add web/src/api/openevo.ts web/src/api/openevo.test.ts web/src/routes/OpenEvoDesktop.tsx web/src/routes/OpenEvoDesktop.test.tsx
git commit -m "feat: run openevo bootstrap from desktop shell"
```

## Task 4: Docs, Final Verification, Review, PR

**Files:**
- Modify: `docs/architecture/openevo-desktop-science-foundation.md`

- [ ] **Step 1: Document bootstrap endpoint**

Add that `POST /openevo-api/desktop/bootstrap` triggers the existing bootstrap executor for config-backed sessions, supports dry-run by default and SSH when selected at `serve` startup, returns the bootstrap report and refreshed shell status, and does not start long-running services.

- [ ] **Step 2: Final verification**

Run:

```bash
PYTHONPATH=src /home/ziyi/ProRL-Agent-Server/.venv/bin/python -m pytest tests/openevo/test_cli.py tests/openevo/sidecar tests/openevo/remote tests/openevo/science tests/evolution/test_models.py -q
/home/ziyi/ProRL-Agent-Server/.venv/bin/ruff check src/openevo/sidecar src/openevo/cli.py tests/openevo/sidecar tests/openevo/test_cli.py
(cd web && npm test -- --run)
(cd web && npm run build)
(cd web && npm audit --omit=dev --audit-level=low)
git diff --check openevo/stable...HEAD
```

- [ ] **Step 3: Commit docs**

```bash
git add docs/architecture/openevo-desktop-science-foundation.md
git commit -m "docs: document openevo sidecar bootstrap api"
```

- [ ] **Step 4: Review and merge**

Request gpt-5.5 high-effort review, push branch, open PR with `Fixes #53`, wait for checks, then squash merge if clean.

## Self-Review

- Spec coverage: covers sidecar endpoint, CLI transport selection, web action, docs, verification, review, and PR.
- Placeholder scan: no placeholder markers.
- Scope check: no long-running service startup, no direct model APIs, no credential vault.
