# Desktop v0.1.10 Native And Renderer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every Desktop mutation retain exact retry identity across ambiguous transport/relaunch, and give users a reusable lifecycle panel with authoritative progress, elapsed time, actual bounded SSH/Daemon logs, cancellation, and recovery.

**Architecture:** A private Tauri journal stores unresolved mutation identity with compare-and-swap durability. The v2 provider canonicalizes every request, reserves or reuses the journal entry before HTTP/native transport, binds accepted lifecycle operation IDs, and clears only after terminal reconciliation. Renderer state observes lifecycle operations through Local API snapshots/SSE with bounded polling fallback and presents them through one accessible panel.

**Tech Stack:** Rust/Tauri 2, TypeScript, React 19, Zod, Vitest/Testing Library, Playwright, CSS.

---

## Renderer/native invariants

- A click-generated action ID is only a proposal. The provider reuses an unresolved journal action ID when kind, scope, canonical request digest, ETag/generation, and stream authority match exactly.
- Changed intent cannot overwrite an unresolved entry. It yields a closed local conflict and exposes Resume/Reconcile.
- HTTP timeout, abort, connection reset, app quit, WebView reload, or typed unknown outcome retains the journal row.
- Direct success is cleared only after the matching resource appears in an authoritative refresh. Lifecycle success/failure/cancellation is cleared only after exact terminal operation observation; the native row is cleared before the sidecar terminal acknowledgement handshake.
- Journal limits are 16 entries, 64 KiB per entry, and 1 MiB aggregate canonical UTF-8.
- UI keeps at most 200 rendered log entries per operation; older retained pages load explicitly.
- SSE transports invalidations only. Log bodies are fetched from the authenticated log route.

## Task 1: Mirror the lifecycle contract in TypeScript

**Files:**

- Modify: `desktop/src/api/v2/schemas.ts`
- Modify: `desktop/src/api/v2/client.ts`
- Modify: `desktop/src/api/v2/schemas.test.ts`
- Modify: `desktop/src/api/v2/client.test.ts`
- Modify: `desktop/src/api/v2/sse.test.ts`

- [ ] Add failing Zod tests for all strict lifecycle models, progress bounds, status/result binding, pending-operation uniqueness, log page identity, truncation metadata, the new event payload digest, and exact Core `OperationV2`/service-log/cache-cleanup projections.
- [ ] Add failing client tests asserting all six long mutations expect 202, project creation no longer accepts 201 `ProjectV2`, operation/log GET identity is checked, cancel and acknowledge send generation/ETag/idempotency headers, and log cursors are URL encoded. Add separate tests for tunnel-only Core operation lookup/cancel, service logs, and cache cleanup.
- [ ] Add a failing test that a response to project creation arrives within the normal bounded request timeout even when the simulated worker continues for more than 15 seconds.
- [ ] Run the red tests:

```bash
cd desktop
npm test -- --run src/api/v2/schemas.test.ts src/api/v2/client.test.ts src/api/v2/sse.test.ts
```

- [ ] Implement exact `.strict()` schemas corresponding to the generated Python/OpenAPI contract. Export `LifecycleOperationV2`, `LifecycleOperationRefV2`, `LifecycleLogEntryV2`, `LifecycleLogPageV2`, `LifecycleCancelV2`, `LifecycleAcknowledgeV2`, and progress/result/resource union types.
- [ ] Extend `DesktopApiClientV2` with:

```ts
getLifecycleOperation(operationId: string): Promise<LifecycleOperationV2>;
lifecycleOperationLogs(
  operationId: string,
  options?: ListRequestOptionsV2,
): Promise<LifecycleLogPageV2>;
cancelLifecycleOperation(
  operationId: string,
  input: LifecycleCancelV2,
  options: ResourceMutationRequestOptionsV2,
): Promise<LifecycleOperationV2>;
acknowledgeLifecycleOperation(
  operationId: string,
  input: LifecycleAcknowledgeV2,
  options: ResourceMutationRequestOptionsV2,
): Promise<void>;
getCoreOperation(operationId: string): Promise<OperationV2>;
cancelCoreOperation(
  operationId: string,
  options: ResourceMutationRequestOptionsV2,
): Promise<OperationV2>;
serviceLogs(serviceId: string, options?: ListRequestOptionsV2): Promise<LogPageV2>;
cleanupCaches(
  input: CacheCleanupRequestV2,
  options: MutationRequestOptionsV2,
): Promise<OperationV2>;
```

