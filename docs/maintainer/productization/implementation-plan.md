# OpenEvo Productization Execution Plan

Status: current execution backlog

Canonical design: `docs/maintainer/productization/spec.md`

Tracking issue: #131

Base branch: `stable`

## How To Use This Plan

This is a workstream backlog, not a second specification. The canonical spec
defines product behavior and release acceptance. This file records the order in
which we will close the current implementation gaps.

Implementation details are decided in the issue and PR that owns the affected
module. When a decision changes a product boundary or release criterion, update
the canonical spec. Otherwise update the relevant architecture doc, module
README, test, or workflow instead of expanding this plan.

## Working Rules

- Work from `stable` on focused branches and merge through PRs linked to #131 or
  a child issue.
- Commit as `ivowang <ziyiwang@ieee.org>` and push branches promptly.
- Do not modify protected evolution algorithm behavior.
- Preserve the existing Polar-derived gateway/rollout/runtime/trajectory/
  evolution/artifact architecture and data flow; restructure packaging and
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
| A. Algorithm protection and benchmark boundary | Protected methods are frozen and Terminal Bench is standalone automation. | This spec | Source/behavior guards pass, benchmark code is outside Core/Desktop, and three independent gates can run through Core. |
| B. Core Backend convergence | Remote Core owns real setup, services, runs, artifacts, and injection. | A contract freeze; can overlap with benchmark migration | Clean-install and integration tests pass for both execution modes and promoted artifact reuse. |
| C. Desktop product maturity | A scientist can complete the workflow without CLI use. | Stable Core API slices from B | Packaged-app E2E covers setup, run, monitoring, artifacts, recovery, and diagnostics. |
| D. Repository, docs, and release engineering | Repository and release artifacts present a coherent External Beta. | A-C interfaces mostly stable | Active identity/docs checks pass and release workflow builds mutually consistent Core/DMG artifacts. |
| E. Release candidate validation | The exact release commit satisfies every canonical gate. | A-D | Three performance gates, two science E2Es, security/privacy, clean install, DMG smoke, and GitHub draft-release validation pass. |

Workstreams are sequencing and ownership tools, not product versions. B, C, and
D may proceed in parallel where their contracts are already stable.

## A. Algorithm Protection And Benchmark Boundary

### A1. Freeze Protected Behavior And Architecture

Create a small, reviewable baseline for:

- `text_memory_expel_reflector`;
- `skill_bundle_reflector`;
- `agent_system_gepa_reflector`;
- their prompts, defaults, filters, candidate/selection helpers, and artifact
  construction paths.
- gateway -> rollout/runtime -> capture/trajectory -> dataset/job/worker ->
  artifact/context resolve -> runtime injection ownership and contract probes.

The baseline may be a normalized source manifest plus focused behavioral
fixtures. It must detect semantic drift without failing on mechanical file moves
or import rewrites. Compare it with the best available pre-productization commit
or archived source, not only the already-reorganized current branch.

Acceptance:

- each protected method has an explicit source and dependency inventory;
- GEPA candidate evaluation, best-result selection, and promotion helpers that
  currently live in Terminal Bench files are classified as protected algorithm
  code;
- method IDs and artifact types are unchanged;
- fixtures exercise representative output shape and selection semantics;
- a deliberate prompt/default/filter/method-body change fails the guard;
- path/import-only relocation can pass after review.
- architecture probes fail if productization bypasses or replaces a protected
  data-flow stage.

### A2. Move Terminal Bench Automation Out Of Core

Create `benchmarks/terminal_bench/` as standalone automation. First separate the
protected GEPA candidate evaluation/best-result selection/promotion helpers from
benchmark transport without changing their normalized behavior, and place them
behind an algorithm-owned Core boundary. Then mechanically move Terminal Bench
task acquisition, Harbor execution, verifier adapter, materializer, reporting,
task configuration, and benchmark commands out of `src/openevo/`.

The automation calls public Core contracts for datasets, jobs, workers,
artifacts, context resolution, injection, and harness runs. Unit tests may call
methods directly, but release performance runs may not.

Acceptance:

- Core and Desktop install/build inventories contain no Terminal Bench runner,
  scorer, task list, or benchmark command;
