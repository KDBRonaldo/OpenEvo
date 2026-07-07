# OpenEvo Desktop Science Design

## Goal

OpenEvo Desktop should let a non-developer run OpenEvo for scientific work from
their own computer while all heavy execution happens on a remote GPU server. The
user should not need to understand Polar, runtime images, rollout services,
gateway services, Docker Compose, or benchmark harness internals.

The first product surface is a science workflow app. It helps the user connect
to a remote server, prepare that server, define a research task, run Codex
through OpenEvo/Polar, and watch memory, skill, and agent-system evolution over
rounds.

Benchmarking is a developer concern. Terminal Bench and future benchmark
integrations remain available through developer tooling and presets, but they do
not appear in the ordinary user flow.

## Product Boundary

OpenEvo has two audiences with different surfaces:

| Audience | Surface | Responsibilities |
|---|---|---|
| Ordinary science user | OpenEvo Desktop Science Project | Connect a remote server, describe a scientific task, provide files or a repo, start and monitor a run. |
| OpenEvo developer / benchmark author | Developer mode, CLI, benchmark adapters | Build benchmark presets, wire benchmark runtimes, tune Docker images, add task generators, and validate leaderboard workflows. |

The default Desktop experience only exposes the science-user surface. Developer
mode can expose lower-level fields when needed, but those fields are not part of
the happy path.

## Non-Goals

The first Desktop release does not:

- Replace Codex as the agent harness. Codex is the only supported harness.
- Call model APIs directly from OpenEvo. OpenEvo always runs a harness through
  Polar/OpenEvo orchestration.
- Train LoRA or adapter weights. Parametric memory can be represented later as
  an artifact lifecycle, but training is out of scope for the first science
  Desktop.
- Guarantee that arbitrary domain-specific system dependencies can be installed
  without user input. OpenEvo provides managed defaults and setup hooks, then
  reports actionable failures when the environment is outside those defaults.
- Put Terminal Bench into the ordinary science-user flow.

## User Flow

The science user flow is:

1. Install and open OpenEvo Desktop on a local macOS or Linux machine.
2. Add a remote GPU server profile by entering SSH host, user, port, and auth
   method.
3. Unlock the local encrypted vault for secrets used by this app session.
4. Let OpenEvo run remote preflight checks over SSH.
5. Let OpenEvo initialize the remote server if checks fail for installable
   dependencies.
6. Create a Science Project.
7. Choose a task source:
   - local folder uploaded to a managed remote workspace;
   - Git repository and branch;
   - existing remote directory;
   - empty scratch workspace.
8. Write the research objective and optional setup commands.
9. Choose the execution mode:
   - Codex subscription transcript mode;
   - managed local inference mode, when a remote vLLM backend is desired.
10. Choose evolution targets:
    - natural-language memory;
    - skill bundle;
    - agent-system instruction.
11. Start the run.
12. Monitor live status, logs, transcripts, generated artifacts, evaluation
    output, and per-round evolution changes.
13. Review final outputs and reuse or export evolved artifacts in later projects.

The user does not choose a runtime image in this flow. Runtime images are an
internal compilation target or an advanced developer override.

## Local Desktop Architecture

OpenEvo Desktop is a Tauri application with a React UI and a local Python
sidecar.

The React UI handles:

- project creation and run configuration;
- remote profile management;
- progress views and artifact diff views;
- proxy and mirror settings;
- error presentation and guided remediation.

The Python sidecar handles:

- SSH command execution and file synchronization;
- SSH tunnel creation to remote localhost services;
- encrypted local vault access;
- local SQLite metadata for projects, remote profiles, and run history;
- compilation from Desktop-level Science Project config to lower-level OpenEvo
  experiment config;
- communication with the remote OpenEvo backend.

The local app stores secrets only in the encrypted vault. SQLite stores metadata
and references to vault entries, not raw API keys, SSH private keys, or tokens.

## Remote Server Architecture

OpenEvo Desktop manages a remote control backend over SSH. The backend binds to
remote localhost, and the Desktop reaches it through an SSH tunnel.

The remote server has two layers:

1. A long-lived OpenEvo remote backend responsible for preflight, installation,
   project workspace management, Docker Compose lifecycle, logs, and status.
2. Per-run service stacks that run Polar/OpenEvo components and optional model
   serving.

Each run uses an isolated compose project with its own state directories, ports,
artifact root, and logs. This allows concurrent runs as long as the user assigns
compatible GPU resources.

The remote backend should manage:

- pulling official OpenEvo images;
- creating and cleaning managed workspaces under `~/.openevo`;
- mounting the selected workspace into the run container;
- starting rollout, gateway, evolution backend, and worker processes;
- starting vLLM only for managed local inference mode;
- preserving run metadata needed for resume and debugging.

## Remote Bootstrap

The user should only need SSH access to a Linux GPU server. OpenEvo attempts to
install or configure the rest.

Preflight checks include:

- SSH connectivity and shell compatibility;
- available disk space;
- GPU visibility and driver presence;
- Docker availability and current user Docker access;
- Docker Compose availability;
- outbound network access or configured proxy;
- Codex CLI availability and subscription login state for subscription mode;
- access to official OpenEvo image registry;
- optional Hugging Face access for managed local inference.

OpenEvo can automatically:

- create `~/.openevo` directories;
- pull OpenEvo remote backend and runtime images;
- configure environment variables for proxy and mirrors;
- install user-space helpers where possible;
- start and update the remote backend container;
- upload local task folders into a managed remote workspace;
- clone Git repositories into managed workspaces.

OpenEvo should not silently make privileged system changes. If root access,
Docker group changes, driver installation, or firewall changes are required, it
reports the exact command or condition the user must handle.

## Network, Proxy, and Mirrors

Because remote servers may be in regions with restricted network access, proxy
configuration is a first-class Desktop setting.

A remote profile can define:

- HTTP proxy URL;
- HTTPS proxy URL;
- `NO_PROXY`;
- Docker registry mirror;
- Python package index URL;
- Hugging Face endpoint and cache directory;
- custom environment variables for remote containers.

The remote backend applies these settings consistently to:

- remote preflight checks;
- Docker pulls;
- Docker Compose services;
- Python package installation;
- Hugging Face model downloads;
- Codex or managed inference runtime commands where applicable.

Failures must distinguish between authentication, DNS, timeout, firewall,
registry, and disk-space cases when possible.

## Science Project Model

A Science Project is the user-facing unit. It contains:

- project name and description;
- remote profile reference;
- task source;
- workspace policy;
- research objective;
- optional setup commands;
- execution mode;
- model or subscription settings;
- evolution targets;
- run history;
- artifact history.

Task source types:

| Source | User Input | Remote Behavior |
|---|---|---|
| Local folder | local path | Desktop syncs files to a managed remote workspace. |
| Git repository | URL, branch, optional credentials | Remote backend clones or updates the repo. |
| Existing remote directory | absolute remote path | Remote backend mounts or copies the directory according to policy. |
| Scratch workspace | name only | Remote backend creates an empty workspace. |

Workspace policy defaults to managed copy. Directly mounting or mutating an
existing remote directory is an advanced option because it can alter user data.

## Environment Profiles

The UI should not ask ordinary users for a runtime image. Instead it exposes
environment profiles:

- `OpenEvo Managed Science Runtime`: default runtime with Codex, Python, Node,
  Git, common shell tools, package managers, and OpenEvo integration helpers.
- `Python Research Runtime`: a narrower science preset with Python data tools.
- `Custom Setup Commands`: user-provided commands run inside the workspace
  before the agent starts.
- `Advanced Custom Image`: developer option that maps directly to a runtime
  image.

The compiler maps environment profiles to Polar `RuntimeSpec` fields. For the
default profiles, image names and workdir paths are controlled by OpenEvo
release metadata, not by the user.

Setup commands are intentionally user-facing because arbitrary scientific
projects often have real dependency requirements. The difference is that the
user writes domain setup, not infrastructure setup.

## Execution Modes

The first Desktop release supports two Codex-based modes.

### Codex Subscription Transcript Mode

This mode uses the Codex subscription login state on the remote server.

Properties:

- no model API calls are made by OpenEvo directly;
- Codex runs inside the Polar/OpenEvo runtime as the harness;
- `agent.settings.auth_mode=subscription`;
- transcript capture is mandatory;
- token-level metrics are unavailable;
- supported evolution targets are text memory, skill bundle, and agent system;
- parametric memory is disabled.

OpenEvo manages a copied Codex home for runs instead of mutating the user's raw
remote `~/.codex`. The user can refresh that managed copy from a selected source
when needed.

### Managed Local Inference Mode

This mode starts a remote vLLM service and points Codex through the Polar
gateway proxy.

Properties:

- Codex still acts as the harness;
- Codex model provider points to the Polar gateway;
- the gateway forwards requests to vLLM and captures completion records;
- token-level trajectory data is available when the backend returns required
  fields;