- [ ] Make connect/disconnect/host-key review/project create/project activate parse `LifecycleOperationV2` with status 202 and the standard 15-second request bound. Remove `REMOTE_LIFECYCLE_REQUEST_TIMEOUT_MS`; long work no longer lives in HTTP.
- [ ] Parse `lifecycle_operation_changed` in SSE without special fallback and retain existing replay/gap guarantees.
- [ ] Run and pass the focused tests plus typecheck:

```bash
cd desktop
npm test -- --run src/api/v2/schemas.test.ts src/api/v2/client.test.ts src/api/v2/sse.test.ts
npm run typecheck
```

- [ ] Commit and push:

```bash
git add desktop/src/api/v2
git commit -m "feat(desktop): consume lifecycle operation contract"
git push
```

## Task 2: Extract a reusable owner-private native JSON journal

**Files:**

- Create: `desktop/src-tauri/src/private_json_journal.rs`
- Modify: `desktop/src-tauri/src/main.rs`
- Modify: `desktop/src-tauri/Cargo.toml`

- [ ] Before refactoring, add/retain Rust tests for the existing run-retry journal: symlink/hardlink/ACL rejection, owner/mode checks, parent replacement, file replacement, CAS conflict, interrupted rename, fsync failure, cross-process lock, oversized bytes, malformed UTF-8, and post-unlink identity.
- [ ] Add failing generic-journal tests for a 1-MiB file budget, missing journal read, exact CAS write/clear, no-follow path traversal, one writer across two process-lock handles, and authoritative readback after publish.
- [ ] Run the red Rust slice:

```bash
cd desktop/src-tauri
cargo test --locked --release private_json_journal -- --test-threads=1
```

- [ ] Move the current FD-relative root/open/identity/kernel-lock/temp-write/fsync/rename/readback machinery from the `RunRetryRecovery*` block into `private_json_journal.rs`. Parameterize only fixed directory/file names, maximum bytes, and stable typed error constructors.
- [ ] Keep no-follow absolute traversal, trusted macOS alias resolution, `0700` root, `0600` link-count-one regular files, extended ACL validation, held-FD/path binding checks, kernel `flock`, directory fsync, and post-publish readback unchanged.
- [ ] Rewrite run-retry recovery to call this module and confirm its pre-existing tests remain green before adding the new mutation journal.
- [ ] Run all Rust tests:

```bash
cd desktop/src-tauri
cargo test --locked --release -- --test-threads=1
```

- [ ] Commit and push:

```bash
git add desktop/src-tauri/src/private_json_journal.rs desktop/src-tauri/src/main.rs \
  desktop/src-tauri/Cargo.toml desktop/src-tauri/Cargo.lock
git commit -m "refactor(desktop): share private native journal storage"
git push
```

## Task 3: Add the bounded mutation-intent journal to Tauri

**Files:**

- Create: `desktop/src-tauri/src/mutation_intent_journal_v2.rs`
- Modify: `desktop/src-tauri/src/main.rs`
- Modify: `desktop/src/product/releaseProvider.ts`
- Modify: `desktop/src/product/releaseProviderV2.test.ts`

- [ ] Add failing Rust tests for strict serde decoding, unknown fields, invalid action/digest/ETag/time, duplicate action IDs, duplicate logical intent, entry/aggregate budgets, 16-row capacity, lifecycle-state transitions, CAS conflicts, and non-secret field enforcement.
- [ ] Add failing TypeScript bridge tests for read/CAS command names and camelCase arguments.
- [ ] Run the red tests:

```bash
cd desktop/src-tauri
cargo test --locked --release mutation_intent_journal_v2 -- --test-threads=1
cd ..
npm test -- --run src/product/releaseProviderV2.test.ts
```

- [ ] Implement strict native models with `#[serde(deny_unknown_fields)]`. The root document is:

```rust
#[derive(Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct PendingMutationJournalV2 {
    schema_version: String,
    revision: u64,
    entries: Vec<PendingMutationIntentV2>,
}

#[derive(Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct PendingMutationIntentV2 {
    action_id: String,
    mutation_kind: String,
    resource_scope: String,
    request_sha256: String,
    authority_sha256: String,
    provider_stream_instance: String,
    provider_stream_epoch: u64,
    chain_step: String,
    accepted_operation_id: Option<String>,
    completed_operation_ids: Vec<String>,
    state: String,
    created_at: String,
    updated_at: String,
}
```

