# OpenEvo 0.1.9 macOS System OpenSSH Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: use
> `superpowers:executing-plans` to implement this plan task-by-task. Every
> behavior change follows `superpowers:test-driven-development`; every failure
> investigation follows `superpowers:systematic-debugging`. Do not delegate
> unless the runtime can explicitly select `gpt-5.6-terra` with high reasoning.

**Goal:** Ship an unsigned OpenEvo Desktop 0.1.9 that starts on the target
Apple Silicon macOS Tahoe machine, uses the user's system OpenSSH alias as the
remote-workspace authority, installs or attaches the exact matching Daemon,
and completes two real Codex Subscription sessions with atomic next-session
evolution reuse.

**Architecture:** Implement the approved design in
`docs/maintainer/development-history/superpowers/specs/2026-07-23-openevo-v019-macos-system-ssh-design.md`.
Keep SSH/bootstrap in the packaged sidecar until a compatible Daemon exists.
Use a separately inventoried native askpass helper for secure prompts. Cut the
renderer, sidecar, and Daemon to strict v2 contracts with distinct Project
Head, Evolution Revision, Runtime Context, Effective Execution, Task Admission,
and Attempt identities. After tunnel negotiation, use only Core Control API v2
for business state.

**Tech stack:** Python 3.11, Pydantic v2, FastAPI, SQLite, pytest, TypeScript,
React 19, Zod, Vitest, Playwright, Rust 1.95, Tauri 2, macOS AppKit/OpenSSH,
PyInstaller 6.21, Docker, GitHub Actions.

**Tracking:** Part of #131. Before opening a PR, use a scoped child issue when
the repository workflow permits it; otherwise describe the PR as `Part of
#131` and do not claim that it closes the productization tracker.

---

## Execution protocol

- Work only in the `feat/v019-macos-system-ssh` worktree.
- Keep `/Applications/OpenEvo Desktop.app` 0.1.8 and retained Preview state
  untouched as reproduction and upgrade evidence.
- Commit as `ivowang <ziyiwang@ieee.org>`.
- Keep each numbered task independently reviewable and commit it after RED,
  GREEN, affected regressions, docs, `git diff --check`, and diff review.
- Never modify protected evolution algorithm bodies, prompts, defaults,
  filtering, selection, scoring, or artifact semantics.
- Never use simulator, dry-run, source-sidecar, direct backend, or legacy-route
  fallback in a release composition.
- Treat v1 provider/state as read-only migration input after the v2 cutover.
- Do not dispatch a candidate workflow until local installed-DMG and real-host
  rehearsal pass.
- At workstream boundaries, request independent review only through an
  explicitly selectable `gpt-5.6-terra` high-effort worker. If unavailable,
  continue implementation and record the review as a release blocker rather
  than substituting an unspecified agent.

## Verification tiers

Use these named tiers throughout the plan:

```bash
# Python focused
uv run pytest <listed test files> -q
uv run ruff check <changed Python files and tests>

# Renderer focused
cd desktop
npm test -- --run <listed test files>
npm run typecheck

# Rust focused
cd desktop/src-tauri
cargo fmt --check
cargo test --locked --release -- --test-threads=1
cargo clippy --locked --release --all-targets -- -D warnings

# Patch hygiene
git diff --check
git status --short
```

The exact broader gate is added in Task 18 after every slice is present.

---

## Phase A: Freeze product and startup behavior

### Task 1: Synchronize canonical SSH and v2 authority documentation

**Files:**

- Modify: `AGENTS.md`
- Modify: `docs/maintainer/productization/spec.md`
- Modify: `docs/maintainer/productization/implementation-plan.md`
- Modify: `docs/architecture/desktop-core-contract-v1.md`
- Create: `docs/architecture/desktop-core-contract-v2.md`
- Modify: `docs/architecture/openevo-desktop-release.md`
- Modify: `docs/architecture/openevo-desktop-ssh-transport-foundation.md`
- Modify: `docs/maintainer/macos-desktop-development-handoff.md`
- Test: `tests/ci/test_check_openevo_release.py`

- [ ] Add a failing documentation/release-policy test that rejects release text
  which still describes manually entered host/user/port or isolated
  `ssh_agent` as the 0.1.9 default, and requires the v2 contract document.
- [ ] Run that test and record RED.
- [ ] Update the canonical specification first: system OpenSSH alias authority,
  native prompt boundary, no OpenEvo credential/trust database, explicit v1
  rebind, changed-key rules, and Subscription-only 0.1.9 capability.
- [ ] Change the repository boundary from `/desktop/v1/*` and Core `/v1/*` to
  negotiated current-major routes while retaining the rule that React talks
  only to authenticated Desktop Local API routes.
- [ ] Mark the v1 architecture as frozen historical behavior, not the 0.1.9
  release path. Define v2 distinct identities and no generic revision field.
- [ ] Update #158/#189 references in docs as historical directions; do not claim
  the issues were edited or closed.
- [ ] Run GREEN, Ruff where applicable, and `git diff --check`.
- [ ] Commit: `docs: define v0.1.9 system OpenSSH authority`

### Task 2: Add a regression for the Tahoe unsigned-runtime mismatch

**Files:**

- Modify: `desktop/src-tauri/tauri.conf.json`
- Modify: `desktop/src-tauri/tauri.release.conf.json`
- Modify: `tests/ci/test_build_sidecar.py`
- Modify: `tests/ci/test_check_openevo_release.py`
- Modify: `scripts/ci/check_openevo_release.py`
- Modify: `scripts/ci/openevo_release_candidate.py`
- Modify: `.github/workflows/openevo-desktop-candidate.yml`

- [ ] Write failing tests which load the merged Tauri release configuration and
  require `bundle.macOS.hardenedRuntime` to be exactly `false` for the unsigned
  Preview.
- [ ] Add a final-app verifier test with fixture output proving that ad-hoc
  signatures are valid, no Developer ID Team is claimed, the sidecar lacks the
  runtime flag, and disable-library-validation is absent.
- [ ] Add negative fixtures for runtime+ad-hoc and broad entitlement cases.
- [ ] Run focused tests and record RED against the current default-true config.
- [ ] Set the release composition to `hardenedRuntime=false`; do not weaken
  executable/archive/FD/resource checks and do not add the entitlement.
