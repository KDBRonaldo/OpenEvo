# OpenEvo Productization Execution Plan

Status: current execution backlog
Canonical design: `docs/maintainer/productization/spec.md`
Tracking issues: #131, #154, #163
Base branch: `stable`

## How To Use This Plan

This is a workstream backlog, not a second specification. The canonical spec
defines product behavior and release acceptance. This file records the order in
which we will close the current implementation gaps.

Implementation details are decided in the issue and PR that owns the affected
module. When a decision changes a product boundary or release criterion, update
the canonical spec. Otherwise update the relevant architecture doc, module
README, test, or workflow instead of expanding this plan.

## Exhibition Critical Path

Issue #163 temporarily makes Desktop delivery the execution priority. The
canonical release scope is unchanged, but work proceeds in this order:

1. Freeze and conformance-test Desktop Local API v1 and Core Control API v1 as
   defined in `docs/architecture/desktop-core-contract-v1.md`.
2. Build the ordinary-scientist React workflow against a deterministic,
   development-only contract simulator while the sidecar adapters are wired.
3. Complete native session security, Keychain-backed credential slots, SSH
   host verification, automatic bootstrap/doctor, and tunnel recovery.
4. Exercise the copied app against real Core for a Codex subscription science
   run, all three non-parametric artifact families, and next-session reuse.
5. Build and rehearse the unsigned DMG on a clean macOS user and the exhibition
   GPU server.

Other backend workstreams pause unless they are required to satisfy the frozen
Core Control API for that real Desktop flow. Contract simulators can unblock UI
work but cannot satisfy release or exhibition evidence. No step may modify the
protected evolution algorithms or replace the existing Polar-based execution,
capture, dataset/job/worker, artifact, context, or runtime path.

## Working Rules

- Work from `stable` on focused branches and merge through PRs linked to #131,
  #154, or a child issue.
- Commit as `ivowang <ziyiwang@ieee.org>` and push branches promptly.
- Do not modify protected evolution algorithm behavior.
- Preserve the OpenEvo gateway/rollout/runtime/trajectory/evolution/artifact
  architecture inherited from the legacy upstream; restructure packaging and
  access paths without redesigning those mechanisms.
- Keep Core, Desktop, and benchmark automation in their declared ownership
  boundaries.
- Write focused regression tests before behavior-changing fixes.
- Record tests and docs impact in every PR.
- At each workstream boundary, use fresh independent `gpt-5.6-sol` reviewers
  with high reasoning. Resolve findings before merge.
- Run `git diff --check` and inspect the complete diff before every commit.

Use executable tests, real build outputs, benchmark summaries, and concise PR
records to prove the work.

## Workstreams

| Workstream | Outcome | Depends on | Complete when |
| --- | --- | --- | --- |
| A. Protected and pluggable evolution | Protected methods are frozen, one target/method registry owns evolution planning, and Terminal Bench is standalone automation. | This spec | Behavior guards pass, existing methods use the plugin contracts unchanged, benchmark code is outside Core/Desktop, and three independent gates can run through Core. |
| B. Core Backend convergence | Remote Core owns real setup, services, runs, artifacts, pinned task revisions, and atomic next-revision activation. | A1/A2 contracts; can overlap with benchmark migration | Clean-install and integration tests pass for both execution modes, queued/not-ready admission, and all-or-nothing artifact reuse. |
| C. Desktop product maturity | A scientist can complete the workflow without CLI use. | Stable Core API slices from B | Packaged-app E2E covers setup, run, monitoring, artifacts, recovery, and diagnostics. |
| D. Repository, docs, and release engineering | Repository and release artifacts present a coherent External Beta. | A-C interfaces mostly stable | Active identity/docs checks pass and release workflow builds mutually consistent Core/DMG artifacts. |
| E. Release candidate validation | The exact release commit satisfies every canonical gate. | A-D | Three performance gates, two science E2Es, security/privacy, clean install, DMG smoke, and GitHub draft-release validation pass. |

Workstreams are sequencing and ownership tools, not product versions. B, C, and
D may proceed in parallel where their contracts are already stable.

## A. Protected And Pluggable Evolution

