# OpenEvo Desktop and Dev Kit Architecture Design

Date: 2026-07-08

## Purpose

OpenEvo needs two product-facing wrappers and one shared backend:

- **OpenEvo Desktop**: a macOS desktop app for ordinary science users who want to run research tasks with OpenEvo.
- **OpenEvo Dev Kit**: a code, CLI, testing, and benchmark toolkit for OpenEvo developers.
- **OpenEvo Core**: the single source of truth used by both Desktop and Dev Kit.

The goal of this design is to make Desktop usable by non-CS researchers while keeping algorithm behavior, runtime behavior, artifact contracts, benchmark contracts, canonical data models, and the evolution flow in Core. Desktop must not become a second implementation of OpenEvo.

This design covers Codex-only execution, subscription mode, self-deployed remote inference mode, remote SSH lifecycle, non-parametric evolution, Desktop monitoring, Dev Kit workflows, and release synchronization.

This design does not specify concrete parameter-evolution algorithms, dynamic adapter loading, extra harness integrations, benchmark dashboards, or multi-user cloud control planes.

## Product Model

OpenEvo is organized as three layers:

```text
OpenEvo Desktop  -> ordinary science user app
OpenEvo Dev Kit  -> OpenEvo developer toolkit
OpenEvo Core     -> shared backend and evolution implementation
```

Desktop and Dev Kit are wrappers. They do not fork the execution model, method registry, artifact contract, remote lifecycle, or context injection logic.

### OpenEvo Core

Core owns:

- science project and experiment configuration models
- science task compilation
- execution mode definitions
- Codex harness integration
- remote SSH lifecycle
- managed runtime preparation
- model download and server startup
- rollout/session execution
- transcript and trajectory construction
- dataset creation
- evolution job scheduling
- method registry
- artifact registration
- promotion and context selection
- run summary and timeline models
- benchmark adapter contracts

### OpenEvo Desktop

Desktop owns:

- macOS native app packaging
- ordinary-user project setup
- remote SSH profile collection
- subscription/self-deployed mode selection
- server-side proxy configuration
- prepare/start/status controls
- run monitoring
- evolution timeline rendering
- artifact viewing
- user-facing failure summaries and remediation hints

Desktop must not own:

- evolution method implementations
- a hardcoded method table
- direct model API calls
- benchmark controls
- raw experiment internals as a primary workflow
- runtime image management as a normal science-user requirement

### OpenEvo Dev Kit

Dev Kit owns developer workflows:

- CLI entrypoints
- Python-facing Core facade
- method development helpers
- benchmark adapters
- local and remote run debugging commands
- artifact inspection tools
- run/timeline inspection tools
- fixtures and golden examples
- developer documentation

Dev Kit is for OpenEvo developers. It is not a hidden advanced mode inside Desktop.

## Ordinary User Desktop Flow

After installing the OpenEvo Desktop `.dmg`, a science user should be able to run a task without using `pip install`, editing source code, or manually preparing the remote execution server. A GPU server is required for self-deployed mode, but subscription mode only requires a remote server that can run Codex and OpenEvo.

The expected flow is:

1. Open OpenEvo Desktop.
2. Create a science project.
3. Enter a research objective and task source.
4. Configure a remote SSH server and workspace root.
5. Configure optional server-side proxy and package mirrors, plus model mirrors for self-deployed mode.
6. Choose subscription mode or self-deployed mode.
7. Choose non-parametric evolution targets.
8. Run prepare checks.
9. Start the run.
10. Monitor execution and evolution.
11. Inspect generated memory, skill, and agent-system artifacts.

### Science Project Inputs

Desktop collects:

- project name
- task id
- research objective
- task source
- remote profile
- execution mode
- enabled non-parametric evolution targets

Task sources are:

- local folder
- Git repository
- remote path
- scratch workspace

For local folders, Desktop should sync or snapshot the task into the remote workspace through Core. For Git repositories, Core should clone or update on the remote server. For remote paths, Core should validate path accessibility. For scratch workspaces, Core should create a clean remote task directory.