- [ ] Extend candidate manifest/evidence schema with the closed signing/runtime
  policy and update validator fixtures atomically.
- [ ] Run focused tests and `git diff --check`.
- [ ] Build the local packaged sidecar and run
  `scripts/ci/smoke_openevo_desktop_sidecar.py` against the exact output.
- [ ] Commit: `fix(desktop): align unsigned sidecar runtime policy`

### Task 3: Classify stock embedded-Python loader failures

**Files:**

- Modify: `desktop/src-tauri/src/main.rs`
- Modify: `desktop/src-tauri/src/desktop_log.rs`
- Modify: `scripts/ci/smoke_openevo_desktop_sidecar.py`
- Modify: `scripts/ci/smoke_openevo_desktop_bundle.py`
- Modify: `scripts/ci/smoke_openevo_desktop_launchservices.py`
- Modify: `tests/ci/test_sidecar_startup_diagnostics.py`
- Modify: `tests/ci/test_smoke_openevo_desktop_launchservices.py`
- Modify: `tests/ci/test_check_openevo_release.py`

- [ ] Add Rust and Python failing tests for a bounded synthetic PyInstaller
  library-validation line containing secret, URL, and absolute-path canaries.
- [ ] Require the stable closed result
  `stage=embedded_python_loader`,
  `code=python_shared_library_validation_failed`; assert no raw line/canary is
  retained.
- [ ] Add negative tests for near-matches and over-budget output.
- [ ] Implement one shared bounded classifier used by native log and smoke
  evidence. Preserve allowlisted `OPENEVO_STARTUP_V1` handling.
- [ ] Record unknown output as count/category/fingerprint only.
- [ ] Run Rust/Python focused tests and a direct failure injection.
- [ ] Commit: `fix(desktop): classify embedded Python loader failure`

### Task 4: Version the complete startup diagnostic envelope

**Files:**

- Modify: `desktop/src-tauri/src/desktop_log.rs`
- Modify: `desktop/src-tauri/src/main.rs`
- Modify: `desktop/sidecar/release_app.py`
- Modify: `desktop/sidecar/release_provider.py`
- Modify: `scripts/ci/smoke_openevo_desktop_bundle.py`
- Modify: `scripts/ci/smoke_openevo_desktop_launchservices.py`
- Modify: `tests/ci/test_sidecar_startup_diagnostics.py`
- Modify: `tests/ci/test_smoke_openevo_desktop_launchservices.py`
- Modify: `tests/openevo/sidecar/test_release_local_api.py`

- [ ] Write failing tests for attempt ID, monotonic stage sequence, last-complete
  stage, first-failed stage, bounded duration bucket, OS/build/architecture,
  app-location, quarantine, and translocation categories.
- [ ] Add separate injections for bootloader, Python entry, state store, Local
  API bind, and renderer bootstrap; assert distinguishable codes.
- [ ] Implement schema-v2 diagnostic events without raw paths or environment.
- [ ] Preserve a bounded export across relaunch and add strict migration from
  old log envelopes.
- [ ] Run focused tests, secret/path scan, and `git diff --check`.
- [ ] Commit: `feat(desktop): persist bounded startup stage diagnostics`

---

## Phase B: Define the next contract major

### Task 5: Define Core Control API v2 identity models first

**Files:**

- Create: `src/openevo/backend/contracts/v2/__init__.py`
- Create: `src/openevo/backend/contracts/v2/models.py`
- Create: `src/openevo/backend/contracts/v2/app.py`
- Create: `src/openevo/backend/contracts/v2/snapshots.py`
- Create: `tests/backend/test_core_control_v2_contract.py`
- Modify: `src/openevo/backend/contracts/__init__.py`

- [ ] Write failing strict-model tests for:
  `ProjectHeadRefV2`, `EvolutionRevisionRefV2`,
  `RuntimeContextSnapshotRefV2`, `EffectiveExecutionSnapshotRefV2`,
  `WorkspaceSnapshotRefV2`, `TaskAdmissionRefV2`, `AttemptRefV2`, and
  `SuccessorTransitionRefV2`.
- [ ] Assert every model is closed, strict, bounded, immutable, and rejects a
  generic `revision`, host path, URI, env, secret, or open dict.
- [ ] Add cross-field tests binding project/generation/digests and immutable
  task-attempt ownership.
- [ ] Implement the minimal models and canonical byte/digest helpers.
- [ ] Define `/v2` routes for system/status, projects, heads, transitions,
  tasks/admissions/attempts, timelines/logs/context, artifacts, services,
  diagnostics, maintenance, and events. Contract-only handlers return 501.
- [ ] Generate canonical OpenAPI and event schema in Task 6; do not hand-edit
  snapshots in this task.
- [ ] Run focused tests and commit:
  `feat(core): define distinct v2 authority identities`

### Task 6: Freeze Core v2 OpenAPI and event snapshots

**Files:**

- Create: `src/openevo/backend/contracts/v2/openapi.json`
- Create: `src/openevo/backend/contracts/v2/events.schema.json`
- Modify: `src/openevo/backend/contracts/v2/snapshots.py`
- Modify: `tests/backend/test_core_control_v2_contract.py`
- Modify: `scripts/ci/check_openevo_release.py`

- [ ] Add failing snapshot/digest tests and malicious payload tests for unknown
  fields, type coercion, recursive depth, oversized JSON, invalid event IDs,
  identity drift, and cursor/idempotency/ETag failures.
- [ ] Generate snapshots through the model app using deterministic canonical
  JSON. Review the generated diff; never edit generated JSON manually.
- [ ] Add release checks requiring v2 digests and forbidding v1 mutation
  features in the 0.1.9 manifest.
- [ ] Run focused contract/release tests and commit:
  `test(core): freeze control API v2 schemas`

### Task 7: Define Desktop Local API v2 and system-OpenSSH profiles

**Files:**