### A1. Freeze Protected Behavior And Architecture

Create focused behavior fixtures for `text_memory_expel_reflector`,
`skill_bundle_reflector`, `agent_system_gepa_reflector`, and the protected
gateway -> rollout/runtime -> capture/trajectory -> dataset/job/worker ->
artifact/context resolve -> runtime injection stages. Use historical source only
to resolve ambiguity; do not build a second specification out of source hashes.

Acceptance:

- GEPA candidate evaluation, best-result selection, and transition helpers that
  currently live in Terminal Bench files are classified as protected algorithm
  code;
- method IDs and artifact types are unchanged;
- fixtures exercise observable payload/artifact construction and selection;
- deliberate prompt/default/filter/artifact/selection changes fail focused tests;
- path/import-only relocation can pass after review;
- architecture probes fail if productization bypasses or replaces a protected
  data-flow stage.

### A2. Build The Pluggable Evolution Framework

Implement `EvolutionTarget`, `EvolutionMethod`, immutable per-run
`EvolutionPlan`, and one authoritative registry. Built-ins load explicitly and
research plugins fail closed under `docs/architecture/evolution-framework.md`.
Keep the existing job/lease/worker/artifact/store lifecycle.

Execute this migration in the following order:

| Step | Requirements | Deliverable |
| --- | --- | --- |
| `A2.1` | `PLUG-1`, `PLUG-2`, `PLUG-3`, `PLUG-4` | Define/test descriptors, ordered method input and invocation contracts, immutable plan identity, bounded config, handler input/output security, versioned capabilities, and a frozen registry. |
| `A2.2` | `PLUG-2`, `PLUG-5` | Register existing methods mechanically with stable IDs and freeze exact GEPA generation/round/objective/history/tie-break behavior. |
| `A2.3` | `PLUG-1`, `PLUG-2`, `PLUG-3` | Add generic project target selections, plan compilation, registry-driven worker dispatch and versioned capabilities on the existing job/lease/artifact path. |
| `A2.4` | `PLUG-4` | Replace target-specific context/runtime projection with validated handler contributions and add the generic Desktop renderer/config projection. |
| `A2.5` | `PLUG-1`, `PLUG-2`, `PLUG-3`, `PLUG-4`, `PLUG-5` | Cut over Core and Desktop, delete duplicate registries/target switches, run behavior/performance gates, and document method/target authoring. |

A2.3 is implemented as reviewable repository slices, not separate product versions:
1. Implemented: migrate callers to sole `evolution.targets`; preserve order and resolve `auto` from the pre-round dataset snapshot.
2. Implemented: load one verified executable registry; persist plan/job identity, filter methods, validate envelopes, and renew leases.
3. Implemented: project versioned capabilities from that registry and proxy remote Core without a local catalog fallback.
A2.3 adds no dual scheduler, fallback registry, or algorithm change. A2.4/A2.5 own generic handler/runtime integration, capability-driven Desktop configuration, and removal of remaining target-specific switches.
The A2.4 Desktop/config, verified handlers, scanner, projection, and internal materializer slices are implemented; strict v2/blob transport, Gateway cutover, and external-target acceptance remain.
The Store now has the first internal B3.1 ledger slice: canonical generation-zero revision identity, producer-sealed closed model/runtime/serving snapshots, allowlisted non-secret admission identity envelopes, transaction-time authoritative-closure validation, consistent authoritative revision/admission reads, durable exact-current pinning, restart-safe queued admission, historical unpinned cancellation, immutable terminal pins, active admission lease counts, and SQL-length-preflight row/byte-bounded B3/context recovery with exact-delta transition write parity. The Store computes persisted snapshot IDs/digests, but the repository has no production `VerifiedExecutionSnapshot` issuer; only the private testkit can issue test seals. This is not deployment/readiness attestation, and release admission remains fail closed until #160/B1 provides a verified deployment producer. It is not yet wired to the Core run owner, HTTP, Gateway, or Desktop and cannot commit a successor. The rest of B3.1 plus all B3.2-B3.5 transition, readiness, atomic activation, recovery, and client state remain backlog. The two-phase recovery budget claim covers store identity, context snapshots/materializations, and B3 ledger rows, not unchanged legacy job/artifact recovery paths.