- [ ] Validate the closed `state` set `reserved`, `accepted`, `terminal_observed`, and `deterministic_rejection`; validate allowed monotonic transitions. Validate `chain_step` as `single`, `native_workspace_prepare`, or `project_create`; require a current operation only in accepted/terminal states and cap unique completed operation IDs at two.
- [ ] Reject field values containing control characters or names associated with secrets (`token`, `password`, `credential`, `environment`, `command`, `host_path`, `core_url`, `secret_ref`). Store only digests and opaque authority.
- [ ] Expose two Tauri commands backed by one CAS operation:

```rust
#[tauri::command]
fn read_mutation_intent_journal_v2(
    app: tauri::AppHandle,
    state: tauri::State<'_, DesktopHostState>,
) -> HostResult<Option<String>>

#[tauri::command(rename_all = "camelCase")]
fn compare_and_swap_mutation_intent_journal_v2(
    app: tauri::AppHandle,
    state: tauri::State<'_, DesktopHostState>,
    expected_value: Option<String>,
    new_value: Option<String>,
) -> HostResult<()>
```

- [ ] Register both commands in the invoke handler and add the bridge methods in `releaseProvider.ts`.
- [ ] Run full Rust tests and focused TypeScript tests:

```bash
cd desktop/src-tauri
cargo test --locked --release -- --test-threads=1
cd ..
npm test -- --run src/product/releaseProviderV2.test.ts
```

- [ ] Commit and push:

```bash
git add desktop/src-tauri/src/mutation_intent_journal_v2.rs desktop/src-tauri/src/main.rs \
  desktop/src/product/releaseProvider.ts desktop/src/product/releaseProviderV2.test.ts
git commit -m "feat(desktop): persist mutation retry intent natively"
git push
```

## Task 4: Implement the TypeScript mutation coordinator

**Files:**

- Create: `desktop/src/product/mutationIntentJournalV2.ts`
- Create: `desktop/src/product/mutationIntentJournalV2.test.ts`
- Modify: `desktop/src/product/localApiProviderV2.ts`
- Modify: `desktop/src/product/localApiProviderV2.test.ts`
- Modify: `desktop/src/product/providerV2.ts`
- Modify: `desktop/src/product/releaseProvider.ts`

- [ ] Add failing unit tests for canonical request/authority digesting, reservation before fetch, exact retry reuse, relaunch restore, changed request rejection, changed generation/ETag rejection, concurrent CAS retry, accepted operation binding, deterministic rejection, unknown outcome retention, terminal reconciliation, direct-response refresh reconciliation, native-folder two-step chain advancement, explicit import discard, and capacity failure.
- [ ] Add a table-driven failing audit test covering every v2 provider mutation: SSH rescan/profile CRUD/rebind/connect/disconnect/review, native source select/cancel/settle, project create/update/activate/validate, Task submit/cancel/retry, transition retry/replace/abandon, service restart, and diagnostic creation.
- [ ] Run red tests:

```bash
cd desktop
npm test -- --run \
  src/product/mutationIntentJournalV2.test.ts \
  src/product/localApiProviderV2.test.ts
```

- [ ] Implement `MutationIntentCoordinatorV2` with async initialization from the native journal and a serialized CAS loop. Canonicalize strict request data with `canonicalJsonV2`; hash request and authority separately using Web Crypto SHA-256.
- [ ] Expose these exact coordinator operations:

```ts
reserve(input: MutationReservationV2): Promise<PendingMutationIntentV2>;
bindAcceptedOperation(actionId: string, operationId: string): Promise<PendingMutationIntentV2>;
advanceNativeProjectChain(actionId: string, completedOperationId: string): Promise<PendingMutationIntentV2>;
markTerminalObserved(actionId: string, operationId: string): Promise<void>;
markDirectResponseObserved(actionId: string, resultSha256: string): Promise<void>;
reconcile(snapshot: DesktopProductSnapshotV2): Promise<readonly PendingMutationIntentV2[]>;
list(): readonly PendingMutationIntentV2[];
```