- Create: `desktop/sidecar/contracts/v2/__init__.py`
- Create: `desktop/sidecar/contracts/v2/models.py`
- Create: `desktop/sidecar/contracts/v2/app.py`
- Create: `desktop/sidecar/contracts/v2/canonical.py`
- Create: `desktop/sidecar/contracts/v2/openapi.json`
- Create: `desktop/sidecar/contracts/v2/events.schema.json`
- Create: `tests/openevo/sidecar/test_desktop_contract_v2.py`
- Modify: `desktop/sidecar/contracts/__init__.py`

- [ ] Write failing tests for `SshHostHintV2`, `SshHostCatalogV2`,
  `SystemOpenSshProfileCreateV2`, `RemoteWorkspaceProfileV2`,
  `LegacyExplicitProfileV2`, prompt/trust states, typed actions, and all Core v2
  resource projections.
- [ ] Assert the profile request contains only display name and literal alias;
  reject host/user/port/key path/auth kind/proxy command/known-host path.
- [ ] Assert responses never expose config paths, effective SSH commands,
  credential refs, Core URLs/tokens, remote paths, or generic revisions.
- [ ] Define catalog list/rescan, profile create/rebind/connect/disconnect,
  changed-key review, project/task, transition, artifact, service, diagnostic,
  and event routes under `/desktop/v2`.
- [ ] Generate and freeze strict OpenAPI/event snapshots; add Python and Zod
  digest fixtures in later renderer task.
- [ ] Run focused tests and commit:
  `feat(desktop): define Local API v2 system SSH contract`

### Task 8: Add discovery negotiation and release fail-closed policy

**Files:**

- Modify: `src/openevo/backend/contracts/v1/app.py`
- Modify: `src/openevo/backend/contracts/v2/app.py`
- Modify: `desktop/sidecar/release_app.py`
- Modify: `desktop/sidecar/release_capabilities.py`
- Modify: `desktop/release-contract.json`
- Modify: `tests/backend/test_core_control_v2_contract.py`
- Modify: `tests/openevo/sidecar/test_release_local_api.py`
- Modify: `tests/ci/test_check_openevo_release.py`

- [ ] Write failing negotiation tests for supported majors, exact OpenAPI/event
  digests, build/release IDs, feature set, registry identity, and mutation
  compatibility.
- [ ] Make unversioned discovery read-only and bounded. Do not let a v1 response
  satisfy a v2 mutation session.
- [ ] Configure the 0.1.9 renderer/provider composition to require v2 and reject
  simulator, scaffold, dry-run, direct Core URL, and route fallback.
- [ ] Keep v1 mounted only for read-only migration/diagnostic tests, not release
  mutation.
- [ ] Run focused tests and commit:
  `feat(desktop): negotiate v2 release authority`

---

## Phase C: System OpenSSH connection path

### Task 9: Build a bounded OpenSSH host catalog

**Files:**

- Create: `desktop/sidecar/ssh_config_catalog.py`
- Create: `tests/openevo/sidecar/test_ssh_config_catalog.py`
- Modify: `desktop/sidecar/release_provider.py`
- Modify: `desktop/sidecar/contracts/v2/models.py`
- Modify: `desktop/sidecar/contracts/v2/app.py`
- Modify: `desktop/sidecar/README.md`

- [ ] Write RED fixtures for literal `Host`, multiple tokens, comments,
  quoting, static relative/absolute `Include`, globs, cycles, duplicate aliases,
  wildcard/negated hosts, `Match`, overlong lines, oversized files, too many
  files/aliases, unreadable files, and hostile UTF-8.
- [ ] Assert catalog output contains aliases and safe warnings only; no source
  path or config text.
- [ ] Implement a lexical parser with immutable aggregate budgets. Never run
  `ssh -G`, `Match exec`, ProxyCommand, or a shell during catalog load.
- [ ] Add explicit bounded manual-alias validation for non-enumerable patterns.
- [ ] Expose list/rescan through the authenticated v2 provider with idempotent
  rescan and stable catalog generation.
- [ ] Run focused tests, Ruff, and commit:
  `feat(desktop): discover configured OpenSSH aliases`

### Task 10: Introduce alias-native SSH profile and pure argv builders

**Files:**

- Modify: `src/openevo/deployment/profile.py`
- Modify: `src/openevo/deployment/ssh.py`
- Modify: `src/openevo/deployment/system_executables.py`
- Modify: `src/openevo/deployment/__init__.py`
- Create: `tests/openevo/remote/test_system_openssh_alias.py`
- Modify: `tests/openevo/remote/test_ssh_transport.py`

- [ ] Write failing tests requiring `/usr/bin/ssh` plus the literal alias and
  forbidding `-F`, `-p`, `-l`, `-i`, `IdentityFile`, `IdentitiesOnly`,
  user/global known-host overrides, auth selection, and route flattening.
- [ ] Define separate closed builders for probe/command, upload, owned master,
  control command, and Core tunnel.
- [ ] Add tests for exact allowlisted environment including `HOME`, locale,
  inherited `SSH_AUTH_SOCK`, askpass variables, and deterministic system path;
  reject arbitrary inherited secrets.
- [ ] Add controlled OpenSSH experiments/tests for user-config forwards and
  `ClearAllForwardings`; prove that it also erases command-line `-L`, then
  freeze the safe owned-master `-W` Core channel from observed supported-client
  behavior.
- [ ] Implement alias-native profile/transport types without deleting the v1
  explicit transport yet. Make the new v2 provider select only the new type.
- [ ] Preserve redaction and bounded timeout/error mapping.
- [ ] Run focused old/new transport tests and commit:
  `feat(deployment): execute system OpenSSH aliases`

### Task 11: Add the native askpass helper artifact

**Files:**

- Modify: `desktop/src-tauri/Cargo.toml`
- Modify: `desktop/src-tauri/Cargo.lock`
- Create: `desktop/src-tauri/src/askpass.rs`
- Create: `desktop/src-tauri/src/bin/openevo-ssh-askpass.rs`
- Modify: `desktop/src-tauri/build.rs`
- Modify: `desktop/src-tauri/tauri.conf.json`
- Modify: `desktop/packaging/build_sidecar.py`
- Modify: `tests/ci/test_build_sidecar.py`
- Modify: `scripts/ci/openevo_desktop_daemon_resource.py`