- old Core benchmark imports are removed rather than wrapped;
- migrated automation produces equivalent task inputs and result summaries;
- benchmark automation calls the protected Core selection/promotion contract
  and contains no duplicate or rewritten GEPA selection policy;
- algorithm-protection tests remain green.

### A3. Make Performance Runs Reproducible

Freeze the task lists and run configuration for the three independent gates.
The benchmark summary needs only the information required to audit pass@1:
task ID, selected artifact, attempt outcome, infrastructure rerun status, and
aggregate rescue count.

For agent-system GEPA, recover the candidate-generation, candidate-evaluation,
best-result selection, and promotion rule from the pre-productization
implementation and 17/25 run records. These are protected method semantics, not
product-layer policy. Preserve them exactly; Core, Desktop, and benchmark
automation consume the method-selected promoted artifact without reranking it.

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

Define one Core install artifact and descriptor. Before Core is available, the
Desktop native host/sidecar uploads or downloads those exact bytes, verifies
their checksum, installs them in an OpenEvo-owned environment, starts Core, and
checks version compatibility. After health succeeds, Core owns doctor/repair,
upgrade, services, and run execution.

Doctor and repair cover SSH, proxy, Python environment, container runtime,
Codex availability/authentication, Hugging Face access, model download, vLLM,
ports, state permissions, and stale services. Automatic repair remains within
the boundaries in the canonical spec.

Create and validate the versioned reference lockfile at
`src/openevo/projects/science/profiles/self-deployed-reference-v1.json`. It pins
model ID and Hugging Face commit, vLLM version/argv, dtype/context length,
host/port, GPU and minimum VRAM, disk/cache, proxy behavior, timeouts, health
probe/served model, and compatible Core versions. Release-mode Desktop uses
that profile unchanged; custom model IDs are explicitly best effort.

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

Codex subscription runs must enforce transcript capture. Self-deployed reference
runs must manage or connect to the supported vLLM profile. Both paths create
datasets, run the selected canonical evolution methods, register artifacts, and
inject promoted artifacts into a later harness session.

Acceptance:

- subscription without transcript capture is rejected;
- transcript records never claim token-level metrics;
- Core capabilities expose the three release-supported method IDs;
- context resolution rejects incompatible or stale artifacts;
- memory, skill, and agent-system payloads are visible to the harness before
  task execution;
- all three artifact families complete produce, render, promote, stage, and
  follow-up reuse in each of the two execution modes;
- GEPA product promotion uses the protected method's own best-result selection;
  Core and Desktop do not substitute another candidate;
- selected artifact IDs and staging results are inspectable through Core.

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
- run timeline, logs, services, and cancellation/retry;
- memory, skill, and agent-system artifacts and diffs;
- diagnostics, data locations, deletion, and application settings.

Acceptance:

- every view has real empty/loading/offline/error/success states;
- ordinary users never need method IDs, worker details, benchmark concepts, or
  shell commands;
- the UI remains responsive during SSH, download, model startup, and long runs;
- reconnecting or restarting Desktop restores durable remote state.

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
- checksum generation is deterministic.

## D. Repository, Docs, And Release Engineering

### D1. Finish Repository Boundaries

Make `src/openevo/`, `desktop/`, and `benchmarks/` visibly match the canonical
architecture. Remove obsolete wrappers, duplicated implementations, stale
package data, and active Polar identity. Historical records remain only under
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
8. all six mode/family cells render, promote, stage, and reuse the selected
   artifact in a later run;
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

The first implementation batch after this plan merges is:

1. Complete A1: inventory and freeze the three protected method paths against
   the best available pre-productization source.
2. Complete A2: move Terminal Bench automation out of Core without wrappers or
   method changes.
3. In parallel, inventory B1 direct sidecar execution and scaffold Core state;
   turn each concrete gap into a focused child issue.
4. Implement the smallest real Core run lifecycle slice used by Desktop, then
   connect one Desktop project/run flow to it.
5. Expand Core and Desktop vertically until one Codex subscription science run
   evolves and reuses one artifact end to end.

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
cd src-tauri
cargo test --locked

git diff --check
```

Do not treat this list as a substitute for testing the actual changed workflow.