- non-parametric evolution targets are supported;
- parametric memory can be added later once adapter lifecycle management is
  implemented.

The user chooses a Hugging Face model name at the Desktop level. The remote
backend handles model download, cache configuration, vLLM startup, readiness
checks, and failure reporting.

## Evolution View

The Desktop should show evolution as a first-class timeline rather than raw
backend records.

For each round, the UI shows:

- run status and duration;
- transcript summary;
- evaluation result;
- active artifact ids;
- generated memory diff;
- generated skill bundle files;
- agent-system diff;
- failure reason and retry action when applicable.

The UI should make it clear that subscription mode uses transcript-level
evolution only. It must not display token-level metrics for subscription runs.

## Benchmark and Developer Mode

Terminal Bench is not part of the ordinary science-user flow.

Developer mode can expose:

- Terminal Bench task selection;
- benchmark adapters;
- low-level runtime image fields;
- benchmark-specific Docker Compose files;
- per-task generators;
- custom artifact materializers;
- raw OpenEvo experiment YAML;
- direct Polar service URLs.

This separation keeps the ordinary Desktop usable while preserving the ability
for OpenEvo developers to build leaderboard workflows.

The existing Terminal Bench code path should be treated as a benchmark preset
owned by developer tooling. If it is surfaced in Desktop later, it appears under
Developer Mode, not as the default project creation path.

## Compilation to Existing OpenEvo/Polar

The Desktop does not replace the current OpenEvo runner. It compiles a Science
Project into existing OpenEvo experiment concepts, then relies on the remote
backend to run them.

Compilation responsibilities:

- task source becomes a remote workspace path;
- environment profile becomes `runtime.image`, `runtime.workdir`, and prepare
  commands;
- execution mode becomes agent auth, capture mode, builder strategy, and model
  backend settings;
- evolution targets become artifact controls and worker method configs;
- project/run metadata becomes task metadata for compatibility and context
  resolution;
- active artifacts from prior rounds become explicit context artifact ids.

This preserves the core OpenEvo rule: OpenEvo wraps an existing harness and
orchestrates Polar/evolution. It never becomes a direct chat-completion client.

## Error Handling

Errors should be categorized by the action the user can take:

| Category | Examples | UI Behavior |
|---|---|---|
| User action required | SSH auth failed, Docker permission denied, Codex not logged in | Show exact failing check and remediation. |
| OpenEvo can retry | transient network timeout, image pull timeout | Offer retry with current proxy settings. |
| Config issue | invalid model name, missing workspace path | Highlight the relevant project field. |
| Remote runtime issue | container failed, vLLM readiness timeout | Show logs, service name, and next action. |
| Evolution issue | worker failed, artifact invalid | Show job id, method, dataset, and error summary. |

The app should prefer partial progress over opaque failure. For example, a run
can fail after producing transcript artifacts; the UI should still show those
artifacts and allow the user to retry from the failed stage when supported.

## Security

Security requirements:

- remote backend listens on remote localhost by default;
- Desktop connects through SSH tunnels;
- secrets are stored locally only in an encrypted vault;
- remote project directories must not contain local vault secrets;
- uploaded local folders should respect ignore rules before transfer;
- existing remote directories are not mutated unless the user explicitly chooses
  a direct-mutation policy;
- managed Codex home copies are separated from raw user `~/.codex`;
- logs redact known secret values before display where possible.

## Testing

The implementation plan should include focused tests for:

- Science Project config validation;
- task source to workspace compilation;
- environment profile to runtime compilation;
- subscription mode enforcing transcript capture;
- managed local inference mode producing gateway/vLLM service config;
- proxy settings propagation into remote backend commands and containers;
- local-folder sync ignore behavior;
- remote preflight classification;
- Desktop-side API contract for run status and artifact timeline;
- developer-mode isolation of benchmark/runtime-image fields from ordinary user
  project creation.

Manual verification should cover:

- a fresh remote server bootstrap with Docker already available;
- a remote server requiring proxy settings;
- Codex subscription transcript run;
- local folder upload to managed workspace;
- Git repository science task;
- failure display for missing Codex login;
- failure display for image pull timeout.

## Open Questions

These questions do not block the first implementation plan, but should be
resolved before a public release:

- Which official base images are released for the first Desktop version?
- Which local folder ignore rules are enabled by default?
- Should Desktop support Windows as a controller after macOS/Linux are stable?
- How much of the remote backend update process is automatic versus
  user-confirmed?
- Which model-serving backends besides vLLM are worth supporting after the
  initial managed local inference mode?