- [ ] Write Rust unit tests for prompt classification, byte limits, yes/no,
  secure-secret result, cancellation, unknown/repeated/concurrent prompts,
  one-use capability, parent/ancestor identity, and connection generation.
- [ ] Write packaging tests requiring the exact helper in app inventory with
  architecture, mode, digest, and ad-hoc signature; reject symlink/replacement.
- [ ] Add only the minimal AppKit bindings needed for an accessory native alert
  and `NSSecureTextField`. The helper must never invoke `osascript`, a shell, or
  a renderer command.
- [ ] Keep prompt classification and dialog invocation behind a narrow trait so
  unit tests use a deterministic fake while release code has exactly one
  AppKit implementation.
- [ ] Implement the helper so secrets go directly to stdout and no secret is
  sent to the sidecar authorization broker.
- [ ] Stage the target-triple helper as a Tauri external binary and include its
  digest in build/candidate metadata.
- [ ] Run Rust, packaging, and archive inventory tests.
- [ ] Commit: `feat(desktop): bundle sealed OpenSSH askpass helper`

### Task 12: Implement the sidecar askpass broker and owned SSH master

**Files:**

- Create: `desktop/sidecar/askpass_broker.py`
- Create: `desktop/sidecar/system_ssh_session.py`
- Create: `tests/openevo/sidecar/test_askpass_broker.py`
- Create: `tests/openevo/sidecar/test_system_ssh_session.py`
- Modify: `src/openevo/deployment/ssh.py`
- Modify: `desktop/sidecar/release_provider.py`
- Modify: `desktop/src-tauri/src/main.rs`

- [ ] Write RED tests for private runtime directory, short control-socket path,
  PID/birth/process-group/socket identity, single-use prompt capabilities,
  generation cancellation, reconnect, and no ambient ControlMaster adoption.
- [ ] Prove helper ancestry for direct SSH and ProxyJump descendant shapes.
- [ ] Implement the broker with bounded local IPC and HMAC/capability replay
  protection. Prompt response bytes must never enter the broker.
- [ ] Start one owned master per connection and route commands/uploads/tunnel
  through its exact socket. Ensure app shutdown's sidecar process-group cleanup
  remains the final owner.
- [ ] Add hard-deadline cleanup and failure-injection tests for child exit,
  socket replacement, PID reuse, cancellation, and poisoned state.
- [ ] Run focused Python/Rust tests and commit:
  `feat(desktop): own interactive system SSH sessions`

### Task 13: Implement first and changed host-key flows

**Files:**

- Modify: `src/openevo/deployment/host_keys.py`
- Modify: `src/openevo/deployment/ssh.py`
- Modify: `desktop/sidecar/system_ssh_session.py`
- Modify: `desktop/sidecar/release_provider.py`
- Create: `tests/openevo/remote/test_system_host_keys.py`
- Modify: `tests/openevo/remote/test_host_keys.py`
- Modify: `tests/openevo/sidecar/test_release_local_api.py`

- [ ] Write RED tests for first-host ask, accept, cancel, policy-forbidden,
  changed key, repeated change, algorithm/fingerprint bounds, and secret/path
  redaction.
- [ ] Add `ssh -G` safety classification tests: one ordinary writable
  `UserKnownHostsFile`, multiple files, `KnownHostsCommand`, global-only trust,
  ambiguous `HostKeyAlias`, `Match exec`, and unsupported output.
- [ ] Implement first-host confirmation in the helper and changed-key typed
  failure in the sidecar.
- [ ] Implement the explicit review action using exact `/usr/bin/ssh-keygen`
  only for the proven simple trust-store case, then reconnect through the
  normal first-host flow. Never write an OpenEvo known-host file.
- [ ] Fail closed with an in-app administrator action for ambiguous policies.
- [ ] Run focused tests and commit:
  `feat(desktop): mediate system host trust safely`

### Task 14: Prove the production SSH boundary with local sshd

**Files:**

- Create: `scripts/ci/run_desktop_system_ssh_integration.py`
- Create: `tests/ci/test_desktop_system_ssh_integration.py`
- Modify: `.github/workflows/openevo-desktop-candidate.yml`
- Modify: `docs/maintainer/testing.md`

- [ ] Create a hermetic fixture with temporary host/user keys, controlled agent,
  encrypted key, password account where supported, known-host file, direct
  server, jump server, and ProxyCommand wrapper.
- [ ] Tests must invoke production alias/session builders and exact
  `/usr/bin/ssh`, not a Python SSH client.
- [ ] Cover direct agent, `IdentityFile`, passphrase askpass, password askpass,
  ProxyJump, ProxyCommand, first key, changed key, command, upload, Core tunnel,
  master reuse, cancellation, and complete cleanup.
- [ ] Prove ambient credentials and ambient ControlMaster are not used.
- [ ] Make the required macOS release gate fail closed when its fixture
  substrate is unavailable; keep unsupported-platform unit tests deterministic.
- [ ] Run locally on the target Mac and commit:
  `test(desktop): gate real system OpenSSH workflows`

---

## Phase D: Production v2 Daemon authority

### Task 15: Seal the Subscription effective-execution snapshot

**Files:**

- Modify: `src/openevo/backend/runtime_identity.py`
- Modify: `src/openevo/backend/run_admission.py`
- Modify: `src/openevo/evolution/revisions.py`
- Create: `src/openevo/backend/subscription_snapshot_issuer.py`
- Create: `tests/backend/test_subscription_snapshot_issuer.py`
- Modify: `tests/evolution/test_revision_admission.py`

- [ ] Write RED tests proving ordinary callers cannot construct a verified
  snapshot and the issuer binds Codex harness identity, subscription model,
  transcript capture, false token metrics, managed runtime digest, task-network
  policy, no serving endpoint, producer identity, and canonical digest.
- [ ] Reject token capture, host path, URI, environment, credentials, arbitrary
  runtime/model dicts, and unavailable managed-runtime identity.
- [ ] Implement the sealed production issuer through the same private
  publication discipline as the store testkit, without exposing a public
  constructor/injection hook.
- [ ] Wire genesis/settings transitions for the Subscription profile only.
- [ ] Keep Self-Deployed typed unavailable.
- [ ] Run focused admission/runtime tests and commit:
  `feat(core): issue verified subscription snapshots`

