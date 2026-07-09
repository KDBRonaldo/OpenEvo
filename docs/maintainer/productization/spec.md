# OpenEvo Pre-Release Productization Design

Tracked by #121.

## Purpose

OpenEvo needs a pre-release productization migration that presents the project as
a mature product rather than a historical Polar repository with prototype
Desktop and Dev Kit layers. The target product has two surfaces:

- **OpenEvo Desktop**: the ordinary-user macOS app for science workflows.
- **OpenEvo Core Backend**: the Python backend/runtime installed and operated on
  remote GPU servers.

There is no separate CLI or Dev Kit product surface in this design. Any command
entrypoints that remain are backend launchers, maintenance tools, CI tools, or
developer automation utilities. They are not presented as a standalone product.

## Non-Negotiables

- Remove `src/polar/` and `src/polar_evolution/` as implementation packages.
- Do not keep legacy `polar` or `polar_evolution` compatibility wrappers.
- Remove `polar` and `polar-evolution` console scripts.
- Remove public `POLAR_*` runtime environment variables.
- Remove public `/polar/session` runtime paths.
- Remove public `polar.session_completed` event names.
- Replace those identities with OpenEvo names:
  - `OPENEVO_*`
  - `/openevo/session`
  - `openevo.session_completed`
- Do not modify the logic of existing evolution algorithms.
- Do not rename existing method IDs.
- Treat any algorithm output difference as a migration bug unless it is only an
  allowed OpenEvo identity string replacement.

## Product Model

The final product relationship is:

```text
Local Mac
└── OpenEvo Desktop
    ├── Tauri/Rust native host
    ├── React UI
    ├── local sidecar/control facade
    └── SSH/provisioning client

Remote GPU Server
└── OpenEvo Core Backend
    ├── backend supervisor / launcher
    ├── gateway
    ├── rollout
    ├── evolution backend
    ├── evolution worker
    ├── model serving manager
    ├── runtime/session manager
    └── artifact/run state store
```

Desktop wraps Core Backend for ordinary users. Core Backend owns all execution,
runtime, trajectory, evolution, deployment, and artifact behavior.

## Target Repository Structure

The target repository should make the two product surfaces obvious:

```text
src/openevo/
  contracts/
  harness/
  runtime/
  gateway/
  rollout/
  trajectory/
  evolution/
  deployment/
  projects/
  experiments/
  backend/
  tools/
  capabilities.py

desktop/
  src/
  src-tauri/
  sidecar/
  packaging/

docs/
examples/
tests/
scripts/
.github/
```

`src/openevo/` is the Core Backend package. `desktop/` is the ordinary-user app.
The dependency direction is strict:

```text
desktop -> src/openevo
src/openevo -> no dependency on desktop
```

## Core Backend Responsibilities

Core Backend owns the business logic and runtime contracts:

- harness execution, starting with Codex;
- subscription transcript mode and self-deployed model mode;
- Docker/Apptainer/runtime session management;
- gateway, rollout, proxy, and session injection;
- token-level and transcript-level trajectory construction;
- events, datasets, jobs, workers, artifacts, context resolution;
- method registry and method metadata;
- experiment configuration, compilation, and run orchestration;
- science project schemas and compilation;
- remote SSH, preflight, bootstrap, workspace sync, service supervision;
- model serving management, including vLLM for self-deployed mode;
- artifact content, lineage, compatibility, scores, and promotion state;
- backend health, status, logs, and typed errors.

Core Backend must not depend on Desktop UI, Tauri, or Desktop sidecar code.

## Evolution Algorithm Preservation

This migration is productization and repository restructuring, not algorithm
development. It may move files, rename packages, update imports, update runtime
identity strings, and update tests/docs. It must not redesign or optimize
existing evolution algorithms.

The following must remain behaviorally stable:

- `text_memory_reflector`
- `text_memory_expel_reflector`
- `skill_bundle_reflector`
- `agent_system_reflector`
- `agent_system_history_reflector`
- `agent_system_pareto_reflector`
- `agent_system_gepa_reflector`
- `parametric_memory_register`
- `parametric_memory_lora_sft`
- OPSD helpers and evaluators
- trajectory-to-skill paths
- benchmark/evaluator filtering and leakage-guard semantics

Rules:

- Existing method IDs remain unchanged.
- Registry and metadata semantics remain unchanged.
- Input and output artifact types remain unchanged.
- Algorithm function bodies should be moved mechanically.
- Import paths may change.
- Runtime path/env/event strings may change from Polar to OpenEvo.
- Any semantic algorithm change requires a separate issue and explicit approval.

## Backend Lifecycle

Desktop interacts with a remote OpenEvo Backend, not individual low-level
gateway, rollout, worker, or vLLM commands.

The lifecycle is:

```text
1. Bootstrap remote host over SSH.
2. Install or update exact-version OpenEvo Core Backend.
3. Start remote openevo-backend.
4. Create an SSH tunnel to the backend API.
5. Use typed backend API for normal operation.
```

Once backend is running, it supervises internal services:

```text
openevo-backend
  -> gateway
  -> rollout
  -> evolution API
  -> evolution worker
  -> model server
  -> runtime containers
```

Desktop should not directly start or stop these internal components except
through backend API calls.

## Backend API Contract

Backend exposes typed JSON APIs. The exact URL shape can evolve during
implementation, but the required capabilities are:

```text
GET  /health
GET  /status
GET  /environment
POST /environment/doctor
POST /environment/repair

POST /projects
GET  /projects
GET  /projects/{id}
PATCH /projects/{id}

POST /runs
GET  /runs
GET  /runs/{id}
POST /runs/{id}/cancel
POST /runs/{id}/retry

GET  /runs/{id}/timeline
GET  /runs/{id}/logs
GET  /runs/{id}/artifacts

GET  /artifacts/{id}
GET  /artifacts/{id}/content
GET  /artifacts/{id}/diff

GET  /services
GET  /services/{id}/logs
POST /services/{id}/restart
POST /services/{id}/stop

GET  /capabilities
```

Backend returns typed objects, not raw shell output. Shell output may appear only
as sanitized diagnostic detail.

### Error Model

All user-visible backend errors use a stable structure:

```json
{
  "code": "docker_permission_denied",
  "message": "Docker is installed but the remote user cannot access it.",
  "severity": "blocking",
  "category": "environment",
  "retryable": false,
  "repair_action": "user_action_required",
  "details": {},
  "logs_ref": "..."
}
```

Allowed repair actions:

- `openevo_can_retry`
- `openevo_can_install`
- `openevo_can_reconfigure`
- `user_action_required`
- `unsupported`

This lets Desktop present clear next actions instead of opaque HTTP or shell
failures.

## Runtime And Data Contract

Runtime identity is OpenEvo-only:

```text
/openevo/session/...
/openevo/session/workspace
/openevo/session/evolution/

OPENEVO_EVOLUTION_CONTEXT
OPENEVO_MEMORY_FILE
OPENEVO_SKILLS_DIR
OPENEVO_AGENT_SYSTEM_FILE
OPENEVO_AGENT_SYSTEM_TARGET
OPENEVO_AGENT_SYSTEM_TARGETS
OPENEVO_ADAPTER_MERGE_SPEC

openevo.session_completed
.openevo/evolution/
```

Remote state layout:

```text
~/.openevo/
  backend/
    config.yaml
    backend.db
    logs/
  workspaces/
  projects/
  runs/
    <run-id>/
      experiment.json
      run.json
      timeline.jsonl
      services/
        logs/
        pids/
      evolution/
        evolution.db
        artifacts/
      artifacts/
```

Old runtime data is not required to remain compatible. This is a breaking
pre-release migration.

## Remote Deployment And Repair

Desktop should let a user connect a fresh GPU server and allow OpenEvo to do as
much setup as safely possible.

Bootstrap inputs:

- SSH host, port, user, and auth reference;
- workspace root;
- HTTP/HTTPS proxy;
- no-proxy list;
- pip index URL;
- Hugging Face endpoint and cache location;
- execution mode;
- Codex model or Hugging Face model ID;
- managed runtime profile or advanced custom runtime.

OpenEvo may automatically:

- create `~/.openevo`;
- create a Python environment or user-site install;
- upload/install the exact OpenEvo backend bundle;
- set process-level proxy environment;
- install Python dependencies;
- pull or build OpenEvo-managed runtime images;
- download Hugging Face snapshots;
- start, stop, or restart OpenEvo backend services;
- clear stale pid/log/tunnel state;
- retry transient network downloads.

