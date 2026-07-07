# OpenEvo Desktop Run Supervisor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a minimal Desktop-side run supervisor so OpenEvo Desktop starts remote `openevo run` work in the background and polls typed run status/log snapshots.

**Architecture:** Keep the supervisor in the local sidecar process for this slice. `POST /openevo-api/desktop/run` validates readiness, creates a run id, records a running status, starts a daemon thread that executes the existing remote command through `RemoteExecutorTransport`, and returns immediately. `GET /openevo-api/desktop/run` returns the latest run status plus shell status so the Web UI can poll until a terminal state.

**Tech Stack:** Python 3.11, FastAPI, Pydantic v2, threading, pytest, TypeScript, React, Vitest.

Tracked by #61.

---

## File Map

- Modify `src/openevo/sidecar/api.py`: run supervisor models, background thread launch, latest-run polling endpoint, status transitions.
- Modify `tests/openevo/sidecar/test_api.py`: async run launch, polling, terminal success/failure, concurrent run rejection, OpenAPI schema tests.
- Modify `web/src/api/openevo.ts`: typed run state payloads, start-run and poll-run client helpers.
- Modify `web/src/api/openevo.test.ts`: payload conversion and mutation-token tests for run polling.
- Modify `web/src/routes/OpenEvoDesktop.tsx`: start run then poll latest run; render latest run state/stdout/stderr summary.
- Modify `web/src/routes/OpenEvoDesktop.test.tsx`: UI start/poll behavior and terminal failure rendering.
- Modify `docs/architecture/openevo-desktop-science-foundation.md`: document the sidecar run supervisor boundary and unsupported service lifecycle features.

## Task 1: Backend Run Supervisor Contract

**Files:**
- Modify: `tests/openevo/sidecar/test_api.py`
- Modify: `src/openevo/sidecar/api.py`

- [ ] **Step 1: Write failing tests for async launch and polling**

Add or replace the run tests with these expectations:

```python
def test_run_endpoint_starts_background_run_and_poll_returns_running() -> None:
    project = ScienceProjectConfig.model_validate(_science_project_payload())
    profile = _remote_profile()
    transport = _BlockingRunTransport()
    client = TestClient(
        create_sidecar_app_for_project(
            project,
            profile,
            transport_factory=lambda _profile: transport,
        )
    )
    token = _sidecar_token(client)
    headers = {"X-OpenEvo-Sidecar-Token": token}
    bootstrap = _prepare_workspace_and_bootstrap(client, headers)

    response = client.post("/openevo-api/desktop/run", headers=headers)

    assert response.status_code == 200
    payload = response.json()
    assert payload["run"]["state"] == "running"
    assert payload["run"]["ready"] is False
    assert payload["run"]["finished_at"] is None
    assert payload["run"]["id"].startswith("run_")
    state_root = bootstrap["report"]["prepared_paths"]["state_root"]
    experiment_snapshot = bootstrap["report"]["prepared_paths"]["experiment_snapshot"]
    assert payload["run"]["output_dir"] == f"{state_root}/runs/{payload['run']['id']}"
    assert payload["run"]["experiment_snapshot"] == experiment_snapshot
    assert transport.run_started.wait(timeout=5)

    poll = client.get("/openevo-api/desktop/run", headers=headers)

    assert poll.status_code == 200
    assert poll.json()["run"]["id"] == payload["run"]["id"]
    assert poll.json()["run"]["state"] == "running"
    services = {service["id"]: service for service in poll.json()["status"]["services"]}
    assert services["openevo-backend"]["state"] == "running"

    transport.run_release.set()
    assert _wait_latest_run_state(client, headers, "succeeded")["run"]["stdout"] == "ok"
```

Add a failure test:

```python
def test_run_poll_returns_failed_terminal_status() -> None:
    project = ScienceProjectConfig.model_validate(_science_project_payload())
    profile = _remote_profile()
    transport = _FailingRunTransport()
    client = TestClient(
        create_sidecar_app_for_project(
            project,
            profile,
            transport_factory=lambda _profile: transport,
        )
    )
    token = _sidecar_token(client)
    headers = {"X-OpenEvo-Sidecar-Token": token}
    _prepare_workspace_and_bootstrap(client, headers)

    response = client.post("/openevo-api/desktop/run", headers=headers)
    payload = _wait_latest_run_state(client, headers, "failed")

    assert response.status_code == 200
    assert payload["run"]["ready"] is False
    assert payload["run"]["return_code"] == 2
    assert payload["run"]["stderr"] == "run failed"
    services = {service["id"]: service for service in payload["status"]["services"]}
    assert services["openevo-backend"]["state"] == "blocked"
```