### Task 16: Implement immutable v2 Task and Attempt ownership

**Files:**

- Modify: `src/openevo/backend/science_run_store.py`
- Modify: `src/openevo/backend/science_run_owner.py`
- Modify: `src/openevo/backend/run_control.py`
- Modify: `src/openevo/backend/contracts/v2/models.py`
- Create: `tests/backend/test_science_task_v2.py`
- Modify: `tests/backend/test_science_run_owner.py`

- [ ] Write RED tests that unresolved successors/settings/rebind/workspace
  transitions return not-ready before creating any Task/admission/attempt.
- [ ] Test one immutable admission pinning exact project head, workspace,
  evolution revision, runtime context, effective execution, registry, and
  normalized intent.
- [ ] Test infrastructure retry appends a numbered Attempt without changing any
  pin; conflicting idempotency or terminal mutation fails closed.
- [ ] Implement the v2 run owner using one transaction for readiness and
  admission. Do not map a queued internal ledger row to a user Task.
- [ ] Keep existing v1 provider behavior isolated and unavailable to the v2
  release route.
- [ ] Run focused owner/store tests and commit:
  `feat(core): own immutable v2 tasks and attempts`

### Task 17: Atomically publish the successor Project Head

**Files:**

- Modify: `src/openevo/backend/science_run_owner.py`
- Modify: `src/openevo/backend/science_execution.py`
- Modify: `src/openevo/evolution/revisions.py`
- Modify: `src/openevo/evolution/context_projection.py`
- Modify: `src/openevo/evolution/context_materialization.py`
- Create: `tests/backend/test_science_successor_v2.py`
- Modify: `tests/backend/test_science_run_owner.py`
- Modify: `tests/evolution/test_context_materialization.py`
- Modify: `tests/evolution/test_revision_admission.py`

- [ ] Write RED success tests for completed transcript, sealed dataset, all
  enabled verified methods outside inference, typed artifacts, materialized
  context, accepted workspace result, one Evolution Revision, one Runtime
  Context Snapshot, and one adjacent successor Project Head.
- [ ] Write fault tests for method, output validation, materialization,
  workspace, DB commit, crash/recovery, and concurrent next-task submission.
- [ ] Assert every failure leaves the predecessor active, exposes no partial
  successor, and keeps the next task not ready.
- [ ] Implement one run-owner transition coordinator around existing verified
  registry/scanner/materializer/store primitives. Do not change algorithm
  functions or introduce target-ID branches.
- [ ] Add a second-session test proving runtime injection consumes the committed
  context and cannot affect the producing session.
- [ ] Run focused and broader evolution regressions; compare protected source
  files to the base commit.
- [ ] Commit: `feat(core): commit atomic science successors`

### Task 18: Publish the Core v2 provider and events

**Files:**

- Create: `src/openevo/backend/contracts/v2/provider.py`
- Create: `src/openevo/backend/contracts/v2/store.py`
- Modify: `src/openevo/backend/service.py`
- Modify: `src/openevo/backend/contracts/v2/app.py`
- Create: `tests/backend/test_core_control_v2_provider.py`

- [ ] Write RED provider tests for project/head/task/attempt/transition,
  capabilities, validation, timeline/log/context, artifacts, services,
  diagnostics, events, idempotency, ETag, cursor replay, reconnect, and
  authority drift.
- [ ] Bind provider responses to the new run owner/store. Return explicit 503
  for genuinely unfinished features; never synthesize success.
- [ ] Publish recoverable typed events for task admitted, attempt appended,
  dataset sealed, transition progressed/failed, evolution revision committed,
  runtime context committed, and project head activated.
- [ ] Require exact framework registry and v2 schema digests before mutation.
- [ ] Run provider/contract tests and commit:
  `feat(core): serve authoritative control API v2`

### Task 18A: Close the executable project, workspace, and admission contract

**Files:**

- Modify: `src/openevo/backend/contracts/v2/models.py`
- Modify: `src/openevo/backend/contracts/v2/app.py`
- Modify: `src/openevo/backend/contracts/v2/openapi.json`
- Modify: `desktop/sidecar/contracts/v2/models.py`
- Modify: `desktop/sidecar/contracts/v2/app.py`
- Modify: both v2 Local API snapshots
- Modify: `tests/backend/test_core_control_v2_contract.py`
- Modify: `tests/openevo/sidecar/test_desktop_contract_v2.py`

- [ ] Write RED tests proving project create/update carries complete closed
  canonical Science configuration and Core, rather than Desktop, computes its
  digest. Reject digest-only authority, env, setup commands, host paths, URIs,
  credentials, unknown fields, unsafe integers, and oversized config.
- [ ] Add bounded resumable Core workspace-upload create/chunk/finalize/abort
  resources. Requests and responses expose only opaque identities, digests,
  sizes, counts, indexes, and state.
- [ ] Replace caller-authored task-envelope/workspace/normalized-intent digests
  with expected project/head/config CAS fields; require Core to derive every
  immutable admission input from saved authority.
- [ ] Project capability projection must carry the complete verified remote
  envelope needed for generic method configuration, not only target IDs and a
  digest.
- [ ] Regenerate both exact OpenAPI snapshots and update release digests only
  through deterministic generators.
- [ ] Run Core/Desktop contract tests and commit:
  `feat(contract): close executable v2 science requests`

### Task 18B: Own project configuration, workspace snapshots, and genesis

**Files:**

- Create: `src/openevo/backend/project_authority_v2.py`
- Create: `src/openevo/backend/workspace_store_v2.py`
- Modify: `src/openevo/backend/contracts/v2/store.py`
- Modify: `src/openevo/backend/contracts/v2/provider.py`
- Modify: `src/openevo/backend/science_run_store.py`
- Create: `tests/backend/test_project_authority_v2.py`
- Create: `tests/backend/test_workspace_store_v2.py`
- Modify: `tests/backend/test_core_control_v2_provider.py`

- [ ] RED-test private roots, exact schema/marker identity, no-follow archive
  extraction, cumulative budgets, duplicate/hardlink/symlink/special-file
  rejection, chunk retry, crash recovery, and final no-replace publication.