- [ ] Add one provider wrapper `dispatchMutationV2` used by every mutation. It reserves before transport, passes the coordinator-selected action ID as `Idempotency-Key`, binds 202 operation authority before UI publication, and classifies errors into deterministic rejection versus ambiguous retention.
- [ ] Change `NativeWorkspaceSelectionIntentV2` to carry the complete strict `ProjectDraftV2` and profile authority used for the eventual create. Canonicalize those safe fields into the chain digest; never add the selected host path or native lease token.
- [ ] Do not let `streamEpoch` alone become retry identity. Bind the journal to negotiated provider instance, latest event ID/sequence digest, resource generation, and ETag supplied by the exact request.
- [ ] On provider startup, initialize/reconcile before enabling mutation controls. Surface journal read/CAS corruption as a typed fail-closed local error that requires Desktop restart or diagnostics; do not fall back to ephemeral IDs.
- [ ] Run and pass provider tests/typecheck:

```bash
cd desktop
npm test -- --run \
  src/product/mutationIntentJournalV2.test.ts \
  src/product/localApiProviderV2.test.ts
npm run typecheck
```

- [ ] Commit and push:

```bash
git add desktop/src/product/mutationIntentJournalV2.ts \
  desktop/src/product/mutationIntentJournalV2.test.ts \
  desktop/src/product/localApiProviderV2.ts \
  desktop/src/product/localApiProviderV2.test.ts \
  desktop/src/product/providerV2.ts desktop/src/product/releaseProvider.ts
git commit -m "feat(desktop): reuse exact mutation identity after ambiguity"
git push
```

## Task 5: Add lifecycle observation, bounded logs, and polling recovery

**Files:**

- Create: `desktop/src/product/lifecycleOperationsV2.ts`
- Create: `desktop/src/product/lifecycleOperationsV2.test.ts`
- Modify: `desktop/src/product/localApiProviderV2.ts`
- Modify: `desktop/src/product/providerV2.ts`
- Modify: `desktop/src/product/localApiProviderV2.test.ts`

- [ ] Add failing tests for pending-operation discovery from state, unacknowledged-terminal recovery, operation refresh on SSE invalidation, missing log-page fetch from watermark, 200-entry in-memory tail, explicit older-page load, cursor-expired reset, SSE disconnect polling, exponential bounded polling, terminal project/profile reconciliation, crash after native clear but before sidecar acknowledgement, and no duplicate mutation during observation. Add Core-operation polling tests that preserve Core progress/ETag and never query the Desktop lifecycle store.
- [ ] Run red tests:

```bash
cd desktop
npm test -- --run \
  src/product/lifecycleOperationsV2.test.ts \
  src/product/localApiProviderV2.test.ts
```

- [ ] Implement an immutable `LifecycleOperationControllerV2` state machine. It may issue only GET/log GET/cancel requests; recovery must never call a lifecycle mutation route.
- [ ] Implement a separate `CoreOperationControllerV2` adapter that polls the namespaced Core projection through the active project tunnel and maps `progress_completed/progress_total` to the shared presentation model. It must stop/fail closed when active project authority changes.
- [ ] Keep one operation map keyed by operation ID and one bounded log tail per operation. Validate every fetched operation has non-regressing status/phase/progress/watermark/ETag against the prior observation.
- [ ] Use SSE invalidation when healthy. When disconnected, poll known nonterminal operations at 500 ms, 1 s, 2 s, then 4 s capped until terminal; reset to 500 ms on any progress.
- [ ] On a terminal project result, refresh projects and require exactly one matching `project_id`. On terminal profile result, refresh profiles and require exact profile/generation state. Clear the exact native journal row first, then call the idempotent terminal acknowledgement route. If acknowledgement is ambiguous, rely on state rediscovery and retry only acknowledgement.
- [ ] Add provider methods `getLifecycleOperation`, `loadLifecycleLogs`, `cancelLifecycleOperation`, `resumeMutationIntent`, `listMutationIntents`, `getCoreOperation`, `cancelCoreOperation`, `loadServiceLogs`, and `cleanupCaches` for the UI.
- [ ] Run tests and typecheck:

```bash
cd desktop
npm test -- --run \
  src/product/lifecycleOperationsV2.test.ts \
  src/product/localApiProviderV2.test.ts
npm run typecheck
```

- [ ] Commit and push:

```bash
git add desktop/src/product/lifecycleOperationsV2.ts \
  desktop/src/product/lifecycleOperationsV2.test.ts \
  desktop/src/product/localApiProviderV2.ts \
  desktop/src/product/localApiProviderV2.test.ts \
  desktop/src/product/providerV2.ts
git commit -m "feat(desktop): recover lifecycle progress and logs"
git push
```

## Task 6: Build the reusable lifecycle panel

**Files:**

- Create: `desktop/src/product/LifecycleOperationPanelV2.tsx`
- Create: `desktop/src/product/LifecycleOperationPanelV2.test.tsx`
- Modify: `desktop/src/styles.css`

- [ ] Add failing component tests for phase title, checkpoint fraction, determinate bytes/items, indeterminate state, elapsed time, latest real log lines/source labels, expand/load older, dropped-log notice, typed failure/action, safe cancel, Resume/Reconcile, keyboard use, `aria-live`, and reduced motion.
- [ ] Add a fake-timer test proving elapsed time updates without implying an ETA or fabricated percentage.
- [ ] Run red tests:

```bash
cd desktop
npm test -- --run src/product/LifecycleOperationPanelV2.test.tsx
```

- [ ] Implement a controlled component whose data/actions are supplied by the provider. Use native `<progress>` for determinate values; for indeterminate progress omit `value`. Show checkpoint text independently of color.
- [ ] Render log text only as React text nodes inside a monospace `<pre>`/list. Do not use `dangerouslySetInnerHTML`, terminal emulation, ANSI interpretation, or linkification.
- [ ] Use a polite status live region for phase/terminal changes and a non-live log viewport so screen readers are not flooded. Support Escape only to close expanded history, not to cancel work.
- [ ] Add `@media (prefers-reduced-motion: reduce)` rules that remove indeterminate animation while retaining visible state.
- [ ] Run the component tests and typecheck:

```bash
cd desktop
npm test -- --run src/product/LifecycleOperationPanelV2.test.tsx
npm run typecheck
```

- [ ] Commit and push:

```bash
git add desktop/src/product/LifecycleOperationPanelV2.tsx \
  desktop/src/product/LifecycleOperationPanelV2.test.tsx desktop/src/styles.css
git commit -m "feat(desktop): show lifecycle progress and process logs"
git push
```

## Task 7: Wire every long lifecycle flow and global recovery UI

**Files:**

- Modify: `desktop/src/App.tsx`
- Modify: `desktop/src/App.test.tsx`
- Modify: `desktop/src/product/DesktopProductAppV2.tsx`
- Modify: `desktop/src/product/DesktopProductAppV2.test.tsx`
- Modify: `desktop/src/product/ScientificProjectSample.test.tsx`
- Modify: `desktop/src/product/localApiProviderV2.ts`
- Modify: `desktop/src/product/releaseProvider.ts`
- Modify: `desktop/src-tauri/src/main.rs`

- [ ] Add failing app tests for native app/sidecar startup, connect/disconnect/host-key review, native workspace preparation, project create/activate, Task/evolution, transition, diagnostic, service restart/logs, cache cleanup, closing/reopening drawers, global pending indicator, relaunch restore, cancel availability, ambiguous retry reuse, and mutation conflicts.
- [ ] Add the reported-regression UI test: click Create once, receive a 202 operation, advance fake time past 15 seconds while still running, observe progress/logs and no timeout banner, then terminally display the one matching project.
- [ ] Add a test that a second click/relaunch cannot create a new request identity while the first operation is unresolved.
- [ ] Run red tests:

```bash
cd desktop
npm test -- --run \
  src/product/DesktopProductAppV2.test.tsx \
  src/product/ScientificProjectSample.test.tsx
```