Add helper:

```python
def _wait_latest_run_state(
    client: TestClient,
    headers: dict[str, str],
    expected_state: str,
) -> dict:
    for _ in range(50):
        response = client.get("/openevo-api/desktop/run", headers=headers)
        assert response.status_code == 200
        payload = response.json()
        if payload["run"]["state"] == expected_state:
            return payload
        time.sleep(0.05)
    raise AssertionError(f"latest run did not reach {expected_state}")
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
PYTHONPATH=src /home/ziyi/ProRL-Agent-Server/.venv/bin/python -m pytest tests/openevo/sidecar/test_api.py -k "run_endpoint or run_poll or run_response_schema" -q
```

Expected: FAIL because run payloads do not include `id`, `state`, or `finished_at`, and `GET /openevo-api/desktop/run` does not exist.

- [ ] **Step 3: Implement minimal backend contract**

In `src/openevo/sidecar/api.py`:

- import `Thread` from `threading`;
- add `latest_run: OpenEvoDesktopRunStatus | None = None` to `OpenEvoSidecarSession`;
- replace `OpenEvoDesktopRunReport` with:

```python
class OpenEvoDesktopRunStatus(_StrictFrozenModel):
    id: str
    state: Literal["running", "succeeded", "failed"]
    ready: bool
    command: str
    return_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    output_dir: str
    experiment_snapshot: str
    started_at: str
    finished_at: str | None = None

    @model_validator(mode="after")
    def _validate_ready_state(self) -> OpenEvoDesktopRunStatus:
        if self.ready != (self.state == "succeeded"):
            raise ValueError("ready must be true only for succeeded runs")
        if self.state == "running" and self.finished_at is not None:
            raise ValueError("running runs must not have finished_at")
        if self.state != "running" and self.finished_at is None:
            raise ValueError("terminal runs must have finished_at")
        return self
```

Update `OpenEvoDesktopRunResponse` to use `OpenEvoDesktopRunStatus`.

Make `POST /desktop/run` acquire `run_lock`, build an initial running status, store it, mark services/evolution running, and spawn a daemon thread:

```python
run_status = _initial_run_status(
    experiment_snapshot=experiment_snapshot,
    state_root=state_root,
)
with active_session.status_lock:
    active_session.latest_run = run_status
    active_session.status = _status_after_run(active_session.status, run_status)
    response_status = _status_with_mutation_token(active_session.status, sidecar_token)
Thread(
    target=_finish_openevo_task_run,
    args=(active_session, run_status, experiment_snapshot, state_root, sidecar_token),
    daemon=True,
).start()
return OpenEvoDesktopRunResponse(run=run_status, status=response_status)
```

Add polling endpoint:

```python
@app.get("/openevo-api/desktop/run", response_model=OpenEvoDesktopRunResponse)
def latest_run(token: str | None = Header(default=None, alias=SIDECAR_MUTATION_TOKEN_HEADER)):
    _validate_mutation_token(token, sidecar_token)
    active_session = current_session()
    if active_session is None:
        raise HTTPException(status_code=409, detail="Desktop run status requires a config-backed sidecar session.")
    with active_session.status_lock:
        if active_session.latest_run is None:
            raise HTTPException(status_code=404, detail="No Desktop run has been launched.")
        return OpenEvoDesktopRunResponse(
            run=active_session.latest_run,
            status=_status_with_mutation_token(active_session.status, sidecar_token),
        )
```

Use a per-run output directory:

```python
def _initial_run_status(*, experiment_snapshot: str, state_root: str) -> OpenEvoDesktopRunStatus:
    run_id = "run_" + datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
    output_dir = posixpath.join(state_root, "runs", run_id)
    command = (
        f"openevo run {shlex.quote(experiment_snapshot)} "
        f"--output-dir {shlex.quote(output_dir)} --json"
    )
    return OpenEvoDesktopRunStatus(
        id=run_id,
        state="running",
        ready=False,
        command=command,
        output_dir=output_dir,
        experiment_snapshot=experiment_snapshot,
        started_at=datetime.now(timezone.utc).isoformat(),
    )
```

Make the background worker always release `run_lock`:

```python
def _finish_openevo_task_run(
    session: OpenEvoSidecarSession,
    started: OpenEvoDesktopRunStatus,
    experiment_snapshot: str,
    state_root: str,
    sidecar_token: str,
) -> None:
    try:
        finished = _run_openevo_task(session, started=started, state_root=state_root)
        with session.status_lock:
            if session.latest_run is not None and session.latest_run.id == started.id:
                session.latest_run = finished
                session.status = _status_after_run(session.status, finished)
    finally:
        session.run_lock.release()
```

- [ ] **Step 4: Run backend tests**

Run:

```bash
PYTHONPATH=src /home/ziyi/ProRL-Agent-Server/.venv/bin/python -m pytest tests/openevo/sidecar/test_api.py -k "run_endpoint or run_poll or run_response_schema" -q
/home/ziyi/ProRL-Agent-Server/.venv/bin/ruff check src/openevo/sidecar tests/openevo/sidecar/test_api.py
```

Expected: PASS.

- [ ] **Step 5: Commit backend supervisor contract**

```bash
git add src/openevo/sidecar/api.py tests/openevo/sidecar/test_api.py
GIT_AUTHOR_NAME='ivowang' GIT_AUTHOR_EMAIL='ziyiwang@ieee.org' GIT_COMMITTER_NAME='ivowang' GIT_COMMITTER_EMAIL='ziyiwang@ieee.org' git commit -m "feat: add desktop run supervisor contract"
```

## Task 2: Web Polling and Run Report Rendering

**Files:**
- Modify: `web/src/api/openevo.ts`
- Modify: `web/src/api/openevo.test.ts`
- Modify: `web/src/routes/OpenEvoDesktop.tsx`
- Modify: `web/src/routes/OpenEvoDesktop.test.tsx`

- [ ] **Step 1: Write failing Web tests**

In `web/src/api/openevo.test.ts`, update run payload tests to expect:

```ts
expect(result.run.state).toBe("running");
expect(result.run.id).toBe("run_20260707170000000000");
expect(result.run.finishedAt).toBeNull();
```

In `web/src/routes/OpenEvoDesktop.test.tsx`, mock `POST /desktop/run` to return running and `GET /desktop/run` to return succeeded on the next poll. Assert Start Run shows running state and then a terminal report:

```ts
expect(await screen.findByText("running")).toBeInTheDocument();
expect(await screen.findByText("succeeded")).toBeInTheDocument();
expect(screen.getByText("/remote/runs/run_20260707170000000000")).toBeInTheDocument();
```

- [ ] **Step 2: Run Web tests to verify failure**

Run:

```bash
cd web && npm test -- --run src/api/openevo.test.ts src/routes/OpenEvoDesktop.test.tsx
```

Expected: FAIL because `pollOpenEvoRunStatus` and run state rendering do not exist.

- [ ] **Step 3: Implement TypeScript API mapping**

Change `OpenEvoRunReport` to:

```ts
export interface OpenEvoRunStatus {
  id: string;
  state: "running" | "succeeded" | "failed";
  ready: boolean;
  command: string;
  returnCode: number | null;
  stdout: string;
  stderr: string;
  outputDir: string;
  experimentSnapshot: string;
  startedAt: string;
  finishedAt: string | null;
}
```

Add payload shape with snake_case fields and a `toOpenEvoRunStatus()` mapper. Add:

```ts
export async function pollOpenEvoRunStatus(): Promise<OpenEvoRunResponse> {
  const headers = sidecarMutationToken
    ? { [sidecarMutationTokenHeader]: sidecarMutationToken }
    : undefined;
  const payload = await api.get<OpenEvoRunResponsePayload>(
    "/openevo-api/desktop/run",
    headers,
  );
  rememberOpenEvoSidecarMutationToken(payload.status);
  return {
    run: toOpenEvoRunStatus(payload.run),
    status: toOpenEvoDesktopShellModel(payload.status),
  };
}
```

- [ ] **Step 4: Implement React polling**

In `OpenEvoDesktop.tsx`:

- store `latestRun` in state;
- after `runOpenEvoStartRun()`, set `latestRun` and start polling every 1000 ms while state is `running`;
- call `pollOpenEvoRunStatus()` and stop polling when state is `succeeded` or `failed`;
- render a compact `Run Status` panel with state, output dir, return code, stdout and stderr snapshots.

Use existing buttons and panels. Do not add marketing text or a new landing page.

- [ ] **Step 5: Run Web tests**

Run:

```bash
cd web && npm test -- --run src/api/openevo.test.ts src/routes/OpenEvoDesktop.test.tsx
cd web && npm run build
```