- [ ] Persist exact canonical project config bytes and authoritative desired
  settings. Create a real empty workspace snapshot for scratch or adopt one
  finalized upload; never accept a caller-created snapshot reference.
- [ ] Resolve verified Subscription service readiness and issue the production
  effective-execution snapshot before genesis. Build and atomically publish the
  empty Evolution Revision, verified Runtime Context Snapshot, and generation
  zero Project Head.
- [ ] Implement capability-backed project validation and task-request
  derivation. Validation and admission use the same frozen registry/compiler
  path and serialized-byte limits.
- [ ] Test settings/registry/runtime drift as typed not-ready with no Task or
  partial head, restart recovery, and exact idempotency/ETag behavior.
- [ ] Run focused and affected store/evolution tests and commit:
  `feat(core): own v2 project genesis and workspaces`

### Task 18C: Execute real Subscription attempts and successors

**Files:**

- Create: `src/openevo/backend/science_execution_v2.py`
- Create: `src/openevo/backend/science_successor_preparer_v2.py`
- Modify: `src/openevo/backend/science_run_owner.py`
- Modify: `src/openevo/backend/science_run_store.py`
- Modify: `src/openevo/backend/service_supervisor.py`
- Modify: `src/openevo/gateway/node.py`
- Modify: `src/openevo/gateway/server.py`
- Modify: `src/openevo/rollout/models.py`
- Create: `tests/backend/test_science_execution_v2.py`
- Create: `tests/backend/test_science_successor_preparer_v2.py`
- Modify: relevant Gateway/Rollout recovery tests

- [x] RED-test Task/Attempt progress, generation-bound run admission, verified
  terminal execution receipt, cancellation race, infrastructure retry, crash
  recovery, and one authoritative Attempt only.
- [x] Compile the immutable v2 admission directly into the managed Codex
  Subscription service graph. Do not create or mutate v1 project/run authority.
- [x] Before Gateway cleanup, publish a bounded no-follow workspace-result
  snapshot through an internal opaque, authenticated, one-owner handoff. Prove
  retry/restart cleanup and never expose a host path through public contracts,
  events, logs, or persisted task envelopes.
- [x] Build the production successor plan from the saved normalized project
  config and runner evidence, seal the transcript dataset, validate exact
  plan-bound method outputs, materialize the complete next-session context, and
  atomically commit the workspace/evolution/runtime successor.
- [x] Add a real two-session integration proving session N cannot consume its
  own outputs and session N+1 receives the committed context and workspace.
- [x] Run focused Gateway/rollout/evolution/run-owner tests and protected-source
  guards; commit:
  `feat(core): execute v2 subscription tasks`

### Task 18D: Make the release Daemon a real v2 composition

**Files:**

- Modify: `src/openevo/backend/launcher.py`
- Modify: `src/openevo/backend/service.py`
- Modify: `src/openevo/backend/contracts/v1/provider.py`
- Create: `tests/backend/test_daemon_v2_composition.py`
- Modify: Daemon bundle and release-contract checks under `tests/ci/`

- [x] RED-test the packaged launcher through the inherited release socket and
  prove `/version`, authenticated `/v2/*`, provider kind, feature set, schema
  digests, registry/runtime identity, project genesis, Task execution, events,
  reconnect, and shutdown against production owners.
- [x] Bind the launcher to the v2 provider, project/workspace authority,
  production executor/preparer, service supervisor, and private run-admission
  endpoint. The ready payload and authenticated status proof bind the v2 build.
- [x] If v1 is mounted, expose only an explicitly negotiated read-only migration
  surface. No v1 mutation owner, shared business URL, or fallback is available
  in the 0.1.9 release composition.
- [x] Advertise `atomic_successor_v2` and other mutation features only when the
  concrete production owners are present and startup recovery is complete.
- [x] Run launcher/bundle/service/provider tests and commit:
  `feat(daemon): launch the production v2 authority`

---

## Phase E: Sidecar v2 state, tunnel, and migration

### Task 19: Add a separate v2 provider store and read-only v1 import

**Files:**

- Create: `desktop/sidecar/provider_store_v2.py`
- Create: `desktop/sidecar/legacy_v1_import.py`
- Create: `tests/openevo/sidecar/test_provider_store_v2.py`
- Create: `tests/openevo/sidecar/test_legacy_v1_import.py`
- Modify: `desktop/sidecar/release_runtime.py`

- [x] Write RED tests for a new private state namespace/schema fingerprint,
  atomic DDL/migration, crash recovery, budgets, inode/path replacement,
  retained v1 state, corrupt/oversized v1 rows, and unrelated startup.
- [x] Import v1 profiles as non-connectable `legacy_explicit` records only.
  Require explicit alias rebind for a new v2 profile.
- [x] Copy draft intent only after v2 validation; never adopt cached v1 remote
  authority or generic revision data.
- [x] Implement exact retry/idempotency/ETag semantics and startup recovery.
- [x] Run focused store tests and commit:
  `feat(sidecar): migrate Preview state into v2 safely`

### Task 20: Implement strict Core v2 client and durable bridge

**Files:**

- Create: `desktop/sidecar/core_client_v2.py`
- Create: `desktop/sidecar/core_bridge_v2.py`
- Create: `desktop/sidecar/core_bridge_store_v2.py`
- Create: `desktop/sidecar/core_bridge_adapters_v2.py`
- Create: `desktop/sidecar/event_broker_v2.py`
- Create: `tests/openevo/sidecar/test_core_client_v2.py`
- Create: `tests/openevo/sidecar/test_core_bridge_v2.py`
- Create: `tests/openevo/sidecar/test_core_bridge_store_v2.py`
- Create: `tests/openevo/sidecar/test_core_bridge_adapters_v2.py`
- Create: `tests/openevo/sidecar/test_event_broker_v2.py`

- [x] Port strict bounded JSON/SSE validation, copy-on-write cache authority,
  generation sealing, global close capacity, idempotency, and replay ledgers
  from v1 without aliasing v1 models.
- [x] Write RED malicious-upstream tests before each ported behavior.
- [x] Persist exact Desktop project/profile/head mapping with distinct identity
  fields and reject any drift or context-dependent generic revision.