### Environment Inputs

Ordinary science users should not be required to provide a runtime image.

Core should prefer a managed runtime and should automatically detect common dependency declarations such as:

- `requirements.txt`
- `pyproject.toml`
- `environment.yml`
- common setup instructions when safely discoverable

If automatic dependency setup fails or is ambiguous, Desktop may ask the user for additional setup commands. This is a fallback for science tasks, not a developer console.

Custom runtime images are a Dev Kit or internal workflow. They should not appear in the normal Desktop science path.

## Execution Modes

OpenEvo supports two Codex-only modes in this design. The difference is the model source used by Codex on the remote server.

For the Desktop science workflow and the covered ordinary-user-visible Core modes, OpenEvo must not directly call model APIs to get task or reflection responses.

This no-direct-model-call rule applies to the Desktop science workflow and to Core methods that are marked as ordinary-user-visible. For the modes covered by this design, task execution and non-parametric reflection must both go through the Codex harness. Direct OpenAI-compatible worker providers, if they remain during migration, are legacy or internal research paths for Dev Kit use only. They must not be selected by Desktop, by ordinary-user-visible Core methods, or by the default Core compilation path for the covered execution modes.

### Subscription Mode

Subscription mode assumes the remote server already has Codex installed and logged in.

Subscription mode does not require a GPU or a Hugging Face model download. It only checks the remote resources needed to run Codex, OpenEvo, and the task runtime.

Prepare checks include:

- SSH connectivity
- remote workspace access
- remote OpenEvo install or installability
- Codex CLI availability
- Codex subscription authentication
- transcript capture configuration
- required disk and runtime prerequisites

Execution path:

```text
Desktop -> local sidecar -> OpenEvo Core -> SSH -> remote OpenEvo Core
remote Core -> Codex CLI subscription run
Codex transcript -> text trajectory -> dataset
dataset -> non-parametric evolution jobs
promoted artifacts -> next-round context
```

Subscription mode is transcript-only. It must explicitly report that token-level metrics such as token ids, logprobs, and loss masks are unavailable.

Subscription mode supports:

- text memory evolution
- skill bundle evolution
- agent-system evolution

Subscription mode does not support parameter evolution in Desktop.

### Self-Deployed Mode

Self-deployed mode assumes the user has a remote GPU server and provides a Hugging Face model name.

`Self-deployed mode` is the product-facing name. Existing internal config values may temporarily use older names while the codebase migrates, but new Desktop labels, user documentation, saved project files, and public Dev Kit surfaces should use `self-deployed`.

Compatibility aliases:

```text
codex_managed_local_inference -> self-deployed
proxy auth used by self-deployed internals -> Codex harness through remote gateway/inference
```

During migration, Core may accept older saved config or API payload values and normalize them to `self-deployed` at the boundary. New public payloads should emit `self-deployed`.

Public boundaries include science project config files, sidecar request/response payloads, frontend API types, shell/status responses, CLI help text, and user documentation.

Core should prepare the remote server by:

- checking SSH connectivity
- checking GPU and disk availability
- setting server-side proxy environment variables
- installing the matching remote OpenEvo Core version
- installing required Python dependencies
- checking or installing Codex CLI
- downloading the Hugging Face model snapshot
- preparing the managed runtime
- starting the inference backend
- starting the gateway and run services
- configuring Codex to use the remote inference path

Execution path:

```text
Desktop -> local sidecar -> OpenEvo Core -> SSH -> remote OpenEvo Core
remote Core -> Codex CLI -> remote gateway -> remote inference server
Codex transcript and any Core-captured records -> dataset
dataset -> non-parametric evolution jobs
promoted artifacts -> next-round context
```

Self-deployed mode still uses Codex as the harness. Core must not bypass Codex and directly call the remote inference server for task or reflection responses.