Expected: PASS.

- [ ] **Step 6: Commit Web polling**

```bash
git add web/src/api/openevo.ts web/src/api/openevo.test.ts web/src/routes/OpenEvoDesktop.tsx web/src/routes/OpenEvoDesktop.test.tsx
GIT_AUTHOR_NAME='ivowang' GIT_AUTHOR_EMAIL='ziyiwang@ieee.org' GIT_COMMITTER_NAME='ivowang' GIT_COMMITTER_EMAIL='ziyiwang@ieee.org' git commit -m "feat: add desktop run polling UI"
```

## Task 3: Docs, Review, and Publish

**Files:**
- Modify: `docs/architecture/openevo-desktop-science-foundation.md`
- Add/modify: `docs/superpowers/plans/2026-07-07-openevo-desktop-run-supervisor.md`

- [ ] **Step 1: Update docs**

Replace the old limitation paragraph that says `/desktop/run` is synchronous with:

```markdown
`POST /openevo-api/desktop/run` is now a sidecar-supervised background launch.
It validates workspace/bootstrap readiness, records a run id, starts the
remote `openevo run` command on a sidecar daemon thread, and returns a running
status immediately. Desktop polls `GET /openevo-api/desktop/run` with the
sidecar mutation token to recover the latest run state, stdout/stderr snapshots,
return code, timestamps, output directory, and refreshed shell status.

This supervisor owns only the local sidecar job table for one latest run per
config-backed session. It does not survive sidecar process restarts, stream
incremental remote log files, cancel remote process groups, start vLLM, start
Polar gateway, run Docker Compose, manage adapters, or supervise evolution
workers as independent daemons.
```

- [ ] **Step 2: Run full focused verification**

Run:

```bash
PYTHONPATH=src /home/ziyi/ProRL-Agent-Server/.venv/bin/python -m pytest tests/openevo/test_cli.py tests/openevo/sidecar tests/openevo/remote tests/openevo/science tests/evolution/test_models.py -q
/home/ziyi/ProRL-Agent-Server/.venv/bin/ruff check src/openevo/sidecar src/openevo/cli.py tests/openevo/sidecar tests/openevo/test_cli.py
cd web && npm test -- --run
cd web && npm run build
cd web && npm audit --omit=dev --audit-level=low
git diff --check openevo/stable...HEAD
```

Expected: PASS / 0 vulnerabilities / no diff-check output.

- [ ] **Step 3: Request gpt-5.5 high effort review**

Ask a subagent to review:

```text
Review branch codex/openevo-desktop-run-supervisor for issue #61. Focus on sidecar thread safety, run_lock release, stale latest-run status, mutation-token protection, Web polling cleanup, and tests. Do not modify files. Report blocking/important findings only.
```

- [ ] **Step 4: Commit docs**

```bash
git add docs/architecture/openevo-desktop-science-foundation.md docs/superpowers/plans/2026-07-07-openevo-desktop-run-supervisor.md
GIT_AUTHOR_NAME='ivowang' GIT_AUTHOR_EMAIL='ziyiwang@ieee.org' GIT_COMMITTER_NAME='ivowang' GIT_COMMITTER_EMAIL='ziyiwang@ieee.org' git commit -m "docs: document desktop run supervisor"
```

- [ ] **Step 5: Push, PR, checks, merge**

```bash
git push -u openevo codex/openevo-desktop-run-supervisor
gh pr create --repo CompLifeLab-ZJU/OpenEvo --base stable --head codex/openevo-desktop-run-supervisor --title "Add OpenEvo Desktop run supervisor" --body-file <body-file>
gh pr view <pr> --repo CompLifeLab-ZJU/OpenEvo --json mergeStateStatus,statusCheckRollup
gh pr merge <pr> --repo CompLifeLab-ZJU/OpenEvo --squash --delete-branch
git fetch openevo --prune
```

PR body must include `Fixes #61`, docs paths, tests run, and review result.

## Self-Review

- Spec coverage: #61 acceptance criteria map to Task 1 backend run id/status/polling, Task 2 UI polling/rendering, and Task 3 docs/verification/review/publish.
- Placeholder scan: no `TBD`, no unspecified "add tests", no omitted function names.
- Type consistency: backend `OpenEvoDesktopRunStatus` maps to frontend `OpenEvoRunStatus`; states are exactly `running | succeeded | failed`; timestamp fields are `started_at/finished_at` in JSON and `startedAt/finishedAt` in TypeScript.