- [x] Use only the active project's system-SSH tunnel. Never fall back to a
  shared backend URL or launcher URL.
- [x] Map remote errors to the closed Local API without Pydantic, URL, token,
  host path, or command leakage.
- [x] Run focused bridge/client tests and commit:
  `feat(sidecar): bridge strict Core v2 authority`

### Task 21: Wire v2 release provider and remote lifecycle

**Files:**

- Create: `desktop/sidecar/release_provider_v2.py`
- Modify: `desktop/sidecar/release_app.py`
- Modify: `desktop/sidecar/remote_lifecycle.py`
- Modify: `desktop/sidecar/release_runtime.py`
- Modify: `desktop/sidecar/workspace_imports.py`
- Create: `tests/openevo/sidecar/test_release_local_api_v2.py`
- Create: `tests/openevo/sidecar/test_release_core_routing_v2.py`

- [ ] Write RED end-to-end Local API tests for catalog, profile/rebind,
  connect/prompt/trust, bootstrap, exact Daemon compatibility, tunnel, project,
  task, transition, artifact, diagnostic, disconnect, reconnect, and restart.
- [ ] Require current profile/project/session generations at every mutation.
- [ ] Keep SSH authority limited to inspection, bundle stage/ensure,
  activation/rollback, tunnel, and manifest-bound maintenance while Core cannot
  start.
- [ ] Prove every post-compatibility business action calls v2 Core and that
  injected Core failures do not call SSH.
- [ ] Mount only v2 mutation routes in release; v1 remains read-only migration
  input outside renderer reach.
- [ ] Run focused provider/routing tests and commit:
  `feat(sidecar): expose the v2 remote workspace workflow`

---

## Phase F: Renderer and product composition

### Task 22: Generate strict TypeScript v2 schemas and client

**Files:**

- Create: `desktop/src/api/v2/schemas.ts`
- Create: `desktop/src/api/v2/client.ts`
- Create: `desktop/src/api/v2/sse.ts`
- Create: `desktop/src/api/v2/index.ts`
- Create: `desktop/src/api/v2/schemas.test.ts`
- Create: `desktop/src/api/v2/client.test.ts`
- Create: `desktop/src/api/v2/sse.test.ts`
- Modify: `desktop/src/product/releaseContract.ts`
- Modify: `desktop/src/product/releaseProvider.ts`

- [ ] Generate/implement Zod models from the canonical v2 contract and pin
  OpenAPI/event digests. Do not import v1 models into v2.
- [ ] Write RED tests for unknown fields, unsafe integers, identity drift,
  invalid discriminators, bounded collections, event replay, and secret/path
  canaries.
- [ ] Require `/desktop/v2` and exact release negotiation. Remove release
  fallback to v1/fixtures while preserving explicit preview/test builds.
- [ ] Run Vitest/typecheck and commit:
  `feat(renderer): consume Desktop Local API v2`

### Task 23: Build the configured-host Add Remote Workspace UI

**Files:**

- Modify: `desktop/src/product/DesktopProductApp.tsx`
- Modify: `desktop/src/product/DesktopProductApp.test.tsx`
- Modify: `desktop/src/product/localApiProvider.ts`
- Modify: `desktop/src/app/connectionState.ts`
- Modify: `desktop/src/styles.css`
- Modify: `desktop/src/product/README.md`

- [ ] Write RED interaction/accessibility tests: Add Remote Workspace opens
  immediately, loads aliases, displays partial warnings, permits bounded manual
  alias, rescans, creates/connects, reports native prompt pending/cancelled,
  reviews changed key, retries, and rebinds a legacy profile.
- [ ] Assert there are no IP/user/port/key/password fields and no secret value
  enters DOM or request fixtures.
- [ ] Implement keyboard/focus/modal behavior and typed recovery actions.
- [ ] Keep both built-in examples discoverable and prove the setup sheet does
  not trigger network work until connect.
- [ ] Run focused Vitest, typecheck, accessibility checks, and commit:
  `feat(desktop): add configured-host workspace setup`

### Task 24: Render distinct task/head/evolution authority

**Files:**

- Modify: `desktop/src/product/DesktopProductApp.tsx`
- Modify: `desktop/src/product/DesktopProductApp.test.tsx`
- Modify: `desktop/src/product/sessionOutputIdentity.ts`
- Modify: `desktop/src/product/runRetryRecovery.ts`
- Modify: `desktop/src/product/scientificProjectSampleData.ts`
- Modify: `desktop/tests/product-browser/release-live-capture.test.ts`
- Modify: `desktop/tests/product-browser/release-readonly.pw.ts`

- [ ] Write RED tests that distinguish Task, Attempt, Project Head generation,
  Evolution Revision, Runtime Context, Effective Execution, and transition.
- [ ] Test not-ready draft behavior before Task creation, immutable retry pins,
  transition failure/retry, and relaunch recovery.
- [ ] Update built-in examples to teach distinct head/revision identities while
  remaining renderer-owned, read-only, and offline.
- [ ] Implement the v2 view models and remove generic revision wording.
- [ ] Run Vitest/typecheck/build/Playwright readonly tests and commit:
  `feat(desktop): render v2 task and evolution authority`

### Task 25: Bump exact 0.1.9 product identities

**Files:**

- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Modify: `desktop/package.json`
- Modify: `desktop/package-lock.json`
- Modify: `desktop/src-tauri/Cargo.toml`
- Modify: `desktop/src-tauri/Cargo.lock`
- Modify: `desktop/src-tauri/tauri.conf.json`
- Modify: `desktop/release-contract.json`
- Modify: release/version fixtures under `tests/ci/` and `desktop/src/`

- [ ] Add/adjust parity tests first and record RED.
- [ ] Change all product identities to exactly 0.1.9 in one mechanical patch;
  do not update unrelated dependencies.
- [ ] Regenerate only lockfile metadata required by the version change.
- [ ] Run version parity, package lock, cargo locked, and release-contract tests.
- [ ] Commit: `chore(release): set OpenEvo Desktop 0.1.9`

---

## Phase G: Integrated verification and release evidence

### Task 26: Run complete affected regression and security review

**Files:**

