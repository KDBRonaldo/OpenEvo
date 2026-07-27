# Desktop v0.1.10 Sidecar Lifecycle Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace synchronous Desktop-owned remote lifecycle requests with durable, observable, cancellable operations that return promptly, replay exactly, and expose bounded SSH/Daemon process logs.

**Architecture:** The provider store atomically reserves an immutable request and a `LifecycleOperationV2`; a single-worker executor advances fixed monotonic phases and commits terminal authority. The renderer observes operations through authenticated Local API reads and SSE invalidations. Native workspace ingestion reserves through the hidden native-to-sidecar route but returns the same renderer-safe operation. Core remains authoritative for project/task data and all post-negotiation business mutations.

**Tech Stack:** Python 3.11, FastAPI, Pydantic v2, SQLite STRICT tables, system OpenSSH, existing Core v2 bridge, pytest.

---

## Contract decisions used by every task

- `LocalOperationV2` remains the projection for short/Core-owned operation acknowledgements. New Desktop-owned long work returns `LifecycleOperationV2`.
- Long Core operations use the exact renderer-safe `core.OperationV2` projection and remain observable only through the active project tunnel; they are never copied into the Desktop lifecycle store.
- Lifecycle kinds are exactly `profile_connect`, `profile_disconnect`, `host_key_review`, `native_workspace_prepare`, `project_create`, and `project_activate`.
- Resource references are a discriminated union: profile, native workspace action, or deterministic Desktop project ID. A request cannot change this reference on replay.
- Each kind publishes one fixed phase plan at reservation time. Stored phase indices are zero-based; the API exposes `phase_index` in the range `0..phase_total - 1`.
- `succeeded` requires a typed result. `failed` requires a typed `DesktopErrorV2`. `cancelled` has neither. Nonterminal rows have neither.
- Logs retain 4,096 entries and 4 MiB per operation, with a 32-MiB provider-wide lifecycle-log cap; an entry is at most 16 KiB UTF-8. Page size is at most 100.
- The store admits at most 16 recoverable operations (nonterminal plus unacknowledged terminal) and the executor has one external-work worker.
- Terminal operations, logs, and lifecycle idempotency rows are retained for seven days after reconciliation acknowledgement; unacknowledged authority is never evicted.

## Task 1: Add the strict lifecycle contract

**Files:**

- Modify: `desktop/sidecar/contracts/v2/models.py`
- Modify: `desktop/sidecar/contracts/v2/app.py`
- Modify: `tests/openevo/sidecar/test_desktop_contract_v2.py`
- Regenerate: `desktop/sidecar/contracts/v2/openapi.json`
- Regenerate: `desktop/sidecar/contracts/v2/events.schema.json`

- [ ] Add failing model tests that cover unknown fields, invalid status/result combinations, regressing or oversized progress values, invalid log pages, cross-wired operation/resource IDs, and a `DesktopStateV2` pending-operation reference whose identity is malformed.
- [ ] Add failing route tests asserting `POST /projects`, profile lifecycle routes, and project activation return HTTP 202 `LifecycleOperationV2`; assert the four observation/control/reconciliation routes exist and require the standard mutation headers on cancel/acknowledge. Also assert Core-operation lookup/cancel, service logs, and cache cleanup are separately namespaced renderer-safe tunnel routes.
- [ ] Add a failing event-schema test for `lifecycle_operation_changed` and prove that the payload contains no log body.
- [ ] Run the red tests:

```bash
uv run pytest -q \
  tests/openevo/sidecar/test_desktop_contract_v2.py \
  -k 'lifecycle or operation or mutation_routes'
```

Expected: failures because the new models, routes, and event discriminator do not exist.

- [ ] Add these closed model families to `models.py`:

```python
class LifecycleProgressIndeterminateV2(StrictModel):
    kind: Literal["indeterminate"]


class LifecycleProgressBytesV2(StrictModel):
    kind: Literal["bytes"]
    completed: int = Field(ge=0, le=MAX_JAVASCRIPT_SAFE_INTEGER)
    total: int = Field(ge=1, le=MAX_JAVASCRIPT_SAFE_INTEGER)

    @model_validator(mode="after")
    def _completed_not_past_total(self) -> LifecycleProgressBytesV2:
        if self.completed > self.total:
            raise ValueError("completed bytes exceed total")
        return self


class LifecycleProgressItemsV2(StrictModel):
    kind: Literal["items"]
    completed: int = Field(ge=0, le=MAX_JAVASCRIPT_SAFE_INTEGER)
    total: int = Field(ge=1, le=MAX_JAVASCRIPT_SAFE_INTEGER)

    @model_validator(mode="after")
    def _completed_not_past_total(self) -> LifecycleProgressItemsV2:
        if self.completed > self.total:
            raise ValueError("completed items exceed total")
        return self
```

- [ ] Add `LifecycleResourceRefV2`, typed profile/project/native-workspace results, `LifecycleOperationRefV2`, `LifecycleOperationV2`, `LifecycleLogEntryV2`, `LifecycleLogPageV2`, `LifecycleCancelV2`, and `LifecycleAcknowledgeV2`. Bind status, timestamps, result kind, resource identity, phase index, progress, log watermark, and strong ETag with validators.
- [ ] Add explicit projections `DesktopCoreOperationV2 = core.OperationV2`, `DesktopServiceLogPageV2 = core.LogPageV2`, and `DesktopCacheCleanupRequestV2 = core.CacheCleanupRequestV2`. Change Task cancel, transition retry/abandon, and service restart responses from lossy `LocalOperationV2` to `DesktopCoreOperationV2`.
- [ ] Use this exact closed phase vocabulary: `validation`, `queued`, `resolving_system_openssh`, `connecting`, `waiting_for_user`, `remote_preflight`, `transferring`, `verifying`, `starting_daemon`, `waiting_for_daemon`, `opening_project_tunnel`, `negotiating_core`, `preparing_native_workspace`, `creating_remote_project`, `verifying_project`, `activating`, `finalizing`.
- [ ] Add `pending_operations: list[LifecycleOperationRefV2]` to `DesktopStateV2` with a maximum length of 16 and unique operation IDs.
- [ ] Add `LifecycleOperationEventPayloadV2` to the event union and `lifecycle_operation_changed` to both event literals.
- [ ] Add the authenticated routes:

```python
@router.get(
    "/operations/{operation_id}",
    operation_id="getDesktopLifecycleOperationV2",
    response_model=m.LifecycleOperationV2,
)
async def lifecycle_operation(operation_id: ResourceId) -> Response:
    return _contract_only()


@router.get(
    "/operations/{operation_id}/logs",
    operation_id="getDesktopLifecycleOperationLogsV2",
    response_model=m.LifecycleLogPageV2,
)
async def lifecycle_operation_logs(
    operation_id: ResourceId,
    limit: Limit = 100,
    after: Cursor = None,
) -> Response:
    return _contract_only()


@router.post(
    "/operations/{operation_id}/cancel",
    operation_id="cancelDesktopLifecycleOperationV2",
    response_model=m.LifecycleOperationV2,
    status_code=202,
)
async def cancel_lifecycle_operation(
    operation_id: ResourceId,
    request: m.LifecycleCancelV2,
    resource_generation: ResourceGeneration,
    if_match: IfMatch,
    idempotency_key: IdempotencyKey,
) -> Response:
    return _contract_only()


@router.post(
    "/operations/{operation_id}/acknowledge",
    operation_id="acknowledgeDesktopLifecycleOperationV2",
    status_code=204,
)
async def acknowledge_lifecycle_operation(
    operation_id: ResourceId,
    request: m.LifecycleAcknowledgeV2,
    resource_generation: ResourceGeneration,
    if_match: IfMatch,
    idempotency_key: IdempotencyKey,
) -> Response:
    return _contract_only()
```

- [ ] Change only the six Desktop-owned long mutation response models/statuses. Do not change Task, transition, service, or diagnostic Core authority.
- [ ] Regenerate canonical snapshots:

```bash
uv run python -c 'from desktop.sidecar.contracts.v2.canonical import write_contract_snapshots; write_contract_snapshots()'
```

- [ ] Run the contract tests and confirm green:

```bash
uv run pytest -q tests/openevo/sidecar/test_desktop_contract_v2.py
```

- [ ] Commit and push:

```bash
git add desktop/sidecar/contracts/v2 tests/openevo/sidecar/test_desktop_contract_v2.py
git commit -m "feat(desktop): define lifecycle operation contract"
git push
```

## Task 2: Migrate the provider store to durable lifecycle authority

**Files:**

- Modify: `desktop/sidecar/provider_store_v2.py`
- Modify: `tests/openevo/sidecar/test_provider_store_v2.py`

- [ ] Add failing tests for schema-v2-to-v3 atomic migration, fresh schema fingerprint, interrupted migration retry, exact reservation replay, changed-request/key conflict, changed ETag/generation conflict, maximum 16 recoverable rows including unacknowledged terminals, exact replay while full, and preservation of all v0.1.9 profiles/drafts/idempotency records.
- [ ] Add failing tests for monotonic phase/progress/log sequence, terminal immutability, atomic terminal result plus replay, idempotent terminal acknowledgement stored outside the immutable operation document, seven-day post-acknowledgement cleanup, and refusal to clean a nonterminal or unacknowledged terminal row.
- [ ] Add failing log budget tests for 16 KiB entry truncation, 4,096-row/4-MiB per-operation eviction, 32-MiB global eviction preferring acknowledged terminals, per-operation `dropped_before_sequence`, invalid UTF-8, and SQL length guards that reject an oversized value before returning it to Python.
- [ ] Run the red store slice:

```bash
uv run pytest -q tests/openevo/sidecar/test_provider_store_v2.py -k 'lifecycle or schema_migration'
```

Expected: missing schema v3 and lifecycle store APIs.

- [ ] Set `SCHEMA_VERSION = 3`, preserve the exact v1/v2 fingerprints, and add fingerprinted STRICT tables `lifecycle_operations`, `lifecycle_operation_logs`, `lifecycle_idempotency_records`, `lifecycle_reconciliation_acknowledgements`, and a singleton 32-byte `lifecycle_cursor_key`. The operation row stores bounded canonical request/phase/result/failure/progress JSON and scalar columns needed to enforce monotonicity without trusting decoded JSON.
- [ ] Add the following public store API with exact Pydantic inputs/outputs:

  - `reserve_lifecycle_operation(request: LifecycleOperationReservationV2, *, idempotency_key: str) -> LifecycleOperationV2`
  - `get_lifecycle_operation(operation_id: str) -> LifecycleOperationV2`
  - `list_pending_lifecycle_operations() -> tuple[LifecycleOperationRefV2, ...]`
  - `claim_next_lifecycle_operation() -> LifecycleOperationWorkV2 | None`
  - `advance_lifecycle_operation(update: LifecycleOperationAdvanceV2) -> LifecycleOperationV2`
  - `append_lifecycle_log(entry: LifecycleLogAppendV2) -> LifecycleOperationV2`
  - `read_lifecycle_logs(operation_id: str, *, limit: int, after: str | None) -> LifecycleLogPageV2`
  - `request_lifecycle_cancellation(operation_id: str, *, if_match: str, idempotency_key: str) -> LifecycleOperationV2`
  - `finish_lifecycle_operation(completion: LifecycleOperationCompletionV2) -> LifecycleOperationV2`
  - `acknowledge_lifecycle_operation(operation_id: str, request: LifecycleAcknowledgeV2, *, if_match: str, idempotency_key: str) -> None`
  - `reconcile_lifecycle_operations() -> tuple[LifecycleOperationWorkV2, ...]`

- [ ] Make profile connection reservation and profile-generation transition one SQLite transaction. Replace `begin_profile_action` at lifecycle call sites with an operation-aware store method so no worker can run against an uncommitted profile generation.
- [ ] Persist the complete validated request document needed for restart recovery, but reject any request shape outside the six closed operation kinds. No command, env, Core endpoint/token, SSH path, or selected native host path may enter these tables.
- [ ] Implement signed HMAC cursors with the owner-private persisted cursor key, bound to operation ID, sequence, dropped-before boundary, and schema/store identity. Return typed cursor-expired authority when eviction invalidates a cursor; validate the singleton key length before any recovery cleanup.
- [ ] Enforce all byte/count budgets with `length(CAST(value AS BLOB))` before reading a row value. Revalidate exact bytes, UTF-8, canonical JSON, ETag, request digest, scalar mirror columns, and aggregate accounting on startup.
- [ ] Run the focused and full store tests:

```bash
uv run pytest -q tests/openevo/sidecar/test_provider_store_v2.py
```

- [ ] Commit and push:

```bash
git add desktop/sidecar/provider_store_v2.py tests/openevo/sidecar/test_provider_store_v2.py
git commit -m "feat(desktop): persist lifecycle operation authority"
git push
```

## Task 3: Add bounded log sanitization and process-output observers

**Files:**

- Create: `desktop/sidecar/lifecycle_logs_v2.py`
- Modify: `desktop/sidecar/system_ssh_session.py`
- Modify: `desktop/sidecar/remote_lifecycle.py`
- Modify: `desktop/sidecar/core_bridge_adapters_v2.py`
- Modify: `desktop/sidecar/core_bridge_v2.py`
- Modify: `src/openevo/deployment/ssh.py`
- Modify: `tests/openevo/sidecar/test_system_ssh_session.py`
- Modify: `tests/openevo/sidecar/test_remote_lifecycle.py`
- Modify: `tests/openevo/sidecar/test_core_bridge_adapters_v2.py`

- [ ] Add failing tests that feed chunked stdout/stderr with ANSI escapes, CR-overwrite sequences, NUL/control bytes, invalid UTF-8, split bearer tokens, proxy userinfo, authorization headers, askpass canaries, Core endpoints, absolute local/remote host paths, and exact 16-KiB boundaries. Assert persisted text is readable but secrets, forbidden authority, and controls never reach the sink.
- [ ] Add failing tests proving SSH command strings and env dictionaries are never passed to the observer, while output source is classified as `ssh_stdout`, `ssh_stderr`, `daemon_stdout`, or `daemon_stderr`.
- [ ] Run the red slice:

```bash
uv run pytest -q \
  tests/openevo/sidecar/test_system_ssh_session.py \
  tests/openevo/sidecar/test_remote_lifecycle.py \
  tests/openevo/sidecar/test_core_bridge_adapters_v2.py \
  -k 'observer or lifecycle_log or secret'
```

- [ ] Implement `LifecycleOutputSanitizerV2` as a per-stream incremental UTF-8 decoder with cross-chunk redaction state. Its callable sink receives only `(source, safe_text, truncated)`.
- [ ] Strip CSI/OSC escape sequences and all C0/C1 controls except normalized newline/tab. Replace credential-pattern matches, configured secret canaries, Core endpoint forms, and absolute POSIX/macOS host paths with reviewed markers before invoking the store callback.
- [ ] Extend the verified subprocess collectors with an optional chunk observer. Keep their existing total byte cap and returned `CompletedProcess`; invoke the observer only after sanitization and never with argv/env.
- [ ] Add optional progress/output observers to `SystemOpenSshRemoteLifecycleV2`, `DesktopCoreSshBridgeAdapterV2`, and `DesktopCoreBridgeV2`. Report explicit checkpoints around existing method calls; do not infer phase from output text.
- [ ] Wrap rich SSH transport results so each Daemon bootstrap/service step forwards its already-sanitized real stdout/stderr with Daemon source classification. For transfer APIs, report exact `(completed_bytes, total_bytes)` from the existing local asset size and bytes accepted by the transport.
- [ ] Preserve all current call signatures for non-Desktop users by making observers keyword-only and optional. No log observer may change success/failure semantics.
- [ ] Run the three full test modules, then Core deployment regressions:

```bash
uv run pytest -q \
  tests/openevo/sidecar/test_system_ssh_session.py \
  tests/openevo/sidecar/test_remote_lifecycle.py \
  tests/openevo/sidecar/test_core_bridge_adapters_v2.py \
  tests/deployment
```

- [ ] Commit and push:

```bash
git add desktop/sidecar/lifecycle_logs_v2.py desktop/sidecar/system_ssh_session.py \
  desktop/sidecar/remote_lifecycle.py desktop/sidecar/core_bridge_adapters_v2.py \
  desktop/sidecar/core_bridge_v2.py src/openevo/deployment/ssh.py \
  tests/openevo/sidecar/test_system_ssh_session.py \
  tests/openevo/sidecar/test_remote_lifecycle.py \
  tests/openevo/sidecar/test_core_bridge_adapters_v2.py
git commit -m "feat(desktop): capture bounded lifecycle process logs"
git push
```

## Task 4: Implement the single-worker lifecycle executor

**Files:**

- Create: `desktop/sidecar/lifecycle_executor_v2.py`
- Create: `tests/openevo/sidecar/test_lifecycle_executor_v2.py`
- Modify: `desktop/sidecar/release_provider_v2.py`

- [ ] Write failing executor tests for prompt reservation latency, FIFO single-worker execution, maximum-16 admission, fixed phase plans, crash checkpoints, restart recovery, cancellation before start, cancellation during an owned process, late-success fencing, and shutdown without losing queued authority.
- [ ] Include a test where the work function blocks for 16 seconds but reservation returns in under 500 ms and replays the identical operation ID.
- [ ] Run the red test:

```bash
uv run pytest -q tests/openevo/sidecar/test_lifecycle_executor_v2.py
```

- [ ] Implement `DesktopLifecycleExecutorV2` with one daemon worker thread, a bounded condition-protected queue, operation-ID deduplication, and kind-to-runner registration performed before recovery starts.
- [ ] The executor must call `claim_next_lifecycle_operation` after reservation; it must never accept an unpersisted callable as authority. On startup it must reconcile persisted work before accepting new external operations.
- [ ] Check cancellation between every checkpoint and pass a cancellation token into subprocess/transfer owners. Mark `cancellable=False` during Core mutation commit and other no-safe-return barriers.
- [ ] Map exceptions only through existing typed Desktop error projection. Never persist raw exception messages or use process output as the terminal result.
- [ ] Add provider helpers that publish a `LifecycleOperationEventPayloadV2` after reservation, each durable advance, log watermark change, and terminal commit.
- [ ] Run executor and provider tests:

```bash
uv run pytest -q \
  tests/openevo/sidecar/test_lifecycle_executor_v2.py \
  tests/openevo/sidecar/test_release_local_api_v2.py
```

- [ ] Commit and push:

```bash
git add desktop/sidecar/lifecycle_executor_v2.py desktop/sidecar/release_provider_v2.py \
  tests/openevo/sidecar/test_lifecycle_executor_v2.py \
  tests/openevo/sidecar/test_release_local_api_v2.py
git commit -m "feat(desktop): execute durable lifecycle operations"
git push
```

## Task 5: Convert profile lifecycle to asynchronous operations

**Files:**

- Modify: `desktop/sidecar/release_provider_v2.py`
- Modify: `desktop/sidecar/release_app.py`
- Modify: `tests/openevo/sidecar/test_release_local_api_v2.py`

- [ ] Rewrite the existing connect/review/disconnect tests first so the mutation returns queued/running authority promptly and the test waits on `GET /operations/{id}` for terminal state.
- [ ] Add failures for exact concurrent retry, changed key/request, host-key review continuation, askpass prompt phase, Daemon bootstrap failure, restart recovery, disconnect cancellation, and one-SSH-owner fencing.
- [ ] Run the red slice:

```bash
uv run pytest -q tests/openevo/sidecar/test_release_local_api_v2.py -k 'profile_connect or disconnect or host_key or operation'
```

- [ ] Change `_connect_profile`, `_disconnect_profile`, and `_review_host_key` to validate and reserve only. Move their current external work into executor runners that use the same operation ID/idempotency key and persisted connection generation.
- [ ] Map real checkpoints to fixed plans: OpenSSH resolution/connect, user wait, remote preflight, Daemon transfer/verify/start/readiness, Core negotiation, saved-project reactivation, finalization.
- [ ] Wire the executor and output observers in `create_release_app_v2`; close them in ownership order and ensure startup recovery finishes registration before the HTTP listener is ready.
- [ ] Confirm the complete release-local test module passes:

```bash
uv run pytest -q tests/openevo/sidecar/test_release_local_api_v2.py
```

- [ ] Commit and push:

```bash
git add desktop/sidecar/release_provider_v2.py desktop/sidecar/release_app.py \
  tests/openevo/sidecar/test_release_local_api_v2.py
git commit -m "feat(desktop): run profile lifecycle asynchronously"
git push
```

## Task 6: Convert native workspace preparation and project activation

**Files:**

- Modify: `desktop/sidecar/release_provider_v2.py`
- Modify: `desktop/sidecar/release_app.py`
- Modify: `desktop/sidecar/native_workspace.py`
- Modify: `desktop/sidecar/workspace_imports.py`
- Modify: `desktop/sidecar/core_bridge_v2.py`
- Modify: `tests/openevo/desktop/test_app.py`
- Modify: `tests/openevo/sidecar/test_release_local_api_v2.py`
- Modify: `tests/openevo/sidecar/test_core_bridge_v2.py`

- [ ] Add failing tests that the hidden native workspace registration returns a lifecycle operation promptly, never returns a host path, reports exact item/byte progress, supports cancellation, and terminally exposes only the existing renderer-safe import reference metadata.
- [ ] Add failing tests that project create/activate return 202 immediately, replay one operation and one Core bridge mutation, survive restart after Core apply, reject cross-wired Core project IDs, and produce a terminal project result only after mapping/import authority is committed.
- [ ] Include the direct reported regression: a bridge activation delayed for 16 seconds must not produce an HTTP timeout or a second Core `create_project_v2` row.
- [ ] Run the red slice:

```bash
uv run pytest -q \
  tests/openevo/desktop/test_app.py \
  tests/openevo/sidecar/test_release_local_api_v2.py \
  tests/openevo/sidecar/test_core_bridge_v2.py \
  -k 'native_workspace or create_project or project_activate or lifecycle'
```

- [ ] Reserve `native_workspace_prepare` in the hidden authenticated route before traversal/archive ingestion. Feed the existing cancellation token and actual byte/item counts into the store. Keep the selected path inside the native request boundary.
- [ ] Make successful native preparation return a typed result that the Tauri host can bind to its pending action without exposing the path. Project create then reuses that action identity and verified import ownership.
- [ ] Change `_create_project` and `_activate_project` to reserve operations. Move `_bridge.activate_project`, native upload/materialization, project verification, and profile active-project binding into a restart-aware runner.
- [ ] In `CoreMutationOutcomeUnknownV2`, retain the operation as running/reconciling and probe the existing bridge mutation ledger. Never issue `create_project_v2` under a new identity.
- [ ] Run the full affected modules:

```bash
uv run pytest -q \
  tests/openevo/desktop/test_app.py \
  tests/openevo/sidecar/test_release_local_api_v2.py \
  tests/openevo/sidecar/test_core_bridge_v2.py \
  tests/openevo/sidecar/test_workspace_imports.py
```

- [ ] Commit and push:

```bash
git add desktop/sidecar/release_provider_v2.py desktop/sidecar/release_app.py \
  desktop/sidecar/native_workspace.py desktop/sidecar/workspace_imports.py \
  desktop/sidecar/core_bridge_v2.py tests/openevo/desktop/test_app.py \
  tests/openevo/sidecar/test_release_local_api_v2.py \
  tests/openevo/sidecar/test_core_bridge_v2.py
git commit -m "feat(desktop): make project lifecycle resumable"
git push
```

## Task 7: Wire observation routes, logs, state recovery, and SSE

**Files:**

- Modify: `desktop/sidecar/release_provider_v2.py`
- Modify: `desktop/sidecar/release_app.py`
- Modify: `desktop/sidecar/event_broker_v2.py`
- Modify: `tests/openevo/sidecar/test_event_broker_v2.py`
- Modify: `tests/openevo/sidecar/test_release_local_api_v2.py`

