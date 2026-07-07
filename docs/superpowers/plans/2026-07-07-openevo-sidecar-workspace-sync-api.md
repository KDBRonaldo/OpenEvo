# OpenEvo Sidecar Workspace Sync API Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let OpenEvo Desktop prepare configured Science task workspaces through the local sidecar, without requiring ordinary users to manually upload folders or clone repositories on the remote server.

**Architecture:** Add a protected `POST /openevo-api/desktop/workspace` endpoint beside the existing bootstrap endpoint. It reuses `build_sidecar_science_plan()` and `execute_sidecar_plan()` so workspace execution stays on the established remote executor contract. The web shell stores the sidecar mutation token from `/desktop/shell`, sends it on workspace sync, and refreshes visible service status from the response.

**Tech Stack:** Python 3.11+, FastAPI, Pydantic v2, existing `openevo.sidecar` and `openevo.remote.executor` contracts, React 19, Vite, Vitest, happy-dom.

---

### Task 1: Sidecar Workspace Endpoint

**Files:**
- Modify: `src/openevo/sidecar/api.py`
- Modify: `tests/openevo/sidecar/test_api.py`

- [ ] **Step 1: Write failing API tests**

Add tests to `tests/openevo/sidecar/test_api.py`:

```python
def test_workspace_endpoint_requires_config_backed_session() -> None:
    client = TestClient(create_sidecar_app())
    token = _sidecar_token(client)

    response = client.post(
        "/openevo-api/desktop/workspace",
        headers={"X-OpenEvo-Sidecar-Token": token},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == (
        "Desktop workspace sync requires a config-backed sidecar session."
    )


def test_workspace_endpoint_rejects_missing_sidecar_token() -> None:
    project = ScienceProjectConfig.model_validate(_science_project_payload())
    profile = _remote_profile()
    client = TestClient(
        create_sidecar_app_for_project(
            project,
            profile,
            transport_factory=lambda _profile: _ApiDryRunTransport(),
        )
    )

    response = client.post("/openevo-api/desktop/workspace")

    assert response.status_code == 403
    assert response.json()["detail"] == "Invalid OpenEvo sidecar token."


def test_workspace_endpoint_marks_remote_path_ready() -> None:
    project = ScienceProjectConfig.model_validate(_science_project_payload())
    profile = _remote_profile()
    client = TestClient(
        create_sidecar_app_for_project(
            project,
            profile,
            transport_factory=lambda _profile: _ApiDryRunTransport(),
        )
    )
    token = _sidecar_token(client)

    response = client.post(
        "/openevo-api/desktop/workspace",
        headers={"X-OpenEvo-Sidecar-Token": token},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["workspace"]["ready"] is True
    assert payload["status"]["services"][1]["state"] == "ready"
    assert payload["status"]["services"][1]["detail"] == (
        "Workspace source is already remote"
    )
```

Add a local-folder success test:

```python
def test_workspace_endpoint_uploads_local_folder_and_refreshes_status(
    tmp_path: Path,
) -> None:
    local_source = tmp_path / "workflow"
    local_source.mkdir()
    project = ScienceProjectConfig.model_validate(
        _science_project_payload()
        | {
            "task": {
                "id": "local-workflow",
                "objective": "Run the local workflow.",
                "source": {"type": "local_folder", "path": str(local_source)},
            }
        }
    )
    profile = _remote_profile()
    transport = _ApiDryRunTransport()
    client = TestClient(
        create_sidecar_app_for_project(
            project,
            profile,
            transport_factory=lambda _profile: transport,
        )
    )
    token = _sidecar_token(client)

    response = client.post(
        "/openevo-api/desktop/workspace",
        headers={"X-OpenEvo-Sidecar-Token": token},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["workspace"]["ready"] is True
    assert transport.uploads[0][0] == str(local_source)
    assert payload["status"]["services"][1]["state"] == "ready"
    assert payload["status"]["services"][1]["detail"] == "Workspace prepared"
```

Add failure and concurrency tests:

```python
def test_workspace_endpoint_preserves_upload_failure_status(
    tmp_path: Path,
) -> None:
    local_source = tmp_path / "workflow"
    local_source.mkdir()
    project = ScienceProjectConfig.model_validate(
        _science_project_payload()
        | {
            "task": {
                "id": "local-workflow",
                "objective": "Run the local workflow.",
                "source": {"type": "local_folder", "path": str(local_source)},
            }
        }
    )
    profile = _remote_profile()
    client = TestClient(
        create_sidecar_app_for_project(
            project,
            profile,
            transport_factory=lambda _profile: _FailingUploadTransport(),
        )
    )
    token = _sidecar_token(client)

    response = client.post(
        "/openevo-api/desktop/workspace",
        headers={"X-OpenEvo-Sidecar-Token": token},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["workspace"]["ready"] is False
    assert payload["status"]["services"][1]["state"] == "blocked"
    assert payload["status"]["services"][1]["detail"] == "upload failed"


def test_workspace_endpoint_rejects_concurrent_runs(tmp_path: Path) -> None:
    local_source = tmp_path / "workflow"
    local_source.mkdir()
    project = ScienceProjectConfig.model_validate(
        _science_project_payload()
        | {
            "task": {
                "id": "local-workflow",
                "objective": "Run the local workflow.",
                "source": {"type": "local_folder", "path": str(local_source)},
            }
        }
    )
    profile = _remote_profile()
    transport = _BlockingWorkspaceTransport()
    client = TestClient(
        create_sidecar_app_for_project(
            project,
            profile,
            transport_factory=lambda _profile: transport,
        )
    )
    token = _sidecar_token(client)
    headers = {"X-OpenEvo-Sidecar-Token": token}

    with ThreadPoolExecutor(max_workers=1) as executor:
        first = executor.submit(
            client.post,
            "/openevo-api/desktop/workspace",
            headers=headers,
        )
        assert transport.started.wait(timeout=5)
        second = client.post("/openevo-api/desktop/workspace", headers=headers)
        transport.release.set()

    assert second.status_code == 409
    assert second.json()["detail"] == "Desktop workspace sync is already running."
    assert first.result(timeout=5).status_code == 200
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
PYTHONPATH=src /home/ziyi/ProRL-Agent-Server/.venv/bin/python -m pytest tests/openevo/sidecar/test_api.py -q
```

Expected: FAIL because `/openevo-api/desktop/workspace` does not exist.

- [ ] **Step 3: Implement the endpoint**

In `src/openevo/sidecar/api.py`:

- Add `workspace_lock` to `OpenEvoSidecarSession`.
- Add `OpenEvoDesktopWorkspaceResponse`.
- Add `POST /openevo-api/desktop/workspace`.
- Use `_validate_mutation_token()` before any work.
- Use `execute_sidecar_plan()` to run workspace preparation.
- Add `_status_after_workspace()` to update only the workspace service.

Implementation outline:

```python
class OpenEvoDesktopWorkspaceResponse(_StrictFrozenModel):
    workspace: dict[str, Any]
    status: OpenEvoDesktopShellStatus


@app.post(
    "/openevo-api/desktop/workspace",
    response_model=OpenEvoDesktopWorkspaceResponse,
)
def workspace(
    token: str | None = Header(
        default=None,
        alias=SIDECAR_MUTATION_TOKEN_HEADER,
    ),
) -> OpenEvoDesktopWorkspaceResponse:
    _validate_mutation_token(token, sidecar_token)
    if session is None:
        raise HTTPException(
            status_code=409,
            detail="Desktop workspace sync requires a config-backed sidecar session.",
        )
    if not session.workspace_lock.acquire(blocking=False):
        raise HTTPException(
            status_code=409,
            detail="Desktop workspace sync is already running.",
        )
    try:
        report = _run_workspace_sync(session)
        session.status = _status_after_workspace(session.status, report)
    finally:
        session.workspace_lock.release()
    response_status = _status_with_mutation_token(session.status, sidecar_token)
    return OpenEvoDesktopWorkspaceResponse(
        workspace=report.workspace.model_dump(mode="json"),
        status=response_status,
    )
```

- [ ] **Step 4: Run tests to verify pass**

Run:

```bash
PYTHONPATH=src /home/ziyi/ProRL-Agent-Server/.venv/bin/python -m pytest tests/openevo/sidecar/test_api.py -q
/home/ziyi/ProRL-Agent-Server/.venv/bin/ruff check src/openevo/sidecar/api.py tests/openevo/sidecar/test_api.py
```

Expected: all tests pass and ruff reports `All checks passed!`.

- [ ] **Step 5: Commit**

```bash
git add src/openevo/sidecar/api.py tests/openevo/sidecar/test_api.py
GIT_AUTHOR_NAME='ivowang' GIT_AUTHOR_EMAIL='ziyiwang@ieee.org' GIT_COMMITTER_NAME='ivowang' GIT_COMMITTER_EMAIL='ziyiwang@ieee.org' git commit -m "feat: add openevo sidecar workspace endpoint"
```

### Task 2: Web Sync Workspace Action

**Files:**
- Modify: `web/src/api/openevo.ts`
- Modify: `web/src/api/openevo.test.ts`
- Modify: `web/src/routes/OpenEvoDesktop.tsx`
- Modify: `web/src/routes/OpenEvoDesktop.test.tsx`

- [ ] **Step 1: Write failing web tests**