- Modify only files required by failures proven in this task.
- Update: `docs/maintainer/testing.md`
- Update: `docs/maintainer/macos-desktop-development-handoff.md`

- [ ] Run all focused tests named above from a clean process.
- [ ] Run full affected suites:

```bash
uv run pytest -q \
  tests/backend \
  tests/evolution \
  tests/openevo/remote \
  tests/openevo/sidecar \
  tests/ci/test_build_sidecar.py \
  tests/ci/test_sidecar_startup_diagnostics.py \
  tests/ci/test_check_openevo_release.py \
  tests/ci/test_openevo_release_candidate.py \
  tests/ci/test_desktop_system_ssh_integration.py
uv run ruff check src/openevo desktop/sidecar scripts/ci tests

cd desktop
npm test -- --run
npm run typecheck
npm run build:openevo
npm run test:product-browser:release-readonly

cd src-tauri
cargo fmt --check
cargo test --locked --release -- --test-threads=1
cargo clippy --locked --release --all-targets -- -D warnings
```

- [ ] Run dependency, license, secret, forbidden-route/text, archive inventory,
  exact system-executable, and protected-algorithm source guards.
- [ ] Inspect the complete diff manually for contracts, path handling,
  subprocess options/environment, logs, fallback behavior, state migrations,
  and protected files.
- [ ] Run `git diff --check` and verify no unrelated user files are staged.
- [ ] Obtain an independent `gpt-5.6-terra` high-effort review when the model is
  explicitly selectable; resolve every actionable finding and rerun affected
  gates.
- [ ] Commit only evidence/doc fixes if needed:
  `test(release): close v0.1.9 affected gates`

### Task 27: Prove a locally installed DMG on the target Mac

**Files:**

- Evidence stays untracked under a dedicated local evidence directory.
- Modify docs only after evidence is complete.

- [ ] Build exact local release inputs, packaged sidecar/helper, product web,
  Daemon Bundle resource, Tauri app, and unsigned DMG.
- [ ] Verify codesign/runtime/helper/app inventory and run mounted/copy/native
  smokes.
- [ ] Install through Finder/LaunchServices; do not replace the retained 0.1.8
  reproduction until its evidence is preserved.
- [ ] Test retained Preview state, clean state in a disposable macOS user,
  injected Retry, Add Remote Workspace, quit/relaunch, and no orphan process.
- [ ] Test the actual configured alias and any required IdentityFile,
  ProxyJump/ProxyCommand, first-host/passphrase/password interaction applicable
  to the controlled matrix.
- [ ] Connect to the qualifying server, install/attach exact local Daemon, and
  prove the active v2 tunnel with no SSH business fallback.
- [ ] Run the first `gpt-5.3-codex-spark` high-effort Subscription task, wait for
  the atomic successor, then run a second task and verify the first task's
  accepted context is injected only there.
- [ ] Quit/reopen/reconnect and compare authoritative remote state.
- [ ] Export diagnostics and scan credential/path/transcript canaries.
- [ ] Record commands, digests, timings, and failures in the handoff; clearly
  label all local-build evidence non-release.

### Task 28: Build and validate the immutable candidate

**Files:**

- Modify: `.github/workflows/openevo-desktop-candidate.yml` only for a proven
  candidate-pipeline defect.
- Modify: release evidence/validation scripts only test-first.
- Update: `docs/maintainer/release-process.md`

- [ ] Push the reviewed branch and open a PR with `Part of #131` or the restored
  scoped issue, docs list, tests, protected-algorithm statement, and exact
  acceptance boundary.
- [ ] Merge through the repository workflow; never publish unreviewed local
  bytes.
- [ ] Dispatch one unsigned candidate from the exact reviewed commit.
- [ ] Require manifest-bound DMG, Daemon Bundle, managed runtime, helper,
  OpenAPI/event digests, checksums, and evidence index.
- [ ] Download and verify every draft asset byte on the target Mac.
- [ ] Repeat the complete Task 27 installed-app and real-host two-session flow
  against those downloaded bytes.
- [ ] Any product failure requires a new commit/candidate; never replace assets
  in place. Infrastructure-only reruns follow canonical policy.
- [ ] Obtain final independent `gpt-5.6-terra` high-effort product/spec and
  release-risk reviews; resolve findings with a new candidate if source changes.

### Task 29: Publish and close the handoff

**Files:**

- Modify: `docs/maintainer/macos-desktop-development-handoff.md`
- Modify: `docs/maintainer/release-process.md`
- Modify: `docs/maintainer/testing.md`
- Modify: user installation/remote-workspace docs selected by `rg` at this step
- Modify: `README.md` only after immutable publication exists

- [ ] Record the exact Tahoe root cause and why unsigned no-hardened-runtime is
  durable for this composition.
- [ ] Record source commit, workflow run, release tag, DMG/Daemon/runtime/helper
  digests, Mac acceptance, host matrix, two-session evidence, and remaining
  unsupported flows.
- [ ] Publish the unchanged draft only through the guarded Preview workflow.
- [ ] Verify immutable public asset URLs and checksums before updating README.
- [ ] Confirm 0.1.8 remains historical and unmodified.
- [ ] Run final doc/link/release checks, `git diff --check`, clean status, and
  mark #131 only as `Part of` unless all canonical G1-G12 gates are genuinely
  complete.

---

## Stop conditions

Stop and request product-owner direction rather than improvising if any of
these occurs:

- system OpenSSH cannot preserve a required user routing/auth/trust directive
  without exposing it to React;
- the supported macOS OpenSSH cannot combine cleared unowned forwards with the
  exact owned Core tunnel safely;
- a native askpass prompt cannot be classified without handling a secret in
  sidecar/renderer memory;
- v2 would need to reinterpret a generic v1 revision or admit a Task before
  readiness;
- the production Subscription snapshot cannot be verified without persisting
  host paths, URI, env, or credentials;
- atomic successor publication would require changing a protected algorithm;
- the exact candidate differs from locally accepted source/composition;
- GitHub, signing, release, or reviewer permissions are required and denied.

Do not mark the work complete until Task 29 is satisfied and the exact 0.1.9
candidate—not merely local code—passes the target-Mac and real-host flow.