- [ ] Change native `select_project_source` to return the renderer-safe lifecycle operation from the hidden sidecar route immediately after folder selection/reservation. Retain the pending private import binding by action ID; never return the selected path.
- [ ] Add a strict `NativeStartupStatusV2` Tauri DTO and `sidecar_startup_status` command bound to the current startup epoch. Expose fixed stage/index/total, elapsed milliseconds, cancellability, and closed failure only. Poll it from `App.tsx` while Local API bootstrap is unavailable and render it through the shared panel; raw bootloader/Python stderr remains outside renderer data.
- [ ] In the project drawer, reserve the native-project chain against the complete current draft before opening the picker and freeze conflicting form controls. Show workspace preparation until its typed result is reconciled, move that operation into the journal's completed list, acknowledge it, and enable Create using the same action ID at `project_create`. Show the second operation until terminal success, then clear/ack only after exact project refresh. Explicit discard is the only early path that releases the chain/import.
- [ ] Embed `LifecycleOperationPanelV2` in profile connection, host-key, project creation, and activation contexts. Add a global operation button/list sourced from `DesktopStateV2.pending_operations` plus retained unresolved journal intents.
- [ ] Adapt native startup status, Core Task state/timeline/logs, successor-transition progress, diagnostic status, Core operation progress, and service logs into the same panel. Preserve each source authority and never synthesize Desktop lifecycle phases for Core work.
- [ ] Disable only conflicting controls. Drawer close/navigation must not cancel or clear. Relaunch must make pending work discoverable before mutation controls enable.
- [ ] Preserve Core Task/timeline/log UI and use the shared visual language without converting Core-owned actions into Desktop lifecycle operations.
- [ ] Run app tests, all Vitest tests, and typecheck:

```bash
cd desktop
npm test -- --run src/product/DesktopProductAppV2.test.tsx src/product/ScientificProjectSample.test.tsx
npm test -- --run
npm run typecheck
```

- [ ] Commit and push:

```bash
git add desktop/src/App.tsx desktop/src/App.test.tsx \
  desktop/src/product/DesktopProductAppV2.tsx \
  desktop/src/product/DesktopProductAppV2.test.tsx \
  desktop/src/product/ScientificProjectSample.test.tsx \
  desktop/src/product/localApiProviderV2.ts desktop/src/product/releaseProvider.ts \
  desktop/src-tauri/src/main.rs
git commit -m "feat(desktop): wire observable long-running workflows"
git push
```

## Task 8: Add browser/visual acceptance and renderer documentation

**Files:**

- Modify: `desktop/tests/product-browser/scientific-project-sample.pw.ts`
- Modify: `desktop/tests/product-browser/system-recovery.pw.ts`
- Modify: `desktop/tests/product-browser/release-readonly.pw.ts`
- Add or update snapshots under: `desktop/tests/product-browser/scientific-project-sample.pw.ts-snapshots/`
- Add or update snapshots under: `desktop/tests/product-browser/release-readonly.pw.ts-snapshots/`
- Modify: `desktop/src/product/README.md`

- [ ] Add browser fixtures with a queued/running lifecycle operation, at least two phases, determinate and indeterminate progress, actual multiline SSH/Daemon log text, dropped-before metadata, failure, cancellation, and recovery.
- [ ] Add responsive visual assertions at the existing desktop and narrow viewport sizes. Verify focus order, keyboard expansion/cancel, log overflow, long unbroken output, high contrast text, and reduced-motion emulation.
- [ ] Run the browser tests before snapshot update and inspect the expected failures visually:

```bash
cd desktop
npm run test:product-browser:preview
npm run test:product-browser:release-readonly
```

- [ ] Update snapshots only after manual image inspection:

```bash
cd desktop
npm run test:product-browser:update
npx playwright test --config playwright.release-readonly.config.ts --update-snapshots
npm run test:product-browser
```

- [ ] Document the provider coordinator, lifecycle controller, component boundary, log-memory cap, polling fallback, and fail-closed native journal behavior.
- [ ] Run the renderer gate:

```bash
cd desktop
npm test -- --run
npm run typecheck
npm run build:openevo
npm run test:product-browser
```

- [ ] Commit and push:

```bash
git add desktop/tests/product-browser desktop/src/product/README.md
git commit -m "test(desktop): cover lifecycle progress experience"
git push
```

## Native/renderer plan completion criteria

- [ ] Every mutation is covered by the table-driven exact-retry audit.
- [ ] Mutation identity survives WebView/Desktop restart and never falls back to ephemeral state after journal failure.
- [ ] Project creation remains visibly active beyond 15 seconds without a request-timeout error.
- [ ] The same panel handles all Desktop-owned long operations and preserves Core-owned authority.
- [ ] Process logs render as inert bounded text and never expose commands, env, credentials, Core endpoints, or absolute host paths.
- [ ] Vitest, TypeScript, Rust, and browser visual gates all pass.