A2 completion acceptance:

- registry tests cover graph/identity/immutability, ordered input and exact legacy job projection, config precedence, four-axis support, handler aggregation, and reachable closure;
- test plugins require no central switches, fail closed, and infer only through a Core harness service;
- remote-Core capabilities expose four separate support axes and stable reasons;
  combined release-profile labels map once and remain presentation only;
- Desktop consumes the remote Core projection and gives every target a toggle,
  friendly method selector, schema-driven editor, durable save/reload, and clear
  unsupported reason; it contains no method or target registry;
- an external test target crosses compiler, resolver, gateway, capabilities, and
  Desktop through an existing contribution/renderer kind with no target-ID branch;
- maintainer documentation gives one complete path for adding a method to an
  existing target and one for adding a target with a constrained handler; both
  examples are exercised as external test plugins rather than privileged
  built-in branches;
- GEPA fixtures preserve `PLUG-5` generation/round/history/objective/`None`/
  tie-break behavior; the separate project `auto` resolver preserves prior-dataset selection;
- the existing store/job/lease/worker/artifact/context regression suite and all
  three performance gates pass without a second scheduler or compatibility path.

### A3. Move Terminal Bench Automation Out Of Core

Create `benchmarks/terminal_bench/` as standalone automation. Keep protected
GEPA evaluation, selection, and transition inside its algorithm-owned method.
Mechanically move only Terminal Bench task acquisition,
Harbor execution, verifier adapter, materializer, reporting, task configuration,
and benchmark commands out of `src/openevo/`.

The migration calls Core dataset, job, worker, artifact, method, and protected GEPA contracts.
Revision admission and queued/not-ready handling remain for the next Issue #156 PR.
Unit tests may call methods directly; release runs cannot claim that contract yet.

Acceptance:

- Core and Desktop install/build inventories contain no Terminal Bench runner,
  scorer, task list, or benchmark command;
- old Core benchmark imports are removed rather than wrapped;
- migrated automation produces equivalent task inputs and result summaries;
- benchmark automation calls the protected Core selection/transition contract without duplicating or rewriting GEPA selection policy;
- a subsequent PR binds automation to revision admission and proves it cannot
  bypass queued/not-ready;
- algorithm-protection tests remain green.

### A4. Make Performance Runs Reproducible

Freeze the task lists and run configuration for the three independent gates.
The benchmark summary needs only the information required to audit pass@1:
task ID, selected artifact, attempt outcome, infrastructure rerun status, and
aggregate rescue count.

Skill bundle's 14/25 is an unverified, user-provided aggregate without per-task evidence; A3 did not run the gate, and every final candidate must rerun all 25 tasks and retain task-level results.

For agent-system GEPA, recover the candidate-generation, candidate-evaluation,
best-result selection, and transition rule from the pre-productization
implementation and 17/25 run records. These are protected algorithm semantics,
not product-layer policy. Preserve them exactly; Core, Desktop, and benchmark
automation consume the algorithm-selected output without reranking it.

Acceptance:

- textual memory can run the fixed 21 applicable tasks;
- trajectory-to-skill and agent-system can each run the fixed 25 tasks;
- one scored attempt is authoritative per task except recorded infrastructure
  replacement;
- the automation rejects any best-of-attempt/candidate behavior not present in
  the recovered historical gate policy;
- the agent-system gate blocks if its historical selection semantics cannot be
  recovered without guessing;
- the final thresholds remain 12/21, 14/25, and 17/25.

## B. Core Backend Convergence

### B1. Establish The Real Backend Boundary

Inventory current Desktop sidecar and backend code. Remove paths where Desktop
directly starts science runs or individual Core services over SSH. Keep only
pre-Core SSH transport/install/start, tunnel establishment, Core lifecycle, and
typed request forwarding in the sidecar.

Core must own persistent projects/runs, idempotent run creation, cancellation,
retry, service lifecycle, logs, timelines, and artifacts. Scaffold and in-memory
responses remain test-only and cannot be reported as release-ready behavior.

Acceptance:

- integration tests exercise Desktop-facing operations through Core API;
- a release-mode test fails if the sidecar invokes direct run/service commands;
- Core state survives backend restart;
- run ownership and retry/idempotency are deterministic.

### B2. Finish Bootstrap, Doctor, Repair, And Upgrade

Define one Core install artifact and descriptor. Before Core is available, the Desktop native host/sidecar uploads or downloads those exact bytes, verifies their checksum, installs them in an OpenEvo-owned environment, starts Core, and checks version compatibility. After health succeeds, Core owns doctor/repair, upgrade, services, and run execution.

Doctor and repair cover SSH, proxy, Python environment, container runtime, Codex availability/authentication, Hugging Face access, model download, vLLM, ports, state permissions, and stale services. Automatic repair remains within the boundaries in the canonical spec.

Create and validate the versioned reference lockfile at `src/openevo/projects/science/profiles/self-deployed-reference-v1.json`. It pins model ID and Hugging Face commit, vLLM version/argv, dtype/context length, host/port, GPU and minimum VRAM, disk/cache, proxy behavior, timeouts, health probe/served model, and compatible Core versions. Release-mode Desktop uses that profile unchanged; custom model IDs are explicitly best effort.

Acceptance:

- clean supported server setup succeeds without manual shell commands;
- China-mainland proxy/mirror configuration reaches remote package and model
  downloads;
- interrupted download/bootstrap can resume or retry safely;
- unsupported system/driver/root conditions return a specific user action;
- missing, mutable, incomplete, or runtime-divergent reference profile fields
  fail self-deployed readiness tests;
- upgrade failure retains a usable previous Core installation.

### B3. Complete Execution And Evolution Integration

Codex subscription enforces transcript capture; self-deployed runs manage the supported vLLM profile. Both seal datasets, run methods outside inference, and atomically activate the complete output set only for the next task. Implement issue #154 in these independently reviewable phases:

| Phase | Scope | Acceptance |
| --- | --- | --- |
| `B3.1 Revision identity and admission` | Persist immutable revision manifests that bind context, model, and adapter state; pin one revision when admitting a task. | Concurrent requests and retries use the pinned revision for the full task; mutation attempts fail; a task requiring an uncommitted successor receives a durable queued or typed not-ready state, never stale fallback. |
| `B3.2 Seal and dispatch` | Seal the completed task dataset, create one transition for all enabled targets, and dispatch evolution/training through independent workers. | Jobs accept only the sealed dataset snapshot; inference never executes evolution/training; slow parameter training cannot alter an active task; timing/background/barrier fields and streaming/in-session ABIs are rejected. |
| `B3.3 Stage and prove readiness` | Keep every enabled target output private while validating outputs, resolving/materializing the complete context, and preparing any required adapter/serving change. | Missing or failed outputs invalidate the transition; subscription rejects parametric/incompatible methods; self-deployed planning and execution fail closed when trainer, adapter-load, restart, or health-check requirements are unavailable. |
| `B3.4 Atomic activation and recovery` | Load adapters or restart serving when required without disturbing active revision leases, pass health checks, then publish outputs, materialization, and serving identity as one next revision. | One commit makes the complete revision admissible; failure/cancellation/crash exposes none, preserves the last committed revision, and leaves the required next task queued/not-ready; restart/retry and staged cleanup are verified. |
| `B3.5 Clients and cadence` | Expose revision/transition state through Core, Desktop, and benchmark-facing contracts and run end-to-end tests. | Desktop renders pinned, preparing, queued/not-ready, failed, and ready states; benchmark automation controls cadence only by requesting tasks and cannot bypass Core admission; E2E proves no task observes mixed context/model/adapter revisions. |

Acceptance:

- subscription without transcript capture is rejected;
- transcript records never claim token-level metrics;
- Core capabilities expose the three release-supported method IDs;
- context resolution rejects incompatible or stale artifacts;
- memory, skill, and agent-system payloads are visible to the harness before
  task execution;
- a task keeps one context/model/adapter revision through all inference requests;
- all enabled artifact families produce, render, promote, materialize, and commit as one next revision before follow-up reuse in each execution mode;
- incomplete evolution/training returns queued/not-ready and never admits a follow-up task with the previous or a partial revision;
- GEPA reuse consumes the protected algorithm-selected output; Core and Desktop
  do not substitute another candidate;