Ordinary-user self-deployed reflection must select `codex_cli` or another Codex-harness path, even if internal fields still use names such as `proxy`. It must not select a direct OpenAI-compatible `openai_chat` provider. The Science/Desktop compilation path must set this explicitly and must not rely on old defaults such as `auth != subscription -> openai_chat`.

Token-level metrics in self-deployed mode may be exposed only when Core actually captured token ids, logprobs, and loss masks through the runtime path. The source of truth is trajectory/session capture metadata or the run summary, not the execution mode name. Desktop must otherwise show transcript/text capture only and must not infer token-level availability from the execution mode name.

Self-deployed mode supports ordinary-user non-parametric evolution. Parameter evolution remains a Core/Dev Kit extension area and is not part of the Desktop science workflow described here.

## Remote Lifecycle

Remote execution is modeled as a state machine exposed by Core and rendered by Desktop.

States:

```text
Not connected
Connected
Preflight running
Preflight failed
Preflight passed
Bootstrap running
Bootstrap failed
Bootstrap passed
Model preparing
Model failed
Model ready
Services starting
Services degraded
Services healthy
Run queued
Run running
Run completed
Run failed
Run canceled
```

Core must expose structured status, logs, and remediation data for each stage. Desktop should show a concise user-facing summary first, with expandable raw logs.

### Remote Workspace

The user provides a remote workspace root, for example:

```text
~/openevo-workspaces
/data/openevo
```

Core creates project snapshots, service manifests, run outputs, logs, model references, and artifacts under this root.

### Server-Side Proxy

Desktop exposes remote proxy and mirror fields:

- `HTTP_PROXY`
- `HTTPS_PROXY`
- `NO_PROXY`
- `PIP_INDEX_URL`
- `HF_ENDPOINT`
- `HF_HOME`

Core applies these values to remote bootstrap, package installation, Hugging Face downloads, model service startup, and OpenEvo services.

System-level proxy configuration, Docker daemon proxy configuration, Docker registry mirrors, and SSH bastion/proxy setup may require root or organization-specific policy. Core may detect these failures and provide remediation, but this design does not require automatic modification of remote system daemons.

### Remote Service Contract

Core should expose service operations through a stable lifecycle contract:

```text
start
status
health
logs
stop
restart
```

The implementation may initially use process ids and log files, but Desktop must talk to the service contract rather than implementation-specific process details.

## Non-Parametric Evolution Capability Model

Desktop must not hardcode evolution method names. Core exposes capabilities and method metadata.

Core capability data includes:

- execution modes
- artifact types
- evolution targets
- method ids
- method display names
- method descriptions
- method input requirements
- method config schema
- method defaults
- supported execution modes
- visibility
- stability level

Method metadata should support at least:

```text
method_id
display_name
artifact_type
description
input_requirements
supported_execution_modes
config_schema
default_config
stability_level
visible_in_desktop
```

Desktop shows only ordinary-user-visible non-parametric methods.

Supported Desktop artifact targets are:

- `text_memory`
- `skill_bundle`
- `agent_system`

Parameter-related artifacts may remain in Core contracts for developer work, but Desktop should not expose them as ordinary science-user options in this design.

### Method Development Contract

Developers add a method by:

1. implementing the method in Core
2. registering the method and metadata in the Core registry
3. defining config schema and defaults
4. adding focused tests
5. updating developer documentation
6. marking visibility according to maturity

If `visible_in_desktop` is true and the method is compatible with the selected execution mode, Desktop may render it automatically through Core capabilities.

## Desktop Information Architecture

Desktop is a Tauri macOS app with a local sidecar that calls OpenEvo Core.

Primary screens:

- Projects
- New Science Project
- Remote Setup
- Prepare
- Run Monitor
- Evolution Timeline
- Artifact Viewer

### Projects

The project list shows:

- project name
- objective summary
- execution mode
- remote profile label
- last run status
- last updated time

### New Science Project

The project wizard collects:

- research objective
- task source
- remote SSH server
- remote workspace root
- execution mode
- model name for self-deployed mode
- proxy and package mirror settings, plus model mirror settings for self-deployed mode
- non-parametric evolution targets