- [ ] Add failing tests for authenticated operation read, signed ascending log pages, cursor expiry/410 reset, cancel CAS, terminal acknowledgement, pending/unacknowledged operations in state, operation rediscovery after restart, crash between native clear and acknowledgement, SSE invalidation/replay/gap handling, and absence of log bodies in events.
- [ ] Run red tests:

```bash
uv run pytest -q \
  tests/openevo/sidecar/test_event_broker_v2.py \
  tests/openevo/sidecar/test_release_local_api_v2.py \
  -k 'lifecycle or cursor or pending_operation'
```

- [ ] Add provider handlers for `getDesktopLifecycleOperationV2`, `getDesktopLifecycleOperationLogsV2`, `cancelDesktopLifecycleOperationV2`, and `acknowledgeDesktopLifecycleOperationV2`; add nonterminal and unacknowledged terminal refs to `_state`.
- [ ] Add tunnel-only provider/bridge handlers for `getDesktopCoreOperationV2`, `cancelDesktopCoreOperationV2`, `getDesktopServiceLogsV2`, and `cleanupDesktopCachesV2`. Preserve complete Core status/progress/error/ETag and reject cross-wired operation/service identity.
- [ ] Project `ProviderNotFoundV2`, cursor expiry, capacity, CAS conflict, and non-cancellable barriers to closed Desktop error codes with correct retry/action fields.
- [ ] Publish only compact operation invalidations through `DesktopEventBrokerV2`; retain all existing sequence, replay, queue, frame, and subscriber budgets.
- [ ] Run all v2 sidecar tests:

```bash
uv run pytest -q tests/openevo/sidecar -k 'v2 or V2'
```

- [ ] Commit and push:

```bash
git add desktop/sidecar/release_provider_v2.py desktop/sidecar/release_app.py \
  desktop/sidecar/event_broker_v2.py tests/openevo/sidecar/test_event_broker_v2.py \
  tests/openevo/sidecar/test_release_local_api_v2.py
git commit -m "feat(desktop): expose lifecycle recovery and logs"
git push
```

## Task 8: Update the architectural contracts and run the Sidecar gate

**Files:**

- Modify: `docs/maintainer/productization/spec.md`
- Modify: `docs/architecture/desktop-core-contract-v2.md`
- Modify: `desktop/sidecar/README.md`
- Modify: `tests/ci/test_build_sidecar.py`

- [ ] Add documentation/tests first that reject the old claim that no SSH output can cross the sidecar boundary. State the exact replacement: bounded sanitized child stdout/stderr may cross; command lines, env, paths, endpoints, tokens, secret refs, and credentials may not.
- [ ] Document operation ownership, phase authority, log budgets, cursor behavior, cancellation, store recovery, native hidden route, and the Core tunnel boundary.
- [ ] Add packaged-sidecar assertions for the new modules and regenerated contract snapshots.
- [ ] Run the Sidecar acceptance gate:

```bash
uv run pytest -q \
  tests/openevo/sidecar \
  tests/openevo/desktop/test_app.py \
  tests/ci/test_build_sidecar.py
```

- [ ] Audit for forbidden renderer-visible fields:

```bash
rg -n 'command|argv|environment|bearer_token|core_url|secret_ref|selected_path' \
  desktop/sidecar/contracts/v2 desktop/sidecar/contracts/v2/openapi.json
```

Expected: no model/property exposing those authorities; documentation descriptions may contain the words only when explicitly forbidding them.

- [ ] Commit and push:

```bash
git add docs/maintainer/productization/spec.md docs/architecture/desktop-core-contract-v2.md \
  desktop/sidecar/README.md tests/ci/test_build_sidecar.py
git commit -m "docs(desktop): specify observable lifecycle authority"
git push
```

## Sidecar plan completion criteria

- [ ] Every Desktop-owned long operation returns durable 202 authority before external work.
- [ ] Exact retries reuse one operation ID and one remote mutation; changed intent cannot reuse it.
- [ ] Fixed phases, progress, logs, cancellation, restart recovery, and capacity bounds have focused tests.
- [ ] Actual SSH/Daemon output is visible only after mandatory sanitization.
- [ ] Renderer-visible contracts contain no host path, command, env, Core URL/token, or secret reference.
- [ ] All affected Python tests pass from a clean process.