- selected artifacts, materialization, serving readiness, transition failure, and committed revision identity are inspectable through Core.

### B4. Harden API, Security, And Diagnostics

Converge the backend models and `docs/core/backend-api.md` together. Keep errors
typed, redact secrets before serialization, authenticate mutation requests, and
bind services only to local/tunneled interfaces.

Acceptance:

- API contract tests cover success, retryable failure, user-action failure, and
  version mismatch;
- canary credentials do not appear in API output, logs, or diagnostics;
- resolved credential canaries are redacted before backend DB, event, timeline,
  transcript, transcript-derived dataset, or complete artifact persistence,
  including registration records, manifests, metadata, lineage, compatibility,
  scores, tags, URIs, payloads, and rendered excerpts;
- security scans cover persisted state and user-shareable files without
  rewriting ordinary scientific task content;
- diagnostics are explicit exports and contain enough redacted state to debug
  bootstrap, service, run, and artifact failures;
- deletion cannot escape configured OpenEvo state/workspace paths.

## C. Desktop Product Maturity

### C1. Replace Prototype Navigation With The Science Workflow

The first screen is the actual project/run workspace, with first-run setup when
no server is configured. Remove benchmark/developer controls and fixture-backed
ready state from release builds.

Required views:

- projects and recent runs;
- remote profiles and secure credentials;
- proxy/network and execution-mode settings;
- doctor/bootstrap progress and recovery;
- science task/workspace/evolution configuration;
- capability-driven target toggles, friendly method selection, and schema-driven
  method settings without exposing internal IDs;
- run timeline, logs, services, cancellation/retry, pinned revision, next-revision transition, and queued/not-ready task state;
- memory, skill, and agent-system artifacts and diffs;
- diagnostics, data locations, deletion, and application settings.

Acceptance:

- every view has real empty/loading/offline/error/success states;
- ordinary users never need method IDs, worker details, benchmark concepts, or
  shell commands;
- the UI remains responsive during SSH, download, model startup, and long runs;
- reconnecting or restarting Desktop restores durable remote state;
- saving and reopening a project preserves each target's enabled state, selected
  method, and validated config; unsupported combinations show Core-provided reasons.

### C2. Complete Native Host And Local Security

Tauri/Rust owns sidecar lifecycle, secure local state, Keychain references,
native file selection, app logging, and clean shutdown/recovery. Release builds
reject developer backend URLs, raw command overrides, and dry-run transports.

Acceptance:

- sidecar crash/restart and tunnel loss have deterministic recovery;
- secrets remain outside React state, persisted project JSON, and diagnostics;
- app lifecycle tests cover first launch, normal quit, forced sidecar failure,
  and relaunch;
- accessibility and supported-window-size checks pass.

### C3. Package And Test The DMG

Build the app and bundled sidecar for supported macOS architecture. Bind the DMG
to the exact Core descriptor/artifact and test the copied application rather
than only Vite or a source checkout.

Acceptance:

- mounted and copied app starts from a clean user profile;
- unsigned Gatekeeper instructions match observed behavior;
- first-run through remote Core health succeeds;
- packaged resources contain no secrets, benchmark automation, development
  override, or stale web bundle;
- a clean sidecar build creates, validates, embeds, and archive-inspects the exact
  Core wheel without a checked-in launcher or pre-staged wheel;
- checksum generation is deterministic.

## D. Repository, Docs, And Release Engineering

### D1. Finish Repository Boundaries

Make `src/openevo/`, `desktop/`, and `benchmarks/` visibly match the canonical
architecture. Remove obsolete wrappers, duplicated implementations, stale
package data, and active legacy upstream identity. Historical records remain only under
clearly marked maintainer history paths.

Acceptance:

- source/package identity audit passes;
- Core does not import Desktop or benchmark modules;
- Desktop does not own Core evolution/experiment logic;
- package and DMG inventories match their intended surfaces.

### D2. Make Documentation Match The Product