### Prepare

Prepare displays:

- SSH connection
- remote OpenEvo version
- Codex availability and authentication
- GPU and disk checks, with GPU checks required only for self-deployed mode
- dependency setup
- proxy and package connectivity
- model download status, shown only for self-deployed mode
- managed runtime status
- service startup status

### Run Monitor

Run Monitor shows each round:

- rollout status
- transcript or trajectory capture status
- dataset creation
- evolution jobs
- worker status
- artifact registration
- promotion decision
- next context preparation

### Evolution Timeline

Evolution Timeline shows:

- input context for each round
- task transcript or summary
- dataset summary
- candidate text memory
- candidate skill bundle
- candidate agent-system artifact
- promotion score or decision
- context injected into the next round

Users can open generated artifacts such as `memory.md`, `SKILL.md`, and `AGENTS.md` from Desktop.

Raw command lines, service ports, config paths, stdout/stderr, artifact ids, method ids, and internal store details should be grouped under diagnostics or expandable technical details. They should not be the primary ordinary-user cards or the only explanation for failures.

## Dev Kit Design

Dev Kit is for OpenEvo developers and benchmark authors.

Developer workflows include:

- adding new evolution methods
- debugging method inputs and outputs
- running local and remote smoke experiments
- inspecting artifacts
- inspecting context injection
- running science benchmarks
- running general agent benchmarks
- running developer benchmarks such as terminal-task suites
- writing regression tests

Benchmark adapters should only translate benchmark tasks and results into Core records, datasets, metrics, and run summaries. They must not implement a separate evolution backend.

Benchmark flow:

```text
benchmark task -> harness run -> Core records/datasets/metrics
Core datasets -> evolution jobs -> methods -> artifacts
artifacts -> evaluation/promotion -> next context
```

## Core API Boundary

Desktop should call local sidecar APIs backed by Core. The exact HTTP shape can evolve, but the API boundary should cover:

```text
GET  /api/capabilities
GET  /api/methods
GET  /api/projects
POST /api/projects
POST /api/remote/preflight
POST /api/remote/bootstrap
POST /api/remote/services/start
GET  /api/remote/services/status
GET  /api/remote/services/health
GET  /api/remote/services/logs
POST /api/remote/services/stop
POST /api/remote/services/restart
POST /api/runs
GET  /api/runs/{id}
GET  /api/runs/{id}/timeline
GET  /api/artifacts/{id}
```

These paths are logical API names. An implementation may keep a compatibility prefix during migration, and it may group lifecycle operations behind a service facade, but the stable contract is the semantic boundary: Desktop calls sidecar/Core capabilities and lifecycle operations, not internal implementation objects.

The API must return structured machine-readable status plus user-facing summaries. Raw logs should be available for diagnostics but should not be the only failure surface.

## Packaging and Version Synchronization

Desktop and Dev Kit must ship against the same Core version.

Release artifacts:

- OpenEvo Desktop `.dmg`
- OpenEvo Dev Kit package and CLI
- source distribution for development

The released ordinary-user Desktop artifact is a native macOS app packaged as a `.dmg`. A browser-served shell such as `openevo desktop serve` or `openevo desktop open` may remain as a Dev Kit debugging, smoke-test, or migration aid, but it is not the ordinary-user release format.

The Desktop bundle must include the matching Core version. Remote installation must install that exact Core version, for example by uploading a bundled wheel or installing a pinned released package. If exact-version installation is impossible, the prepare step must fail with a clear remediation instead of silently falling back to an unrelated latest version.

Release checks should verify:

- Tauri `.dmg` artifact is produced for the ordinary-user Desktop release
- Python package, bundled frontend/web assets, sidecar-reported Core version, Desktop bundle metadata, and Dev Kit CLI version agree
- Desktop sidecar reports the expected Core version
- Dev Kit CLI reports the expected Core version
- remote prepare or smoke tests report a remote Core version matching the Desktop-bundled Core version
- capability schema loads
- non-parametric method metadata loads
- subscription-mode experiment config validates
- self-deployed experiment config validates
- timeline schema can be produced from a smoke run or fixture