Add `runOpenEvoWorkspaceSync()` mapper/header coverage to `web/src/api/openevo.test.ts` and route tests that click `Sync Workspace`, assert pending text, success status refresh, and error rendering.

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
cd web && npm test -- --run
```

Expected: FAIL because `runOpenEvoWorkspaceSync()` and button behavior are missing.

- [ ] **Step 3: Implement web client and route**

In `web/src/api/openevo.ts`, add:

```ts
export interface OpenEvoWorkspaceResponsePayload {
  workspace: Record<string, any>;
  status: OpenEvoDesktopShellStatusPayload;
}

export interface OpenEvoWorkspaceResponse {
  workspace: Record<string, any>;
  status: OpenEvoDesktopShellModel;
}

export async function runOpenEvoWorkspaceSync(): Promise<OpenEvoWorkspaceResponse> {
  const headers = sidecarMutationToken
    ? { [sidecarMutationTokenHeader]: sidecarMutationToken }
    : undefined;
  const payload = await api.post<OpenEvoWorkspaceResponsePayload>(
    "/openevo-api/desktop/workspace",
    {},
    headers,
  );
  rememberOpenEvoSidecarMutationToken(payload.status);
  return {
    workspace: payload.workspace,
    status: toOpenEvoDesktopShellModel(payload.status),
  };
}
```

In `OpenEvoDesktop.tsx`, import `runOpenEvoWorkspaceSync`, add `workspaceRunning` and `workspaceError`, enable the Sync Workspace button when the sidecar is connected, and update the model from `response.status`.

- [ ] **Step 4: Run web tests/build**

Run:

```bash
cd web && npm test -- --run
cd web && npm run build
cd web && npm audit --omit=dev --audit-level=low
```

Expected: tests and build pass; audit reports 0 production vulnerabilities.

- [ ] **Step 5: Commit**

```bash
git add web/src/api/openevo.ts web/src/api/openevo.test.ts web/src/routes/OpenEvoDesktop.tsx web/src/routes/OpenEvoDesktop.test.tsx
GIT_AUTHOR_NAME='ivowang' GIT_AUTHOR_EMAIL='ziyiwang@ieee.org' GIT_COMMITTER_NAME='ivowang' GIT_COMMITTER_EMAIL='ziyiwang@ieee.org' git commit -m "feat: wire openevo workspace sync action"
```

### Task 3: Documentation, Verification, Review, PR

**Files:**
- Modify: `docs/architecture/openevo-desktop-science-foundation.md`

- [ ] **Step 1: Document workspace sync**

Update the Desktop Web Shell section with:

- `POST /openevo-api/desktop/workspace`.
- Same mutation token requirement as bootstrap.
- Dry-run vs SSH behavior.
- Workspace readiness stays independent from bootstrap/backend readiness.

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

Expected: all pass.

- [ ] **Step 3: HTTP smoke**

Start sidecar serve on a temp Science Project and remote profile, read `sidecar.mutation_token` from `/desktop/shell`, POST `/desktop/workspace`, and assert workspace ready in the returned status and subsequent shell status.

- [ ] **Step 4: Commit docs**

```bash
git add docs/architecture/openevo-desktop-science-foundation.md
GIT_AUTHOR_NAME='ivowang' GIT_AUTHOR_EMAIL='ziyiwang@ieee.org' GIT_COMMITTER_NAME='ivowang' GIT_COMMITTER_EMAIL='ziyiwang@ieee.org' git commit -m "docs: document openevo workspace sync api"
```

- [ ] **Step 5: Request gpt-5.5 high-effort review**

Dispatch a reviewer for `openevo/stable...HEAD`, including Issue #55, verification output, and smoke output. Fix Critical/Important findings before PR.

- [ ] **Step 6: Push, open PR, wait CI, merge**

```bash
git push -u openevo codex/openevo-sidecar-workspace-sync
gh pr create --repo CompLifeLab-ZJU/OpenEvo --base stable --head codex/openevo-sidecar-workspace-sync --title "Add OpenEvo Desktop workspace sync API" --body-file /tmp/openevo-workspace-sync-pr.md
gh pr checks <PR> --repo CompLifeLab-ZJU/OpenEvo
gh pr merge <PR> --repo CompLifeLab-ZJU/OpenEvo --squash --delete-branch
git fetch openevo stable --prune
```

PR body must include `Fixes #55`, docs paths, tests, smoke, and review summary.

---

## Self-Review

- Spec coverage: Issue #55 acceptance criteria map to Tasks 1-3.
- Placeholder scan: no `TBD`, `TODO`, or intentionally vague implementation steps remain.
- Type consistency: response names use `OpenEvoDesktopWorkspaceResponse` on Python and `OpenEvoWorkspaceResponse` on TypeScript; token header reuses `X-OpenEvo-Sidecar-Token`.