Update the README and user, Core, architecture, and maintainer docs while each
workflow is implemented. Delete or mark obsolete foundation instructions when
the replacement works; do not leave two current paths.

Acceptance:

- a new user can install the unsigned app, configure both modes, run a science
  task, inspect artifacts, export diagnostics, delete data, and uninstall using
  docs alone;
- typed errors link to useful troubleshooting actions;
- no user doc presents CLI/devkit or benchmark automation as a product;
- links and example smoke tests pass.

### D3. Replace Disabled Release Workflows

Keep release/PyPI publishing disabled until the real workflow is implemented.
The replacement builds and tests exact Core and DMG bytes, writes checksums,
creates a draft GitHub Release, downloads it again, and verifies its contents
before publication. PyPI remains disabled for this release.

Acceptance:

- release jobs cannot publish from an unreviewed branch or failing gate;
- final DMG, Core descriptor/artifact, checksums, and release notes agree on
  version and commit;
- dependency lock, vulnerability, and license checks cover shipped Python,
  npm, and Rust dependencies;
- rollback and failed-candidate behavior are documented and tested where
  practical.

## E. Release Candidate Validation

Run all gates from one candidate commit and retain the direct outputs:

1. protected-method and source-boundary tests;
2. textual-memory 12/21 performance gate;
3. trajectory-to-skill 14/25 performance gate;
4. agent-system 17/25 performance gate;
5. clean Core install and both execution-mode integration tests;
6. Codex subscription science E2E for text memory, skill, and agent-system;
7. self-deployed reference science E2E for text memory, skill, and agent-system;
8. all six mode/family cells render, promote, materialize, atomically commit,
   and reuse the selected artifact in the next task, with queue/not-ready and
   failure-path checks that prohibit stale or partial revisions;
9. packaged DMG lifecycle, recovery, security, privacy, and diagnostics tests;
10. repository identity, docs, dependency, checksum, and draft-release checks.

Any failed gate blocks the candidate. Infrastructure failures may be rerun only
when the failure prevented the behavior from being evaluated and the rerun is
recorded. Product or benchmark failures are fixed in code and start a new
candidate.

Before publication, two fresh-context `gpt-5.6-sol` high-effort reviewers
independently review:

- product/spec compliance and unsupported claims;
- release risk, algorithm preservation, artifacts, and test evidence.

## Immediate Execution Order

Continue from the implemented A1/A2.3 and the completed internal A2.4 slices:

1. Complete the remaining A2.4/A2.5 strict transport, Gateway cutover, external
   target E2E, duplicate-registry/switch removal, and protected gates.
2. In parallel, mechanically move A3 Terminal Bench automation out of Core
   without wrappers or method changes, and implement B1/B2 plus B3.1-B3.5.
   Mechanical A3 migration may use the current public Core surface, but it is
   not accepted as complete before B3 revision admission exists.
3. Bind the migrated automation to B3 admission/queued/not-ready contracts,
   then complete A3 equivalence and all three performance gates.
4. Reconnect Desktop through the durable B1 project/run/service APIs and the
   B3 revision state, while finishing both release execution-mode E2Es.
5. Expand the mature Desktop workflow and evidence until both science E2Es and
   every D/E release gate pass on the same release commit.

This vertical path is the first product milestone. Broader UI polish,
self-deployed automation, all artifact viewers, packaging, and final benchmark
runs build on it rather than being designed in isolation.

## PR And Verification Template

Each implementation PR records:

```text
Issue: Fixes/Part of #...
Workstream: A/B/C/D/E
Behavior changed:
Protected algorithm impact: none / requires preservation rerun
Tests run:
Docs updated:
Remaining follow-up:
```

Use the smallest focused command set that proves the change, then broaden tests
for shared contracts. Typical suites include:

```bash
pytest tests/test_evolution_agent_harnesses.py -q
pytest tests/trajectory -q
pytest tests/evolution -q
pytest tests/gateway/test_evolution_integration.py -q
pytest tests/backend -q
pytest tests/ci -q

cd desktop
npm test -- --run
npm run build
npm run build:sidecar
cd src-tauri
cargo test --locked

git diff --check
```

Do not treat this list as a substitute for testing the actual changed workflow.