OpenEvo must not automatically:

- modify Docker daemon configuration;
- install or mutate system packages;
- modify systemd;
- edit global shell profiles;
- require root without explicit future design;
- generate or upload user SSH private keys;
- perform Codex subscription login;
- bypass institutional network policies.

## Execution Modes

### Codex Subscription Transcript Mode

- Uses a subscription-authenticated Codex harness.
- Requires transcript capture.
- Does not call model APIs directly.
- Does not expose token-level metrics.
- Supports non-parametric evolution: text memory, skill bundle, agent system.
- Requires remote Codex CLI and valid remote Codex login.
- May stage Codex auth into runtime session paths without exposing auth content.

### Self-Deployed Mode

- Uses a remote self-deployed model serving path, initially vLLM.
- Requires a Hugging Face model ID or compatible model configuration.
- Supports non-parametric evolution.
- Provides deployment structure for future parametric memory work.
- Does not define new parametric algorithms in this spec.

## Desktop Architecture

Desktop is a real macOS app, not a passive WebView wrapper.

```text
desktop/
  src-tauri/
  src/
  sidecar/
  packaging/
```

### Tauri / Rust Host

Rust owns native app responsibilities:

- start, supervise, and stop local sidecar;
- allocate local ports or IPC channels;
- create and manage SSH tunnels;
- manage app lifecycle and crash recovery;
- read/write macOS Keychain entries;
- open native file and directory pickers;
- emit local notifications;
- collect app logs;
- support future auto-update, signing, and notarization.

Rust must not implement Core business logic or evolution algorithms.

### React UI

React owns user experience:

- first-run setup;
- project dashboard;
- remote profile and proxy editors;
- execution mode selection;
- workspace and task configuration;
- service health and logs;
- run monitor;
- evolution timeline;
- artifact preview and diff;
- user-facing troubleshooting.

React must not SSH directly, read raw secrets, or hardcode Core method tables.

### Local Sidecar

The local sidecar is a Desktop facade. It may:

- expose local Desktop APIs to React;
- call Core contracts;
- call Tauri-provided native capabilities through explicit interfaces;
- manage local Desktop state;
- translate remote backend typed responses into Desktop view models;
- shape user-facing errors.

It must not:

- implement a second method registry;
- implement experiment runner logic;
- implement remote backend service orchestration;
- interpret artifact/context semantics independently of Core;
- run evolution algorithms.

## Desktop User Experience

The intended ordinary-user flow:

```text
Install OpenEvo Desktop .dmg
-> launch app
-> create or select project
-> configure remote GPU server
-> configure proxy/network settings
-> run remote doctor/bootstrap
-> install/start remote backend
-> choose subscription or self-deployed mode
-> configure science task/workspace
-> start run
-> monitor services/logs/run status
-> inspect evolution timeline and artifacts
```

Desktop must not show demo-ready fixture state when sidecar or backend is not
connected. It must show the actual setup state and next action.

Desktop must avoid ordinary-user exposure of:

- worker leases;
- raw artifact roots;
- SQLite paths;
- method registry internals;
- Terminal Bench controls;
- raw backend mutation endpoints;
- hidden policy version strings.

Desktop may present user-facing concepts:

- memory updated;
- skill learned;
- instructions improved;
- remote service needs attention;
- model server is starting;
- Codex login is needed on the remote server.

## Evolution Visualization

Desktop should make evolution understandable through a run timeline:

```text
Run
  Round
    trajectories / transcripts
    dataset
    evolution jobs
      memory
      skill
      agent system
    promoted artifacts
    evaluation / outcome
```

Artifact UX requirements:

- memory preview and diff;
- skill `SKILL.md` summary and helper-file list;
- agent-system instruction diff and target path;
- lineage from task, round, dataset, and trajectory;
- promotion/review status when available;
- clear failure phase and next action;
- future space for parametric memory status in self-deployed mode.

Desktop renders Core-provided artifact content and metadata. It does not invent
new algorithm output fields for UI convenience.

## Repository Presentation

The repository should present OpenEvo Core Backend and OpenEvo Desktop. It
should not present historical Polar product identity.

Remove or move out of the release-facing repository:

- `README.polar.md`;
- Polar logo/assets;
- legacy Polar examples as default examples;
- internal presentation files;
- development-process specs and plans from the public docs path.

Historical development notes may remain only under explicit maintainer archive
paths such as `docs/maintainer/development-history/`,
`docs/maintainer/productization/`, or `docs/dev/`. These archives are not
ordinary-user or release-facing product documentation.

Recommended docs structure:

```text
docs/
  user/
    desktop-quickstart.md
    remote-server-setup.md
    proxy-and-network.md
    troubleshooting.md
  core/
    architecture.md
    backend-api.md
    runtime-contract.md
    evolution-contract.md
    method-integration.md
  maintainer/
    release-process.md
    testing.md
    repository-structure.md
    migration-notes.md
```

Recommended examples:

```text
examples/
  science-minimal/
  science-with-local-folder/
  self-deployed-model/
  backend-automation/
  research-benchmarks/
```

Research benchmark examples must be clearly marked as developer/research
examples, not ordinary-user Desktop quickstarts.

## Release Readiness Gates

### Desktop Release

The Desktop `.dmg` gate requires:

- Tauri app starts local sidecar;
- app enters real first-run/setup state;
- no demo-ready fallback when disconnected;
- Keychain or equivalent secret reference path;
- remote backend bootstrap smoke;
- SSH tunnel health smoke;
- service status/log rendering smoke;
- run timeline/artifact rendering smoke;
- `.dmg` build and launch smoke on macOS;
- signing, notarization, and update strategy documented.

### Core Backend Release

The Core Backend package gate requires:

- only `openevo` package identity;
- no `polar` or `polar-evolution` console scripts;
- no public Polar runtime contract;
- installed backend can start `/health`;
- capabilities endpoint returns method metadata;
- focused tests cover harness, runtime, gateway, rollout, trajectory,
  evolution, deployment, projects, experiments, backend;
- algorithm preservation tests pass.

### GitHub Repository Gate

Repository gate requires:

- clear Core Backend + Desktop README;
- OpenEvo-only workflow names and release artifacts;
- `CHANGELOG.md`;
- `CONTRIBUTING.md`;
- `SECURITY.md`;
- issue and PR templates updated for OpenEvo;
- docs link checks;
- examples dry-run or smoke checks where practical.

## Testing Strategy

Testing must prove two things:

```text
1. OpenEvo identity migration is complete.
2. Evolution algorithm behavior is preserved.
```

Test classes:

- identity tests for paths, env vars, event names, scripts, docs, packaged
  artifacts;
- Core focused tests for each module;
- algorithm preservation tests for method registry and method outputs;
- backend integration tests for health, status, services, runs, logs, artifacts;
- Desktop tests for sidecar lifecycle, first-run state, tunnel failure, timeline,
  artifact diff, service logs, keychain refs;
- release smoke tests for wheel/backend and `.dmg`.

The final identity guard scans the release-facing active surface. It may
allowlist explicit maintainer archives, migration/productization notes, and
tests that verify old names are gone; it must not allow legacy identity strings
in active product code, examples, user docs, release docs, workflows, package
metadata, or Desktop assets.

## Development Workflow

Development follows the repository process:

- base work on `stable`;
- issue-first for non-trivial changes;
- commit as `ivowang <ziyiwang@ieee.org>`;
- push promptly to the remote repository;
- use subagent-driven development for implementation phases;
- use fresh-context independent review subagents with `gpt-5.5` high effort at
  phase boundaries;
- run focused tests and `git diff --check`;
- manually review diffs before commit.

Reviewers must specifically check:

- no legacy package wrappers;
- no public Polar identity remains;
- no evolution algorithm logic changed;
- method IDs unchanged;
- runtime/data contract fully OpenEvo;
- Desktop is not a passive WebView wrapper;
- backend API is typed and user-recoverable;
- docs and examples present OpenEvo, not historical Polar.

## Out Of Scope

- Designing new evolution algorithms.
- Optimizing OPSD, parametric memory, trajectory-to-skill, or reflector logic.
- Backward compatibility for old pre-release Polar runtime data.
- Publishing a separate Dev Kit product surface.
- Making CLI the ordinary-user entrypoint.
- Full multi-platform Desktop support beyond macOS release readiness.