## Security and Credentials

Desktop handles sensitive local inputs:

- SSH host and username
- SSH key reference
- optional passphrase handling
- remote workspace path
- proxy URLs
- optional package/model mirror settings

Desktop should prefer system keychain or OS-supported secret storage for secrets. Plain project files should not contain private keys, API tokens, or subscription secrets.

OpenEvo does not require users to enter a direct model API key for the covered Codex-only flows. If future harness or provider support adds keys, those keys must still be treated as harness/runtime configuration, not as a reason for Core to directly call model APIs for task results.

## Acceptance Criteria

The design is satisfied when the architecture supports the following.

Ordinary user criteria:

- a user installs OpenEvo Desktop as a macOS app, not through `pip install`
- the user creates a science project in Desktop
- the user configures a remote SSH server and workspace root
- the user configures server-side proxy and mirror fields when needed
- the user chooses subscription or self-deployed mode
- subscription mode checks remote Codex availability and authentication
- self-deployed mode prepares dependencies, downloads the model, and starts remote services
- the user starts a run from Desktop
- the user monitors rollout, dataset, evolution job, artifact, promotion, and context status
- the user opens evolved memory, skill, and agent-system artifacts
- failures show stage, summary, logs, and remediation hints

Developer criteria:

- developers use Dev Kit, CLI, source code, and tests for OpenEvo development
- developers can add non-parametric methods through Core
- method metadata controls Desktop visibility
- benchmark adapters use the Core dataset/job/artifact/context contract
- Desktop and Dev Kit use the same Core version

Architecture criteria:

- Desktop does not implement an evolution method registry
- Desktop does not directly call model APIs
- ordinary-user-visible Core methods do not bypass Codex to request task or reflection responses in the covered modes
- subscription mode is transcript-only
- self-deployed mode uses Codex through the remote inference path
- token-level metrics are not claimed when unavailable
- parameter evolution is not exposed in the ordinary Desktop science workflow
- target product terminology uses OpenEvo Core, OpenEvo Desktop, and OpenEvo Dev Kit

## Current Code Migration Notes

The current repository already has useful foundations:

- science project/config models
- remote SSH and preflight foundations
- bootstrap and service startup foundations
- server-side proxy fields
- managed runtime and custom image concepts
- Hugging Face model download foundations
- local sidecar API foundations
- React Desktop shell foundations
- evolution method registry foundations
- dataset, job, artifact, and context flow foundations
- benchmark-related developer utilities
- wheel and CLI release workflows

Main gaps to close:

- package Desktop as a Tauri `.dmg` rather than a browser-only shell
- keep browser-served Desktop commands as Dev Kit/debugging tools only, if they remain
- make Desktop an ordinary-user science app, not a developer console
- add Core capability and method metadata APIs
- remove Desktop hardcoding of evolution target booleans
- ensure self-deployed non-parametric reflection uses Codex harness flow, not direct Core model API calls
- migrate public execution-mode naming to `self-deployed`, with any older internal enum treated as a compatibility alias
- report token-level metrics only when token ids, logprobs, and loss masks are actually captured
- expose remote lifecycle as structured status, logs, stop, and restart operations
- install the exact matching remote Core version and fail clearly when that cannot be done
- keep custom runtime images out of the ordinary Desktop science path
- formalize Dev Kit as the developer wrapper around Core
- align documentation and UI terminology around Core, Desktop, and Dev Kit

## Explicit Non-Goals

This design does not cover:

- concrete parameter-evolution algorithms
- dynamic adapter loading and unloading
- adapter promotion and rollback UX
- harness integrations beyond Codex
- benchmark dashboards in Desktop
- multi-user hosted service mode
- automatic modification of remote Docker daemon or system proxy settings
- guaranteed automatic repair of every science environment dependency

These areas should attach to Core contracts later without changing the Desktop/Core/Dev Kit boundary.
